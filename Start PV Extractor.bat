@echo off
setlocal
rem One-click launcher for the PV Extractor GUI.
rem First run: creates the Python environment (needs Python 3.12+ installed).
rem Every run after that: checks the environment, installs any missing deps,
rem then starts the local GUI and opens your browser.

cd /d "%~dp0"
title PV Extractor

echo ============================================================
echo  PV Extractor - environment check
echo  Creating/updating .venv if needed.
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\bootstrap.ps1" -WithGui
if errorlevel 1 (
    echo.
    echo Setup did not finish - see the messages above.
    echo Most common cause: Python 3.12+ is not installed. Get it from
    echo https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo then double-click this file again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\pv-extractor.exe" (
    echo.
    echo Setup finished but .venv\Scripts\pv-extractor.exe was not created.
    echo Delete .venv and double-click this file again, or run:
    echo   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -WithGui
    echo.
    pause
    exit /b 1
)

echo Starting the PV Extractor GUI - your browser will open shortly.
echo Keep this window open while you use the program. Close it to stop.
echo.
".venv\Scripts\pv-extractor.exe" gui
if errorlevel 1 (
    echo.
    echo PV Extractor stopped with an error - see the messages above.
    echo.
    pause
)
endlocal
