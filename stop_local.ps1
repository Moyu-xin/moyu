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
$PidFile = Join-Path $Root "data\chatgpt2api-local.pid"
$ServeScript = Join-Path $Root "serve_local.py"
$DefaultPort = 8000

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
    $Port = if ($Record.port) { [int]$Record.port } else { $ExpectedPort }
    if ($Port -ne $ExpectedPort) {
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
        $Listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { [int]$_.LocalPort -eq $Port -and $_.OwningProcess -eq $ProcessId } | Select-Object -First 1
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

function Get-AuthKey {
    param([string]$RootPath)
    if ($env:CHATGPT2API_AUTH_KEY) {
        return $env:CHATGPT2API_AUTH_KEY.Trim()
    }
    $ConfigPath = Join-Path $RootPath "config.json"
    try {
        $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop
        return ([string]$Config.'auth-key').Trim()
    } catch {
        return ""
    }
}

$PidRecord = Read-PidRecord -Path $PidFile -ExpectedScript $ServeScript -ExpectedPort $DefaultPort
if (-not $PidRecord) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $PidRecord = Find-OwnedListenerRecord -ExpectedScript $ServeScript -ExpectedPort $DefaultPort
    if (-not $PidRecord) {
        Write-Output "No verified project process is listening on port $DefaultPort."
        exit 0
    }
    Write-Output "Recovered project process from port $DefaultPort because the PID file was missing or invalid."
}

$ProcessId = [int]$PidRecord.pid
$Process = Get-OwnedProcess -Record $PidRecord -ExpectedScript $ServeScript -ExpectedPort $DefaultPort
if ($Process) {
    $Port = if ($PidRecord.port) { [int]$PidRecord.port } else { $DefaultPort }
    $AuthKey = Get-AuthKey -RootPath $Root
    $ManagedCleanupSucceeded = $false
    try {
        if ($AuthKey) {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/proxy-pool/managed-mihomo/stop" -Headers @{ Authorization = "Bearer $AuthKey" } -TimeoutSec 15 | Out-Null
            $ManagedCleanupSucceeded = $true
        } else {
            Write-Output "Managed Mihomo cleanup skipped: auth key is not configured."
        }
    } catch {
        Write-Output "Managed Mihomo cleanup skipped: $($_.Exception.Message)"
    }
    if (-not $ManagedCleanupSucceeded) {
        $PythonExe = Resolve-ProjectPython -RootPath $Root
        $ManagedCleanupScript = Join-Path $Root "scripts\cleanup_managed_mihomo.py"
        if (Test-Path -LiteralPath $ManagedCleanupScript) {
            try {
                & $PythonExe $ManagedCleanupScript | Write-Output
            } catch {
                Write-Output "Managed Mihomo fallback cleanup failed: $($_.Exception.Message)"
            }
        }
    }
    Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
    try {
        Wait-Process -Id $ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    } catch {
        # Fall through to the verified force-stop below.
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Output "Stopped PID $ProcessId"
} else {
    $Unowned = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Unowned) {
        $RecoveredRecord = Find-OwnedListenerRecord -ExpectedScript $ServeScript -ExpectedPort $DefaultPort
        if ($RecoveredRecord) {
            $RecoveredId = [int]$RecoveredRecord.pid
            Stop-Process -Id $RecoveredId -Force -ErrorAction SilentlyContinue
            Write-Output "PID $ProcessId was stale; stopped verified project PID $RecoveredId from port $DefaultPort."
        } else {
            Write-Output "PID $ProcessId does not belong to this project; it was not stopped."
        }
    } else {
        Write-Output "PID $ProcessId is not running."
    }
}

Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
} finally {
    if ($HasControlLock) {
        $ControlMutex.ReleaseMutex()
    }
    $ControlMutex.Dispose()
}
