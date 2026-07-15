param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$WebRoot = Join-Path $Root "web"
$OutDir = Join-Path $WebRoot "out"
$TargetDir = Join-Path $Root "web_dist"

function Invoke-CorepackChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & corepack @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "corepack $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

if (-not (Get-Command corepack -ErrorAction SilentlyContinue)) {
    throw "Corepack was not found. Install Node.js 20.9 or newer."
}

$NodeVersionText = (& node --version).Trim().TrimStart("v")
if ($LASTEXITCODE -ne 0) {
    throw "Unable to determine the Node.js version."
}
$NodeVersion = [Version]$NodeVersionText
if ($NodeVersion -lt [Version]"20.9.0") {
    throw "Node.js 20.9 or newer is required. Found $NodeVersionText."
}

Push-Location $WebRoot
try {
    if (-not $SkipInstall) {
        $PreviousCi = $env:CI
        try {
            $env:CI = "true"
            Invoke-CorepackChecked @("pnpm", "install", "--frozen-lockfile", "--prefer-offline")
        } finally {
            $env:CI = $PreviousCi
        }
    }
    Invoke-CorepackChecked @("pnpm", "typecheck")
    Invoke-CorepackChecked @("pnpm", "build")
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $OutDir "index.html"))) {
    throw "Frontend build did not produce web\out\index.html."
}

$ResolvedTarget = [System.IO.Path]::GetFullPath($TargetDir)
$ResolvedRoot = [System.IO.Path]::GetFullPath($Root)
if (-not $ResolvedTarget.StartsWith($ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace a frontend directory outside the project root."
}

if (Test-Path -LiteralPath $TargetDir) {
    Remove-Item -LiteralPath $TargetDir -Recurse -Force
}
Copy-Item -LiteralPath $OutDir -Destination $TargetDir -Recurse
Write-Output "Frontend published to $TargetDir"
