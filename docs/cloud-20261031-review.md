# 독립 보안·개인정보 리뷰 (2026-10-31, 클라우드 세션)

- 대상: `main` 기준 커밋 `a5eb4d2` (브랜치 `claude/kakaotalk-mcp-security-review-ogufca`)
- 범위: MCP 도구 → `KakaoService` → 상태 DB/어댑터/웹훅 경로. 허용 목록, 전송 승인, 승인 재사용, 로그 누출, 비정상 입력.
- 방법: 실제 코드 경로 추적 + 합성 데이터 재현 테스트(`review/test_review_repro.py`). 실제 계정·카카오톡 UI·외부 네트워크는 사용하지 않았습니다(웹훅 재현은 127.0.0.1 포트만 사용).
- 프로덕션 코드, 워크플로, 의존성은 수정하지 않았습니다.

## 실행 결과

| 명령 | 결과 |
|---|---|
| `python -m venv /tmp/claude-0/rv && /tmp/claude-0/rv/bin/pip install -e ".[dev]"` | 성공 (Linux, Python 3.13, mcp 1.30.0) |
| `ruff check .` | All checks passed |
| `pytest` | **95 passed**, 0 failed |
| `python tests/smoke_mcp.py` | exit 0 (`health_is_error: false`, `commit_transport_is_error: false`) |
| `pytest review -q -p no:cacheprovider` | **5 passed** (아래 R1–R5 현재 동작 재현) |

`review/`는 `pyproject.toml`의 `testpaths = ["tests"]` 밖이라 기본 `pytest`/CI에는 영향을 주지 않습니다. 재현 테스트는 **현재(결함) 동작**을 assert하므로, 수정 후에는 해당 테스트가 실패해야 정상입니다.

## 요약

| ID | 분류 | 심각도 | 내용 |
|---|---|---|---|
| R1 | 검증된 결함 | P1 | 사용자 지정 `idempotency_key` 재사용 시 다른 방/다른 본문의 기존 작업이 반환되어 엉뚱한 방으로 전송 |
| R2 | 검증된 설계 위험 | P1(설계) | 확인 코드가 commit을 호출하는 같은 MCP 호출자에게 반환되어 "명시적 사용자 승인"을 코드가 강제하지 못함 |
| R3 | 검증된 결함 | P2 | 닫혀 있던 방은 commit 시 전송이 항상 실패하는데 `send_unknown`으로 기록되고 idempotency 키가 소진됨 |
| R4 | 검증된 위험(환경 의존) | P2 | loopback 웹훅이 `HTTP_PROXY`/시스템 프록시를 따라가 메시지 본문이 프록시로 전송될 수 있음 |
| R5 | 검증된 결함 | P3 | 비활성화된 방의 오래된 일정 후보가 LIMIT을 차지해 허용된 방의 후보가 반환되지 않음 |
| S1 | 정적 위험 | P3 | 설정은 프로세스 시작 시 1회 로드(`lru_cache`) — 방 비활성화가 서버 재시작 전까지 반영되지 않음 |
| S2 | 정적 위험 | P3 | 방 식별이 창 제목 문자열에만 의존(1:1 방 제목은 상대 닉네임) |

## 상세

### R1. idempotency 키 재사용 → 다른 방/본문 작업 반환 (검증된 결함, P1)

- 위치: `src/hermes_kakao_mcp/operations.py:71-74`, 호출부 `src/hermes_kakao_mcp/service.py:360-380`
- 트리거: `prepare_reply("room-a", fp_a, "X", "shared-key")` 후 60초 TTL 안에(미커밋 상태) `prepare_reply("room-b", fp_b, "Y", "shared-key")`.
- 관찰: 두 번째 호출이 오류 없이 **첫 번째 작업(room-a, 본문 X)** 을 그대로 반환. 응답에는 `draft_sha256`만 있고 본문이 없어 호출자가 알아차리기 어려움. 사용자가 "Y를 B방에"를 승인하고 commit하면 **X가 A방으로 전송**됨(`test_r1_*`).
- 기대: 같은 키라도 `room_id`·`expected_fingerprint`·`text_hash`가 다르면 `idempotency_conflict`로 거부.
- 수정 방향: `OperationStore.prepare`에서 기존 작업의 `room_id/expected_fingerprint/text_hash`를 비교해 불일치 시 `ApprovalError`. 또는 내부 키를 `f"{room_id}:{text_hash}:{user_key}"`로 항상 바인딩.

### R2. 확인 코드가 호출자 자신에게 반환됨 (검증된 설계 위험, P1-설계)

- 위치: `src/hermes_kakao_mcp/operations.py:30-40`(`public()`에 `confirmation_code` 포함), `src/hermes_kakao_mcp/operations.py:78`(코드는 `operation_id`와 `draft_hash`에서 결정적으로 파생), `src/hermes_kakao_mcp/server.py:118-145`
- 트리거: `kakao_prepare_reply` 응답의 `confirmation_code`로 곧바로 `kakao_commit_reply` 호출.
- 관찰: 사람 개입 없이 `sent_verified`(`test_r2_*`). "현재 턴의 명시적 승인"은 도구 docstring/서버 instructions에만 있고 코드상 강제되지 않음. 프롬프트 인젝션(채팅 본문은 신뢰할 수 없는 입력 — `SECURITY.md`)으로 모델이 승인 없이 commit할 수 있음. `send_enabled=false`가 유일한 하드 게이트.
- 기대: 승인 비밀이 모델이 아닌 사람 채널로만 전달.
- 수정 방향: MCP elicitation(`Context.elicit`, 설치된 mcp 1.30 지원)으로 commit 시 본문·방 별칭을 사용자에게 직접 확인받거나, 확인 코드를 로컬 토스트/CLI에만 표시하고 응답에서 제거. 최소한 README에 "send_enabled=true면 모델이 단독으로 전송 가능"을 명시.

### R3. 닫힌 방 commit이 `send_unknown`으로 기록 (검증된 결함, P2)

- 위치: `src/hermes_kakao_mcp/service.py:406`(`_snapshot`이 `room_session`을 열고 **닫은 뒤**) → `service.py:416`(`send_text`는 세션 밖) → `src/hermes_kakao_mcp/adapters/win32.py:783`(`_find_exact_room`, 자동 열기 없음) → `service.py:417-433`
- 트리거: 허용된 방이 열려 있지 않은 상태에서 read → prepare → commit (Win32는 `room_session`이 자동으로 열고 닫음; 테스트는 이를 흉내 낸 `AutoOpenAdapter`).
- 관찰: `room_not_open` 예외, 실제 입력 없음(`adapter.sent == []`), 그러나 작업 상태 `send_unknown`, commits 테이블에 기록되어 같은 키로 재준비 시 `already_committed`(`test_r3_*`). 사용 예시의 흐름이 방을 미리 열어 두지 않으면 동작하지 않음. 또한 fingerprint 재검증과 입력이 서로 다른 창 세션에서 일어나 TOCTOU 창이 존재.
- 기대: 재검증 → 입력 → readback을 하나의 `room_session` 안에서 수행하거나, 입력 전 단계 실패는 `send_not_attempted`로 구분하고 커밋 기록을 남기지 않음.
- 수정 방향: `_commit_reply_locked`에서 `with self.adapter.room_session(room.title):`로 재검증과 `send_text`를 감싸고, `send_text` 내부의 사전 검사(`room_not_open`, `input_not_empty`, `control_not_found`)를 "미시도" 오류로 분리.

### R4. loopback 웹훅이 프록시 환경을 따름 (검증된 위험, P2, 환경 의존)

- 위치: `src/hermes_kakao_mcp/webhook.py:46-48`(`urllib.request.urlopen` 기본 opener), 검증은 `src/hermes_kakao_mcp/config.py:308-313`(URL 호스트만 검사)
- 트리거: `http_proxy`가 설정되어 있고 `no_proxy`에 loopback이 없는 환경에서 watcher가 이벤트 전달.
- 관찰: 요청이 대상 127.0.0.1 서버가 아니라 **프록시로 전송**되고 본문(`messages[].text`)이 프록시에 그대로 도착(`test_r4_*`, 프록시도 로컬 포트로 대체). Windows에서는 레지스트리 프록시 설정도 읽음. 또한 기본 opener는 301/302/303 리다이렉트를 임의 호스트로 따라감(본문은 제거되지만 서명·요청 ID 헤더는 전달).
- 기대: loopback 전용 웹훅은 프록시·리다이렉트 없이 직접 연결.
- 수정 방향: `urllib.request.build_opener(urllib.request.ProxyHandler({}), <리다이렉트 거부 핸들러>)` 사용, 또는 `http.client.HTTPConnection`으로 직접 연결.

### R5. 비활성 방 후보가 허용 후보를 가림 (검증된 결함, P3)

- 위치: `src/hermes_kakao_mcp/state.py:484-495`(LIMIT 후) → `src/hermes_kakao_mcp/service.py:259-265`(Python 필터)
- 트리거: 비활성화된 방의 `pending_analysis` 후보가 `limit`개 이상 남아 있음.
- 관찰: `poll_schedule_candidates(limit=3)` → 0건, 그러나 `pending_analysis_count`는 1(`test_r5_*`). 데이터 누출은 없음(필터는 정상).
- 같은 패턴: `service.py:253-255`/`state.py:391-395`의 `poll_events`는 비허용 방 이벤트를 반환하지 않으면서 **delivered로 표시**해 조용히 소비함(누출 아님, 재활성화 시 손실).
- 수정 방향: 허용 room_id 목록을 SQL `WHERE room_id IN (...)`로 전달(`pending_schedule_candidate_count`가 이미 같은 방식).

### S1. 허용 목록 변경이 재시작 전까지 미반영 (정적 위험, P3)

- 위치: `src/hermes_kakao_mcp/server.py:22-24`(`@lru_cache` 1회 로드)
- 트리거: 실행 중 `config.json`에서 방을 `enabled: false`로 변경하거나 `send_enabled`를 끔.
- 관찰(정적): 실행 중인 MCP 서버는 이전 허용 목록·`send_enabled`로 계속 동작.
- 수정 방향: 문서에 "설정 변경 후 MCP 서버 재시작 필수" 명시, 또는 commit 직전 `send_enabled`/방 enabled를 설정 파일에서 재확인.

### S2. 제목 문자열 기반 방 식별 (정적 위험, P3)

- 위치: `src/hermes_kakao_mcp/adapters/win32.py:104-120`, `win32.py:384-389`
- 관찰(정적): 동일 제목 창이 2개면 `ambiguous_room`으로 fail-closed(양호). 그러나 허용된 방이 열려 있지 않고 같은 제목의 다른 방(예: 상대가 닉네임을 허용 제목과 같게 변경한 1:1 방)만 열려 있으면 그 창이 대상이 됨. backend 경로는 `backend_v2.py:459-467`에서 유일 매핑을 요구하고 `source_hash`로 고정해 상대적으로 안전.
- 수정 방향: 문서화로 충분할 수 있음. 강화하려면 읽은 transcript 헤더의 참여자 수(`member_count`)를 설정값과 비교하는 등 보조 신호 추가.

## 이미 보호됨 / 문제 없음

- **허용 목록 적용**: `read_room`, `observe_room`, `prepare_reply`, `commit_reply`(commit 시 `_room` 재검사, `service.py:405`), `poll_events`(room 지정 시 검사 + 결과 필터), `get/update_schedule_candidate`(후보의 room 재검사), watcher(`watcher.py:47-51`), backend(`config.py:267-276`) 모두 `_room`/설정 검증을 거침. 비허용 room_id는 `room_not_allowed`. — 이미 보호됨(`tests/test_service.py:291`).
- **승인 재사용**: 커밋 후 같은 operation → `already_committed`(state DB), `send_unknown`/`stale`/`expired` → `operation_not_pending`/`operation_expired`, 커밋된 idempotency 키로 재준비 거부. fingerprint 불일치 시 미전송. — 이미 보호됨(`tests/test_service.py:217-289`).
- **기록 실패 후 재시도**: 전송 후 `record_commit`이 실패하면 상태 DB에 기록이 없어도, 재시도 시 transcript fingerprint가 바뀌어 `stale_draft`로 차단됨. — 문제 없음(심층 방어).
- **로그/오류 출력**: 서버 `_call`은 예상 밖 예외를 일반 메시지로 치환(`server.py:27-37`), CLI·watcher·backend watcher는 예외 타입명/오류 코드만 출력, `observe_room`/watcher 출력에는 본문 없음, `read_room`은 transcript 헤더의 방 제목을 반환하지 않음, `health`/`allowed_rooms`에 제목 없음. 오류 details에는 room_id·개수만 포함. 예외: `config_not_found`가 로컬 설정 경로를 `details.path`로 반환(`config.py:117-121`) — 로컬 클라이언트 대상이라 문제 없음으로 분류.
- **비정상 입력**: 도구 인자는 FastMCP/pydantic 타입 검증. `max_messages` 1–50, `limit` 1–100으로 클램프, 빈 본문·길이 초과·잘못된 fingerprint(64 hex)·256자 초과 키 거부, 후보 상태는 화이트리스트와 전이 규칙으로 검증, SQL은 모두 파라미터 바인딩. 입력창에 기존 초안이 있으면 덮어쓰지 않음(`win32.py:785-790`). — 문제 없음.
- **설정 강제**: `auto_reply_enabled=true`는 로드 거부, backend 활성 시 `send_enabled=true` 거부, 웹훅은 loopback http만 허용. — 이미 보호됨(`tests/test_config.py`).

## 재현 명령

```bash
python3 -m venv /tmp/claude-0/rv
/tmp/claude-0/rv/bin/pip install -e ".[dev]"
/tmp/claude-0/rv/bin/ruff check .
/tmp/claude-0/rv/bin/pytest                      # 95 passed
/tmp/claude-0/rv/bin/python tests/smoke_mcp.py   # exit 0
/tmp/claude-0/rv/bin/pytest review -q -p no:cacheprovider   # 5 passed (R1–R5 재현)
```
