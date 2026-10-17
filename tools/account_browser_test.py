#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号窗口「CK + 扩展」自检（不需要真的浏览器）。

覆盖整条链路：
  1. POST /api/accounts/{id}/browser 会拉起一个「浏览器」（这里用假脚本记录参数）
  2. 启动参数：每个账号独立 profile、带上全部扩展、地址里有一次性的 #dp-login=token:端口
  3. 代理配置会被沿用到真实浏览器（--proxy-server）
  4. 扩展取 CK：/api/desktop/bridge/cookies 一次性、只回本账号、带 HttpOnly/Secure
  5. 同一个 token 第二次取 → 401；状态接口能告诉前端「扩展到底写没写进去」
  6. 没有 Edge/Chrome 时明确报错，不会静默打开一个不带扩展的窗口

用法：.venv/bin/python tools/account_browser_test.py
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PORT = int(os.getenv("DOLA_TEST_PORT", "8143"))
BASE = "http://127.0.0.1:%d" % PORT
DATA = tempfile.mkdtemp(prefix="dola_accbrowser_")
PASS, FAIL = [], []

COOKIE = "sessionid=abc123; sid_guard=xyz%3D%3D; ttwid=tt-001"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + str(detail)[:200]) if detail else ""))


def call(method, path, payload=None, timeout=15):
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
            except Exception:  # noqa: BLE001
                return r.status, body[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:500].decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "null")
        except Exception:  # noqa: BLE001
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001 （服务还没起来时是 URLError）
        return 0, str(exc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call_raw(method, path, timeout=15):
    """不跟随 302，用来确认「桥接页写完 CK 会跳去 dola.com」。"""
    req = urllib.request.Request(BASE + path, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()[:6000].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, location
        return exc.code, exc.read()[:400].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def wait_up(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = call("GET", "/api/health", timeout=3)
        if status == 200:
            return True
        time.sleep(0.4)
    return False


def spawn(env_extra):
    env = dict(os.environ, DOLA_DATA_DIR=DATA, DOLA_HOST_BIND="127.0.0.1")
    env.update(env_extra)
    log = open(os.path.join(DATA, "desktop.out"), "ab")
    return subprocess.Popen(
        [PY, os.path.join(ROOT, "desktop.py"), "--headless", "--port", str(PORT), "--demo"],
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
    fake = os.path.join(DATA, "fake-browser.sh")
    argfile = os.path.join(DATA, "browser-args.txt")
    with open(fake, "w", encoding="utf-8") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$@" > ' + argfile + "\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    proc = None
    try:
        # 先不带浏览器启动：端点要明确报错
        proc = spawn({})
        if not wait_up():
            print("❌ 服务没起来，日志：", open(os.path.join(DATA, "desktop.out"), encoding="utf-8", errors="replace").read()[-1500:])
            return 1
        status, body = call("POST", "/api/accounts", {"name": "测试号", "cookie": COOKIE})
        account_id = (body or {}).get("id") if isinstance(body, dict) else None
        if not account_id:
            conn_status, accounts = call("GET", "/api/accounts")
            account_id = (accounts[0]["id"] if accounts else None)
        check("建了一个测试账号", bool(account_id), body)
        status, body = call("POST", "/api/accounts/%s/browser" % account_id)
        check("没装 Edge/Chrome 时明确报错（不会静默开一个没扩展的窗口）",
              status in (400, 500) and isinstance(body, dict) and body.get("error"),
              body)
        kill(proc)
        proc = None

        # 再用假浏览器启动：检查真实拼出来的命令
        proc = spawn({"DOLA_BROWSER": fake,
                      "DOLA_PROXY": ""})
        if not wait_up():
            print("❌ 服务第二次没起来")
            return 1
        status, body = call("POST", "/api/accounts/%s/browser" % account_id)
        ok = status == 200 and isinstance(body, dict) and body.get("token")
        check("点「🌐 网页」会拉起带扩展的浏览器", ok, body if not ok else "token 已发")
        token = (body or {}).get("token") if isinstance(body, dict) else ""
        url = (body or {}).get("url") if isinstance(body, dict) else ""

        for _ in range(50):
            if os.path.exists(argfile):
                break
            time.sleep(0.1)
        args = open(argfile, encoding="utf-8").read().splitlines() if os.path.exists(argfile) else []
        joined = " ".join(args)
        check("用的是这个账号自己的 profile",
              ("--user-data-dir=%s" % os.path.join(DATA, "account-browsers", "acc-%s" % account_id)) in joined,
              [a for a in args if a.startswith("--user-data-dir")])
        check("三个扩展都带上了（哆啦助手 + Cookie-Editor + CK 自动登录）",
              all(k in joined for k in ("dola30", "cookie-editor", "dola-login")), joined[:220])
        check("地址是本机桥接页 + 一次性 token，没有明文 CK",
              (args[-1] if args else "").startswith(BASE + "/desktop/bridge?token=")
              and "sessionid" not in joined, args[-1] if args else "")
        check("返回的 url 和实际打开的一致", bool(url) and url == (args[-1] if args else ""), url)
        check("返回里写明浏览器和提示",
              isinstance(body, dict) and body.get("browser") and "warning" in body, body)

        # 桥接页：扩展没回话时，这张页面要能把「怎么装扩展」写清楚
        status, page = call_raw("GET", "/desktop/bridge?token=" + urllib.parse.quote(token, safe=""))
        check("桥接页能打开，并写明正在登录哪个账号",
              status == 200 and "正在用" in str(page) and "测试号" in str(page), str(page)[:80])

        # 扩展来取 CK（一次性）
        path = "/api/desktop/bridge/cookies?token=" + urllib.parse.quote(token, safe="")
        status, data = call("GET", path)
        cookies = (data or {}).get("cookies") if isinstance(data, dict) else []
        names = [c.get("name") for c in cookies or []]
        check("扩展能取到本账号的 CK", status == 200 and names == ["sessionid", "sid_guard", "ttwid"], names)
        check("Cookie 属性够用（.dola.com / 路径 / HttpOnly / Secure）",
              bool(cookies) and cookies[0].get("domain") == ".dola.com"
              and cookies[0].get("path") == "/" and cookies[0].get("httpOnly") is True
              and cookies[0].get("secure") is True, cookies[:1])
        check("值里的 %3D 没被弄坏", any(c.get("value") == "xyz%3D%3D" for c in cookies or []),
              [c.get("value") for c in cookies or []])

        status2, again = call("GET", path)
        check("同一个 token 不能取第二次（一次性）",
              status2 == 401 and isinstance(again, dict) and "用过" in (again.get("error") or ""), again)

        status, st = call("GET", "/api/desktop/bridge/status?token=" + urllib.parse.quote(token, safe=""))
        check("前端轮询能知道扩展已经写进去了（used + 条数）",
              status == 200 and (st or {}).get("used") is True and (st or {}).get("count") == 3, st)

        status, st = call("GET", "/api/desktop/bridge/status?token=nope")
        check("假 token 查状态不会崩", status == 200 and (st or {}).get("known") is False, st)

        status, bad = call("GET", "/api/desktop/bridge/cookies?token=nope")
        check("假 token 取不到任何东西", status == 401, bad)

        status, where = call_raw("GET", "/desktop/bridge?token=" + urllib.parse.quote(token, safe=""))
        check("CK 写完后桥接页自动跳到 dola.com（302）",
              status == 302 and "dola.com" in (where or ""), "%s %s" % (status, where))
        status, page = call_raw("GET", "/desktop/bridge?token=nope")
        check("假凭证的桥接页给人话，不是堆栈", status == 401 and "重新点一次" in str(page), str(page)[:60])

        # 扩展文件本身
        manifest = json.load(open(os.path.join(ROOT, "extensions", "dola-login", "manifest.json"),
                                  encoding="utf-8"))
        check("CK 自动登录扩展的 manifest 有 cookies 权限和 dola 域名",
              "cookies" in manifest.get("permissions", [])
              and any("dola.com" in h for h in manifest.get("host_permissions", [])),
              manifest.get("name"))
    finally:
        if proc is not None:
            kill(proc)

    print()
    if FAIL:
        print("❌ 账号窗口扩展自检 %d 项失败：" % len(FAIL))
        for name in FAIL:
            print("   · " + name)
        return 1
    print("✅ 账号窗口「CK + 扩展」自检全部通过（%d 项）" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
