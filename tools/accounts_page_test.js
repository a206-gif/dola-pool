#!/usr/bin/env node
/**
 * 账号池页面回归自检（jsdom，不需要真浏览器）。
 *
 * 覆盖用户 v2.0.12 提的新需求：
 *   ① 能导出账号池 CK（按钮 + 三种格式 + 真的会下载）
 *   ② 能改已添加账号的 ID 名称 / 今日用量 / 每日上限（编辑弹窗）
 *   ③ 能把账号窗口「后台挂起」（列表里显示 运行中 / 已挂起 / 已关闭）
 *
 * 用法：DOLA_BASE=http://127.0.0.1:8000 node tools/accounts_page_test.js
 */
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = process.env.DOLA_BASE || 'http://127.0.0.1:8000';

(async () => {
  // 服务端不再造数据：直接在 jsdom 里用假的接口数据驱动界面
  const html = await (await fetch(BASE + '/')).text();
  const errors = [];
  const calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));

  const accounts = [
    { id: 1, name: '主号 A', note: '竖屏', status: 'active', today_usage: 2, quota: 15,
      daily_limit: 5, usage_manual_today: true, check_state: 'local_ready', check_detail: '本地格式检查通过',
      last_checked: '', cookie: '••••••' },
    { id: 2, name: '小号 B', note: '', status: 'ready', today_usage: 0, quota: 15,
      daily_limit: 0, usage_manual_today: false, check_state: 'ready', check_detail: '', last_checked: '', cookie: '••••••' },
  ];
  let sessions = { mode: 'web', can_manage: false, sessions: [] };
  // 桌面版 + 扩展：账号页应该出现「🧩 浏览器扩展」卡片
  const extInfo = { mode: 'desktop', browser: 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    browser_version: '142.0.3595.53', can_launch: true, can_open_manager: true,
    roots: ['C:/DolaPool/extensions'],
    extensions: [{ name: '哆啦助手 · 30秒无水印助手', version: '1.6.12', path: 'C:/DolaPool/extensions/dola30' },
                 { name: 'Cookie-Editor', version: '1.13.0', path: 'C:/DolaPool/extensions/cookie-editor' }] };
  let openedWithExtension = 0, openedManager = 0, revealedDir = 0;
  let accountBrowserCalls = 0, accountNativeCalls = 0;

  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o) => {
        const url = String(u);
        calls.push((o && o.method ? o.method + ' ' : 'GET ') + url);
        const body = url.includes('api/desktop/extensions/open') ? (openedWithExtension++, { ok: true, browser: extInfo.browser })
          : /api\/accounts\/\d+\/browser\/manager/.test(url) ? (openedManager++, { ok: true, browser: extInfo.browser, url: 'edge://extensions' })
          : /api\/accounts\/\d+\/browser/.test(url) ? (accountBrowserCalls++, { ok: true, mode: 'desktop', token: 'tok-1',
              browser: extInfo.browser, url: 'https://www.dola.com/chat/#dp-login=tok-1:8000', warning: '' })
          : url.includes('api/desktop/bridge/status') ? { ok: true, known: true, used: true, count: 3 }
          : /api\/accounts\/\d+\/web/.test(url) ? (accountNativeCalls++, { ok: true, mode: 'desktop', url: '/dola-web/1/' })
          : url.includes('api/desktop/extensions/manager') ? (openedManager++, { ok: true, browser: extInfo.browser, url: 'edge://extensions' })
          : url.includes('api/desktop/extensions/reveal') ? (revealedDir++, { ok: true, path: extInfo.roots[0] })
          : url.includes('api/desktop/extensions') ? extInfo
          : url.includes('api/desktop/sessions') ? sessions
          : url.includes('api/accounts/export') ? 'sessionid=abc\nsessionid=def\n'
          : /api\/accounts\/\d+\/update/.test(url) ? { ok: true, account: accounts[0] }
          : url.includes('api/accounts') ? accounts
          : url.includes('api/queue') ? { queue_depth: 0, active: 0, workers: 4 }
          : url.includes('api/tasks') ? []
          : [];
        const text = typeof body === 'string' ? body : JSON.stringify(body);
        return {
          ok: true, status: 200,
          headers: { get: () => 'attachment; filename="dola-ck-2-20261017.json"' },
          text: async () => text, json: async () => body,
          blob: async () => ({ size: 64, type: 'application/json' }),
        };
      };
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'x';
      w.URL.createObjectURL = () => 'blob:fake';
      w.URL.revokeObjectURL = () => {};
      w.HTMLAnchorElement.prototype.click = function () { calls.push('下载 ' + this.download); };
      w.console.error = (...a) => errors.push('console.error: ' + a.join(' ').slice(0, 200));
    },
  });
  const w = dom.window, d = w.document;
  const $ = id => d.getElementById(id);
  await new Promise(r => setTimeout(r, 800));

  await w.eval("go('accounts')");
  await new Promise(r => setTimeout(r, 700));

  const out = [];
  out.push(['账号池渲染出表格', !!d.querySelector('table'), '']);
  out.push(['有「导出 CK」按钮', !!$('exportCk'), '']);
  out.push(['每行都有「编辑」按钮（改名称/用量）',
            d.querySelectorAll('button[onclick^="editAccount("]').length === 2,
            d.querySelectorAll('button[onclick^="editAccount("]').length + ' 个']);
  out.push(['有「窗口」列（挂起/运行中/已关闭）',
            /窗口/.test(d.querySelector('thead').textContent), '']);
  out.push(['网页版提示：没有可挂起的原生窗口',
            /网页版/.test(d.body.textContent), '']);
  out.push(['今日用量显示手动标记', d.body.textContent.indexOf('手动') >= 0, '']);

  // ④ 浏览器扩展卡片：列出扩展 + 一键用带扩展的浏览器打开
  const extPanel = Array.from(d.querySelectorAll('.panel'))
    .find(p => /浏览器扩展/.test(p.textContent));
  out.push(['账号页有「🧩 浏览器扩展」卡片，并列出识别到的扩展',
            !!extPanel && /哆啦助手/.test(extPanel.textContent),
            extPanel ? extPanel.textContent.replace(/\s+/g, ' ').slice(0, 70) : '没有卡片']);
  out.push(['卡片写明本机浏览器路径（知道会用哪个打开）',
            !!extPanel && /msedge\.exe/.test(extPanel.textContent), '']);
  const extBtn = Array.from(d.querySelectorAll('button'))
    .find(b => /带扩展浏览器打开/.test(b.textContent));
  out.push(['「带扩展浏览器打开」按钮可用', !!extBtn && !extBtn.disabled, '']);
  if (extBtn) {
    extBtn.click();
    await new Promise(r => setTimeout(r, 400));
  }
  out.push(['点按钮会调用桌面端扩展启动接口',
            openedWithExtension === 1 &&
            calls.some(c => c.includes('api/desktop/extensions/open')),
            '调用 ' + openedWithExtension + ' 次']);
  out.push(['两套扩展（哆啦助手 / Cookie-Editor）和浏览器版本都显示出来',
            !!extPanel && /哆啦助手/.test(extPanel.textContent) && /Cookie-Editor/.test(extPanel.textContent)
              && /142\.0/.test(extPanel.textContent), '']);
  const mgrBtn = Array.from(d.querySelectorAll('button')).find(b => /手动加载/.test(b.textContent));
  const revBtn = Array.from(d.querySelectorAll('button')).find(b => /扩展文件夹/.test(b.textContent));
  out.push(['「⚙ 手动加载」「📂 扩展文件夹」两个兜底按钮都在', !!mgrBtn && !!revBtn, '']);
  if (mgrBtn) { mgrBtn.click(); await new Promise(r => setTimeout(r, 300)); }
  if (revBtn) { revBtn.click(); await new Promise(r => setTimeout(r, 300)); }
  out.push(['点「手动加载」会打开扩展管理页、点「扩展文件夹」会定位目录',
            openedManager === 1 && revealedDir === 1,
            '管理页 ' + openedManager + ' 次 / 目录 ' + revealedDir + ' 次']);

  // ① 导出 CK：点按钮 → 选格式 → 真的发起下载
  $('exportCk').click();
  await new Promise(r => setTimeout(r, 200));
  const fmtBtn = d.querySelector('[data-fmt="txt"]');
  out.push(['导出弹窗里给出 txt / csv / json 三种格式',
            !!fmtBtn && !!d.querySelector('[data-fmt="csv"]') && !!d.querySelector('[data-fmt="json"]'), '']);
  fmtBtn.click();
  await new Promise(r => setTimeout(r, 600));
  out.push(['点 TXT 后真的调用了导出接口',
            calls.some(c => c.includes('api/accounts/export?format=txt')), calls.filter(c => c.includes('export')).join(',')]);
  out.push(['下载被触发（a.download 带文件名）',
            calls.some(c => c.startsWith('下载 ')), calls.filter(c => c.startsWith('下载')).join(',')]);
  if (d.querySelector('.modal')) w.eval('closeModal()');

  // ② 编辑账号：改名称 + 今日用量
  w.eval('editAccount(1)');
  await new Promise(r => setTimeout(r, 200));
  const nameInput = d.querySelector('#mform input, #mform textarea');
  out.push(['编辑弹窗打开且带当前名称', !!nameInput && nameInput.value === '主号 A', nameInput ? nameInput.value : '']);
  out.push(['编辑弹窗有「今日用量」与「每日上限」两项',
            /今日用量/.test(d.body.textContent) && /每日上限/.test(d.body.textContent), '']);
  const form = $('mform');
  form.querySelector('input').value = '改过的名字';
  const fields = form.querySelectorAll('input, select, textarea');
  // daily_limit / today_usage 是第 3、4 个字段
  fields[2].value = '7';
  fields[3].value = '3';
  form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 500));
  out.push(['提交编辑 → 调用了 update 接口',
            calls.some(c => c.includes('api/accounts/1/update')), calls.filter(c => c.includes('update')).join(',')]);

  // ③ 会话列表：桌面版模式下显示 运行中 / 已挂起 / 已关闭 + 挂起按钮
  sessions = { mode: 'desktop', can_manage: true, sessions: [
    { account_id: 1, name: '主号 A', state: 'running', note: '前台显示中（真站已渲染）', title: '', updated_at: '' },
    { account_id: 2, name: '小号 B', state: 'parked', note: '已挂起在后台（真站页面已就绪，切换秒开）', title: '', updated_at: '' },
  ] };
  await w.eval('refreshSessions()');
  await new Promise(r => setTimeout(r, 300));
  const cells = $('sess-1').textContent + ' ' + $('sess-2').textContent;
  out.push(['桌面版：窗口列显示「前台显示中」和「后台挂起」',
            /前台显示中/.test(cells) && /后台挂起/.test(cells), cells.slice(0, 60)]);
  out.push(['运行中的行有「挂起」按钮，挂起的行有「切换过去」',
            /挂起/.test(cells) && /切换过去/.test(cells), '']);
  out.push(['会话总览列出运行中/已挂起',
            /运行中/.test($('sessionBox').textContent) && /已挂起/.test($('sessionBox').textContent),
            $('sessionBox').textContent.slice(0, 50)]);
  calls.length = 0;
  await w.eval("sessionAction(2, 'activate')");
  await new Promise(r => setTimeout(r, 400));
  out.push(['点「切换过去」→ 调用会话接口 activate',
            calls.some(c => /api\/accounts\/2\/session/.test(c)), calls.join('|').slice(0, 120)]);
  // ⑤ 账号行：「🌐 网页」= 带扩展的浏览器 + 这个账号的 CK；「🪟 原生」= 老窗口
  await w.eval('render()');
  await new Promise(r => setTimeout(r, 500));
  const webBtn = Array.from(d.querySelectorAll('button')).find(b => /🌐 网页/.test(b.textContent));
  const nativeBtn = Array.from(d.querySelectorAll('button')).find(b => /🪟 原生/.test(b.textContent));
  out.push(['账号行有「🌐 网页」和「🪟 原生」两个入口', !!webBtn && !!nativeBtn, '']);
  if (webBtn) { webBtn.click(); await new Promise(r => setTimeout(r, 300)); }
  out.push(['点「🌐 网页」会走带扩展的账号窗口接口（不是原生窗口）',
            accountBrowserCalls === 1 && accountNativeCalls === 0,
            '扩展窗口 ' + accountBrowserCalls + ' 次 / 原生 ' + accountNativeCalls + ' 次']);
  if (nativeBtn) { nativeBtn.click(); await new Promise(r => setTimeout(r, 300)); }
  out.push(['点「🪟 原生」仍然走老的原生窗口', accountNativeCalls === 1, '原生 ' + accountNativeCalls + ' 次']);
  out.push(['token 用完后前端会轮询状态并弹「已登录」', /已用 CK 登录/.test(d.body.textContent), '']);

  out.push(['无 JS 错误', errors.length === 0, errors.slice(0, 2).join(' | ')]);

  let bad = 0;
  for (const [name, ok, detail] of out) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (detail ? '  → ' + detail : ''));
  }
  console.log(bad ? `\n❌ 账号池自检 ${bad} 项失败` : '\n✅ 账号池自检全部通过（导出 CK / 编辑账号 / 窗口挂起）');
  process.exit(bad ? 1 : 0);
})();
