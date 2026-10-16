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


def preflight() -> None:
    """打包前的环境自检：把常见的失败原因提前说清楚。"""
    problems = []
    if platform.system() == "Linux":
        import sysconfig
        libdir = sysconfig.get_config_var("LIBDIR") or ""
        ver = "%d.%d" % sys.version_info[:2]
        candidates = [os.path.join(libdir, "libpython%s.so.1.0" % ver),
                      os.path.join(libdir, "libpython%s.so" % ver)]
        if sysconfig.get_config_var("Py_ENABLE_SHARED") and not any(os.path.exists(p) for p in candidates):
            problems.append(
                "当前 Python 缺少 libpython%s.so（Debian/Ubuntu 的系统 Python 常见）。\n"
                "    解决办法二选一：\n"
                "      · 安装共享库： sudo apt install libpython%s\n"
                "      · 或换用官方安装包 / pyenv / conda 的 Python 再打包" % (ver, ver))
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("缺少 PyInstaller，请先执行： pip install pyinstaller")
    try:
        import webview  # noqa: F401
    except ImportError:
        problems.append("缺少 pywebview，请先执行： pip install -r requirements-desktop.txt")

    if problems:
        print("[build] ⚠ 打包前自检发现问题：", flush=True)
        for p in problems:
            print("    · " + p, flush=True)
        print("[build] 提示：也可以在 GitHub 上用 .github/workflows/build-desktop.yml 云端打包，"
              "无需本地环境。", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Dola Pool 桌面版")
    parser.add_argument("--onefile", action="store_true", help="生成单文件版本")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/ dist/")
    parser.add_argument("--name", default=NAME, help="产物名称")
    args = parser.parse_args()

    preflight()
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

    print("[build] 当前平台：%s / Python %s" % (platform.system(), platform.python_version()), flush=True)
    print("[build] 执行：PyInstaller …（首次打包约 1-3 分钟）", flush=True)
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
