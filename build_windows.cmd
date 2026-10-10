@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python capture_openid.py install
    if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" build_windows.py
if errorlevel 1 goto :failed
echo Build completed. See releases\OpenIDTool\OpenIDTool.exe
pause
exit /b 0
:failed
echo Build failed. See the error above.
pause
exit /b 1
