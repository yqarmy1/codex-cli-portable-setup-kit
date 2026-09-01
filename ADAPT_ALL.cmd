@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Universal Multi-Agent Adaptor

echo ============================================================
echo  Universal Multi-Agent Adaptor - Zero-Friction Setup
echo ============================================================
echo.

:: 1. Check if running directly inside a temporary/unextracted ZIP
echo "%~dp0" | findstr /i "\\AppData\\Local\\Temp\\ \\Temp\\ Temp1_" >nul 2>&1
if not errorlevel 1 (
  echo [ERROR] Detected running directly from inside a ZIP archive!
  echo [!] Please EXTRACT the entire ZIP folder first before running ADAPT_ALL.cmd.
  echo.
  echo Steps:
  echo   1. Right-click the downloaded ZIP file.
  echo   2. Select "Extract All...".
  echo   3. Open the extracted folder and run ADAPT_ALL.cmd again.
  echo ============================================================
  pause
  exit /b 1
)

:: 2. Check for PowerShell with fallback
set "PS_EXE=powershell.exe"
where powershell.exe >nul 2>&1
if errorlevel 1 (
  if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
  ) else (
    echo [ERROR] Windows PowerShell was not found on this system.
    echo This tool requires Windows PowerShell 5.1 or higher.
    echo ============================================================
    pause
    exit /b 1
  )
)

:: 3. Run adapt.ps1 with execution policy bypass
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0adapt.ps1" -ProjectRoot "%CD%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
  echo ============================================================
  echo [OK] All agent platforms adapted successfully!
  echo ============================================================
) else (
  echo ============================================================
  echo [!] Adaptor finished with code %EXIT_CODE%.
  echo ============================================================
)

echo.
echo Press any key to close this window...
pause >nul
exit /b %EXIT_CODE%
