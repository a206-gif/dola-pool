#!/usr/bin/env node
/**
 * 网页界面自检（jsdom，不需要真浏览器）。
 *
 * 用法：DOLA_BASE=http://127.0.0.1:8000 node tools/ui_check.js
 *      （需要 jsdom：npm i jsdom，用 NODE_PATH 指到 node_modules）
 *
 * 检查：控制台页面能渲染出侧栏、版本号显示正确、菜单在、没有 JS 错误。
 */
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = process.env.DOLA_BASE || 'http://127.0.0.1:8000';
const WANT = process.env.DOLA_WANT_VERSION || '';

(async () => {
  const html = await (await fetch(BASE + '/')).text();
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));
  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(String(u).startsWith('http') ? u : BASE + (String(u).startsWith('/') ? u : '/' + u), o);
      w.alert = () => {};
      w.confirm = () => true;
      w.console.error = (...a) => errors.push('console.error: ' + a.join(' ').slice(0, 200));
    },
  });
  await new Promise(r => setTimeout(r, 3000));
  const d = dom.window.document;
  const bar = d.querySelector('#sidebar');
  const text = bar ? bar.textContent : d.body.textContent;
  const out = [];
  out.push(['页面渲染出侧栏', !!bar, text.slice(0, 40)]);
  out.push(['入口菜单在（账号池 / 设置）', /账号池/.test(text) && /设置/.test(text), '']);
  if (WANT) {
    out.push(['侧栏版本号 = ' + WANT, text.indexOf(WANT) >= 0, (text.match(/v[\d.]+[^\n]{0,12}/) || [''])[0]]);
  }
  out.push(['无 JS 错误', errors.length === 0, errors.slice(0, 3).join(' | ')]);

  let bad = 0;
  for (const [name, ok, detail] of out) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (detail ? '  → ' + detail : ''));
  }
  console.log(bad ? `\n❌ ${bad} 项失败` : '\n✅ 界面自检全部通过');
  process.exit(bad ? 1 : 0);
})();
