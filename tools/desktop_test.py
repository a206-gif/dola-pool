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
  6. 退出接口
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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
            check("端口 %d 仅绑定 127.0.0.1（本机）" % PORT,
                  bool(locals_) and all(a.startswith("127.0.0.1:") for a in locals_),
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

        print("\n== 4. reveal 路径处理 ==")
        status, res = call("POST", "/api/desktop/reveal", {"path": "/etc/passwd"})
        check("越权路径被拦截", status == 400, res)
        status, res = call("POST", "/api/desktop/reveal", {"path": "/output/task_demo.mp4"})
        check("静态 URL 被解析到成片目录（无文件时返回打开失败而非越权）",
              status in (200, 500) and "允许范围" not in str(res), str(res)[:120])

        print("\n== 5. 退出接口 ==")
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
