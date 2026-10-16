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
import base64
import json
import os
import platform
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
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
def _make_output_robust() -> None:
    """控制台编码兜底。

    Windows 控制台默认编码可能是 cp1252（英文版系统），这时 print 中文会抛
    UnicodeEncodeError —— 而我们的日志大多打在后台线程里，抛出去就等于悄悄中断
    （真机上踩过一次同类问题）。这里把编不出来的字符换成 ?，保住线程；
    控制台本来就支持中文时（cp936 / UTF-8）显示不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 （没有 reconfigure 的对象、已关闭的管道都无所谓）
            pass


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


_APP_MODULE = None          # Backend.start() 里赋值；Control 用它回写会话状态


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
        global _APP_MODULE
        _APP_MODULE = app_module          # 供 Control 回写账号会话状态
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
# 账号窗口不给页面暴露任何 Python 对象（血泪教训）
#
# 真机上「随便点一处就未响应 + 控制台满屏 Empty. + maximum recursion depth
# exceeded + 退出码 -805306369(0xCFFFFFFF，Windows 栈溢出)」的根因：
# 之前给账号窗口传了 js_api（一个带 .win 属性、指向 pywebview Window 的对象），
# 而 pywebview 每次页面导航完成都会调 inject_pywebview() → get_functions()
# **递归 getattr 遍历整个 js_api 对象图**（Window → events → native → WinForms
# 的 WebView2 控件 → 每个 .NET 属性），在 UI 线程上边走边取属性值，最终把栈走爆，
# 整个进程直接死掉。点一下页面就可能触发导航 → 于是「随便点一处就崩」。
#
# 所以账号窗口现在**不传 js_api**（window.pywebview.api 不存在），诊断页上的
# 操作全部用普通链接 + 本机的 HTTP 端点（/api/desktop/open-external）实现。
# --------------------------------------------------------------------------


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
# 读内核时抛异常最多容忍几次（内核初始化中途偶尔会抛），超过就当真的不可用
_INJECT_RETRY_LIMIT = 12


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


def _ui_invoke_bounded(holder, fn, timeout: float = 3.0) -> bool:
    """`_ui_invoke` 的带超时版本：UI 线程万一卡住，调用方不会跟着一起卡死。

    （真机上吃过一次亏：pywebview 的 evaluate_js 会永久阻塞；凡是「等 UI 线程回话」
      的地方都得有上限。）
    """
    box = {}

    def worker():
        try:
            _ui_invoke(holder, fn)
            box["ok"] = True
        except Exception as exc:  # noqa: BLE001
            box["error"] = str(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(max(0.05, timeout))
    if thread.is_alive():
        box["timeout"] = True
    return bool(box.get("ok"))


def _clr_ready() -> tuple:
    """pythonnet（clr）是否可用 —— 只有 Windows 的 WebView2 后端才有。"""
    try:
        import clr  # noqa: F401
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------
# 让浏览器内核走和「验活」用的同一个代理
#
# 真机现象：账号池里验活显示「dola.com 可达」（Python 侧的 urllib 会读
# DOLA_PROXY / HTTPS_PROXY 环境变量），可账号窗口就是一片空白、右键菜单还在
# （说明内核活着、只是没把网页取回来）。原因是 WebView2 内核**不读这两个环境
# 变量**，它只认系统代理设置：机器上只配了环境变量代理时，窗口其实是直连
# dola.com，被挡住 → 停在 about:blank。
#
# 为什么不把账号密码直接写进 --proxy-server：Chromium 不支持（会报
# ERR_NO_SUPPORTED_PROXIES），WebView2 也没有代理认证弹窗。所以本机再起一个
# 免认证的小代理，由它带着账号密码去连上游。
# --------------------------------------------------------------------------
_PROXY_ENV_KEYS = ("DOLA_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
# 本机服务必须绕过代理，否则界面自己都打不开
_PROXY_BYPASS = "--proxy-bypass-list=127.0.0.1;localhost;<local>"
_SITE_TIMEOUT = 25.0        # 真站自检最多等多久
_PROBE_HARD_LIMIT = 5.0     # 内核完全没回话时，最多再等这么久就放弃并出诊断页


def _app_version() -> str:
    """后端 app 模块里的版本号（写进标题/诊断页，方便判断有没有装成新版）。"""
    return str(getattr(sys.modules.get("app"), "VERSION", "") or "")


def _split_proxy_url(raw: str):
    """'http://用户:密码@主机:6666' -> (scheme, host, port, user, password)；看不懂给 None。"""
    raw = str(raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
        host = u.hostname
        if not host:
            return None
        scheme = (u.scheme or "http").lower()
        port = u.port or (80 if scheme == "http" else 1080)
        user = urllib.parse.unquote(u.username or "")
        password = urllib.parse.unquote(u.password or "")
    except Exception:  # noqa: BLE001
        return None
    return scheme, host, port, user, password


def _real_host(url: str) -> str:
    """'https://www.dola.com/chat/' -> 'dola.com'（去掉 www 之类，跳转后也能认出来）。"""
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class _LocalProxyTunnel:
    """免认证的本机小代理：转发给上游代理并补上 Proxy-Authorization。

    只做两件事：CONNECT（https 的隧道）和明文 HTTP 转发。够浏览器加载网页用了。
    """

    def __init__(self, parts):
        self.scheme, self.host, self.port, self.user, self.password = parts
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.stats = {"connect": 0, "forward": 0, "ok": 0, "fail": 0, "last_error": ""}

    # ---- 生命周期 ----
    def start(self) -> str:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))          # 只监听本机，不对外
        self._sock.listen(64)
        self._thread = threading.Thread(target=self._accept_loop, name="dola-proxy-tunnel",
                                        daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        if self._sock is None:
            return ""
        try:
            return "http://127.0.0.1:%d" % self._sock.getsockname()[1]
        except OSError:
            return ""

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:  # noqa: BLE001
            pass

    def note(self) -> str:
        return "%s:%d" % (self.host, self.port)

    # ---- 内部 ----
    def _auth_header(self) -> bytes:
        if not self.user:
            return b""
        token = base64.b64encode(("%s:%s" % (self.user, self.password)).encode("utf-8"))
        return b"Proxy-Authorization: Basic " + token + b"\r\n"

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    @staticmethod
    def _read_head(sock, limit: int = 65536) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < limit:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    def _serve(self, client):
        try:
            head = self._read_head(client)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            words = first.split(" ")
            method = (words[0] if words else "").upper()
            target = words[1] if len(words) > 1 else ""
            upstream = socket.create_connection((self.host, self.port), timeout=20)
            if method == "CONNECT":
                self.stats["connect"] += 1
                req = ("CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)).encode() \
                    + self._auth_header() + b"\r\n"
                upstream.sendall(req)
                resp = self._read_head(upstream)
                status = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                if " 200" not in status:
                    self.stats["fail"] += 1
                    self.stats["last_error"] = "上游代理拒绝：%s" % status[:90]
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.stats["ok"] += 1
            else:
                self.stats["forward"] += 1
                rest = head.split(b"\r\n", 1)[1] if b"\r\n" in head else b""
                keep = [ln for ln in rest.split(b"\r\n")
                        if ln and not ln.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))]
                forwarded = head.split(b"\r\n", 1)[0] + b"\r\n" + b"\r\n".join(keep) \
                    + b"\r\n" + self._auth_header() + b"Connection: close\r\n\r\n"
                upstream.sendall(forwarded)
                self.stats["ok"] += 1
            self._pump(client, upstream)
        except Exception as exc:  # noqa: BLE001
            self.stats["fail"] += 1
            self.stats["last_error"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _pump(a, b) -> None:
        """两个方向来回搬字节，任意一侧断开就收工。"""
        try:
            while True:
                try:
                    ready, _, broken = select.select([a, b], [], [a, b], 120)
                except OSError:
                    return
                if broken:
                    return
                if not ready:
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (b if src is a else a).sendall(data)
        except OSError:
            pass
        finally:
            for sock in (a, b):
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass


def _wrap_injection_skip() -> int:
    """把各 pywebview 模块里的 inject_pywebview 包一层，支持「这个窗口别注入」。

    幂等：重复调用只包装一次。返回包装了几个模块（0 表示没有 pywebview，无所谓）。
    """
    done = 0
    for name, mod in list(sys.modules.items()):
        if not name.startswith("webview") or mod is None:
            continue
        orig = getattr(mod, "inject_pywebview", None)
        if orig is None or getattr(orig, "_dlp_wrapped", False):
            continue

        def wrapper(platform, win, _orig=orig):
            if getattr(win, "_dlp_no_inject", False):
                try:                     # pywebview 内部在等这个事件，必须补上
                    win.events.loaded.set()
                except Exception:  # noqa: BLE001
                    pass
                return None
            return _orig(platform, win)

        wrapper._dlp_wrapped = True
        try:
            setattr(mod, "inject_pywebview", wrapper)
            done += 1
        except Exception:  # noqa: BLE001
            pass
    return done


def _skip_injection_for(window) -> bool:
    """给这个窗口打标记：不要往它的页面里注入 pywebview 脚本。

    账号窗口打开的是**别人家的正式站点**（dola.com），不该往里塞东西；而且注入桥
    正是真机崩溃的现场（见上面那段注释）。主窗口不带这个标记，一切照旧。
    """
    try:
        window._dlp_no_inject = True
        return True
    except Exception:  # noqa: BLE001
        return False


def _prepare_browser_network(config=None, env=None, log=print,
                             allow_env: bool = True) -> dict:
    """把内核的网络也挂到同一个代理上。返回一份说明（给日志和诊断页用）。"""
    env = os.environ if env is None else env
    info = {"applied": False, "source": "", "proxy": "", "note": "", "args": "", "tunnel": None}
    existing = str(env.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
    if existing:
        info["note"] = "环境里已经设过 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS，保持不动"
        log("[desktop] 浏览器内核参数：%s（%s）" % (info["note"], existing[:80]))
        return info
    raw, source = "", ""
    if isinstance(config, dict):
        raw = str(config.get("proxy") or "").strip()
        source = "config.json 里的 proxy" if raw else ""
    if not raw and allow_env:
        for key in _PROXY_ENV_KEYS:
            value = str(env.get(key) or "").strip()
            if value:
                raw, source = value, key
                break
    if not raw:
        info["note"] = "没发现代理配置（%s），窗口直连" % "/".join(_PROXY_ENV_KEYS[:2])
        log("[desktop] 浏览器内核网络：%s" % info["note"])
        return info
    parts = _split_proxy_url(raw)
    if not parts:
        info["note"] = "代理地址看不懂，按直连处理：%s" % raw[:60]
        log("[desktop] 浏览器内核网络：%s" % info["note"])
        return info
    scheme, host, port, user, password = parts
    if scheme not in ("http", "https"):
        info["note"] = "只支持 http/https 代理（拿到的是 %s），按直连处理" % scheme
        log("[desktop] 浏览器内核网络：%s" % info["note"])
        return info
    shown = "%s://%s:%d" % (scheme, host, port)
    if user:
        tunnel = _LocalProxyTunnel(parts)
        try:
            tunnel.start()
        except Exception as exc:  # noqa: BLE001
            proxy_arg, note = shown, "本机中转起不来（%s），仍把请求交给 %s（可能因无认证被拒）" % (exc, shown)
        else:
            info["tunnel"] = tunnel
            proxy_arg = tunnel.url
            note = "经本机中转 %s → %s（账号密码由中转带上，内核不直接接触）" % (tunnel.url, shown)
    else:
        proxy_arg, note = shown, "直接交给内核：%s" % shown
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--proxy-server=%s %s" % (proxy_arg, _PROXY_BYPASS)
    info.update(applied=True, source=source, proxy=shown, note=note,
                args=env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"])
    log("[desktop] 浏览器内核网络：%s（来自 %s；本机地址直连）" % (note, source))
    return info


# --------------------------------------------------------------------------
# 真站加载自检：窗口里到底渲染出来没有？让内核里的 JS 自己回答。
# 有这一层，白屏就不会再靠人肉发现 —— 窗口会直接写出「卡在哪一步」。
# --------------------------------------------------------------------------
_PROBE_JS = r"""(function () {
  var nav = null;
  try { var e = performance.getEntriesByType('navigation'); if (e && e.length) nav = e[0]; } catch (err) {}
  var body = document.body;
  return {
    href: String(location.href || ''),
    ready: String(document.readyState || ''),
    title: String(document.title || '').slice(0, 80),
    html_len: body ? String(body.innerHTML || '').length : -1,
    text: body ? String(body.innerText || '').replace(/\s+/g, ' ').slice(0, 100) : '',
    status: nav ? (nav.responseStatus || 0) : 0,
    ms: nav ? Math.round(nav.duration || 0) : 0
  };
})()"""


def _judge_real_site(report, host: str = "dola.com") -> tuple:
    """(渲染成功?, 一句话原因)。判断依据只有内核自己报上来的东西。"""
    if not isinstance(report, dict) or not report:
        return False, "拿不到页面信息（浏览器内核没回话）"
    href = str(report.get("href") or "")
    if "chromewebdata" in href or "edge-error" in href:
        return False, "导航失败：内核跳到了错误页（%s）" % href[:70]
    if not href or href in ("about:blank", "about:blank#blocked"):
        return False, "地址还是 about:blank —— 内核没能把 dola.com 打开（网络/代理层面被挡住）"
    if host and host not in href:
        return False, "地址被跳到了 %s（不是 %s）" % (href[:70], host)
    html_len = report.get("html_len")
    text = str(report.get("text") or "")
    if not isinstance(html_len, int):
        html_len = -1
    if len(text) >= 2:
        return True, "标题「%s」，可见文字 %d 字，HTML %d 字符，HTTP %s" % (
            report.get("title") or "", len(text), html_len, report.get("status"))
    if html_len >= 4000:      # 纯 canvas 页面：没有可见文字但有实打实的 DOM
        return True, "标题「%s」，HTML %d 字符（页面无文字节点，按渲染成功算）" % (
            report.get("title") or "", html_len)
    return False, "页面打开了但内容几乎是空的（HTML %s 字符、可见文字 %d 字，HTTP %s）" % (
        html_len, len(text), report.get("status"))


# WebErrorStatus（.NET 枚举）→ 人话。窗口打不开真站时，这一行就是答案。
_NAV_ERROR_HINTS = {
    "HostNameNotResolved": "域名解析失败（DNS 没解析出来：没网 / 被拦 / hosts 有问题）",
    "CannotConnectToProxy": "连不上代理（代理地址或端口不对，或者代理要账号密码）",
    "ProxyAuthenticationRequired": "代理要认证（把账号密码写进 config.json 的 proxy）",
    "Timeout": "超时（网络太慢，或者这条链路被挡住了）",
    "ConnectionAborted": "连接被中断（链路被重置：常见于该走代理却直连、或代理不稳）",
    "ConnectionReset": "连接被重置（同上）",
    "ConnectionRefused": "连接被拒绝（对方端口没开 / 本机防火墙拦了）",
    "ServerUnreachable": "服务器不可达（网络不通）",
    "Disconnected": "网络断开",
    "NavigationCanceled": "导航被取消（页面自己取消了，或者被拦截）",
    "OperationCanceled": "操作被取消",
    "BlockedByClient": "请求被本机拦截（防火墙 / 杀软 / 广告拦截）",
    "BlockedByResponse": "响应被拦截",
    "HttpInvalidServerResponse": "服务器返回了不合法的响应",
    "RedirectFailed": "跳转失败",
    "UnexpectedError": "未预期错误",
    "ValidAuthenticationCredentialsRequired": "站点要求登录认证",
    "ValidAuthenticationCredentialsOptional": "站点想要认证信息",
    "CertificateCommonNameIsIncorrect": "证书域名不匹配",
    "CertificateExpired": "证书过期",
    "ClientCertificateContainsErrors": "客户端证书有问题",
}


def _hint_for_nav_error(code: str) -> str:
    name = str(code or "").split(".")[-1].strip()
    if not name:
        return ""
    return _NAV_ERROR_HINTS.get(name, "内核报的错误码：%s" % name)


def _install_page_probe(window, log=print, state: dict = None) -> dict:
    """在 UI 线程上装两样东西：.NET 的导航事件监听 + 一个不依赖 pywebview 的 JS 探针。

    为什么要自己写，而不用 pywebview 的 evaluate_js：
    它的实现是 `self.webview.Invoke(...ExecuteScriptAsync(...).ContinueWith(
        lambda task: _callback(json.loads(task.Result)), syncContextScheduler))`，
    再加一句 `semaphore.acquire()` —— 只要那个 continuation 里出一点意外
    （脚本报错时 task.Result 可能是 null，json.loads(None) 直接抛），
    信号量就永远不放，调用线程**永久卡死**。真机上就是这么卡住的：
    诊断页写出「脚本通道：evaluate_js 一直没返回」。
    所以这里改成：自己拿 CoreWebView2，异步读结果、解析失败也只记一笔，绝不阻塞。
    """
    holder = _webview_holder(window)
    if holder is None:
        return {} if state is None else state
    if state is None:
        state = {"ready": False, "source": "", "started": "", "done": False, "ok": None,
                 "http": None, "error_code": "", "js": None, "js_error": "",
                 "js_seq": 0, "js_at": 0.0}

    def _read_core():
        ctrl = getattr(holder, "webview", None)
        if ctrl is None:
            browser = getattr(holder, "browser", None)
            ctrl = getattr(browser, "webview", None) if browser is not None else None
        core = getattr(ctrl, "CoreWebView2", None) if ctrl is not None else None
        return core

    def _setup():
        """只在 UI 线程上跑。"""
        try:
            core = _read_core()
            if core is None:
                return
            if not state.get("hooked"):
                state["hooked"] = True

                def on_started(sender, args):
                    try:
                        state["started"] = str(args.Uri)
                    except Exception:  # noqa: BLE001
                        pass

                def on_done(sender, args):
                    try:
                        state["source"] = str(sender.Source)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        state["ok"] = bool(args.IsSuccess)
                    except Exception:  # noqa: BLE001
                        state["ok"] = None
                    try:
                        state["http"] = int(args.HttpStatusCode)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        state["error_code"] = str(args.WebErrorStatus)
                    except Exception:  # noqa: BLE001
                        pass
                    state["done"] = True

                core.NavigationStarting += on_started
                core.NavigationCompleted += on_done
            try:
                state["source"] = str(core.Source)
            except Exception:  # noqa: BLE001
                pass
            state["ready"] = True
        except Exception as exc:  # noqa: BLE001
            state["js_error"] = "读内核失败：%s" % exc

    def _start_js():
        """在 UI 线程上发起一次 ExecuteScriptAsync（不阻塞等结果）。"""
        try:
            core = _read_core()
            if core is None:
                return
            task = core.ExecuteScriptAsync(_PROBE_JS)

            def _cb(t):
                try:
                    raw = t.Result
                except Exception as exc:  # noqa: BLE001
                    state["js_error"] = "脚本通道报错：%s" % exc
                    return
                if raw is None:
                    state["js_error"] = "内核返回空（脚本没执行）"
                    return
                try:
                    state["js"] = json.loads(str(raw))
                    state["js_error"] = ""
                except Exception as exc:  # noqa: BLE001
                    state["js_error"] = "解析脚本结果失败：%s" % exc

            try:
                from System import Action
                from System.Threading.Tasks import Task
                task.ContinueWith(Action[Task](_cb))
            except Exception:  # noqa: BLE001
                # 没有 pythonnet 就没法异步取结果。**绝不能在 UI 线程上 .Result 等它** ——
                # ExecuteScriptAsync 的完成要回到 UI 线程的消息循环，等它就会把 UI 线程
                # 锁死（这正是 pywebview 的 evaluate_js 永久卡死的机制）。
                # 脚本探针只是加分项，主证据是上面的 .NET 导航事件；这里直接放弃。
                state["js_error"] = "没有 pythonnet，跳过脚本探针（以导航事件为准）"
            state["js_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            state["js_error"] = "发起脚本失败：%s" % exc

    # 先把「怎么再发起一次脚本」和宿主存好（哪怕下面装监听没成功，也还能继续用）
    state["_start_js"] = _start_js
    state["_holder"] = holder
    # 没挂上就再试：窗口刚建好时内核可能还没就绪，等一会儿就能挂上
    if not state.get("hooked") and not _ui_invoke_bounded(holder, _setup, timeout=4.0):
        if not state.get("hooked"):
            state["js_error"] = "内核没回话（装导航监听超时）"
    return state


def _page_state(state: dict, do_js: bool = True) -> dict:
    """把探针状态整理成「页面的样子」（线程安全：只读普通字典）。"""
    report = {}
    js = state.get("js")
    if isinstance(js, dict):
        report = js
    href = str((report.get("href") if isinstance(report, dict) else "") or "")
    source = str(state.get("source") or "")
    return {
        "report": report if isinstance(report, dict) else {},
        "href": href or source,
        "source": source,
        "started": str(state.get("started") or ""),
        "nav_done": bool(state.get("done")),
        "nav_ok": state.get("ok"),
        "http": state.get("http"),
        "error_code": str(state.get("error_code") or ""),
        "error_hint": _hint_for_nav_error(state.get("error_code") or ""),
        "js_ok": isinstance(js, dict),
        "js_error": str(state.get("js_error") or ""),
        "js_at": float(state.get("js_at") or 0.0),
        "core_ready": bool(state.get("ready")),
    }


def _judge_page_state(page: dict, host: str = "dola.com") -> tuple:
    """(成功?, 一句话原因) —— 依据 .NET 的导航结果 + JS 探针，两边都能解释清楚。"""
    href = str(page.get("href") or "").strip()
    report = page.get("report") or {}
    http = page.get("http")
    nav_done = page.get("nav_done")
    nav_ok = page.get("nav_ok")
    if "chromewebdata" in href or "edge-error" in href:
        return False, "导航失败：内核跳到了错误页（%s）" % href[:70]
    if not href or href in ("about:blank", "about:blank#blocked"):
        return False, ("地址还是 about:blank —— 内核没把 %s 打开（导航都没开始，或一开始就被取消）"
                       % (host or "真站"))
    if host and host not in href:
        return False, "地址被跳到了 %s（不是 %s）" % (href[:70], host)
    if nav_done and nav_ok is False:
        hint = page.get("error_hint") or ""
        return False, "导航失败：%s%s" % (page.get("error_code") or "内核报错",
                                     ("（%s）" % hint) if hint else "")
    if isinstance(http, int) and http >= 400:
        return False, "站上返回了 HTTP %d（页面是错误页）" % http
    # 导航这一层没问题了，再看页面里到底有没有东西
    html_len = report.get("html_len")
    text = str(report.get("text") or "")
    if page.get("js_ok"):
        if len(text) >= 2:
            return True, "标题「%s」，可见文字 %d 字，HTML %s 字符，HTTP %s" % (
                (report.get("title") or ""), len(text), html_len if html_len is not None else "?",
                (report.get("status") or http or "?"))
        if isinstance(html_len, int) and html_len >= 4000:
            return True, "标题「%s」，HTML %d 字符（页面无文字节点，按渲染成功算）" % (
                (report.get("title") or ""), html_len)
        return False, "页面打开了但内容几乎是空的（HTML %s 字符、可见文字 %d 字，HTTP %s）" % (
            html_len if html_len is not None else "?", len(text), (report.get("status") or http or "?"))
    # JS 通道没回话：导航成功就算成功，但要在原因里写清楚只验证到哪一步
    if nav_done and nav_ok is True:
        return True, "导航成功（HTTP %s）—— 脚本通道没回话，只验证到导航这一层" % (
            http if http is not None else "?")
    if nav_done and nav_ok is None:
        return True, "导航结束（内核没给出成败标志，HTTP %s）" % (http if http is not None else "?")
    return False, "导航还没结束（HTTP %s）" % (http if http is not None else "?")


def _inspect_real_site(win, timeout: float = _SITE_TIMEOUT, poll: float = 0.4,
                       host: str = "dola.com", probe: dict = None) -> dict:
    """看真站到底渲染出来没有。

    不再用 pywebview 的 evaluate_js（它会永久卡死，见 _install_page_probe 的注释）：
    - 主证据：.NET 的 NavigationCompleted 事件（地址、成败、HTTP 码、WebErrorStatus）；
    - 辅助证据：我们自己的 ExecuteScriptAsync 探针（页面的标题 / 正文长度 / 可见文字）。
    两条路都是「发起后不等」，不会把线程卡住。
    """
    state = _install_page_probe(win, state=probe)
    if not state:
        return {"ok": False, "why": "拿不到浏览器内核（窗口还没建好）", "report": {},
                "error": "no core", "page": {}}
    start_js = state.get("_start_js")
    deadline = time.time() + max(0.0, timeout)
    last_js = 0.0
    last_hook = time.time()
    page = _page_state(state)
    while True:
        page = _page_state(state)
        ok, why = _judge_page_state(page, host)
        if ok:
            return {"ok": True, "why": why, "report": page.get("report") or {},
                    "error": page.get("js_error") or "", "page": page}
        if time.time() >= deadline:
            break
        # 定期重发 JS 探针（页面还在加载时拿不到 DOM；通道坏了也只是记一笔，不阻塞）
        now = time.time()
        if start_js is not None and now - last_js >= 0.7:
            last_js = now
            if not _ui_invoke_bounded(state["_holder"], start_js, timeout=3.0):
                state["js_error"] = "内核没回话（发起脚本超时）"
        # 导航监听还没挂上（窗口刚建、内核未就绪）→ 每隔一会儿再补挂一次，
        # 免得监听装晚了漏掉这次导航的完成事件
        if not state.get("hooked") and now - last_hook >= 1.5:
            last_hook = now
            _install_page_probe(win, state=state)
        time.sleep(poll)
    page = _page_state(state)
    ok, why = _judge_page_state(page, host)
    return {"ok": ok, "why": why, "report": page.get("report") or {},
            "error": page.get("js_error") or "", "page": page}


def _format_report(info: dict, proxy_url: str, version: str) -> str:
    """把自检结果排成一段可以直接复制发出来的文字。"""
    rep = info.get("report") or {}
    page = info.get("page") or {}
    if page.get("nav_done"):
        nav = "成功" if page.get("nav_ok") else ("失败：%s" % (page.get("error_code") or "内核报错"))
    elif page.get("started"):
        nav = "还没结束"
    else:
        nav = "从未开始"
    lines = [
        "  · 结论：%s" % info.get("why", ""),
        "  · 地址：%s" % (page.get("href") or rep.get("href") or "(空)"),
        "  · 导航：%s ｜ HTTP=%s ｜ 就绪=%s" % (nav, page.get("http", "?"), rep.get("ready") or "?"),
        "  · 脚本通道：%s" % ("可用" if page.get("js_ok") else (page.get("js_error") or "没回话")),
        "  · 标题：%s" % (rep.get("title") or "(空)"),
        "  · 正文：HTML %s 字符，可见文字 %s 字" % (
            rep.get("html_len") if rep.get("html_len") is not None else "?",
            len(str(rep.get("text") or ""))),
        "  · 本机代理页：%s" % proxy_url,
        "  · 启动器版本：v%s" % (version or "?",),
    ]
    return "\n".join(lines)


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _diagnose_html(info: dict, real_url: str, proxy_url: str, title: str,
                   version: str, net_note: str = "", base_url: str = "") -> str:
    """真站没渲染出来时显示这一页：把看到的事实和下一步都写清楚。

    页面上的按钮都是**普通链接**：/api/desktop/open-external 是本机服务的一个端点，
    由桌面进程去开系统浏览器 —— 这样窗口里就不需要暴露任何 Python 对象。
    """
    rep = info.get("report") or {}
    base = (base_url or "").rstrip("/")
    open_endpoint = (base + "/api/desktop/open-external?url="
                     + urllib.parse.quote(proxy_url or "", safe="")) if base else (proxy_url or "")
    rep = info.get("report") or {}
    page = info.get("page") or {}
    nav = "从未开始（内核没发起导航）"
    if page.get("nav_done"):
        nav = "成功" if page.get("nav_ok") else ("失败：%s%s" % (
            page.get("error_code") or "内核报错",
            ("（%s）" % page.get("error_hint")) if page.get("error_hint") else ""))
    elif page.get("started"):
        nav = "还没结束（正在连 %s）" % str(page.get("started"))[:60]
    js_state = "可用" if page.get("js_ok") else ("没回话：%s" % (page.get("js_error") or "未知原因"))
    http = page.get("http") if page.get("http") is not None else rep.get("status", "?")
    rows = [
        ("结论", info.get("why", "")),
        ("地址", page.get("href") or rep.get("href") or "(空)"),
        ("导航结果", nav),
        ("HTTP 状态", str(http)),
        ("页面标题", rep.get("title") or "(空)"),
        ("HTML 长度", "%s 字符" % (rep.get("html_len") if rep.get("html_len") is not None else "?")),
        ("可见文字", "%s 字" % len(str(rep.get("text") or ""))),
        ("脚本通道", js_state),
        ("内核网络", net_note or "（见黑窗口日志）"),
        ("启动器版本", "v%s" % (version or "?")),
    ]
    table = "".join(
        '<tr><th>%s</th><td>%s</td></tr>' % (_esc(k), _esc(v)) for k, v in rows)
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>真站打不开 · 诊断</title><style>
html,body{margin:0;background:#0a0f11;color:#dfeae8;
font:14px/1.75 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 22px 40px}
h1{font-size:19px;margin:0 0 6px;color:#ffd479}
.sub{color:#9db3b1;font-size:13px;margin-bottom:18px}
table{border-collapse:collapse;width:100%%;margin:0 0 18px;font-size:13px}
th,td{border:1px solid #2b3a38;padding:7px 10px;text-align:left;vertical-align:top;word-break:break-all}
th{width:112px;color:#9db3b1;font-weight:600;background:#101b1a}
td{font-family:ui-monospace,Consolas,monospace}
h2{font-size:15px;margin:20px 0 8px;color:#cfe3e0}
li{margin-bottom:7px;color:#c9d8d6}
code{background:#101b1a;border:1px solid #2b3a38;border-radius:5px;padding:1px 6px;
font-family:ui-monospace,Consolas,monospace}
a.btn{display:inline-block;margin:14px 10px 0 0;padding:10px 16px;border-radius:9px;
background:#0d6f62;color:#fff;text-decoration:none;font-weight:600;cursor:pointer;border:0;font-size:13.5px}
a.btn.alt{background:#2a3a38}
.note{margin-top:22px;padding-top:14px;border-top:1px solid #2b3a38;color:#93a8a6;font-size:12.5px}
</style></head><body><div class="wrap">
<h1>真站没能在窗口里显示</h1>
<div class="sub">这是个诊断页 —— 下面是我从浏览器内核里问到的实情，不是猜的。</div>
<table>""" + table + """</table>
<h2>接下来可以这样</h2>
<ul>
  <li><b>改看本机代理页</b>：由服务端带着这个账号的 Cookie 去取 Dola，地址是本机，一定能打开
      （上面按钮 / 或点窗口右键 → 刷新）。</li>
  <li><b>用系统浏览器打开</b>：那个浏览器走系统代理，往往能看到页面，也能对照是不是网络问题。</li>
  <li><b>配一下代理</b>：如果 <code>DOLA_PROXY</code> 只写在环境变量里，浏览器内核读不到 ——
      在程序目录的 <code>config.json</code> 里加一行
      <code>"proxy": "http://用户:密码@主机:6666"</code>，或启动时加 <code>--proxy</code>，重启程序即可。</li>
    <li><b>点了就卡死 / 未响应</b>：那是 v2.0.10 及更早版本的 bug —— 窗口里挂了 JS 桥，真站脚本一碰就递归爆栈（黑窗口里能看到满屏 Empty. + maximum recursion depth exceeded）。v2.0.11 起账号窗口不再暴露任何 Python 对象，也没有 JS 桥。</li>
<li><b>做个对照实验</b>：关掉程序，改用 <code>--no-proxy</code> 启动一次（或把
      <code>config.json</code> 里的 <code>proxy</code> 删掉）—— 能分辨是「代理配错了」
      还是「内核本身取不回页面」。两种结果都告诉我。</li>
  <li><b>把这一页告诉我</b>：连同黑窗口里 <code>[desktop] v%s 自检</code> 那几行，我按里面的数字继续修。</li>
</ul>
<a class="btn" href="%s">◀ 改看本机代理页</a>
<a class="btn alt" href="%s">↻ 重新打开真站</a>
<a class="btn alt" href="%s">🌐 用系统浏览器打开</a>
<div class="note">要打开的真站：%s<br>本机代理页：%s<br>窗口：%s</div>
<div class="note">这三个按钮都是普通链接、加上本机的一个 HTTP 端点，不依赖注入脚本 ——
页面卡住时照样能用。</div>
</div></body></html>""" % (version or "?", _esc(proxy_url), _esc(real_url), _esc(open_endpoint),
                      _esc(real_url), _esc(proxy_url), _esc(title))


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
<div style="opacity:.45;margin-top:12px;font-size:12.5px"><!--VER--></div>
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

    state = {"done": False, "count": 0, "err": "", "fatal": False, "fails": 0, "holder": None}

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
            state["fails"] = 0                 # 能拿到内核说明前面那次异常是暂时的
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
            state["fails"] += 1
            if state["fails"] >= _INJECT_RETRY_LIMIT:
                state["fatal"] = True        # 反复失败就别耗到超时了

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
        self._sessions = {}         # account_id → 会话记录（窗口/登录态/状态）
        self._kernel_account = None # 内核 Cookie 里现在是谁的登录态
        self.net_note = ""          # 内核网络说明（代理还是直连），写进诊断页
        self.demo = False           # 演示模式：账号窗口不碰真站，走本机代理页

    def __call__(self, action: str, payload=None) -> None:
        """由后端接口触发。"""
        if action == "open_web":
            self.open_web(payload or {})
            return
        if action == "session":
            self.session(payload or {})
            return
        if action == "open_external":
            # 账号窗口里的「用系统浏览器打开」：走本机 HTTP 端点进来，不经任何 JS 桥
            target = str((payload or {}).get("url") or "").strip()
            print(f"[desktop] 用系统浏览器打开：{target}")
            if target:
                _open_in_browser(target)
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

    # ---------------- 账号会话：前台打开 / 后台挂起 / 切换 / 关闭 ----------------
    def session(self, payload: dict) -> None:
        """账号窗口的会话指令入口（由后端 /api/accounts/{id}/session 触发）。"""
        payload = payload or {}
        action = str(payload.get("action") or "open").strip().lower()
        if action == "close":
            self.close_session(payload.get("account_id"))
            return
        if action == "suspend":
            self.suspend_web(payload)
            return
        if action == "activate":
            info = self._prepare_payload(payload, hidden=False)
            if info is None:
                return
            rec = self._sessions.get(info["account_id"])
            if rec is not None and self._win_alive(rec.get("win")):
                self._activate(rec, info)
            else:
                self._create_window(info, hidden=False)
            return
        self.open_web(payload)

    def _prepare_payload(self, payload: dict, hidden: bool = False):
        url = str(payload.get("url") or "")
        if not url:
            return None
        if url.startswith("/"):
            url = (self.base_url or "").rstrip("/") + url
        account_id = payload.get("account_id")
        try:
            account_id = int(account_id) if account_id is not None else None
        except (TypeError, ValueError):
            account_id = None
        return {
            "url": url,
            "title": str(payload.get("title") or "Dola 账号网页"),
            "real": str(payload.get("real") or "").strip(),          # 真实 dola.com 地址（可空）
            "cookie": str(payload.get("cookie") or "").strip(),      # 该账号的 Cookie
            "domain": str(payload.get("domain") or "").strip() or ".dola.com",
            "account_id": account_id,
            "hidden": bool(hidden),
        }

    def open_web(self, payload: dict) -> None:
        """前台打开账号网页：已有窗口就直接显示（秒切），没有才建。"""
        info = self._prepare_payload(payload, hidden=False)
        if info is None:
            return
        rec = self._sessions.get(info["account_id"])
        if rec is not None and self._win_alive(rec.get("win")):
            rec.update({"cookie": info["cookie"] or rec.get("cookie"),
                        "real": info["real"] or rec.get("real"),
                        "url": info["url"] or rec.get("url"),
                        "title": info["title"] or rec.get("title"),
                        "domain": info["domain"] or rec.get("domain")})
            self._activate(rec, info)
            return
        self._create_window(info, hidden=False)

    def suspend_web(self, payload: dict) -> None:
        """后台挂起：窗口建好但不显示（hidden），先把登录态和真站页面准备好。

        这样切到这个账号时只是「把窗口显示出来」，不用等加载。
        注意内核（WebView2）的 Cookie 存储是所有窗口共用的：别的账号写入登录态后，
        挂起的窗口会变成 stale，切回来自动重连（注入 Cookie + 刷新）。
        """
        info = self._prepare_payload(payload, hidden=True)
        if info is None:
            return
        rec = self._sessions.get(info["account_id"])
        if rec is not None and self._win_alive(rec.get("win")):
            rec.update({"cookie": info["cookie"] or rec.get("cookie"),
                        "real": info["real"] or rec.get("real"),
                        "title": info["title"] or rec.get("title")})
            self._hide(rec.get("win"))
            state = "parked"
            note = "已挂起在后台（内核登录态还是它，切换秒开）"
            if rec.get("injected") and self._kernel_account != info["account_id"]:
                state, note = "stale", "已挂起在后台（登录态被别的账号顶掉，切换时会自动重连）"
            self._publish(rec, state, note=note)
            return
        self._create_window(info, hidden=True)

    def close_session(self, account_id) -> None:
        """关掉某个账号的窗口（会话记录一起清掉）。"""
        try:
            account_id = int(account_id) if account_id is not None else None
        except (TypeError, ValueError):
            account_id = None
        if account_id is None:
            return
        rec = self._sessions.get(account_id)
        if rec is None:
            self._publish_closed(account_id)
            return
        win = rec.get("win")
        self._forget(rec)
        try:
            win.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 关闭账号窗口失败：{exc}")

    def _forget(self, rec: dict) -> None:
        """把窗口从会话表里摘掉（用户自己关窗 / 我们主动关，都走这里）。"""
        win = rec.get("win")
        if win in self.web_windows:
            self.web_windows.remove(win)
        account_id = rec.get("account_id")
        if account_id is not None and self._sessions.get(account_id) is rec:
            self._sessions.pop(account_id, None)
            self._publish_closed(account_id)
        if self.web_account == account_id:
            self.web_account = None
        if self._kernel_account == account_id:
            self._kernel_account = None

    def _publish_closed(self, account_id) -> None:
        try:
            if _APP_MODULE is not None:
                _APP_MODULE.session_update(account_id, "closed")
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 会话状态回写失败（不影响窗口）：{exc}")

    def _publish(self, rec: dict, state: str, note: str = "") -> None:
        """把会话状态写给后端（账号池页面用它显示「前台/挂起/已关闭」）。"""
        rec["state"] = state
        if note:
            rec["note"] = note
        account_id = rec.get("account_id")
        if account_id is None:
            return
        try:
            if _APP_MODULE is not None:
                _APP_MODULE.session_update(account_id, state, note=rec.get("note") or "",
                                           title=rec.get("title") or "",
                                           url=rec.get("real") or rec.get("url") or "")
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 会话状态回写失败（不影响窗口）：{exc}")

    @staticmethod
    def _win_alive(win) -> bool:
        return win is not None and hasattr(win, "load_url")

    def _show(self, win) -> None:
        try:
            win.show()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 显示窗口失败（{exc}）")

    def _hide(self, win) -> None:
        try:
            win.hide()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 挂起窗口失败（{exc}）")

    def _hide_others(self, keep_rec: dict) -> None:
        """切到某个账号时，把别的账号窗口收进后台（不销毁，切回去更快）。"""
        for aid, rec in list(self._sessions.items()):
            if rec is keep_rec or not self._win_alive(rec.get("win")):
                continue
            self._hide(rec.get("win"))
            if rec.get("state") == "running":
                state = "parked"
                note = "已挂起在后台（切回去很快）"
                if rec.get("injected") and self._kernel_account != aid:
                    state, note = "stale", "已挂起（登录态被别的账号顶掉，切回自动重连）"
                self._publish(rec, state, note=note)

    def _mark_kernel_owner(self, account_id) -> None:
        """记住内核 Cookie 里现在是谁的登录态；别的账号窗口标记为「需重连」。"""
        self._kernel_account = account_id
        if account_id is None:
            return
        for aid, rec in list(self._sessions.items()):
            if aid == account_id or not rec.get("injected"):
                continue
            if rec.get("state") in ("running", "parked", "preparing"):
                self._publish(rec, "stale",
                              note="登录态被账号 #%s 顶掉，切回它时会自动重连" % account_id)

    def _create_window(self, info: dict, hidden: bool = False):
        """新建账号窗口（hidden=True 就是「后台挂起打开」）。

        注意：pywebview 只有在 start() 之后、且从非主线程调用 create_window，
        新窗口才会立刻出现（见 pywebview 源码 create_window 末尾）。
        这里的调用来自后端 uvicorn 线程，正好满足条件。
        """
        account_id = info["account_id"]
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 没装 pywebview（{exc}），改用系统浏览器")
            _open_in_browser(info["url"])
            return None
        if getattr(webview, "guilib", None) is None:
            # --headless / --browser / 原生窗口启动失败时没有 GUI 循环，
            # 此时 create_window 只会静默返回一个不显示的窗口，所以直接走浏览器
            print("[desktop] 当前没有原生窗口环境，改用系统浏览器打开账号网页")
            _open_in_browser(info["url"])
            return None
        # 浏览器内核的 Cookie 存储是所有窗口共用的：换账号时把上一个账号的窗口收进后台，
        # 免得两个窗口的登录态互相顶（切回去时再自动重连）
        if account_id is not None and self.web_account not in (None, account_id):
            self._hide_others(None)
        # 能注入 Cookie 就直接开真站；否则退回本机代理页
        # （演示模式不碰外网：不写内核 Cookie、不开真站）
        try_cookie = bool(info["real"] and info["cookie"] and _can_inject_cookies() and not self.demo)
        if self.demo and info["real"] and info["cookie"]:
            print("[desktop] 演示模式：账号窗口用本机代理页（演示模式不访问真站）")
        try:
            # 真站窗口连注入脚本也不要有（页面里不留 window.pywebview）
            _wrap_injection_skip()
            # 注意：这里**故意不传 js_api** —— 暴露 Python 对象会被 pywebview 的
            # 注入桥递归遍历、把进程搞崩（v2.0.11 已修的崩溃根因）。
            win = webview.create_window(
                info["title"], "about:blank" if try_cookie else info["url"],
                width=1180, height=860,
                min_size=(720, 520),
                text_select=True,
                hidden=bool(hidden),
            )
            _skip_injection_for(win)     # 标记：这个窗口的页面别再注入 pywebview
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 打开账号网页窗口失败（{exc}），改用系统浏览器")
            _open_in_browser(info["url"])
            return None
        rec = {"win": win, "account_id": account_id, "cookie": info["cookie"],
               "real": info["real"], "url": info["url"], "title": info["title"],
               "domain": info["domain"], "state": "closed", "note": "", "injected": False}
        self.web_windows.append(win)
        if account_id is not None:
            self._sessions[account_id] = rec
            self.web_account = account_id
        try:  # 用户自己关掉窗口后从会话表里摘掉，免得 accumulate
            win.events.closed += lambda: self._forget(rec)
        except Exception:  # noqa: BLE001
            pass
        if try_cookie:
            # 先摆一个「正在准备」页，别让用户对着空白窗口猜（挂起时也留着，反正看不见）
            try:
                # 不传 base_uri：各后端默认值不同（Windows 忽略、macOS 拿它建
                # NSURL），让它用 pywebview 自己的默认更稳
                win.load_html(_WAIT_HTML.replace(
                    "<!--VER-->", "启动器 v%s · 桌面版" % (_app_version() or "?")))
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] 显示等待页失败（不影响后续）：{exc}")
            self._publish(rec, "preparing",
                          note="正在后台准备（登录态 + 真站页面）" if hidden else "正在写入登录态…")
            # 尽量在导航之前就把 .NET 导航监听挂上：否则监听装晚了，
            # 真站这次导航的 NavigationCompleted 就漏掉了（自检只剩脚本探针一条证据）
            try:
                rec["probe"] = _install_page_probe(win)
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] 提前挂导航监听失败（不影响开窗，自检时会再挂）：{exc}")
            # 把 Cookie 写进内核，再打开真正的 dola.com（放后台线程，
            # 免得等内核就绪把接口调用卡住）
            threading.Thread(
                target=self._finish_cookie_window,
                args=(win, info["url"], info["real"], info["cookie"], info["domain"],
                      info["title"], rec, hidden),
                daemon=True,
            ).start()
        else:
            print(f"[desktop] 已打开账号网页窗口（本机代理页）：{info['title']}")
            self._publish(rec, "parked" if hidden else "running",
                          note="后台挂起（本机代理页，不占内核登录态）" if hidden else "前台显示中（本机代理页）")
        return rec

    def _activate(self, rec: dict, info: dict = None) -> None:
        """把已有窗口显示到前台；内核登录态不是它的话先重连（注入 Cookie + 刷新）。"""
        win = rec.get("win")
        self._hide_others(rec)
        self._show(win)
        account_id = rec.get("account_id")
        if account_id is not None:
            self.web_account = account_id
        need = bool(
            account_id is not None
            and rec.get("injected")
            and self._kernel_account != account_id
            and rec.get("cookie") and rec.get("real")
            and _can_inject_cookies() and not self.demo
            and rec.get("state") != "preparing"
        )
        if not need:
            self._publish(rec, "running", note="前台显示中（页面没动过，切换是秒开的）")
            return
        self._publish(rec, "preparing", note="登录态被别的账号顶掉了，正在重新写入…")
        self._set_win_title(win, rec["title"] + " · 正在重连…")
        threading.Thread(target=self._reconnect, args=(rec,), daemon=True).start()

    def _reconnect(self, rec: dict) -> None:
        """把某个账号的登录态重新写进内核并刷新页面（切回挂起窗口时用）。"""
        win = rec.get("win")
        version = _app_version() or "?"
        try:
            ok, note = _inject_webview2_cookies(win, rec.get("cookie") or "",
                                                rec.get("domain") or ".dola.com",
                                                timeout=_INJECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            ok, note = False, "注入过程出错（%s）" % exc
        if ok:
            rec["injected"] = True
            self._mark_kernel_owner(rec.get("account_id"))
            print(f"[desktop] 切换账号：{note}；刷新真站：{rec.get('real')}")
            self._navigate(win, rec.get("real") or rec.get("url") or "", rec.get("url") or "")
            self._set_win_title(win, rec["title"] + " · dola.com")
            self._publish(rec, "running", note="前台显示中（已重新写入登录态）")
        else:
            print(f"[desktop] 切换账号：注入不可用（{note}），改用本机代理页")
            self._navigate(win, rec.get("url") or "", "")
            self._set_win_title(win, rec["title"] + " · 本机代理页")
            self._publish(rec, "running", note="前台显示中（本机代理页：%s）" % note)
        if not ok or self.demo:
            return
        info = _inspect_real_site(win, host=_real_host(rec.get("real") or ""),
                                  probe=rec.get("probe"))
        if not info.get("ok"):
            print("[desktop] v%s 自检：重连后真站没渲染出来 ——\n%s"
                  % (version, _format_report(info, rec.get("url") or "", version)))
            self._publish(rec, "stale", note="重连后真站没渲染出来（窗口里有诊断页）")

    def _finish_cookie_window(self, win, proxy_url: str, real_url: str,
                              cookie: str, domain: str, title: str,
                              rec: dict = None, hidden: bool = False) -> None:
        """注入 Cookie 成功就打开真实 dola.com，失败就退回本机代理页。"""
        version = _app_version() or "?"
        try:
            ok, note = _inject_webview2_cookies(win, cookie, domain, timeout=_INJECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  任何意外都不能让窗口停在空白页
            ok, note = False, "注入过程出错（%s）" % exc
        if ok:
            print(f"[desktop] {note}；打开真实站点：{real_url}")
            target, suffix = real_url, " · dola.com"
            if rec is not None:
                rec["injected"] = True
                self._mark_kernel_owner(rec.get("account_id"))
        else:
            print(f"[desktop] Cookie 注入不可用（{note}），改用本机代理页：{proxy_url}")
            target, suffix = proxy_url, " · 本机代理页"
        # 先导航、再写标题：标题没写上也不能挡住导航
        self._navigate(win, target, proxy_url)
        self._set_win_title(win, title + suffix)
        if rec is not None:
            if hidden:
                self._publish(rec, "parked", note="已挂起在后台（真站页面已就绪，切换秒开）"
                              if ok else "已挂起在后台（本机代理页）")
            else:
                self._publish(rec, "running", note="前台显示中（真站）" if ok else "前台显示中（本机代理页）")
        if not ok or self.demo:
            return
        probe = (rec or {}).get("probe")
        if hidden:
            # 后台窗口看不见，别给用户弹诊断页；只用会话备注说明情况
            info = _inspect_real_site(win, host=_real_host(real_url), probe=probe)
            if info.get("ok"):
                print(f"[desktop] v{version} 挂起自检：真站已渲染 —— {info['why']}")
                if rec is not None:
                    self._publish(rec, "parked", note="已挂起在后台（真站已就绪，切换秒开）")
            else:
                print("[desktop] v%s 挂起自检：真站没渲染出来（窗口在后台，不弹诊断页）——\n%s"
                      % (version, _format_report(info, proxy_url, version)))
                if rec is not None:
                    self._publish(rec, "stale", note="后台挂起，但真站没渲染出来（切换过去看诊断页）")
            return
        # 打开了真站，但要确认它真的渲染出来了 —— 白屏不能再靠人肉发现
        info = _inspect_real_site(win, host=_real_host(real_url), probe=probe)
        if info["ok"]:
            print(f"[desktop] v{version} 自检：真站已渲染 —— {info['why']}")
            self._set_win_title(win, title + " · dola.com")
            if rec is not None:
                self._publish(rec, "running", note="前台显示中（真站已渲染）")
            return
        print("[desktop] v%s 自检：真站没渲染出来 ——\n%s"
              % (version, _format_report(info, proxy_url, version)))
        try:
            win.load_html(_diagnose_html(info, real_url, proxy_url, title, version,
                                         net_note=self.net_note, base_url=self.base_url))
            self._set_win_title(win, title + " · 真站打不开（窗口里有说明）")
            if rec is not None:
                self._publish(rec, "running", note="前台显示中，但真站打不开（窗口里有诊断页）")
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 诊断页也显示不出来（{exc}），退回本机代理页")
            self._navigate(win, proxy_url, "")
            self._set_win_title(win, title + " · 本机代理页")
            if rec is not None:
                self._publish(rec, "running", note="前台显示中（本机代理页）")

    @staticmethod
    def _navigate(win, target: str, fallback: str) -> None:
        """load_url；打不开就退到兜底地址（宁可显示兜底页，也别停在空白）。"""
        try:
            win.load_url(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 打开 {target} 失败（{exc}）")
            if fallback and fallback != target:
                try:
                    win.load_url(fallback)
                except Exception as exc2:  # noqa: BLE001
                    print(f"[desktop] 兜底地址也打不开：{exc2}")

    @staticmethod
    def _set_win_title(win, text: str) -> None:
        """标题栏写明走的是哪条路（真站 / 本机代理页 / 真站打不开）。"""
        try:
            win.title = text
            return
        except Exception:  # noqa: BLE001
            pass
        try:  # pywebview 的 title 要等页面加载完才认；直接改窗体文字更稳
            holder = _webview_holder(win)
            if holder is not None:
                _ui_invoke(holder, lambda: setattr(holder, "Text", text))
        except Exception:  # noqa: BLE001
            pass

    def close_web_windows(self) -> None:
        """主窗口关闭时把账号网页窗口一起收掉。

        否则 webview.start() 会因为还有窗口活着而不返回，程序就卡住不退出/不重启了。
        """
        for account_id in list(self._sessions.keys()):
            self.close_session(account_id)
        for win in list(self.web_windows):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.web_windows.clear()
        self.web_account = None
        self._kernel_account = None

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
    _make_output_robust()
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
    parser.add_argument("--proxy", default="",
                        help="给账号窗口用的代理，如 http://用户:密码@主机:6666"
                             "（默认读配置里的 proxy / DOLA_PROXY / HTTPS_PROXY）")
    parser.add_argument("--no-proxy", action="store_true", help="账号窗口直连，不用任何代理")
    args = parser.parse_args(argv)

    if args.demo:
        demo = True
    elif args.no_demo:
        demo = False
    else:
        demo = bool(cfg.get("demo", True))

    cfg.update({"demo": demo, "width": args.width, "height": args.height})
    if args.proxy:
        cfg["proxy"] = args.proxy
    elif args.no_proxy:
        cfg.pop("proxy", None)
    save_config(cfg)

    port = pick_port(args.port or int(cfg.get("port") or 0))
    control = Control()
    control.demo = demo
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

    # 内核（WebView2）的网络必须先准备好：WebView2 只在**创建浏览器环境时**读
    # WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS，等窗口建出来再设就没用了。
    control.net_note = _prepare_browser_network(
        {} if args.no_proxy else cfg, log=print, allow_env=not args.no_proxy).get("note", "")

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
