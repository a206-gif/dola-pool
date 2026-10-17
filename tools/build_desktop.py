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


def _make_output_robust() -> None:
    """让中文输出在 Windows 控制台上不再抛 UnicodeEncodeError。

    背景：GitHub 的 Windows 机器上 stdout 是 cp1252，脚本第一行中文就崩，
    表现为「打包步骤 1 秒失败、看不到任何 PyInstaller 输出」，很难查。
    CI 里强制 UTF-8（日志/注解按 UTF-8 采集）；本地保留控制台编码（中文正常显示），
    只把编码不下的字符替换掉。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if os.getenv("GITHUB_ACTIONS"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


_make_output_robust()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "desktop.py")
NAME = "DolaPool"

# 运行时需要的数据文件（模板 / 静态资源 / 生成核心）
DATA_DIRS = ["templates", "static", "gen", "tools", "extensions"]   # extensions：浏览器扩展（2.5/30 秒）
DATA_FILES = ["cleanup.sh", "README.md", ".env.example", "requirements.txt",
              "requirements-desktop.txt", "scripts"]

HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.protocols",
    "uvicorn.protocols.http", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan", "uvicorn.lifespan.on",
    "webview", "webview.platforms", "sqlite3", "requests", "Crypto",
    "dola_gen_clean",
]



def _pillow_version() -> str:
    """Pillow 版本（macOS 打包图标等场景需要它）。"""
    try:
        import PIL
        return getattr(PIL, "__version__", "已安装")
    except ImportError:
        return "未安装（macOS 打图标时需要：pip install pillow）"

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
    # 便于排错：把版本信息打出来（云端打包失败时能从日志/注解里直接看到）
    try:
        import PyInstaller
        print("[build] PyInstaller %s / Pillow %s / %s" % (
            PyInstaller.__version__, _pillow_version(), platform.platform()), flush=True)
    except Exception:  # noqa: BLE001
        pass

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

    log_path = os.path.join(ROOT, "build", "pyinstaller.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    tail: list[str] = []
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
        for line in proc.stdout or ():
            sys.stdout.write(line)
            fh.write(line)
            tail.append(line.rstrip("\r\n"))
            del tail[:-80]
    code = proc.wait()

    if code != 0:
        last = "\n".join(tail[-25:]) or "（PyInstaller 没有输出）"
        print("[build] ❌ 打包失败（退出码 %d）" % code)
        print("[build] ── PyInstaller 输出尾部 ──")
        print(last)
        print("[build] 完整日志：%s" % log_path)
        if os.getenv("GITHUB_ACTIONS"):
            # 云端打包时，把原因写成一条 GitHub 注解 —— 这样用 API 就能读到，
            # 不必登录网页翻几万行日志。
            flat = " | ".join(x for x in tail[-14:] if x.strip())[:900]
            print("::error title=PyInstaller 打包失败::" + flat, flush=True)
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as sf:
                    sf.write("### PyInstaller 打包失败（%s）\n\n```\n%s\n```\n" % (
                        platform.system(), "\n".join(tail[-80:])))
            except OSError:
                pass
        return code

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
