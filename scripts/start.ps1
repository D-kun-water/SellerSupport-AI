$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    throw '请先执行：cd frontend; npm.cmd ci --cache ../storage/npm-cache; npm.cmd run build'
}
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
