@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境，请先按 README.md 安装依赖。
  pause
  exit /b 1
)
echo 正在启动默写批改网站，请保持此窗口打开。
echo 浏览器打开 http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py
pause
