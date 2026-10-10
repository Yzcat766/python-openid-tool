@echo off
cd /d "%~dp0"
python capture_openid.py restore %*
set tool_exit=%errorlevel%
pause
exit /b %tool_exit%
