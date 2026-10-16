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
import urllib.parse
import urllib.request

try:  # 反向代理用；requirements.txt 已包含 requests
    import requests as _requests
except Exception:  # noqa: BLE001 - 缺失时「打开 Dola 网页」会给出可读提示
    _requests = None

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

VERSION = "2.0.5"

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
# 官方账号验证接口：默认留空。留空时「验活」只做本地格式检查 + 连通性探测，
# 不会因为接口地址不对而把好账号误判为失效。知道真实接口时可自行配置。
ACCOUNT_CHECK_URL = os.getenv("DOLA_ACCOUNT_CHECK_URL", "").strip()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "是")


DEMO_MODE = _env_flag("DOLA_DEMO", False)          # 演示模式：不访问外网
DESKTOP_MODE = _env_flag("DOLA_DESKTOP", False)    # 桌面版（desktop.py 启动时置 1）
DESKTOP_CONFIG = os.getenv("DOLA_DESKTOP_CONFIG", "").strip()
DEMO_SECONDS = float(os.getenv("DOLA_DEMO_SECONDS", "6"))  # 演示渲染耗时

# 「打开账号 Dola 网页」用的本地反向代理：窗口里访问 /dola-web/{账号id}/…，
# 由后端注入该账号 Cookie 后再转发到 Dola。之所以不用 webview 直接开 dola.com，
# 是因为登录 Cookie（sessionid / sid_guard）多半是 HttpOnly，网页 JS 写不进去，
# 桌面 webview 也没有写 Cookie 的接口；走代理则 HttpOnly 一样生效。
WEB_PROXY = _env_flag("DOLA_WEB_PROXY", True)
# 打开账号网页时先落到哪个路径。Dola 的主界面是 /chat/，直接进这里比进根路径靠谱；
# 想改成根路径就把 DOLA_WEB_START 设成 /（留空表示不改写，直接请求根路径）
WEB_PROXY_START = os.getenv("DOLA_WEB_START", "/chat/").strip()
DOLA_WEB_BASE = (os.getenv("DOLA_WEB_BASE", "").strip().rstrip("/")
                 or DOLA_COOKIE_DOMAIN.strip().rstrip("/")
                 or "https://www.dola.com")

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
_control_hook = None          # 桌面版注入：切换运行模式 / 退出应用


def set_control_hook(hook) -> None:
    """由 desktop.py 注入控制回调；Web 版不注入，相关接口会自动降级为「不支持」。"""
    global _control_hook
    _control_hook = hook


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


# ---------------------------------------------------------------- Cookie 归一化
_COOKIE_KEYS = (
    "cookie", "cookies", "cookie_str", "cookiestr", "cookie_string", "cookiestring",
    "cookie_header", "cookieheader", "session", "value",
)
_WRAPPER_KEYS = ("accounts", "data", "items", "list", "result", "results", "rows", "content")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _cookie_from_node(node, depth: int = 0) -> list:
    """从任意 JSON 结构里尽力提取 Cookie 字符串（支持嵌套数组/对象）。"""
    if depth > 4:
        return []
    found = []
    if isinstance(node, str):
        text = node.strip()
        if text and "=" in text:
            found.append(text)
    elif isinstance(node, dict):
        low = {str(k).lower(): v for k, v in node.items()}
        for key in _COOKIE_KEYS:
            value = low.get(key)
            if isinstance(value, str) and "=" in value:
                found.append(value.strip())
                break
        else:
            for key in _WRAPPER_KEYS:
                if key in low:
                    found.extend(_cookie_from_node(low[key], depth + 1))
                    break
    elif isinstance(node, (list, tuple)):
        # Playwright / DevTools 风格：[{"name": "sessionid", "value": "..."}, ...]
        pairs = []
        for item in node:
            if isinstance(item, dict):
                low = {str(k).lower(): v for k, v in item.items()}
                name, value = low.get("name"), low.get("value")
                if isinstance(name, str) and value is not None and not isinstance(value, (dict, list)):
                    pairs.append(f"{name}={value}")
        if pairs:
            found.append("; ".join(pairs))
        else:
            for item in node:
                found.extend(_cookie_from_node(item, depth + 1))
    return found


def sanitize_cookie(raw) -> tuple:
    """把用户粘贴的内容整理成一行合法 Cookie，返回 (cookie, 错误说明)。

    能识别：纯 Cookie 文本、多行文本（取第一条）、JSON 数组/对象
    （含 cookie / cookies / value 字段，或 name+value 键值对）。
    这样即使把整个 JSON 文件粘进来，也不会像以前那样被当成 Cookie 存下来，
    进而在发请求时炸出 "Invalid header value b'[\\n {\\n'"。
    """
    if raw is None:
        return "", "未检测到 Cookie"
    if not isinstance(raw, str):
        candidates = _cookie_from_node(raw)
        return (candidates[0], "") if candidates else ("", "内容里没有找到 Cookie 字段")

    text = raw.strip()
    if not text:
        return "", "未检测到 Cookie"

    if text[0] in "[{":
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            return "", f"这段内容以 JSON 开头但解析失败（{exc}）；请只粘贴 Cookie 本身"
        candidates = _cookie_from_node(data)
        if not candidates:
            return "", ("识别出这是 JSON，但里面没有可用的 Cookie 字段；"
                        "请粘贴 Cookie 本身，形如 sessionid=xxx; sid_guard=yyy")
        text = candidates[0]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", "未检测到 Cookie"
    cookie = _CONTROL_CHARS.sub(" ", lines[0]).strip()
    if "=" not in cookie:
        return "", ("这不是 Cookie：缺少 name=value 结构；"
                    "请粘贴形如 sessionid=xxx; sid_guard=yyy 的内容")
    if len(cookie) < 8:
        return "", "Cookie 太短，格式不完整"
    return cookie, ""


_probe_cache = {"at": 0.0, "result": (True, "")}


def _probe_reachable(timeout: float = 8.0) -> tuple:
    """轻量探测能否连通 dola.com，用来区分「网络不通」和「Cookie 无效」。"""
    now = time.time()
    if now - _probe_cache["at"] < 60:
        return _probe_cache["result"]
    url = DOLA_COOKIE_DOMAIN.rstrip("/") or "https://www.dola.com"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DolaPool"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            result = (resp.status < 500, f"HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        result = (True, f"HTTP {exc.code}")  # 拿到状态码就说明网络通
    except Exception as exc:  # noqa: BLE001
        result = (False, f"{type(exc).__name__}: {exc}")
    _probe_cache.update(at=now, result=result)
    return result


def local_account_check(account_id: int, deep: bool = True) -> dict:
    """账号验活：规范化 Cookie → 本地格式检查 →（可选）联网验证。"""
    conn = db()
    row = conn.execute("select id,name,cookie from accounts where id=?", (account_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "id": account_id, "state": "missing", "detail": "账号不存在"}

    stored = row["cookie"] or ""
    cookie, problem = sanitize_cookie(stored)
    note = ""
    if not problem and cookie != stored.strip():
        # 粘贴内容里包着 JSON / 多行文本：自动提取并写回，免得每次验活都报错
        conn.execute("update accounts set cookie=? where id=?", (cookie, account_id))
        conn.commit()
        note = "已从粘贴内容中自动提取 Cookie；"

    if problem:
        state, detail, text_ok = "invalid", problem, 0
    elif DEMO_MODE or not deep:
        state, detail, text_ok = "local_ready", note + "本地格式检查通过（演示模式未联网验证）", 1
    else:
        state, detail, text_ok = _remote_account_check(cookie)
        detail = note + detail

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
    """联网验证 Cookie；未配置官方验证接口时退化为「能否连通 dola.com」。"""
    if not ACCOUNT_CHECK_URL:
        reachable, detail = _probe_reachable()
        if reachable:
            return ("local_ready",
                    "本地格式通过；dola.com 可达（未配置官方验证接口，无法确认 Cookie 是否有效）", 1)
        return ("unknown",
                f"本地格式通过；但连不上 dola.com（{detail}）——请检查网络 / VPN / 代理", 1)

    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        req = urllib.request.Request(ACCOUNT_CHECK_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except ValueError as exc:
        # http.client 对非法请求头（含换行等）抛 ValueError
        return "invalid", f"Cookie 无法作为请求头发送：{exc}", 0
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "invalid", f"Cookie 已失效（HTTP {exc.code}）", 0
        if exc.code == 429:
            return "rate_limited", "被限流，稍后重试", 1
        return "unknown", f"验证接口返回 HTTP {exc.code}（检查 DOLA_ACCOUNT_CHECK_URL 是否正确）", 1
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"联网检查失败：{exc}（请检查网络 / VPN / 代理）", 1

    if '"error"' in body and '"code":0' not in body.replace(" ", ""):
        return "invalid", f"接口返回异常：{body[:120]}", 0
    return "ready", "官方接口验证通过", 1


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
        .replace("{{DESKTOP}}", "1" if DESKTOP_MODE else "0")
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
    cookie, problem = sanitize_cookie(x.get("cookie"))
    if problem:
        return JSONResponse({"error": problem}, 400)
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

    # Playwright / DevTools 导出的形式：一组 {"name":..., "value":...} 属于同一个账号
    if items and all(isinstance(i, dict) and "name" in i and "value" in i for i in items):
        joined = "; ".join(
            f"{i['name']}={i['value']}" for i in items
            if not isinstance(i["value"], (dict, list)) and i.get("name")
        )
        items = [{"cookie": joined}] if joined else []

    conn = db()
    added, skipped, reasons = 0, 0, []
    for i, item in enumerate(items, 1):
        item = {"cookie": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        raw = item.get("cookie") or item.get("cookies") or item.get("value") or item
        cookie, problem = sanitize_cookie(raw)
        if problem:
            skipped += 1
            if len(reasons) < 3:
                reasons.append(problem)
            continue
        conn.execute(
            "insert into accounts(name,cookie,created_at,note) values(?,?,?,?)",
            (item.get("name") or f"导入账号 {i}", cookie, utcnow(), item.get("note") or ""),
        )
        added += 1
    conn.commit()
    conn.close()
    audit("account.import", f"ok={added} skipped={skipped}")
    return {"ok": True, "count": added, "skipped": skipped, "reasons": reasons}


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


# ---------------------------------- 桌面版 ----------------------------------
def _desktop_mode_file() -> str:
    return DESKTOP_CONFIG or os.path.join(DATA_DIR, "desktop.json")


def _read_desktop_config() -> dict:
    try:
        with open(_desktop_mode_file(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_desktop_config(data: dict) -> None:
    path = _desktop_mode_file()
    merged = _read_desktop_config()
    merged.update(data)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("写入桌面配置失败：%s", exc)


@app.get("/api/desktop/config")
def api_desktop_config() -> dict:
    return {
        "desktop": DESKTOP_MODE,
        "demo": DEMO_MODE,
        "demo_seconds": DEMO_SECONDS,
        "version": VERSION,
        "data_dir": DATA_DIR,
        "output_dir": OUTPUT_DIR,
        "config_path": _desktop_mode_file(),
        "can_control": bool(_control_hook),
    }


@app.post("/api/desktop/mode")
async def api_desktop_mode(request: Request):
    """切换演示/生产模式：写配置 + 请求桌面外壳重启（Web 版下返回 need_restart）。"""
    if not DESKTOP_MODE:
        return JSONResponse({"error": "该接口仅在桌面版可用"}, 400)
    body = await request.json()
    demo = bool(body.get("demo"))
    _write_desktop_config({"demo": demo})
    audit("desktop.mode", "demo=%s" % demo)
    if _control_hook:
        threading.Thread(target=_control_hook, args=("restart",), daemon=True).start()
        return {"ok": True, "demo": demo, "restarting": True}
    return {"ok": True, "demo": demo, "restarting": False,
            "detail": "已写入配置，下次启动生效"}


@app.post("/api/desktop/quit")
def api_desktop_quit():
    if not _control_hook:
        return JSONResponse({"error": "该接口仅在桌面版可用"}, 400)
    audit("desktop.quit")
    threading.Thread(target=_control_hook, args=("quit",), daemon=True).start()
    return {"ok": True}


@app.post("/api/desktop/reveal")
async def api_desktop_reveal(request: Request):
    """在文件管理器里定位成片 / 上传目录（桌面版右键「打开所在文件夹」用）。"""
    body = await request.json()
    target = str(body.get("path") or "").strip()
    # 允许前端直接传静态 URL（/output/xxx.mp4、/uploads/xxx.jpg）
    if target.startswith("/output/"):
        target = os.path.join(OUTPUT_DIR, os.path.basename(target))
    elif target.startswith("/uploads/"):
        target = os.path.join(UPLOAD_DIR, os.path.basename(target))
    allowed = (os.path.abspath(OUTPUT_DIR), os.path.abspath(UPLOAD_DIR), os.path.abspath(DATA_DIR))
    if not target:
        target = DATA_DIR
    target = os.path.abspath(target)
    if not target.startswith(allowed):
        return JSONResponse({"error": "路径不在允许范围内"}, 400)
    if not os.path.exists(target):
        target = os.path.dirname(target)
    try:
        system = platform.system()
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", target] if os.path.isfile(target) else ["explorer", target])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", target] if os.path.isfile(target) else ["open", target])
        else:
            subprocess.Popen(["xdg-open", target if os.path.isdir(target) else os.path.dirname(target)])
        return {"ok": True, "path": target}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"打开失败：{exc}"}, 500)


# --------------------------------------------------------------------------
# 「打开 Dola 网页」：本地反向代理
# --------------------------------------------------------------------------
# 账号池里点【🌐 网页】会在新窗口打开 /dola-web/{账号id}/…
# 后端拿该账号的 Cookie 去请求 Dola，把响应回给页面。
# 这样即便登录 Cookie 是 HttpOnly（JS 写不进去）也能正常看到登录后的页面。
WEB_PROXY_PREFIX = "/dola-web"
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_PROXY_PREFIX_TAIL_RE = re.escape(WEB_PROXY_PREFIX.lstrip("/"))  # 斜杠会被正则吃掉，断言里不再带

# dola.com 的绝对地址或协议相对地址（含任意子域）；负向断言避免误伤 dola.com.cn
_RE_DOLA_URL = re.compile(r"(?:https?:)?//(?:[\w-]+\.)*dola\.com(?![\w.-])", re.I)
# 以 / 开头的站内绝对路径（HTML 属性与 CSS url()）。
# 正则会吃掉那个斜杠，所以两个断言都从斜杠「之后」开始看：
#   (?!/)         排除协议相对的 //host
#   (?!dola-web/) 排除已经加过前缀的，保证反复处理也幂等
_RE_ROOT_ATTR = re.compile(
    r"(\s(?:href|src|action|poster|formaction|data-src|data-original)\s*=\s*[\"'])/(?!/)(?!"
    + _PROXY_PREFIX_TAIL_RE + r"/)", re.I)
_RE_CSS_ROOT_URL = re.compile(r"(url\(\s*[\"']?)/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)", re.I)
# 下面几条是「浏览器端拦不住」的写法，只能在文本层面改写：
#   import("/assets/x.js")      —— 动态导入不是函数调用，注入脚本覆写不了
#   new Worker("/w.js")         —— Worker 里是另一个上下文
#   @import "/x.css"            —— CSS 的另一种写法
#   srcset="/a.png 1x, /b.png"  —— 图片多倍图
_RE_DYNAMIC_IMPORT = re.compile(
    r"\bimport\s*\(\s*([\"'])/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)")
_RE_WORKER = re.compile(
    r"\bnew\s+(?:Shared)?Worker\s*\(\s*([\"'])/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)")
_RE_IMPORT_SCRIPTS = re.compile(
    r"\bimportScripts\s*\(\s*([\"'])/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)")
_RE_CSS_IMPORT = re.compile(
    r"(@import\s*)([\"'])/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)", re.I)
_RE_SRCSET = re.compile(r"(\ssrcset\s*=\s*[\"'])([^\"']*)([\"'])", re.I)
_RE_SRCSET_URL = re.compile(r"(^|,\s*)/(?!/)(?!" + _PROXY_PREFIX_TAIL_RE + r"/)")
_RE_CSP_META = re.compile(
    r"<meta[^>]+http-equiv\s*=\s*[\"']?content-security-policy[\"']?[^>]*>", re.I)
_RE_BASE_TAG = re.compile(r"<base\b[^>]*>", re.I)
_RE_INTEGRITY = re.compile(r"\s+integrity\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)
_RE_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.I)
_RE_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.I)

# 转发给 Dola 时保留的请求头（Accept-Encoding 故意不保留：交给 requests 自己协商，
# 否则上游可能返回 brotli/zstd 而我们解不开）
_PROXY_KEEP_REQ = frozenset({
    "user-agent", "accept", "accept-language", "content-type", "range",
    "if-none-match", "if-modified-since", "cache-control", "pragma",
    "x-requested-with", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
})
# 回给浏览器时要丢掉的头：前四个会破坏本地代理，CSP/XFO/HSTS 会拦脚本或强制 HTTPS，
# set-cookie 单独处理
_PROXY_DROP_RESP = frozenset({
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "content-security-policy", "content-security-policy-report-only",
    "x-frame-options", "strict-transport-security", "public-key-pins",
    "report-to", "nel", "alt-svc", "upgrade", "trailer", "set-cookie",
})


def _html_escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _proxy_prefix(account_id: int) -> str:
    return "%s/%d" % (WEB_PROXY_PREFIX, int(account_id))


def _js_json(obj) -> str:
    """把 Python 对象塞进 <script> 里的安全写法（转义 < > & 防提前闭合）。"""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _rewrite_dola_urls(text: str, account_id: int) -> str:
    """把 dola 的绝对地址换成代理地址，并把站内绝对路径也挂到代理下。"""
    prefix = _proxy_prefix(account_id)
    text = _RE_DOLA_URL.sub(prefix, text)
    text = _RE_ROOT_ATTR.sub(lambda m: m.group(1) + prefix + "/", text)
    text = _RE_CSS_ROOT_URL.sub(lambda m: m.group(1) + prefix + "/", text)
    text = _RE_DYNAMIC_IMPORT.sub(lambda m: "import(" + m.group(1) + prefix + "/", text)
    text = _RE_WORKER.sub(lambda m: "new Worker(" + m.group(1) + prefix + "/", text)
    text = _RE_IMPORT_SCRIPTS.sub(lambda m: "importScripts(" + m.group(1) + prefix + "/", text)
    text = _RE_CSS_IMPORT.sub(lambda m: m.group(1) + m.group(2) + prefix + "/", text)
    text = _RE_SRCSET.sub(
        lambda m: m.group(1) + _RE_SRCSET_URL.sub(lambda mm: mm.group(1) + prefix + "/", m.group(2)) + m.group(3),
        text)
    return text


def _rewrite_location(location: str, account_id: int) -> str:
    """重定向目标：绝对 dola 地址与站内绝对路径都要拉回代理。"""
    m = _RE_DOLA_URL.match(location)
    if m:
        return _proxy_prefix(account_id) + location[m.end():]
    if location.startswith("/") and not location.startswith("//"):
        return _proxy_prefix(account_id) + location
    return location


def _merge_cookies(primary: str, secondary: str) -> str:
    """账号 Cookie 优先；浏览器回传的只补缺，免得页面上的旧值盖掉账号的。"""
    seen, out = set(), []
    for chunk in (primary or "", secondary or ""):
        for part in chunk.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key = part.split("=", 1)[0].strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(part)
    return "; ".join(out)


def _patch_set_cookie(value: str, account_id: int) -> str:
    """Dola 回写的 Cookie 落到本地代理上：去掉 Domain/Secure，Path 换成代理前缀。"""
    parts = [p.strip() for p in (value or "").split(";")]
    if not parts or "=" not in parts[0]:
        return ""
    out = [parts[0]]
    for attr in parts[1:]:
        low = attr.lower()
        if low.startswith("domain=") or low.startswith("secure") or low.startswith("partitioned"):
            continue  # 本地是 http，Secure 会让浏览器直接丢弃
        if low.startswith("path="):
            continue
        if low.startswith("samesite=none"):
            out.append("SameSite=Lax")
            continue
        out.append(attr)
    out.append("Path=" + _proxy_prefix(account_id) + "/")
    return "; ".join(out)


_PROXY_CSS = (
    "<style>#dlp-bar{position:fixed;left:0;right:0;bottom:0;min-height:28px;z-index:2147483647;"
    "background:rgba(10,22,20,.95);color:#bfe6de;font:12px/28px -apple-system,BlinkMacSystemFont,"
    "'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;padding:0 12px;display:flex;flex-wrap:wrap;"
    "align-items:center;gap:14px;box-sizing:border-box;border-top:1px solid #1e6f61;text-align:left}"
    "#dlp-bar a{color:#7fd4c4;text-decoration:none;cursor:pointer}"
    "#dlp-bar a:hover{text-decoration:underline}"
    "#dlp-bar .dlp-x{margin-left:auto;cursor:pointer;color:#7fa9a2}"
    "#dlp-log{flex:0 0 100%;max-height:38vh;overflow:auto;padding:6px 0 8px;color:#ffb4a8;"
    "font:11.5px/1.6 ui-monospace,Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-all;"
    "border-top:1px solid #1e6f61;margin-top:4px}</style>"
)

_PROXY_JS = r"""/* Dola Pool 本地代理：注入页面的运行时脚本（单独存放便于语法检查，实际由 app.py 内联下发） */
(function () {
  var C = window.__DLP__ || {};
  var P = C.prefix || '';
  var RE = /^(?:https?:)?\/\/(?:[\w-]+\.)*dola\.com(?![\w.-])/i;

  // 把 dola 绝对地址、协议相对地址、站内绝对路径统一拉回本地代理
  function fix(u) {
    if (typeof u !== 'string' || !u) return u;
    if (u.indexOf(P + '/') === 0 || u === P) return u;
    var m = u.match(RE);
    if (m) return P + u.slice(m[0].length);
    if (u.charAt(0) === '/' && u.charAt(1) !== '/') return P + u;
    return u;
  }
  window.__dlpFix = fix;

  // ---------------------------------------------------------------- 诊断面板
  var diag = { errors: [], failed: [] };
  var painted = null;
  function report(kind, text) {
    if (diag[kind].length >= 30) return;
    text = String(text).slice(0, 240);
    if (diag[kind].indexOf(text) >= 0) return;
    diag[kind].push(text);
    paint();
  }
  window.addEventListener('error', function (e) {
    if (e && e.target && e.target !== window && (e.target.src || e.target.href)) {
      report('failed', '资源失败 ' + (e.target.src || e.target.href));
      return;
    }
    report('errors', (e && e.message ? e.message : String(e)) + (e && e.filename ? ' @ ' + e.filename : ''));
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    report('errors', 'Promise 未捕获 ' + ((r && (r.message || r)) || ''));
  });

  // ---------------------------------------------------------------- 网络拦截
  try {
    var _fetch = window.fetch;
    if (_fetch) {
      window.fetch = function () {
        try {
          var a = arguments;
          if (typeof a[0] === 'string') a[0] = fix(a[0]);
          else if (a[0] && typeof a[0].url === 'string') {
            var n = fix(a[0].url);
            if (n !== a[0].url) { try { a[0] = new Request(n, a[0]); } catch (e2) { a[0] = n; } }
          }
        } catch (e) {}
        var p = _fetch.apply(this, arguments);
        try {
          p.then(function (r) {
            if (r && r.status >= 400) report('failed', 'HTTP ' + r.status + ' ' + String(r.url || '').slice(0, 120));
            return r;
          }, function (err) { report('errors', 'fetch 失败 ' + ((err && err.message) || err)); });
        } catch (e) {}
        return p;
      };
    }
  } catch (e) {}

  try {
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) {
      try { arguments[1] = fix(u); } catch (e) {}
      return _open.apply(this, arguments);
    };
    var _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      var xhr = this;
      try {
        xhr.addEventListener('loadend', function () {
          if (xhr.status >= 400) report('failed', 'XHR ' + xhr.status + ' ' + String(xhr.responseURL || '').slice(0, 120));
        });
      } catch (e) {}
      return _send.apply(this, arguments);
    };
  } catch (e) {}

  try {
    var _wopen = window.open;
    window.open = function (u) {
      try { arguments[0] = fix(u); } catch (e) {}
      return _wopen.apply(this, arguments);
    };
  } catch (e) {}

  try {
    var _beacon = navigator.sendBeacon && navigator.sendBeacon.bind(navigator);
    if (_beacon) navigator.sendBeacon = function (u, d) { return _beacon(fix(u), d); };
  } catch (e) {}

  // ---------------------------------------------------------------- 元素属性拦截
  var ATTRS = { src: 1, href: 1, action: 1, poster: 1, data: 1, 'data-src': 1, formaction: 1 };
  var _setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    try { if (ATTRS[String(name).toLowerCase()] && typeof value === 'string') value = fix(value); } catch (e) {}
    return _setAttr.call(this, name, value);
  };

  function patchProp(ctor, prop) {
    try {
      if (!ctor) return;
      var d = Object.getOwnPropertyDescriptor(ctor.prototype, prop);
      if (!d || !d.set || !d.get) return;
      var setter = d.set, getter = d.get;
      Object.defineProperty(ctor.prototype, prop, {
        configurable: true, enumerable: d.enumerable,
        get: function () { return getter.call(this); },
        set: function (v) { return setter.call(this, typeof v === 'string' ? fix(v) : v); }
      });
    } catch (e) {}
  }
  patchProp(window.HTMLScriptElement, 'src');
  patchProp(window.HTMLImageElement, 'src');
  patchProp(window.HTMLLinkElement, 'href');
  patchProp(window.HTMLIFrameElement, 'src');
  patchProp(window.HTMLMediaElement, 'src');
  patchProp(window.HTMLSourceElement, 'src');
  patchProp(window.HTMLTrackElement, 'src');
  patchProp(window.HTMLAnchorElement, 'href');
  patchProp(window.HTMLFormElement, 'action');
  patchProp(window.HTMLObjectElement, 'data');
  patchProp(window.HTMLEmbedElement, 'src');

  function fixNode(node) {
    try {
      if (!node) return;
      if (node.nodeType === 1) {
        if (node.attributes) {
          for (var i = 0; i < node.attributes.length; i++) {
            var a = node.attributes[i];
            if (a && ATTRS[String(a.name).toLowerCase()]) {
              var nv = fix(a.value);
              if (nv !== a.value) _setAttr.call(node, a.name, nv);
            }
          }
        }
      } else if (node.nodeType === 11) { /* DocumentFragment */
        var kids = node.childNodes;
        for (var k = 0; k < kids.length; k++) fixNode(kids[k]);
        return;
      } else {
        return;
      }
      var list = node.querySelectorAll ? node.querySelectorAll('*') : null;
      if (list) for (var j = 0; j < list.length; j++) fixNode(list[j]);
    } catch (e) {}
  }

  try {
    var _append = Node.prototype.appendChild;
    Node.prototype.appendChild = function (n) { fixNode(n); return _append.call(this, n); };
    var _insert = Node.prototype.insertBefore;
    Node.prototype.insertBefore = function (n, ref) { fixNode(n); return _insert.call(this, n, ref); };
    var _replace = Node.prototype.replaceChild;
    Node.prototype.replaceChild = function (n, old) { fixNode(n); return _replace.call(this, n, old); };
  } catch (e) {}

  try {
    var dHtml = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
    if (dHtml && dHtml.set) {
      Object.defineProperty(Element.prototype, 'innerHTML', {
        configurable: true, enumerable: dHtml.enumerable, get: dHtml.get,
        set: function (v) { var r = dHtml.set.call(this, v); fixNode(this); return r; }
      });
    }
  } catch (e) {}

  try {
    if (window.MutationObserver) {
      var mo = new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var m = muts[i];
          if (m.type === 'attributes') fixNode(m.target);
          else for (var j = 0; j < m.addedNodes.length; j++) fixNode(m.addedNodes[j]);
        }
      });
      var startMo = function () {
        try {
          mo.observe(document.documentElement, {
            childList: true, subtree: true, attributes: true,
            attributeFilter: ['src', 'href', 'action', 'poster', 'data-src', 'formaction']
          });
        } catch (e) {}
      };
      if (document.documentElement) startMo();
      else document.addEventListener('DOMContentLoaded', startMo);
    }
  } catch (e) {}

  try {
    if (navigator.serviceWorker && navigator.serviceWorker.register) {
      navigator.serviceWorker.register = function () {
        return Promise.reject(new Error('service worker disabled by local proxy'));
      };
    }
  } catch (e) {}

  // ---------------------------------------------------------------- 底部提示条
  function inDesktop() {
    return !!(window.pywebview && window.pywebview.api && window.pywebview.api.open_external);
  }

  function openExternal() {
    var url = location.href;
    if (inDesktop()) {
      try { window.pywebview.api.open_external(url); return; } catch (e) {}
    }
    try { window.open(url, '_blank'); } catch (e) {}
  }
  window.__dlpOpenExternal = openExternal;

  function paint() {
    try {
      var badge = document.getElementById('dlp-badge');
      if (badge) {
        var n = diag.errors.length + diag.failed.length;
        badge.textContent = n ? ('⚠ ' + n) : '';
        badge.style.display = n ? '' : 'none';
      }
      var log = document.getElementById('dlp-log');
      if (log && log.style.display !== 'none') {
        log.textContent = '';
        var all = diag.errors.concat(diag.failed);
        for (var i = 0; i < all.length; i++) {
          var line = document.createElement('div');
          line.textContent = '· ' + all[i];
          log.appendChild(line);
        }
        if (!all.length) log.textContent = '暂无错误';
      }
    } catch (e) {}
  }

  function buildBar() {
    if (document.getElementById('dlp-bar')) return;
    var d = document.createElement('div');
    d.id = 'dlp-bar';

    var mk = function (tag, text, cls) {
      var el = document.createElement(tag);
      if (text) el.textContent = text;
      if (cls) el.className = cls;
      return el;
    };

    d.appendChild(mk('span', '🔗 本地代理'));
    d.appendChild(mk('span', C.label || ''));
    var ext = mk('a', '🌐 用系统浏览器打开', 'dlp-act');
    ext.href = 'javascript:void 0';
    ext.onclick = openExternal;
    d.appendChild(ext);
    var re = mk('a', '↻ 重载', 'dlp-act');
    re.href = 'javascript:void 0';
    re.onclick = function () { location.reload(); };
    d.appendChild(re);

    var badge = mk('a', '', 'dlp-act');
    badge.id = 'dlp-badge';
    badge.href = 'javascript:void 0';
    badge.style.display = 'none';
    badge.onclick = function () {
      var log = document.getElementById('dlp-log');
      if (!log) return;
      log.style.display = log.style.display === 'none' ? '' : 'none';
      paint();
    };
    d.appendChild(badge);

    var hide = mk('span', '✕ 隐藏', 'dlp-x');
    hide.onclick = function () { d.style.display = 'none'; };
    d.appendChild(hide);

    var log = mk('div', '');
    log.id = 'dlp-log';
    log.style.display = 'none';
    d.appendChild(log);

    (document.body || document.documentElement).appendChild(d);
    paint();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildBar);
  else buildBar();
})();"""


def _proxy_snippet(account_id: int, account: dict) -> str:
    """注入页面的样式 + 运行时脚本。

    脚本单独放在 _PROXY_JS 里（tools/webproxy_test.py 会从本文件抠出来做语法与行为检查），
    配置先塞进 window.__DLP__ 再执行脚本本身，保证测试的就是上线的那一份。
    """
    cfg = _js_json({
        "prefix": _proxy_prefix(account_id),
        "label": "账号 #%s %s" % (account_id, (account.get("name") or "").strip()),
        "real": DOLA_WEB_BASE + "/",
    })
    # 注意：_PROXY_JS 是纯 JS（方便 tools/inject_test.js 直接抠出来 eval），
    # 这里必须自己补 <script> 标签 —— 漏了的话浏览器会把整段代码当文本显示出来
    return (_PROXY_CSS
            + "<script>window.__DLP__=" + cfg + ";</script>"
            + "<script>" + _PROXY_JS + "</script>")


def _proxy_inject(text: str, account_id: int, account: dict) -> str:
    """把提示条脚本插到 <head>（插在 head 里最不容易被页面自身的样式影响）。"""
    snippet = _proxy_snippet(account_id, account)
    m = _RE_HEAD_OPEN.search(text) or _RE_HTML_OPEN.search(text)
    if m:
        return text[:m.end()] + snippet + text[m.end():]
    return snippet + text


def _rewrite_dola_html(text: str, account_id: int, account: dict) -> str:
    text = _RE_CSP_META.sub("", text)                                    # 站点 CSP 会拦注入脚本
    text = _RE_INTEGRITY.sub("", text)                                   # 改了地址，SRI 校验必然不过
    text = _RE_BASE_TAG.sub(lambda m: _rewrite_dola_urls(m.group(0), account_id), text)
    text = _rewrite_dola_urls(text, account_id)
    return _proxy_inject(text, account_id, account)


def _proxy_notice(title: str, message: str, status: int = 200) -> HTMLResponse:
    """代理自己产生的说明页（账号不存在、连不上 Dola 等）。"""
    page = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>" + _html_escape(title) + "</title><style>"
        "body{margin:0;min-height:100vh;background:#0a0f11;color:#e6efed;display:flex;"
        "align-items:center;justify-content:center;font:15px/1.7 -apple-system,BlinkMacSystemFont,"
        "'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;padding:24px}"
        ".c{max-width:560px;background:#141a1c;border:1px solid #33413f;border-radius:14px;padding:26px}"
        "h1{margin:0 0 10px;font-size:18px;color:#0ca58c}p{margin:0 0 10px;color:#c9d8d6;white-space:pre-wrap}"
        "small{color:#8ba1a1}</style></head><body><div class=\"c\"><h1>"
        + _html_escape(title) + "</h1><p>" + _html_escape(message) + "</p>"
        "<small>Dola Pool 本地代理 · " + _html_escape(DOLA_WEB_BASE) + "</small>"
        "</div></body></html>"
    )
    return HTMLResponse(page, status_code=status)


def _upstream_url(rest: str, query: str) -> str:
    rest = (rest or "").lstrip("/")
    url = DOLA_WEB_BASE + "/" + rest if rest else DOLA_WEB_BASE + "/"
    if query:
        url += "?" + query
    return url


async def _serve_dola_proxy(account_id: int, rest: str, request: Request):
    """代理核心：把 rest 对应的 Dola 路径取回来、改写后交给窗口。"""
    if not WEB_PROXY:
        return _proxy_notice("本地代理已关闭", "启动时设置了 DOLA_WEB_PROXY=0。", 403)
    if _requests is None:
        return _proxy_notice("缺少 requests 库", "请执行 pip install -r requirements.txt", 500)

    # 只允许代理到配置的 Dola 站点，防止被当成任意中转
    if "://" in rest or any(seg == ".." for seg in rest.split("/")):
        return _proxy_notice("请求被拒绝", "路径不合法。", 400)

    conn = db()
    row = conn.execute("select * from accounts where id=?", (account_id,)).fetchone()
    conn.close()
    if row is None:
        return _proxy_notice("账号不存在", "账号 #%s 已被删除，请刷新账号池。" % account_id, 404)
    account = dict(row)
    cookie = (account.get("cookie") or "").strip()
    if not cookie:
        return _proxy_notice("该账号没有 Cookie", "先在账号池里编辑这个账号，填入 Cookie。", 400)

    # 入口 /dola-web/2 补尾斜杠，并跳到 Dola 的真正主界面（默认 /chat/）。
    # 尾斜杠只在这里补：/static/app.js、/api/me 这类真实文件路径补了就成 404。
    if not rest:
        if not request.url.path.endswith("/"):
            target = request.url.path + "/"
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(target, status_code=307)
        if WEB_PROXY_START:
            return RedirectResponse(_proxy_prefix(account_id) + WEB_PROXY_START, status_code=302)

    url = _upstream_url(rest, request.url.query)
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in _PROXY_KEEP_REQ:
            headers[key] = value
    headers["Cookie"] = _merge_cookies(cookie, request.headers.get("cookie", ""))
    headers["Origin"] = DOLA_WEB_BASE
    ref = request.headers.get("referer", "")
    if ref:
        parsed = urllib.parse.urlparse(ref)
        path = parsed.path
        prefix = _proxy_prefix(account_id)
        if path.startswith(prefix):
            path = path[len(prefix):] or "/"
        headers["Referer"] = DOLA_WEB_BASE + path + (("?" + parsed.query) if parsed.query else "")

    body = None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        body = await request.body()

    try:
        up = _requests.request(request.method, url, headers=headers, data=body,
                               allow_redirects=False, timeout=(10, 90))
    except Exception as exc:  # noqa: BLE001
        log.warning("代理请求失败 %s %s：%s", request.method, url, exc)
        return _proxy_notice(
            "连不上 Dola",
            "请求 %s 失败：%s\n\n请检查电脑网络；如果开了 VPN / 代理，确认它能访问 dola.com。" % (url, exc),
            502,
        )

    content_type = (up.headers.get("content-type") or "").lower()
    payload = up.content
    if "text/html" in content_type or "application/xhtml" in content_type:
        charset = up.encoding or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
            charset = "utf-8"
        text = _rewrite_dola_html(text, account_id, account)
        payload = text.encode(charset, errors="replace")
    elif "text/css" in content_type or "javascript" in content_type or "ecmascript" in content_type:
        charset = up.encoding or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
            charset = "utf-8"
        payload = _rewrite_dola_urls(text, account_id).encode(charset, errors="replace")

    resp = Response(content=payload, status_code=up.status_code)
    for key, value in up.headers.items():
        low = key.lower()
        if low in _PROXY_DROP_RESP:
            continue
        resp.headers.append(key, value)
    if "location" in up.headers:
        resp.headers["location"] = _rewrite_location(up.headers["location"], account_id)
    for raw in (up.raw.headers.get_all("Set-Cookie") or []):
        patched = _patch_set_cookie(raw, account_id)
        if patched:
            resp.headers.append("set-cookie", patched)
    resp.headers["cache-control"] = "no-store"
    return resp


@app.api_route("/dola-web/{account_id}", methods=_PROXY_METHODS)
@app.api_route("/dola-web/{account_id}/{rest:path}", methods=_PROXY_METHODS)
async def api_dola_web(account_id: int, request: Request, rest: str = ""):
    """「🌐 网页」的入口：/dola-web/{账号id}/…"""
    return await _serve_dola_proxy(account_id, rest, request)


@app.post("/api/accounts/{account_id}/web")
def api_account_web(account_id: int):
    """在 webview（桌面版）或新标签页（Web 版）里打开该账号的 Dola 页面。"""
    if not WEB_PROXY:
        return JSONResponse({"error": "本地代理已关闭（DOLA_WEB_PROXY=0）"}, 400)
    conn = db()
    row = conn.execute("select id,name,cookie from accounts where id=?", (account_id,)).fetchone()
    conn.close()
    if row is None:
        return JSONResponse({"error": "账号不存在"}, 404)
    account = dict(row)
    cookie, _note = sanitize_cookie(account.get("cookie") or "")
    if not cookie:
        return JSONResponse({"error": "该账号没有可用的 Cookie，先补上再打开网页"}, 400)
    url = _proxy_prefix(account_id) + "/"
    title = "Dola · #%s %s" % (account_id, (account.get("name") or "").strip())
    audit("account.web", "id=%s" % account_id)
    if DESKTOP_MODE and _control_hook:
        try:
            # pywebview 要求「start() 之后 + 非主线程」才会立刻建窗口，
            # 这里和 restart / quit 一样丢到线程里执行
            threading.Thread(target=_control_hook, args=("open_web", {"url": url, "title": title}),
                             daemon=True).start()
            return {"ok": True, "mode": "desktop", "url": url, "title": title}
        except Exception as exc:  # noqa: BLE001
            log.warning("桌面窗口打开失败，退回浏览器：%s", exc)
    return {"ok": True, "mode": "browser", "url": url, "title": title}


_RE_PROXY_REFERER = re.compile(
    "^https?://[^/]+" + re.escape(WEB_PROXY_PREFIX) + r"/(\d+)(?:/|$)", re.I)


@app.api_route("/{fallback_path:path}", methods=_PROXY_METHODS, include_in_schema=False)
async def api_fallback(fallback_path: str, request: Request):
    """兜底：页面里没被改写干净的站内绝对路径（比如 JS 里 location.href='/chat/x'）。

    这些请求会落到本机根路径上。只要 Referer 指向某个账号的代理页面，
    就按同一个账号转发给 Dola —— 浏览器端拦不住的跳转也能正常工作。
    （/static、/output、/uploads 是静态挂载，走不到这里；页面里那类路径由注入脚本改写。）
    """
    referer = request.headers.get("referer", "") or request.headers.get("Referer", "")
    m = _RE_PROXY_REFERER.match(referer or "")
    if m and WEB_PROXY:
        return await _serve_dola_proxy(int(m.group(1)), fallback_path, request)
    return JSONResponse({"error": "Not Found", "path": "/" + fallback_path}, 404)


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
    if DESKTOP_MODE:
        log.info("桌面模式：控制接口已启用（%s）", DESKTOP_CONFIG or "默认配置路径")


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
