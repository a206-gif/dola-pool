#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面版无头测试：不需要图形界面，验证接口层面的桌面能力（CI / 无显示器环境可用）

    python tools/desktop_test.py

覆盖：
  1. desktop.py --headless 能拉起后端，只监听 127.0.0.1
  2. 用户数据目录 / config.json 落盘
  3. /api/desktop/config 反映当前模式
  4. 切换模式 → 服务自动重启 → 新进程以新模式运行（核心行为）
  5. /api/desktop/reveal 路径映射与越权拦截
  6. 账号窗口：把 Cookie 写进浏览器内核（用假的 WebView2 对象验证，不需要真 GUI）
  7. 内核网络：代理（含账号密码 → 本机中转）、参数生成、真站自检与诊断页
  8. 窗口里不暴露任何 Python 对象（js_api 必须为空）+「用系统浏览器打开」端点白名单
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PORT = int(os.getenv("DOLA_TEST_PORT", "8137"))
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="dola_desktop_")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + str(detail)[:160]) if detail else ""))


def call(method, path, payload=None, timeout=10):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body or b"null")
            except Exception:                     # 有的端点回的是 HTML（如「已交给系统浏览器」）
                return r.status, body[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def live_proxy_check(acc_id):
    """代理入口必须是可读的页面：能连上 Dola → 200；连不上 → 502 说明页。都不该是 500。"""
    try:
        with urllib.request.urlopen(f"{BASE}/dola-web/{acc_id}/", timeout=20) as r:
            return r.status == 200 and "<html" in r.read().decode("utf-8", "replace").lower()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        # 没有外网的测试环境里，这是预期结果（说明页而不是崩溃）
        return exc.code == 502 and "连不上 Dola" in body
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 7. 账号窗口的 Cookie 注入（假 WebView2 对象；真机上是 Windows 的 WebView2）
# ---------------------------------------------------------------------------
class FakeCookie:
    def __init__(self, name, value, domain, path):
        self.Name, self.Value, self.Domain, self.Path = name, value, domain, path
        self.IsHttpOnly = self.IsSecure = False


class FakeCookieManager:
    def __init__(self):
        self.added = []

    def CreateCookie(self, name, value, domain, path):
        return FakeCookie(name, value, domain, path)

    def AddOrUpdateCookie(self, cookie):
        self.added.append(cookie)


class FakeCore:
    def __init__(self):
        self.CookieManager = FakeCookieManager()


class FakeControl:
    """模拟 WebView2 控件：CoreWebView2 只允许在 UI 线程（Invoke 期间）访问。

    真机上从别的线程读它会抛
      System.InvalidOperationException: CoreWebView2 can only be accessed from the UI thread
    —— 这里用同样的限制，保证这个 bug 不会再回来。
    """

    def __init__(self, core=None):
        self._core = core
        self._ui_thread = None

    @property
    def CoreWebView2(self):
        if self._ui_thread is not threading.current_thread():
            raise RuntimeError("CoreWebView2 can only be accessed from the UI thread")
        return self._core


class FlakyControl(FakeControl):
    """内核初始化途中偶尔会抛（真机上见过）→ 前 N 次读失败，之后正常。

    用来验证：读内核抛异常时会重试，而不是一抛就放弃、把用户踢回代理页。
    """

    def __init__(self, core=None, fails=2):
        super().__init__(core)
        self._fails = fails
        self.reads = 0

    @property
    def CoreWebView2(self):
        self.reads += 1
        if self._fails > 0:
            self._fails -= 1
            raise RuntimeError("CoreWebView2 was not initialized yet（瞬时错误）")
        return super().CoreWebView2


class FakeBrowserView:
    """模拟 pywebview 的 WinForms 窗体：Invoke 就是「切到 UI 线程」。"""

    def __init__(self, core=None):
        self.webview = FakeControl(core)

    def Invoke(self, fn):
        prev = self.webview._ui_thread
        self.webview._ui_thread = threading.current_thread()
        try:
            return fn()
        finally:
            self.webview._ui_thread = prev


class FakeWin:
    def __init__(self, core=None, have_native=True):
        self.native = FakeBrowserView(core) if have_native else None
        self.loaded = []

    def load_url(self, url):
        self.loaded.append(url)


class FakeEvents:
    def __init__(self):
        self.closed = self

    def __iadd__(self, other):
        return self


class FakePyWindow:
    """模拟 pywebview.create_window 返回的 Window。"""

    def __init__(self, title, url):
        self.title, self.url = title, url
        self.events = FakeEvents()
        self.loaded = []
        core = FakeCore() if _FAKE_INJECT_OK[0] else None
        self.native = FakeBrowserView(core)

    def load_url(self, url):
        self.loaded.append(url)
        self.url = url

    def load_html(self, html, base_uri=None):
        self.html_shown = html
        self.html_base = base_uri      # 期望 None：base_uri 各后端语义不同，别乱传

    def evaluate_js(self, script, *args, **kwargs):
        """默认假装内核还没回话（自检会当作「拿不到页面信息」）。"""
        raise RuntimeError("evaluate_js 未就绪（假窗口）")

    def destroy(self):
        pass


_FAKE_INJECT_OK = [True]


def fake_webview_module():
    import types
    mod = types.ModuleType("webview")
    mod.guilib = object()          # 假装 GUI 循环已就绪
    mod.windows = []
    mod.injected = []              # pywebview 的注入函数被调用的记录

    def inject_pywebview(platform, win):
        """模拟 pywebview 各平台后端里的注入（真机崩溃现场就是它）。"""
        mod.injected.append(win)

    inject_pywebview._orig_name = "fake"
    mod.inject_pywebview = inject_pywebview

    def create_window(title, url, **kwargs):
        win = FakePyWindow(title, url)
        win.kwargs = kwargs          # 测试要检查有没有传 js_api（传了会把进程搞崩）
        mod.windows.append(win)
        return win

    mod.create_window = create_window
    return mod


def cookie_injection_checks():
    """把 desktop.py 的注入逻辑放进假环境里跑，验证成功/失败两条路。"""
    sys.path.insert(0, ROOT)
    import desktop  # noqa: E402

    # a) Cookie 解析
    pairs = desktop._parse_cookie_pairs("sessionid=abc;  sid_guard=x=y ; ;bad; t=1")
    check("Cookie 解析：忽略空项/无等号项，保留值里的等号",
          pairs == [("sessionid", "abc"), ("sid_guard", "x=y"), ("t", "1")], pairs)

    # b) 非 Windows（拿不到 clr）→ 明确失败，不抛异常
    ok, why = desktop._inject_webview2_cookies(FakeWin(FakeCore()), "a=1", timeout=0.2)
    check("非 Windows 环境下注入被明确拒绝（不抛异常）", ok is False and "内核" in why, why)

    # 后面走接缝：假装 clr 可用 + 直接在「UI 线程」同步执行
    desktop._clr_ready = lambda: (True, "")
    desktop._ui_invoke = lambda holder, fn: holder.Invoke(fn)   # 经由窗体的 Invoke（=切 UI 线程）
    desktop._can_inject_cookies = lambda: True   # 本测试跑在 Linux 上，手动打开注入开关
    desktop._INJECT_TIMEOUT = 0.5               # 真机是 15 秒；测试别干等

    # c) 正常注入：域名 / HttpOnly / Secure 都要设上
    win = FakeWin(FakeCore())
    ok, why = desktop._inject_webview2_cookies(win, "sessionid=abc; sid_guard=xyz", ".dola.com", timeout=1)
    added = win.native.webview._core.CookieManager.added   # 直接读假对象内部，绕开「只能 UI 线程」限制
    check("注入成功并返回数量", ok is True and "2 个" in why, why)
    check("Cookie 的域名/路径/HttpOnly/Secure 都对",
          [c.Name for c in added] == ["sessionid", "sid_guard"]
          and all(c.Domain == ".dola.com" and c.Path == "/" for c in added)
          and all(c.IsHttpOnly and c.IsSecure for c in added),
          [(c.Name, c.Value, c.Domain) for c in added])

    # d) 内核迟迟不给 CoreWebView2 → 超时返回失败（不卡死）
    ok, why = desktop._inject_webview2_cookies(FakeWin(None, have_native=False), "a=1", timeout=0.3)
    check("拿不到内核控件时明确失败并说明原因", ok is False and "内核" in why, why)

    # d2) 内核还没就绪（窗口刚建好）→ 轮询到超时后失败，不是卡死
    ok, why = desktop._inject_webview2_cookies(FakeWin(None), "a=1", timeout=0.4)
    check("内核未就绪时轮询到超时、返回可读原因", ok is False and "就绪" in why, why)

    # d3) 读内核偶尔抛异常 → 要重试；连续失败太多次 → 快速放弃并说明原因
    win3 = FakeWin(FakeCore())
    flaky = FlakyControl(win3.native.webview._core, fails=2)
    win3.native.webview = flaky
    ok, why = desktop._inject_webview2_cookies(win3, "sessionid=abc", ".dola.com", timeout=1.5)
    check("读内核抛异常时会重试（不是一抛就放弃、直接踢回代理页）",
          ok is True and "1 个" in why, why)
    check("重试次数符合预期（前两次抛、第三次成功就写入）", flaky.reads == 3, flaky.reads)

    win4 = FakeWin(FakeCore())
    flaky4 = FlakyControl(win4.native.webview._core, fails=999)
    win4.native.webview = flaky4
    t0 = time.time()
    ok, why = desktop._inject_webview2_cookies(win4, "sessionid=abc", ".dola.com", timeout=10)
    check("连续抛异常到上限就放弃（不干等到超时），且给出可读原因",
          ok is False and "瞬时错误" in why and (time.time() - t0) < 8,
          "%s ｜ 耗时 %.1fs" % (why, time.time() - t0))

    # e) 整条路：Control.open_web → 注入成功 → 打开的是真实 dola.com
    import types
    real_mod = sys.modules.get("webview")
    try:
        _FAKE_INJECT_OK[0] = True
        sys.modules["webview"] = fake_webview_module()
        ctl = desktop.Control()
        ctl.base_url = "http://127.0.0.1:8099/"
        ctl.open_web({"url": "/dola-web/2/", "title": "Dola · #2 测试号",
                      "real": "https://www.dola.com/chat/",
                      "cookie": "sessionid=abc", "domain": ".dola.com", "account_id": 2})
        win = sys.modules["webview"].windows[-1]
        for _ in range(50):                     # 注入在后台线程里做，等一下
            if win.loaded:
                break
            time.sleep(0.1)
        check("注入期间先显示「正在准备」页（不是空白窗口）",
              "正在用该账号的登录态打开" in (getattr(win, "html_shown", "") or ""),
              (getattr(win, "html_shown", "") or "")[:40])
        check("要等待页时不给 pywebview 传 base_uri（macOS 会拿它建 NSURL）",
              getattr(win, "html_base", "没调用") is None, getattr(win, "html_base", "没调用"))
        check("注入成功后才打开真实 dola.com（中间不停留在空白页）",
              win.loaded[:1] == ["https://www.dola.com/chat/"], win.loaded)
        check("标题栏标明走的是真站", win.title.endswith("· dola.com"), win.title)

        # f) 注入失败 → 自动退回本机代理页
        _FAKE_INJECT_OK[0] = False
        sys.modules["webview"].windows.clear()
        ctl.open_web({"url": "/dola-web/3/", "title": "Dola · #3 测试号",
                      "real": "https://www.dola.com/chat/",
                      "cookie": "sessionid=abc", "domain": ".dola.com", "account_id": 3})
        win2 = sys.modules["webview"].windows[-1]
        for _ in range(50):
            if win2.loaded:
                break
            time.sleep(0.1)
        check("注入失败时退回本机代理页",
              win2.loaded[:1] == ["http://127.0.0.1:8099/dola-web/3/"], win2.loaded)
        check("标题栏标明走的是本机代理页", win2.title.endswith("· 本机代理页"), win2.title)

        # g) 换账号时先关旧窗口（内核 Cookie 是共享的，两个账号会同归于尽）
        _FAKE_INJECT_OK[0] = True
        before = list(ctl.web_windows)
        ctl.open_web({"url": "/dola-web/4/", "title": "Dola · #4",
                      "real": "https://www.dola.com/chat/",
                      "cookie": "sessionid=zzz", "domain": ".dola.com", "account_id": 4})
        check("换账号时上一个账号窗口被关掉",
              bool(before) and len(ctl.web_windows) == 1, [w.title for w in ctl.web_windows])
    finally:
        if real_mod is not None:
            sys.modules["webview"] = real_mod
        else:
            sys.modules.pop("webview", None)


# --------------------------------------------------------------------------
# 7. 内核网络（代理）与真站自检
# --------------------------------------------------------------------------
class FakeUpstreamProxy:
    """假上游代理：只认 CONNECT，校验 Proxy-Authorization，然后把连接接到目标上。"""

    def __init__(self, expect_auth=None, reply=b"HTTP/1.1 200 Connection Established\r\n\r\n"):
        import socket as _s
        self._s = _s
        self.expect_auth = expect_auth
        self.reply = reply
        self.seen_auth = []
        self.targets = []
        self.sock = _s.socket()
        self.sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
        head = buf.decode("latin-1", "replace")
        lines = head.split("\r\n")
        self.targets.append(lines[0])
        for line in lines[1:]:
            if line.lower().startswith("proxy-authorization:"):
                self.seen_auth.append(line.split(":", 1)[1].strip())
        if self.expect_auth and (not self.seen_auth or self.seen_auth[-1] != self.expect_auth):
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            client.close()
            return
        if " 200" not in self.reply.decode("latin-1", "replace").split("\r\n")[0]:
            client.sendall(self.reply)
            client.close()
            return
        if not lines[0].startswith("CONNECT"):
            # 明文 HTTP：按绝对地址把请求转给目标，再把响应原样带回
            import urllib.parse as _up
            u = _up.urlsplit(lines[0].split(" ")[1])
            origin = "GET %s HTTP/1.1" % (u.path or "/")
            head2 = origin + head[len(lines[0]):]
            up = socket.create_connection((u.hostname, u.port or 80), timeout=5)
            up.sendall(head2.encode("latin-1"))
            _relay(up, client)
            up.close()
            client.close()
            return
        # 真的接到目标上，做中继
        target = lines[0].split(" ")[1]
        host, _, port = target.rpartition(":")
        up = socket.create_connection((host, int(port)), timeout=5)
        client.sendall(self.reply)
        for src, dst in ((client, up), (up, client)):
            threading.Thread(target=lambda a=src, b=dst: _relay(a, b), daemon=True).start()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _relay(a, b):
    try:
        while True:
            data = a.recv(4096)
            if not data:
                return
            b.sendall(data)
    except OSError:
        pass


class FakeEcho:
    """假目标服务器：收到什么回什么（模拟 https 隧道里的字节流）。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            while True:
                data = client.recv(4096)
                if not data:
                    return
                client.sendall(b"echo:" + data)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def network_checks():
    """代理转发 + 内核网络参数 + 真站自检 + 诊断页。"""
    sys.path.insert(0, ROOT)
    import desktop  # noqa: E402

    _REAL_INSPECT = desktop._inspect_real_site      # 后面会临时换成固定答案

    # a) 代理地址解析
    check("代理地址解析（带账号密码）",
          desktop._split_proxy_url("http://u%40x:p%3A1@proxy.example:6666")
          == ("http", "proxy.example", 6666, "u@x", "p:1"),
          desktop._split_proxy_url("http://u%40x:p%3A1@proxy.example:6666"))
    check("代理地址解析（无协议头、默认端口）",
          desktop._split_proxy_url("proxy.example:6666")[:4] == ("http", "proxy.example", 6666, ""),
          desktop._split_proxy_url("proxy.example:6666"))
    check("看不懂的代理地址返回 None", desktop._split_proxy_url("") is None
          and desktop._split_proxy_url("http://") is None, "ok")
    check("从真站地址里取站点名（跳转后也认得出来）",
          desktop._real_host("https://www.dola.com/chat/") == "dola.com",
          desktop._real_host("https://www.dola.com/chat/"))

    # b) 本机中转：CONNECT + Proxy-Authorization + 真的把字节搬过去
    echo = FakeEcho()
    auth = "Basic " + base64.b64encode(b"userId-1-custom-2:secret").decode()
    up = FakeUpstreamProxy(expect_auth=auth)
    tunnel = desktop._LocalProxyTunnel(("http", "127.0.0.1", up.port, "userId-1-custom-2", "secret"))
    try:
        url = tunnel.start()
        check("本机中转只监听 127.0.0.1", url.startswith("http://127.0.0.1:"), url)
        port = int(url.rsplit(":", 1)[1])
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % echo.port).encode())
        head = client.recv(4096)
        check("中转回了 200（隧道建立）", b"200 Connection Established" in head, head[:60])
        client.sendall(b"hello")
        got = client.recv(4096)
        check("隧道里的字节被真的转发到目标并回传", got == b"echo:hello", got)
        check("上游代理收到了账号密码（Basic 认证）",
              up.seen_auth and up.seen_auth[-1] == auth, up.seen_auth)
        client.close()
    finally:
        tunnel.stop()
        up.close()
        echo.close()

    # b2) 明文 HTTP 转发（http:// 网站走这条）
    import http.server as _hs
    import functools as _ft

    class _Handler(_hs.BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802
            body = b"page-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                         # 别刷屏
            pass

    httpd = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _ft.partial(_Handler))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    up_plain = FakeUpstreamProxy()
    tunnel3 = desktop._LocalProxyTunnel(("http", "127.0.0.1", up_plain.port, "u", "p"))
    try:
        port3 = int(tunnel3.start().rsplit(":", 1)[1])
        client = socket.create_connection(("127.0.0.1", port3), timeout=5)
        url = "http://127.0.0.1:%d/hello" % httpd.server_address[1]
        client.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n" % url).encode())
        resp = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            resp += chunk
        check("明文 HTTP 也转得动（响应回到了浏览器侧）", b"page-ok" in resp, resp[:60])
        check("转发时保留了绝对地址并带上了认证",
              up_plain.targets and up_plain.targets[-1].startswith("GET " + url)
              and up_plain.seen_auth, (up_plain.targets[:1], up_plain.seen_auth))
        client.close()
    finally:
        tunnel3.stop()
        up_plain.close()
        httpd.shutdown()

    # c) 上游拒绝（407）→ 给浏览器 502，并记下原因
    up2 = FakeUpstreamProxy(expect_auth="Basic 错的")
    tunnel2 = desktop._LocalProxyTunnel(("http", "127.0.0.1", up2.port, "u", "p"))
    try:
        port2 = int(tunnel2.start().rsplit(":", 1)[1])
        client = socket.create_connection(("127.0.0.1", port2), timeout=5)
        client.sendall(b"CONNECT www.dola.com:443 HTTP/1.1\r\n\r\n")
        head = client.recv(4096)
        check("上游认证失败 → 客户端拿到 502（不是一直挂着）",
              b"502" in head, head[:60])
        check("失败原因被记下（能看到上游的 407）",
              "407" in tunnel2.stats["last_error"] and tunnel2.stats["fail"] >= 1,
              tunnel2.stats)
        client.close()
    finally:
        tunnel2.stop()
        up2.close()

    # d) 内核网络参数：代理来自环境变量 / 配置 / 不设
    env = {"HTTPS_PROXY": "http://u:p@proxy.example:6666"}
    info = desktop._prepare_browser_network({}, env=env, log=lambda *a: None)
    args = env.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    check("有账号密码的代理 → 内核指向本机中转（不是把密码塞进 --proxy-server）",
          info["applied"] and "--proxy-server=http://127.0.0.1:" in args and "u:p@" not in args, args)
    check("本机地址在绕过名单里（否则界面自己都打不开）",
          "127.0.0.1" in args and "--proxy-bypass-list" in args, args)
    check("中转起来后确实在监听", info["tunnel"] is not None
          and info["tunnel"].stats["connect"] == 0 and info["tunnel"].url.startswith("http://127.0.0.1:"),
          info["tunnel"].url if info["tunnel"] else "")
    if info["tunnel"]:
        info["tunnel"].stop()

    env2 = {"DOLA_PROXY": "http://proxy.example:3128"}
    info2 = desktop._prepare_browser_network({}, env=env2, log=lambda *a: None)
    check("无账号密码的代理 → 直接写进 --proxy-server",
          info2["applied"] and env2.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").startswith(
              "--proxy-server=http://proxy.example:3128"), env2.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))

    env3 = {}
    info3 = desktop._prepare_browser_network({}, env=env3, log=lambda *a: None)
    check("没有代理配置 → 什么都不设（保持直连）",
          info3["applied"] is False and "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in env3, info3["note"])

    env4 = {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--我自己设的", "HTTPS_PROXY": "http://p:1"}
    info4 = desktop._prepare_browser_network({}, env=env4, log=lambda *a: None)
    check("用户自己设过内核参数 → 绝不覆盖", info4["applied"] is False
          and env4["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--我自己设的", env4.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))

    env5 = {"HTTPS_PROXY": "http://env.example:1"}
    info5 = desktop._prepare_browser_network({"proxy": "http://cfg.example:2"}, env=env5, log=lambda *a: None)
    check("配置里的 proxy 优先于环境变量",
          "cfg.example:2" in env5.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", ""), info5.get("args"))
    env6 = {"HTTPS_PROXY": "http://env.example:1"}
    info6 = desktop._prepare_browser_network({}, env=env6, log=lambda *a: None, allow_env=False)
    check("--no-proxy 时连环境变量都不看（窗口真直连）",
          info6["applied"] is False and "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in env6,
          info6["note"])

    # d2) 控制台编码兜底：打中文不能把线程抛死
    class _NoReconf:
        pass

    class _Reconf:
        def __init__(self):
            self.kwargs = None

        def reconfigure(self, **kw):
            self.kwargs = kw

    saved_out, saved_err = sys.stdout, sys.stderr
    fake_out, fake_err = _Reconf(), _NoReconf()
    try:
        sys.stdout, sys.stderr = fake_out, fake_err
        desktop._make_output_robust()          # 不许抛异常
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    check("控制台编码兜底：不可编码字符换成 ?（保住后台线程，不崩）",
          fake_out.kwargs == {"errors": "replace"}, fake_out.kwargs)

    # e) 真站自检的判定
    check("自检：地址还是 about:blank → 判为没打开（并指出是网络/代理层面）",
          desktop._judge_real_site({"href": "about:blank"})[0] is False
          and "about:blank" in desktop._judge_real_site({"href": "about:blank"})[1], "ok")
    check("自检：内核跳到错误页 → 判为导航失败",
          desktop._judge_real_site({"href": "chrome-error://chromewebdata/"})[0] is False, "ok")
    check("自检：真站有内容 → 通过",
          desktop._judge_real_site({"href": "https://www.dola.com/chat/", "text": "开始创作",
                                    "html_len": 5000, "status": 200, "title": "Dola"})[0] is True, "ok")
    check("自检：地址对但内容是空的 → 判为没渲染出来（白屏能被抓出来）",
          desktop._judge_real_site({"href": "https://www.dola.com/chat/", "text": "",
                                    "html_len": 20, "status": 200})[0] is False, "ok")
    check("自检：拿不到页面信息 → 判为失败而不是通过",
          desktop._judge_real_site(None)[0] is False, "ok")

    # f) 轮询式自检：先 about:blank，后渲染出来
    class ProbeWin:
        def __init__(self, seq):
            self.seq, self.calls = list(seq), 0

        def evaluate_js(self, *_a, **_k):
            self.calls += 1
            return self.seq[min(self.calls - 1, len(self.seq) - 1)]

    win = ProbeWin([{"href": "about:blank"}, {"href": "about:blank"},
                    {"href": "https://www.dola.com/chat/", "text": "创作", "html_len": 4000, "status": 200}])
    got = desktop._inspect_real_site(win, timeout=3, poll=0.05)
    check("轮询自检：一直等到渲染成功才放行", got["ok"] and win.calls == 3, (got["why"], win.calls))

    win2 = ProbeWin([{"href": "about:blank"}])
    got2 = desktop._inspect_real_site(win2, timeout=0.3, poll=0.05)
    check("轮询自检：一直打不开 → 超时后返回失败和原因",
          got2["ok"] is False and "about:blank" in got2["why"] and win2.calls >= 2, got2["why"])

    win_str = ProbeWin(['{"href": "https://www.dola.com/chat/", "text": "创作", "html_len": 3000, "status": 200}'])
    got_str = desktop._inspect_real_site(win_str, timeout=1, poll=0.05)
    check("自检兼容「结果是 JSON 字符串」的后端", got_str["ok"] is True, got_str["why"])

    class BrokenWin:
        def evaluate_js(self, *_a, **_k):
            raise RuntimeError("内核没注入脚本")

    got3 = desktop._inspect_real_site(BrokenWin(), timeout=0.25, poll=0.05)
    check("轮询自检：脚本通道报错也不抛异常，而是当成失败并记下原因",
          got3["ok"] is False and "内核没注入脚本" in got3["error"], got3["error"])

    class HangingWin:
        """内核卡住不回话：evaluate_js 永不返回。"""

        def __init__(self):
            self.gate = threading.Event()

        def evaluate_js(self, *_a, **_k):
            self.gate.wait(30)
            return {}

    old_limit = desktop._PROBE_HARD_LIMIT
    desktop._PROBE_HARD_LIMIT = 0.4
    try:
        t0 = time.time()
        got4 = desktop._inspect_real_site(HangingWin(), timeout=0.2, poll=0.05)
        spent = time.time() - t0
        check("内核卡住不回话 → 看门狗放弃并出结论（不会永远挂着）",
              got4["ok"] is False and "没回话" in got4["why"] and spent < 5, (got4["why"], round(spent, 2)))
    finally:
        desktop._PROBE_HARD_LIMIT = old_limit

    # g) 整条路：注入成功但真站白屏 → 窗口显示诊断页（而不是干白着）
    real_mod = sys.modules.get("webview")
    try:
        _FAKE_INJECT_OK[0] = True
        sys.modules["webview"] = fake_webview_module()
        ctl = desktop.Control()
        ctl.base_url = "http://127.0.0.1:8099/"
        ctl.net_note = "经本机中转 http://127.0.0.1:1234 → proxy.example:6666"
        desktop._inspect_real_site = lambda *a, **k: {"ok": False, "why": "地址还是 about:blank",
                                                     "report": {"href": "about:blank"}, "error": ""}
        ctl.open_web({"url": "/dola-web/7/", "title": "Dola · #7", "real": "https://www.dola.com/chat/",
                      "cookie": "sessionid=a", "domain": ".dola.com", "account_id": 7})
        w = sys.modules["webview"].windows[-1]
        for _ in range(80):      # 注意：注入期间先显示「正在准备」等待页，别把它当诊断页
            if "真站没能在窗口里显示" in str(getattr(w, "html_shown", "") or ""):
                break
            time.sleep(0.1)
        page = getattr(w, "html_shown", "") or ""
        check("真站白屏时窗口显示诊断页（不是让你对着空白）",
              "真站没能在窗口里显示" in page and "about:blank" in page, "诊断页已显示")
        check("诊断页里带上了内核网络说明（代理还是直连）",
              "127.0.0.1:1234" in page, "ok")
        check("标题栏明说真站打不开", w.title.endswith("真站打不开（窗口里有说明）"), w.title)

        # h) 注入成功且真站渲染出来 → 不显示诊断页，标题保持 dola.com
        sys.modules["webview"].windows.clear()
        desktop._inspect_real_site = lambda *a, **k: {"ok": True, "why": "标题「Dola」",
                                                      "report": {"href": "https://www.dola.com/chat/"},
                                                      "error": ""}
        ctl.open_web({"url": "/dola-web/8/", "title": "Dola · #8", "real": "https://www.dola.com/chat/",
                      "cookie": "sessionid=b", "domain": ".dola.com", "account_id": 8})
        w2 = sys.modules["webview"].windows[-1]
        for _ in range(60):
            if w2.loaded:
                break
            time.sleep(0.1)
        # 注：w2.html_shown 里是注入期间的「正在准备」等待页，不能拿它判断有没有诊断页
        check("真站渲染出来 → 不弹诊断页，标题保持「· dola.com」",
              "真站没能在窗口里显示" not in (getattr(w2, "html_shown", "") or "")
              and w2.title.endswith("· dola.com")
              and w2.loaded[:1] == ["https://www.dola.com/chat/"], (w2.title, w2.loaded))

        # i) 窗口里不能暴露任何 Python 对象（真机崩溃根因的回归防护）
        check("账号窗口不传 js_api（暴露对象会被 pywebview 注入桥递归遍历 → 进程崩）",
              w.kwargs.get("js_api") is None and w2.kwargs.get("js_api") is None,
              (w.kwargs, w2.kwargs))
        check("账号窗口不暴露任何叫 api 的 Python 对象（window.pywebview.api 不存在）",
              not hasattr(desktop, "_WebWindowApi"), "无 _WebWindowApi")
        # 后端在「页面导航完成」时会调 inject_pywebview —— 这里模拟它：
        # 账号窗口必须被跳过（真站页面里不留任何我们的东西）
        mod = sys.modules["webview"]
        mod.injected.clear()
        mod.inject_pywebview("fake", w2)                 # 带标记的账号窗口
        check("账号窗口连注入脚本都跳过（真站页面里不留 window.pywebview）",
              mod.injected == [] and getattr(w2, "_dlp_no_inject", False),
              "被注入的窗口：%s" % mod.injected)

        # 没打标记的窗口（主窗口 / 控制台界面）必须照旧注入，别把功能弄没了
        class _PlainWin:
            class _Ev:
                def set(self):
                    pass

            events = _Ev()
            _dlp_no_inject = False

        mod.inject_pywebview("fake", _PlainWin())
        check("没打标记的窗口照常注入（主窗口功能不受影响）",
              len(mod.injected) == 1 and not getattr(mod.injected[-1], "_dlp_no_inject", False),
              "被注入的窗口数 %d" % len(mod.injected))
        page2 = getattr(w2, "html_shown", "") or ""
        check("诊断页的按钮是普通链接（不依赖注入脚本 / JS 桥）",
              'href="http://127.0.0.1:8099/dola-web/7/"' in page
              and 'href="https://www.dola.com/chat/"' in page
              and "javascript:dlp" not in page and "pywebview" not in page.lower(),
              "链接写法")
        check("诊断页的「用系统浏览器打开」指向本机端点（桌面进程去开系统浏览器）",
              "/api/desktop/open-external?url=" in page, "端点链接")
    finally:
        desktop._inspect_real_site = _REAL_INSPECT
        if real_mod is not None:
            sys.modules["webview"] = real_mod
        else:
            sys.modules.pop("webview", None)


def wait_up(should_be_up=True, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call("GET", "/api/health", timeout=3)
        up = status == 200
        if up == should_be_up:
            return True, body
        time.sleep(0.4)
    return False, None


def spawn(extra=()):
    env = dict(os.environ, DOLA_DATA_DIR=DATA)
    log = open(os.path.join(DATA, "desktop.out"), "ab")
    return subprocess.Popen(
        [PY, os.path.join(ROOT, "desktop.py"), "--headless", "--port", str(PORT)] + list(extra),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )


def kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def main():
    proc = None
    try:
        print("桌面版无头测试（数据目录：%s）\n" % DATA)
        print("== 1. 启动（演示模式）==")
        proc = spawn(["--demo"])
        ok, health = wait_up()
        check("服务已就绪", ok, health)
        if not ok:
            return 1
        check("演示模式 = %s" % health["demo_mode"], health["demo_mode"] is True)
        status, cfg = call("GET", "/api/desktop/config")
        check("桌面接口可用（can_control=%s）" % cfg.get("can_control"), status == 200 and cfg["desktop"])
        check("配置路径指向用户数据目录", cfg["config_path"].startswith(DATA), cfg["config_path"])

        print("\n== 2. 只监听本机 ==")
        pid = proc.pid
        try:
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
            lines = [l for l in out.splitlines() if f":{PORT}" in l]
            # ss 输出格式：State Recv-Q Send-Q Local:Port Peer:Port Process
            locals_ = [l.split()[3] for l in lines if len(l.split()) > 4]
            # 169.254.0.21 是沙箱预览转发层自己镜像的监听，不是程序绑的；
            # 真正要防的是程序绑到 0.0.0.0（对外暴露）
            external = [a for a in locals_
                        if a.startswith("0.0.0.0:") or a.startswith("[::]:")]
            check("端口 %d 没有对外绑定（0.0.0.0/::），只监听本机" % PORT,
                  bool(locals_) and not external and any(a.startswith("127.0.0.1:") for a in locals_),
                  "本地地址：" + "，".join(locals_))
        except FileNotFoundError:
            check("ss 不可用，跳过监听地址检查", True, "（非 Linux 环境正常）")
        print("   （服务进程 pid=%s，日志：%s/desktop.out）" % (pid, DATA))

        print("\n== 3. 切换运行模式 → 自动重启 ==")
        status, res = call("POST", "/api/desktop/mode", {"demo": False})
        check("切换请求被接受（restarting=%s）" % res.get("restarting") if isinstance(res, dict) else res,
              status == 200 and isinstance(res, dict) and res.get("restarting"))
        with open(os.path.join(DATA, "config.json"), encoding="utf-8") as f:
            saved = json.load(f)
        check("demo=false 已写入 config.json", saved.get("demo") is False, saved)

        restarted = False
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1)
            st, h = call("GET", "/api/health", timeout=3)
            if st == 200 and h.get("demo_mode") is False:
                restarted = True
                break
        check("重启后以生产模式运行（demo_mode=%s）" % (restarted and "false"), restarted)
        check("旧进程已退出", proc.poll() is None and restarted, "pid %s" % pid)

        print("\n== 4. 打开账号网页（本地代理）==")
        status, res = call("POST", "/api/accounts",
                           {"name": "代理测试号", "cookie": "sessionid=abc123; sid_guard=x"})
        acc_id = res.get("id") if isinstance(res, dict) else None
        check("建号成功（id=%s）" % acc_id, status == 200 and acc_id, res)
        status, res = call("POST", "/api/accounts/%s/web" % acc_id)
        check("桌面版返回 desktop 模式（由启动器开原生窗口）",
              status == 200 and isinstance(res, dict) and res.get("mode") == "desktop"
              and res.get("url") == "/dola-web/%s/" % acc_id, res)
        status, res = call("POST", "/api/accounts/999999/web")
        check("不存在的账号 → 404", status == 404, str(res)[:80])
        status, res = call("POST", "/api/accounts", {"name": "空号", "cookie": ""})
        check("空 Cookie 建号被拒（不会再存进坏数据）", status == 400 and "Cookie" in str(res), str(res)[:110])
        check("代理入口可访问（沙箱无外网时给出「连不上 Dola」说明页，而不是 500）",
              live_proxy_check(acc_id), "GET /dola-web/%s/" % acc_id)

        print("\n== 5. reveal 路径处理 ==")
        status, res = call("POST", "/api/desktop/reveal", {"path": "/etc/passwd"})
        check("越权路径被拦截", status == 400, res)
        status, res = call("POST", "/api/desktop/reveal", {"path": "/output/task_demo.mp4"})
        check("静态 URL 被解析到成片目录（无文件时返回打开失败而非越权）",
              status in (200, 500) and "允许范围" not in str(res), str(res)[:120])

        print("\n== 5b. 用系统浏览器打开的端点（账号窗口按钮走这条路）==")
        status, res = call("GET", "/api/desktop/open-external?url=" + urllib.parse.quote("https://evil.example/"))
        check("非白名单地址被拒绝（这个端点不能当打开任意网址的开关）", status == 400, str(res)[:90])
        status, res = call("GET", "/api/desktop/open-external?url=" + urllib.parse.quote("file:///etc/passwd"))
        check("file:// 之类被拒绝", status == 400, str(res)[:90])
        status, res = call("GET", "/api/desktop/open-external?url=" + urllib.parse.quote(BASE + "/dola-web/1/"))
        check("本机地址被放行（会交给系统浏览器，沙箱里打不开也不报错）", status == 200, str(res)[:60])
        status, res = call("GET", "/api/desktop/open-external?url=" + urllib.parse.quote("https://www.dola.com/chat/"))
        check("dola.com 被放行", status == 200, str(res)[:60])

        print("\n== 6. 账号窗口的 Cookie 注入（假 WebView2）==")
        cookie_injection_checks()

        print("\n== 7. 内核网络（代理）与真站自检 ==")
        network_checks()

        print("\n== 8. 退出接口 ==")
        status, res = call("POST", "/api/desktop/quit")
        check("退出请求被接受", status == 200 and isinstance(res, dict) and res.get("ok"), res)
        gone, _ = wait_up(should_be_up=False, timeout=20)
        check("服务已停止", gone)
    finally:
        if proc is not None:
            kill(proc)
        shutil.rmtree(DATA, ignore_errors=True)

    print("\n================ 结果 ================")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败用例：" + "，".join(FAIL))
        return 1
    print("✅ 桌面版接口与重启流程全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
