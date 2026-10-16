#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Dola Pool 打包成免安装桌面程序（PyInstaller）

用法：
    pip install -r requirements-desktop.txt pyinstaller
    python tools/build_desktop.py                 # 生成单目录版本（启动快，推荐）
    python tools/build_desktop.py --onefile       # 生成单文件版本（分发方便，启动稍慢）
    python tools/build_desktop.py --clean         # 先删掉 build/ dist/ 再打包

产物：
    Windows : dist/DolaPool/DolaPool.exe      或 dist/DolaPool.exe（--onefile）
    macOS   : dist/DolaPool.app               或 dist/DolaPool（--onefile）
    Linux   : dist/DolaPool/DolaPool          或 dist/DolaPool（--onefile）

注意：PyInstaller 不能跨平台打包 —— 在 Windows 上打包得到 .exe，
      在 macOS 上得到 .app，请在想运行的平台上执行本脚本。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "desktop.py")
NAME = "DolaPool"

# 运行时需要的数据文件（模板 / 静态资源 / 生成核心）
DATA_DIRS = ["templates", "static", "gen", "tools"]
DATA_FILES = ["cleanup.sh", "README.md", ".env.example", "requirements.txt",
              "requirements-desktop.txt", "scripts"]

HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.protocols",
    "uvicorn.protocols.http", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan", "uvicorn.lifespan.on",
    "webview", "webview.platforms", "sqlite3", "requests", "Crypto",
    "dola_gen_clean",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Dola Pool 桌面版")
    parser.add_argument("--onefile", action="store_true", help="生成单文件版本")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/ dist/")
    parser.add_argument("--name", default=NAME, help="产物名称")
    args = parser.parse_args()

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
        print("[build] 已清理 build/ dist/")

    sep = ";" if platform.system() == "Windows" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", args.name,
        "--onefile" if args.onefile else "--onedir",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
        "--windowed",                      # 桌面程序：不要弹控制台窗口
        "--paths", os.path.join(ROOT, "gen"),
        "--log-level", "WARN",
    ]
    icon_name = "app_icon.ico" if platform.system() == "Windows" else "app_icon.png"
    icon = os.path.join(ROOT, "static", icon_name)
    if os.path.exists(icon):
        cmd += ["--icon", icon]
    for d in DATA_DIRS:
        path = os.path.join(ROOT, d)
        if os.path.exists(path):
            cmd += ["--add-data", f"{path}{sep}{d}"]
    for f in DATA_FILES:
        path = os.path.join(ROOT, f)
        if os.path.exists(path):
            cmd += ["--add-data", f"{path}{sep}."]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    if platform.system() == "Darwin":
        cmd += ["--osx-bundle-identifier", "com.dolapool.desktop"]
    cmd.append(ENTRY)

    print("[build] 当前平台：%s / Python %s" % (platform.system(), platform.python_version()))
    print("[build] 执行：PyInstaller …（首次打包约 1-3 分钟）")
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        print("[build] ❌ 打包失败，请检查上面的 PyInstaller 输出")
        return proc.returncode

    target = os.path.join(ROOT, "dist", args.name + (".app" if platform.system() == "Darwin" else ""))
    if not os.path.exists(target):
        target = os.path.join(ROOT, "dist", args.name)
    print(f"""
[build] ✅ 打包完成
        产物：{target}
        说明：双击即可运行；首次启动会在用户数据目录创建数据库与配置
              Windows  %APPDATA%\\DolaPool
              macOS    ~/Library/Application Support/DolaPool
              Linux    ~/.local/share/dola-pool
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
