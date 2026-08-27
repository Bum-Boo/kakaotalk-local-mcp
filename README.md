# KakaoTalk Local MCP

> **Unofficial community project.** This repository is not affiliated with or endorsed by
> Kakao Corp. It automates a user-owned Windows KakaoTalk session and may break when the client
> changes. Review Kakao's terms and your local law before use.

Windows 카카오톡 PC 앱을 Hermes에서 **허용된 방만** 읽고, 승인된 초안을 **한 번만** 전송하기 위한 제한형 stdio MCP 서버다.

## 현재 범위

- Windows KakaoTalk 실행 상태 확인
- 로컬 allowlist의 불투명 `room_id`만 공개
- 이미 열려 있는 정확한 제목의 방만 읽기
- 최초 관찰 시 baseline만 만들고 과거 메시지는 재생하지 않음
- 새 텍스트 fingerprint/event 저장(짧은 보존 기간)
- opt-in 방의 새 수신 메시지에서만 로컬 일정 후보 생성
- Schedule Manager에 후보 목록·단건 조회·상태 전이만 최소 공개
- 선택적 loopback Hermes webhook V2(HMAC-SHA256) 이벤트 전달
- `prepare → transcript CAS 재검증 → commit → readback` 전송
- `send_enabled: false` 기본값
- `auto_reply_enabled: false` 강제 및 일정 pipeline의 카카오 자동응답 금지
- 명시한 소수 방의 SQLCipher v2 DB/WAL을 RAM에서만 읽는 선택적 backend watcher
- backend 최초 max-log baseline, source-bound monotonic cursor, restart no-replay
- 검증된 KakaoTalk client version pin과 mismatch fail-closed
- Mock adapter와 MCP 프로토콜 smoke test

의도적으로 제외한 것:

- LOCO/비공개 네트워크 프로토콜
- 인증정보·세션 추출, raw DB key·평문 DB·전체 transcript 저장
- 임의 방 검색·전체 방 목록 노출
- 일괄전송·이미지·멘션
- 자동 재전송
- AI 호출 또는 API 키 저장

## 설치 (Windows PowerShell)

```powershell
cd C:\path\to\kakaotalk-local-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

설치 스크립트는 프로젝트 전용 `.venv`를 만들고 `config.json`이 없을 때만 안전한 기본 설정을 복사한다.

## 설정

`config.json`의 `rooms`에 직접 허용한 방만 넣는다. 제목은 **현재 열려 있는 카카오톡 채팅창의 정확한 제목**이어야 한다.

```json
{
  "adapter": "win32",
  "send_enabled": false,
  "auto_reply_enabled": false,
  "schedule_automation_enabled": false,
  "backend_collector": null,
  "rooms": [
    {
      "id": "self-test",
      "title": "정확한 채팅창 제목",
      "my_name": "내 카카오톡 표시 이름",
      "enabled": true,
      "schedule_watch_enabled": false
    }
  ]
}
```

- `id`: Hermes에만 보이는 로컬 별칭이다.
- `title`: MCP 결과에 노출하지 않는다.
- `my_name`: watcher가 자기 메시지를 다시 이벤트로 만드는 루프를 막는다.
- `send_enabled`: 실제 전송 검증 전에는 `false`로 유지한다.
- `auto_reply_enabled`: 항상 `false`여야 한다. 일정 pipeline은 source chat에 답장하지 않는다.
- `schedule_automation_enabled`: Schedule Manager wake의 전역 opt-in이다. 이 값만으로 Calendar를 쓰지 않는다.
- `schedule_watch_enabled`: 해당 방의 **새 수신** 메시지를 로컬 일정 후보로 감지할지 정하는 방별 opt-in이다.
- 방 제목 중복과 미등록 `room_id`는 거부한다.

방 제목을 Telegram이나 명령 출력에 노출하지 않고 등록하려면 대상 채팅창만 별도 창으로 하나 열고 다음을 실행한다.

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json adopt-open-room --room-id self-test
```

열린 채팅창이 정확히 하나가 아니면 아무 설정도 바꾸지 않는다. 성공해도 방 제목은 출력하지 않고 `send_enabled`를 강제로 `false`로 둔다. watcher까지 사용할 때만 로컬에서 `--my-name "내 표시 이름"`을 함께 지정한다.

## 로컬 점검

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json validate-config
.\scripts\doctor.cmd
```

`doctor`는 메시지를 읽지 않고 카카오톡 실행 여부와 열린 채팅창 수만 반환한다.

## MCP 도구

| 도구 | 동작 | 카카오 UI 접근 |
|---|---|---|
| `kakao_health` | 브리지·프로세스 상태 | 없음 |
| `kakao_allowed_rooms` | 허용된 불투명 ID | 없음 |
| `kakao_read_room` | 최근 메시지와 fingerprint | 있음 |
| `kakao_observe_room` | baseline/새 메시지 이벤트 생성 | 있음 |
| `kakao_poll_events` | 로컬 이벤트 drain | 없음 |
| `kakao_poll_schedule_candidates` | pending 일정 후보 목록 | 없음 |
| `kakao_get_schedule_candidate` | opaque candidate 단건 조회 | 없음 |
| `kakao_update_schedule_candidate` | candidate 상태 기록 | 없음 |
| `kakao_prepare_reply` | fingerprint 재검증·1회용 승인 생성 | 읽기만 |
| `kakao_commit_reply` | 최종 재검증·1회 전송·readback | 있음 |
| `kakao_operation_status` | 초안 상태 조회 | 없음 |

### 안전한 전송 순서

1. `kakao_read_room`에서 `fingerprint`를 받는다.
2. 답장 초안을 생성한다.
3. `kakao_prepare_reply(room_id, fingerprint, text)`를 호출한다.
4. 사용자에게 초안과 `confirmation_code`를 보여 명시적으로 승인받는다.
5. 승인된 현재 턴에서만 `kakao_commit_reply`를 호출한다.
6. 최신 fingerprint가 달라졌으면 전송하지 않는다.
7. 전송 후 readback이 실패해도 자동 재시도하지 않는다.
8. UI 입력 결과가 불명확하면 operation은 `send_unknown`으로 종료되어 재사용할 수 없다.

실제 전송은 로컬 `config.json`의 `send_enabled`가 `true`일 때만 열린다.

## 로컬 watcher

```powershell
.\.venv\Scripts\hermes-kakao-watch.exe --once
.\.venv\Scripts\hermes-kakao-watch.exe
```

watcher 자체는 AI를 호출하지 않는다. SQLite에 room tail hash만 유지하고, 새 메시지 payload는 `event_retention_minutes` 동안만 보존한다. Hermes는 `kakao_poll_events`로 이벤트를 가져간다.

### 선택적 RAM-only backend watcher

Windows v2 backend는 사용자가 별도로 승인한 **명시적 room subset**만 감시한다. 전체 `rooms` allowlist가 자동으로 backend scope가 되지 않는다.

```json
{
  "send_enabled": false,
  "auto_reply_enabled": false,
  "backend_collector": {
    "enabled": true,
    "mode": "ram_only_v2",
    "room_ids": ["approved-room-one"],
    "max_batch_rows": 200,
    "bootstrap_retry_seconds": 30,
    "expected_client_version": "26.7.1.5263"
  }
}
```

```powershell
# 현재 history를 baseline으로만 잡는 1회 검증
.\.venv\Scripts\hermes-kakao-backend-watch.exe --config .\config.json --once

# 상태는 본문·방 제목 없이 count/status만 반환
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json backend-status

# 로그인 세션에서 상시 실행 / 정확한 task만 rollback
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-backend-watcher-task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall-backend-watcher-task.ps1
```

- bootstrap은 방을 하나씩 열어 exact DB key만 찾고 bridge가 연 창을 즉시 닫는다.
- key는 watcher RAM에만 유지하며 재시작할 때 다시 bootstrap한다.
- encrypted DB와 committed WAL은 안정된 snapshot으로 읽고 평문 DB를 파일로 쓰지 않는다.
- 첫 실행은 현재 최대 log ID만 저장해 과거 대화를 후보로 재생하지 않는다.
- 빈 값·인사-only를 제외한 새 수신 text는 후보 queue에 들어가며, agent/model은 queue가 비어 있을 때 호출되지 않는다.
- KakaoTalk version이 config pin과 다르면 key scan 전에 중단한다.

### 일정 후보와 Schedule Manager wake

일정 감지는 카카오 읽기/전송과 별도인 local-only 경로다.

```text
새 incoming text (방별 opt-in)
→ 빈 값·인사-only local gate (그 외 내용 제외 목록 없음)
→ bounded local candidate queue
→ 2분 no-agent count trigger
→ 후보가 있을 때만 Schedule Manager one-shot job
```

- self message, media/system message, 최초 baseline, 중복 fingerprint, 인사-only text는 후보가 될 수 없다. 광고·일반 대화 등 별도의 내용 제외 목록은 두지 않고 Schedule Manager가 일정 여부를 판정한다.
- trigger는 transcript·room title을 읽지 않고 pending count와 automation opt-in만 확인한다.
- source는 `scripts/kakao_schedule_candidate_trigger.py`에 버전 관리하며, Schedule Manager profile의 launcher는 이 파일만 실행한다.
- `pending_analysis`만 poll한다. 질문이 필요한 후보는 `needs_user_choice`로 옮겨 반복 질문을 막고, opaque `candidate_id`로만 재개한다.
- one-shot job은 `kakao-schedule-ingest`와 `schedule-calendar` MCP만 받는다. browser·terminal·raw Google API 접근은 없다.
- `schedule-calendar`은 Schedule Manager profile-local primary Google Calendar credential을 직접 복사하지 않고 사용한다. 도구 surface는 busy time block 조회, candidate-bound event create, create 후 readback뿐이며, 수정·삭제·attendee·description·다른 Calendar 선택은 제공하지 않는다.
- 자동 등록은 제목·연도 포함 날짜·시작/종료 또는 종일 의미·KST·참석/의무가 모두 명확하고 선택지·비용·외부 회신·충돌이 없을 때만 가능하다. timed candidate는 KST range로, 종일 candidate는 명시 `all_day=true`의 Korea-day range로 busy block을 먼저 확인한다. opaque `candidate_id`를 private extended property로 저장해 중복 생성을 막으며, 반환 event ID의 readback 뒤에만 `registered`로 전이한다.
- 불명확한 일정·선택지·참석·충돌·비용·외부 회신·Calendar 도구 오류가 있으면 Calendar를 만들지 않고 Telegram에서 사용자에게 물어야 한다.
- 이 경로는 `kakao_prepare_reply`나 `kakao_commit_reply`를 호출하지 않는다.

### 선택적 signed webhook

유휴 agent polling 없이 새 이벤트에서만 Hermes를 깨우려면 `webhook`을 설정할 수 있다. 외부 URL은 거부하고 Windows에서 접근 가능한 loopback HTTP만 허용한다.

```json
{
  "webhook": {
    "url": "http://127.0.0.1:8644/webhooks/kakao-reply",
    "secret_env": "HERMES_KAKAO_WEBHOOK_SECRET",
    "event_type": "kakao.message",
    "timeout_seconds": 5
  }
}
```

- secret 값은 `config.json`이나 Git에 넣지 않고 watcher 프로세스 환경으로만 전달한다.
- 요청은 Hermes generic webhook V2 규격인 `<timestamp>.<body>` HMAC-SHA256으로 서명한다.
- 동일 이벤트는 고정 `X-Request-ID`를 사용하며, HTTP 성공 뒤에만 로컬 delivered 상태로 바뀐다.
- webhook 소비와 `kakao_poll_events` 소비를 동시에 운용하지 않는다.
- 기본값은 `null`이며 별도 설정 전에는 네트워크 요청이 전혀 없다.

## Windows 동작 주의점

현재 KakaoTalk PC의 transcript 읽기는 카카오톡 자체 `Ctrl+A/Ctrl+C` 기능을 사용한다.

- 허용된 방이 닫혀 있으면 로컬 exact title로 메인 창에서 검색해 연다.
- 검색 결과의 top-level 창 제목이 allowlist title과 정확히 일치해야만 읽는다.
- stable snapshot 동안 한 번만 열고, bridge가 자동으로 연 창만 읽기 후 다시 닫는다.
- 사용자가 이미 열어 둔 방은 닫지 않는다. 전송은 계속 이미 열린 방만 허용한다.
- 잠깐 해당 방을 포커스하고 기존 foreground 창을 복원한다.
- 텍스트 clipboard는 복원한다.
- clipboard가 이미지 등 비텍스트 데이터면 훼손하지 않고 읽기를 거부한다.
- 마우스 커서는 움직이지 않는다.

## 개발 검증

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python tests\smoke_mcp.py
```

WSL에서 Windows stdio 브리지까지 확인하려면:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --extra dev
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run pytest
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run ruff check .
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run python tests/smoke_wsl_bridge.py
```

## 출처와 라이선스

프로젝트 자체는 MIT다. 설계에 참고한 MIT 프로젝트와 고정 commit은 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 기록돼 있다.

개인정보·데이터 경계는 [`PRIVACY.md`](PRIVACY.md), 취약점 신고와 위협 모델은
[`SECURITY.md`](SECURITY.md)를 확인한다. 실제 `config.json`, 상태 DB, 로그, 방 제목,
참여자 정보와 개인 Schedule Manager 구성은 이 저장소에 포함하면 안 된다.
