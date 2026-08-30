@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Codex CLI Portable Setup Kit

set "RC=1"
echo ============================================================
echo  Codex CLI Portable Setup Kit - stable launcher
echo ============================================================
echo.
echo This launcher uses a single stable command window.
echo The window will always pause before closing.
echo.

if not exist "%~dp0launcher.ps1" (
  echo [ERROR] launcher.ps1 is missing next to install.cmd.
  echo Extract the ENTIRE ZIP first, then run install.cmd again.
  goto :finish
)

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell ^(powershell.exe^) was not found.
  echo This package requires Windows PowerShell 5.1 or newer.
  goto :finish
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo ============================================================
  echo [OK] Installer finished successfully.
  echo ============================================================
) else (
  echo ============================================================
  echo [ERROR] Installer exited with code %RC%.
  echo See install-last.log in this folder for details.
  echo ============================================================
)

:finish
echo.
echo Press any key to close this window.
pause >nul
exit /b %RC%
