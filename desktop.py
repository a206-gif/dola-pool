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


class _WebWindowApi:
    """账号网页窗口能调用的少量能力。

    只做一件事：把当前页面丢到系统浏览器里打开 —— WebView2 万一渲染不出来，
    用户点提示条上的「🌐 用系统浏览器打开」就能换个浏览器对照。
    只接受本机地址，免得页面脚本借这个口子去开任意网址。
    """

    def open_external(self, url: str = "") -> dict:
        target = str(url or "").strip()
        if not target.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return {"ok": False, "detail": "只允许打开本机地址"}
        _open_in_browser(target)
        return {"ok": True, "url": target}


# --------------------------------------------------------------------------
# 把账号 Cookie 直接写进浏览器内核（Windows / WebView2）
#
# 为什么要有这段：本地代理页虽然能把 Cookie 加在请求里，但地址是
# http://127.0.0.1:端口/dola-web/2/chat/ —— Dola 前端按路径做路由，
# 路径对不上就渲染成白屏。直接把 Cookie 写进内核的 Cookie 存储，
# 就能打开**真正的 www.dola.com**，页面本身是官网，不存在改写问题。
# --------------------------------------------------------------------------
# 等内核就绪的最长时间（测试会把它调小）
_INJECT_TIMEOUT = 15.0


def _can_inject_cookies() -> bool:
    """只有 Windows（Edge WebView2 后端）能直接写内核 Cookie 存储。"""
    return platform.system() == "Windows"


def _parse_cookie_pairs(raw: str) -> list:
    """'a=1; b=2' -> [('a', '1'), ('b', '2')]"""
    pairs = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def _ui_invoke(holder, fn) -> None:
    """把 fn 丢到 UI 线程上同步执行（WinForms 的窗体/控件只能从 UI 线程碰）。"""
    from System import Func, Type  # pythonnet，Windows 上装 pywebview 时自带
    holder.Invoke(Func[Type](fn))


def _clr_ready() -> tuple:
    """pythonnet（clr）是否可用 —— 只有 Windows 的 WebView2 后端才有。"""
    try:
        import clr  # noqa: F401
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


_WAIT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>正在打开…</title><style>
html,body{margin:0;height:100%;background:#0a0f11;color:#c9d8d6;
font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
display:flex;align-items:center;justify-content:center;text-align:center}
b{color:#0ca58c}
</style></head><body><div>
<div style="font-size:15px">正在用该账号的登录态打开 <b>Dola</b>…</div>
<div style="opacity:.72;margin-top:6px">把该账号的 Cookie 写进浏览器内核后再跳转（最多等 15 秒）<br>
写不进去会自动改用本机代理页</div>
</div></body></html>"""


def _webview_holder(window):
    """拿到能 Invoke 到 UI 线程的宿主对象（pywebview 的 WinForms 窗体）。"""
    native = getattr(window, "native", None)
    if native is None:
        return None
    if hasattr(native, "webview") or hasattr(native, "browser"):
        return native
    return None


def _inject_webview2_cookies(window, cookie: str, domain: str = ".dola.com",
                             timeout: float = 15.0) -> tuple:
    """把 Cookie 写进浏览器内核（WebView2）。返回 (是否成功, 说明)。

    真机踩过的坑：WebView2 的 CoreWebView2 属性**只能在 UI 线程上访问**，
    在后台线程读它会抛
      System.InvalidOperationException: CoreWebView2 can only be accessed from the UI thread
    （底层是 COM 的 E_NOINTERFACE），整个后台线程就崩了 —— 窗口于是停在 about:blank 白屏。
    所以「探测内核是否就绪」和「写入 Cookie」都必须包在 Invoke 里、在 UI 线程上做。
    """
    pairs = _parse_cookie_pairs(cookie)
    if not pairs:
        return False, "Cookie 为空"
    ready, why = _clr_ready()
    if not ready:  # 尽早失败：pythonnet 不在就说明不是 Windows/WebView2 后端
        return False, "非 Windows 内核（%s）" % why

    state = {"done": False, "count": 0, "err": "", "fatal": False, "holder": None}

    def _probe_and_write():
        """只在 UI 线程上跑：内核还没就绪就直接返回，等下一轮。"""
        holder = state["holder"]
        try:
            ctrl = getattr(holder, "webview", None)
            if ctrl is None:
                browser = getattr(holder, "browser", None)
                ctrl = getattr(browser, "webview", None) if browser is not None else None
            core = getattr(ctrl, "CoreWebView2", None) if ctrl is not None else None
            if core is None:
                return                      # 还没初始化好，下一轮再试
            manager = core.CookieManager
            failed = []
            for name, value in pairs:
                try:                        # 单个 Cookie 失败不影响其它的
                    c = manager.CreateCookie(name, value, domain, "/")
                    for attr, val in (("IsHttpOnly", True), ("IsSecure", True)):
                        try:
                            setattr(c, attr, val)
                        except Exception:  # noqa: BLE001
                            pass
                    manager.AddOrUpdateCookie(c)
                    state["count"] += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append("%s：%s" % (name, exc))
            if failed:
                state["err"] = "；".join(failed)[:200]
            state["done"] = state["count"] > 0
        except Exception as exc:  # noqa: BLE001
            state["err"] = str(exc)
            state["fatal"] = True            # 这类错误重试也没用，直接放弃

    deadline = time.time() + max(0.0, timeout)
    while True:
        holder = _webview_holder(window)
        if holder is None:
            return False, "拿不到浏览器内核控件（pywebview 的窗口还没建好）"
        state["holder"] = holder
        state["done"] = False
        state["fatal"] = False
        try:
            _ui_invoke(holder, _probe_and_write)
        except Exception as exc:  # noqa: BLE001
            return False, "调用内核失败（%s）" % exc
        if state["done"] or state["fatal"] or time.time() >= deadline:
            break
        time.sleep(0.25)

    if state["done"]:
        note = "已写入 %d 个 Cookie（域 %s）" % (state["count"], domain)
        if state["err"]:
            note += "，另有失败：" + state["err"]
        return True, note
    return False, (state["err"] or "等不到内核就绪（CoreWebView2 还没初始化）")


# --------------------------------------------------------------------------
# 控制信号（切换运行模式 / 退出应用 / 打开账号网页窗口）
# --------------------------------------------------------------------------
class Control:
    def __init__(self):
        self.action = None          # None | "quit" | "restart"
        self.window = None
        self.base_url = ""          # 后端地址，main() 里设置，用于把 /dola-web/… 补成绝对地址
        self.web_windows = []       # 「打开账号网页」开出的窗口
        self.web_account = None     # 当前账号网页窗口对应的账号 id（内核 Cookie 是共享的）

    def __call__(self, action: str, payload=None) -> None:
        """由后端接口触发。"""
        if action == "open_web":
            self.open_web(payload or {})
            return
        self.action = action
        print(f"[desktop] 收到指令：{action}")
        win = self.window
        if win is not None:
            try:
                # 关掉窗口会让 webview.start() 返回，主线程再决定是退出还是重开
                threading.Thread(target=win.destroy, daemon=True).start()
            except Exception as exc:
                print(f"[desktop] 关闭窗口失败：{exc}")

    def open_web(self, payload: dict) -> None:
        """开一个原生窗口看某个账号在 Dola 的页面。

        注意：pywebview 只有在 start() 之后、且从非主线程调用 create_window，
        新窗口才会立刻出现（见 pywebview 源码 create_window 末尾）。
        这里的调用来自后端 uvicorn 线程，正好满足条件。
        """
        url = str(payload.get("url") or "")
        if not url:
            return
        if url.startswith("/"):
            url = (self.base_url or "").rstrip("/") + url
        title = str(payload.get("title") or "Dola 账号网页")
        real = str(payload.get("real") or "").strip()          # 真实 dola.com 地址（可空）
        cookie = str(payload.get("cookie") or "").strip()      # 该账号的 Cookie
        domain = str(payload.get("domain") or "").strip() or ".dola.com"
        account_id = payload.get("account_id")
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 没装 pywebview（{exc}），改用系统浏览器")
            _open_in_browser(url)
            return
        if getattr(webview, "guilib", None) is None:
            # --headless / --browser / 原生窗口启动失败时没有 GUI 循环，
            # 此时 create_window 只会静默返回一个不显示的窗口，所以直接走浏览器
            print("[desktop] 当前没有原生窗口环境，改用系统浏览器打开账号网页")
            _open_in_browser(url)
            return
        # 浏览器内核的 Cookie 存储是所有窗口共用的：换账号时先把上一个账号窗口关掉，
        # 否则两个窗口会互相顶掉登录态
        if account_id is not None and self.web_account not in (None, account_id) and self.web_windows:
            print("[desktop] 换了账号：先关掉上一个账号窗口（内核 Cookie 是共享的）")
            self.close_web_windows()
        if account_id is not None:
            self.web_account = account_id
        # 能注入 Cookie 就直接开真站；否则退回本机代理页
        try_cookie = bool(real and cookie and _can_inject_cookies())
        try:
            win = webview.create_window(
                title, "about:blank" if try_cookie else url,
                width=1180, height=860,
                min_size=(720, 520),
                text_select=True,
                js_api=_WebWindowApi(),
            )
            self.web_windows.append(win)
            try:  # 用户自己关掉窗口后从列表里摘掉，免得 accumulate
                win.events.closed += lambda: self.web_windows.remove(win) if win in self.web_windows else None
            except Exception:
                pass
            if try_cookie:
                # 先摆一个「正在准备」页，别让用户对着空白窗口猜
                try:
                    win.load_html(_WAIT_HTML, "")
                except Exception as exc:  # noqa: BLE001
                    print(f"[desktop] 显示等待页失败（不影响后续）：{exc}")
                # 把 Cookie 写进内核，再打开真正的 dola.com（放后台线程，
                # 免得等内核就绪把接口调用卡住）
                threading.Thread(
                    target=self._finish_cookie_window,
                    args=(win, url, real, cookie, domain, title),
                    daemon=True,
                ).start()
            else:
                print(f"[desktop] 已打开账号网页窗口（本机代理页）：{title}")
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 打开账号网页窗口失败（{exc}），改用系统浏览器")
            _open_in_browser(url)

    def _finish_cookie_window(self, win, proxy_url: str, real_url: str,
                              cookie: str, domain: str, title: str) -> None:
        """注入 Cookie 成功就打开真实 dola.com，失败就退回本机代理页。"""
        try:
            ok, note = _inject_webview2_cookies(win, cookie, domain, timeout=_INJECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  任何意外都不能让窗口停在空白页
            ok, note = False, "注入过程出错（%s）" % exc
        if ok:
            print(f"[desktop] {note}；打开真实站点：{real_url}")
            self._set_win_title(win, title + " · dola.com")
            target = real_url
        else:
            print(f"[desktop] Cookie 注入不可用（{note}），改用本机代理页：{proxy_url}")
            self._set_win_title(win, title + " · 本机代理页")
            target = proxy_url
        try:
            win.load_url(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 打开 {target} 失败（{exc}），退回本机代理页")
            try:
                win.load_url(proxy_url)
            except Exception as exc2:  # noqa: BLE001
                print(f"[desktop] 本机代理页也打不开：{exc2}")

    @staticmethod
    def _set_win_title(win, text: str) -> None:
        """标题栏写明用的是真站还是本机代理页，方便一眼看出走的哪条路。"""
        try:
            win.title = text
        except Exception:  # noqa: BLE001
            pass

    def close_web_windows(self) -> None:
        """主窗口关闭时把账号网页窗口一起收掉。

        否则 webview.start() 会因为还有窗口活着而不返回，程序就卡住不退出/不重启了。
        """
        for win in list(self.web_windows):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.web_windows.clear()



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
        control.base_url = url
        # 主窗口一关，就把「打开 Dola 网页」开出的子窗口一起收掉，
        # 否则 webview.start() 会因为还有窗口活着而不返回，退不掉也重启不了
        try:
            window.events.closing += control.close_web_windows
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 注册窗口关闭回调失败（不影响使用）：{exc}")
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


def _open_in_browser(url: str) -> None:
    """打开系统浏览器；无图形环境时只记日志，绝不抛异常。"""
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] 打开系统浏览器失败：{exc}")


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
