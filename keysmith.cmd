@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE="

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
  set "PYTHON_EXE=python"
) else (
  where py >nul 2>&1
  if %ERRORLEVEL% equ 0 (
    set "PYTHON_EXE=py"
  )
)

if "%PYTHON_EXE%"=="" (
  echo [ERROR] Python is required to run codex-keysmith. Please install Python 3.10+ or ensure python is in PATH.
  exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_DIR%codex-instruct.py" %*
exit /b %ERRORLEVEL%
