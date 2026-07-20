$ErrorActionPreference = "Stop"

$ControlMutex = New-Object System.Threading.Mutex($false, "Local\chatgpt2api-local-v16-control")
$HasControlLock = $false
try {
    $HasControlLock = $ControlMutex.WaitOne([TimeSpan]::FromSeconds(30))
    if (-not $HasControlLock) {
        throw "Timed out waiting for the local service control lock."
    }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeScript = Join-Path $Root "scripts\local_runtime.ps1"
. $RuntimeScript
$DataDir = Join-Path $Root "data"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null

$Grok2ApiTunnel = Join-Path $Root "scripts\ensure_grok2api_tunnel.ps1"
if (Test-Path -LiteralPath $Grok2ApiTunnel) {
    try {
        & $Grok2ApiTunnel
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "grok2api SSH tunnel exited with code $LASTEXITCODE; continuing local service startup."
        }
    }
    catch {
        # Grok2API is optional. Keep the local service available while making
        # the tunnel failure visible in the startup output and log file.
        Write-Warning "grok2api SSH tunnel is unavailable; continuing local service startup: $($_.Exception.Message)"
    }
}

$PidFile = Join-Path $DataDir "chatgpt2api-local.pid"
$ServeScript = Join-Path $Root "serve_local.py"
$Port = 8000

function Read-PidRecord {
    param([string]$Path, [string]$ExpectedScript, [int]$ExpectedPort)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $Raw = [string](Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue)
    $Raw = $Raw.Trim()
    if (-not $Raw) {
        return $null
    }
    try {
        $Record = $Raw | ConvertFrom-Json -ErrorAction Stop
        if ($Record.pid) {
            return $Record
        }
    } catch {
        # Legacy PID files contained only the numeric process id.
    }
    $FirstLine = ($Raw -split "`r?`n" | Select-Object -First 1)
    $LegacyProcessId = 0
    if ([int]::TryParse($FirstLine, [ref]$LegacyProcessId)) {
        return [pscustomobject]@{ pid = $LegacyProcessId; script = $ExpectedScript; port = $ExpectedPort }
    }
    return $null
}

function Get-OwnedProcess {
    param([object]$Record, [string]$ExpectedScript, [int]$ExpectedPort, [switch]$RequireListener)
    if (-not $Record -or -not $Record.pid) {
        return $null
    }
    $ProcessId = [int]$Record.pid
    if ($Record.script -and [System.IO.Path]::GetFullPath([string]$Record.script) -ne [System.IO.Path]::GetFullPath($ExpectedScript)) {
        return $null
    }
    if ($Record.port -and [int]$Record.port -ne $ExpectedPort) {
        return $null
    }
    $CimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $CimProcess) {
        return $null
    }
    $Expected = [System.IO.Path]::GetFullPath($ExpectedScript)
    $CommandLine = [string]$CimProcess.CommandLine
    if (-not $CommandLine -or $CommandLine.IndexOf($Expected, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $null
    }
    if ($Record.started_at) {
        try {
            $RecordedStart = [DateTimeOffset]::Parse([string]$Record.started_at).UtcDateTime
            $ActualStart = ([DateTime]$CimProcess.CreationDate).ToUniversalTime()
            if ([Math]::Abs(($ActualStart - $RecordedStart).TotalSeconds) -gt 10) {
                return $null
            }
        } catch {
            return $null
        }
    }
    if ($RequireListener) {
        $Listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { [int]$_.LocalPort -eq $ExpectedPort -and $_.OwningProcess -eq $ProcessId } | Select-Object -First 1
        if (-not $Listener) {
            return $null
        }
    }
    return (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Find-OwnedListenerRecord {
    param([string]$ExpectedScript, [int]$ExpectedPort)
    $Listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { [int]$_.LocalPort -eq $ExpectedPort } | Select-Object -First 1
    if (-not $Listener) {
        return $null
    }
    $ListenerProcessId = [int]$Listener.OwningProcess
    $CimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ListenerProcessId" -ErrorAction SilentlyContinue
    $Expected = [System.IO.Path]::GetFullPath($ExpectedScript)
    $CommandLine = [string]$CimProcess.CommandLine
    if ($CimProcess -and $CommandLine.IndexOf($Expected, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return [pscustomobject]@{
            pid = $ListenerProcessId
            started_at = ([DateTime]$CimProcess.CreationDate).ToUniversalTime().ToString("o")
            script = $Expected
            port = $ExpectedPort
        }
    }
    return $null
}

$ExistingRecord = Read-PidRecord -Path $PidFile -ExpectedScript $ServeScript -ExpectedPort $Port
if ($ExistingRecord) {
    $Existing = Get-OwnedProcess -Record $ExistingRecord -ExpectedScript $ServeScript -ExpectedPort $Port -RequireListener
    if ($Existing) {
        Write-Output "Already running: PID $($Existing.Id)"
        exit 0
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Output "Removed stale PID file."
}

$RecoveredRecord = Find-OwnedListenerRecord -ExpectedScript $ServeScript -ExpectedPort $Port
if ($RecoveredRecord) {
    $RecoveredRecord | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8
    Write-Output "Recovered running project process and PID record: PID $($RecoveredRecord.pid)"
    exit 0
}
$OccupiedListener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { [int]$_.LocalPort -eq $Port } | Select-Object -First 1
if ($OccupiedListener) {
    throw "Port $Port is already used by unverified PID $($OccupiedListener.OwningProcess)."
}

$Stdout = Join-Path $LogDir "local-server.out.log"
$Stderr = Join-Path $LogDir "local-server.err.log"
$ConfigFile = Join-Path $Root "config.json"
$WebIndex = Join-Path $Root "web_dist\index.html"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$SetupMarker = Join-Path $Root ".venv\.moyu-setup-complete"
$MissingSetupItems = @()
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    $MissingSetupItems += "config.json"
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $MissingSetupItems += ".venv"
}
if (-not (Test-Path -LiteralPath $WebIndex)) {
    $MissingSetupItems += "web_dist"
}
if (-not (Test-Path -LiteralPath $SetupMarker)) {
    $MissingSetupItems += "setup marker"
} else {
    $MarkerTime = (Get-Item -LiteralPath $SetupMarker).LastWriteTimeUtc
    $SetupInputs = @(
        (Join-Path $Root "pyproject.toml"),
        (Join-Path $Root "setup_local.ps1"),
        (Join-Path $Root "build_frontend.ps1"),
        (Join-Path $Root "web\package.json"),
        (Join-Path $Root "web\pnpm-lock.yaml"),
        (Join-Path $Root "web\next.config.ts")
    )
    $FrontendSource = Join-Path $Root "web\src"
    if (Test-Path -LiteralPath $FrontendSource) {
        $SetupInputs += Get-ChildItem -LiteralPath $FrontendSource -Recurse -File | Select-Object -ExpandProperty FullName
    }
    $HasNewerSource = $SetupInputs | Where-Object {
        (Test-Path -LiteralPath $_) -and (Get-Item -LiteralPath $_).LastWriteTimeUtc -gt $MarkerTime
    } | Select-Object -First 1
    if ($HasNewerSource) {
        $MissingSetupItems += "updated source files"
    }
}
if ($MissingSetupItems.Count -gt 0) {
    Write-Output "First-run setup required: $($MissingSetupItems -join ', ')"
    & (Join-Path $Root "setup_local.ps1")
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "First-run setup failed with exit code $LASTEXITCODE."
    }
}
$PythonExe = Resolve-ProjectPython -RootPath $Root

$ManagedCleanupScript = Join-Path $Root "scripts\cleanup_managed_mihomo.py"
if (Test-Path -LiteralPath $ManagedCleanupScript) {
    $CleanupOutput = @(& $PythonExe $ManagedCleanupScript 2>&1)
    if ($LASTEXITCODE -eq 0) {
        $CleanupOutput | Write-Output
    } else {
        Write-Output "Managed Mihomo startup cleanup skipped with exit code $LASTEXITCODE."
    }
}

$Process = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList @('"' + $ServeScript + '"') `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -PassThru

Start-Sleep -Milliseconds 500
$Deadline = (Get-Date).AddSeconds(15)
$ServerRecord = $null
do {
    $Process.Refresh()
    $ServerRecord = Find-OwnedListenerRecord -ExpectedScript $ServeScript -ExpectedPort $Port
    if (-not $ServerRecord) {
        Start-Sleep -Milliseconds 200
    }
} while (-not $ServerRecord -and (Get-Date) -lt $Deadline)

if (-not $ServerRecord) {
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
    throw "Server did not listen on port $Port within 15 seconds. Check $Stderr"
}

$ServerRecord | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8

Write-Output "Started ChatGPT register-only at http://127.0.0.1:8000/register/"
Write-Output "PID: $($ServerRecord.pid)"
Write-Output "Log files:"
Write-Output "  $Stdout"
Write-Output "  $Stderr"
} finally {
    if ($HasControlLock) {
        $ControlMutex.ReleaseMutex()
    }
    $ControlMutex.Dispose()
}
