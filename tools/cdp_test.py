#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部浏览器（带扩展的 Edge/Chrome）窗口控制自检 —— 不装浏览器也能跑。

做法：起一个假 CDP 服务（HTTP /json/version + /json/list + WebSocket），
       让它像 Edge 一样回话，然后验证我们真的发出了：
         Browser.getWindowForTarget → Browser.setWindowBounds(minimized)   = 挂起
         Browser.setWindowBounds(normal) + Target.activateTarget           = 切换过去
         Browser.close                                                     = 关闭
以及 Control 有没有把状态回写给后端（账号池「窗口」列靠它）。

用法：.venv/bin/python tools/cdp_test.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DOLA_DATA_DIR", tempfile.mkdtemp())

import desktop  # noqa: E402

OUT = []
COMMANDS = []
STATE = {"window_state": "normal"}


def check(name, ok, detail=""):
    OUT.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# 假 CDP 服务
# ---------------------------------------------------------------------------
def _ws_frame(text: str) -> bytes:
    data = text.encode()
    return bytes([0x81, len(data)]) + data if len(data) < 126 else \
        bytes([0x81, 126]) + struct.pack(">H", len(data)) + data


def _read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_frame(conn) -> str:
    b1, b2 = _read_exact(conn, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _read_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _read_exact(conn, 8))[0]
    mask = _read_exact(conn, 4) if b2 & 0x80 else b""
    payload = _read_exact(conn, n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload.decode("utf-8", "replace")


def serve(port_holder, stop):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.3)
    port_holder.append(srv.getsockname()[1])
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn, port_holder[0]), daemon=True).start()
    srv.close()


def handle(conn, port):
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        lines = head.split(b"\r\n")
        path = lines[0].split(b" ")[1].decode()
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get(b"upgrade", b"").lower() == b"websocket":
            key = headers.get(b"sec-websocket-key", b"")
            accept = base64.b64encode(hashlib.sha1(
                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            while True:
                msg = json.loads(_read_frame(conn))
                method = msg.get("method")
                COMMANDS.append((method, msg.get("params") or {}))
                result = {}
                if method == "Browser.getWindowForTarget":
                    result = {"windowId": 7, "bounds": {"windowState": STATE["window_state"]}}
                elif method == "Browser.setWindowBounds":
                    STATE["window_state"] = (msg.get("params") or {}).get("bounds", {}).get("windowState")
                elif method == "Browser.close":
                    result = {}
                elif method == "Network.getCookies":
                    result = {"cookies": [
                        {"name": "sessionid", "value": "abc", "domain": ".dola.com"},
                        {"name": "sid_guard", "value": "x%3D", "domain": ".dola.com"},
                        {"name": "other", "value": "1", "domain": ".example.com"}]}
                conn.sendall(_ws_frame(json.dumps({"id": msg.get("id"), "result": result})))
        else:
            if path == "/json/version":
                body = {"Browser": "Edg/155.0", "webSocketDebuggerUrl":
                        "ws://127.0.0.1:%d/devtools/browser/fake" % port}
            elif path == "/json/list":
                body = [{"type": "page", "id": "PAGE1", "url": "https://www.dola.com/chat/",
                         "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/page/PAGE1" % port}]
            else:
                body = {}
            raw = json.dumps(body).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def main():
    ports, stop = [], threading.Event()
    threading.Thread(target=serve, args=(ports, stop), daemon=True).start()
    for _ in range(50):
        if ports:
            break
        time.sleep(0.05)
    port = ports[0]

    check("能读到假浏览器的调试信息", desktop.cdp_ready(port), desktop._http_json(port, "/json/version"))
    target = desktop.first_page_target(port)
    check("能拿到第一个页面 target", bool(target) and target.get("id") == "PAGE1", target)

    res = desktop.browser_window_action(port, "park")
    check("挂起 = 最小化窗口", res.get("ok") and STATE["window_state"] == "minimized", res)

    res = desktop.browser_window_action(port, "activate")
    check("切换过去 = 还原窗口 + 激活页面",
          res.get("ok") and STATE["window_state"] == "normal"
          and any(m == "Target.activateTarget" for m, _ in COMMANDS), res)

    res = desktop.browser_window_action(port, "close")
    check("关闭 = Browser.close", res.get("ok") and any(m == "Browser.close" for m, _ in COMMANDS), res)

    check("命令顺序对得上（先拿窗口 id 再设窗口状态）",
          [m for m, _ in COMMANDS][:2] == ["Browser.getWindowForTarget", "Browser.setWindowBounds"],
          [m for m, _ in COMMANDS])

    # —— CDP 直接写 CK：不依赖扩展有没有醒过来 ——
    COMMANDS.clear()
    login = desktop.cdp_login(port, "sessionid=abc123; sid_guard=xyz%3D%3D",
                              url="https://www.dola.com/chat/")
    check("CDP 能把 CK 直接写进浏览器（不用等扩展醒）",
          login.get("ok") is True and login.get("count") == 2 and login.get("url"), login)
    methods = [m for m, _ in COMMANDS]
    check("写 CK 的顺序：先 enable 再 setCookies 再 navigate",
          "Page.enable" in methods
          and methods.index("Network.setCookies") < methods.index("Page.navigate")
          and methods.index("Page.enable") < methods.index("Network.setCookies"),
          methods[:6])
    sent = [p for m, p in COMMANDS if m == "Network.setCookies"]
    chrome_cookies = (sent[0].get("cookies") if sent else []) or []
    check("写进去的 Cookie 是 .dola.com + 值没被弄坏",
          len(chrome_cookies) == 2 and all(c.get("domain") == ".dola.com" for c in chrome_cookies)
          and chrome_cookies[1].get("value") == "xyz%3D%3D", chrome_cookies)
    check("最后跳到 dola 首页",
          [p.get("url") for m, p in COMMANDS if m == "Page.navigate"] == ["https://www.dola.com/chat/"],
          [p.get("url") for m, p in COMMANDS if m == "Page.navigate"])
    COMMANDS.clear()
    desktop.cdp_login(port, "sessionid=abc123", url="https://www.dola.com/chat/", navigate=False)
    check("navigate=False 时只写 Cookie、不动当前页面",
          "Page.navigate" not in [m for m, _ in COMMANDS], [m for m, _ in COMMANDS])
    check("空 CK 直接说明原因，不去连浏览器",
          desktop.cdp_login(port, "  ").get("ok") is False
          and "Cookie" in desktop.cdp_login(port, "  ").get("error", ""),
          desktop.cdp_login(port, "  "))

    # Control：状态要回写给后端，账号池的「窗口」列才显示得出来
    class FakeApp:
        def __init__(self):
            self.calls = []

        def session_update(self, account_id, state, note="", title="", url="", kind=""):
            self.calls.append((account_id, state, kind))

    fake = FakeApp()
    desktop._APP_MODULE = fake
    ctl = desktop.Control()
    ctl._browser_sessions[5] = {"port": port, "browser": "/x/msedge", "browser_version": "155"}
    out = {}

    def hook(action, payload=None):
        payload = payload or {}
        payload["out"] = out
        ctl(action, payload)

    COMMANDS.clear()
    hook("account_browser", {"account_id": 5, "url": "https://www.dola.com/chat/",
                             "cookie": "sessionid=abc123; ttwid=tt-1"})
    check("再点一次「🌐 网页」不会开第二个窗口，而是把已有的叫到前台",
          out.get("reused") is True and out.get("ok") is True, out)
    check("复用已开的窗口时也会重新写一遍 CK（CK 过期不用关窗重登）",
          out.get("login") == "cdp" and out.get("cookie_count") == 2
          and "Network.setCookies" in [m for m, _ in COMMANDS],
          {"login": out.get("login"), "count": out.get("cookie_count"),
           "methods": [m for m, _ in COMMANDS]})

    # —— 指纹：同一账号固定一套、不同账号不一样 ——
    fp = desktop.fingerprint_profile(2)
    check("同一个账号的指纹是固定的（不会每次开窗都变）",
          desktop.fingerprint_profile(2) == fp, fp.get("ua"))
    check("不同账号的指纹不一样", desktop.fingerprint_profile(2)["ua"] != desktop.fingerprint_profile(3)["ua"],
          desktop.fingerprint_profile(3)["ua"][-30:])
    major = fp["metadata"]["fullVersion"].split(".")[0]
    check("UA 和 client hints 的主版本一致（不会自相矛盾）",
          ("Chrome/" + fp["metadata"]["fullVersion"]) in fp["ua"]
          and any(b["version"] == major for b in fp["metadata"]["brands"]), fp["metadata"]["brands"])
    cmd_fp = desktop.build_account_browser_command("/x/msedge", "/tmp/p", ["/e/a"], "http://x/",
                                                   debug_port=9333, fingerprint=fp)
    check("开了指纹：启动参数里带 UA 与语言",
          any(c.startswith("--user-agent=") and fp["ua"] in c for c in cmd_fp)
          and ("--lang=" + fp["lang"]) in cmd_fp, [c for c in cmd_fp if c.startswith("--lang")])
    check("没开指纹：启动参数不带 UA（保持真实身份）",
          not any(c.startswith("--user-agent") for c in
                  desktop.build_account_browser_command("/x/msedge", "/tmp/p", ["/e/a"], "http://x/")), "")

    COMMANDS.clear()
    res = desktop.apply_fingerprint(port, fp)
    methods = [m for m, _ in COMMANDS]
    check("指纹真的套到了页面上（UA / 时区 / 初始化脚本）",
          res.get("ok") and "Emulation.setUserAgentOverride" in methods
          and "Emulation.setTimezoneOverride" in methods
          and "Page.addScriptToEvaluateOnNewDocument" in methods, methods)
    sent_ua = [p for m, p in COMMANDS if m == "Emulation.setUserAgentOverride"]
    check("UA 覆盖带完整 client hints（不让 sec-ch-ua 露馅）",
          bool(sent_ua) and sent_ua[0].get("userAgent") == fp["ua"]
          and sent_ua[0].get("userAgentMetadata", {}).get("fullVersion") == fp["metadata"]["fullVersion"]
          and sent_ua[0].get("acceptLanguage") == fp["accept_language"], sent_ua[:1])
    check("套指纹默认不刷新页面（有 CK 的窗口马上要被导航到 dola，不用多刷一次）",
          "Page.reload" not in methods, methods)
    COMMANDS.clear()
    desktop.apply_fingerprint(port, fp, reload=True)
    check("没 CK 的实例：套完指纹立刻重载一次（client hints 也要变）",
          "Page.reload" in [m for m, _ in COMMANDS], [m for m, _ in COMMANDS])
    scripts = [p.get("source") for m, p in COMMANDS if m == "Page.addScriptToEvaluateOnNewDocument"]
    check("初始化脚本里把 CPU / 内存 / 平台也固定住",
          bool(scripts) and "hardwareConcurrency" in scripts[0] and str(fp["cores"]) in scripts[0], scripts[:1])

    # —— 从窗口读回 CK（手动登录后用）——
    read = desktop.cdp_read_cookies(port)
    check("能从窗口读回 dola 的 CK",
          read.get("ok") and read.get("count") == 2 and "sessionid=abc" in read.get("cookie", ""),
          read)
    check("别的域的 Cookie 不会被混进来", "other=1" not in read.get("cookie", ""), read.get("cookie"))
    out = {}
    hook("account_browser", {"op": "read_cookies", "account_id": 5})
    check("账号页的「从窗口读取 CK」走的就是这个接口",
          out.get("ok") and out.get("count") == 2, out)

    ctl.session({"account_id": 5, "action": "suspend"})
    check("挂起扩展窗口后，后端记成 parked + kind=browser",
          ("5", "parked", "browser") in [(str(c[0]), c[1], c[2]) for c in fake.calls], fake.calls)

    ctl.session({"account_id": 5, "action": "activate"})
    check("切换过去后，后端记成 running", ("5", "running") in [(str(c[0]), c[1]) for c in fake.calls],
          fake.calls)

    ctl.session({"account_id": 5, "action": "close"})
    check("关闭后会话被清掉", 5 not in ctl._browser_sessions, list(ctl._browser_sessions))
    check("关闭后后端收到 closed", any(c[1] == "closed" for c in fake.calls), fake.calls)

    # 浏览器已经被用户手动关掉的情况
    ctl._browser_sessions[6] = {"port": 1}          # 没人监听这个端口
    before = len(fake.calls)
    ctl.session({"account_id": 6, "action": "suspend"})
    check("端口已经没了 → 不崩，并且把会话标成 closed",
          6 not in ctl._browser_sessions and any(c[1] == "closed" for c in fake.calls[before:]),
          fake.calls[before:])

    # 启动参数
    cmd = desktop.build_account_browser_command("/x/msedge", "/tmp/p", ["/e/a"], "http://x/", debug_port=9333)
    check("启动参数带调试端口（挂起/切换靠它）",
          "--remote-debugging-port=9333" in cmd and "--remote-debugging-address=127.0.0.1" in cmd, cmd)

    stop.set()
    bad = [n for n, ok, _ in OUT if not ok]
    for name, ok, detail in OUT:
        print(("✅ " if ok else "❌ ") + name + (("  → " + str(detail)[:120]) if detail and not ok else ""))
    print()
    if bad:
        print("❌ CDP 自检 %d 项失败" % len(bad))
        return 1
    print("✅ 扩展浏览器窗口控制自检全部通过（%d 项）" % len(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
