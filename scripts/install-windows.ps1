param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required and was not found on PATH"
}

uv sync --extra dev

$Config = Join-Path $Root "config.json"
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Root "config.example.json") $Config
    Write-Host "Created fail-closed config.json (no rooms, sending disabled)."
}

$env:HERMES_KAKAO_CONFIG = $Config
$env:PYTHONUTF8 = "1"
& (Join-Path $Root ".venv\Scripts\python.exe") -m hermes_kakao_mcp.cli validate-config
if ($LASTEXITCODE -ne 0) {
    throw "Configuration validation failed"
}
