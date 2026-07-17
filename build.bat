@echo off
echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building Ownkey...
pyinstaller --onedir --windowed --name Ownkey ownkey.py

echo.
echo Done! Ownkey.exe is in dist\Ownkey\
pause
