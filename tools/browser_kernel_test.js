#!/usr/bin/env node
/**
 * 扩展内核（static/browser_kernel.js）回归自检 —— 用 jsdom 搭一个「像 Dola 的页面」。
 *
 * 为什么要测：这段脚本要跑在用户的真浏览器里点模型/时长/发送，出错时报错信息
 * 必须能指出「是没进视频模式 / 没有 2.5 / 没有 30s / 没发出请求」中的哪一种，
 * 不能只丢一个「未知原因」。这里把四种情况都演一遍。
 *
 * 用法：NODE_PATH=/path/to/node_modules node tools/browser_kernel_test.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'static', 'browser_kernel.js'), 'utf8');

const FAKE_PAGE = `<!doctype html><html><body>
  <div id="app">
    <div id="bar">
      <button id="addBtn" aria-label="添加"><svg><path d="M12 2.25a1 1 0 0 0-1 1v8.75H2.25a1 1 0 0 0 0 2H11V22a1 1 0 0 0 2 0v-8.25h8.75a1 1 0 0 0 0-2H13V3.25a1 1 0 0 0-1-1z"></path></svg></button>
    </div>
    <div id="composer">
      <div id="editor" contenteditable="true" role="textbox"></div>
      <button id="sendBtn" type="submit">发送</button>
    </div>
  </div>
  <script>
  (function(){
    var state = { mode: 'chat', model: '', duration: '', ratio: '' };
    var extOn = false;
    function el(tag, attrs, text){
      var e = document.createElement(tag);
      Object.keys(attrs || {}).forEach(function(k){ e.setAttribute(k, attrs[k]); });
      if (text) e.textContent = text;
      return e;
    }
    function closeMenus(){ Array.from(document.querySelectorAll('[role="menu"]')).forEach(function(m){ m.remove(); }); }
    function menu(items){
      closeMenus();
      var box = el('div', { role: 'menu', 'data-popper-placement': 'top-start' });
      items.forEach(function(it){
        var row = el('div', { role: 'menuitem' }, it.label);
        row.addEventListener('click', function(){ it.pick(); closeMenus(); });
        box.appendChild(row);
      });
      document.body.appendChild(box);
      return box;
    }
    function renderBar(){
      if (state.mode !== 'video') return;
      if (document.querySelector('[data-input-engine-actionbar-control-key="video-model"]')) return;
      var wrap = el('div', { id: 'actionbar' });
      var model = el('button', { 'data-input-engine-actionbar-control-key': 'video-model' }, state.model || 'Seedance 2.0 Fast');
      var dur = el('button', { 'data-input-engine-actionbar-control-key': 'video-duration' }, state.duration || '10s');
      var ratio = el('button', { 'data-input-engine-actionbar-control-key': 'video-ratio' }, state.ratio || '16:9');
      model.addEventListener('click', function(){
        menu([
          { label: 'Dreamina Seedance 2.0 Fast', pick: function(){ state.model = 'Dreamina Seedance 2.0 Fast'; model.textContent = state.model; } }
        ].concat(window.__with25 ? [
          { label: 'Dreamina Seedance 2.5', pick: function(){ state.model = 'Dreamina Seedance 2.5'; model.textContent = state.model; } }
        ] : []));
      });
      dur.addEventListener('click', function(){
        var items = [
          { label: '10s', pick: function(){ state.duration = '10s'; dur.textContent = '10s'; } }
        ];
        if (window.__with30 && (extOn || localStorage.getItem('intl_doubao_enable_30s_v1') === '1')) {
          items.push({ label: '30s', pick: function(){ state.duration = '30s'; dur.textContent = '30s'; } });
        }
        menu(items);
      });
      ratio.addEventListener('click', function(){
        menu([
          { label: '9:16', pick: function(){ state.ratio = '9:16'; ratio.textContent = '9:16'; } },
          { label: '16:9', pick: function(){ state.ratio = '16:9'; ratio.textContent = '16:9'; } }
        ]);
      });
      wrap.appendChild(model); wrap.appendChild(dur); wrap.appendChild(ratio);
      document.getElementById('composer').prepend(wrap);
    }
    document.getElementById('addBtn').addEventListener('click', function(){
      menu([{ label: '视频生成', pick: function(){ state.mode = 'video'; renderBar(); } }]);
    });
    document.getElementById('sendBtn').addEventListener('click', function(){
      var text = document.getElementById('editor').textContent || '';
      if (!text.trim()) return;
      window.__sent = text;
      window.__timeline = state.model + '|' + state.duration + '|' + state.ratio;
      document.getElementById('editor').textContent = '';
      window.fetch('/chat/completion?aid=582465&a_bogus=fake');
    });
    window.__state = state;
    window.setDola30s = function(on){ extOn = !!on; localStorage.setItem('intl_doubao_enable_30s_v1', on ? '1' : '0'); return extOn; };
    window.getDola30sStatus = function(){ return JSON.stringify({ on: extOn, duration: 30, ready: true }); };
  })();
  </script>
</body></html>`;

function makeDom(opts) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + e.message));
  const dom = new JSDOM(FAKE_PAGE, {
    url: 'https://www.dola.com/chat/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      // jsdom 没有布局：给所有元素一个稳定的矩形，hidden/display:none 才算不可见
      w.Element.prototype.getBoundingClientRect = function () {
        const hidden = this.hidden || (this.style && this.style.display === 'none') || this.closest('[hidden]');
        return hidden
          ? { width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0, x: 0, y: 0 }
          : { width: 120, height: 32, left: 10, top: 10, right: 130, bottom: 42, x: 10, y: 10 };
      };
      w.TextDecoder = TextDecoder;
      w.__with25 = opts.with25 !== false;
      w.__with30 = opts.with30 !== false;
      if (opts.withExt === false) {
        delete w.setDola30s;
        delete w.getDola30sStatus;
      }
      w.fetch = async function () {
        const enc = new TextEncoder();
        const body = new ReadableStream({
          start(c) {
            c.enqueue(enc.encode('data: {"conversation_id":"7123456789012345678","status":1}\n\n'));
            setTimeout(() => {
              c.enqueue(enc.encode('data: {"status":2}\n\n'));
              c.close();
            }, 30);
          }
        });
        return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
      };
    }
  });
  return { dom, w: dom.window, errors };
}

const results = [];
function check(name, ok, detail) { results.push([name, !!ok, String(detail == null ? '' : detail)]); }

async function scenarioBasic() {
  const { dom, w, errors } = makeDom({});
  w.eval(SRC);
  const K = w.__dolaPoolKernel;
  check('内核注入到页面', !!K && typeof K.prepare === 'function', K ? 'v' + K.version : '没有 __dolaPoolKernel');

  const prep = await K.prepare({ model: 'seedance_v2.5', duration: 30, ratio: '9:16', extTimeout: 300 });
  check('从普通对话切到「视频生成」并选中 2.5 / 30s / 9:16',
    prep.ok, '错误=' + (prep.error || '') + ' 状态=' + JSON.stringify(prep.status && {
      model: prep.status.model, duration: prep.status.duration, ratio: prep.status.ratio
    }));
  check('页面状态确实是 2.5 + 30s', prep.status && /2\.5/.test(prep.status.model) && /30/.test(prep.status.duration),
    prep.status ? prep.status.model + ' / ' + prep.status.duration : '');

  const sub = await K.submit('雨夜街头，霓虹倒影，慢动作水花');
  check('提示词写进输入框并点了发送', sub.ok && /雨夜街头/.test(w.__sent || ''), sub.error || w.__sent);
  check('发送时的档位是 2.5|30s|9:16', w.__timeline === 'Dreamina Seedance 2.5|30s|9:16', w.__timeline);
  check('内核在页面里记录了 /chat/completion 请求', (K.status().completions.length > 0) || K.status().sent,
    'sent=' + K.status().sent);

  const conv = await K.waitConversation(5000);
  check('旁路读到 conversation_id', conv.ok && conv.id === '7123456789012345678', conv.id || '没有');
  check('收尾后没有 jsdom 报错', errors.length === 0, errors.join(' / ').slice(0, 200));
  dom.window.close();
}

async function scenarioNo25() {
  const { dom, w } = makeDom({ with25: false });
  w.eval(SRC);
  const prep = await w.__dolaPoolKernel.prepare({ model: 'seedance_v2.5', duration: 30, force30: false, extTimeout: 200, menuTimeout: 400 });
  check('没有 2.5 选项时：明确报「选不到模型」而不是卡死',
    !prep.ok && /2\.5|模型/.test(prep.error || ''), prep.error || '没报错');
  check('失败时把菜单里实际有的选项带出来',
    (prep.steps || []).some(s => /现有/.test(s.detail || '')), JSON.stringify(prep.steps || []).slice(0, 160));
  dom.window.close();
}

async function scenarioNo30() {
  const { dom, w } = makeDom({ withExt: false, with30: false });
  w.eval(SRC);
  const prep = await w.__dolaPoolKernel.prepare({ model: 'seedance_v2.5', duration: 30, extTimeout: 150, menuTimeout: 400 });
  check('30s 选项不存在时：明确说是「扩展没生效」',
    !prep.ok && /30\s*秒|30s/i.test(prep.error || ''), prep.error || '没报错');
  check('提示里点出「扩展」这个原因', /扩展/.test(prep.error || ''), prep.error || '');
  dom.window.close();
}

async function scenarioAlreadyInVideo() {
  const { dom, w } = makeDom({});
  w.eval(SRC);
  // 先手动进视频模式（模拟用户已经停在视频页）
  w.document.getElementById('addBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const item = Array.from(w.document.querySelectorAll('[role="menuitem"]')).find(e => /视频生成/.test(e.textContent));
  item.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const prep = await w.__dolaPoolKernel.prepare({ model: '2.0', duration: 10, ratio: '16:9', force30: false, extTimeout: 100 });
  check('已经在视频生成模式时不重复点「+」', prep.ok && (prep.steps || []).some(s => s.name === '视频生成模式' && /already/.test(s.detail)),
    prep.ok ? JSON.stringify(prep.steps[0]) : prep.error);
  dom.window.close();
}

(async () => {
  await scenarioBasic();
  await scenarioNo25();
  await scenarioNo30();
  await scenarioAlreadyInVideo();
  let bad = 0;
  for (const [name, ok, detail] of results) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (ok ? '' : '  →  ' + detail));
  }
  console.log(bad === 0
    ? `✅ 扩展内核自检全部通过（${results.length} 项）`
    : `❌ 扩展内核自检失败 ${bad}/${results.length} 项`);
  process.exit(bad === 0 ? 0 : 1);
})().catch(e => { console.error('❌ 测试崩了：', e); process.exit(1); });
