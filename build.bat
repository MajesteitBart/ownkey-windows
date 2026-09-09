@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Building an UNSIGNED DEVELOPMENT backend bundle.
echo Installing PyInstaller...
py -m pip install --disable-pip-version-check -q pyinstaller
if errorlevel 1 goto :fail

echo.
echo Building Ownkey...
py -m PyInstaller --noconfirm --clean Ownkey.spec
if errorlevel 1 goto :fail

echo.
echo Done. Unsigned development output is in dist\Ownkey\
exit /b 0

:fail
echo.
echo Unsigned development backend build failed.
exit /b 1
