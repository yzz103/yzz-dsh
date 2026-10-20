@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=C:\Program Files\Python312\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python not found at: %PY%
  echo         Edit this bat and set PY to your python.exe path.
  pause
  exit /b 1
)

title agnes2api local gateway
"%PY%" "%~dp0agnes2api.py" %*

echo.
echo [done] agnes2api stopped.
pause
