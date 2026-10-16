@echo off
setlocal
cd /d "%~dp0"
title Dola Pool Desktop

echo ============================================================
echo    Dola Pool Desktop  -  Launcher
echo ============================================================
echo.

rem ---------- 0) self-heal: drop a broken .venv left by a failed run ----------
if not exist ".venv\Scripts\python.exe" goto findpy
".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
if not errorlevel 1 goto usevenv
echo [i] Existing .venv is broken - rebuilding it...
rmdir /s /q ".venv"
goto findpy

:usevenv
set "PY=%CD%\.venv\Scripts\python.exe"
goto havepy

rem ---------- 1) find a usable Python ----------
:findpy
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto havepy

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto havepy

python3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python3"
if defined PY goto havepy

goto nopython

:havepy
echo [1/3] Python found: %PY%
%PY% --version
echo.

if exist ".venv\Scripts\python.exe" goto deps
echo [2/3] Creating virtual environment (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto venvfail
echo.

:deps
set "PY=%CD%\.venv\Scripts\python.exe"
%PY% -c "import fastapi, uvicorn, webview, requests, Crypto" >nul 2>nul
if not errorlevel 1 goto run

echo [2/3] Installing dependencies - first run takes 1-3 minutes...
echo       (it may look frozen while downloading - please wait)
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements-desktop.txt
if errorlevel 1 goto depfail
echo.

:run
echo [3/3] Starting Dola Pool desktop app...
echo       A window should appear in a few seconds.
echo.
%PY% desktop.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto runfail
echo Program closed normally.
goto end

:nopython
echo.
echo [ERROR] Python was not found on this computer.
echo.
echo   Please install Python 3.10 or newer:
echo       https://www.python.org/downloads/
echo.
echo   IMPORTANT: during installation, tick the checkbox
echo              "Add Python to PATH"
echo.
echo   Then double-click start-windows.bat again.
echo.
echo   Chinese step-by-step guide: open the .txt file in this folder
goto end

:venvfail
echo.
echo [ERROR] Failed to create the virtual environment (.venv).
echo.
echo   Please run these commands manually in this folder:
echo       %PY% -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
echo       .venv\Scripts\python.exe desktop.py
echo.
goto end

:depfail
echo.
echo [ERROR] Dependency installation failed - probably a network problem.
echo.
echo   Check your internet connection and try again, or run manually:
echo       .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
echo.
goto end

:runfail
echo [WARNING] The app exited with an error (code %RC%).
echo.
echo   Please send a screenshot of the messages above.
echo   If no window appeared, try this instead:
echo       .venv\Scripts\python.exe desktop.py --browser
echo.

:end
echo.
echo ------------------------------------------------------------
pause
endlocal
