[CmdletBinding()]
param(
    [string]$Destination = "",
    [switch]$SkipGitInit
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Destination) {
    $Destination = Join-Path (Split-Path $sourceRoot -Parent) ((Split-Path $sourceRoot -Leaf) + "-github-clean")
}
$destinationRoot = [System.IO.Path]::GetFullPath($Destination)

if ($destinationRoot.StartsWith($sourceRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The clean release destination must be outside the working project."
}
if (Test-Path -LiteralPath $destinationRoot) {
    throw "Destination already exists: $destinationRoot"
}

New-Item -ItemType Directory -Path $destinationRoot | Out-Null

function Copy-ReleaseFile {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $source = Join-Path $sourceRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required release file is missing: $RelativePath"
    }
    $target = Join-Path $destinationRoot $RelativePath
    $parent = Split-Path $target -Parent
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $target
}

$rootFiles = @(
    ".dockerignore", ".env.example", ".gitignore", ".python-version", "LICENSE",
    "README.md", "LOCAL_DEPLOYMENT.md", "DEPLOYMENT.md", "CONFIGURATION.md",
    "DISCLAIMER.md", "MAINTENANCE.md", "CONTRIBUTING.md", "SPONSORS.md",
    "CHANGELOG.md", "SECURITY.md", "VERSION",
    "Dockerfile", "docker-compose.yml", "deploy_server.sh",
    "main.py", "pyproject.toml", "config.example.json", "serve_local.py",
    "run_local.ps1", "start_local.ps1", "stop_local.ps1",
    "setup_local.ps1", "build_frontend.ps1"
)
foreach ($file in $rootFiles) {
    Copy-ReleaseFile $file
}

$sourceTrees = @(
    @{ Path = ".github"; Extensions = @() },
    @{ Path = "api"; Extensions = @(".py") },
    @{ Path = "scripts"; Extensions = @(".py", ".ps1", ".sh") },
    @{ Path = "services"; Extensions = @(".py", ".html", ".css", ".js") },
    @{ Path = "tests"; Extensions = @(".py") },
    @{ Path = "utils"; Extensions = @(".py", ".cjs") },
    @{ Path = "web\src"; Extensions = @() },
    @{ Path = "web\public"; Extensions = @() }
)

foreach ($tree in $sourceTrees) {
    $treeRoot = Join-Path $sourceRoot $tree.Path
    if (-not (Test-Path -LiteralPath $treeRoot -PathType Container)) {
        throw "Required release directory is missing: $($tree.Path)"
    }
    foreach ($file in Get-ChildItem -LiteralPath $treeRoot -Recurse -File) {
        if ($tree.Extensions.Count -and $tree.Extensions -notcontains $file.Extension.ToLowerInvariant()) {
            continue
        }
        $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart("\", "/")
        Copy-ReleaseFile $relative
    }
}

$webFiles = @(
    "web\.gitignore", "web\.npmrc", "web\components.json",
    "web\eslint.config.mjs", "web\next.config.ts", "web\package.json",
    "web\pnpm-lock.yaml", "web\pnpm-workspace.yaml",
    "web\postcss.config.mjs", "web\tsconfig.json"
)
foreach ($file in $webFiles) {
    Copy-ReleaseFile $file
}

$forbidden = @(
    "config.json", "config.json.bak", "data", "logs", "runtime", "backups", ".mimocode",
    ".deps", ".venv", ".pytest_cache", "__pycache__", "web_dist",
    "web\node_modules", "web\.next", "web\out", "web\bun.lock"
)
foreach ($item in $forbidden) {
    if (Test-Path -LiteralPath (Join-Path $destinationRoot $item)) {
        throw "Forbidden runtime item entered the clean release: $item"
    }
}

if (-not $SkipGitInit) {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        throw "Git is required unless -SkipGitInit is used."
    }
    & $git.Source -C $destinationRoot init --initial-branch=main | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "git init failed for $destinationRoot"
    }
}

Write-Output "Clean release created at $destinationRoot"
