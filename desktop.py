#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dola Pool 桌面版启动器

把 Web 控制台装进一个原生窗口：内置启动后端服务（127.0.0.1，不对外暴露），
用系统自带的浏览器内核渲染界面（Windows: WebView2 / macOS: WKWebView / Linux: WebKitGTK）。

用法：
    python desktop.py                 # 默认演示模式，双击即可用
    python desktop.py --no-demo       # 生产模式：调用真实 Dola 生成
    python desktop.py --browser       # 用系统默认浏览器打开（不开原生窗口）
    python desktop.py --headless      # 只启动服务不弹窗口（调试 / 服务器上用）
    python desktop.py --port 8123     # 指定端口（默认自动选空闲端口）

首次运行会在用户数据目录生成 config.json，之后用 --demo/--no-demo 或
界面里的「运行模式」开关切换都会写进这个文件。

打包成免安装程序：python tools/build_desktop.py
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

APP_NAME = "Dola Pool"
APP_ID = "DolaPool"
DEFAULT_SIZE = (1440, 940)
MIN_SIZE = (960, 640)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "config.json"


# --------------------------------------------------------------------------
# 用户数据目录（桌面版默认不写在程序目录里，避免只读安装目录 / 权限问题）
# --------------------------------------------------------------------------
def user_data_dir() -> str:
    override = os.getenv("DOLA_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    system = platform.system()
    if system == "Windows":
        base = os.getenv("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(base, APP_ID)
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), APP_ID)
    base = os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "dola-pool")


DATA_DIR = user_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, CONFIG_NAME)


def load_config() -> dict:
    cfg = {"demo": True, "width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], "port": 0}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # 配置写不进去不该导致应用打不开
        print(f"[warn] 配置保存失败：{exc}")


# --------------------------------------------------------------------------
# 端口与服务
# --------------------------------------------------------------------------
def pick_port(preferred: int = 0) -> int:
    for candidate in ([preferred] if preferred else []) + [0]:
        if candidate == 0:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    """在后台线程里跑 FastAPI（uvicorn），窗口关掉就跟着退出。"""

    def __init__(self, port: int, demo: bool, quiet: bool = True):
        self.port = port
        self.demo = demo
        self.quiet = quiet
        self.server = None
        self.thread = None
        self.app_module = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self, restart_hook) -> None:
        # 必须在 import app 之前把环境变量准备好
        os.makedirs(DATA_DIR, exist_ok=True)
        os.environ.setdefault("DOLA_DATA_DIR", DATA_DIR)
        os.environ.setdefault("DOLA_DESKTOP", "1")
        os.environ["DOLA_DESKTOP_CONFIG"] = CONFIG_PATH
        os.environ["DOLA_HOST_BIND"] = "127.0.0.1"      # 桌面版只监听本机
        os.environ["DOLA_PORT"] = str(self.port)
        os.environ["DOLA_DEMO"] = "1" if self.demo else "0"

        if HERE not in sys.path:
            sys.path.insert(0, HERE)

        import uvicorn

        import app as app_module  # noqa: E402  （依赖上面的环境变量）

        self.app_module = app_module
        app_module.set_control_hook(restart_hook)

        config = uvicorn.Config(
            app_module.app,
            host="127.0.0.1",
            port=self.port,
            log_level="info" if not self.quiet else "warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="dola-uvicorn", daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 40.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "api/health", timeout=2) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        return False

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=8)


# --------------------------------------------------------------------------
# 控制信号（切换运行模式 / 退出应用）
# --------------------------------------------------------------------------
class Control:
    def __init__(self):
        self.action = None          # None | "quit" | "restart"
        self.window = None

    def __call__(self, action: str) -> None:
        """由后端接口触发。"""
        self.action = action
        print(f"[desktop] 收到指令：{action}")
        win = self.window
        if win is not None:
            try:
                # 关掉窗口会让 webview.start() 返回，主线程再决定是退出还是重开
                threading.Thread(target=win.destroy, daemon=True).start()
            except Exception as exc:
                print(f"[desktop] 关闭窗口失败：{exc}")


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------
def restart_argv() -> list:
    """自动重启时用的命令行：去掉模式参数，让 config.json（刚写入的选择）决定模式。"""
    keep = [a for a in sys.argv[1:] if a not in ("--demo", "--no-demo")]
    return [sys.executable, os.path.abspath(__file__)] + keep


def banner(url: str, demo: bool, data_dir: str) -> None:
    mode = "演示模式（不访问外网，用内置示例成片）" if demo else "生产模式（调用真实 Dola 生成）"
    print(f"""
  ▣ {APP_NAME} 桌面版
  ├─ 服务   {url}
  ├─ 模式   {mode}
  ├─ 数据   {data_dir}
  ├─ 配置   {CONFIG_PATH}
  └─ 提示   关闭窗口即退出；切换运行模式会自动重启
""", flush=True)


def window_icon():
    """按平台挑选窗口图标，并校验文件真实有效。

    Windows 的 pywebview 走 .NET System.Drawing.Icon，只吃 .ico，
    且不能是 PNG 压缩条目；传错会让应用启动即崩溃（System.ArgumentException）。
    """
    system = platform.system()
    name = "app_icon.ico" if system == "Windows" else "app_icon.png"
    path = os.path.join(HERE, "static", name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        if system == "Windows":
            # ICONDIR: reserved=0, type=1, count>0
            if head[0:2] != b"\x00\x00" or head[2:4] != b"\x01\x00" or int.from_bytes(head[4:6], "little") == 0:
                print("[desktop] 图标文件不是有效的 .ico，已忽略")
                return None
        elif head[:8] != b"\x89PNG\r\n\x1a\n":
            print("[desktop] 图标文件不是有效的 .png，已忽略")
            return None
    except Exception as exc:
        print(f"[desktop] 图标读取失败（已忽略）：{exc}")
        return None
    return path


def open_window(url: str, title: str, size, storage_path: str, control: Control, debug: bool = False):
    """用 pywebview 打开原生窗口；返回 False 表示环境不支持（调用方回退到浏览器）。"""
    try:
        import webview
    except Exception as exc:
        print(f"[desktop] 未安装 pywebview（{exc}）")
        return False

    try:
        window = webview.create_window(
            title,
            url,
            width=size[0],
            height=size[1],
            min_size=MIN_SIZE,
            confirm_close=False,
            text_select=True,
        )
        control.window = window
        # private_mode=False + storage_path：界面里的 localStorage（分镜、画布工作流、
        # 管理令牌）才会在重启后保留
        kwargs = {
            "debug": debug,
            "private_mode": False,
            "storage_path": storage_path,
            "http_server": False,
        }
        icon = window_icon()
        if icon:
            kwargs["icon"] = icon
        webview.start(**kwargs)
        return True
    except Exception as exc:
        print(f"[desktop] 原生窗口不可用（{exc}）")
        print("[desktop] 提示：Linux 需要 WebKitGTK，例如 sudo apt install python3-gi gir1.2-webkit2-4.1")
        print("[desktop]       也可以 pip install \"pywebview[qt]\" 使用 Qt 后端")
        return False


def open_browser(url: str) -> None:
    print(f"[desktop] 已在默认浏览器打开：{url}")
    print("[desktop] 按 Ctrl+C 退出服务")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[desktop] 已退出")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 桌面版")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="演示模式（默认）")
    mode.add_argument("--no-demo", action="store_true", help="生产模式：调用真实 Dola 生成")
    parser.add_argument("--port", type=int, default=0, help="指定端口（默认自动选择空闲端口）")
    parser.add_argument("--width", type=int, default=int(cfg.get("width") or DEFAULT_SIZE[0]))
    parser.add_argument("--height", type=int, default=int(cfg.get("height") or DEFAULT_SIZE[1]))
    parser.add_argument("--browser", action="store_true", help="用系统浏览器打开，不开原生窗口")
    parser.add_argument("--headless", action="store_true", help="只启动服务，不打开任何界面")
    parser.add_argument("--debug", action="store_true", help="打开窗口调试模式（可看控制台报错）")
    args = parser.parse_args(argv)

    if args.demo:
        demo = True
    elif args.no_demo:
        demo = False
    else:
        demo = bool(cfg.get("demo", True))

    cfg.update({"demo": demo, "width": args.width, "height": args.height})
    save_config(cfg)

    port = pick_port(args.port or int(cfg.get("port") or 0))
    control = Control()
    backend = Backend(port, demo)
    backend.start(control)

    if not backend.wait_ready():
        print("[desktop] ❌ 后端启动超时，请检查依赖是否装全：pip install -r requirements-desktop.txt")
        backend.stop()
        return 1

    banner(backend.url, demo, DATA_DIR)

    if args.headless:
        print("[desktop] --headless：服务已就绪（Ctrl+C 退出）", flush=True)
        try:
            while control.action is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        action = control.action
        print(f"[desktop] --headless：收到 {action or 'interrupt'}", flush=True)
        backend.stop()
        if action == "restart":
            os.execv(sys.executable, restart_argv())
        return 0

    if args.browser:
        try:
            open_browser(backend.url)
        finally:
            backend.stop()
        return 0

    storage_path = os.path.join(DATA_DIR, "webview")
    os.makedirs(storage_path, exist_ok=True)
    opened = open_window(backend.url, f"{APP_NAME} · AI 视频生成池化平台",
                         (args.width, args.height), storage_path, control, debug=args.debug)

    action = control.action

    if not opened:
        # 没有原生窗口（缺 GUI 库 / 窗口启动失败）：保持服务在线，改用浏览器
        if action == "quit":
            backend.stop()
            return 0
        try:
            open_browser(backend.url)
        finally:
            backend.stop()
        return 0

    backend.stop()

    if action == "restart":
        print("[desktop] 正在以新的运行模式重启…", flush=True)
        # pywebview 每个进程只能 start() 一次，直接重开进程最稳；
        # 模式参数已被 restart_argv() 去掉，新模式来自 config.json
        os.execv(sys.executable, restart_argv())
    print("[desktop] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
