@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "DRY_RUN=0"
set "SHOW_TEST=0"
set "RESTART_EXISTING=1"
set "DEBUG_CONSOLE=0"
for %%A in (%*) do (
  if /I "%%~A"=="--dry-run" set "DRY_RUN=1"
  if /I "%%~A"=="--show-test" set "SHOW_TEST=1"
  if /I "%%~A"=="--keep-existing" set "RESTART_EXISTING=0"
  if /I "%%~A"=="--debug-console" set "DEBUG_CONSOLE=1"
)

rem Force Python backend to use Tauri overlay bridge instead of Tkinter overlay.
set "OWNKEY_TAURI_OVERLAY_ONLY=1"

set "OVERLAY_RELEASE=%CD%\overlay-ui\src-tauri\target\release\ownkey-overlay.exe"
set "OVERLAY_DEBUG=%CD%\overlay-ui\src-tauri\target\debug\ownkey-overlay.exe"
set "BACKEND_EXE=%CD%\dist\Ownkey\Ownkey.exe"
set "BACKEND_SCRIPT=%CD%\ownkey.py"

set "OVERLAY_EXE="
if exist "%OVERLAY_RELEASE%" (
  set "OVERLAY_EXE=%OVERLAY_RELEASE%"
) else if exist "%OVERLAY_DEBUG%" (
  set "OVERLAY_EXE=%OVERLAY_DEBUG%"
)

if not defined OVERLAY_EXE (
  echo [ERROR] Overlay executable not found.
  echo         Expected: "%OVERLAY_RELEASE%" or "%OVERLAY_DEBUG%"
  echo         Build it with: cd overlay-ui ^& pnpm build
  goto :fail
)

set "BACKEND_MODE="
if exist "%BACKEND_EXE%" (
  set "BACKEND_MODE=exe"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "BACKEND_MODE=python"
  )
)

if not defined BACKEND_MODE (
  echo [ERROR] Backend executable not found and 'py' launcher is unavailable.
  echo         Expected: "%BACKEND_EXE%"
  echo         Build it with: build.bat
  goto :fail
)

if "%DEBUG_CONSOLE%"=="1" (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] --debug-console requires Python launcher 'py' in PATH.
    goto :fail
  )
  set "BACKEND_MODE=python_console"
  set "OWNKEY_DEBUG_OVERLAY=1"
)

if "%DRY_RUN%"=="0" if "%RESTART_EXISTING%"=="1" call :stop_existing

echo Starting overlay:
echo   "%OVERLAY_EXE%"
if "%DRY_RUN%"=="0" start "Ownkey Overlay" "%OVERLAY_EXE%"

if /I "%BACKEND_MODE%"=="exe" (
  echo Starting backend:
  echo   "%BACKEND_EXE%"
  if "%DRY_RUN%"=="0" start "Ownkey Backend" "%BACKEND_EXE%"
) else if /I "%BACKEND_MODE%"=="python_console" (
  echo Starting backend in this console:
  echo   py -u "%BACKEND_SCRIPT%"
  if "%SHOW_TEST%"=="1" if "%DRY_RUN%"=="0" call :send_test
  if "%DRY_RUN%"=="0" (
    py -u "%BACKEND_SCRIPT%"
    exit /b %ERRORLEVEL%
  )
) else (
  echo Starting backend:
  echo   py "%BACKEND_SCRIPT%"
  if "%DRY_RUN%"=="0" start "Ownkey Backend" py "%BACKEND_SCRIPT%"
)

if "%DRY_RUN%"=="0" if "%SHOW_TEST%"=="1" call :send_test

if "%DRY_RUN%"=="1" (
  echo Dry run complete. No processes were started.
) else (
  echo Ownkey launched. Overlay appears while recording or processing.
  if "%SHOW_TEST%"=="1" echo Test overlay state was sent once.
)
exit /b 0

:stop_existing
echo [INFO] Stopping existing ownkey-overlay.exe and Ownkey.exe if present...
taskkill /F /T /IM ownkey-overlay.exe >nul 2>nul
taskkill /F /T /IM Ownkey.exe >nul 2>nul
timeout /t 1 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ps = Get-CimInstance Win32_Process | Where-Object {($_.Name -match '^(python|pythonw|py)\.exe$') -and ($_.CommandLine -match 'ownkey\\.py')}; foreach($p in $ps){ try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>nul
exit /b 0

:send_test
timeout /t 1 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sock = New-Object System.Net.Sockets.UdpClient; for($i=0; $i -lt 28; $i++){ if (($i %% 2) -eq 0) { $lvl = 0.18 } else { $lvl = 0.72 }; $payload = @{connection='online'; listening='listening'; processing='idle'; target='selected'; level=$lvl; visible=$true; message='Overlay test'} | ConvertTo-Json -Compress; $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload); [void]$sock.Send($bytes, $bytes.Length, '127.0.0.1', 38485); Start-Sleep -Milliseconds 250 }; $sock.Dispose();" >nul 2>nul
echo [INFO] Sent test burst to overlay (visible=true, ~7 seconds).
exit /b 0

:fail
echo.
echo run-all.bat could not start all components.
echo.
echo Options:
echo   --dry-run        Check paths and commands without starting anything
echo   --show-test      Send one visible test payload to overlay after launch
echo   --keep-existing  Do not stop already-running Ownkey/overlay processes
echo   --debug-console  Run backend in this terminal with overlay UDP debug logs
exit /b 1
