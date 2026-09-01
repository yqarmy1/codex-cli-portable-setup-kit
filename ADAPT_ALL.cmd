@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0adapt.ps1" -ProjectRoot "%CD%"
endlocal
