param([string]$Database='storage/new-merchant.sqlite3',[int]$Port=8015,[switch]$EnableAI)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {throw '先执行 npm.cmd run build --prefix frontend'}
$env:PYTHONIOENCODING='utf-8'
$env:SELLERSUPPORT_DB=$Database
$env:SELLERSUPPORT_AI_ENABLED=if($EnableAI){'true'}else{'false'}
$env:SELLERSUPPORT_SEED_MODE='empty'
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
