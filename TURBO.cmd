@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Codex CLI Turbo Max Power Mode

echo ============================================================
echo  Codex CLI Portable Setup Kit - TURBO MAX BOUNDARY MODE
echo ============================================================
echo.

:: 1. Check if running directly inside a temporary/unextracted ZIP
echo "%~dp0" | findstr /i "\\AppData\\Local\\Temp\\ \\Temp\\ Temp1_" >nul 2>&1
if not errorlevel 1 (
  echo [ERROR] Detected running directly from inside a ZIP archive!
  echo [!] Please EXTRACT the entire ZIP folder first before running TURBO.cmd.
  echo.
  echo Steps:
  echo   1. Right-click the downloaded ZIP file.
  echo   2. Select "Extract All...".
  echo   3. Open the extracted folder and run TURBO.cmd again.
  echo ============================================================
  pause
  exit /b 1
)

:: 2. Check for required companion files
if not exist "%~dp0turbo.ps1" (
  echo [ERROR] Required file 'turbo.ps1' is missing in this directory.
  echo Folder: %~dp0
  echo Please make sure all files from the kit were extracted together.
  echo ============================================================
  pause
  exit /b 1
)

:: 3. Check for PowerShell with fallback
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

:: 4. Run turbo.ps1 with execution policy bypass
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0turbo.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

:: 5. Stable pause to ensure the window never flashes away
echo.
if "%EXIT_CODE%"=="0" (
  echo ============================================================
  echo [OK] Turbo operation completed successfully.
  echo ============================================================
) else (
  echo ============================================================
  echo [!] Turbo mode finished with exit code %EXIT_CODE%.
  echo ============================================================
)

echo.
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%