#!/usr/bin/env node
/**
 * 生成视频页回归自检（jsdom，不需要真浏览器）。
 *
 * 背景：用户实测「生成视频页总闪烁、上传的参考图闪一下就没了」。
 * 原因是 pageGenerate 被 4 秒定时器整页重渲染（setTimers([pageGenerate])），
 * 表单里填好的提示词、上传好的参考图全部被清掉。
 * 现在改成「骨架只渲染一次 + 定时器只刷新队列/成片/最近任务」。
 *
 * 用法：DOLA_BASE=http://127.0.0.1:8000 node tools/generate_page_test.js
 *      （需要 jsdom：npm i jsdom，用 NODE_PATH 指到 node_modules）
 */
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = process.env.DOLA_BASE || 'http://127.0.0.1:8000';
const SHOT = process.env.DOLA_SHOT || '';   // 可选：把渲染后的 HTML 存下来人工看

(async () => {
  const html = await (await fetch(BASE + '/')).text();
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));

  let queueCalls = 0, uploadCalls = 0;
  const fakeQueue = () => ({ queue_depth: ++queueCalls, active: 1, workers: 4 });
  const fakeTasks = () => ([{ id: 7, status: 'done', prompt: '测试', video_url: '/files/a.mp4', created_at: '' }]);

  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o) => {
        const url = String(u);
        const body = url.includes('api/queue') ? fakeQueue()
          : url.includes('api/tasks') ? fakeTasks()
          : url.includes('api/upload-image') ? (uploadCalls++, { ok: true, path: '/tmp/ref1.png', url: '/uploads/ref1.png' })
          : url.includes('api/proxy-status') ? {}
          : url.includes('api/stats') ? {}
          : [];
        return { ok: true, status: 200, text: async () => JSON.stringify(body), json: async () => body };
      };
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'x';
      w.console.error = (...a) => errors.push('console.error: ' + a.join(' ').slice(0, 200));
    },
  });
  const w = dom.window, d = w.document;
  const $ = id => d.getElementById(id);
  await new Promise(r => setTimeout(r, 800));

  // 进入「生成视频」页
  await w.eval("go('generate')");
  await new Promise(r => setTimeout(r, 600));

  const out = [];
  out.push(['生成页渲染出提示词框', !!$('genPrompt'), '']);
  out.push(['生成页渲染出参考图上传框', !!$('genRefs'), '']);
  out.push(['生成页有动态区（队列/成片/最近任务）', !!$('genQueueBox') && !!$('genLatestBox') && !!$('genTasksBox'), '']);

  // 模拟用户：填提示词 + 选 3 张参考图（顺带验证上传调用）
  $('genPrompt').value = '第一行提示词\n第二行提示词';
  $('genCopies').value = '3';
  const input = $('genRefs');
  const files = ['a.png', 'b.png', 'c.png'].map(n => new w.File([new Uint8Array([1, 2, 3])], n, { type: 'image/png' }));
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  input.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 800));

  out.push(['上传接口被调用 3 次', uploadCalls === 3, '实际 ' + uploadCalls + ' 次']);
  out.push(['缩略图出现 3 张', d.querySelectorAll('#refThumbs img').length === 3, '实际 ' + d.querySelectorAll('#refThumbs img').length + ' 张']);

  // 等 2 个刷新周期（定时器 4s），确认表单/参考图没被清掉、动态区确实更新了
  const before = queueCalls;
  await new Promise(r => setTimeout(r, 9500));

  out.push(['刷新周期确实在跑', queueCalls > before, before + ' → ' + queueCalls]);
  out.push(['提示词没被清掉', $('genPrompt') && $('genPrompt').value === '第一行提示词\n第二行提示词', ($('genPrompt') || {}).value]);
  out.push(['重复次数没被重置', $('genCopies') && $('genCopies').value === '3', ($('genCopies') || {}).value]);
  out.push(['参考图没被清掉', d.querySelectorAll('#refThumbs img').length === 3, '实际 ' + d.querySelectorAll('#refThumbs img').length + ' 张']);
  out.push(['队列数字有刷新', /[0-9]/.test(($('genQueueBox') || {}).textContent || ''), (($('genQueueBox') || {}).textContent || '').slice(0, 30)]);
  out.push(['页面没有整页重渲染（表单还是同一个 DOM 节点）', $('genPrompt') === d.getElementById('genPrompt') && !!$('genPrompt'), '']);
  out.push(['无 JS 错误', errors.length === 0, errors.slice(0, 3).join(' | ')]);

  if (SHOT) require('fs').writeFileSync(SHOT, d.body.innerHTML);

  let bad = 0;
  for (const [name, ok, detail] of out) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (detail ? '  → ' + detail : ''));
  }
  console.log(bad ? `\n❌ 生成页自检 ${bad} 项失败` : '\n✅ 生成页自检全部通过（不再闪烁、上传不会被清）');
  process.exit(bad ? 1 : 0);
})();
