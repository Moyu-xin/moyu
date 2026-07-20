param(
    [string]$SshHost = "47.251.140.170",
    [int]$SshPort = 2222,
    [string]$SshUser = "root",
    [string]$KeyPath = "",
    [int]$LocalPort = 18084,
    [int]$RemotePort = 18084
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $KeyPath) {
    $KeyPath = Join-Path (Split-Path -Parent $Root) "tmp\ssh\opengpt.pem"
}

function Test-Grok2ApiHealth {
    try {
        $result = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/healthz" -TimeoutSec 3
        return [bool]$result.ok
    }
    catch {
        return $false
    }
}

if (Test-Grok2ApiHealth) {
    Write-Host "grok2api SSH tunnel is already healthy on 127.0.0.1:$LocalPort"
    exit 0
}

$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "Local port $LocalPort is already occupied, but grok2api health check failed."
}
if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "grok2api SSH key not found: $KeyPath"
}

$sshArgs = @(
    "-N",
    "-L", "${LocalPort}:127.0.0.1:${RemotePort}",
    "-i", $KeyPath,
    "-p", $SshPort,
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "${SshUser}@${SshHost}"
)

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StdoutLog = Join-Path $LogDir "grok2api-tunnel.out.log"
$StderrLog = Join-Path $LogDir "grok2api-tunnel.err.log"
$sshTarget = $sshArgs[-1]
$sshArgs = @($sshArgs[0..($sshArgs.Count - 2)]) + @("-E", $StderrLog, $sshTarget)
$LastFailure = ""

for ($attempt = 1; $attempt -le 3; $attempt++) {
    Remove-Item -LiteralPath $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue
    $sshExe = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
    $process = Start-Process `
        -FilePath $sshExe `
        -ArgumentList $sshArgs `
        -WindowStyle Hidden `
        -PassThru
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) {
            $sshError = if (Test-Path -LiteralPath $StderrLog) {
                ((Get-Content -LiteralPath $StderrLog -Tail 8 -ErrorAction SilentlyContinue) -join " ").Trim()
            } else { "" }
            $errorSuffix = if ($sshError) { ": $sshError" } else { "" }
            $LastFailure = "attempt $attempt exited $($process.ExitCode)$errorSuffix"
            break
        }
        if (Test-Grok2ApiHealth) {
            Write-Host "grok2api SSH tunnel started on 127.0.0.1:$LocalPort (PID $($process.Id), attempt $attempt)"
            exit 0
        }
    }
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $LastFailure = "attempt $attempt timed out waiting for /healthz"
    }
    if ($attempt -lt 3) {
        Start-Sleep -Seconds 2
    }
}

throw "grok2api SSH tunnel did not become healthy after 3 attempts: $LastFailure"
