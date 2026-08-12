@echo off
rem code flow - start the server (Windows)
rem Reads .codeflow.env (created by scripts\install.bat) and runs the app.
setlocal
cd /d "%~dp0\.."

if not exist venv (
    echo No venv found - run:  scripts\install.bat
    pause
    exit /b 1
)

if exist .codeflow.env (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".codeflow.env") do set "%%a=%%b"
)

if "%CODEFLOW_HOST%"=="" set "CODEFLOW_HOST=127.0.0.1"
if "%CODEFLOW_PORT%"=="" set "CODEFLOW_PORT=8000"
set "URL=http://%CODEFLOW_HOST%:%CODEFLOW_PORT%"

echo code flow ^> %URL%
echo workflows: %CODEFLOW_WORKFLOWS_DIR%
echo (Ctrl+C to stop)

rem open the browser once the server is up
start "" /b cmd /c "timeout /t 2 >nul & start "" %URL%"

venv\Scripts\python app.py
pause
