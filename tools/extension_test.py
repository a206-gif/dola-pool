#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器扩展加载自检（不联网、不真的开浏览器）。

覆盖：
1) 能在 extensions/ 里找到带 manifest.json 的扩展（包括 zip 解压出来多套一层的情况）
2) 跳过 _ 开头 / 隐藏目录（WebView2 和 Chromium 都不认这种扩展名）
3) 启动参数里有 --load-extension / --disable-extensions-except / 独立 profile
4) 没有扩展时明确报错，不会瞎开浏览器
5) 真的能把进程拉起来（用假浏览器脚本记录收到的参数）

用法：.venv/bin/python tools/extension_test.py
"""
import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DOLA_DATA_DIR", tempfile.mkdtemp())

import desktop  # noqa: E402

OUT = []


def check(name, ok, detail=""):
    OUT.append((name, bool(ok), detail))


tmp = tempfile.mkdtemp()

# 1) 嵌套扩展（Extensions.zip → extensions/dola30/manifest.json）
deep = os.path.join(tmp, "Extensions", "dola30")
os.makedirs(deep, exist_ok=True)
with open(os.path.join(deep, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump({"manifest_version": 3, "name": "测试扩展", "version": "9.9.9"}, f)

# 2) 下划线目录应被跳过
skip = os.path.join(tmp, "_metadata")
os.makedirs(skip, exist_ok=True)
with open(os.path.join(skip, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump({"name": "不该出现"}, f)

found = desktop.discover_extensions([tmp])
check("能在多套一层的目录里找到扩展", len(found) == 1, str([f["path"] for f in found]))
check("读到 manifest 里的名字和版本",
      found and found[0]["name"] == "测试扩展" and found[0]["version"] == "9.9.9",
      str(found[:1]))
check("跳过 _ 开头的目录", all("_metadata" not in f["path"] for f in found), "")

# 3) 仓库自带的扩展能被识别
builtin = desktop.discover_extensions([os.path.join(HERE, "extensions")])
check("程序自带的 extensions/ 能被识别", len(builtin) >= 1, str([f["name"] for f in builtin]))

# 4) 启动参数
cmd = desktop.build_extension_browser_command("/x/msedge", "/tmp/p", ["/e/a", "/e/b"],
                                              "https://www.dola.com/chat/")
check("启动参数带 --load-extension", "--load-extension=/e/a,/e/b" in cmd, " ".join(cmd))
check("启动参数带 --disable-extensions-except", "--disable-extensions-except=/e/a,/e/b" in cmd, "")
check("用独立 profile（不碰用户自己的浏览器配置）", "--user-data-dir=/tmp/p" in cmd, "")
check("最后一项是要打开的地址", cmd[-1] == "https://www.dola.com/chat/", cmd[-1])

# 5) 没有扩展时不启动
res = desktop.launch_extension_browser("https://www.dola.com/chat/", roots=[os.path.join(tmp, "nothing")])
check("没有扩展时直接报错，不去开浏览器", res.get("ok") is False and res.get("error"), res.get("error", "")[:60])

# 6) 真拉起一个「假浏览器」，检查它收到的参数
fake = os.path.join(tmp, "fake-browser.sh")
argfile = os.path.join(tmp, "args.txt")
with open(fake, "w", encoding="utf-8") as f:
    f.write('#!/bin/sh\nprintf "%s\\n" "$@" > ' + argfile + "\n")
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
res = desktop.launch_extension_browser("https://www.dola.com/chat/", roots=[tmp], browser=fake)
for _ in range(40):
    if os.path.exists(argfile):
        break
    time.sleep(0.05)
got = open(argfile, encoding="utf-8").read().splitlines() if os.path.exists(argfile) else []
check("能真的把浏览器拉起来（这里用假脚本验证）", res.get("ok") is True, res.get("error", ""))
check("假浏览器收到的参数里有扩展目录",
      any(a.startswith("--load-extension=") and deep in a for a in got), " | ".join(got)[:120])
check("profile 目录被自动创建", os.path.isdir(desktop.extension_browser_profile()), "")

bad = 0
for name, ok, detail in OUT:
    if not ok:
        bad += 1
    print(("✅ " if ok else "❌ ") + name + (("  → " + detail[:110]) if detail else ""))
print("\n" + ("✅ 扩展加载自检全部通过" if not bad else "❌ 扩展加载自检 %d 项失败" % bad))
sys.exit(1 if bad else 0)
