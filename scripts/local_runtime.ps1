function Invoke-PythonProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$LauncherArguments = @()
    )

    for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
        $PreviousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            $Output = @(& $Executable @LauncherArguments -c "import sys; print(sys.executable) if sys.version_info[:2] >= (3, 13) else sys.exit(3)" 2>$null)
            $ExitCode = $LASTEXITCODE
        } catch {
            $Output = @()
            $ExitCode = 1
        } finally {
            $ErrorActionPreference = $PreviousPreference
        }

        if ($ExitCode -eq 0) {
            $Resolved = $Output | Select-Object -First 1
            if ($Resolved -and (Test-Path -LiteralPath $Resolved)) {
                return (Resolve-Path -LiteralPath $Resolved).Path
            }
        }
        if ($Attempt -lt 5) {
            Start-Sleep -Milliseconds 300
        }
    }
    return $null
}

function Resolve-ProjectPython {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [switch]$SkipProjectVenv
    )

    $Candidates = @()
    if ($env:CHATGPT2API_PYTHON) {
        $ConfiguredPython = [System.IO.Path]::GetFullPath($env:CHATGPT2API_PYTHON)
        $ProjectVenvRoot = [System.IO.Path]::GetFullPath((Join-Path $RootPath ".venv"))
        if (-not $SkipProjectVenv -or -not $ConfiguredPython.StartsWith($ProjectVenvRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $Candidates += $ConfiguredPython
        }
    }
    if (-not $SkipProjectVenv) {
        $Candidates += (Join-Path $RootPath ".venv\Scripts\python.exe")
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {
            $Resolved = Invoke-PythonProbe -Executable (Resolve-Path -LiteralPath $Candidate).Path
            if ($Resolved) {
                return $Resolved
            }
            $VersionText = [string](Get-Item -LiteralPath $Candidate).VersionInfo.ProductVersion
            if ($VersionText -match '^3\.13(?:\.|$)') {
                return (Resolve-Path -LiteralPath $Candidate).Path
            }
        }
    }

    $StandardCandidates = @()
    $LocalAppDataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $ProgramFilesRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    if ($LocalAppDataRoot) {
        $StandardCandidates += (Join-Path $LocalAppDataRoot "Programs\Python\Python313\python.exe")
    }
    if ($ProgramFilesRoot) {
        $StandardCandidates += (Join-Path $ProgramFilesRoot "Python313\python.exe")
    }
    foreach ($StandardPython in $StandardCandidates) {
        if (Test-Path -LiteralPath $StandardPython) {
            return (Resolve-Path -LiteralPath $StandardPython).Path
        }
    }

    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $Resolved = Invoke-PythonProbe -Executable $PythonCommand.Source
        if ($Resolved) {
            return $Resolved
        }
    }

    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($Selector in @("-V:3.13", "-3.13")) {
            $Resolved = Invoke-PythonProbe -Executable $PyLauncher.Source -LauncherArguments @($Selector)
            if ($Resolved) {
                return $Resolved
            }
        }
    }

    throw "Python 3.13 was not found. Install it, create .venv, or set CHATGPT2API_PYTHON."
}
