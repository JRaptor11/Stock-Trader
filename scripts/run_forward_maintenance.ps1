param(
    [Parameter(Mandatory = $true)]
    [string]$AlpacaEnv,

    [Parameter(Mandatory = $true)]
    [string]$ResearchEnv,

    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$RuntimeRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'forward-maintenance-runtime'),
    [string]$MinimumRevision = '01c1105'
)

$ErrorActionPreference = 'Stop'
$logRoot = Join-Path $RuntimeRoot 'logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$logPath = Join-Path $logRoot "forward-maintenance-$stamp.log"

Push-Location -LiteralPath $RepoRoot
try {
    & python -m research.forward_maintenance_runner `
        --repo-root $RepoRoot `
        --workspace-root $WorkspaceRoot `
        --alpaca-env $AlpacaEnv `
        --token-env $ResearchEnv `
        --runtime-root $RuntimeRoot `
        --minimum-revision $MinimumRevision *>&1 |
        Tee-Object -LiteralPath $logPath
    $result = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($null -eq $result) {
    $result = 1
}
exit $result
