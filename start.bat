@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境，请先按 README.md 安装依赖。
  pause
  exit /b 1
)
if exist "%~dp0local-data-dir.txt" set /p MOXIEBEN_TENANT_DIR=<"%~dp0local-data-dir.txt"
if exist "%~dp0local-data-dir.txt" if not exist "%MOXIEBEN_TENANT_DIR%\dictation.sqlite3" (
  echo 固定的教师数据库未找到，请检查 local-data-dir.txt；不会创建空数据库。
  pause
  exit /b 1
)
if not defined MOXIEBEN_TENANT_DIR if exist "%LOCALAPPDATA%\Moxieben\TenantData\dictation.sqlite3" set "MOXIEBEN_TENANT_DIR=%LOCALAPPDATA%\Moxieben\TenantData"
echo 正在启动默写批改网站，请保持此窗口打开。
echo 浏览器打开 http://127.0.0.1:5000
echo 教师数据目录：%MOXIEBEN_TENANT_DIR%
".venv\Scripts\python.exe" app.py
pause
