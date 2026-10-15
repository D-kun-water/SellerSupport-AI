$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$testRunDirectory = Join-Path $projectRoot ('storage/test-' + [guid]::NewGuid().ToString('N'))
try {
    python -m pytest -p no:cacheprovider "--basetemp=$testRunDirectory"
    if ($LASTEXITCODE -ne 0) { throw '后端测试失败' }
} finally {
    if (Test-Path -LiteralPath $testRunDirectory) {
        $resolvedRoot = (Resolve-Path -LiteralPath $projectRoot).Path.TrimEnd([System.IO.Path]::DirectorySeparatorChar)
        $resolvedTarget = (Resolve-Path -LiteralPath $testRunDirectory).Path
        $storagePrefix = (Join-Path $resolvedRoot 'storage') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $resolvedTarget.StartsWith($storagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝清理工作区外目录：$resolvedTarget"
        }
        $target = Get-Item -LiteralPath $resolvedTarget
        if (($target.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝清理链接目录：$resolvedTarget"
        }
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}
python scripts/evaluate_rules.py
if ($LASTEXITCODE -ne 0) { throw '规则回归失败' }
Push-Location -LiteralPath frontend
try {
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
} finally { Pop-Location }
