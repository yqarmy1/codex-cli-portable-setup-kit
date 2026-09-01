@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Configure API Relays (.env)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0config_relays.ps1"
echo.
echo Press any key to exit...
pause >nul
endlocal
