param(
    [string]$TaskName = "Hermes-Kakao-Backend-Watcher"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Config = Join-Path $Root "config.json"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Runner = Join-Path $Root "scripts\run-backend-watcher.ps1"
$StateDir = Join-Path $Root "state"

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

function Get-LogicalWatcherRoots([object[]]$Processes) {
    $ProcessIds = @{}
    foreach ($Process in $Processes) {
        $ProcessIds[[int]$Process.ProcessId] = $true
    }
    return @($Processes | Where-Object { -not $ProcessIds.ContainsKey([int]$_.ParentProcessId) })
}

function Stop-BackendWatcherProcesses {
    $Deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $Processes = @(Get-BackendWatcherProcesses)
        if ($Processes.Count -eq 0) {
            return
        }
        foreach ($Process in $Processes) {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Existing backend watcher processes could not be stopped"
}

foreach ($Required in @($Config, $Python, $Runner)) {
    if (-not (Test-Path $Required)) {
        throw "Required backend watcher file is missing"
    }
}

$env:HERMES_KAKAO_CONFIG = $Config
$env:PYTHONUTF8 = "1"
& $Python -m hermes_kakao_mcp.cli --config $Config validate-config | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Configuration validation failed"
}

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$Backup = $null
if ($Existing) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $Backup = Join-Path $StateDir "scheduled-task-$Stamp.xml"
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $Backup -Encoding UTF8
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Stop-BackendWatcherProcesses

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Runner + '"'
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$Deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    Start-Sleep -Milliseconds 250
    $Current = Get-ScheduledTask -TaskName $TaskName
    $Processes = @(Get-BackendWatcherProcesses)
    $LogicalRoots = @(Get-LogicalWatcherRoots $Processes)
    if ($Current.State -eq "Running" -and $LogicalRoots.Count -eq 1) {
        break
    }
} while ([DateTime]::UtcNow -lt $Deadline)

if ($Current.State -ne "Running" -or $LogicalRoots.Count -ne 1) {
    throw "Backend watcher did not reach one logical running instance"
}
[pscustomobject]@{
    ok = $true
    task_state = [string]$Current.State
    watcher_process_count = $LogicalRoots.Count
    watcher_python_process_count = $Processes.Count
    backup_created = [bool]$Backup
    send_enabled = $false
    auto_reply_enabled = $false
} | ConvertTo-Json -Compress
