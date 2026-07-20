param(
    [switch]$SkipBackendInstall,
    [switch]$SkipFrontendInstall,
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeScript = Join-Path $Root "scripts\local_runtime.ps1"
. $RuntimeScript

function Assert-NativeSuccess {
    param([Parameter(Mandatory = $true)][string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

$ConfigFile = Join-Path $Root "config.json"
$ConfigExample = Join-Path $Root "config.example.json"
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    $Config = Get-Content -LiteralPath $ConfigExample -Raw -Encoding UTF8 | ConvertFrom-Json
    $Config.'auth-key' = "moyu666"
    $Json = $Config | ConvertTo-Json -Depth 20
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($ConfigFile, $Json, $Utf8NoBom)
    Write-Output "Created config.json with the initial admin key: moyu666"
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not $SkipBackendInstall) {
    $BootstrapPython = Resolve-ProjectPython -RootPath $Root -SkipProjectVenv
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        & $BootstrapPython -m venv (Join-Path $Root ".venv")
        Assert-NativeSuccess "Python virtual environment creation"
    }
    & $BootstrapPython -m pip --python (Join-Path $Root ".venv") install --upgrade pip
    Assert-NativeSuccess "pip upgrade"
    & $BootstrapPython -m pip --python (Join-Path $Root ".venv") install -e $Root
    Assert-NativeSuccess "Backend dependency installation"
    if (-not (Invoke-PythonProbe -Executable $VenvPython)) {
        throw "The project virtual environment was created but its Python executable is not usable."
    }
}

if (-not $SkipFrontendBuild) {
    $BuildArgs = @()
    if ($SkipFrontendInstall) {
        $BuildArgs += "-SkipInstall"
    }
    & (Join-Path $Root "build_frontend.ps1") @BuildArgs
    $SetupMarker = Join-Path $Root ".venv\.moyu-setup-complete"
    [System.IO.File]::WriteAllText($SetupMarker, [DateTimeOffset]::UtcNow.ToString("o"), (New-Object System.Text.UTF8Encoding($false)))
}

Write-Output "Setup complete. Start with .\start_local.ps1"
