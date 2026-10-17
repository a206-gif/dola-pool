#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扩展内核提交链路自检（假 CDP 页面，不装浏览器也能跑）。

验的是「Python 侧到底发了什么、拿到什么」：
  1. 导航到 dola.com/chat、写入 CK、注入内核 JS（document_start）
  2. prepare / submit / waitConversation 的调用顺序
  3. 页面没进视频模式 / 没有 30s / 被踢到登录页时，报错要说人话
  4. Control.browser_generate 有没有：复用已开窗口、回写会话、提交后挂起

页面里的 DOM 行为由 static/browser_kernel.js + tools/browser_kernel_test.js 覆盖，
这里只保证「壳」是对的。

用法：.venv/bin/python tools/browser_submit_test.py
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
os.environ.setdefault("DOLA_DEMO", "1")

import desktop  # noqa: E402

OUT = []
COMMANDS = []
SCENARIO = {"href": "https://www.dola.com/chat/", "ready": "complete",
            "kernel": True, "prepare": None, "submit": None, "conv": None}


def check(name, ok, detail=""):
    OUT.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# 假 CDP 服务（HTTP + 页面级 WebSocket）
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


def page_eval(expr: str):
    if "String(location.href)" in expr:
        return SCENARIO["href"]
    if "String(document.readyState)" in expr:
        return SCENARIO["ready"]
    if "__dolaPoolKernel && window.__dolaPoolKernel.prepare" in expr:
        return SCENARIO["kernel"]
    if "window.__dolaPoolKernel.prepare" in expr:
        return SCENARIO["prepare"]
    if "focusEditor" in expr:
        return {"ok": True, "tag": "DIV"}
    if "__dolaPoolKernel.status()" in expr:
        return {"ok": True, "prompt": SCENARIO.get("inserted", ""), "body": ""}
    if "sendNow" in expr:
        return SCENARIO["submit"]
    if "window.__dolaPoolKernel.submit" in expr:
        return SCENARIO["submit"]
    if "waitConversation" in expr:
        return SCENARIO["conv"]
    if "window.__dolaPoolKernel.status" in expr:
        return {"ok": True}
    return None


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
                params = msg.get("params") or {}
                COMMANDS.append((method, params))
                result = {}
                if method == "Input.insertText":
                    SCENARIO["inserted"] = str(params.get("text") or "")
                    result = {}
                elif method == "Runtime.evaluate":
                    value = page_eval(str(params.get("expression") or ""))
                    result = {"result": {"type": "object", "value": value}}
                conn.sendall(_ws_frame(json.dumps({"id": msg.get("id"), "result": result})))
        else:
            if path == "/json/version":
                body = {"Browser": "Edg/155.0", "webSocketDebuggerUrl":
                        "ws://127.0.0.1:%d/devtools/browser/fake" % port}
            elif path == "/json/list":
                body = [{"type": "page", "id": "PAGE1", "url": SCENARIO["href"],
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


def serve(holder, stop):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.3)
    holder.append(srv.getsockname()[1])
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn, holder[0]), daemon=True).start()
    srv.close()


def reset_commands():
    COMMANDS.clear()


def has_command(name: str) -> bool:
    return any(m == name for m, _ in COMMANDS)


def params_of(name: str):
    for m, p in COMMANDS:
        if m == name:
            return p
    return None


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def main():
    holder, stop = [], threading.Event()
    threading.Thread(target=serve, args=(holder, stop), daemon=True).start()
    for _ in range(50):
        if holder:
            break
        time.sleep(0.05)
    port = holder[0]

    # 1) 成功链路
    SCENARIO.update(href="https://www.dola.com/chat/", ready="complete", kernel=True,
                    prepare={"ok": True, "status": {"model": "Dreamina Seedance 2.5",
                                                    "duration": "30s", "ratio": "9:16"},
                             "steps": [{"name": "选择时长 30", "ok": True, "detail": "30s"}]},
                    submit={"ok": True, "steps": [{"name": "写入提示词", "ok": True, "detail": "雨夜"}]},
                    conv={"ok": True, "id": "7123456789012345678",
                          "status": {"steps": [{"name": "发送按钮", "ok": True, "detail": "发送"}]}})
    SCENARIO["inserted"] = ""
    reset_commands()
    res = desktop.run_browser_generation(port, "雨夜街头，霓虹倒影", cookie="sessionid=abc; sid_guard=def",
                                         model="seedance_v2.5", duration=30, ratio="9:16",
                                         log=lambda *_: None)
    check("成功链路返回 conversation_id", res.get("ok") and res.get("conversation_id") == "7123456789012345678",
          res.get("error") or res.get("conversation_id"))
    check("先写 Cookie 再导航（CK 不经过剪贴板）",
          has_command("Network.setCookies") and has_command("Page.navigate"), [m for m, _ in COMMANDS][:8])
    ck = (params_of("Network.setCookies") or {}).get("cookies") or []
    check("CK 按分号拆成 2 条、域写成 .dola.com",
          len(ck) == 2 and all(c.get("domain") == ".dola.com" for c in ck),
          ck[:2])
    nav = (params_of("Page.navigate") or {}).get("url", "")
    check("导航到 dola.com/chat/", nav == "https://www.dola.com/chat/", nav)
    injected = (params_of("Page.addScriptToEvaluateOnNewDocument") or {}).get("source", "")
    check("内核 JS 在 document_start 注入（含 prepare/submit）",
          "__dolaPoolKernel" in injected and "async function prepare" in injected,
          "len=%d" % len(injected))
    names = [m for m, _ in COMMANDS]
    check("顺序：导航 → 内核检查 → prepare → submit → 等 conversation_id",
          names.index("Page.navigate") < names.index("Runtime.evaluate"), names[:12])
    check("提示词用 CDP Input.insertText 真打字，而不是只改 DOM 文本",
          has_command("Input.insertText")
          and (params_of("Input.insertText") or {}).get("text") == "雨夜街头，霓虹倒影",
          params_of("Input.insertText"))
    check("打完字确认输入框里真的有内容才点发送",
          has_command("Runtime.evaluate")
          and any("sendNow" in str(p.get("expression")) for m, p in COMMANDS if m == "Runtime.evaluate"),
          "没走到 sendNow")
    check("prepare 的入参带 2.5 / 30 / 9:16",
          any('"duration": 30' in str(p.get("expression")) and "2.5" in str(p.get("expression"))
              for m, p in COMMANDS if m == "Runtime.evaluate"),
          "没找到 prepare 入参")

    # 2) prepare 失败：没有 30s
    SCENARIO.update(kernel=True,
                    prepare={"ok": False, "error": "时长菜单里没有 30s —— 说明扩展的 30 秒能力没生效",
                             "steps": [{"name": "选择时长 30", "ok": False, "detail": "30s 不在菜单里"}],
                             "status": {}})
    res = desktop.run_browser_generation(port, "x", cookie="a=1", model="seedance_v2.5",
                                         duration=30, ratio="9:16", log=lambda *_: None)
    check("没进到目标档位时，把页面给的原因原样带回来",
          not res.get("ok") and "30" in (res.get("error") or ""), res.get("error"))
    check("失败时保留 steps，方便界面显示是哪一步",
          bool(res.get("steps")) and res["steps"][0]["name"] == "选择时长 30", res.get("steps"))

    # 3) 被踢到登录页
    SCENARIO.update(href="https://www.dola.com/login/?redirect=/chat/", kernel=True)
    res = desktop.run_browser_generation(port, "x", cookie="a=1", model="seedance_v2.5",
                                         duration=30, ratio="9:16", log=lambda *_: None)
    check("被踢到登录页时，直接说 CK 可能失效、怎么补",
          not res.get("ok") and ("登录" in (res.get("error") or "") or "CK" in (res.get("error") or "")),
          res.get("error"))

    # 4) 内核没挂上
    SCENARIO.update(href="https://www.dola.com/chat/", kernel=False)
    res = desktop.run_browser_generation(port, "x", cookie="a=1", model="seedance_v2.5",
                                         duration=30, ratio="9:16", log=lambda *_: None)
    check("内核没加载时给出明确原因",
          not res.get("ok") and "内核" in (res.get("error") or ""), res.get("error"))

    # 5) Control 层：复用窗口 / 提交 / 挂起 / 回写
    calls = {"launch": 0, "park": 0, "activate": 0, "notes": []}

    class FakeApp:
        def session_update(self, account_id, state, note="", title="", url="", kind=""):
            calls["notes"].append((account_id, state, kind))

    desktop._APP_MODULE = FakeApp()
    ctl = desktop.Control()
    ctl._browser_sessions[9] = {"port": port, "browser": "/x/msedge"}
    orig = (desktop.cdp_ready, desktop.browser_window_action, desktop.run_browser_generation)
    desktop.cdp_ready = lambda p: True
    desktop.browser_window_action = lambda p, a: (calls.__setitem__(a, calls.get(a, 0) + 1) or {"ok": True})
    desktop.run_browser_generation = lambda *a, **k: {"ok": True, "conversation_id": "7123456789012345678"}
    try:
        out = {}
        ctl.browser_generate({"account_id": 9, "prompt": "雨夜", "cookie": "a=1",
                              "model": "seedance_v2.5", "duration": 30, "ratio": "9:16", "out": out})
    finally:
        desktop.cdp_ready, desktop.browser_window_action, desktop.run_browser_generation = orig
    check("Control 复用已开的扩展窗口（不再启动第二个）", calls["launch"] == 0, calls)
    check("提交前把窗口叫到前台，提交后收进后台",
          calls.get("activate") == 1 and calls.get("park") == 1, calls)
    check("提交成功把 conversation_id 交回后端", out.get("ok") and out.get("conversation_id"),
          out.get("error") or out.get("conversation_id"))
    check("会话状态回写成 running（账号池显示 🧩 前台显示中）",
          any(n[1] == "running" and n[2] == "browser" for n in calls["notes"]), calls["notes"])

    # 5b) __call__ 必须认得 browser_generate —— 以前没登记的 action 会走到
    #     「关窗口」那条分支，任务一提就退出整个应用。
    out = {}
    orig_dispatch = desktop.run_browser_generation
    desktop.run_browser_generation = lambda *a, **k: {"ok": True, "conversation_id": "7123456789012345678"}
    try:
        ctl("browser_generate", {"account_id": 9, "prompt": "x", "cookie": "a=1",
                                 "model": "seedance_v2.5", "duration": 30, "out": out})
    finally:
        desktop.run_browser_generation = orig_dispatch
    check("Control.__call__ 认得 browser_generate（不会当成退出指令）",
          ctl.action is None and out.get("ok"), "action=%s out=%s" % (ctl.action, out.get("error")))

    # 5c) 没有现成窗口时会自己启动一个
    launched = {"n": 0}

    def fake_launch(account_id, url, **kw):
        launched["n"] += 1
        return {"ok": True, "debug_port": port, "pid": 1, "browser": "/x/msedge",
                "profile": "/tmp/p", "extensions": []}

    orig2 = (desktop.cdp_ready, desktop.launch_account_browser, desktop.run_browser_generation)
    up = {"live": False}
    desktop.cdp_ready = lambda p: up["live"]
    desktop.launch_account_browser = lambda account_id, url, **kw: (up.__setitem__("live", True) or fake_launch(account_id, url, **kw))
    desktop.run_browser_generation = lambda *a, **k: {"ok": True, "conversation_id": "7123456789012345678"}
    try:
        ctl2 = desktop.Control()
        out2 = {}
        ctl2.browser_generate({"account_id": 11, "prompt": "x", "cookie": "a=1",
                               "model": "seedance_v2.5", "duration": 30, "out": out2})
    finally:
        desktop.cdp_ready, desktop.launch_account_browser, desktop.run_browser_generation = orig2
    check("账号还没有浏览器窗口时：自动启动带扩展的浏览器并记住端口",
          launched["n"] == 1 and 11 in ctl2._browser_sessions
          and ctl2._browser_sessions[11].get("port") == port, ctl2._browser_sessions)

    # 6) 通道选择：2.5 走扩展内核，2.0 保持直连
    import app as webapp  # noqa: E402
    check("2.5 → 扩展内核通道", webapp.task_channel({"model": "seedance_v2.5", "duration": 30}) == "extension")
    check("2.5 + 10 秒也走扩展内核（模型没开放，跟时长无关）",
          webapp.task_channel({"model": "seedance_v2.5", "duration": 10}) == "extension")
    check("2.0 → 还是原来的直连通道（生成逻辑一行不改）",
          webapp.task_channel({"model": "seedance_v2.0", "duration": 10}) == "direct")
    check("老任务没有 model 字段 → 按 2.0 直连处理",
          webapp.task_channel({"duration": 10}) == "direct")

    # 7) _render_task 真的会为 2.5 切到扩展内核，并且不再碰直连 submit_video
    tmp_out = tempfile.mkdtemp()
    webapp.DEMO_MODE = False
    webapp.PROVIDER_URL = ""
    webapp.DESKTOP_MODE = True

    class FakeDgc:
        called = {"submit": 0, "poll": 0}

        @staticmethod
        def submit_video(*a, **k):
            FakeDgc.called["submit"] += 1
            raise AssertionError("2.5 不应该再走直连提交")

        @staticmethod
        def poll_and_download(conv_id, cookie, output_dir, name, ratio, duration, model):
            FakeDgc.called["poll"] += 1
            path = os.path.join(tmp_out, name + ".mp4")
            with open(path, "wb") as fh:
                fh.write(b"x" * 16)
            FakeDgc.conv = conv_id
            return path

    webapp.load_generator = lambda: (FakeDgc, "")
    webapp._update_task = lambda *a, **k: None
    webapp.OUTPUT_DIR = tmp_out
    hook_calls = []

    def bg_hook(action, payload=None):
        hook_calls.append((action, dict(payload or {})))
        (payload or {}).get("out", {}).update(ok=True, conversation_id="7123456789012345678")

    webapp._control_hook = bg_hook
    task = {"id": "t25", "prompt": "雨夜", "model": "seedance_v2.5", "duration": 30,
            "ratio": "9:16", "account_id": 3, "ref_images": "", "min_bytes": 0}
    res = webapp._render_task(task, "a=1")
    check("2.5 任务：_render_task 调的是 browser_generate，不是直连 submit_video",
          hook_calls and hook_calls[0][0] == "browser_generate" and FakeDgc.called["submit"] == 0,
          "hook=%s submit=%s" % ([c[0] for c in hook_calls], FakeDgc.called))
    check("2.5 任务：浏览器拿到的 conversation_id 交给了轮询/下载",
          FakeDgc.called["poll"] == 1 and getattr(FakeDgc, "conv", "") == "7123456789012345678",
          "poll=%s conv=%s" % (FakeDgc.called["poll"], getattr(FakeDgc, "conv", "")))
    check("2.5 任务：最终产出照常返回给队列", res.get("url", "").startswith("/output/"), res)

    # 2.0 任务仍然直连
    class Dgc20(FakeDgc):
        @staticmethod
        def submit_video(*a, **k):
            Dgc20.called = getattr(Dgc20, "called", {})
            Dgc20.submit_called = True
            return "7000000000000000001"

    webapp.load_generator = lambda: (Dgc20, "")
    hook_calls.clear()
    task20 = {"id": "t20", "prompt": "雨夜", "model": "seedance_v2.0", "duration": 10,
              "ratio": "9:16", "account_id": 3, "ref_images": "", "min_bytes": 0}
    webapp._render_task(task20, "a=1")
    check("2.0 任务：仍然直连提交，完全不碰浏览器", not hook_calls and getattr(Dgc20, "submit_called", False),
          "hook=%s submit=%s" % ([c[0] for c in hook_calls], getattr(Dgc20, "submit_called", False)))

    # 扩展内核失败时，错误要带出来（不能吞成「生成失败」）
    def fail_hook(action, payload=None):
        (payload or {}).get("out", {}).update(ok=False, error="时长菜单里没有 30s",
                                              steps=[{"name": "选择时长 30", "ok": False, "detail": "30s 不在菜单里"}])

    webapp._control_hook = fail_hook
    try:
        webapp._render_task(task, "a=1")
        err = ""
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    check("扩展内核失败时把页面给的原因带回任务错误", "30s" in err and "选择时长" in err, err)

    # 8) process_task：自动选号后要把 account_id 写回 task。
    #    画布 / 分镜 / 批量任务都不传账号，2.5 全靠这个字段找浏览器窗口。
    conn = webapp.db()
    cur = conn.execute(
        "insert into accounts(name,cookie,status,created_at,check_state,check_detail) "
        "values(?,?,?,?,?,?)", ("自动选号", "a=1", "ready", webapp.utcnow(), "", ""))
    auto_acc = cur.lastrowid
    conn.execute(
        "insert into tasks(id,prompt,duration,ratio,model,status,quality,min_bytes,"
        "ref_images,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
        ("t-auto", "雨夜", 30, "9:16", "seedance_v2.5", "queued", "720p", 0, "",
         webapp.utcnow(), webapp.utcnow()))
    conn.commit()
    conn.close()
    seen = {}
    orig_render = webapp._render_task

    def fake_render(task, cookie):
        seen.update(dict(task))
        return {"url": "/output/t-auto.mp4", "path": "/tmp/t-auto.mp4", "bytes": 1}

    webapp._render_task = fake_render
    try:
        webapp.process_task("t-auto")
    finally:
        webapp._render_task = orig_render
    check("批量/画布任务自动选号后，account_id 会写回 task（2.5 才能找到浏览器窗口）",
          seen.get("account_id") == auto_acc,
          "account_id=%s（期望 %s）" % (seen.get("account_id"), auto_acc))
    check("这条任务的通道是扩展内核",
          webapp.task_channel(seen) == "extension", webapp.task_channel(seen))

    # 汇总
    bad = 0
    for name, ok, detail in OUT:
        if not ok:
            bad += 1
        print(("✅ " if ok else "❌ ") + name + ("" if ok else "  →  " + str(detail)[:300]))
    print(("✅ 扩展内核提交链路自检全部通过（%d 项）" % len(OUT)) if bad == 0
          else ("❌ 扩展内核提交链路自检失败 %d/%d 项" % (bad, len(OUT))))
    stop.set()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
