@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   UNSIGNED DEVELOPMENT BUILD - NOT FOR PUBLIC RELEASE
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Build-WindowsInstaller.ps1" -Mode DevelopmentUnsigned %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Unsigned development installer build failed.
exit /b 1
