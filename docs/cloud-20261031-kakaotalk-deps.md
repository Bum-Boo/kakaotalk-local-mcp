# 의존성 업데이트 검증 보고서 (2026-10-31)

대상: Dependabot PR #2 `urllib3 2.7.0 → 2.8.0`, #3 `pyjwt 2.13.0 → 2.15.0`, #5 `mcp 1.29.0 → 1.30.0`.
브랜치 `claude/verify-dependency-updates-w8fwq2`(기준 `main` a5eb4d2)에서 세 PR의 `uv.lock` 변경을 그대로 적용하고 검증했습니다. 카카오톡·Google·웹훅 등 외부 서비스에는 접속하지 않았고 mock adapter와 기존 fake만 사용했습니다.

## 결론

| PR | 단독 병합 | 함께 병합 | 근거 |
|---|---|---|---|
| #2 urllib3 2.8.0 | 안전 | 안전 | 프로젝트 코드가 import하지 않는 간접 의존성(calendar extra → google-api-core → requests). 보안 수정 3건 포함 |
| #3 pyjwt 2.15.0 | 안전 | 안전 | mcp의 OAuth client-credentials 확장에서만 사용, 프로젝트 실행 경로에서는 로드되지 않음. 보안 수정 다수 포함 |
| #5 mcp 1.30.0 | 안전 | 안전 | stdio FastMCP 도구 등록·스키마·구조화 출력이 1.29.0과 바이트 단위로 동일. 변경은 HTTP/SSE/OAuth 경로에 집중 |

세 PR은 `uv.lock`의 서로 다른 hunk(711, 951, 1331행 부근)만 바꾸므로 순서와 무관하게 충돌 없이 병합됩니다. 의존성 목록(`Requires-Dist`)도 1.29.0과 1.30.0이 동일해 lock 재해석이 필요 없습니다(`uv lock --check` 통과).

## 변경 사항

- `uv.lock`: 세 PR과 동일한 버전·해시로 갱신(커밋 3개, 패키지별 1개).
- `tests/test_mcp_contract.py`(신규): 두 FastMCP 서버를 in-memory `ClientSession`으로 붙여 검증하는 회귀 테스트.
  - `:73` 도구 이름, 필수/선택 인자, `outputSchema`의 `type: object`·루트 `$ref` 부재를 고정(1.30의 `_inline_root_ref`·출력 스키마 `$ref` 해석 변경 대비).
  - `:89` `kakao_health`/`kakao_allowed_rooms`/`kakao_read_room`이 클라이언트 측 출력 스키마 검증(1.30에서 `referencing.Registry` 사용으로 바뀐 경로)을 통과하고 실제 방 제목이 노출되지 않음.
  - `:110` MCP를 거친 prepare → commit이 `send_enabled=false`에서 `sending_disabled`로 막히고 `MockKakaoAdapter.sent`가 비어 있음.
  - `:142` 필수 인자 누락 시 SDK가 `isError`로 거부.
  - `:153` `calendar_health`가 Google 클라이언트를 만들지 않음.
  - 이전에는 MCP 프로토콜 표면이 pytest 밖의 `tests/smoke_mcp.py`로만 확인되었습니다. 이 테스트가 실제로 회귀를 잡는지 확인하려고 `kakao_read_room`의 `max_messages` 기본값을 일부러 지워 보았고, 2개 테스트가 실패하는 것을 본 뒤 되돌렸습니다.

## 확인한 API 경로

- MCP 서버: `FastMCP(name, instructions=)` + `@app.tool()` + `app.run(transport="stdio")` — `src/hermes_kakao_mcp/server.py:13,40,155`, `src/hermes_kakao_mcp/calendar_mcp.py:333,406`.
  1.30의 `fastmcp/server.py` diff는 `session_idle_timeout`/`max_sessions`(Streamable HTTP 전용), SSE 본문 크기 제한, `BearerAuthBackend` 리소스 검증, `Settings.model_rebuild()`뿐입니다. `func_metadata.py`의 `_inline_root_ref`는 루트가 `$ref`인 스키마에만 작동하며, 이 프로젝트의 `dict[str, Any]` 반환은 `{"type":"object","additionalProperties":true}`이므로 영향이 없습니다. 15개 도구의 입력·출력 스키마와 설명을 1.29.0/1.30.0에서 덤프해 비교한 결과 동일했습니다.
- MCP 클라이언트: `ClientSession`, `stdio_client`, `StdioServerParameters` — `tests/smoke_mcp.py`, `tests/smoke_wsl_bridge.py`. `client/stdio`는 바뀌지 않았고, `session.py`의 출력 검증 변경은 위 테스트로 확인했습니다.
- JWT: 프로젝트는 `jwt`를 import하지 않습니다. 서버 모듈 import와 calendar 서비스 생성 뒤에도 `sys.modules`에 `jwt`가 없음을 확인했습니다. mcp 내부 사용처는 `mcp/client/auth/extensions/client_credentials.py`의 `jwt.encode`뿐이고, 합성 RSA/EC/HMAC 키로 2.13.0과 2.15.0에서 encode/decode 왕복을 확인했습니다.
- HTTP: 웹훅은 표준 라이브러리 `urllib.request`를 씁니다(`src/hermes_kakao_mcp/webhook.py:47-57`). urllib3과 무관하며 `tests/test_webhook.py`가 로컬 `HTTPServer`로 검증합니다. Calendar는 `googleapiclient.discovery.build`(`calendar_mcp.py:196-206`)를 호출하며 전송 계층은 `google_auth_httplib2.AuthorizedHttp`(httplib2)입니다. 합성 토큰으로 서비스를 생성한 뒤에도 `urllib3`/`requests`는 로드되지 않았습니다(네트워크 호출 없음). `requests 2.34.2` + `urllib3 2.8.0`의 import, `Session` 생성, `parse_url`도 경고 없이 동작했습니다.

## 업스트림 변경 요약과 분류

| 항목 | 분류 | 설명 |
|---|---|---|
| mcp 1.30 Streamable HTTP 유휴 세션 30분 만료, 최대 10,000 세션 | non-issue | stdio만 사용 |
| mcp 1.30 HTTP/SSE 클라이언트 cross-origin redirect 차단, OAuth issuer 검증 | non-issue | HTTP 클라이언트와 OAuth 미사용 |
| mcp 1.30 출력 스키마 `$ref`를 문서 내부에서만 해석 | already covered | `test_mcp_contract.py:73,89` |
| mcp 1.30 `Settings.model_rebuild()` | non-issue(개선) | 1.29에서 매 실행마다 나던 pydantic-settings `IncompleteFieldDefinitionWarning`(`lifespan`)이 사라짐 |
| pyjwt 2.14 HMAC 키 검증 강화, JWKS redirect 금지, `decode` options 변경 금지, 엄격한 compact JWS 디코딩 | non-issue | 프로젝트 경로에서 로드되지 않음. 보안상 업데이트 권장 |
| pyjwt 2.15 패딩(`=`)이 있는 세그먼트 거부 → 2.15.1에서 다시 허용 | static risk(낮음) | 이 프로젝트는 decode를 하지 않음. 2.15.1은 이번 범위 밖이라 올리지 않음 |
| urllib3 2.8 HTTPS 프록시 TLS 설정 분리(`proxy_ssl_context`), chunk 길이 무제한 버퍼링, deflate 무한 루프 수정 | non-issue | 프로젝트 경로에서 로드되지 않음. 보안상 업데이트 권장 |
| urllib3 2.8 RFC 3986 기반의 엄격한 host 파싱, 빈 `allowed_methods` deprecated | non-issue | 직접 사용하지 않음 |

## 그 밖의 발견

1. **static risk — CI가 `uv.lock`을 검증하지 않음.** `.github/workflows/ci.yml:25`는 `python -m pip install -e ".[dev]"`로 lock 없이 최신 버전을 설치합니다. 이 VM에서 같은 명령을 실행하면 `mcp 1.30.0`, `PyJWT 2.15.1`이 설치되었습니다. 따라서 Dependabot PR의 CI 녹색(PR #5 체크 8개 모두 success)은 lock 내용과 무관합니다. 반면 사용자 설치 스크립트 `scripts/install-windows.ps1:11`은 `uv sync --extra dev`로 lock을 사용합니다. calendar extra도 CI에 설치되지 않습니다(관련 테스트는 fake 사용).
2. **static risk — 선언된 하한 `mcp>=1.20`(`pyproject.toml:14`)이 현재 pydantic과 호환되지 않음.** mcp 1.20.0을 설치하면 `pydantic._internal._typing_extra.eval_type_backport` ImportError로 `calendar_mcp`/서버 import가 실패합니다(pytest 수집 오류 2건). 이번 브리프에서는 버전 범위를 바꿀 수 없어 수정하지 않았습니다.
3. **non-issue — 브리프의 "main 수집 오류 4건".** 의존성 없이 `pytest`만 설치한 환경에서 재현했습니다. `cryptography`가 없어 3건, `mcp`가 없어 1건입니다. 둘 다 선택 패키지가 아니라 필수 의존성이며, `uv sync --extra dev` 또는 `pip install -e ".[dev]"` 후에는 오류가 0건입니다.
4. **참고 — `ruff format --check .`는 main에서도 19개 파일을 재포맷 대상으로 보고합니다.** CI와 CONTRIBUTING은 `ruff check`만 요구하므로 손대지 않았습니다.

## 실행한 명령과 결과

공통: `UV_SYSTEM_CERTS=1`, Python 3.12.x(필요 시 3.11), uv 0.11.32, Linux.

| 명령 | main(before) | 브랜치(after) |
|---|---|---|
| `uv sync --all-extras --frozen -p 3.12` | 성공(mcp 1.29.0/pyjwt 2.13.0/urllib3 2.7.0) | 성공(1.30.0/2.15.0/2.8.0) |
| `uv run --frozen pytest` | 95 passed, 경고 1(pydantic-settings) | 101 passed, 경고 0 |
| `uv run --frozen ruff check .` | All checks passed | All checks passed |
| `uv run --frozen python tests/smoke_mcp.py` | 도구 11개, health/commit `isError=false` | 동일 |
| `uv build` | sdist + wheel 성공 | sdist + wheel 성공 |
| `uv lock --check` | — | 통과 |
| Python 3.11 venv에서 `uv sync --all-extras --frozen -p 3.11` 후 `pytest`, `smoke_mcp.py` | — | 101 passed, 스모크 동일 |
| CI 방식 `pip install -e ".[dev]"` 후 `pytest`(3.11) | 95 passed(실제 설치 mcp 1.30.0, PyJWT 2.15.1) | — |
| pytest만 있는 venv에서 `PYTHONPATH=src pytest` | 수집 오류 4건(위 3번) | — |
| 단독 적용: main lock + urllib3만 / pyjwt만 / mcp만 → `pytest`, `smoke_mcp.py` | — | 각각 101 passed, 스모크 통과 |
| 구버전 venv(mcp 1.29.0, pyjwt 2.13.0, urllib3 2.7.0) + 신규 테스트 | — | 101 passed, 도구 스키마 덤프가 1.30.0과 동일 |

## 이 VM에서 확인하지 못한 것

- Windows 실행 전반: `pywin32`/`comtypes` 기반 win32 adapter, PowerShell 설치 스크립트, `run-mcp.cmd`, 작업 스케줄러 등록.
- 실제 카카오톡 PC 앱, 실제 메시지 읽기와 전송, backend watcher(SQLCipher 대상 실제 DB).
- `tests/smoke_wsl_bridge.py`(WSL에서 Windows `cmd.exe`가 필요).
- Claude Desktop 등 실제 MCP 클라이언트와의 연결.
- 실제 Google Calendar OAuth 갱신과 API 호출, 실제 Hermes 웹훅 수신 서버.
- macOS(지원 대상 아님).

## 소유자 다음 단계

1. #2, #3, #5를 순서와 관계없이 병합하세요. 각 병합 뒤 Dependabot이 나머지 PR을 rebase합니다.
2. 이 브랜치의 `tests/test_mcp_contract.py`를 함께 받아 두면 이후 mcp 업데이트에서 도구 표면 변경이 pytest에서 바로 드러납니다.
3. CI가 lock을 검증하도록 Install 단계에 `uv sync --extra dev --frozen` 또는 `uv export --frozen | pip install -r -` 기반 job을 추가하는 것을 검토하세요(발견 1).
4. `mcp` 하한을 실제로 동작하는 버전으로 올리는 것을 검토하세요(예: 1.29 이상, 발견 2).
5. pyjwt 2.15.1이 나와 있으니 다음 Dependabot PR에서 받으면 됩니다(이 프로젝트에는 영향 없음).
6. Windows 실기기에서 `uv sync --extra dev` 후 `pytest`, `tests\smoke_mcp.py`, `kakao_health` 한 번을 확인하세요.
