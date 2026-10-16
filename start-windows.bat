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
if not errorlevel 1 goto havepy
echo [i] Existing .venv is broken - rebuilding it...
rmdir /s /q ".venv"
goto findpy

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
echo [1/4] Python found: %PY%
%PY% --version
%PY% -c "import sysconfig;print('  [WARN] this is a free-threaded build - packages may have no wheel') if sysconfig.get_config_var('Py_GIL_DISABLED') else ''"
%PY% -c "import sys;sys.exit(0 if sys.version_info[:2] < (3,14) else 1)"
if not errorlevel 1 goto newvenv
echo   [i] Python 3.14+ is very new. If installation keeps failing,
echo       installing Python 3.12 is the most reliable choice.
echo.

:newvenv
if exist ".venv\Scripts\python.exe" goto depscheck
echo [2/4] Creating virtual environment (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto venvfail
echo.

rem ---------- 2) dependencies ----------
:depscheck
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

:softcheck
%PY% -c "import webview" >nul 2>nul
if not errorlevel 1 goto run
echo [i] pywebview is not installed - the app will open in your web browser
echo     instead of a native window. That works fine.
echo.

rem ---------- 3) run ----------
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
echo [ERROR] Python was not found on this computer.
echo.
echo   Please install Python 3.10 or newer:
echo       https://www.python.org/downloads/
echo.
echo   IMPORTANT: during installation, tick the checkbox
echo              "Add Python to PATH"
echo.
echo   Recommended version: 3.12 (most reliable for this app)
echo   Then double-click start-windows.bat again.
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
echo [ERROR] Dependency installation failed on all three sources.
echo.
echo   This is almost always a network problem. Options:
echo.
echo   1) Use a mirror permanently, then run this file again:
echo        .venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo   2) Install manually with a mirror:
echo        .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo   3) If you see errors about building "pyyaml" from source,
echo      you are using an OLD copy of this package. Download the
echo      latest one: the new requirements no longer need pyyaml.
echo.
echo   4) If you use Python 3.13/3.14 and it still fails, install
echo      Python 3.12 instead - it has prebuilt packages for everything.
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
