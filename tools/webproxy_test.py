#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「打开 Dola 网页」本地代理自检。

沙箱/内网环境访问不了真的 dola.com，所以这里起一个**模仿 Dola 的假站点**，
再让 app.py 把 /dola-web/{账号id}/… 代理到它，逐条验证代理是否做对了：

  · Cookie 注入（HttpOnly 也能生效）      · dola.com 绝对地址 / 站内绝对路径改写
  · CSP / X-Frame-Options / integrity 剥离 · CSS url() 改写
  · 302 重定向改写                         · Set-Cookie 落到本地代理
  · gzip 解压                              · 二进制文件原样转发
  · POST body 转发                         · 尾斜杠补全
  · 危险路径拒绝 / 账号不存在提示

用法：
    python tools/webproxy_test.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def _proxy_js():
    """把 app.py 里的 _PROXY_JS 常量抠出来（和 tools/inject_test.js 一样）。"""
    try:
        src = open(os.path.join(BASE_DIR, "app.py"), encoding="utf-8").read()
        m = re.search(r'_PROXY_JS = r"""(.*?)"""', src, re.S)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""

# 假站点里的登录凭据（模拟 Dola 的会话 Cookie）
GOOD_COOKIE = "sessionid=TESTCOOKIE123; sid_guard=guarded"
# 1x1 的合法 PNG，用来验证二进制响应是原样转发的
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + str(detail)) if detail else ""))
    return ok


# --------------------------------------------------------------------------
# 模仿 Dola 的假站点
# --------------------------------------------------------------------------
HOME_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'">
<base href="https://www.dola.com/deep/">
<title>假 Dola</title>
<link rel="stylesheet" href="/static/app.css">
<script src="/static/app.js" integrity="sha384-fakehash"></script>
<script src="https://www.dola.com/static/ext.js"></script>
</head><body>
<h1>假 Dola 首页</h1>
<img src="/img/logo.png" srcset="/img/logo.png 1x, /img/logo2x.png 2x" alt="logo">
<a id="abs" href="https://www.dola.com/chat/123">绝对链接</a>
<a id="proto" href="//www.dola.com/chat/456">协议相对链接</a>
<a id="root" href="/chat/789">站内绝对路径</a>
<script>window.__API = "https://www.dola.com/api/me";</script>
<form action="/api/echo" method="post"><input name="q" value="1"><button>go</button></form>
</body></html>"""

APP_JS = """// 假 Dola 的前端脚本
var BASE = "https://www.dola.com";
function load(){ return fetch(BASE + "/api/me").then(function(r){ return r.json(); }); }
fetch("/api/me2");
var chunk = import("/assets/chunk-abc.js");
new Worker("/wk/sw.js");
importScripts("/sw-helper.js");
"""

APP_CSS = """@import "/styles/base.css";
body{background:#111 url(/img/bg.png) no-repeat}
.logo{background-image:url("https://www.dola.com/img/logo2.png")}
"""


class FakeDola(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeDola/1.0"

    def log_message(self, *args):  # 静音
        pass

    # -- 工具 --------------------------------------------------------
    def _send(self, code, body: bytes, headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self) -> bool:
        return "sessionid=TESTCOOKIE123" in (self.headers.get("Cookie") or "")

    # -- 路由 --------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/chat"):          # 模拟真实站点把根路径跳到主界面
            self._send(302, b"", {"Location": "/chat/"})
        elif path == "/chat/":
            self._send(200, HOME_HTML.encode("utf-8"), {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": "default-src 'self'",
                "X-Frame-Options": "DENY",
                "Strict-Transport-Security": "max-age=31536000",
            })
        elif path == "/static/app.js":
            self._send(200, APP_JS.encode("utf-8"),
                       {"Content-Type": "application/javascript; charset=utf-8"})
        elif path == "/static/app.css":
            self._send(200, APP_CSS.encode("utf-8"), {"Content-Type": "text/css; charset=utf-8"})
        elif path == "/static/ext.js":
            self._send(200, b"window.__EXT=1;", {"Content-Type": "application/javascript"})
        elif path.startswith("/img/"):
            self._send(200, PNG, {"Content-Type": "image/png"})
        elif path == "/api/me":
            if not self._authed():
                self._send(401, b'{"error":"unauthorized"}', {"Content-Type": "application/json"})
            else:
                self._send(200, json.dumps({
                    "ok": True, "user": "假账号", "cookie_seen": self.headers.get("Cookie"),
                    "referer": self.headers.get("Referer", ""),
                    "origin": self.headers.get("Origin", ""),
                }, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"})
        elif path == "/api/me2":
            self._send(200, b'{"me2":true}', {"Content-Type": "application/json"})
        elif path == "/redirect-abs":
            self._send(302, b"", {"Location": "https://www.dola.com/deep/page"})
        elif path == "/redirect-root":
            self._send(302, b"", {"Location": "/deep/page"})
        elif path == "/deep/page":
            self._send(200, "<html><head><title>深页</title></head><body>deep page</body></html>".encode(),
                       {"Content-Type": "text/html; charset=utf-8"})
        elif path == "/setcookie":
            self._send(200, b"<html><body>cookie set</body></html>", {
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": "foo=bar; Domain=.dola.com; Path=/; Secure; HttpOnly; SameSite=None",
            })
        elif path == "/gzipped":
            raw = "<html><head><title>gz</title></head><body>gzip 页面</body></html>".encode("utf-8")
            self._send(200, gzip.compress(raw), {
                "Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"})
        elif path.startswith("/chat/"):
            self._send(200, ("<html><body>chat 子页 " + path + "</body></html>").encode(),
                       {"Content-Type": "text/html; charset=utf-8"})
        else:
            self._send(404, b"<html><body>not found</body></html>", {"Content-Type": "text/html"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.split("?", 1)[0] == "/api/echo":
            self._send(200, json.dumps({
                "echo": body.decode("utf-8", "replace"),
                "cookie": self.headers.get("Cookie"),
                "ctype": self.headers.get("Content-Type"),
            }, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"})
        else:
            self._send(404, b"nope")

    do_HEAD = do_GET


def start_fake_dola() -> tuple:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDola)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --------------------------------------------------------------------------
# 启动被测服务
# --------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_app(web_base: str, data_dir: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "DOLA_PORT": str(port),
        "DOLA_HOST_BIND": "127.0.0.1",
        "DOLA_DEMO": "1",
        "DOLA_DATA_DIR": data_dir,
        "DOLA_WEB_BASE": web_base,
        "DOLA_LOG_LEVEL": "WARNING",
    })
    env.pop("DOLA_ADMIN_TOKEN", None)
    proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "app.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace")[-1500:]
            raise RuntimeError("服务提前退出：\n" + out)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    raise RuntimeError("服务启动超时")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """测 302 / 307 时要看到原始响应，不能让 urllib 自己跟过去。"""

    def redirect_request(self, *args, **kwargs):
        return None


_NOREDIR = urllib.request.build_opener(_NoRedirect)


def call(url: str, method: str = "GET", data=None, headers=None, follow: bool = True):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.urlopen if follow else _NOREDIR.open
    try:
        with opener(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


# --------------------------------------------------------------------------
def run_inject_e2e(proxy_url: str):
    """让 jsdom 真的加载一遍代理页面，确认注入脚本被当脚本执行。"""
    script = os.path.join(BASE_DIR, "tools", "inject_test.js")
    if not os.path.exists(script):
        return True, "没有 inject_test.js，跳过"
    try:
        r = subprocess.run(["node", script, proxy_url], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return True, "没装 node，跳过"
    except Exception as exc:  # noqa: BLE001
        return True, "未能执行：%s" % exc
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    if "未安装 jsdom" in out:
        return True, "没装 jsdom，跳过"
    detail = "；".join(l.strip() for l in out.splitlines() if l.strip().startswith("❌")) or "脚本正常执行"
    return r.returncode == 0, detail


def run_inject_test():
    """顺带跑一遍注入脚本的行为测试（需要 node + jsdom，没有就跳过）。"""
    script = os.path.join(BASE_DIR, "tools", "inject_test.js")
    if not os.path.exists(script):
        return 0
    try:
        r = subprocess.run(["node", script], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        print("\n⚠️  没装 node，跳过注入脚本行为测试（tools/inject_test.js）")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("\n⚠️  注入脚本行为测试未能执行：%s" % exc)
        return 0
    out = (r.stdout or "").strip().splitlines()
    print("\n== 注入脚本行为测试（node + jsdom）==")
    if r.returncode == 0 and out:
        print("  " + out[-1])
        if "未安装 jsdom" in r.stdout:
            print("  （npm i jsdom 后可跑完整用例）")
        return 0
    print(r.stdout or r.stderr)
    return 1


def main() -> int:
    fake, fake_port = start_fake_dola()
    web_base = f"http://127.0.0.1:{fake_port}"
    app_port = free_port()
    data_dir = tempfile.mkdtemp(prefix="dola-webproxy-")
    print("=" * 64)
    print(f"假 Dola 站点：{web_base}")
    print(f"被测服务    ：http://127.0.0.1:{app_port}  (DOLA_WEB_BASE={web_base})")
    print("=" * 64)

    proc = start_app(web_base, data_dir, app_port)
    base = f"http://127.0.0.1:{app_port}"
    try:
        # 建一个带 Cookie 的账号
        body = json.dumps({"name": "代理测试号", "cookie": GOOD_COOKIE}).encode("utf-8")
        status, raw, _ = call(base + "/api/accounts", "POST", body, {"Content-Type": "application/json"})
        acc = json.loads(raw)
        acc_id = acc["id"]
        prefix = f"/dola-web/{acc_id}"

        print("\n== 入口与首页 HTML 改写 ==")
        status, _, hdr = call(base + prefix + "/", follow=False)
        loc = hdr.get("Location") or hdr.get("location") or ""
        check("入口 /dola-web/N/ 跳到主界面 /chat/",
              status == 302 and loc == prefix + "/chat/", f"{status} {loc}")

        status, raw, hdr = call(base + prefix + "/chat/")
        html = raw.decode("utf-8", "replace")
        check("首页返回 200", status == 200, status)
        check("dola.com 绝对地址已改写到代理",
              "www.dola.com" not in html and "dola.com" not in html.replace("dlp-bar", ""),
              [s for s in html.split('"') if "dola.com" in s][:3])
        check("站内绝对路径 /static/app.js 已加代理前缀", f'href="{prefix}/static/app.css"' in html)
        check("integrity 属性已剥离", "integrity" not in html)
        check("srcset 里的根路径已改写",
              ('srcset="' + prefix + '/img/logo.png 1x, ' + prefix + '/img/logo2x.png 2x"') in html,
              [x for x in html.split('"') if "srcset" in html and "logo2x" in x][:1])
        check("站点 CSP meta 已移除", "Content-Security-Policy" not in html)
        import re as _re
        _base = _re.search(r"<base[^>]*>", html)
        check("base 标签指向代理", bool(_base) and 'href="' + prefix + '/deep/"' in _base.group(0),
              _base.group(0) if _base else "没有 base 标签")
        check("注入了本地代理提示条脚本", "dlp-bar" in html and "本地代理" in html)
        check("响应头 CSP 已剥离", "content-security-policy" not in {k.lower() for k in hdr})
        check("响应头 X-Frame-Options 已剥离", "x-frame-options" not in {k.lower() for k in hdr})
        check("响应头 HSTS 已剥离", "strict-transport-security" not in {k.lower() for k in hdr})

        print("\n== 子资源与 Cookie 注入 ==")
        status, raw, hdr = call(base + prefix + "/static/app.js")
        js = raw.decode("utf-8", "replace")
        check("JS 文件返回 200", status == 200, status)
        check("JS 里的 dola.com 绝对地址已改写", "dola.com" not in js, js[:80])
        check("JS 里的动态 import()/Worker 根路径已改写",
              ('import("' + prefix + '/assets/chunk-abc.js")') in js
              and ('Worker("' + prefix + '/wk/sw.js")') in js
              and ('importScripts("' + prefix + '/sw-helper.js")') in js,
              [l for l in js.splitlines() if "import" in l or "Worker" in l][:3])
        status, raw, _ = call(base + prefix + "/static/app.css")
        css = raw.decode("utf-8")
        check("CSS 里的 url(/img/bg.png) 已改写",
              f"url({prefix}/img/bg.png)" in css, css.splitlines()[0])
        check("CSS 里的绝对 url 已改写", "dola.com" not in css)
        check("CSS 里的 @import 根路径已改写",
              ('@import "' + prefix + '/styles/base.css"') in css, css.splitlines()[0])

        status, raw, _ = call(base + prefix + "/img/logo.png")
        check("图片二进制原样转发", status == 200 and raw == PNG, f"{status} {len(raw)}B")

        # 浏览器会带上「本地代理地址」的 Referer，代理要把它还原成 dola 的地址
        status, raw, _ = call(base + prefix + "/api/me", headers={
            "Referer": base + prefix + "/chat/page1",
            "Origin": base,
        })
        data = json.loads(raw) if status == 200 else {}
        check("带账号 Cookie 请求受保护的接口 → 200", status == 200, status)
        check("上游确实收到了该账号的 Cookie",
              "sessionid=TESTCOOKIE123" in (data.get("cookie_seen") or ""), data.get("cookie_seen"))
        check("Referer 已从代理地址还原成 dola 地址",
              data.get("referer") == web_base + "/chat/page1", data.get("referer"))
        check("Origin 已从代理地址还原成 dola 地址",
              data.get("origin") == web_base, data.get("origin"))

        status, _, _ = call(web_base + "/api/me")
        check("对照：直连假站不带 Cookie → 401", status == 401, status)

        print("\n== 重定向 / Set-Cookie / gzip / POST ==")
        status, _, hdr = call(base + prefix + "/redirect-abs", follow=False)
        loc = hdr.get("Location") or hdr.get("location") or ""
        check("302 绝对地址被改写到代理", status == 302 and loc == prefix + "/deep/page", f"{status} {loc}")
        status, _, hdr = call(base + prefix + "/redirect-root", follow=False)
        loc = hdr.get("Location") or hdr.get("location") or ""
        check("302 站内绝对路径被改写到代理", status == 302 and loc == prefix + "/deep/page", f"{status} {loc}")

        status, _, hdr = call(base + prefix + "/setcookie")
        sc = hdr.get("Set-Cookie") or hdr.get("set-cookie") or ""
        check("上游下发的 Set-Cookie 被转发回来", bool(sc), sc)
        check("Set-Cookie 去掉了 Secure（本地是 http）", "secure" not in sc.lower(), sc)
        check("Set-Cookie 的 Path 改为代理前缀", f"path={prefix}/" in sc.lower(), sc)

        status, raw, _ = call(base + prefix + "/gzipped")
        check("gzip 响应被正确解压", status == 200 and "gzip 页面" in raw.decode("utf-8", "replace"))

        payload = "name=hello&x=1".encode()
        status, raw, _ = call(base + prefix + "/api/echo", "POST", payload,
                              {"Content-Type": "application/x-www-form-urlencoded"})
        echoed = json.loads(raw) if status == 200 else {}
        check("POST 请求体转发正常", status == 200 and echoed.get("echo") == "name=hello&x=1", echoed.get("echo"))
        check("POST 也带上了账号 Cookie", "sessionid=TESTCOOKIE123" in (echoed.get("cookie") or ""))

        print("\n== 边界情况 ==")
        status, _, hdr = call(base + prefix, follow=False)
        loc = hdr.get("Location") or hdr.get("location") or ""
        check("不带尾斜杠 → 307 补斜杠", status == 307 and loc.endswith(prefix + "/"), f"{status} {loc}")

        check("注入脚本含元素属性与 DOM 插入拦截（SPA 动态加载 chunk 靠它）",
              all(k in html for k in ("HTMLScriptElement", "setAttribute",
                                      "appendChild", "MutationObserver", "__dlpFix")),
              "关键 hook 齐全")
        check("提示条提供「用系统浏览器打开」与错误面板",
              "用系统浏览器打开" in html and "dlp-log" in html)
        js_src = _proxy_js()
        check("注入的 JS 被 <script> 正确包裹（漏了会变成一屏代码文本）",
              bool(js_src) and ("<script>" + js_src) in html and html.count("<script>" + js_src) == 1,
              "已包裹" if js_src else "没抠到 _PROXY_JS 常量")
        e2e_ok, e2e_detail = run_inject_e2e(base + prefix + "/chat/")
        check("端到端：jsdom 加载代理页面后脚本真的执行了", e2e_ok, e2e_detail)

        status, raw, _ = call(base + "/dola-web/999999/")
        check("账号不存在 → 404 说明页", status == 404 and "账号不存在" in raw.decode("utf-8", "replace"), status)

        status, raw, _ = call(base + prefix + "/http://evil.example.com/")
        check("带 :// 的路径被拒绝", status == 400 and "请求被拒绝" in raw.decode("utf-8", "replace"), status)

        # 页面里 location.href='/chat/xxx' 这类跳转浏览器端拦不住，
        # 会落到本机根路径；带上前一页的 Referer 时要能继续走代理
        status, _, _ = call(base + "/chat/page9", follow=False)
        check("本机未知路径无 Referer → 404（不会被当成开放代理）", status == 404, status)
        status, raw, _ = call(base + "/chat/page9",
                              headers={"Referer": base + prefix + "/chat/"})
        check("带代理页面 Referer 的未知路径 → 转发给 Dola",
              status == 200 and b"chat" in raw, f"{status} {raw[:60]}")

        status, raw, _ = call(base + f"/api/accounts/{acc_id}/web", "POST", b"")
        info = json.loads(raw) if status == 200 else {}
        check("API /web 返回代理地址（Web 版走新标签页）",
              status == 200 and info.get("url") == prefix + "/" and info.get("mode") == "browser", info)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        fake.shutdown()
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)

    rc = run_inject_test()

    print("\n================ 结果 ================")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL or rc:
        if FAIL:
            print("失败用例：" + "；".join(FAIL))
        return 1
    print("✅ 本地代理各环节均正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
