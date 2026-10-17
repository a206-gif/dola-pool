/* Dola Pool · 账号网页「生成助手」小条（纯 JS，无 pywebview 桥）
 *
 * 作用：在 dola.com 页面右下角放一个小条，直接提供
 *   - 模型：Dreamina Seedance 2.5 / 2.0 Fast
 *   - 时长：10s / 15s / 30s
 *   - 「应用」：按页面上的文字点开站点自己的菜单并选中对应项（不拦截请求、不改接口）
 *
 * 为什么要这样：网页端的模型/时长菜单是站点自己的 React 组件，没有对外接口；
 * 我们只能模拟用户点击。菜单结构会变，所以每一步都有状态提示；找不到就明说，
 * 不假装成功。默认不自动执行，只有用户点「应用」或勾了「自动」才会动。
 *
 * 这个文件同时被 desktop.py 注入、被 tools/model_bar_test.js 在 jsdom 里测试。
 */
(function () {
  if (window.__DLP_HELPER__) return;
  var host = String(location.hostname || '');
  // 只认真站；本机代理页 / 诊断页 / about:blank 一律不注入
  if (!/(^|\.)dola\.com$/i.test(host)) return;
  window.__DLP_HELPER__ = true;

  var LS_AUTO = 'dlp_helper_auto';
  var LS_HIDE = 'dlp_helper_hide';
  var state = { model: '2.5', duration: '30', busy: false };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function visible(node) {
    if (!node || !node.getBoundingClientRect) return false;
    var r = node.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return false;
    var cs = window.getComputedStyle ? window.getComputedStyle(node) : null;
    if (cs && (cs.visibility === 'hidden' || cs.display === 'none' || (cs.opacity && Number(cs.opacity) === 0))) return false;
    return true;
  }

  // 找页面上「文字匹配、可见、且不是我们自己的条」的元素；文字越短越可能是按钮本身
  function findText(re, preferBottom) {
    var list = document.querySelectorAll('button,[role="button"],a,div,span,li');
    var best = null, bestLen = 1e9;
    var vh = window.innerHeight || 800;
    for (var i = 0; i < list.length; i++) {
      var node = list[i];
      if (node.closest && node.closest('#dlp-helper')) continue;
      var t = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t || t.length > 60) continue;
      if (!re.test(t)) continue;
      if (!visible(node)) continue;
      var r = node.getBoundingClientRect();
      var score = t.length;
      if (preferBottom && r.top < vh * 0.45) score += 50;   // 输入框在页面底部，优先下面
      if (score < bestLen) { best = node; bestLen = score; }
    }
    return best;
  }

  function tap(node) {
    if (!node) return false;
    try {
      var r = node.getBoundingClientRect();
      var x = r.left + r.width / 2, y = r.top + r.height / 2;
      var opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
      try { node.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch (e1) {}
      node.dispatchEvent(new MouseEvent('mousedown', opts));
      try { node.dispatchEvent(new PointerEvent('pointerup', opts)); } catch (e2) {}
      node.dispatchEvent(new MouseEvent('mouseup', opts));
      node.dispatchEvent(new MouseEvent('click', opts));
      return true;
    } catch (e) {
      try { node.click(); return true; } catch (e3) { return false; }
    }
  }

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function status(msg, bad) {
    var s = document.getElementById('dlp-helper-msg');
    if (!s) return;
    s.textContent = msg;
    s.style.color = bad ? '#ffb4a8' : '#bff5e8';
  }

  function paint() {
    var bar = document.getElementById('dlp-helper');
    if (!bar) return;
    var chips = bar.querySelectorAll('[data-m]');
    for (var i = 0; i < chips.length; i++) {
      var on = chips[i].getAttribute('data-m') === state.model;
      chips[i].style.background = on ? '#078d78' : '#1b2425';
      chips[i].style.color = on ? '#fff' : '#bff5e8';
      chips[i].style.borderColor = on ? '#078d78' : '#3a484b';
    }
    var ds = bar.querySelectorAll('[data-d]');
    for (var j = 0; j < ds.length; j++) {
      var on2 = ds[j].getAttribute('data-d') === state.duration;
      ds[j].style.background = on2 ? '#078d78' : '#1b2425';
      ds[j].style.color = on2 ? '#fff' : '#bff5e8';
      ds[j].style.borderColor = on2 ? '#078d78' : '#3a484b';
    }
  }

  // 站点菜单里的模型名可能是 Dreamina Seedance 2.5 / Seedance 2.5 / SD2.5
  function modelRe(name) {
    return name === '2.5' ? /(?:Dreamina\s*)?Seedance\s*2\.5|SD\s*2\.5/i
                          : /(?:Dreamina\s*)?Seedance\s*2\.0|SD\s*2\.0/i;
  }

  async function applyModel() {
    var want = state.model;
    var trigger = findText(/模型/, true) || findText(modelRe(want), true);
    if (!trigger) { status('没找到页面上的「模型」按钮：请手动点底部模型菜单', true); return false; }
    if (modelRe(want).test(String(trigger.innerText || ''))) {
      status('模型已经是 Seedance ' + want);
      return true;
    }
    tap(trigger);
    await wait(450);
    var item = findText(modelRe(want), false);
    if (!item) {
      // 菜单可能还没渲染完，再等一次
      await wait(500);
      item = findText(modelRe(want), false);
    }
    if (!item) {
      status('菜单里没找到 Seedance ' + want + '（可能是这个账号网页端没开放）→ 用本软件【生成】页选 2.5 / 30 秒', true);
      return false;
    }
    tap(item);
    await wait(350);
    status('已切到 Seedance ' + want);
    return true;
  }

  async function applyDuration() {
    var want = state.duration;
    var trigger = findText(/^\d+\s*s$/i, true) || findText(/时长|秒数/, true);
    if (!trigger) { status('没找到页面上的时长按钮：请手动点底部的 10s/30s', true); return false; }
    if (new RegExp('^' + want + '\\s*s$', 'i').test(String(trigger.innerText || '').trim())) {
      status('时长已经是 ' + want + ' 秒');
      return true;
    }
    tap(trigger);
    await wait(450);
    var item = findText(new RegExp('^' + want + '\\s*s$|^' + want + '\\s*秒$', 'i'), false);
    if (!item) { await wait(450); item = findText(new RegExp(want + '\\s*s\\b'), false); }
    if (!item) { status('时长菜单里没找到 ' + want + 's（2.5 才支持 30 秒）', true); return false; }
    tap(item);
    await wait(300);
    status('时长已设成 ' + want + ' 秒');
    return true;
  }

  async function apply() {
    if (state.busy) return false;
    state.busy = true;
    try {
      status('正在按页面菜单选择…');
      var m = await applyModel();
      var d = m ? await applyDuration() : false;
      if (m && d) status('✅ 已设成 Seedance ' + state.model + ' · ' + state.duration + ' 秒');
      return !!(m && d);
    } catch (e) {
      status('设置失败：' + ((e && e.message) || e), true);
      return false;
    } finally {
      state.busy = false;
    }
  }
  window.__dlpApply2530 = apply;   // 方便测试 / 控制台手动调用

  function build() {
    if (document.getElementById('dlp-helper')) return;
    var box = el('div');
    box.id = 'dlp-helper';
    box.setAttribute('style',
      'position:fixed;right:16px;bottom:74px;z-index:2147483646;width:236px;box-sizing:border-box;' +
      'background:rgba(12,22,20,.97);border:1px solid #1e6f61;border-radius:10px;padding:10px 12px;' +
      'box-shadow:0 12px 34px rgba(0,0,0,.45);color:#cfdcd9;' +
      'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;' +
      'text-align:left');

    var head = el('div');
    head.setAttribute('style', 'display:flex;align-items:center;gap:6px;margin-bottom:8px');
    var title = el('b', '', '⚡ 生成助手');
    title.setAttribute('style', 'font-size:12.5px;color:#fff;flex:1');
    var mini = el('button', '', '—');
    mini.title = '收起';
    mini.setAttribute('style', 'background:none;border:0;color:#7fa9a2;cursor:pointer;font-size:14px;padding:0 2px');
    mini.onclick = function () { box.style.display = 'none'; var m = document.getElementById('dlp-mini'); if (m) m.style.display = 'block'; };
    head.appendChild(title); head.appendChild(mini);

    function chip(label, attr, val) {
      var b = el('button', '', label);
      b.setAttribute(attr, val);
      b.setAttribute('style', 'border:1px solid #3a484b;border-radius:6px;padding:4px 7px;font-size:11.5px;' +
        'cursor:pointer;background:#1b2425;color:#bff5e8;margin:0 5px 5px 0');
      b.onclick = function () { state[attr === 'data-m' ? 'model' : 'duration'] = val; paint(); };
      return b;
    }

    var r1 = el('div'); r1.appendChild(el('span', '', '模型 '));
    r1.appendChild(chip('2.5', 'data-m', '2.5')); r1.appendChild(chip('2.0 Fast', 'data-m', '2.0'));
    var r2 = el('div'); r2.appendChild(el('span', '', '时长 '));
    r2['appendChild'](chip('10s', 'data-d', '10')); r2.appendChild(chip('15s', 'data-d', '15'));
    r2.appendChild(chip('30s', 'data-d', '30'));

    var r3 = el('div');
    r3.setAttribute('style', 'display:flex;align-items:center;gap:8px;margin-top:2px');
    var run = el('button', '', '应用');
    run.id = 'dlp-helper-apply';
    run.setAttribute('style', 'flex:1;background:#078d78;border:0;border-radius:6px;color:#fff;' +
      'font-weight:600;padding:6px 8px;font-size:12px;cursor:pointer');
    run.onclick = apply;
    var autoWrap = el('label');
    autoWrap.setAttribute('style', 'display:flex;align-items:center;gap:4px;color:#9aabad;font-size:11.5px;cursor:pointer');
    var auto = el('input');
    auto.type = 'checkbox';
    auto.id = 'dlp-helper-auto';
    try { auto.checked = localStorage.getItem(LS_AUTO) === '1'; } catch (e) {}
    auto.onchange = function () {
      try { localStorage.setItem(LS_AUTO, auto.checked ? '1' : '0'); } catch (e) {}
      if (auto.checked) apply();
    };
    autoWrap.appendChild(auto); autoWrap.appendChild(document.createTextNode('自动'));
    r3.appendChild(run); r3.appendChild(autoWrap);

    var msg = el('div', '', '选好后点「应用」；不改接口，只点站点自己的菜单');
    msg.id = 'dlp-helper-msg';
    msg.setAttribute('style', 'margin-top:7px;font-size:11px;line-height:1.5;color:#8fb7ae;word-break:break-word');

    box.appendChild(head); box.appendChild(r1); box.appendChild(r2); box.appendChild(r3); box.appendChild(msg);
    document.documentElement.appendChild(box);
    paint();

    // 收起后的恢复小标
    var miniBtn = el('div', '', '⚡ 2.5');
    miniBtn.id = 'dlp-mini';
    miniBtn.title = '展开生成助手';
    miniBtn.setAttribute('style', 'display:none;position:fixed;right:16px;bottom:74px;z-index:2147483646;' +
      'background:#078d78;color:#fff;border-radius:999px;padding:6px 12px;font:12px/1.4 sans-serif;' +
      'cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.4)');
    miniBtn.onclick = function () { miniBtn.style.display = 'none'; box.style.display = 'block'; };
    document.documentElement.appendChild(miniBtn);

    try {
      if (localStorage.getItem(LS_HIDE) === '1') { box.style.display = 'none'; miniBtn.style.display = 'block'; }
    } catch (e) {}

    if (auto.checked) setTimeout(apply, 1500);   // 自动模式：页面就绪后再试，避免点空
  }

  // 页面已经解析出 body 就直接建；否则等 DOMContentLoaded（build 自身幂等）
  if (document.body) build();
  else document.addEventListener('DOMContentLoaded', build);
})();
