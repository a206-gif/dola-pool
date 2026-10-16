#!/bin/bash
# Dola Pool 桌面版启动器（Linux）
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "   Dola Pool 桌面版  启动器"
echo "============================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 没有检测到 python3，请先安装 Python 3.10+（推荐 3.12）"
  exit 1
fi

echo "[1/4] 检测到 Python：$(python3 --version 2>&1)"
echo

if [ -d ".venv" ] && ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then
  echo "[i] 检测到损坏的 .venv，正在重建…"
  rm -rf .venv
fi

echo "[2/4] 准备运行环境…"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi
PY=".venv/bin/python"

echo "[3/4] 检查依赖…"
if $PY -c "import fastapi, uvicorn, requests, Crypto, multipart" >/dev/null 2>&1; then
  echo "      依赖已就绪"
else
  $PY -m pip install --upgrade pip --timeout 60 --retries 2 --disable-pip-version-check >/dev/null 2>&1 || true
  install_deps() {
    echo "      $1"
    $PY -m pip install -r requirements-desktop.txt --timeout 60 --retries 2 --disable-pip-version-check $2
  }
  if install_deps "首次安装约 1-3 分钟，请耐心等待…" ""; then
    :
  elif install_deps "官方源失败，改用清华镜像重试…" "-i https://pypi.tuna.tsinghua.edu.cn/simple"; then
    :
  elif install_deps "清华源失败，改用阿里云镜像重试…" "-i https://mirrors.aliyun.com/pypi/simple/"; then
    :
  else
    echo
    echo "[错误] 三个源都装不上，几乎都是网络问题。可以试试："
    echo "  1) 永久换镜像后再跑一次本文件："
    echo "       $PY -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple"
    echo "  2) 手动安装："
    echo "       $PY -m pip install -r requirements-desktop.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    echo "  3) 如果报错里出现 compiling/building 'pyyaml'，说明你用的是旧版压缩包，"
    echo "     新版依赖已不需要 pyyaml，请重新下载最新包。"
    exit 1
  fi
fi

# Linux 原生窗口需要 WebKitGTK；没有就自动转浏览器模式
MODE=""
if ! $PY -c "import gi" >/dev/null 2>&1; then
  echo "      [i] 未检测到 WebKitGTK（python3-gi / gir1.2-webkit2-4.1）"
  echo "          安装后可用原生窗口： sudo apt install python3-gi gir1.2-webkit2-4.1"
  echo "          这里先用浏览器方式打开界面。"
  MODE="--browser"
fi

echo "[4/4] 正在启动界面…"
echo
$PY desktop.py $MODE "$@"
echo
echo "程序已退出。"
