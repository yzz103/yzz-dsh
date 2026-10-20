@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Find a Python 3 interpreter: py launcher first, then PATH.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [ERROR] Python 3 not found in PATH.
  echo         Install Python 3.9+ and make sure "python" or "py" works in cmd.
  echo         You can also run manually: python "%~dp0agnes2api.py" serve
  pause
  exit /b 1
)

title agnes2api local gateway
%PY% "%~dp0agnes2api.py" %*

echo.
echo [done] agnes2api stopped.
pause
