[CmdletBinding()]
param(
    [string]$PackagesRoot = "",
    [string]$Label = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $PackagesRoot) {
    $PackagesRoot = Join-Path (Split-Path $sourceRoot -Parent) "moyu-release-packages"
}
$packagesRootPath = [System.IO.Path]::GetFullPath($PackagesRoot)
if ($packagesRootPath.StartsWith($sourceRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The packages directory must be outside the running project."
}
if (-not (Test-Path -LiteralPath $packagesRootPath -PathType Container)) {
    New-Item -ItemType Directory -Path $packagesRootPath -Force | Out-Null
}

$version = ([string](Get-Content -LiteralPath (Join-Path $sourceRoot "VERSION") -Raw)).Trim()
$safeVersion = [regex]::Replace(($version -replace '^v', ''), '[^0-9A-Za-z._-]+', '-')
$safeLabel = [regex]::Replace($Label.Trim(), '[^0-9A-Za-z._-]+', '-')
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$baseName = "moyu-$safeVersion-$timestamp"
if ($safeLabel) {
    $baseName += "-$safeLabel"
}
$destination = Join-Path $packagesRootPath $baseName
$suffix = 1
while (Test-Path -LiteralPath $destination) {
    $destination = Join-Path $packagesRootPath "$baseName-$suffix"
    $suffix += 1
}

$builder = Join-Path $PSScriptRoot "build_clean_release.ps1"
& $builder -Destination $destination -SkipGitInit | Write-Output
if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
    throw "Clean package generation failed: $destination"
}

$latestPath = Join-Path $packagesRootPath "LATEST.txt"
[System.IO.File]::WriteAllText(
    $latestPath,
    $destination + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Output "GitHub package ready: $destination"
Write-Output "Latest package pointer: $latestPath"
