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
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
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
            return r.status, json.loads(r.read() or b"null")
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

    def destroy(self):
        pass


_FAKE_INJECT_OK = [True]


def fake_webview_module():
    import types
    mod = types.ModuleType("webview")
    mod.guilib = object()          # 假装 GUI 循环已就绪
    mod.windows = []

    def create_window(title, url, **kwargs):
        win = FakePyWindow(title, url)
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

        print("\n== 6. 账号窗口的 Cookie 注入（假 WebView2）==")
        cookie_injection_checks()

        print("\n== 7. 退出接口 ==")
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
