$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '未找到项目虚拟环境。请先运行：py -3.11 -m venv .venv，然后 .venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$tenantData = Join-Path $env:LOCALAPPDATA 'Moxieben\TenantData'
$dataPin = Join-Path $PSScriptRoot 'local-data-dir.txt'
if (Test-Path -LiteralPath $dataPin) {
    $env:MOXIEBEN_TENANT_DIR = (Get-Content -LiteralPath $dataPin -Raw).Trim()
    if (-not $env:MOXIEBEN_TENANT_DIR -or -not (Test-Path -LiteralPath (Join-Path $env:MOXIEBEN_TENANT_DIR 'dictation.sqlite3'))) {
        throw '固定的教师数据库未找到，请检查 local-data-dir.txt；不会创建空数据库。'
    }
} elseif (-not $env:MOXIEBEN_TENANT_DIR -and (Test-Path -LiteralPath (Join-Path $tenantData 'dictation.sqlite3'))) {
    $env:MOXIEBEN_TENANT_DIR = $tenantData
}
Write-Host "教师数据目录：$env:MOXIEBEN_TENANT_DIR"
& $python (Join-Path $PSScriptRoot 'app.py')
