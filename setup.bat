@echo off
setlocal
title PV Extractor - setup

rem A repo in the WSL filesystem can't be driven by this Windows installer
rem (cmd.exe rejects \\wsl.localhost\... working directories). Use WSL instead.
echo %~dp0| findstr /i /c:"wsl.localhost" /c:"\wsl$" >nul && goto wsl

rem cd into the repo (works on C:\; on a UNC share it may fail, but the
rem PowerShell call below uses an absolute path and sets its own location).
cd /d "%~dp0" 2>nul

echo ============================================================
echo  PV Extractor - first-time setup
echo  This can take several minutes the first time (downloads deps).
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
set RC=%errorlevel%
echo.
if "%RC%"=="0" goto ok

echo Setup did not finish. See the messages above and setup_log.txt in this folder.
goto end

:ok
echo Setup finished. To start the program, double-click "Start PV Extractor.bat".
goto end

:wsl
echo ============================================================
echo  This repo is on a WSL path:
echo    %~dp0
echo  Set it up from your Ubuntu/WSL terminal instead:
echo.
echo    cd ~/dev/PV_Extractor_vBETA
echo    ./scripts/setup.sh
echo ============================================================
goto end

:end
echo.
pause
endlocal
