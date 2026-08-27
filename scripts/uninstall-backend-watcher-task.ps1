param(
    [string]$TaskName = "Hermes-Kakao-Backend-Watcher"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Config = Join-Path $Root "config.json"

function Get-BackendWatcherProcesses {
    $ModulePattern = "(?i)(^|\s)-m\s+hermes_kakao_mcp\.backend_watcher(\s|$)"
    $ConfigPattern = [regex]::Escape($Config)
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -like "python*.exe" -and
                $_.CommandLine -match $ModulePattern -and
                $_.CommandLine -match $ConfigPattern
            }
    )
}

function Stop-BackendWatcherProcesses {
    $Stopped = 0
    $Deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $Processes = @(Get-BackendWatcherProcesses)
        if ($Processes.Count -eq 0) {
            return $Stopped
        }
        foreach ($Process in $Processes) {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
            $Stopped += 1
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Backend watcher processes could not be stopped"
}

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Stopped = Stop-BackendWatcherProcesses
[pscustomobject]@{
    ok = $true
    removed = [bool]$Existing
    stopped_process_count = $Stopped
    watcher_process_count = @(Get-BackendWatcherProcesses).Count
} | ConvertTo-Json -Compress
