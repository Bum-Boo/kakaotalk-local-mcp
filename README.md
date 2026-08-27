# KakaoTalk Local MCP

[![CI](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4.svg)](#필요-환경)

Windows 카카오톡 PC 앱을 로컬 MCP 클라이언트와 연결하는 **비공식·로컬 우선 브리지**입니다. 사용자가 직접 허용한 채팅방만 다루며, 메시지 전송과 자동 답장은 기본적으로 비활성화되어 있습니다.

> [!WARNING]
> 이 프로젝트는 Kakao Corp.와 관계가 없으며 Kakao의 공식 제품이 아닙니다. 카카오톡 업데이트에 따라 기능이 중단될 수 있습니다. 사용 전 카카오톡 이용약관과 관련 법률을 직접 확인해 주세요.

## 주요 특징

- 허용 목록에 등록한 채팅방만 접근합니다.
- 외부에는 실제 방 제목 대신 사용자가 정한 불투명 `room_id`를 노출합니다.
- 최초 관찰 시 현재 상태를 기준선으로 저장하여 과거 대화를 신규 메시지로 재생하지 않습니다.
- 동일 메시지와 중복 작업을 fingerprint 및 idempotency 상태로 차단합니다.
- 답장 전송은 `prepare → 사용자 승인 → commit → readback` 순서를 따릅니다.
- `send_enabled`와 `auto_reply_enabled`의 기본값은 `false`입니다.
- 선택적으로 일정 후보를 로컬에서 선별하여 별도 일정 관리 에이전트에 전달할 수 있습니다.
- 선택적 backend watcher는 명시적으로 고른 소수의 방만 처리하며, raw key와 평문 데이터베이스를 파일로 저장하지 않습니다.
- 유휴 상태에서는 AI 모델을 호출하지 않습니다.

## 안전 경계

이 프로젝트는 다음 기능을 제공하지 않습니다.

- 카카오톡 계정 비밀번호·세션·인증정보 추출
- 비공개 네트워크 프로토콜 구현
- 무제한 전체 채팅방 수집
- 전체 대화 내보내기
- raw DB key 또는 평문 DB 저장
- 일괄 메시지 전송
- 승인 없는 자동 답장

로컬 MCP 서버를 인터넷이나 공용 네트워크에 직접 노출하지 마세요. 실제 설정, 상태 DB, 로그와 채팅 캡처는 Git 저장소 또는 클라우드 동기화 폴더에 올리지 않는 것을 권장합니다.

## 필요 환경

- Windows 10 또는 Windows 11
- 로그인된 카카오톡 PC 앱
- Python 3.11 이상
- PowerShell
- stdio MCP 서버를 실행할 수 있는 MCP 클라이언트

## 설치

PowerShell에서 저장소를 받은 뒤 설치 스크립트를 실행해 주세요.

```powershell
git clone https://github.com/Bum-Boo/kakaotalk-local-mcp.git
cd kakaotalk-local-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

설치 스크립트는 프로젝트 전용 `.venv`를 만들고, `config.json`이 없을 때만 안전한 예제 설정을 복사합니다.

## 기본 설정

`config.json`은 공개 저장소에 포함되지 않습니다. 처음에는 전송과 일정 자동화를 모두 끈 상태로 시작해 주세요.

```json
{
  "adapter": "win32",
  "send_enabled": false,
  "auto_reply_enabled": false,
  "schedule_automation_enabled": false,
  "backend_collector": null,
  "rooms": []
}
```

### 채팅방 등록

대상 채팅방을 별도 창으로 하나만 연 다음 아래 명령을 실행하면 방 제목을 콘솔에 표시하지 않고 등록할 수 있습니다.

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json adopt-open-room --room-id self-test
```

열린 채팅방이 정확히 하나가 아니면 설정을 바꾸지 않습니다. `room_id`는 MCP에서 사용할 로컬 별칭이며 실제 채팅방 제목과 달라도 됩니다.

설정 적용 후 다음 명령으로 검사해 주세요.

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json validate-config
.\scripts\doctor.cmd
```

## MCP 클라이언트 연결

MCP 클라이언트의 stdio 서버 설정에서 다음 실행 파일을 등록해 주세요. 실제 저장소 경로로 바꿔야 합니다.

```json
{
  "mcpServers": {
    "kakaotalk-local": {
      "command": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/d",
        "/s",
        "/c",
        "C:\\path\\to\\kakaotalk-local-mcp\\scripts\\run-mcp.cmd"
      ]
    }
  }
}
```

연결 후 먼저 `kakao_health`만 호출하여 로컬 브리지 상태와 전송 비활성 여부를 확인해 주세요.

## 제공 도구

| 도구 | 설명 |
|---|---|
| `kakao_health` | 메시지를 읽지 않고 실행 상태와 승인된 출처 별칭을 확인합니다. |
| `kakao_allowed_rooms` | 허용된 불투명 방 ID만 반환합니다. |
| `kakao_read_room` | 허용된 방의 제한된 최근 메시지와 fingerprint를 읽습니다. |
| `kakao_observe_room` | 기준선을 만들거나 신규 메시지 이벤트를 생성합니다. |
| `kakao_poll_events` | 로컬에 저장된 신규 이벤트를 가져옵니다. |
| `kakao_poll_schedule_candidates` | 분석 대기 중인 일정 후보를 가져옵니다. |
| `kakao_get_schedule_candidate` | 불투명 candidate ID로 후보 하나를 조회합니다. |
| `kakao_update_schedule_candidate` | 후보의 처리 상태를 기록합니다. |
| `kakao_prepare_reply` | 현재 fingerprint에 묶인 일회용 전송 승인을 준비합니다. |
| `kakao_commit_reply` | 승인된 초안을 한 번만 전송하고 결과를 다시 확인합니다. |
| `kakao_operation_status` | 준비된 작업의 현재 상태를 확인합니다. |

## 메시지 전송

실제 전송이 필요한 경우에도 다음 순서를 지켜 주세요.

1. `kakao_read_room`으로 최신 fingerprint를 확인합니다.
2. 보낼 초안을 사용자에게 보여 줍니다.
3. `kakao_prepare_reply`로 일회용 작업을 준비합니다.
4. 사용자가 현재 턴에서 명시적으로 승인합니다.
5. `kakao_commit_reply`를 한 번만 호출합니다.
6. 더 최신 메시지가 생겼거나 readback 결과가 불명확하면 자동 재시도하지 않습니다.

설정의 `send_enabled`가 `false`이면 commit 단계에서 전송하지 않습니다.

## 선택적 watcher

일반 UI watcher는 다음과 같이 실행할 수 있습니다.

```powershell
.\.venv\Scripts\hermes-kakao-watch.exe --once
.\.venv\Scripts\hermes-kakao-watch.exe
```

선택적 backend watcher는 별도로 승인한 방 ID와 현재 카카오톡 버전을 설정한 경우에만 사용해 주세요.

```json
{
  "backend_collector": {
    "enabled": true,
    "mode": "ram_only_v2",
    "room_ids": ["approved-room-one"],
    "max_batch_rows": 200,
    "bootstrap_retry_seconds": 30,
    "expected_client_version": "현재 검증한 버전"
  }
}
```

카카오톡 버전이 설정값과 다르면 backend watcher는 데이터 접근 전에 중단합니다.

## 개발 및 검증

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python tests\smoke_mcp.py
```

GitHub Actions에서도 Windows와 Ubuntu, Python 3.11과 3.12 조합을 검사합니다.

## 제작자 표기 부탁드립니다

이 프로젝트를 사용한 글, 영상, 데모, 연구 또는 파생 프로젝트를 공개하실 때 아래처럼 제작자와 저장소를 함께 언급해 주시면 감사하겠습니다.

> Made with [KakaoTalk Local MCP](https://github.com/Bum-Boo/kakaotalk-local-mcp) by [@Bum-Boo](https://github.com/Bum-Boo)

MIT 라이선스가 요구하는 저작권·라이선스 고지는 반드시 유지해 주세요. 위 문구를 통한 공개 언급은 법적 조건을 추가하려는 것이 아니라, 프로젝트를 만든 사람과 원본 저장소를 찾을 수 있도록 부탁드리는 것입니다.

## 영감을 주신 프로젝트

다음 오픈소스 프로젝트의 아이디어와 선행 작업에서 영감을 받았습니다. 좋은 작업을 공개해 주신 제작자분들께 감사드립니다.

- [kronenz/kakaotalk-mcp](https://github.com/kronenz/kakaotalk-mcp) — Win32 창 탐색과 MCP 연결 방식
- [johklo/moltbot](https://github.com/johklo/moltbot) — 기준선, 메시지 fingerprint와 전송 전 재검증
- [channprj/kmsg](https://github.com/channprj/kmsg) — 로컬 별칭, 제한된 상태 관리와 fail-closed 설계
- [is-theo/kakao-cli-win](https://github.com/is-theo/kakao-cli-win) — Windows v2 SQLCipher 구조 연구의 출발점

참고한 revision과 라이선스 정보는 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 기록되어 있습니다. 위 프로젝트의 코드를 그대로 번들하거나 공식 지원을 받는다는 의미는 아닙니다.

## 개인정보·보안·라이선스

- 개인정보 처리 경계: [`PRIVACY.md`](PRIVACY.md)
- 취약점 신고와 위협 모델: [`SECURITY.md`](SECURITY.md)
- 제3자 고지: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
- 라이선스: [MIT](LICENSE)
