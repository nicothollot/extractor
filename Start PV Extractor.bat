@echo off
setlocal
title PV Extractor

rem A repo in the WSL filesystem can't be driven by this Windows launcher.
echo %~dp0| findstr /i /c:"wsl.localhost" /c:"\wsl$" >nul && goto wsl

cd /d "%~dp0" 2>nul

echo ============================================================
echo  PV Extractor - environment check
echo  Creating/updating .venv if needed.
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" -WithGui
if errorlevel 1 goto setupfail
if not exist "%~dp0.venv\Scripts\pv-extractor.exe" goto novenv

echo Starting the PV Extractor GUI - your browser will open shortly.
echo Keep this window open while you use the program. Close it to stop.
echo.
"%~dp0.venv\Scripts\pv-extractor.exe" gui
if errorlevel 1 goto runfail
goto end

:setupfail
echo Setup did not finish - see the messages above.
echo If Python 3.12 isn't installed yet, run setup.bat first.
goto end

:novenv
echo .venv was not created. Run setup.bat first, then start this again.
goto end

:runfail
echo PV Extractor stopped with an error - see the messages above.
goto end

:wsl
echo ============================================================
echo  This repo is on a WSL path. Start it from your WSL terminal:
echo    cd ~/dev/PV_Extractor_vBETA
echo    ./scripts/setup.sh           (first time only)
echo    .venv/bin/pv-extractor gui
echo ============================================================
goto end

:end
echo.
pause
endlocal
