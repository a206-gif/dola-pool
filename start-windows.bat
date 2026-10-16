@echo off
setlocal
cd /d "%~dp0"
title Dola Pool Desktop

echo ============================================================
echo    Dola Pool Desktop  -  Launcher
echo ============================================================
echo.

rem ============================================================
rem  0) Reuse the existing .venv when it is healthy, rebuild it
rem     when it is broken. Always assign PY before leaving here.
rem ============================================================
if not exist ".venv\Scripts\python.exe" goto findpy
".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
if errorlevel 1 goto brokenvenv
set "PY=%CD%\.venv\Scripts\python.exe"
goto havepy

:brokenvenv
echo [i] Existing .venv looks broken - rebuilding it...
rmdir /s /q ".venv"

rem ============================================================
rem  1) Find a usable Python interpreter
rem ============================================================
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

rem ============================================================
rem  2) Sanity check the interpreter we picked
rem ============================================================
:havepy
if not defined PY goto nopython
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython

echo [1/4] Python found: %PY%
%PY% -c "import sys;print('      version', sys.version.split()[0])"
%PY% -c "import sysconfig;print('      [WARN] free-threaded build detected - many packages have no wheel for it') if sysconfig.get_config_var('Py_GIL_DISABLED') else ''"
%PY% -c "import sys;sys.exit(0 if sys.version_info[:2] <= (3,13) else 1)"
if not errorlevel 1 goto newvenv
echo      [i] Python 3.14 or newer detected. If installation keeps failing,
echo          using Python 3.12 is the most reliable choice.
echo.

rem ============================================================
rem  3) Virtual environment
rem ============================================================
:newvenv
if exist ".venv\Scripts\python.exe" goto depscheck
echo [2/4] Creating virtual environment (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto venvfail
echo.

rem ============================================================
rem  4) Dependencies (official source, then CN mirrors)
rem ============================================================
:depscheck
if not exist ".venv\Scripts\python.exe" goto venvfail
set "PY=%CD%\.venv\Scripts\python.exe"
%PY% -c "import fastapi, uvicorn, requests, Crypto, multipart" >nul 2>nul
if not errorlevel 1 goto softcheck

echo [3/4] Installing dependencies (first run: 1-3 minutes)...
echo       Please wait - it may look frozen while downloading.
echo.
%PY% -m pip install --upgrade pip --timeout 60 --retries 2 --disable-pip-version-check >nul 2>nul

%PY% -m pip install -r requirements-desktop.txt --timeout 60 --retries 2 --disable-pip-version-check
if not errorlevel 1 goto depsok

echo.
echo [i] Official PyPI failed. Retrying with Tsinghua mirror...
echo.
%PY% -m pip install -r requirements-desktop.txt --timeout 60 --retries 2 --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto depsok

echo.
echo [i] Retrying with Aliyun mirror...
echo.
%PY% -m pip install -r requirements-desktop.txt --timeout 60 --retries 2 --disable-pip-version-check -i https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 goto depsok

goto depfail

:depsok
echo.
echo       Dependencies installed.
echo.

rem ============================================================
rem  5) Native window needs pywebview; otherwise use the browser
rem ============================================================
:softcheck
%PY% -c "import webview" >nul 2>nul
if not errorlevel 1 goto run
echo [i] pywebview is missing - the app will open in your web browser
echo     instead of a native window. That works fine.
echo.

:run
echo [4/4] Starting Dola Pool...
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
echo [ERROR] No usable Python interpreter was found.
echo.
echo   Please install Python 3.10 or newer (3.12 is recommended):
echo       https://www.python.org/downloads/
echo.
echo   IMPORTANT: during installation, tick the checkbox
echo              "Add Python to PATH"
echo.
echo   Then double-click start-windows.bat again.
goto end

:venvfail
echo.
echo [ERROR] Failed to create or use the virtual environment (.venv).
echo.
echo   Please run these commands manually in this folder:
echo       %PY% -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
echo       .venv\Scripts\python.exe desktop.py
echo.
goto end

:depfail
echo.
echo [ERROR] Dependency installation failed on all three sources.
echo.
echo   This is almost always a network problem. Options:
echo.
echo   1) Set a mirror permanently, then run this file again:
echo        .venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo   2) Install manually with a mirror:
echo        .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo   3) If errors mention building "pyyaml" from source, you are using an
echo      old copy of this package - download the latest one.
echo.
echo   4) If you use Python 3.13/3.14 and it still fails, install Python 3.12.
echo.

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
