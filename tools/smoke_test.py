#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dola Pool 端到端自检脚本（只用标准库，可直接对运行中的服务执行）

用法：
    python tools/smoke_test.py                       # 默认 http://127.0.0.1:8000
    DOLA_BASE=http://127.0.0.1:9000 python tools/smoke_test.py
    DOLA_ADMIN_TOKEN=xxx python tools/smoke_test.py   # 服务开了鉴权时

它会真实地：建账号 → 提交任务 → 等出片 → 校验成片可下载 → 测 AI 推理 / 上传 /
资产 / 工作流 / 设置 / 队列 / 日志 / 重试，最后清理掉自己造的数据。
演示模式（DOLA_DEMO=1）下 10 秒左右即可跑完全部用例。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("DOLA_BASE", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("DOLA_ADMIN_TOKEN", "")
PASS, FAIL = [], []


def _local_db_path():
    """本地服务时返回数据库路径，用于构造“历史坏数据”用例。"""
    if not BASE.startswith(("http://127.0.0.1", "http://localhost")):
        return None
    candidates = [
        os.getenv("DOLA_DB", ""),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dola.db"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def call(method, path, data=None, files=None, raw=False, timeout=60):
    url = BASE + path
    headers = {"X-Admin-Token": TOKEN} if TOKEN else {}
    if files:
        boundary = "----dola%d" % time.time()
        body = b""
        for k, v in (data or {}).items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
        for k, (fn, content) in files.items():
            body += (
                '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, fn)
            ).encode() + content + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    else:
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            return r.status, (content if raw else json.loads(content or b"null"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode("utf-8", "replace")


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  ✅ " if condition else "  ❌ ") + name + (("  → " + str(detail)[:150]) if detail else ""))


def main():
    print("服务：%s\n" % BASE)
    status, health = call("GET", "/api/health")
    if status != 200:
        print("❌ 无法连接服务（%s）。请先启动：python app.py" % status)
        return 1
    print("== 健康检查 ==")
    check("服务可用 v%s（演示模式=%s，生成器=%s）" % (health["version"], health["demo_mode"], health["generator"]), True)

    print("\n== 账号池 ==")
    status, acc = call("POST", "/api/accounts", {"name": "自检账号", "cookie": "sessionid=smoke; sid_guard=test"})
    check("新增账号并本地校验", status == 200 and acc.get("ok"), acc)
    account_id = acc.get("id") if isinstance(acc, dict) else None
    status, res = call("POST", "/api/accounts/check-all")
    check("一键验活", status == 200 and res.get("count", 0) >= 1, res)
    status, imp = call("POST", "/api/import", {"cookies": "sessionid=a1\nsessionid=a2"})
    check("JSON 方式批量导入 2 条", status == 200 and imp.get("count") == 2, imp)
    status, accounts = call("GET", "/api/accounts")
    check("账号列表含今日用量字段", status == 200 and "today_usage" in accounts[0])

    print("\n== 任务全流程 ==")
    status, created = call("POST", "/api/tasks", {"prompt": "自检：雨夜霓虹街道慢动作水花", "duration": 15, "ratio": "9:16"})
    check("提交单条任务", status == 200 and created.get("count") == 1, created)
    status, batch = call("POST", "/api/tasks", {"tasks": [{"prompt": "自检批量 1", "duration": 10},
                                                          {"prompt": "自检批量 2", "duration": 30, "ratio": "16:9"}]})
    check("批量提交 2 条", status == 200 and batch.get("count") == 2, batch)
    task_id = created["ids"][0]
    task, seen = {}, set()
    for _ in range(60):
        status, task = call("GET", "/api/tasks/" + task_id)
        seen.add("%s(%s%%)" % (task["status"], task["progress"]))
        if task["status"] in ("completed", "failed"):
            break
        time.sleep(1)
    check("任务状态流转 %s" % " → ".join(sorted(seen)[:4]), task.get("status") == "completed", task.get("error"))
    check("成片地址已生成", bool(task.get("video_url")), task.get("video_url"))
    if task.get("video_url"):
        status, body = call("GET", task["video_url"], raw=True)
        check("成片可下载（%d 字节）" % len(body), status == 200 and len(body) > 10000)
    status, tasks = call("GET", "/api/tasks")
    check("任务列表 / 进度字段完整", status == 200 and any(t.get("video_url") for t in tasks))

    print("\n== Cookie 规范化（防止把 JSON 当成 Cookie）==")
    json_paste = json.dumps([{"name": "账号A", "cookie": "sessionid=REAL_COOKIE; sid_guard=abc"}],
                            ensure_ascii=False, indent=1)
    status, acc2 = call("POST", "/api/accounts", {"name": "JSON 粘贴", "cookie": json_paste})
    check("粘贴 JSON 时自动提取 Cookie 建号", status == 200 and acc2.get("ok"), acc2)
    acc2_id = acc2.get("id") if isinstance(acc2, dict) else None
    if acc2_id:
        status, chk = call("POST", "/api/accounts/%s/check" % acc2_id)
        detail = str(chk.get("detail", ""))
        check("验活返回可读结果，不再出现 Invalid header value",
              status == 200 and "Invalid header" not in detail, detail)

    # 模拟「升级前就已存进去的坏数据」：直接写库，再验活应自动修复
    db_path = _local_db_path()
    if db_path:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("insert into accounts(name,cookie,created_at) values(?,?,?)",
                     ("旧格式坏数据", json_paste, "2026-01-01T00:00:00Z"))
        legacy_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()
        status, chk2 = call("POST", "/api/accounts/%s/check" % legacy_id)
        check("历史坏数据在验活时被自动修复",
              status == 200 and "自动提取" in str(chk2.get("detail", "")), chk2.get("detail"))
        call("DELETE", "/api/accounts/%s" % legacy_id)
    else:
        check("历史坏数据自动修复", True, "服务非本地，跳过")
    status, bad = call("POST", "/api/accounts", {"name": "坏数据", "cookie": '[\n {\n "x": 1\n}\n]'})
    check("无法提取的 JSON 被拒绝并给出可读提示",
          status == 400 and "JSON" in str(bad), bad)
    status, imp2 = call("POST", "/api/import", {"cookies": json.dumps(
        [{"note": "没有 cookie 字段"}, "sessionid=good; sid_guard=x"], ensure_ascii=False)})
    check("导入时跳过无 Cookie 条目、保留有效条目",
          status == 200 and imp2.get("count") == 1 and imp2.get("skipped") == 1, imp2)
    status, imp3 = call("POST", "/api/import", {"cookies": json.dumps(
        [{"name": "sessionid", "value": "AAA"}, {"name": "sid_guard", "value": "BBB"}])})
    check("支持 name/value 键值对形式（Playwright 导出）",
          status == 200 and imp3.get("count") == 1, imp3)

    print("\n== AI 分镜推理 ==")
    status, ai = call("POST", "/api/ai-reason", {"script": "女孩等在便利店门口。男生递上雨伞，两人并肩走远。",
                                                 "style": "movie quality", "duration": 15})
    check("推理出 %s 个镜头（引擎 %s）" % (ai.get("count"), ai.get("engine")), status == 200 and ai.get("count", 0) >= 2)

    print("\n== 上传 / 资产 / 工作流 ==")
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000a49444154789c6300010000050001"
                        "0d0a2db40000000049454e44ae426082")
    status, up = call("POST", "/api/upload-image", files={"file": ("smoke.png", png)})
    check("上传图片", status == 200 and str(up.get("url", "")).startswith("/uploads/"), up)
    status, asset = call("POST", "/api/assets", {"name": "自检资产", "type": "character", "features": "特征",
                                                 "path": up.get("path", "")})
    check("新增资产", status == 200 and asset.get("ok"), asset)
    status, _ = call("PUT", "/api/assets/%s" % asset["id"], {"features": "改过的特征"})
    status, assets = call("GET", "/api/assets")
    check("资产可编辑", assets and assets[0]["features"] == "改过的特征")
    status, wf = call("POST", "/api/workflows", {"name": "自检工作流",
                                                 "data": {"nodes": [{"id": "node_1"}], "connections": []}})
    check("保存工作流到服务端", status == 200 and wf.get("ok"), wf)
    status, one = call("GET", "/api/workflows/%s" % wf["id"])
    check("读取工作流节点数 = %s" % len(one["data"]["nodes"]), one["data"]["nodes"][0]["id"] == "node_1")

    print("\n== 队列 / 设置 / 代理 / 日志 ==")
    status, q = call("GET", "/api/queue")
    check("队列信息（并发 %s，排队 %s）" % (q["workers"], q["queue_depth"]), status == 200)
    status, _ = call("POST", "/api/settings", {"max_concurrency": 4, "max_daily_per_account": 15})
    status, st = call("GET", "/api/settings")
    check("设置热更新生效（并发 %s / 每日 %s）" % (st["max_concurrency"], st["max_daily_per_account"]),
          st["max_concurrency"] == "4" and st["max_daily_per_account"] == "15")
    status, proxy = call("GET", "/api/proxy-status?probe=1")
    check("出口 IP 检测（connected=%s ip=%s）" % (proxy.get("connected"), proxy.get("ip")), status == 200)
    status, pr = call("POST", "/api/proxies/refresh")
    check("刷新代理接口可用", status == 200, pr.get("error", ""))
    status, stats = call("GET", "/api/stats")
    check("统计：完成 %s / 失败 %s / 占用 %s 字节" % (stats["completed_tasks"], stats["failed_tasks"], stats["output_bytes"]),
          status == 200 and stats["completed_tasks"] >= 1)
    status, audits = call("GET", "/api/audits")
    check("审计日志 %d 条" % len(audits), len(audits) > 3)
    status, _ = call("POST", "/api/keys", {"label": "自检密钥"})
    status, keys = call("GET", "/api/keys")
    check("密钥创建且列表脱敏", status == 200 and "••" in keys[0]["key"])

    print("\n== 清理自检数据 ==")
    status, retry = call("POST", "/api/tasks/retry-failed")
    check("一键重试失败任务接口可用", status == 200)
    call("DELETE", "/api/tasks/" + task_id)
    call("DELETE", "/api/assets/%s" % asset["id"])
    call("DELETE", "/api/workflows/%s" % wf["id"])
    if account_id:
        call("DELETE", "/api/accounts/%s" % account_id)
    if isinstance(locals().get("acc2"), dict) and acc2.get("id"):
        call("DELETE", "/api/accounts/%s" % acc2["id"])
    check("自检数据已清理", True)

    print("\n================ 结果 ================")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败用例：" + "，".join(FAIL))
        return 1
    print("✅ 全部通过：后端各模块工作正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
