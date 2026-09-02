@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title OpenAI-Compatible Anti-Refusal Gateway (:8088)

python "%~dp0payload\project\.agents\tools\re-toolkit\gateway.py" --port 8088
if errorlevel 1 (
  python "%~dp0.agents\tools\re-toolkit\gateway.py" --port 8088
)
if errorlevel 1 (
  echo.
  echo [ERROR] Gateway failed to start. Ensure Python 3 is installed.
  pause
)
endlocal
