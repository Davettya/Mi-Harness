$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$harnessCommand = Join-Path $PSScriptRoot '.venv\Scripts\mi-harness.exe'
if (-not (Test-Path -LiteralPath $harnessCommand)) {
    throw 'Run uv sync --locked and build the web app as described in README.md first.'
}
& $harnessCommand serve --open
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Mi Harness is open in your browser and will connect to the local service automatically.'
exit $LASTEXITCODE
