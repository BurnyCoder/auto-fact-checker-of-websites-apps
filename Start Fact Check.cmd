@echo off
rem Start Fact Check.cmd - double-click to start the fact-check app on Windows.
rem It goes to this folder, checks that uv is installed, then runs "uv run factcheck"; the first start installs the
rem Python dependencies into .venv, which can take a minute. The app lives in the Windows tray (blue check-mark icon):
rem Ctrl+Alt+F checks the whole screen, Ctrl+Alt+R a part of it. Quit from the tray menu, or close this window.
rem Kept with CRLF line endings (.gitattributes): cmd.exe can misread labels in batch files with LF-only endings.
title Fact check what you see
rem The folder is saved once: %~dp0 is re-resolved after "cd" when the script was started by a relative path
rem (e.g. ..\Start Fact Check.cmd) and would then name the wrong folder.
set "HERE=%~dp0"
cd /d "%HERE%"

where uv >nul 2>nul
if errorlevel 1 goto no_uv

echo Starting the fact-check app. Its icon appears in the Windows tray (maybe behind the ^^ arrow).
echo Quit from the tray menu, or close this window. Logs are in "%HERE%logs".
echo.
uv run factcheck
if %errorlevel%==2 goto running
if errorlevel 1 goto failed
exit /b 0

:running
echo.
echo The app is already running: look for its blue check-mark icon in the Windows tray (maybe behind the ^^ arrow).
pause
exit /b 0

:no_uv
echo uv is not installed (https://docs.astral.sh/uv/). Install it by running this in PowerShell, then try again:
echo.
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
echo.
pause
exit /b 1

:failed
echo.
echo The app stopped with an error: see the messages above, or the newest file in "%HERE%logs".
echo If it says port 8765 is in use, another program uses it: set SERVER_PORT in .env and the same port in
echo the extension's Options (WebSocket URL).
pause
exit /b 1
