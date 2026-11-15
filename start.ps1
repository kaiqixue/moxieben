$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '未找到项目虚拟环境。请先运行：py -3.11 -m venv .venv，然后 .venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$tenantData = Join-Path $env:LOCALAPPDATA 'Moxieben\TenantData'
if (-not $env:MOXIEBEN_TENANT_DIR -and (Test-Path -LiteralPath (Join-Path $tenantData 'dictation.sqlite3'))) {
    $env:MOXIEBEN_TENANT_DIR = $tenantData
}
& $python (Join-Path $PSScriptRoot 'app.py')
