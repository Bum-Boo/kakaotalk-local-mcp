@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
if not defined HERMES_KAKAO_CONFIG set "HERMES_KAKAO_CONFIG=%ROOT%\config.json"
"%ROOT%\.venv\Scripts\python.exe" -m hermes_kakao_mcp.server
exit /b %ERRORLEVEL%
