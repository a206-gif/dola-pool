/* 哆啦助手 · 扩展内核（页面侧）
 *
 * 这段脚本由桌面版通过 CDP 注入到「带扩展的账号浏览器」里，在 dola.com 页面
 * 自己的环境里干活：
 *   1. 切到「视频生成」
 *   2. 打开扩展的 30 秒模式（window.setDola30s(true)）
 *   3. 选 Seedance 2.5 / 10-30 秒 / 比例
 *   4. 把提示词写进输入框，点发送
 *   5. 旁路记录 /chat/completion 的 conversation_id，交给主程序去轮询下载
 *
 * 为什么要绕浏览器：Dola 的提交 URL 带 a_bogus/msToken 签名，改参数会触发
 * 710022002；2.5/30 秒的入口又是扩展在 document_start 改写能力配置后才有的。
 * 所以「让页面自己发」是唯一稳的做法 —— 生成核心一行不用改。
 *
 * 约束：只用 DOM + 原生事件，不依赖任何第三方库；所有函数都返回普通对象，
 * 方便 Python 侧 returnByValue 直接取。
 */
(function () {
  "use strict";
  if (window.__dolaPoolKernel) return;

  var K = {
    v: 1,
    installedAt: Date.now(),
    completions: [],      // [{id, url, at, text}]
    requests: [],         // 最近的 /chat/completion 请求（只记时间/URL）
    chain: null,          // 最近一次带 video_model 的 /im/chain/single 原文
    chainCount: 0,
    wantChain: false,
    lastSubmitAt: 0,
    lastError: "",
    steps: []
  };
  window.__dolaPoolKernel = K;

  // ---------------------------------------------------------------- 工具
  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function textOf(el) {
    if (!el) return "";
    return String(el.innerText || el.textContent || el.getAttribute && el.getAttribute("aria-label") || "")
      .replace(/\s+/g, " ").trim();
  }

  function visible(el) {
    try {
      if (!el || !el.isConnected) return false;
      var r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) return false;
      var s = getComputedStyle(el);
      return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity || 1) > 0.01;
    } catch (e) { return false; }
  }

  function inPanel(el) {
    try { return !!(el && el.closest && el.closest("#watermark-free-media-panel")); } catch (e) { return false; }
  }

  function step(name, ok, detail) {
    K.steps.push({ name: name, ok: !!ok, detail: String(detail || ""), at: Date.now() });
    if (K.steps.length > 40) K.steps.shift();
    return ok;
  }

  function fireClick(el) {
    if (!el) return false;
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
    var r;
    try { r = el.getBoundingClientRect(); } catch (e) { r = { left: 0, top: 0, width: 1, height: 1 }; }
    var base = {
      bubbles: true, cancelable: true, composed: true, view: window,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
      button: 0, buttons: 1, detail: 1
    };
    try { el.dispatchEvent(new PointerEvent("pointerdown", Object.assign({}, base, { pointerId: 1, pointerType: "mouse", isPrimary: true }))); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("mousedown", base)); } catch (e) {}
    try { el.dispatchEvent(new PointerEvent("pointerup", Object.assign({}, base, { pointerId: 1, pointerType: "mouse", isPrimary: true, buttons: 0 }))); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("mouseup", Object.assign({}, base, { buttons: 0 }))); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("click", Object.assign({}, base, { buttons: 0 }))); } catch (e) {}
    return true;
  }

  function menuRoots() {
    var sel = '[role="menu"],[role="listbox"],[data-radix-popper-content-wrapper],[data-slot*="dropdown-menu"],[data-state="open"][aria-haspopup],[data-popper-placement]';
    var out = [];
    var all = document.querySelectorAll(sel);
    for (var i = 0; i < all.length; i++) {
      if (visible(all[i]) && !inPanel(all[i])) out.push(all[i]);
    }
    // 有些菜单只有 ul/div，没有 role：取「含有 ≥2 个可点项、浮在输入栏上方」的容器
    if (!out.length) {
      var cands = document.querySelectorAll("body div, body ul");
      for (var j = 0; j < cands.length; j++) {
        var el = cands[j];
        if (!visible(el) || inPanel(el)) continue;
        var r = el.getBoundingClientRect();
        if (r.height < 40 || r.height > 600 || r.width < 80) continue;
        if (el.querySelectorAll('[role="menuitem"],[role="option"],li,button').length < 2) continue;
        out.push(el);
      }
    }
    return out;
  }

  function pickInMenus(matcher, before) {
    var roots = menuRoots();
    var best = null, bestLen = 1e9;
    for (var i = 0; i < roots.length; i++) {
      var root = roots[i];
      if (before && before.length && before.indexOf(root) >= 0) continue;
      var nodes = root.querySelectorAll("div,li,button,span,[role='menuitem'],[role='option'],[role='radio']");
      for (var j = 0; j < nodes.length; j++) {
        var el = nodes[j];
        if (!visible(el)) continue;
        var t = textOf(el);
        if (!t || t.length > 80) continue;
        if (!matcher(t, el)) continue;
        if (t.length < bestLen) { best = el; bestLen = t.length; }
      }
    }
    return best;
  }

  function clickableOf(el) {
    if (!el) return null;
    try {
      return el.closest('[role="menuitem"],[role="option"],[role="radio"],button,a,li') || el;
    } catch (e) { return el; }
  }

  function norm(s) {
    return String(s || "").replace(/\s+/g, "").replace(/[✓✔√]/g, "").trim();
  }

  async function waitFor(fn, timeout, interval) {
    var end = Date.now() + (timeout || 5000);
    var last = null;
    while (Date.now() < end) {
      try { last = fn(); } catch (e) { last = null; }
      if (last) return last;
      await sleep(interval || 150);
    }
    return null;
  }

  // ------------------------------------------------------- 能力注入（30 秒）
  function enableThirty() {
    var api = typeof window.setDola30s === "function";
    try {
      if (api) window.setDola30s(true);
      else {
        localStorage.setItem("intl_doubao_enable_30s_v1", "1");
        localStorage.setItem("intl_doubao_target_duration_v1", "30");
      }
    } catch (e) { /* 隐私模式 */ }
    var on = false;
    try {
      var raw = typeof window.getDola30sStatus === "function" ? window.getDola30sStatus() : "";
      on = !!JSON.parse(raw || "{}").on;
    } catch (e) { on = false; }
    K.thirty = { api: api, on: on };
    return K.thirty;
  }

  // ------------------------------------------------------------ 元素定位
  var MODEL_SEL = '[data-input-engine-actionbar-control-key="video-model"]';
  var DUR_SEL = '[data-input-engine-actionbar-control-key="video-duration"]';

  function modelTrigger() {
    var el = document.querySelector(MODEL_SEL);
    return el && visible(el) ? el : null;
  }

  function durationTrigger() {
    var el = document.querySelector(DUR_SEL);
    return el && visible(el) ? el : null;
  }

  function ratioTrigger() {
    var keys = ["video-ratio", "ratio", "aspect-ratio", "video-size", "size"];
    for (var i = 0; i < keys.length; i++) {
      var el = document.querySelector('[data-input-engine-actionbar-control-key="' + keys[i] + '"]');
      if (el && visible(el)) return el;
    }
    var all = document.querySelectorAll("[data-input-engine-actionbar-control-key]");
    for (var j = 0; j < all.length; j++) {
      var key = String(all[j].getAttribute("data-input-engine-actionbar-control-key") || "");
      if (/(ratio|aspect|size)/i.test(key) && visible(all[j])) return all[j];
    }
    // 有的版本比例不是独立 key：工具栏里直接显示「9:16」的按钮就是它
    for (var k = 0; k < all.length; k++) {
      if (!visible(all[k])) continue;
      if (/^\d{1,2}:\d{1,2}$/.test(norm(textOf(all[k])))) return all[k];
    }
    return null;
  }

  function findAddButton() {
    var keys = ["add", "plus", "more", "skill", "mode"];
    for (var i = 0; i < keys.length; i++) {
      var el = document.querySelector('[data-input-engine-actionbar-control-key="' + keys[i] + '"]');
      if (el && visible(el)) return el;
    }
    var buttons = document.querySelectorAll("button,[role='button']");
    for (var j = 0; j < buttons.length; j++) {
      var b = buttons[j];
      if (!visible(b) || inPanel(b)) continue;
      var label = textOf(b) || b.getAttribute("aria-label") || b.getAttribute("title") || "";
      // 「+」才是技能入口；带「附件/上传/图片」的按钮点下去会弹系统文件框，必须先排除
      if (/附件|上传|文件|图片|照片/.test(label)) continue;
      if (/添加附件|添加|更多|技能|技能包|add/i.test(label)) return b;
      if (label) continue;
      var paths = b.querySelectorAll("svg path");
      var d = "";
      for (var k = 0; k < paths.length; k++) d += " " + (paths[k].getAttribute("d") || "");
      if (/V10\.9951H2|V11H3|M12(?:\.0005)?\s+2\.(?:25|05)/.test(d)) return b;
    }
    return null;
  }

  function findByText(re) {
    var all = document.querySelectorAll("button,[role='button'],[role='tab'],[role='menuitem'],[role='option'],li,a,div,span");
    var cands = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!visible(el) || inPanel(el)) continue;
      var t = textOf(el);
      if (!t || t.length > 40) continue;
      if (!re.test(t)) continue;
      if (el.querySelector("input,textarea,[contenteditable='true']")) continue;
      cands.push(el);
    }
    // 取「最里层」的那个：菜单容器和菜单项文本一样，点容器是没反应的
    for (var j = 0; j < cands.length; j++) {
      var nested = false;
      for (var k = 0; k < cands.length; k++) {
        if (j !== k && cands[j].contains(cands[k])) { nested = true; break; }
      }
      if (!nested) return cands[j];
    }
    return null;
  }

  async function enterVideoMode(timeout) {
    if (modelTrigger()) return { ok: true, how: "already" };
    var tab = findByText(/^视频生成$|^视频创作$|视频生成/);
    if (tab) {
      fireClick(clickableOf(tab));
      if (await waitFor(modelTrigger, 4000)) return { ok: true, how: "tab" };
    }
    for (var attempt = 0; attempt < 3; attempt++) {
      var add = findAddButton();
      if (add) {
        fireClick(add);
        await sleep(600);
        var item = findByText(/视频生成|视频创作/);
        if (item) {
          fireClick(clickableOf(item));
          if (await waitFor(modelTrigger, Math.max(2500, (timeout || 12000) / 3))) {
            return { ok: true, how: "add-menu" };
          }
        }
      }
      // 点错了会留下一个弹层：发个 Escape，别让它挡住下一轮查找
      try {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
        document.body.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
      } catch (e) {}
      await sleep(400);
    }
    return { ok: false, error: "没找到「视频生成」入口（页面结构可能变了）" };
  }

  async function pickOption(trigger, matcher, label, timeout) {
    if (!trigger) return { ok: false, error: "找不到" + label + "控件" };
    var cur = textOf(trigger);
    if (matcher(cur, trigger)) return { ok: true, skipped: true, text: cur };
    var before = menuRoots();
    fireClick(trigger);
    var target = await waitFor(function () {
      return pickInMenus(matcher, before) || pickInMenus(matcher, null);
    }, timeout || 3500, 120);
    if (!target) {
      try { document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); } catch (e) {}
      return { ok: false, error: label + "菜单里没有目标选项", current: cur, options: menuTexts() };
    }
    fireClick(clickableOf(target));
    await sleep(350);
    var after = textOf(trigger);
    var ok = matcher(after, trigger);
    return { ok: ok, text: after, picked: textOf(target) };
  }

  function menuTexts() {
    var out = [];
    var roots = menuRoots();
    for (var i = 0; i < roots.length; i++) {
      var items = roots[i].querySelectorAll('[role="menuitem"],[role="option"],li,button');
      for (var j = 0; j < items.length && out.length < 12; j++) {
        var t = textOf(items[j]);
        if (t && t.length <= 40) out.push(t);
      }
    }
    return out;
  }

  function modelMatcher(kind) {
    var re = kind === "2.5" ? /2\.5|seedance[^\d]*2[^\d]*5/i : /2\.0|seedance[^\d]*2[^\d]*0|fast/i;
    return function (t) { return re.test(norm(t)); };
  }

  function durationMatcher(seconds) {
    var n = String(seconds);
    return function (t) {
      var s = norm(t);
      return s === n + "s" || s === n + "秒" || s === n;
    };
  }

  function ratioMatcher(ratio) {
    return function (t) { return norm(t) === norm(ratio); };
  }

  // ------------------------------------------------------------- 提示词输入
  function editorCandidates() {
    var all = document.querySelectorAll("textarea,input[type='text'],[contenteditable='true'],[role='textbox']");
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!visible(el) || inPanel(el)) continue;
      if (el.disabled || el.readOnly) continue;
      if (el.closest("[role='menu'],[role='dialog']")) continue;
      out.push(el);
    }
    return out;
  }

  function editorText(el) {
    if (!el) return "";
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") return String(el.value || "");
    return String(el.innerText || el.textContent || "");
  }

  function findEditor() {
    var list = editorCandidates();
    if (!list.length) return null;
    var anchor = modelTrigger() || durationTrigger();
    if (!anchor) return list[list.length - 1];
    var ar = anchor.getBoundingClientRect();
    var best = null, bestScore = 1e18;
    for (var i = 0; i < list.length; i++) {
      var r = list[i].getBoundingClientRect();
      // 优先「在工具栏左侧/上方、距离最近」的输入框
      var dx = Math.max(0, ar.left - r.right);
      var dy = Math.abs((r.top + r.bottom) / 2 - (ar.top + ar.bottom) / 2);
      var score = dx + dy * 3 + (editorText(list[i]).trim() ? 0 : 500);
      if (score < bestScore) { bestScore = score; best = list[i]; }
    }
    return best;
  }

  function writeNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: value }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function writeEditor(el, value) {
    if (!el) return false;
    try { el.focus(); } catch (e) {}
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      writeNativeValue(el, value);
      return true;
    }
    // contenteditable：先全选再用 execCommand，React 才能收到真实的 beforeinput
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      var ok = false;
      try {
        if (typeof document.execCommand === "function") ok = document.execCommand("insertText", false, value);
      } catch (e) { ok = false; }
      if (!ok || !editorText(el).trim()) {
        el.textContent = value;
        el.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: value }));
        el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: value }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return true;
    } catch (e) {
      K.lastError = "写提示词失败：" + (e && e.message || e);
      return false;
    }
  }

  function findSendButton(editor) {
    if (!editor) return null;
    var er = editor.getBoundingClientRect();
    var candidates = [];
    var container = editor.parentElement;
    for (var depth = 0; container && depth < 14; depth++, container = container.parentElement) {
      var buttons = container.querySelectorAll("button,[role='button']");
      for (var i = 0; i < buttons.length; i++) {
        var b = buttons[i];
        if (!visible(b) || b.disabled || editor.contains(b) || inPanel(b)) continue;
        if (b.hasAttribute("data-input-engine-actionbar-control-key") || b.closest("[data-input-engine-actionbar-control-key]")) continue;
        var label = textOf(b) || b.getAttribute("aria-label") || b.getAttribute("title") || "";
        if (/选择?(?:模型|比例|时长)|模型切换|时长切换|seedance/i.test(label)) continue;
        if (/^(\d+)\s*(s|秒)$/i.test(label)) continue;
        var r = b.getBoundingClientRect();
        var near = r.left >= er.right - 200 && r.right <= er.right + 300 &&
                   r.top >= er.top - 100 && r.bottom <= er.bottom + 200;
        if (!near) continue;
        var strong = b.type === "submit" || /发送|提交|生成|send|submit/i.test(label);
        var score = (strong ? 0 : 1000) + Math.abs((r.top + r.bottom) / 2 - (er.top + er.bottom) / 2) +
                    Math.abs(r.right - er.right);
        if (b.querySelector("svg") || strong) candidates.push({ el: b, score: score });
      }
      if (candidates.some(function (c) { return c.score < 1000; })) break;
    }
    if (!candidates.length) return null;
    candidates.sort(function (a, b) { return a.score - b.score; });
    return candidates[0].el;
  }

  // ------------------------------------------------------- 网络旁路（只读）
  function pushCompletion(id, url, text) {
    for (var i = 0; i < K.completions.length; i++) {
      if (K.completions[i].id === id) return;
    }
    K.completions.push({ id: id, url: String(url || ""), at: Date.now(), text: String(text || "").slice(0, 4000) });
    if (K.completions.length > 5) K.completions.shift();
  }

  function inspectCompletion(response, url) {
    try {
      var clone = response.clone();
      if (!clone.body || !clone.body.getReader) {
        clone.text().then(function (t) { scanCompletionText(t, url); }).catch(function () {});
        return;
      }
      var reader = clone.body.getReader();
      var dec = new TextDecoder("utf-8");
      var buf = "";
      var pump = function () {
        reader.read().then(function (r) {
          if (r.done) { scanCompletionText(buf, url); return; }
          buf += dec.decode(r.value, { stream: true });
          if (scanCompletionText(buf, url) || buf.length > 200000) {
            try { reader.cancel(); } catch (e) {}
            return;
          }
          pump();
        }).catch(function () {});
      };
      pump();
    } catch (e) {}
  }

  function scanCompletionText(text, url) {
    if (!text) return false;
    if (!K.lastCompletionText) K.lastCompletionText = String(text).slice(0, 3000);
    var m = String(text).match(/"conversation_id"\s*:\s*"?(\d{6,})"?/);
    if (m) {
      pushCompletion(m[1], url, text);
      return true;
    }
    if (/STREAM_ERROR|只支持|不支持|风险|频繁/i.test(text)) {
      K.lastCompletionText = String(text).slice(0, 3000);
    }
    return false;
  }

  function inspectChain(response, url) {
    if (!K.wantChain) return;
    try {
      var clone = response.clone();
      clone.text().then(function (t) {
        if (!t || t.indexOf("video_model") < 0) return;
        K.chainCount += 1;
        K.chain = { text: t.slice(0, 2000000), at: Date.now(), url: String(url || "") };
      }).catch(function () {});
    } catch (e) {}
  }

  (function installHooks() {
    if (K.hooksInstalled) return;
    K.hooksInstalled = true;
    var origFetch = window.fetch;
    if (typeof origFetch === "function") {
      window.fetch = function (input, init) {
        var url = "";
        try { url = typeof input === "string" ? input : (input && input.url) || ""; } catch (e) {}
        var p = origFetch.apply(this, arguments);
        try {
          if (url.indexOf("/chat/completion") >= 0) {
            K.lastSubmitAt = Date.now();
            K.requests.push({ url: url.slice(0, 300), at: Date.now() });
            if (K.requests.length > 8) K.requests.shift();
            if (K.wantCompletion !== false) p.then(function (r) { inspectCompletion(r, url); }).catch(function () {});
          } else if (url.indexOf("/im/chain/single") >= 0) {
            inspectChain(p, url);
          }
        } catch (e) {}
        return p;
      };
    }
    var xhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      try {
        var u = String(url || "");
        if (u.indexOf("/chat/completion") >= 0) {
          K.lastSubmitAt = Date.now();
          var self = this;
          var scan = function () {
            try {
              if (self.responseType && self.responseType !== "text") return;
              scanCompletionText(self.responseText || "", u);
            } catch (e) {}
          };
          this.addEventListener("progress", scan);
          this.addEventListener("readystatechange", function () {
            if (self.readyState >= 3) scan();
          });
        }
        if (u.indexOf("/im/chain/single") >= 0 && K.wantChain) {
          this.addEventListener("load", function () {
            try {
              var t = this.responseText || "";
              if (t.indexOf("video_model") >= 0) {
                K.chainCount += 1;
                K.chain = { text: t.slice(0, 2000000), at: Date.now(), url: u };
              }
            } catch (e) {}
          });
        }
      } catch (e) {}
      return xhrOpen.apply(this, arguments);
    };
  })();

  // --------------------------------------------------------------- 对外 API
  function status() {
    var m = modelTrigger(), d = durationTrigger(), r = ratioTrigger();
    var ed = findEditor();
    return {
      ok: true,
      href: String(location.href),
      title: String(document.title || ""),
      ready: document.readyState,
      inVideo: !!m,
      model: m ? textOf(m) : "",
      duration: d ? textOf(d) : "",
      ratio: r ? textOf(r) : "",
      prompt: ed ? editorText(ed).slice(0, 80) : "",
      body: document.body ? textOf(document.body).slice(0, 300) : "",
      thirty: K.thirty || null,
      sent: !!K.lastSubmitAt,
      completions: K.completions.slice(-3),
      lastCompletionText: K.lastCompletionText || "",
      chain: K.chain ? { length: K.chain.text.length, at: K.chain.at } : null,
      steps: K.steps.slice(-12),
      lastError: K.lastError || ""
    };
  }

  async function prepare(opts) {
    opts = opts || {};
    K.steps = [];
    K.lastError = "";
    var out = { ok: false, steps: [], error: "" };
    try {
      if (opts.reset) { K.completions = []; K.lastCompletionText = ""; K.lastSubmitAt = 0; K.steps = []; }
      var mode = await enterVideoMode(opts.modeTimeout || 12000);
      step("视频生成模式", mode.ok, mode.how || mode.error);
      if (!mode.ok) throw new Error(mode.error || "进不了视频生成模式");

      var need30 = opts.duration && Number(opts.duration) > 15;
      if (need30 || opts.force30 !== false) {
        // 扩展的 30 秒开关：等 duration-main.js（document_idle）把 API 挂上
        await waitFor(function () { return typeof window.setDola30s === "function"; }, opts.extTimeout || 8000, 200);
        var th = enableThirty();
        step("30 秒开关", th.api, th.api ? ("on=" + th.on) : "扩展没加载，改成 localStorage 兜底");
        await sleep(500);
      }

      await waitFor(modelTrigger, opts.uiTimeout || 8000, 150);
      var mt = modelTrigger(), dt = durationTrigger();
      if (!mt) throw new Error("视频生成模式下没出现模型选择器");
      step("模型控件", true, textOf(mt));
      step("时长控件", !!dt, dt ? textOf(dt) : "找不到");

      var model = String(opts.model || "2.5");
      var kind = /2\.5/.test(model) ? "2.5" : "2.0";
      var pm = await pickOption(mt, modelMatcher(kind), "模型", opts.menuTimeout || 4000);
      step("选择模型 " + kind, pm.ok, pm.ok ? (pm.text || pm.picked) : (pm.error + " 现有：" + (pm.options || []).join("/")));
      if (!pm.ok) throw new Error(pm.error || "选不到模型 " + kind);

      if (opts.duration) {
        var dd = durationTrigger();
        var pd = await pickOption(dd, durationMatcher(opts.duration), "时长", opts.menuTimeout || 4000);
        step("选择时长 " + opts.duration, pd.ok, pd.ok ? (pd.text || pd.picked) : (pd.error + " 现有：" + (pd.options || []).join("/")));
        if (!pd.ok && Number(opts.duration) >= 30) {
          throw new Error("时长菜单里没有 30s —— 说明扩展的 30 秒能力没生效（页面不是带扩展打开 / 扩展被禁用）");
        }
      }

      if (opts.ratio) {
        var rt = ratioTrigger();
        if (rt) {
          var pr = await pickOption(rt, ratioMatcher(opts.ratio), "比例", opts.menuTimeout || 3000);
          step("选择比例 " + opts.ratio, pr.ok, pr.ok ? (pr.text || pr.picked) : pr.error);
        } else {
          step("选择比例 " + opts.ratio, true, "页面没有独立的比例控件，沿用默认");
        }
      }

      out.ok = true;
      out.status = status();
    } catch (e) {
      K.lastError = String(e && e.message || e);
      out.error = K.lastError;
    }
    out.steps = K.steps.slice(-12);
    return out;
  }

  async function submit(prompt) {
    var out = { ok: false, error: "" };
    try {
      var ed = findEditor();
      if (!ed) throw new Error("找不到提示词输入框");
      var wrote = writeEditor(ed, String(prompt || ""));
      step("写入提示词", wrote, (prompt || "").slice(0, 60));
      if (!wrote) throw new Error("写提示词失败");
      await sleep(300);
      var btn = findSendButton(ed);
      if (!btn) throw new Error("找不到发送按钮");
      step("发送按钮", true, textOf(btn) || btn.getAttribute("aria-label") || "图标按钮");
      K.wantChain = true;
      K.wantCompletion = true;
      K.lastSubmitAt = 0;
      K.completions = [];
      K.lastCompletionText = "";
      fireClick(btn);
      // 等请求真的发出去（fetch 钩子记录时间，或输入框被清空）
      var started = await waitFor(function () {
        return K.lastSubmitAt || !editorText(ed).trim();
      }, 8000, 150);
      out.ok = !!started;
      if (!started) out.error = "点了发送，但没看到 /chat/completion 请求";
      out.status = status();
    } catch (e) {
      K.lastError = String(e && e.message || e);
      out.error = K.lastError;
    }
    out.steps = K.steps.slice(-12);
    return out;
  }

  async function waitConversation(timeoutMs) {
    var end = Date.now() + (timeoutMs || 120000);
    while (Date.now() < end) {
      if (K.completions.length) return { ok: true, id: K.completions[K.completions.length - 1].id, status: status() };
      await sleep(400);
    }
    return { ok: false, status: status() };
  }

  function latestChain() {
    return K.chain ? { ok: true, text: K.chain.text } : { ok: false };
  }

  function reset() {
    K.completions = [];
    K.lastCompletionText = "";
    K.lastSubmitAt = 0;
    K.steps = [];
    K.lastError = "";
    K.chain = null;
    K.chainCount = 0;
    return { ok: true };
  }

  K.status = status;
  K.prepare = prepare;
  K.submit = submit;
  K.waitConversation = waitConversation;
  K.latestChain = latestChain;
  K.reset = reset;
  K.enableThirty = enableThirty;
  K.enterVideoMode = enterVideoMode;
  K.version = 1;
})();
