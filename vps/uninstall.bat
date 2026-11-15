@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%runtime\python\python.exe" "%ROOT%install.py" uninstall
exit /b %ERRORLEVEL%
