@echo off
setlocal
set "ROOT=%~dp0"
set PYTHONUTF8=1
"%ROOT%runtime\python\python.exe" "%ROOT%install.py" install
exit /b %ERRORLEVEL%
