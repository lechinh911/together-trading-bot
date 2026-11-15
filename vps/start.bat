@echo off
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "TBOT_DATA=%ROOT%\app\data"
cd /d "%ROOT%\app" || exit /b 1
"%ROOT%\runtime\python\python.exe" -m tbot
exit /b %ERRORLEVEL%
