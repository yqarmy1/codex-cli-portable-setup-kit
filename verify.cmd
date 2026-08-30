@echo off
setlocal EnableExtensions DisableDelayedExpansion

if /I "%~1"=="--inner" goto :inner
if "%~1"=="" (
  start "Codex CLI Verify" "%ComSpec%" /D /K ""%~f0" --inner"
) else (
  start "Codex CLI Verify" "%ComSpec%" /D /K ""%~f0" --inner "%~1""
)
if errorlevel 1 pause
exit /b

:inner
cd /d "%~dp0"
title Codex CLI verification
cls
echo ============================================================
echo  Codex CLI verification - persistent window mode
echo ============================================================
echo.
set "PROJECT_ROOT=%~2"
set "LOG=%~dp0verify-last.log"

if not exist "%~dp0verify.ps1" (
  echo [ERROR] verify.ps1 is missing. Extract the ENTIRE ZIP first.
  goto :stay
)
if not exist "%~dp0payload\" (
  echo [ERROR] payload folder is missing. Extract the ENTIRE ZIP first.
  goto :stay
)
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell ^(powershell.exe^) was not found.
  goto :stay
)

if defined PROJECT_ROOT (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify.ps1" -Installed -ProjectRoot "%PROJECT_ROOT%" > "%LOG%" 2>&1
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify.ps1" > "%LOG%" 2>&1
)
set "RC=%ERRORLEVEL%"
type "%LOG%"
echo.
if "%RC%"=="0" (
  echo [OK] Verification completed successfully.
) else (
  echo [ERROR] Verification failed with exit code %RC%.
  echo [ERROR] Log file: %LOG%
)
echo.
:stay
echo This window will stay open. Type EXIT when you want to close it.
exit /b
