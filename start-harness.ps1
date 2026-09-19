# Compatibility entry point for existing shortcuts.
& (Join-Path $PSScriptRoot 'start-mi-harness.ps1')
exit $LASTEXITCODE
