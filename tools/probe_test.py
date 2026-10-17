#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型体检的分支自检（不联网）。

重点验证两件事：
1) 提交端点 GET 404 时，会再用 OPTIONS / 去查询串的同路径探测，
   不能把「只收 POST 的网关」误报成「接口改版」；
2) 没勾「真实提交」时 ok=None，不能显示成 ❌、也不能污染结论。

用法：DOLA_DATA_DIR=$(mktemp -d) DOLA_DEMO=0 .venv/bin/python tools/probe_test.py
"""
import os
import sys
import tempfile
import types

os.environ.setdefault("DOLA_DATA_DIR", tempfile.mkdtemp())
os.environ["DOLA_DEMO"] = "0"
os.environ.pop("DOLA_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("HTTP_PROXY", None)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import app  # noqa: E402

OUT = []


def check(name, ok, detail=""):
    OUT.append((name, bool(ok), detail))


class Resp:
    def __init__(self, code, url="https://www.dola.com/chat/", allow=""):
        self.status_code = code
        self.url = url
        self.text = "<html>chat</html>"
        self.headers = {"Allow": allow} if allow else {}


class Session:
    mode = "post_only"

    def get(self, url, **kw):
        if "chat/completion" in url:
            return Resp(404, url)
        if "/im/chain/single" in url:
            return Resp(400, url)
        return Resp(200)

    def options(self, url, **kw):
        if Session.mode == "post_only":
            return Resp(405, url, allow="POST, OPTIONS")
        return Resp(404, url)


def fresh_account():
    conn = app.db()
    conn.execute("delete from accounts")
    cur = conn.execute("insert into accounts(name,cookie) values(?,?)",
                       ("测号", "sessionid=abc; sid_guard=def"))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def run(mode):
    Session.mode = mode
    fake = types.SimpleNamespace(Session=lambda: Session())
    app._requests = fake
    app._probe_tcp_tls = lambda host, port=443, timeout=8.0: (True, "DNS 1ms / TCP 2ms / TLS 3ms")
    return app._probe_model(fresh_account(), "seedance_v2.5", 30, "9:16", False)


def step(result, name):
    for s in result.get("steps", []):
        if s["name"] == name:
            return s
    return {}


r1 = run("post_only")
ep = step(r1, "提交端点")
check("GET 404 + OPTIONS 405(Allow: POST) → 判定端点仍然存在", ep.get("ok") is True, ep.get("detail", ""))
check("不会把网关的 404 说成接口改版", "改版" not in (ep.get("detail", "") + ep.get("hint", "")), ep.get("detail", ""))
check("没勾真实提交时 ok=None（不是 False）", step(r1, "真实提交").get("ok") is None,
      repr(step(r1, "真实提交").get("ok")))
check("结论不把「没做真实提交」算成问题（结论是 ✅，不是红叉）",
      r1.get("verdict", "").startswith("✅") and "有问题的是" not in r1.get("verdict", ""),
      r1.get("verdict", ""))

r2 = run("unknown")
ep2 = step(r2, "提交端点")
check("OPTIONS 也 404 → 端点标成「未知」而不是失败", ep2.get("ok") is None, ep2.get("detail", ""))
check("未知时结论给出 ⚠️ 并建议真实提交", "⚠️" in r2.get("verdict", "") and "真实提交" in r2.get("verdict", ""),
      r2.get("verdict", ""))

r3 = run("post_only")
m = step(r3, "生成核心")
check("v2.5 只支持 10/30：传 30 能正常构造提交 URL", m.get("ok") is True, m.get("detail", ""))

bad = 0
for name, ok, detail in OUT:
    if not ok:
        bad += 1
    print(("✅ " if ok else "❌ ") + name + (("  → " + detail[:100]) if detail else ""))
print("\n" + ("✅ 模型体检分支自检全部通过" if not bad else f"❌ 模型体检分支自检 {bad} 项失败"))
sys.exit(1 if bad else 0)
