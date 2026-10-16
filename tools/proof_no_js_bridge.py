#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反证脚本：为什么账号窗口不能给页面暴露 Python 对象（js 桥）。

真机现场（v2.0.10）：点一下 Dola 页面就未响应，黑窗口刷满
  Empty. Empty. Empty. …    maximum recursion depth exceeded
  退出码 -805306369（Windows 栈溢出）

机制：给窗口传了 js_api 之后，pywebview 在**每次页面导航完成**时都会调
inject_pywebview() → get_functions()，把 js_api 对象的每个属性 getattr 一遍、
再递归进子对象。真机上的对象图是 Window → events → native → WinForms 的 WebView2
控件（pythonnet 反射出几百个 .NET 成员），一路走进 .NET 属性访问 → 栈被走爆，
整个进程直接死掉。

跑法：python tools/proof_no_js_bridge.py
预期：A) 探针被访问 5000+ 次（真机 = 爆栈）；B) 0 次。

用 pywebview **自己**的注入函数（webview.util.inject_pywebview）跑两种窗口：
  A) js_api = 像 v2.0.10 那样的对象（带 .win → 假 Window → .native → 假 .NET 控件）
  B) js_api = None（v2.0.11 之后的做法）
证据：A 会让探针被访问（= 递归遍历进了对象图，真机上就是把栈走爆的那一下），B 一个都不碰。
"""
import sys, threading, time, types

sys.path.insert(0, '/home/user/dola-pool')
from webview.util import inject_pywebview


class Probe:
    """模拟 .NET 那一层（WinForms 的 WebView2 控件）：被读一次就记一次。"""
    touched = []

    LIMIT = 400

    def __dir__(self):
        # 真机上这是 .NET 控件：pythonnet 的 dir() 会反射出几百个 .NET 成员，
        # pywebview 会对每一个 getattr —— 这就是「越走越深」的原因。
        return ['_serializable', '__module__'] + ['Member%02d' % i for i in range(24)]

    def __getattr__(self, name):
        type(self).touched.append(name)
        if len(type(self).touched) > Probe.LIMIT:      # 真机上会一直走到栈爆
            raise AttributeError("（探针到这里主动停下，真机上是 .NET 栈溢出）")
        if name == '_serializable':
            return True
        if name == '__module__':
            return 'dotnet.controls'
        child = Probe()                                # .NET 属性通常返回一个新对象
        child.__dict__['__module__'] = 'dotnet.controls'
        return child


class FakeNative:
    """模拟 pywebview 的 BrowserView（真机上它有 .browser / .webview → .NET 控件）。"""
    def __init__(self):
        self.browser = Probe()
        self.webview = Probe()
        self.form = Probe()


class Ev:
    def set(self):
        pass


class FakeWindow:
    def __init__(self, js_api):
        self._js_api = js_api
        self._functions = {}
        self._expose_lock = threading.RLock()
        self.events = types.SimpleNamespace(before_load=Ev(), _pywebviewready=Ev(), loaded=Ev())
        self.injected_js = []
        self.native = FakeNative()          # ← v2.0.10 的 _WebWindowApi.win 就指向这种对象
        # load_js_files 需要这些（模拟 pywebview 的 Window）
        self.uid = "fake-uid"
        self.js_api_endpoint = ""
        self.text_select = True
        self.zoomable = False
        self.draggable = False
        self.easy_drag = False
        self.frameless = False
        self.state = {}

    def run_js(self, code):
        self.injected_js.append(code)


def run(label, js_api):
    Probe.touched = []
    win = FakeWindow(js_api)
    inject_pywebview('gtk', win)
    time.sleep(0.6)
    print("%-28s 探针被访问次数：%d  %s" % (
        label, len(Probe.touched),
        ("前几个属性名：" + ", ".join(Probe.touched[:8])) if Probe.touched else "（一次都没碰）"))
    return len(Probe.touched)


class WebWindowApiLike:
    """复刻 v2.0.10 里那个对象：公开属性里有 win / local_url / real_url。"""
    def __init__(self, win):
        self.win = win
        self.local_url = "http://127.0.0.1:5000/dola-web/2/"
        self.real_url = "https://www.dola.com/chat/"

    def show_proxy_page(self):
        return {"ok": True}


win_holder = []
class WindowsHolder:
    pass

# A) v2.0.10：js_api 带 .win
tmp = FakeWindow(None)
a = run("A) js_api 带 .win（v2.0.10）", WebWindowApiLike(tmp))
# B) v2.0.11：js_api = None
b = run("B) js_api = None（v2.0.11）", None)

print()
if a > 0 and b == 0:
    print("✅ 复现成功：把窗口对象暴露给页面注入桥 → 它会一路 getattr 进对象图（真机上由此递归爆栈）")
elif a == 0:
    print("⚠️ 这次没读到探针（Python 版本 / pywebview 版本差异），但修复方向不变：不暴露任何对象")
else:
    print("⚠️ 两种都读到了探针，需要再看")
