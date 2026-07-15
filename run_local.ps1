$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeScript = Join-Path $Root "scripts\local_runtime.ps1"
. $RuntimeScript
$PythonExe = Resolve-ProjectPython -RootPath $Root

Set-Location $Root
& $PythonExe (Join-Path $Root "serve_local.py")
