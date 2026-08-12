@echo off
rem code flow installer - Windows
rem Creates a virtualenv, installs dependencies, lets you choose where your
rem workflows live, and writes the config to .codeflow.env
setlocal enabledelayedexpansion
cd /d "%~dp0\.."
set "ROOT=%CD%"

echo == code flow install ==

rem --- python check ------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.10+ not found. Install it from https://www.python.org/downloads/
    echo        ^(tick "Add python.exe to PATH" in the installer^)
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10+ required.
    python --version
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo using %%v

rem --- venv + deps ---------------------------------------------------------
if not exist venv (
    python -m venv venv
)
call venv\Scripts\pip install --upgrade pip -q
call venv\Scripts\pip install -r requirements.txt -q
echo dependencies installed

rem --- workflows path --------------------------------------------------------
set "WF="
set /p WF=Workflows folder [%ROOT%\workflows]:
if "%WF%"=="" set "WF=%ROOT%\workflows"
if not exist "%WF%" mkdir "%WF%"
rem make absolute
pushd "%WF%"
set "WF=%CD%"
popd

rem seed an empty custom folder with the sample flows
if /i not "%WF%"=="%ROOT%\workflows" (
    dir /b "%WF%" 2>nul | findstr . >nul
    if errorlevel 1 (
        set "SEED=Y"
        set /p SEED=Folder is empty - copy the sample flows into it? [Y/n]:
        if /i not "!SEED!"=="n" (
            xcopy "%ROOT%\workflows" "%WF%" /e /i /q >nul
            for /d /r "%WF%" %%d in (__pycache__) do if exist "%%d" rd /s /q "%%d"
            echo sample flows copied
        )
    )
)

rem --- config ----------------------------------------------------------------
(
    echo CODEFLOW_WORKFLOWS_DIR=%WF%
    echo CODEFLOW_ENVIRONMENTS_DIR=%ROOT%\environments
    echo CODEFLOW_HISTORY_DIR=%ROOT%\history
    echo CODEFLOW_HOST=127.0.0.1
    echo CODEFLOW_PORT=8000
) > .codeflow.env
echo config written to .codeflow.env:
type .codeflow.env

echo.
echo Done. Start the server with:  scripts\start.bat
pause
