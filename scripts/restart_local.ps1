$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogFile = Join-Path $Root "logs\restart.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

function Write-RestartLog {
    param([string]$Message)
    Add-Content -LiteralPath $LogFile -Encoding utf8 -Value "$(Get-Date -Format o) $Message"
}

Start-Sleep -Seconds 2
try {
    Write-RestartLog "Restart requested."
    & (Join-Path $Root "stop_local.ps1") | ForEach-Object { Write-RestartLog ([string]$_) }
    & (Join-Path $Root "start_local.ps1") | ForEach-Object { Write-RestartLog ([string]$_) }
    Write-RestartLog "Restart completed."
} catch {
    Write-RestartLog "Restart failed: $($_.Exception.Message)"
    exit 1
}
