#!/usr/bin/env node
/**
 * 账号网页「生成助手」小条自检（jsdom）。
 *
 * 背景：用户希望像截图里的工具栏一样，在网页上直接选
 * Dreamina Seedance 2.5 + 30 秒。真实站点没有对外接口，只能模拟点击它自己的
 * 菜单，所以这里把菜单做成假的，验证：条子能出现、按钮能选中、
 * 「应用」确实点了模型/时长菜单、找不到时给出明确提示、非 dola.com 不注入。
 *
 * 用法：node tools/model_bar_test.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'static', 'dola_helper.js'), 'utf8');

function makeDom(url) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
    <div id="composer">
      <button id="modelBtn">✦ 模型 Dreamina Seedance 2.0 Fast</button>
      <div id="modelMenu" style="display:none">
        <div role="button">Dreamina Seedance 2.5</div>
        <div role="button">Dreamina Seedance 1.0</div>
      </div>
      <button id="durBtn">10s</button>
      <div id="durMenu" style="display:none">
        <div role="button">15s</div>
        <div role="button">30s</div>
      </div>
    </div>
  </body></html>`, {
    url, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.Element.prototype.getBoundingClientRect = function () {
        return { width: 90, height: 24, top: 700, left: 100, right: 190, bottom: 724, x: 100, y: 700 };
      };
      Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
        configurable: true,
        get() { return this.textContent; },
        set(v) { this.textContent = v; },
      });
      w.console.error = (...a) => errors.push('console.error: ' + a.join(' ').slice(0, 200));
      w.onerror = (m) => errors.push('error: ' + m);
    },
  });
  const w = dom.window, d = w.document;
  // 假菜单：点开→显示；点条目→改按钮文字并收起
  d.getElementById('modelBtn').addEventListener('click', () => { d.getElementById('modelMenu').style.display = 'block'; });
  d.querySelectorAll('#modelMenu [role=button]').forEach(item => {
    item.addEventListener('click', () => {
      d.getElementById('modelBtn').textContent = '✦ 模型 ' + item.textContent;
      d.getElementById('modelMenu').style.display = 'none';
    });
  });
  d.getElementById('durBtn').addEventListener('click', () => { d.getElementById('durMenu').style.display = 'block'; });
  d.querySelectorAll('#durMenu [role=button]').forEach(item => {
    item.addEventListener('click', () => {
      d.getElementById('durBtn').textContent = item.textContent;
      d.getElementById('durMenu').style.display = 'none';
    });
  });
  return { dom, w, d, errors };
}

(async () => {
  const out = [];
  const push = (row) => out.push([row[0], !!row[1], row[2] || '']);

  // 1) 非 dola.com：不注入
  {
    const { w, d, errors } = makeDom('https://example.com/');
    w.eval(SRC);
    push(['非 dola.com 页面不注入（不干扰别的站点）',
          !d.getElementById('dlp-helper') && errors.length === 0, errors[0] || '没有条子']);
    w.close();
  }

  // 2) 真站：出现条子 + 一键设置 2.5 / 30s
  {
    const { w, d, errors } = makeDom('https://www.dola.com/chat/');
    w.eval(SRC);
    await new Promise(r => setTimeout(r, 50));
    const bar = d.getElementById('dlp-helper');
    push(['dola.com 页面出现「生成助手」小条', !!bar, bar ? '在' : '没有']);
    push(['条子里能直接选 2.5 / 2.0 和 10/15/30 秒',
          !!d.querySelector('[data-m="2.5"]') && !!d.querySelector('[data-d="30"]'), '']);

    d.querySelector('[data-m="2.5"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    d.querySelector('[data-d="30"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    const ok = await w.__dlpApply2530();
    push(['点「应用」真的点开了模型菜单并选中 Seedance 2.5',
          ok && /2\.5/.test(d.getElementById('modelBtn').textContent),
          d.getElementById('modelBtn').textContent]);
    push(['同样把时长点成 30s', d.getElementById('durBtn').textContent.trim() === '30s',
          d.getElementById('durBtn').textContent]);
    push(['状态栏给出成功结果', /已设成.*2\.5.*30/.test(d.getElementById('dlp-helper-msg').textContent),
          d.getElementById('dlp-helper-msg').textContent]);
    push(['无 JS 错误', errors.length === 0, errors.slice(0, 2).join(' | ')]);
    w.close();
  }

  // 3) 菜单里没有 2.5：不能假装成功
  {
    const { w, d } = makeDom('https://www.dola.com/chat/');
    d.querySelector('#modelMenu [role=button]').textContent = 'Dreamina Seedance 2.0 Fast';
    d.querySelectorAll('#modelMenu [role=button]')[1].textContent = 'Dreamina Seedance 1.0';
    w.eval(SRC);
    await new Promise(r => setTimeout(r, 50));
    d.querySelector('[data-m="2.5"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    const ok = await w.__dlpApply2530();
    push(['菜单里没有 2.5 时会明确报错（不会假装已切换）',
          !ok && /没找到/.test(d.getElementById('dlp-helper-msg').textContent),
          d.getElementById('dlp-helper-msg').textContent.slice(0, 60)]);
    w.close();
  }

  // 4) 收起 / 恢复
  {
    const { w, d } = makeDom('https://www.dola.com/chat/');
    w.eval(SRC);
    await new Promise(r => setTimeout(r, 50));
    d.querySelector('#dlp-helper button[title="收起"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    push(['能收起成小标，点小标再展开',
          d.getElementById('dlp-helper').style.display === 'none' &&
          d.getElementById('dlp-mini').style.display === 'block',
          '']);
    w.close();
  }

  let bad = 0;
  for (const [name, ok, detail] of out) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (detail ? '  → ' + detail : ''));
  }
  console.log(bad ? `\n❌ 生成助手自检 ${bad} 项失败` : '\n✅ 生成助手自检全部通过');
  process.exit(bad ? 1 : 0);
})();
