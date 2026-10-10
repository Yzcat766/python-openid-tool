@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run install.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" capture_openid.py capture %*
set tool_exit=%errorlevel%
pause
exit /b %tool_exit%
