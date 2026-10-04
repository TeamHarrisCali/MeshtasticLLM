@echo off
rem Double-click to set this project up on Windows (finds Python, builds the environment, installs what is needed).
rem Options are passed on, e.g.  setup.bat --check   setup.bat --pull-model   setup.bat --recreate
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
echo.
pause
