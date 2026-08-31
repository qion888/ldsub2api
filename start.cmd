@echo off
setlocal

rem Launch the PowerShell bootstrapper with a process-only bypass so downloaded
rem copies with Windows Mark-of-the-Web can be started by double-clicking.
set "LDXP_AUTO_INSTALL=1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo LDXP startup failed with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
