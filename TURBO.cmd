@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Codex CLI Turbo Max Power Mode

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell (powershell.exe) was not found.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0turbo.ps1" %*
if errorlevel 1 (
  echo.
  echo [!] Turbo mode exited with an error.
  pause
)
