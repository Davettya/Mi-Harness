$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$harnessCommand = Join-Path $PSScriptRoot '.venv\Scripts\harness.exe'
if (-not (Test-Path -LiteralPath $harnessCommand)) {
    throw '请先按 README.md 完成 uv sync --locked 和前端构建。'
}
& $harnessCommand serve --open
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host '工作台已在浏览器中打开，将自动连接本机服务。'
exit $LASTEXITCODE
