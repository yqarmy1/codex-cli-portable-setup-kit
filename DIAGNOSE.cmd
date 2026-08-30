@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /I "%~1"=="--inner" goto :inner
start "Codex CLI Diagnostic" "%ComSpec%" /D /K ""%~f0" --inner"
exit /b
:inner
cd /d "%~dp0"
title Codex CLI diagnostic
cls
echo ============================================================
echo  Codex CLI launcher diagnostic
echo ============================================================
echo Script: %~f0
echo Folder: %~dp0
echo COMSPEC: %ComSpec%
echo.
if exist "%~dp0install.ps1" (echo [OK] install.ps1 found) else (echo [FAIL] install.ps1 missing)
if exist "%~dp0payload\" (echo [OK] payload folder found) else (echo [FAIL] payload folder missing)
if exist "%~dp0MANIFEST.sha256" (echo [OK] MANIFEST.sha256 found) else (echo [FAIL] MANIFEST.sha256 missing)
where powershell.exe >nul 2>&1 && (echo [OK] powershell.exe found) || (echo [FAIL] powershell.exe missing)
where node.exe >nul 2>&1 && (echo [OK] node.exe found) || (echo [WARN] node.exe missing)
where npm.cmd >nul 2>&1 && (echo [OK] npm found) || (echo [WARN] npm missing)
where codex.cmd >nul 2>&1 && (echo [OK] codex found) || (where codex.exe >nul 2>&1 && (echo [OK] codex found) || (echo [WARN] codex not found))
echo.
echo This window will stay open. Type EXIT when done.
exit /b
