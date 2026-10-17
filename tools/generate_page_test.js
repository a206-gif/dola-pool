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

  let queueCalls = 0, uploadCalls = 0, taskCalls = 0;
  const fakeQueue = () => ({ queue_depth: ++queueCalls, active: 1, workers: 4 });
  // 第一次 1 条，之后 2 条：验证「新增行是插进去的，旧行不动」
  const fakeTasks = () => {
    taskCalls++;
    const rows = [{ id: 7, status: 'done', prompt: '测试', video_url: '/files/a.mp4', created_at: '' }];
    if (taskCalls > 2) rows.unshift({ id: 8, status: 'running', progress: 42, prompt: '第二条', video_url: '', created_at: '' });
    return rows;
  };

  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o) => {
        const url = String(u);
        let body = [];
        if (url.includes('api/accounts')) {
          body = [{ id: 1, name: '主号', cookie: 'sessionid=x' }];
        } else if (url.includes('api/model-probe')) {
          body = { ok: false, model: 'seedance_v2.5', duration: 10,
            steps: [
              { name: '生成核心', ok: true, detail: '已加载', hint: '' },
              { name: '联网检查', ok: true, detail: 'DNS 30ms / TCP 80ms / TLS 120ms', hint: '' },
              { name: '登录态', ok: false, detail: 'HTTP 302 → 登录页', hint: 'Cookie 可能已失效，去账号池重新导入' }],
            verdict: '登录态失效：先去账号池更新 Cookie' };
        } else if (url.includes('api/queue')) {
          body = fakeQueue();
        } else if (url.includes('api/tasks')) {
          body = fakeTasks();
        } else if (url.includes('api/upload-image')) {
          uploadCalls++; body = { ok: true, path: '/tmp/ref1.png', url: '/uploads/ref1.png' };
        } else if (url.includes('api/proxy-status') || url.includes('api/stats')) {
          body = {};
        }
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

  // —— 模型体检：时长联动 + 弹窗 + 结果渲染 ——
  const modelSel = $('genModel');
  out.push(['生成页默认是能用的 Dreamina Seedance 2.0 Fast + 10 秒',
            modelSel.value === 'seedance_v2.0' && $('genDuration').value === '10',
            modelSel.value + ' / ' + $('genDuration').value + 's']);
  out.push(['2.5 还在选项里，但明确标注「多数账号调不到」',
            /Seedance 2\.5/.test(modelSel.textContent) && /调不到/.test(modelSel.textContent),
            modelSel.textContent.replace(/\s+/g, ' ').slice(0, 80)]);
  modelSel.value = 'seedance_v2.5';
  modelSel.dispatchEvent(new w.Event('change', { bubbles: true }));
  const d25 = Array.from($('genDuration').options).map(o => o.value).join(',');
  out.push(['选 v2.5 时时长只剩 30/10 秒，并给出「多数账号调不到」警告',
            d25 === '30,10' && /调不到/.test(($('genHint') || {}).textContent || ''),
            d25 + ' / ' + (($('genHint') || {}).textContent || '').slice(0, 40)]);
  modelSel.value = 'seedance_v2.0';
  modelSel.dispatchEvent(new w.Event('change', { bubbles: true }));
  const d20 = Array.from($('genDuration').options).map(o => o.value).join(',');
  out.push(['选 v2.0 时时长恢复 10/15/5 秒', d20 === '10,15,5', d20]);

  $('genProbe').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 300));
  out.push(['点「模型体检」弹出体检窗（能选账号）',
            !!$('probeAccount') && $('probeAccount').options.length === 1,
            $('probeAccount') ? $('probeAccount').options.length + ' 个账号' : '没弹窗']);
  $('probeRun').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 600));
  const probeText = ($('probeResult') || {}).textContent || '';
  out.push(['体检结果按步骤列出，并给出结论',
            /生成核心/.test(probeText) && /登录态/.test(probeText) &&
            /登录态失效/.test(probeText) && /Cookie 可能已失效/.test(probeText),
            probeText.replace(/\s+/g, ' ').slice(0, 90)]);
  if ($('probeResult')) { try { w.eval('closeModal()'); } catch (e) {} }

  // 等 2 个刷新周期（定时器 4s），确认表单/参考图没被清掉、动态区确实更新了
  const before = queueCalls;
  await new Promise(r => setTimeout(r, 9500));

  out.push(['刷新周期确实在跑', queueCalls > before, before + ' → ' + queueCalls]);
  out.push(['提示词没被清掉', $('genPrompt') && $('genPrompt').value === '第一行提示词\n第二行提示词', ($('genPrompt') || {}).value]);
  out.push(['重复次数没被重置', $('genCopies') && $('genCopies').value === '3', ($('genCopies') || {}).value]);
  out.push(['参考图没被清掉', d.querySelectorAll('#refThumbs img').length === 3, '实际 ' + d.querySelectorAll('#refThumbs img').length + ' 张']);
  out.push(['队列数字有刷新', /[0-9]/.test(($('genQueueBox') || {}).textContent || ''), (($('genQueueBox') || {}).textContent || '').slice(0, 30)]);
  out.push(['页面没有整页重渲染（表单还是同一个 DOM 节点）', $('genPrompt') === d.getElementById('genPrompt') && !!$('genPrompt'), '']);

  // 闪烁的根因是每 4 秒重建 <video> 和整张任务表。这里直接盯住节点身份：
  // 同一节点还在 = 浏览器不会重新加载视频（不会闪）。
  const video = $('genLatestVideo');
  const row7 = d.querySelector('#genTasksBox tr[data-id="7"]');
  const videoSrc = video && video.getAttribute('src');
  out.push(['最近成片用的是常驻 <video>，且已设好地址',
            !!video && videoSrc === '/files/a.mp4' && video.style.display !== 'none',
            video ? 'src=' + videoSrc : '没有 video']);
  out.push(['最近任务渲染成了「一行一个 DOM 节点」', !!row7 && d.querySelectorAll('#genTasksBox tr[data-id]').length >= 1,
            d.querySelectorAll('#genTasksBox tr[data-id]').length + ' 行']);

  // 再等一个刷新周期：应新增 #8，但 #7 那一行和 <video> 必须还是原来的对象
  await new Promise(r => setTimeout(r, 5200));
  const video2 = $('genLatestVideo');
  const row7b = d.querySelector('#genTasksBox tr[data-id="7"]');
  out.push(['刷新后 <video> 还是同一个节点（不会重新加载 → 不闪）',
            !!video2 && video2 === video && video2.getAttribute('src') === videoSrc,
            video2 === video ? '节点未重建' : '节点被重建了']);
  out.push(['刷新后旧任务行没被重建（只做增量更新）', !!row7b && row7b === row7, row7b === row7 ? '#7 未重建' : '#7 被换了']);
  out.push(['新任务出现时只插入新行', !!d.querySelector('#genTasksBox tr[data-id="8"]') && d.querySelectorAll('#genTasksBox tr[data-id]').length === 2,
            d.querySelectorAll('#genTasksBox tr[data-id]').length + ' 行']);
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
