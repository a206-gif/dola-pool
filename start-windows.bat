@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Dola Pool 桌面版
echo ============================================
echo    Dola Pool 桌面版  启动器
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有检测到 Python。
  echo        请先到 https://www.python.org/downloads/ 安装 Python 3.10 或更高版本，
  echo        安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 首次运行，正在创建运行环境...
  python -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建运行环境失败。
    pause
    exit /b 1
  )
) else (
  echo [1/3] 运行环境已存在，跳过创建
)

echo [2/3] 检查依赖...
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, webview, requests, Crypto" >nul 2>nul
if errorlevel 1 (
  echo       正在安装依赖，首次大约 1-3 分钟，请耐心等待...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt -q
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    echo        也可以手动执行： .venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
    pause
    exit /b 1
  )
) else (
  echo       依赖已就绪
)

echo [3/3] 正在启动界面...
echo.
".venv\Scripts\python.exe" desktop.py %*

echo.
echo 程序已退出。若启动失败，请把上面的报错信息截图反馈。
pause
