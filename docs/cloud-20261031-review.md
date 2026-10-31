# 독립 보안·개인정보 리뷰 (2026-10-31, 클라우드 세션)

- 대상: `main` @ `a5eb4d2` (브랜치 `claude/kakaotalk-mcp-security-review-796dpn`)
- 범위: 허용 목록 강제, 전송 승인 게이트, 승인 재사용·우회, 로그·오류 출력 누출, 악의적 입력 처리
- 방식: 실제 코드 경로 추적 + 합성 데이터 재현 테스트(`review/test_review_repro.py`). 실제 계정, 네트워크, UI 자동화는 쓰지 않았습니다.
- 제약: Linux VM(Python 3.13.18)이라 Win32 경로는 정적 분석과 mock 모델링으로만 확인했습니다. `calendar_mcp.py`는 훑어보기만 했습니다.
- 프로덕션 코드, 워크플로, 의존성은 수정하지 않았습니다.

## 실행 결과

```bash
python3 -m venv <scratch>/venv && <scratch>/venv/bin/pip install -e ".[dev]"
ruff check .                         # All checks passed!
pytest                               # 95 passed (기존 tests/)
python tests/smoke_mcp.py            # 정상 종료, health_is_error=false
pytest review/test_review_repro.py   # 11 passed (아래 재현이 모두 "관찰된 동작"을 단언)
```

`review/`는 `testpaths = ["tests"]` 밖에 있어서 기존 CI 수집 대상에 들어가지 않습니다. 각 테스트는 현재의 결함 동작을 단언하므로, 고친 뒤에는 기대 동작에 맞게 단언을 뒤집어야 합니다.

## 요약

| ID | 분류 | 심각도 | 내용 |
|---|---|---|---|
| F1 | 확인된 결함 | P1(조건부) | 호출자가 같은 `idempotency_key`를 다시 쓰면 다른 방의 이전 초안 작업이 반환되고, 그 초안이 전송됨 |
| F2 | 확인된 결함(mock 모델) | P2 | 닫혀 있던 방은 commit 단계에서 전송되지 않고 `send_unknown`으로 소진됨 |
| F4 | 확인된 결함 | P2 | 허용되지 않은 방의 후보 100건 이상이 허용된 방 후보를 영구히 가림 |
| F7 | 정적 위험(설계) | P2 | 승인 코드를 같은 호출자에게 돌려주므로 사람 승인이 코드로 강제되지 않음 |
| F5 | 확인된 결함(파서) | P2 | 여러 줄 메시지 본문으로 `[나] [시각]` 발신자를 위조할 수 있음 |
| R1 | 정적 위험 | P2 | 허용 목록의 신원이 "표시 제목"뿐이어서 같은 제목의 다른 방과 구분되지 않음(Win32 경로) |
| F3 | 확인된 결함 | P3 | 만료된 작업이 같은 키를 프로세스 재시작 전까지 막음 |
| F6 | 확인된 결함 | P3 | watcher webhook이 허용 목록에서 빠진 방의 이벤트도 전달함 |
| F8 | 정적 위험(완화됨) | P3 | 전송 뒤 commit 원장 기록이 실패하면 메모리 상태가 재commit을 허용함. fingerprint 재검사가 재전송을 막음 |
| F9b | 확인된 결함 | P3 | `calendar_event_id`의 길이·형식 검증 없음 |
| F10 | 정적 위험 | P3 | `get_service()` 예외가 `_call()` 정제 경로 밖에서 발생함 |
| R2 | 정적 위험 | P3 | 자동 열기로 인한 읽음 처리, 전체 방 발견 시 기본 활성화 |
| R3 | 정적 위험 | P3 | 보존 기한이 없는 상태(대기 중 후보, 메모리 내 작업 원문) |

## 상세

### F1. idempotency key 충돌로 다른 방에 다른 초안 전송 — 확인된 결함, P1(조건부)

- 위치: `src/hermes_kakao_mcp/operations.py:71-74`, `src/hermes_kakao_mcp/service.py:360-380`
- 발생 조건: `prepare_reply(room-a, fp_a, "A 초안", idempotency_key="reply-1")`(사용자가 거절)를 호출한 뒤 `prepare_reply(room-b, fp_b, "B 초안", idempotency_key="reply-1")`를 호출하는 경우
- 관찰: 두 번째 호출이 room-a의 작업과 확인 코드를 그대로 돌려줍니다. 이 값으로 commit하면 room-a에 "A 초안"이 전송됩니다(`test_f1_...`). 응답의 `room_id`는 `room-a`이지만 초안 원문은 응답에 없어서 LLM이나 사용자가 알아채기 어렵습니다.
- 기대: 다른 방이나 다른 본문에 같은 키를 쓰면 `idempotency_key_conflict` 오류를 내야 합니다.
- 조건: 기본 키(`room:fingerprint:text_hash`)를 쓸 때는 발생하지 않습니다. LLM이 `reply-1` 같은 일반 키를 넘길 때만 발생합니다.
- 수정 방향: `OperationStore.prepare`에서 기존 작업과 `room_id`, `expected_fingerprint`, `text_hash`가 모두 같을 때만 기존 작업을 반환하고, 하나라도 다르면 거부합니다. 또는 키 해시에 `room_id`와 `text_hash`를 항상 포함합니다.

### F2. 자동으로 연 방은 commit 단계에서 전송되지 않음 — 확인된 결함(mock 모델), P2

- 위치: `service.py:406`, `service.py:416`, `service.py:440`, `adapters/win32.py:601-627`, `adapters/win32.py:783`
- 원인: `_snapshot()`은 `room_session()` 안에서 방을 열고, 블록이 끝나면 닫습니다. 그 뒤 `send_text()`는 세션 밖에서 `_find_exact_room()`을 호출하므로 창이 없어 `room_not_open`이 발생합니다.
- 관찰(`test_f2_...`, Win32의 열기·닫기 동작을 mock으로 모델링): 아무것도 입력되지 않았는데도 작업이 `send_unknown`으로 기록됩니다. 사용자에게는 "결과 불명, 재시도 금지"로 안내됩니다. README 예시처럼 닫혀 있는 방에 답장하는 흐름은 실패합니다.
- 부수 문제: 재검사, 전송, readback이 서로 다른 세션에서 실행되므로 그 사이에 새 메시지가 도착할 수 있는 TOCTOU 창이 넓어집니다.
- 수정 방향: `_commit_reply_locked`의 재검사, `send_text`, readback을 하나의 `with self.adapter.room_session(room.title):` 블록으로 감쌉니다. 어댑터 락이 RLock이고 중첩 세션은 이미 열린 창을 재사용하므로 가능합니다. 전송 이전 단계에서 난 `room_not_open`은 `send_unknown`이 아니라 "전송 안 됨"으로 분류합니다.

### F4. 일정 후보 기아 — 확인된 결함, P2

- 위치: `service.py:258-266`, `state.py:477-495`
- 원인: SQL에서 상위 `limit`건(최대 100건)을 먼저 가져온 뒤 허용 방 필터를 적용합니다. 제거되었거나 비활성화된 방의 대기 후보가 100건 이상 앞에 있으면 허용된 방의 후보는 결코 반환되지 않습니다. poll은 상태를 바꾸지 않으므로 이 상태가 영구히 유지됩니다.
- 관찰(`test_f4_...`): `pending_analysis_count == 1`인데 `poll_schedule_candidates(limit=100)`은 0건을 반환합니다.
- 수정 방향: `pending_schedule_candidate_count`처럼 `room_id IN (...)` 조건을 SQL로 내립니다.

### F7. 승인은 권고일 뿐 코드로 강제되지 않음 — 정적 위험(설계), P2

- 위치: `operations.py:30-40`, `operations.py:78`, `service.py:380`, `server.py:142` (docstring의 "HARD GATE")
- 관찰(`test_f7_...`): `prepare_reply` 응답에 `confirmation_code`가 들어 있습니다. 같은 MCP 호출자가 사람의 개입 없이 바로 commit할 수 있습니다. 코드는 `operation_id`와 초안 해시에서 결정적으로 계산됩니다.
- 위험: SECURITY.md는 채팅 본문을 "신뢰할 수 없는 입력"으로 정의합니다. `send_enabled=true`이고 MCP 클라이언트가 도구 호출을 자동 승인하는 설정이면, 읽어 온 메시지 속 프롬프트 인젝션이 prepare에서 commit까지 이어질 수 있습니다. 실제 방어선은 `send_enabled`와 클라이언트의 도구 승인 UI 두 가지뿐입니다.
- 수정 방향(택1 또는 조합):
  - 확인 코드를 MCP 응답이 아니라 로컬 채널(콘솔, OS 알림, 별도 CLI `approve <op>`)로만 보여 줍니다.
  - commit 전에 로컬 사람 승인 플래그를 요구합니다.
  - 최소한 README와 SECURITY에 "승인 강제는 MCP 클라이언트의 도구 승인에 의존한다"고 명시하고, `kakao_commit_reply`의 자동 승인을 금지하도록 안내합니다.

### F5. 여러 줄 본문으로 발신자 위조 — 확인된 결함(파서), P2

- 위치: `parser.py:172-183` (모든 줄에 `MESSAGE_PATTERN`을 적용)
- 발생 조건: 상대방이 둘째 줄이 `[나] [오전 10:01] 좋아요, 송금할게요`인 여러 줄 메시지를 보내는 경우
- 관찰(`test_f5_...`):
  - `read_room`이 이 줄을 `sender="나"`인 별도 메시지로 반환합니다.
  - `observe_room`은 이 줄을 자기 메시지로 보고 이벤트에서 제외합니다(`service.py:221`).
  - 일정 후보 context의 `sender_role`도 `self`로 위조됩니다.
- 영향:
  - LLM이 "내가 이미 동의했다"고 오인할 수 있습니다.
  - 상대가 보낸 본문 일부가 감시 이벤트에서 누락됩니다.
  - readback 검증(`service.py:441-447`)은 발신자와 본문이 같으면 성공하므로, 상대가 정확한 본문을 예측하면 이론적으로 위조할 수 있습니다.
  - backend 경로는 `authorId`를 쓰므로 영향이 없습니다.
- 수정 방향: 클립보드 형식만으로는 완전한 해결이 어렵습니다. 대신 다음을 권합니다.
  - 같은 시각 블록 안에서 발신자가 바뀌는 등 의심스러운 경우 `ambiguous` 플래그를 붙입니다.
  - readback은 "전송 직전 마지막 메시지 다음에 정확히 1건 추가"를 확인합니다.
  - MCP 도구 설명에 `sender`가 위조될 수 있다는 점을 명시합니다.

### R1. 표시 제목 기반 방 신원 — 정적 위험, P2

- 위치: `adapters/win32.py:104-120`, `adapters/win32.py:250-395`, `config.py:153`
- 내용: 허용 목록은 표시 제목 문자열로만 방을 식별합니다.
  - 같은 이름의 친구 두 명과의 1:1 방처럼 제목이 같은 다른 방이 있으면, 창이 하나만 열려 있을 때 그 방을 허용된 방으로 간주해 읽고 보냅니다.
  - 자동 열기는 검색 결과에서 제목이 일치하는 첫 행을 엽니다.
  - backend 경로는 제목이 모호하면 실패로 닫힙니다(`backend_v2.py:458-468`). Win32 경로에는 이런 검사가 없습니다.
- 수정 방향:
  - 검색 결과에 제목이 정확히 일치하는 행이 2개 이상이면 `ambiguous_room`으로 거부합니다.
  - 등록 시 transcript 헤더의 `member_count`를 저장해 두고 읽을 때마다 대조합니다.
  - 이 한계를 문서화합니다.

### F3. 만료된 작업이 같은 키를 계속 막음 — 확인된 결함, P3

- 위치: `operations.py:50-59` (`status == "prepared"`인 작업만 purge), `operations.py:100-102` (commit 시 `expired`로 변경)
- 관찰(`test_f3_...`): 만료 뒤 commit을 시도하면 상태가 `expired`가 되고, 이후 같은 방·fingerprint·본문으로 prepare하면 같은 만료 작업이 계속 반환됩니다. 프로세스를 재시작하기 전까지 다시 보낼 수 없습니다.
- 수정 방향: 만료된 작업은 상태와 관계없이 purge합니다. 또는 `prepare`에서 기존 작업이 `prepared`가 아니거나 만료되었으면 새 작업을 만듭니다.

### F6. watcher webhook이 비허용 방의 이벤트를 전달함 — 확인된 결함, P3

- 위치: `watcher.py:24` (`pending_events(limit=20)`에 방 필터 없음)
- 관찰(`test_f6_...`): 허용 목록에서 빠진 `removed-room`의 이벤트가 webhook으로 전달됩니다. 같은 이벤트가 MCP `poll_events`에서는 걸러집니다.
- 같은 계열 문제: `service.poll_events(room_id=None)`(`service.py:253-255`, `state.py:391-395`)는 비허용 방의 이벤트도 delivered로 표시한 뒤 버리고, limit을 먼저 적용한 뒤 필터하므로 실제 반환 건수가 줄어듭니다.
- 수정 방향: 허용된 방 ID 목록을 SQL 조건으로 넘기고, 필터를 통과한 이벤트만 delivered로 표시합니다.

### F8. 전송 뒤 원장 기록 실패 — 정적 위험(완화됨), P3

- 위치: `service.py:451-460`, `operations.py:103`
- 관찰(`test_f8_...`): `record_commit`이 실패(예: `database is locked`)하면 메모리 상태는 `sent_verified`로 남습니다. 이 상태는 `require_for_commit`의 허용 목록에 들어 있습니다. DB에는 기록이 없어 다시 commit하면 `committed_operation` 검사를 통과합니다. 지금은 fingerprint 재검사(`stale_draft`) 덕분에 재전송되지 않았습니다. 다만 UI에 아직 보이지 않는 전송 직후처럼 fingerprint가 바뀌지 않은 경우에는 이중 전송 여지가 있습니다.
- 수정 방향: `send_text`가 성공하면 메모리 상태를 즉시 재commit 불가한 종결 상태로 바꿉니다. `require_for_commit`은 `prepared` 상태만 허용하고, `already_committed` 응답은 메모리 상태에서 만듭니다.

### F9. 악의적 입력 처리

- 이미 처리됨(`test_f9_hostile_inputs_fail_closed`):
  - `../room-a`, 대문자 ID, 공백이 붙은 ID, 빈 문자열, 실제 제목을 `room_id`로 넘기면 모두 `room_not_allowed`가 됩니다.
  - 7자리 코드는 `confirmation_mismatch`가 됩니다.
  - 비 ASCII 코드는 `compare_digest`에서 `TypeError`가 나고 `internal_error`로 실패해 닫힙니다(무해하지만 `confirmation_mismatch`로 정규화하는 편이 깔끔합니다).
  - 16진수가 아닌 fingerprint, 빈 본문, 길이 초과는 각각 거부됩니다.
  - 모든 SQL은 파라미터 바인딩을 씁니다. backend의 f-string SQL은 고정 후보 중에서 고른 컬럼명만 넣고, 대상도 메모리 내 읽기 전용 DB입니다.
- F9b. 확인된 결함, P3: `kakao_update_schedule_candidate`의 `calendar_event_id`는 길이·형식 검증 없이 200,000자도 저장됩니다(`service.py:310-328`, `state.py:529-562`). `calendar_mcp.EVENT_ID_PATTERN`과 같은 검증을 적용하는 것을 권합니다.

### F10. 서비스 생성 예외가 정제 경로 밖에서 발생 — 정적 위험, P3

- 위치: `server.py:43`, `server.py:49`, `server.py:60` 등. `_call(get_service().health)`에서 `get_service()`가 `_call` 밖에서 평가됩니다.
- 관찰: 설정 파일이 없으면 FastMCP가 `ToolError: Error executing tool kakao_health: Kakao MCP configuration file was not found`를 반환합니다. 메시지에만 그치고 `details.path`는 노출되지 않아 지금은 누출이 없습니다. 하지만 앞으로 `str(exc)`에 경로 등을 담는 예외가 생기면 정제 없이 클라이언트로 전달됩니다.
- 수정 방향: `_call(lambda: get_service().health())`처럼 서비스 생성까지 `_call` 안으로 옮깁니다.

### R2. 자동 열기와 전체 방 발견의 부작용 — 정적 위험, P3

- 닫힌 방에 `read_room`이나 `observe_room`을 호출하면 KakaoTalk UI로 방을 엽니다(`win32.py:250-395`). 이때 미확인 메시지가 읽음 처리되어 상대에게 읽음 표시가 전달될 수 있습니다. README와 PRIVACY에는 이 내용이 없습니다.
- `discover-all-rooms`는 모든 방을 하나씩 열고, `--apply`는 발견한 모든 방을 `enabled: true`로 허용 목록에 추가합니다(`config.py:440-450`). "모든 읽기 가능한 방은 명시적으로 허용된다"는 불변식과 긴장 관계입니다. 사용자가 직접 실행하는 CLI이므로 P3로 분류했습니다.
- `classify-room-policies --apply`는 제목 정규식으로 분류해 `auto_` 방도 backend 감시 대상(최대 16개)에 넣을 수 있습니다(`room_policy.py`의 `_managed_copy`).
- 수정 방향: 발견한 방은 기본값을 `enabled: false`로 둡니다. 읽음 처리 부작용을 문서화하고, 방별 `allow_auto_open` 옵션(기본 false)을 검토합니다.

### R3. 보존 기한이 없는 상태 — 정적 위험, P3

- `pending_analysis`와 `needs_user_choice` 상태의 후보는 본문 context를 담은 채 purge되지 않습니다. 종결 상태만 삭제됩니다(`state.py:570-582`).
- 이벤트 purge는 `observe_room`이 호출될 때만 실행됩니다(`service.py:237`). watcher가 멈추면 이벤트 본문이 남습니다.
- 메모리 내 `PreparedOperation`은 `prepared`가 아닌 상태가 되면 영구 보관되고 초안 원문을 계속 들고 있습니다(`operations.py:50-59`).
- 수정 방향: 비종결 후보에도 최대 보존 기간을 둡니다. purge를 MCP poll과 서버 시작 시에도 실행하고, TTL에 유예 시간을 더한 뒤에는 모든 작업을 메모리에서 제거합니다.

## 이미 처리됨 또는 문제없음

- 허용 목록 강제: `read_room`, `observe_room`, `prepare_reply`, `commit_reply`(commit 시 `_room` 재검사, `service.py:405`), `get_schedule_candidate`와 `update_schedule_candidate`, `poll_events` 필터가 모두 `_room()`이나 허용 ID 집합을 거칩니다.
- 전송 게이트:
  - `send_enabled`의 기본값은 false이고, commit의 첫 검사가 UI 접근보다 먼저 실행됩니다(`service.py:387`, 기존 테스트 `test_commit_is_blocked_when_sending_disabled`).
  - `auto_reply_enabled=true`는 설정 로드 단계에서 거부됩니다(`config.py:221`).
  - backend 감시는 `send_enabled=false`를 강제합니다(`config.py:282`, `backend_watcher.py:163`).
- 승인 재사용: 같은 작업을 두 번 commit하면 `already_committed`가 됩니다(DB 원장). 오래된 초안은 전송하지 않습니다(`stale_draft`). 만료는 `operation_expired`가 됩니다. 기존 테스트가 있습니다.
- 로그와 오류 출력:
  - watcher, backend watcher, CLI는 ID·개수·오류 코드만 출력합니다.
  - 예기치 않은 예외는 타입명만 출력합니다(`cli.py:232`, `backend_watcher.py:150-151`, `backend_watcher.py:278`).
  - MCP의 `_call`은 `internal_error`로 마스킹합니다.
  - `AdapterError.details`는 `room_id`와 개수만 담습니다.
  - `adopt-open-room`은 제목을 출력하지 않습니다.
  - 방 제목은 어느 출력 경로에도 나가지 않았습니다. `read_room` 응답에는 참여자 표시 이름과 본문이 들어 있는데, 이는 설계상 의도된 출력입니다.
- 클립보드: 서식이 있는 데이터나 텍스트가 아닌 데이터가 있으면 손대지 않고 실패합니다. 기존 테스트가 있습니다.
- webhook: loopback HTTP만 허용하고, HMAC 서명 비밀값은 환경변수에서 읽습니다.
- backend: 복호화한 DB는 메모리에서만 deserialize하고 키는 0으로 덮어씁니다(`backend_crypto.py:294-303`). 방 매핑이 모호하면 실패로 닫힙니다.

## 권장 우선순위

1. F1: idempotency 충돌 시 거부(작은 수정, 큰 효과)
2. F2: commit 전체를 단일 `room_session`으로 묶기
3. F7: 승인 경로를 로컬 채널로 분리하거나, 최소한 문서와 도구 설명에 명시
4. F4, F6: 허용 필터를 SQL로 내리기
5. 나머지 P3 항목
