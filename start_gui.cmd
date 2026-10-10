@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "desktop.py" %*
    exit /b 0
)
if exist "releases\OpenIDTool\OpenIDTool.exe" (
    start "" "releases\OpenIDTool\OpenIDTool.exe" %*
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "desktop.py" %*
    exit /b 0
)
echo Please install Python 3.12+ or extract the Windows release package.
pause
exit /b 1
