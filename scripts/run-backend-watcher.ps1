param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Config = Join-Path $Root "config.json"

if (-not (Test-Path $Python)) {
    throw "Windows virtual environment is missing"
}
if (-not (Test-Path $Config)) {
    throw "Private config.json is missing"
}

$env:HERMES_KAKAO_CONFIG = $Config
$env:PYTHONUTF8 = "1"
& $Python -m hermes_kakao_mcp.backend_watcher --config $Config *> $null
exit $LASTEXITCODE
