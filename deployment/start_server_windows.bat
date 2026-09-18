@echo off
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo.
echo ==========================================
echo       CRAINBOW CBT LOCAL SERVER
echo ==========================================
echo.
echo Starting the CBT server...
echo Keep this window open during the examination.
echo.

%PYTHON% app.py --host 0.0.0.0 --port 5000

if errorlevel 1 (
    echo.
    echo The server stopped with an error.
    pause
)
