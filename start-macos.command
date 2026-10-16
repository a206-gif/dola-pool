#!/bin/bash
# Dola Pool 桌面版启动器（macOS：在访达里双击本文件即可）
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "   Dola Pool 桌面版  启动器"
echo "============================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 没有检测到 python3。"
  echo "       请先安装 Python 3.10+（https://www.python.org/downloads/ 或 brew install python）"
  echo "       然后重新双击本文件。"
  read -r -p "按回车退出…"
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
  echo "      依赖安装完成"
else
  echo "      依赖已就绪"
fi

echo "[3/3] 正在启动界面…"
echo
.venv/bin/python desktop.py "$@" || {
  echo
  echo "[提示] 启动失败。若是提示缺少 GUI 库，可先这样跑："
  echo "       .venv/bin/python desktop.py --browser"
  read -r -p "按回车退出…"
  exit 1
}

echo
echo "程序已退出。"
