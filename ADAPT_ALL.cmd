@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0adapt.ps1" -ProjectRoot "%CD%"
echo.
echo Press any key to close...
pause >nul
endlocal
