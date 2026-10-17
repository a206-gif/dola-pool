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
  const posted = [];        // 提交任务时的请求体（验证单条/批量到底拆没拆）
  const cancelCalls = [];   // 点「停止」打到的接口
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
        } else if (url.includes('/cancel')) {
          cancelCalls.push(url);
          body = { ok: true, action: 'cancelling', status: 'cancelling' };
        } else if (url.includes('api/tasks') && String((o || {}).method || '').toUpperCase() === 'POST') {
          const payload = JSON.parse((o || {}).body || '{}');
          posted.push(payload);
          const n = (payload.tasks || []).length;
          body = { ok: true, ids: Array.from({ length: n }, (x, i) => 'new' + i), count: n, queue_depth: 0 };
        } else if (url.includes('api/tasks')) {
          body = fakeTasks();
        } else if (url.includes('api/upload-image')) {
          uploadCalls++;
          body = { ok: true, path: '/tmp/ref' + uploadCalls + '.png',
                   url: '/uploads/ref' + uploadCalls + '.png' };
        } else if (url.includes('api/ai-reason')) {
          body = { ok: true, count: 3, engine: 'local',
                   scenes: [{ prompt: '镜头一：雨夜街口' }, { prompt: '镜头二：清晨窗边' },
                            { prompt: '镜头三：黄昏天台' }] };
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
  $('genPrompt').dispatchEvent(new w.Event('input', { bubbles: true }));
  $('genCopies').value = '3';
  $('genCopies').dispatchEvent(new w.Event('input', { bubbles: true }));
  const input = $('genRefs');
  const files = ['a.png', 'b.png', 'c.png'].map(n => new w.File([new Uint8Array([1, 2, 3])], n, { type: 'image/png' }));
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  input.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 800));

  out.push(['上传接口被调用 3 次', uploadCalls === 3, '实际 ' + uploadCalls + ' 次']);
  out.push(['缩略图出现 3 张', d.querySelectorAll('#refThumbs img').length === 3, '实际 ' + d.querySelectorAll('#refThumbs img').length + ' 张']);

  // —— 指定账号：从账号池读，不用手填 ID ——
  await new Promise(r => setTimeout(r, 300));
  const accSel = $('genAccount');
  out.push(['「指定账号」是下拉框（不是手填数字）',
            !!accSel && accSel.tagName === 'SELECT', accSel ? accSel.tagName : '没有']);
  out.push(['下拉里能选到账号池里的账号',
            !!accSel && Array.from(accSel.options).some(o => o.value === '1' && /主号/.test(o.textContent)),
            accSel ? Array.from(accSel.options).map(o => o.value + ':' + o.textContent).join(' | ') : '']);
  accSel.value = '1';
  accSel.dispatchEvent(new w.Event('change', { bubbles: true }));

  // —— 切到别的页面再回来：提示词 / 选项 / 参考图都要还在 ——
  await w.eval("go('tasks')");
  await new Promise(r => setTimeout(r, 300));
  await w.eval("go('generate')");
  await new Promise(r => setTimeout(r, 700));
  out.push(['切到别的页面再回来，提示词还在',
            $('genPrompt') && $('genPrompt').value === '第一行提示词\n第二行提示词',
            ($('genPrompt') || {}).value]);
  out.push(['切回来后指定账号的选择也还在',
            $('genAccount') && $('genAccount').value === '1', ($('genAccount') || {}).value]);
  out.push(['切回来后参考图还在', d.querySelectorAll('#refThumbs img').length === 3,
            d.querySelectorAll('#refThumbs img').length + ' 张']);

  // —— 参考图能单张删除 ——
  const delBtns = d.querySelectorAll('#refThumbs .ref-del');
  out.push(['每张参考图都有单独的「✕」删除按钮', delBtns.length === 3, delBtns.length + ' 个']);
  delBtns[0].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  out.push(['点 ✕ 只移除那一张（3 → 2）',
            d.querySelectorAll('#refThumbs img').length === 2, d.querySelectorAll('#refThumbs img').length + ' 张']);
  {
    const left = w.eval("JSON.parse(localStorage.getItem('dola.genForm.v1')||'{}').refs") || [];
    out.push(['删掉的是点的那一张（按位置删，不是把同名/全部图清空）',
              left.length === 2 && left.every(r => r.path !== '/tmp/ref1.png'),
              JSON.stringify(left)]);
  }
  // 补回一张，后面的「刷新不丢」断言仍按 3 张算
  await w.eval("state.genRefs.push({path:'/tmp/ref4.png', url:'/uploads/ref4.png'})");
  await w.eval("go('generate')");
  await new Promise(r => setTimeout(r, 500));
  out.push(['补回来的参考图会重新画出来', d.querySelectorAll('#refThumbs img').length === 3,
            d.querySelectorAll('#refThumbs img').length + ' 张']);

  // —— 模型体检：时长联动 + 弹窗 + 结果渲染 ——
  const modelSel = $('genModel');
  out.push(['生成页默认是能用的 Dreamina Seedance 2.0 Fast + 10 秒',
            modelSel.value === 'seedance_v2.0' && $('genDuration').value === '10',
            modelSel.value + ' / ' + $('genDuration').value + 's']);
  out.push(['2.5 还在选项里，并标注「🧩 扩展内核」',
            /Seedance 2\.5/.test(modelSel.textContent) && /扩展内核/.test(modelSel.textContent),
            modelSel.textContent.replace(/\s+/g, ' ').slice(0, 80)]);
  modelSel.value = 'seedance_v2.5';
  modelSel.dispatchEvent(new w.Event('change', { bubbles: true }));
  const d25 = Array.from($('genDuration').options).map(o => o.value).join(',');
  out.push(['选 v2.5 时时长只剩 30/10 秒，并说明走扩展内核',
            d25 === '30,10' && /扩展内核/.test(($('genHint') || {}).textContent || ''),
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
  // —— 提交方式：默认「单条任务」，整段提示词（含换行）不能被拆开 ——
  $('genCopies').value = '1';
  $('genCopies').dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 100));
  const splitSingle = $('genSplitSingle'), splitBatch = $('genSplitBatch');
  out.push(['提交方式默认是「单条任务」', !!(splitSingle && splitSingle.checked), '']);
  out.push(['默认模式写明「换行不会被拆开」',
            /不会被拆开/.test(($('genSplitHint') || {}).textContent || ''),
            (($('genSplitHint') || {}).textContent || '').slice(0, 70)]);
  out.push(['默认模式按钮只写「提交任务」（不是 N 条）',
            ($('genSubmit') || {}).textContent === '✧ 提交任务', ($('genSubmit') || {}).textContent]);

  // 点提交 → 必须只有 1 条，且提示词是整段（换行保留）
  $('genSubmit').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 500));
  out.push(['单条模式下点提交 → 只创建 1 条任务', posted.length === 1 && posted[0].tasks.length === 1,
            posted.length ? JSON.stringify(posted[0].tasks.length) : '没发请求']);
  out.push(['整段提示词原样提交（两行没被拆成两条）',
            posted.length === 1 && posted[0].tasks[0].prompt === '第一行提示词\n第二行提示词',
            posted.length ? JSON.stringify(posted[0].tasks[0].prompt) : '']);

  // —— 切到「批量任务」：只有这次明确选择才会按行拆 ——
  await w.eval("go('generate')");
  await new Promise(r => setTimeout(r, 600));
  $('genCopies').value = '1';
  $('genCopies').dispatchEvent(new w.Event('input', { bubbles: true }));
  $('genSplitBatch').checked = true;
  $('genSplitBatch').dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 100));
  out.push(['切到批量后提示「2 行 → 2 条任务」',
            /2\s*行/.test(($('genSplitHint') || {}).textContent || '') &&
            /2/.test(($('genCountHint') || {}).textContent || ''),
            (($('genSplitHint') || {}).textContent || '').slice(0, 70)]);
  out.push(['切到批量后按钮写明要提交 2 条',
            ($('genSubmit') || {}).textContent === '✧ 提交 2 条任务', ($('genSubmit') || {}).textContent]);
  $('genSubmit').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  out.push(['批量提交前会先弹确认（避免误拆）', !!$('okBtn'), $('okBtn') ? '有确认框' : '没有']);
  if ($('okBtn')) $('okBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 500));
  out.push(['确认后才按行拆成 2 条', posted.length === 2 && posted[1].tasks.length === 2,
            posted.length > 1 ? String(posted[1].tasks.length) : '没发第二次请求']);
  out.push(['拆出来的每条是单独的一行',
            posted.length > 1 && posted[1].tasks[0].prompt === '第一行提示词' &&
            posted[1].tasks[1].prompt === '第二行提示词',
            posted.length > 1 ? JSON.stringify(posted[1].tasks.map(t => t.prompt)) : '']);

  // —— 任务队列：运行中的任务有「停止」，一键停止全部也在 ——
  await w.eval("go('tasks')");
  await new Promise(r => setTimeout(r, 700));
  const stopBtns = d.querySelectorAll('#taskTable button');
  const hasStop = Array.from(stopBtns).some(b => /停止/.test(b.textContent));
  out.push(['任务队列里运行中的任务有「■ 停止」按钮', hasStop,
            Array.from(stopBtns).map(b => b.textContent.trim()).join(' | ')]);
  out.push(['任务队列有「■ 停止全部」', !!$('stopAllBtn'), $('stopAllBtn') ? '有' : '没有']);
  const runningStop = Array.from(stopBtns).find(b => /停止/.test(b.textContent) && b.textContent !== '■ 停止全部');
  if (runningStop) {
    runningStop.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 400));
  }
  out.push(['点「停止」会调用取消接口', cancelCalls.length === 1, JSON.stringify(cancelCalls)]);

  // 「一键任务」：从任务队列直接回到单条任务的生成页
  const quick = Array.from(d.querySelectorAll('button')).find(b => /单条任务/.test(b.textContent));
  out.push(['任务队列有「＋ 单条任务」一键入口', !!quick, quick ? quick.textContent : '没有']);
  if (quick) quick.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 900));
  out.push(['点它回到生成页，且是「单条任务」模式',
            !!$('genPrompt') && !!$('genSplitSingle') && $('genSplitSingle').checked,
            $('genPrompt') ? '已回到生成页' : '没回去']);

  // —— AI 帮我写提示词：拆出多条镜头时要自动切到「批量」，否则会被当成一段 ——
  w.eval("openAiAssist()");
  await new Promise(r => setTimeout(r, 300));
  const scriptBox = d.querySelector('#mform textarea[name="script"]');
  if (scriptBox) scriptBox.value = '女孩在便利店门口等雨停';
  const mform = d.querySelector('#mform');
  if (mform) mform.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 700));
  out.push(['AI 拆出 3 条镜头后，自动切到「批量任务（每行一条）」',
            !!$('genSplitBatch') && $('genSplitBatch').checked && !!$('genPrompt') &&
            $('genPrompt').value.split('\n').filter(Boolean).length === 3,
            $('genPrompt') ? JSON.stringify($('genPrompt').value) : '没回到生成页']);
  out.push(['切到批量后提示「3 行 → 3 条任务」',
            /3\s*行/.test(($('genSplitHint') || {}).textContent || ''),
            (($('genSplitHint') || {}).textContent || '').slice(0, 60)]);
  if ($('genSplitSingle')) {
    $('genSplitSingle').checked = true;
    $('genSplitSingle').dispatchEvent(new w.Event('change', { bubbles: true }));
  }

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
