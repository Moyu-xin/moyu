$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RuntimeScript = Join-Path $Root "scripts\local_runtime.ps1"
. $RuntimeScript

$PythonExe = Resolve-ProjectPython -RootPath $Root -SkipProjectVenv
$RestartService = Join-Path $Root "scripts\restart_service.py"
$Grok2ApiTunnel = Join-Path $Root "scripts\ensure_grok2api_tunnel.ps1"

try {
    & $Grok2ApiTunnel
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "grok2api SSH tunnel exited with code $LASTEXITCODE; continuing local service restart."
    }
}
catch {
    # Grok2api is an optional integration. A remote SSH/tunnel outage must not
    # prevent local registration and Plus services from loading new code.
    Write-Warning "grok2api SSH tunnel is unavailable; continuing local service restart: $($_.Exception.Message)"
}

& $PythonExe $RestartService $Root
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
