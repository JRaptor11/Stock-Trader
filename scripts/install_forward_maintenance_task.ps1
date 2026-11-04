param(
    [Parameter(Mandatory = $true)]
    [string]$AlpacaEnv,

    [Parameter(Mandatory = $true)]
    [string]$ResearchEnv,

    [string]$TaskName = 'StockTrader-ForwardMaintenance',
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$RuntimeRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'forward-maintenance-runtime')
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_forward_maintenance.ps1'
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Maintenance runner does not exist: $runner"
}
foreach ($required in @($AlpacaEnv, $ResearchEnv, $RepoRoot, $WorkspaceRoot)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required maintenance path does not exist: $required"
    }
}

$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $runner),
    '-AlpacaEnv', ('"{0}"' -f $AlpacaEnv),
    '-ResearchEnv', ('"{0}"' -f $ResearchEnv),
    '-RepoRoot', ('"{0}"' -f $RepoRoot),
    '-WorkspaceRoot', ('"{0}"' -f $WorkspaceRoot),
    '-RuntimeRoot', ('"{0}"' -f $RuntimeRoot)
) -join ' '

$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument $arguments `
    -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At 4:15PM
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Broker-free frozen forward maintenance with fail-closed pre/post guards.' `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    NextRunTime = $info.NextRunTime
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    StartWhenAvailable = $task.Settings.StartWhenAvailable
    WakeToRun = $task.Settings.WakeToRun
    MultipleInstances = $task.Settings.MultipleInstances
    ExecutionTimeLimit = $task.Settings.ExecutionTimeLimit
} | ConvertTo-Json -Depth 4
