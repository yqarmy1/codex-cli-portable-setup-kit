@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title 1-Click Codex & Desktop Auto-Repair

set "PS_EXE=powershell.exe"
where powershell.exe >nul 2>&1
if errorlevel 1 (
  if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
  )
)

"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair.ps1"
echo.
echo Press any key to close this window...
pause >nul
endlocal
