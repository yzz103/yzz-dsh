@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title agnes2api local gateway
echo Starting agnes2api local gateway ...
echo.
"%~dp0agnes2api.exe" %*
echo.
echo [done] agnes2api stopped.
pause
