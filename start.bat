@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import flask, flask_sqlalchemy, argon2, psutil, yt_dlp, PIL" >nul 2>nul
if errorlevel 1 (
  echo Installing Python dependencies...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
)

echo.
echo   SIMPLE IPTV
echo   http://127.0.0.1:5000
echo.
"%PY%" app.py
pause
