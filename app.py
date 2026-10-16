#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dola Pool —— AI 视频生成池化平台

把仓库原本散落在 /opt/dola-pool 的脚本整合成一个可以直接运行的完整程序：

  * 账号池      ：多账号导入 / 验活 / 每日限额 / 智能选号
  * 任务队列    ：并发 worker、断点恢复、失败重试、进度追踪（SQLite 持久化）
  * 代理池      ：动态住宅 IP 配置与轮换（可选）
  * 视频生成    ：调用 gen/dola_gen_clean.py（真实）或内置演示渲染（DOLA_DEMO=1）
  * AI 分镜推理 ：调用 Agnes AI 生成电影级分镜提示词（未配置 Key 时本地规则降级）
  * 资产库      ：角色 / 场景 / 道具参考图，供分镜与节点画布引用
  * 节点画布    ：可视化工作流（static/node-canvas.html，画布可保存到服务端）
  * 运维        ：统计面板、错误日志、审计日志、可选 HTTP 代理刷新

启动：
    python app.py                 # http://127.0.0.1:8000
    DOLA_DEMO=1 python app.py     # 演示模式：不联网，也能看到完整流程

环境变量见 README.md
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import logging
import os
import platform
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

VERSION = "2.0.0"

# --------------------------------------------------------------------------
# 目录与配置
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
GEN_DIR = os.path.join(BASE_DIR, "gen")
DATA_DIR = os.getenv("DOLA_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.getenv("DOLA_DB", os.path.join(DATA_DIR, "dola.db"))
OUTPUT_DIR = os.getenv("DOLA_OUTPUT_DIR", os.path.join(DATA_DIR, "output"))
UPLOAD_DIR = os.getenv("DOLA_UPLOAD_DIR", os.path.join(DATA_DIR, "uploads"))

for _d in (DATA_DIR, OUTPUT_DIR, UPLOAD_DIR):
    os.makedirs(_d, exist_ok=True)
if GEN_DIR not in sys.path:
    sys.path.insert(0, GEN_DIR)

HOST = os.getenv("DOLA_HOST_BIND", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))
ADMIN_TOKEN = os.getenv("DOLA_ADMIN_TOKEN", "").strip()
PROVIDER_URL = os.getenv("VIDEO_PROVIDER_URL", "").strip()
PROVIDER_TOKEN = os.getenv("VIDEO_PROVIDER_TOKEN", "").strip()
AI_API_KEY = os.getenv("AGNES_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AGNES_MODEL", "agnes-3.0-flash")
DOLA_COOKIE_DOMAIN = os.getenv("DOLA_COOKIE_DOMAIN", "https://www.dola.com")
ACCOUNT_CHECK_URL = os.getenv(
    "DOLA_ACCOUNT_CHECK_URL", "https://www.dola.com/passport/account/info/v2/"
)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "是")


DEMO_MODE = _env_flag("DOLA_DEMO", False)          # 演示模式：不访问外网
DEMO_SECONDS = float(os.getenv("DOLA_DEMO_SECONDS", "6"))  # 演示渲染耗时

logging.basicConfig(
    level=os.getenv("DOLA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("dola")

# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
SCHEMA = """
create table if not exists accounts(
    id integer primary key, name text, cookie text, status text default 'active',
    points integer default 0, created_at text, check_state text default 'unchecked',
    check_detail text default '', last_checked text, text_ok integer default 0,
    image_ok integer default 0, note text default '');
create table if not exists proxies(
    id integer primary key, host text, port integer, username text, password text,
    region text default '', status text default 'unknown', created_at text);
create table if not exists tasks(
    id text primary key, prompt text, duration integer, ratio text, model text,
    account_id integer, status text, progress integer default 0, result text,
    quality text default '720p', min_bytes integer default 30000000,
    created_at text, started_at text, finished_at text, error text,
    ref_images text default '', updated_at text);
create table if not exists api_keys(
    id integer primary key, label text, key text, enabled integer default 1, created_at text);
create table if not exists settings(k text primary key, v text);
create table if not exists errors(
    id integer primary key, level text, message text, created_at text);
create table if not exists audits(
    id integer primary key, action text, detail text, created_at text);
create table if not exists assets(
    id integer primary key, name text, type text default 'character', features text default '',
    path text default '', filename text default '', created_at text);
create table if not exists workflows(
    id integer primary key, name text, data text, created_at text, updated_at text);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=10000")
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    # 兼容旧库：补列
    for stmt in (
        "alter table tasks add column started_at text",
        "alter table tasks add column finished_at text",
        "alter table tasks add column error text",
        "alter table tasks add column ref_images text default ''",
        "alter table tasks add column updated_at text",
        "alter table accounts add column note text default ''",
        "alter table proxies add column region text default ''",
        "alter table proxies add column created_at text",
    ):
        conn = db()
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()


def utcnow() -> str:
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def audit(action: str, detail: str = "") -> None:
    try:
        conn = db()
        conn.execute(
            "insert into audits(action,detail,created_at) values(?,?,?)", (action, detail, utcnow())
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:  # 审计失败不能影响主流程
        log.warning("audit failed: %s", exc)


def error_log(level: str, message: str) -> None:
    try:
        conn = db()
        conn.execute(
            "insert into errors(level,message,created_at) values(?,?,?)", (level, message[:2000], utcnow())
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        log.warning("error_log failed: %s", exc)


def get_setting(key: str, default: str = "") -> str:
    try:
        conn = db()
        row = conn.execute("select v from settings where k=?", (key,)).fetchone()
        conn.close()
        return row["v"] if row else default
    except sqlite3.Error:
        return default


def set_setting(key: str, value: str) -> None:
    conn = db()
    conn.execute(
        "insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v",
        (str(key), str(value)),
    )
    conn.commit()
    conn.close()


def all_settings() -> dict:
    conn = db()
    out = {r["k"]: r["v"] for r in conn.execute("select k,v from settings")}
    conn.close()
    return out


def rows(table: str, limit: int = 2000) -> list:
    conn = db()
    data = [dict(r) for r in conn.execute(f"select * from {table} order by rowid desc limit ?", (limit,))]
    conn.close()
    return data


# --------------------------------------------------------------------------
# 运行时状态
# --------------------------------------------------------------------------
class DynamicSemaphore:
    """可动态调整上限的并发闸门（配合设置页里的并发数热更新）。"""

    def __init__(self, limit: int):
        self._limit = max(1, int(limit))
        self._active = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait(timeout=1.0)
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def active(self) -> int:
        with self._cond:
            return self._active


TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
CONCURRENCY_LIMIT = max(1, min(int(get_setting("max_concurrency", os.getenv("VIDEO_MAX_CONCURRENCY", "6"))), 100))
GATE = DynamicSemaphore(CONCURRENCY_LIMIT)
MAX_DAILY_PER_ACCOUNT = max(1, int(get_setting("max_daily_per_account", os.getenv("MAX_DAILY_PER_ACCOUNT", "20"))))
DEFAULT_MIN_BYTES = int(get_setting("min_bytes_default", "30000000"))
START_TIME = time.time()
_STOP = threading.Event()

_ai_module = None
_memory_ai_key = AI_API_KEY or ""


def load_generator():
    """按需导入 gen/dola_gen_clean.py；失败时给出可读原因。"""
    try:
        import dola_gen_clean as dgc  # type: ignore

        return dgc, None
    except Exception as exc:  # pragma: no cover - 依赖缺失时
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# 账号池
# --------------------------------------------------------------------------
def _today() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def account_usage(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "select count(*) from tasks where account_id=? and date(created_at)=?",
        (account_id, _today()),
    ).fetchone()[0]


def select_account_for_task():
    """智能选号：跳过正在跑任务的号，选今日用量最少的可用号。"""
    conn = db()
    accounts = conn.execute(
        "select id,name,status,cookie from accounts where status in ('active','ready') and cookie != ''"
    ).fetchall()
    if not accounts:
        conn.close()
        return None, "没有可用账号（请先在「账号」或「导入」页添加 Dola Cookie）"

    best, best_usage = None, None
    for acc in accounts:
        running = conn.execute(
            "select count(*) from tasks where account_id=? and status='running'", (acc["id"],)
        ).fetchone()[0]
        if running > 0:
            continue  # 每个号同时只跑一个任务，避免同 IP 并发触发风控
        usage = account_usage(conn, acc["id"])
        if usage >= MAX_DAILY_PER_ACCOUNT:
            continue
        if best_usage is None or usage < best_usage:
            best, best_usage = acc, usage
    conn.close()
    if best is None:
        return None, "所有账号都在运行中或今日额度已用完"
    return best, f"今日已用 {best_usage}/{MAX_DAILY_PER_ACCOUNT}"


def local_account_check(account_id: int, deep: bool = True) -> dict:
    """账号验活：本地格式检查 +（可选）调用官方接口验证 Cookie 是否有效。"""
    conn = db()
    row = conn.execute("select id,name,cookie from accounts where id=?", (account_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "id": account_id, "state": "missing", "detail": "账号不存在"}

    cookie = (row["cookie"] or "").strip()
    if not cookie:
        state, detail, text_ok = "invalid", "未检测到 Cookie", 0
    elif "=" not in cookie or len(cookie) < 8:
        state, detail, text_ok = "invalid", "Cookie 格式不完整", 0
    elif DEMO_MODE or not deep:
        state, detail, text_ok = "local_ready", "本地格式检查通过（演示模式未联网验证）", 1
    else:
        state, detail, text_ok = _remote_account_check(cookie)

    checked = utcnow()
    conn.execute(
        "update accounts set check_state=?,check_detail=?,last_checked=?,text_ok=?,image_ok=0,status=? where id=?",
        (state, detail, checked, text_ok, "ready" if text_ok else "invalid", account_id),
    )
    conn.commit()
    conn.close()
    audit("account.check", f"{account_id}:{state}")
    return {
        "ok": True,
        "id": account_id,
        "name": row["name"],
        "state": state,
        "detail": detail,
        "text_ok": bool(text_ok),
        "last_checked": checked,
    }


def _remote_account_check(cookie: str) -> tuple:
    req = urllib.request.Request(
        ACCOUNT_CHECK_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
        if '"error"' in body and '"code":0' not in body.replace(" ", ""):
            return "invalid", f"接口返回异常：{body[:120]}", 0
        return "ready", "官方接口验证通过", 1
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "invalid", f"Cookie 已失效（HTTP {exc.code}）", 0
        if exc.code == 429:
            return "rate_limited", "被限流，稍后重试", 1
        return "unknown", f"HTTP {exc.code}", 1
    except Exception as exc:
        return "unknown", f"联网检查失败：{exc}", 1


def ensure_demo_account() -> int:
    """演示模式兜底：没有账号时自动建一个演示账号，保证流程能跑通。"""
    conn = db()
    row = conn.execute("select id from accounts order by id limit 1").fetchone()
    if row:
        account_id = row["id"]
        conn.execute("update accounts set status='ready' where id=?", (account_id,))
    else:
        cur = conn.execute(
            "insert into accounts(name,cookie,status,created_at,check_state,check_detail) "
            "values(?,?,?,?,?,?)",
            ("演示账号", "demo_cookie=1; demo_session=ok", "ready", utcnow(), "local_ready", "演示模式自动创建"),
        )
        account_id = cur.lastrowid
    conn.commit()
    conn.close()
    return account_id


# --------------------------------------------------------------------------
# 任务处理
# --------------------------------------------------------------------------
def process_task(task_id: str) -> None:
    conn = db()
    row = conn.execute("select * from tasks where id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return
    task = dict(row)
    if task["status"] == "cancelled":
        return

    account_id = task.get("account_id")
    cookie = ""
    if account_id:
        conn = db()
        acc = conn.execute("select cookie from accounts where id=?", (account_id,)).fetchone()
        conn.close()
        if acc:
            cookie = acc["cookie"] or ""
    if not cookie:
        acc, info = select_account_for_task()
        if acc is None and DEMO_MODE:
            demo_id = ensure_demo_account()
            conn = db()
            row = conn.execute("select cookie from accounts where id=?", (demo_id,)).fetchone()
            conn.close()
            acc = {"id": demo_id, "cookie": (row["cookie"] if row else "demo_cookie=1")}
            info = "演示账号"
        if acc is None:
            _finish(task_id, "failed", info, info)
            return
        account_id = acc["id"]
        cookie = acc["cookie"]
        conn = db()
        conn.execute("update tasks set account_id=? where id=?", (account_id, task_id))
        conn.commit()
        conn.close()

    _update_task(task_id, status="running", progress=10, started_at=utcnow(), result="已分配账号，准备提交")
    log.info("task %s -> account %s", task_id, account_id)

    try:
        result = _render_task(task, cookie)
        if result.get("url"):
            _finish(task_id, "completed", f"{result['path']}|{result['bytes']}bytes", None, progress=100)
            audit("task.complete", task_id)
        else:
            raise RuntimeError(result.get("error") or "生成失败")
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，便于一键重试
        msg = f"{type(exc).__name__}: {exc}"
        _finish(task_id, "failed", msg, msg)
        error_log("generate", f"{task_id}: {msg}")
        audit("task.failed", task_id)
        log.warning("task %s failed: %s", task_id, msg)


def _render_task(task: dict, cookie: str) -> dict:
    """真实生成（gen/dola_gen_clean.py）或演示渲染。"""
    if DEMO_MODE:
        return _demo_render(task)
    if PROVIDER_URL:
        return _provider_render(task)

    dgc, err = load_generator()
    if dgc is None:
        raise RuntimeError(f"无法加载视频生成模块 gen/dola_gen_clean.py（{err}）")

    prompt = task.get("prompt", "")
    ratio = task.get("ratio", "16:9")
    duration = int(task.get("duration") or 30)
    model = task.get("model") or "seedance_v2.0"

    refs = [p.strip() for p in (task.get("ref_images") or "").split(",") if p.strip()]
    _update_task(task["id"], progress=20, result="正在提交到 Dola…")
    conv_id = dgc.submit_video(prompt, ratio, duration, model, cookie, image_paths=refs or None)
    if not conv_id:
        raise RuntimeError("提交失败：未返回 conversation_id")

    _update_task(task["id"], progress=35, result="已提交，等待出片…")
    name = f"task_{task['id']}"
    video_path = dgc.poll_and_download(conv_id, cookie, OUTPUT_DIR, name, ratio, duration, model)
    if not video_path or not os.path.exists(video_path):
        raise RuntimeError("轮询超时或下载失败")

    size = os.path.getsize(video_path)
    min_bytes = int(task.get("min_bytes") or DEFAULT_MIN_BYTES)
    if 0 < size < min_bytes:
        log.warning("task %s 产出偏小：%s < %s", task["id"], size, min_bytes)
    return {"path": video_path, "bytes": size, "url": "/output/" + os.path.basename(video_path)}


def _provider_render(task: dict) -> dict:
    """走外部视频供应商 HTTP 接口（VIDEO_PROVIDER_URL）。"""
    payload = json.dumps({k: task.get(k) for k in ("prompt", "duration", "ratio", "model", "quality")}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "DolaPool/2.0"}
    if PROVIDER_TOKEN:
        headers["Authorization"] = "Bearer " + PROVIDER_TOKEN
    last_err = "provider 未返回结果"
    for attempt in range(4):
        req = urllib.request.Request(PROVIDER_URL, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            url = body.get("result_url") or body.get("url")
            if not url:
                raise RuntimeError(f"provider 返回缺少 url：{str(body)[:200]}")
            name = f"task_{task['id']}.mp4"
            path = os.path.join(OUTPUT_DIR, name)
            _update_task(task["id"], progress=70, result="正在下载成片…")
            with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            return {"path": path, "bytes": os.path.getsize(path), "url": "/output/" + name}
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            time.sleep(min(20, 2 ** attempt))
    raise RuntimeError(f"provider 调用失败：{last_err}")


def _demo_render(task: dict) -> dict:
    """演示渲染：把内置示例视频按任务参数复制到输出目录，逐段推进进度。"""
    ratio = task.get("ratio") or "16:9"
    sample = os.path.join(STATIC_DIR, "demo", "sample_9x16.mp4" if ratio == "9:16" else "sample_16x9.mp4")
    if not os.path.exists(sample):
        raise RuntimeError("缺少演示素材 static/demo/*.mp4，请先执行 python tools/make_assets.py")
    steps = [(25, "演示模式：正在解析提示词…"), (45, "演示模式：正在采样关键帧…"),
             (70, "演示模式：正在合成画面…"), (90, "演示模式：正在封装视频…")]
    seg = max(0.4, DEMO_SECONDS / len(steps))
    for progress, note in steps:
        for _ in range(int(seg * 10)):
            if _STOP.is_set():
                raise RuntimeError("服务停止")
            time.sleep(0.1)
        _update_task(task["id"], progress=progress, result=note)
    name = f"task_{task['id']}.mp4"
    dest = os.path.join(OUTPUT_DIR, name)
    shutil.copyfile(sample, dest)
    return {"path": dest, "bytes": os.path.getsize(dest), "url": "/output/" + name}


def _update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = db()
    conn.execute(f"update tasks set {cols} where id=?", (*fields.values(), task_id))
    conn.commit()
    conn.close()


def _finish(task_id: str, status: str, result: str, err, progress: int = 0) -> None:
    conn = db()
    conn.execute(
        "update tasks set status=?,progress=?,result=?,error=?,finished_at=?,updated_at=? where id=?",
        (status, progress, result, err, utcnow(), utcnow(), task_id),
    )
    conn.commit()
    conn.close()


def dispatcher_loop() -> None:
    """从队列取任务 -> 等并发槽 -> 执行。"""
    while not _STOP.is_set():
        try:
            task_id = TASK_QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue
        GATE.acquire()
        if _STOP.is_set():
            GATE.release()
            TASK_QUEUE.task_done()
            break
        threading.Thread(target=_slot, args=(task_id,), daemon=True).start()


def _slot(task_id: str) -> None:
    try:
        process_task(task_id)
    except Exception as exc:  # noqa: BLE001 - worker 不能挂
        log.exception("worker crashed on %s: %s", task_id, exc)
        error_log("worker", f"{task_id}: {exc}")
    finally:
        GATE.release()
        TASK_QUEUE.task_done()


def recover_stuck_tasks() -> None:
    """服务重启后把 running/queued 的任务重新排队。"""
    conn = db()
    stuck = [r["id"] for r in conn.execute("select id from tasks where status in ('running','queued')")]
    conn.execute("update tasks set status='queued',progress=0,result='服务重启，重新排队' where status in ('running','queued')")
    conn.commit()
    conn.close()
    for tid in stuck:
        TASK_QUEUE.put(tid)
    if stuck:
        log.info("已恢复 %d 个未完成任务", len(stuck))


# --------------------------------------------------------------------------
# AI 分镜推理
# --------------------------------------------------------------------------
def ai_available() -> bool:
    return bool(_memory_ai_key)


def ai_reason(payload: dict) -> dict:
    script = (payload.get("script") or "").strip()
    style = payload.get("style") or "movie quality"
    duration = int(payload.get("duration") or 15)
    image_url = payload.get("image_url") or ""
    if not script and not image_url:
        return {"error": "请填写脚本或上传参考图"}

    if ai_available():
        try:
            return {"ok": True, "scenes": _ai_reason_remote(script, style, duration, image_url), "engine": "agnes"}
        except Exception as exc:  # noqa: BLE001 - 远程失败时本地降级
            log.warning("AI 推理失败，改用本地规则：%s", exc)
            error_log("ai", str(exc))
    return {"ok": True, "scenes": _ai_reason_local(script, style, duration), "engine": "local"}


def _ai_reason_remote(script: str, style: str, duration: int, image_url: str) -> list:
    system_prompt = (
        "你是专业 AI 视频分镜师。根据用户文案生成可直接投喂视频模型的中文提示词。"
        "每个镜头一段，100-200 字，包含：主体与外观、动作与表演节奏、镜头运动、光线与影调、画质描述。"
        "只输出提示词，每行一条，不要编号，不要解释。"
    )
    user_text = (
        f"文案：{script}\n风格：{style}\n单镜头时长：{duration} 秒\n"
        "请输出 3-8 条分镜提示词。"
    )
    if image_url:
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    else:
        user_content = user_text
    body = json.dumps(
        {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.7,
            "max_tokens": 2000,
        }
    ).encode()
    req = urllib.request.Request(
        AI_BASE_URL + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + _memory_ai_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    content = data["choices"][0]["message"]["content"]
    return _split_prompts(content, duration, image_url)


def _split_prompts(content: str, duration: int, image_url: str = "") -> list:
    scenes = []
    for line in (content or "").splitlines():
        line = re.sub(r"^\s*(?:\d+[\.、:：)]|[-*•]|Scene[_ ]?\d+[:：]?)\s*", "", line.strip())
        if len(line) > 8:
            scenes.append(line)
    if not scenes and content:
        scenes = [content.strip()[:400]]
    out = []
    for i, prompt in enumerate(scenes[:15]):
        out.append(
            {
                "title": f"镜头_{i + 1}",
                "script": "",
                "prompt": prompt,
                "duration": duration,
                "ratio": "9:16",
                "status": "ready",
                "ref_image": image_url or "",
            }
        )
    return out


def _ai_reason_local(script: str, style: str, duration: int) -> list:
    """没有 AI Key 时的本地规则降级：按句子切分并套用电影级提示词模板。"""
    text = (script or "").strip()
    if not text:
        return [
            {
                "title": "镜头_1",
                "script": "",
                "prompt": f"{style}，9:16 竖屏，{duration} 秒，电影级光影，柔和逆光，浅景深，"
                "主体居中偏下，缓慢推轨镜头，8K 超清，细腻质感，无字幕无水印。",
                "duration": duration,
                "ratio": "9:16",
                "status": "ready",
            }
        ]
    chunks = [c.strip() for c in re.split(r"[。！？\n]+", text) if c.strip()]
    chunks = chunks[:15] or [text]
    lens = ["缓慢推轨镜头", "手持轻微呼吸感", "环绕半圈运镜", "低角度仰拍", "俯拍下压",
            "侧向横移", "缓慢拉远", "固定机位微晃"]
    light = ["黄金时刻侧逆光", "柔和窗光", "霓虹冷调", "顶光硬阴影", "黄昏暖调补光", "阴天柔光"]
    out = []
    for i, chunk in enumerate(chunks):
        prompt = (
            f"{style}，9:16 竖屏，{duration} 秒。画面内容：{chunk}。"
            f"镜头：{lens[i % len(lens)]}。光线：{light[i % len(light)]}。"
            "表演节奏：预备—主动作—过冲—回稳。画质：电影级质感，8K 超清，60 帧，浅景深，"
            "肤色通透，运动平滑，无字幕、无水印、无 UI。"
        )
        out.append(
            {
                "title": f"镜头_{i + 1}",
                "script": chunk,
                "prompt": prompt,
                "duration": duration,
                "ratio": "9:16",
                "status": "ready",
            }
        )
    return out


# --------------------------------------------------------------------------
# 代理
# --------------------------------------------------------------------------
def proxy_status(demo: bool | None = None) -> dict:
    demo = DEMO_MODE if demo is None else demo
    proxy_url = os.getenv("DOLA_PROXY") or os.getenv("HTTPS_PROXY") or ""
    if demo:
        return {
            "connected": True,
            "ip": "203.0.113.88",
            "region": "台湾 · 演示",
            "sticky_min": 50,
            "provider": "demo",
            "proxy": proxy_url or "（演示模式未使用代理）",
        }
    if not proxy_url:
        return {"connected": False, "error": "未配置代理（DOLA_PROXY / HTTPS_PROXY）", "provider": "none"}
    try:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://api.ipify.org?format=json", timeout=12) as resp:
            ip = json.loads(resp.read().decode()).get("ip", "unknown")
        return {
            "connected": True,
            "ip": ip,
            "region": os.getenv("DOLA_PROXY_REGION", "未知"),
            "sticky_min": 50,
            "provider": os.getenv("DOLA_PROXY_PROVIDER", "custom"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "error": str(exc), "provider": "custom"}


def refresh_proxy() -> dict:
    """执行 scripts/refresh-proxy.sh（存在且配置了刷新地址时）。"""
    script = os.path.join(BASE_DIR, "scripts", "refresh-proxy.sh")
    if DEMO_MODE:
        return {"ok": True, "demo": True, "detail": "演示模式：模拟刷新代理成功", "status": proxy_status()}
    if not os.path.exists(script):
        return {"ok": False, "error": "缺少 scripts/refresh-proxy.sh"}
    if not os.getenv("DOLA_PROXY_REFRESH_URL"):
        return {"ok": False, "error": "未配置 DOLA_PROXY_REFRESH_URL，跳过刷新"}
    try:
        out = subprocess.run(["/bin/sh", script], capture_output=True, text=True, timeout=60)
        audit("proxy.refresh", out.stdout.strip()[:500])
        return {"ok": out.returncode == 0, "stdout": out.stdout, "stderr": out.stderr,
                "status": proxy_status()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(title="Dola Pool", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def _authed(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return True
    token = request.headers.get("x-admin-token") or request.query_params.get("token") \
        or request.cookies.get("dola_admin")
    return bool(token) and secrets.compare_digest(token, ADMIN_TOKEN)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not ADMIN_TOKEN:
        return await call_next(request)
    if path in ("/login", "/api/login", "/api/health") or path.startswith("/static"):
        return await call_next(request)
    if _authed(request):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"error": "未授权：请提供 X-Admin-Token"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


def _read_template(name: str) -> str:
    with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (
        _read_template("index.html")
        .replace("{{VERSION}}", VERSION)
        .replace("{{DEMO}}", "1" if DEMO_MODE else "0")
        .replace("{{AI}}", "1" if ai_available() else "0")
        .replace("{{PORT}}", str(PORT))
    )
    return HTMLResponse(html)


@app.get("/canvas", response_class=HTMLResponse)
def canvas() -> HTMLResponse:
    return HTMLResponse(_read_template("node-canvas.html"))


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dola Pool 登录</title>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c1113;color:#e7eeee;font:14px system-ui,Segoe UI,Arial}
form{background:#161c1e;border:1px solid #24312f;border-radius:12px;padding:28px;width:320px}
h1{font-size:18px;margin:0 0 16px}input{width:100%;padding:11px;border-radius:6px;border:1px solid #33413f;
background:#0f1416;color:#e7eeee;margin-bottom:14px}button{width:100%;padding:11px;border:0;border-radius:6px;
background:#078d78;color:#fff;cursor:pointer}p{color:#7fa39c;font-size:12px;margin:12px 0 0}</style></head>
<body><form method="post" action="/api/login"><h1>▣ Dola Pool</h1>
<input type="password" name="token" placeholder="管理员令牌 (DOLA_ADMIN_TOKEN)" autofocus>
<button type="submit">进入控制台</button><p>令牌由服务端环境变量 DOLA_ADMIN_TOKEN 配置</p></form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login")
async def do_login(request: Request):
    token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        token = str(body.get("token", ""))
    else:
        form = await request.form()
        token = str(form.get("token", ""))
    if ADMIN_TOKEN and secrets.compare_digest(token, ADMIN_TOKEN):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("dola_admin", token, httponly=True, samesite="lax", max_age=86400 * 7)
        audit("auth.login")
        return resp
    return JSONResponse({"error": "令牌不正确"}, status_code=401)


@app.get("/api/health")
def health() -> dict:
    dgc, gen_err = load_generator()
    return {
        "ok": True,
        "version": VERSION,
        "demo_mode": DEMO_MODE,
        "python": platform.python_version(),
        "uptime_seconds": int(time.time() - START_TIME),
        "generator": "ok" if dgc else f"unavailable ({gen_err})",
        "provider_configured": bool(PROVIDER_URL),
        "ai_configured": ai_available(),
        "admin_token": bool(ADMIN_TOKEN),
    }


# ---------------------------------- 统计 ----------------------------------
@app.get("/api/stats")
def api_stats() -> dict:
    conn = db()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    data = {
        "total_accounts": q("select count(*) from accounts"),
        "active_accounts": q("select count(*) from accounts where status in ('active','ready')"),
        "invalid_accounts": q("select count(*) from accounts where status='invalid'"),
        "cooling_accounts": q("select count(*) from accounts where check_state in ('rate_limited','cooling')"),
        "total_tasks": q("select count(*) from tasks"),
        "completed_tasks": q("select count(*) from tasks where status='completed'"),
        "failed_tasks": q("select count(*) from tasks where status='failed'"),
        "running_tasks": q("select count(*) from tasks where status='running'"),
        "queued_tasks": q("select count(*) from tasks where status='queued'"),
        "today_completed": q(
            "select count(*) from tasks where status='completed' and created_at >= datetime('now','-1 day')"
        ),
        "total_assets": q("select count(*) from assets"),
        "total_workflows": q("select count(*) from workflows"),
        "output_bytes": _dir_size(OUTPUT_DIR),
    }
    conn.close()
    data["demo_mode"] = DEMO_MODE
    data["ai_configured"] = ai_available()
    return data


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@app.get("/api/queue")
def api_queue() -> dict:
    conn = db()
    counts = {r["status"]: r["n"] for r in conn.execute("select status,count(*) n from tasks group by status")}
    conn.close()
    return {
        "queue_depth": TASK_QUEUE.qsize(),
        "workers": GATE.limit,
        "active": GATE.active,
        "counts": counts,
        "demo_mode": DEMO_MODE,
        "provider_configured": bool(PROVIDER_URL),
    }


# ---------------------------------- 账号 ----------------------------------
SECRET_FIELDS = {"cookie", "password", "key", "token"}


def _mask(row: dict) -> dict:
    return {k: ("••••••" if k in SECRET_FIELDS and v else v) for k, v in row.items()}


@app.get("/api/accounts")
def api_accounts() -> list:
    conn = db()
    accounts = [dict(r) for r in conn.execute("select * from accounts order by id desc")]
    for acc in accounts:
        acc["today_usage"] = account_usage(conn, acc["id"])
        acc["quota"] = MAX_DAILY_PER_ACCOUNT
    conn.close()
    return [_mask(a) for a in accounts]


@app.post("/api/accounts")
async def api_add_account(request: Request):
    x = await request.json()
    name = (x.get("name") or "").strip() or f"账号 {utcnow()[:16]}"
    cookie = (x.get("cookie") or "").strip()
    if not cookie:
        return JSONResponse({"error": "请填写 Cookie"}, 400)
    conn = db()
    cur = conn.execute(
        "insert into accounts(name,cookie,points,created_at,note) values(?,?,?,?,?)",
        (name, cookie, int(x.get("points") or 0), utcnow(), x.get("note") or ""),
    )
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    audit("account.create", name)
    result = local_account_check(account_id, deep=bool(x.get("verify")))
    return {"ok": True, "id": account_id, "check": result}


@app.post("/api/accounts/{account_id}/check")
def api_check_account(account_id: int) -> dict:
    return local_account_check(account_id)


@app.post("/api/accounts/check-all")
def api_check_all() -> dict:
    conn = db()
    ids = [r["id"] for r in conn.execute("select id from accounts order by id")]
    conn.close()
    results = [local_account_check(i) for i in ids]
    return {"ok": True, "count": len(results), "results": results}


@app.delete("/api/accounts/{account_id}")
def api_delete_account(account_id: int):
    conn = db()
    conn.execute("delete from accounts where id=?", (account_id,))
    conn.commit()
    conn.close()
    audit("account.delete", str(account_id))
    return {"ok": True}


@app.post("/api/import")
async def api_import(request: Request, file: UploadFile = File(None), cookies: str = Form("")):
    """支持：纯文本（每行一个 Cookie）、CSV、JSON 数组。

    既可表单上传文件（file 字段），也可直接 POST JSON：{"cookies": "..."} 或 {"cookies": [...]}。
    """
    text, filename, items = cookies, "", []
    if file is not None:
        filename = (file.filename or "").lower()
        text = (await file.read()).decode("utf-8-sig", "ignore")
    elif request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        if isinstance(body, list):
            items = body
            text = ""
        else:
            raw = body.get("cookies", body.get("cookie", ""))
            if isinstance(raw, list):
                items = raw
                text = ""
            else:
                items = []
                text = str(raw or "")
    try:
        if items:
            pass
        elif filename.endswith(".json") or text.strip().startswith(("[", "{")):
            data = json.loads(text)
            items = data if isinstance(data, list) else [data]
        elif filename.endswith(".csv"):
            items = list(csv.DictReader(io.StringIO(text)))
        else:
            items = [{"cookie": line.strip()} for line in text.splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"解析失败：{exc}"}, 400)

    conn = db()
    added = 0
    for i, item in enumerate(items, 1):
        item = {"cookie": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        cookie = item.get("cookie") or item.get("cookies") or item.get("value") or ""
        if not cookie:
            continue
        conn.execute(
            "insert into accounts(name,cookie,created_at,note) values(?,?,?,?)",
            (item.get("name") or f"导入账号 {i}", cookie, utcnow(), item.get("note") or ""),
        )
        added += 1
    conn.commit()
    conn.close()
    audit("account.import", str(added))
    return {"ok": True, "count": added}


# ---------------------------------- 任务 ----------------------------------
DURATIONS = (5, 10, 15, 30)
RATIOS = ("9:16", "16:9", "1:1")


@app.get("/api/tasks")
def api_tasks(status: str = "", q: str = "", limit: int = 500) -> list:
    sql, params = "select * from tasks", []
    clauses = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if q:
        clauses.append("(id like ? or prompt like ?)")
        params += [f"%{q}%", f"%{q}%"]
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by rowid desc limit ?"
    params.append(max(1, min(int(limit), 2000)))
    conn = db()
    data = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    for t in data:
        t["video_url"] = _task_video_url(t)
    return data


def _task_video_url(task: dict) -> str:
    result = task.get("result") or ""
    if "|" in result:
        path = result.split("|")[0]
        if os.path.exists(path):
            return "/output/" + os.path.basename(path)
    return ""


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    conn = db()
    row = conn.execute("select * from tasks where id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"error": "任务不存在"}, 404)
    task = dict(row)
    task["video_url"] = _task_video_url(task)
    return task


@app.post("/api/tasks")
async def api_create_tasks(request: Request):
    """支持单条 {prompt:...} 或批量 {tasks:[{prompt:...}, ...]}。"""
    x = await request.json()
    raw_items = x.get("tasks") if isinstance(x.get("tasks"), list) else [x]
    if not raw_items:
        return JSONResponse({"error": "没有任务"}, 400)
    if len(raw_items) > 500:
        return JSONResponse({"error": "单次最多提交 500 条"}, 400)

    created, conn = [], db()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        prompt = (item.get("prompt") or "").strip()
        if not prompt:
            continue
        duration = int(item.get("duration") or x.get("duration") or 30)
        if duration not in DURATIONS:
            duration = min(DURATIONS, key=lambda d: abs(d - duration))
        ratio = item.get("ratio") or x.get("ratio") or "16:9"
        if ratio not in RATIOS:
            ratio = "16:9"
        model = item.get("model") or x.get("model") or ("seedance_v2.0" if duration <= 15 else "seedance_v2.5")
        quality = item.get("quality") or x.get("quality") or "720p"
        min_bytes = int(item.get("min_bytes") or x.get("min_bytes") or DEFAULT_MIN_BYTES)
        ref_images = item.get("ref_images") or x.get("ref_images") or ""
        if isinstance(ref_images, list):
            ref_images = ",".join(ref_images)
        account_id = item.get("account_id") or x.get("account_id") or None
        task_id = secrets.token_hex(6)
        conn.execute(
            "insert into tasks(id,prompt,duration,ratio,model,account_id,status,quality,min_bytes,"
            "ref_images,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, prompt, duration, ratio, model, account_id, "queued", quality, min_bytes,
             ref_images, utcnow(), utcnow()),
        )
        created.append(task_id)
    conn.commit()
    conn.close()
    for tid in created:
        TASK_QUEUE.put(tid)
    if created:
        audit("task.create", ",".join(created[:20]))
    return {"ok": True, "ids": created, "count": len(created), "queue_depth": TASK_QUEUE.qsize()}


@app.post("/api/tasks/{task_id}/retry")
def api_retry(task_id: str):
    conn = db()
    row = conn.execute("select id from tasks where id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "任务不存在"}, 404)
    conn.execute(
        "update tasks set status='queued',progress=0,result=null,error=null,account_id=null,"
        "started_at=null,finished_at=null,updated_at=? where id=?",
        (utcnow(), task_id),
    )
    conn.commit()
    conn.close()
    TASK_QUEUE.put(task_id)
    audit("task.retry", task_id)
    return {"ok": True, "queue_depth": TASK_QUEUE.qsize()}


@app.post("/api/tasks/retry-failed")
def api_retry_failed() -> dict:
    conn = db()
    ids = [r["id"] for r in conn.execute("select id from tasks where status='failed'")]
    conn.execute(
        "update tasks set status='queued',progress=0,result=null,error=null,account_id=null,updated_at=? "
        "where status='failed'",
        (utcnow(),),
    )
    conn.commit()
    conn.close()
    for tid in ids:
        TASK_QUEUE.put(tid)
    audit("task.retry_all", str(len(ids)))
    return {"ok": True, "count": len(ids), "queue_depth": TASK_QUEUE.qsize()}


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str):
    conn = db()
    row = conn.execute("select result from tasks where id=?", (task_id,)).fetchone()
    conn.execute("delete from tasks where id=?", (task_id,))
    conn.commit()
    conn.close()
    if row and row["result"] and "|" in row["result"]:
        path = row["result"].split("|")[0]
        try:
            if os.path.isfile(path) and os.path.abspath(path).startswith(os.path.abspath(OUTPUT_DIR)):
                os.remove(path)
        except OSError:
            pass
    audit("task.delete", task_id)
    return {"ok": True}


# ---------------------------------- 资产 ----------------------------------
@app.get("/api/assets")
def api_assets() -> list:
    return rows("assets")


@app.post("/api/assets")
async def api_add_asset(request: Request):
    x = await request.json()
    conn = db()
    cur = conn.execute(
        "insert into assets(name,type,features,path,filename,created_at) values(?,?,?,?,?,?)",
        (x.get("name") or f"asset_{int(time.time())}", x.get("type") or "character",
         x.get("features") or "", x.get("path") or x.get("url") or "", x.get("filename") or "", utcnow()),
    )
    asset_id = cur.lastrowid
    conn.commit()
    conn.close()
    audit("asset.create", str(asset_id))
    return {"ok": True, "id": asset_id}


@app.put("/api/assets/{asset_id}")
async def api_update_asset(asset_id: int, request: Request):
    x = await request.json()
    fields = {k: x[k] for k in ("name", "type", "features", "path", "filename") if k in x}
    if fields:
        conn = db()
        conn.execute(
            f"update assets set {', '.join(f'{k}=?' for k in fields)} where id=?",
            (*fields.values(), asset_id),
        )
        conn.commit()
        conn.close()
    return {"ok": True}


@app.delete("/api/assets/{asset_id}")
def api_delete_asset(asset_id: int):
    conn = db()
    conn.execute("delete from assets where id=?", (asset_id,))
    conn.commit()
    conn.close()
    audit("asset.delete", str(asset_id))
    return {"ok": True}


@app.post("/api/upload-image")
async def api_upload_image(request: Request):
    try:
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"error": "缺少 file 字段"}, 400)
        ext = os.path.splitext(getattr(upload, "filename", "") or "")[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            return JSONResponse({"error": "仅支持图片格式"}, 400)
        name = secrets.token_hex(8) + ext
        path = os.path.join(UPLOAD_DIR, name)
        with open(path, "wb") as f:
            f.write(await upload.read())
        return {"ok": True, "path": path, "url": "/uploads/" + name}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, 500)


# ---------------------------------- 推理 ----------------------------------
@app.post("/api/ai-reason")
async def api_ai_reason(request: Request):
    payload = await request.json()
    result = ai_reason(payload)
    if "error" in result:
        return JSONResponse(result, 400)
    result["count"] = len(result["scenes"])
    for key in ("script", "style", "duration", "image_url"):
        if key in payload:
            audit("ai.reason", f"{key}=...")
            break
    return result


# ---------------------------------- 代理 / 密钥 / 日志 ----------------------------------
@app.get("/api/proxy-status")
def api_proxy_status(probe: int = 1) -> dict:
    if probe and not DEMO_MODE:
        return proxy_status()
    return proxy_status(demo=DEMO_MODE or not probe)


@app.post("/api/proxies/refresh")
def api_proxy_refresh() -> dict:
    return refresh_proxy()


@app.get("/api/proxies")
def api_proxies() -> list:
    return [_mask(r) for r in rows("proxies")]


@app.post("/api/proxies")
async def api_add_proxy(request: Request):
    x = await request.json()
    host = (x.get("host") or "").strip()
    port = int(x.get("port") or 0)
    if not host or not 1 <= port <= 65535:
        return JSONResponse({"error": "请填写有效的主机与端口"}, 400)
    conn = db()
    conn.execute(
        "insert into proxies(host,port,username,password,region,created_at) values(?,?,?,?,?,?)",
        (host, port, x.get("username") or "", x.get("password") or "", x.get("region") or "", utcnow()),
    )
    conn.commit()
    conn.close()
    audit("proxy.create", host)
    return {"ok": True}


@app.delete("/api/proxies/{proxy_id}")
def api_delete_proxy(proxy_id: int):
    conn = db()
    conn.execute("delete from proxies where id=?", (proxy_id,))
    conn.commit()
    conn.close()
    audit("proxy.delete", str(proxy_id))
    return {"ok": True}


@app.get("/api/keys")
def api_keys() -> list:
    return [_mask(r) for r in rows("api_keys")]


@app.post("/api/keys")
async def api_add_key(request: Request):
    x = await request.json()
    key = "dola_" + secrets.token_urlsafe(24)
    conn = db()
    conn.execute(
        "insert into api_keys(label,key,created_at) values(?,?,?)",
        (x.get("label") or "API key", key, utcnow()),
    )
    conn.commit()
    conn.close()
    audit("key.create", x.get("label") or "")
    return {"key": key}


@app.delete("/api/keys/{key_id}")
def api_delete_key(key_id: int):
    conn = db()
    conn.execute("delete from api_keys where id=?", (key_id,))
    conn.commit()
    conn.close()
    audit("key.delete", str(key_id))
    return {"ok": True}


@app.get("/api/errors")
def api_errors(limit: int = 300) -> list:
    return rows("errors", limit)


@app.delete("/api/errors")
def api_clear_errors():
    conn = db()
    conn.execute("delete from errors")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/audits")
def api_audits(limit: int = 300) -> list:
    return rows("audits", limit)


@app.delete("/api/audits")
def api_clear_audits():
    conn = db()
    conn.execute("delete from audits")
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------- 设置 ----------------------------------
SETTING_KEYS = ("max_concurrency", "max_daily_per_account", "min_bytes_default",
                "default_model", "default_ratio", "default_quality", "ai_api_key", "ai_base_url",
                "ai_model", "demo_seconds")


@app.get("/api/settings")
def api_settings() -> dict:
    saved = all_settings()
    saved.pop("ai_api_key", None)  # 不把密钥回传浏览器
    saved.update(
        {
            "max_concurrency": str(CONCURRENCY_LIMIT),
            "max_daily_per_account": str(MAX_DAILY_PER_ACCOUNT),
            "min_bytes_default": str(DEFAULT_MIN_BYTES),
            "demo_mode": "1" if DEMO_MODE else "0",
            "ai_configured": "1" if ai_available() else "0",
            "ai_base_url": AI_BASE_URL,
            "ai_model": AI_MODEL,
            "db_path": DB_PATH,
            "output_dir": OUTPUT_DIR,
            "data_dir": DATA_DIR,
            "version": VERSION,
        }
    )
    return saved


@app.post("/api/settings")
async def api_save_settings(request: Request):
    global CONCURRENCY_LIMIT, MAX_DAILY_PER_ACCOUNT, DEFAULT_MIN_BYTES, _memory_ai_key, AI_BASE_URL, AI_MODEL
    x = await request.json()
    if not isinstance(x, dict):
        return JSONResponse({"error": "设置必须是对象"}, 400)

    if "max_concurrency" in x:
        try:
            CONCURRENCY_LIMIT = max(1, min(int(x["max_concurrency"]), 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "并发数必须是 1-100 的整数"}, 400)
        GATE.set_limit(CONCURRENCY_LIMIT)
        x["max_concurrency"] = CONCURRENCY_LIMIT
    if "max_daily_per_account" in x:
        try:
            MAX_DAILY_PER_ACCOUNT = max(1, min(int(x["max_daily_per_account"]), 1000))
        except (TypeError, ValueError):
            return JSONResponse({"error": "每日限额必须是 1-1000 的整数"}, 400)
        x["max_daily_per_account"] = MAX_DAILY_PER_ACCOUNT
    if "min_bytes_default" in x:
        try:
            DEFAULT_MIN_BYTES = max(0, int(x["min_bytes_default"]))
        except (TypeError, ValueError):
            return JSONResponse({"error": "最小体积必须是整数"}, 400)
        x["min_bytes_default"] = DEFAULT_MIN_BYTES
    if x.get("ai_api_key"):
        _memory_ai_key = str(x["ai_api_key"]).strip()
    if x.get("ai_base_url"):
        AI_BASE_URL = str(x["ai_base_url"]).strip().rstrip("/")
    if x.get("ai_model"):
        AI_MODEL = str(x["ai_model"]).strip()

    conn = db()
    for k, v in x.items():
        conn.execute(
            "insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (str(k), str(v))
        )
    conn.commit()
    conn.close()
    audit("settings.save", ",".join(x.keys()))
    return {"ok": True, "max_concurrency": CONCURRENCY_LIMIT, "ai_configured": ai_available()}


# ---------------------------------- 节点画布工作流 ----------------------------------
@app.get("/api/workflows")
def api_workflows() -> list:
    conn = db()
    out = [
        {"id": r["id"], "name": r["name"], "updated_at": r["updated_at"], "nodes": _wf_count(r["data"])}
        for r in conn.execute("select id,name,data,updated_at from workflows order by id desc")
    ]
    conn.close()
    return out


def _wf_count(data: str) -> int:
    try:
        return len(json.loads(data).get("nodes", []))
    except Exception:  # noqa: BLE001
        return 0


@app.post("/api/workflows")
async def api_save_workflow(request: Request):
    x = await request.json()
    name = (x.get("name") or "").strip() or f"工作流 {utcnow()[:16]}"
    data = json.dumps(x.get("data") or {}, ensure_ascii=False)
    wid = x.get("id")
    conn = db()
    if wid:
        conn.execute("update workflows set name=?,data=?,updated_at=? where id=?", (name, data, utcnow(), wid))
    else:
        cur = conn.execute(
            "insert into workflows(name,data,created_at,updated_at) values(?,?,?,?)",
            (name, data, utcnow(), utcnow()),
        )
        wid = cur.lastrowid
    conn.commit()
    conn.close()
    audit("workflow.save", str(wid))
    return {"ok": True, "id": wid, "name": name}


@app.get("/api/workflows/{workflow_id}")
def api_workflow(workflow_id: int):
    conn = db()
    row = conn.execute("select * from workflows where id=?", (workflow_id,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"error": "工作流不存在"}, 404)
    return {"id": row["id"], "name": row["name"], "data": json.loads(row["data"] or "{}"),
            "updated_at": row["updated_at"]}


@app.delete("/api/workflows/{workflow_id}")
def api_delete_workflow(workflow_id: int):
    conn = db()
    conn.execute("delete from workflows where id=?", (workflow_id,))
    conn.commit()
    conn.close()
    audit("workflow.delete", str(workflow_id))
    return {"ok": True}


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def bootstrap() -> None:
    init_db()
    recover_stuck_tasks()
    threading.Thread(target=dispatcher_loop, daemon=True, name="dispatcher").start()
    log.info("Dola Pool v%s 启动 | 数据目录 %s | 并发 %d | 演示模式 %s | AI %s",
             VERSION, DATA_DIR, CONCURRENCY_LIMIT, "开" if DEMO_MODE else "关",
             "已配置" if ai_available() else "未配置（将使用本地规则）")
    if DEMO_MODE:
        log.info("演示模式：任务会 6 秒内生成内置示例视频，不需要真实账号与代理")


bootstrap()


def main() -> None:
    import uvicorn

    print(f"""
  ▣ Dola Pool v{VERSION} —— AI 视频生成池化平台
  ├─ 控制台   http://127.0.0.1:{PORT}/          （节点画布 /canvas）
  ├─ 数据目录 {DATA_DIR}
  ├─ 成片目录 {OUTPUT_DIR}
  ├─ 并发     {CONCURRENCY_LIMIT}   每日每号限额 {MAX_DAILY_PER_ACCOUNT}
  ├─ 演示模式 {'开（不访问外网）' if DEMO_MODE else '关（真实调用 Dola）'}
  └─ 鉴权     {'已开启（DOLA_ADMIN_TOKEN）' if ADMIN_TOKEN else '未开启'}
""")
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.getenv("DOLA_LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
