#!/bin/bash
# Dola Pool 桌面版启动器（Linux）
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "   Dola Pool 桌面版  启动器"
echo "============================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 没有检测到 python3，请先安装 Python 3.10+"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 首次运行，正在创建运行环境…"
  python3 -m venv .venv
else
  echo "[1/3] 运行环境已存在，跳过创建"
fi

echo "[2/3] 检查依赖…"
if ! .venv/bin/python -c "import fastapi, uvicorn, webview, requests, Crypto" >/dev/null 2>&1; then
  echo "      正在安装依赖，首次大约 1-3 分钟…"
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements-desktop.txt -q
else
  echo "      依赖已就绪"
fi

# Linux 原生窗口需要 WebKitGTK；缺失时自动提示（仍可用浏览器模式）
if ! .venv/bin/python -c "import gi" >/dev/null 2>&1; then
  echo
  echo "[提示] 未检测到 WebKitGTK（python3-gi / gir1.2-webkit2-4.1）。"
  echo "       安装后可用原生窗口：  sudo apt install python3-gi gir1.2-webkit2-4.1"
  echo "       没有也没关系，下面会用浏览器方式打开界面。"
  echo
  MODE="--browser"
else
  MODE=""
fi

echo "[3/3] 正在启动界面…"
echo
.venv/bin/python desktop.py $MODE "$@"
echo
echo "程序已退出。"
