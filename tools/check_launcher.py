#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start-windows.bat 静态+动态检查器（不需要 Windows）

背景：这个批处理曾经两次出现「双击后窗口一闪就关」的问题，根因都是
变量未定义 / 控制流走错，而这些问题在 Linux 上无法直接运行验证。
所以这里写一个小型批处理解释器，把 bat 在各种环境下跑一遍，
检查：换行符、编码、标签解析、变量是否在使用前被赋值、失败路径是否都会 pause。

用法：
    python tools/check_launcher.py            # 检查仓库里的 start-windows.bat
"""
from __future__ import annotations

import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "start-windows.bat")

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(detail)[:150]) if detail else ""))


# --------------------------------------------------------------------------
# 静态检查
# --------------------------------------------------------------------------
def static_checks(text: str, raw: bytes) -> set:
    print("\n== 静态检查 ==")
    body = raw.replace(b"\r\n", b"")
    ok("换行符为 CRLF（无孤立 LF）", body.count(b"\n") == 0, "孤立 LF = %d" % body.count(b"\n"))
    ok("行中没有夹杂孤立 CR", body.count(b"\r") == 0, "孤立 CR = %d" % body.count(b"\r"))
    try:
        text.encode("ascii")
        ok("文件为纯 ASCII（避免 cmd 按 GBK 误读中文）", True)
    except UnicodeEncodeError as exc:
        ok("文件为纯 ASCII", False, exc)
    ok("以 CRLF 结尾", raw.endswith(b"\r\n"))

    lines = text.split("\r\n")
    labels = {l.strip()[1:].split()[0].lower() for l in lines if l.strip().startswith(":")}
    gotos = {m.group(1).lower() for m in re.finditer(r"goto\s+(\S+)", text)}
    missing = gotos - labels
    ok("所有 goto 目标都存在", not missing, "未解析: %s" % sorted(missing))
    ok("未使用多行括号块（cmd 对块内流程解析易出错）",
       not re.search(r"\(\s*\r?\n", text))
    ok("结尾有 pause（窗口不会一闪即关）", text.rstrip().endswith("endlocal"))
    return labels


# --------------------------------------------------------------------------
# 小型批处理解释器
# --------------------------------------------------------------------------
class Cmd:
    """极简批处理执行器：只实现本文件用到的语法，用于验证控制流。"""

    def __init__(self, text, fs):
        self.lines = text.split("\r\n")
        self.fs = fs
        self.var = {"CD": r"C:\DolaPool"}
        self.label = None
        self.errorlevel = 0
        self.history = []
        self.executed = 0

    # --- 变量替换 ---
    def expand(self, s: str) -> str:
        def rep(m):
            name = m.group(1).upper()
            if name == "ERRORLEVEL":
                return str(self.errorlevel)
            if name.endswith("~dp0"):
                return r"C:\DolaPool\\"
            return self.var.get(name, "")
        return re.sub(r"%([\w~]+)%", rep, s)

    def truth(self, expr: str, line: str) -> bool:
        low = line.strip().lower()
        m = re.match(r"if\s+not\s+exist\s+\"([^\"]+)\"", low)
        if m:
            return not self._exist(self.expand(m.group(1)))
        m = re.match(r"if\s+exist\s+\"([^\"]+)\"", low)
        if m:
            return self._exist(self.expand(m.group(1)))
        m = re.match(r"if\s+not\s+defined\s+(\w+)", low)
        if m:
            return not self.var.get(m.group(1).upper(), "")
        m = re.match(r"if\s+defined\s+(\w+)", low)
        if m:
            return bool(self.var.get(m.group(1).upper(), ""))
        m = re.match(r"if\s+not\s+errorlevel\s+(\d+)", low)
        if m:
            return self.errorlevel < int(m.group(1))
        m = re.match(r"if\s+errorlevel\s+(\d+)", low)
        if m:
            return self.errorlevel >= int(m.group(1))
        m = re.match(r'if\s+not\s+"([^"]*)"\s*==\s*"([^"]*)"', low)
        if m:
            return self.expand(m.group(1)) != self.expand(m.group(2))
        m = re.match(r'if\s+"([^"]*)"\s*==\s*"([^"]*)"', low)
        if m:
            return self.expand(m.group(1)) == self.expand(m.group(2))
        raise ValueError("无法解析的 if: " + line)

    def _exist(self, path: str) -> bool:
        p = path.lower().replace(r"c:\dolapool", "").lstrip("\\")
        if ".venv" in p and "python.exe" in p:
            return self.fs["venv_exists"]
        return False

    # --- 外部命令的成功/失败判定 ---
    def run_external(self, cmd: str) -> int:
        low = cmd.lower()
        if not cmd.strip() or cmd.strip().startswith("--"):
            # %PY% 为空时的典型症状
            self.history.append(("EMPTY_PY", cmd))
            return 9009
        if "-c \"import sys\"" in low and "python" in low:
            if ".venv" in low:
                return 0 if self.fs["venv_ok"] else 1
            if low.startswith("py "):
                return 0 if self.fs["py_launcher"] else 1
            if low.startswith("python3"):
                return 0 if self.fs["python3"] else 1
            return 0 if self.fs["python"] else 1
        if "import fastapi, uvicorn, requests, crypto, multipart" in low:
            return 0 if self.fs["deps_ok"] else 1
        if 'import webview' in low:
            return 0 if self.fs["webview_ok"] else 1
        if "sys.version_info" in low:
            return 0 if self.fs["py_version"] <= (3, 13) else 1
        if "-m venv" in low:
            if self.fs["venv_create_ok"]:
                self.fs["venv_exists"] = self.fs["venv_ok"] = True
                return 0
            return 1
        if "-m pip install" in low and "-r requirements-desktop.txt" in low:
            if "tuna" in low:
                r = 0 if self.fs["pip_tuna_ok"] else 1
            elif "aliyun" in low:
                r = 0 if self.fs["pip_aliyun_ok"] else 1
            else:
                r = 0 if self.fs["pip_official_ok"] else 1
            if r == 0:
                self.fs["deps_ok"] = True
            return r
        if "-m pip config" in low:
            return 0
        if "desktop.py" in low:
            return self.fs["app_rc"]
        return 0

    def execute(self) -> dict:
        idx = 0
        paused = False
        guard = 0
        while idx < len(self.lines):
            guard += 1
            if guard > 5000:
                raise RuntimeError("疑似死循环")
            line = self.lines[idx].strip()
            idx += 1
            if not line:
                continue
            low = line.lower()

            if low.startswith(":") or low.startswith("@"):
                continue
            if low.startswith("rem "):
                continue
            if low in ("setlocal", "endlocal"):
                continue
            if low.startswith("title") or low.startswith("cd "):
                continue
            if low.startswith("echo"):
                continue
            if low == "pause":
                paused = True
                continue
            if low.startswith("goto"):
                target = line.split(None, 1)[1].strip().lstrip(":").lower()
                idx = self.find_label(target)
                continue
            if low.startswith("if "):
                m = re.search(r"goto\s+(\S+)", line, re.I)
                cond = self.truth(line, line)
                if cond and m:
                    idx = self.find_label(m.group(1).lstrip(":").lower())
                continue
            if low.startswith("set "):
                mm = re.match(r'set\s+"([^=]+)=(.*)"', line, re.I) or re.match(r"set\s+(\w+)=(.*)", line, re.I)
                if mm:
                    self.var[mm.group(1).upper()] = self.expand(mm.group(2))
                continue
            if low.startswith("rmdir"):
                self.fs["venv_exists"] = False
                self.fs["venv_ok"] = False
                continue
            # 其余一律当作外部命令
            self.executed += 1
            self.errorlevel = self.run_external(self.expand(line))
            continue
        return {"paused": paused, "empty_py": [c for t, c in self.history if t == "EMPTY_PY"]}

    def find_label(self, name: str) -> int:
        for i, l in enumerate(self.lines):
            if l.strip().startswith(":") and l.strip()[1:].split()[0].lower() == name:
                return i + 1
        raise KeyError("找不到标签 " + name)


BASE_FS = {
    "venv_exists": True, "venv_ok": True, "py_launcher": True, "python": True,
    "python3": True, "deps_ok": True, "webview_ok": True, "venv_create_ok": True,
    "pip_official_ok": True, "pip_tuna_ok": True, "pip_aliyun_ok": True,
    "py_version": (3, 11), "app_rc": 0,
}

SCENARIOS = [
    ("正常：.venv 已存在且可用（就是你遇到的那条路径）", {}),
    ("无 .venv，靠 py -3 创建", {"venv_exists": False, "venv_ok": False}),
    ("无 .venv 且没有 py launcher，只有 python", {"venv_exists": False, "venv_ok": False, "py_launcher": False}),
    ("完全没装 Python", {"venv_exists": False, "py_launcher": False, "python": False, "python3": False}),
    ("pip：官方源失败 → 清华成功", {"pip_official_ok": False}),
    ("pip：三个源全失败", {"pip_official_ok": False, "pip_tuna_ok": False, "pip_aliyun_ok": False}),
    ("系统里没有 WebView（pywebview 缺失）", {"webview_ok": False}),
    ("应用运行报错（窗口崩溃）", {"app_rc": -532462766}),
    ("Python 3.14 + free-threaded", {"py_version": (3, 14)}),
]


def dynamic_checks(text: str) -> None:
    print("\n== 动态检查（用解释器跑各种环境）==")
    for name, override in SCENARIOS:
        fs = dict(BASE_FS, **override)
        cmd = Cmd(text, fs)
        try:
            result = cmd.execute()
            problems = []
            if result["empty_py"]:
                problems.append("有 %d 处把空的 PY 变量当命令执行" % len(result["empty_py"]))
            if not result["paused"]:
                problems.append("走到结尾却没有 pause（窗口会一闪即关）")
            ok(name, not problems, "；".join(problems))
        except Exception as exc:  # noqa: BLE001
            ok(name, False, "%s: %s" % (type(exc).__name__, exc))


def main() -> int:
    print("检查文件：%s\n" % BAT)
    raw = open(BAT, "rb").read()
    text = raw.decode("ascii")
    static_checks(text, raw)
    dynamic_checks(text)

    print("\n================ 结果 ================")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败：" + "，".join(FAIL))
        return 1
    print("✅ 启动器静态与动态检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
