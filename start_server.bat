@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run this first:
    echo   python -m venv venv
    echo   venv\Scripts\pip.exe install -r requirements.txt
    pause
    exit /b 1
)

echo Starting Presupuesto App - the browser opens automatically.
echo Close this window to stop the server.
echo.

rem Exits 0 immediately when the app is already running (it just opens the
rem browser to it); pause only on an error so the message stays readable.
venv\Scripts\python.exe app.py || pause
