#!/usr/bin/env node
/**
 * 节点画布「连线」回归自检（jsdom，不需要真浏览器）。
 *
 * 背景：用户实测「画布无法连线」。原因是端口只有 14px、还必须正好松手在
 * 目标圆点上，手一抖就静默失败（没有临时线以外的任何反馈），
 * 而且旧代码只认「按住输出圆点拖到输入圆点」这一种操作。
 * 现在支持：① 拖拽（落点 60px 容错 / 落在节点身上也能连）② 先点一端再点另一端
 * ③ 从输入端口反向拖；并有 toast/高亮反馈。
 *
 * 用法：DOLA_BASE=http://127.0.0.1:8000 node tools/canvas_test.js
 *      （需要 jsdom：npm i jsdom，用 NODE_PATH 指到 node_modules）
 */
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = process.env.DOLA_BASE || 'http://127.0.0.1:8000';

(async () => {
  const html = await (await fetch(BASE + '/canvas')).text();
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));

  const dom = new JSDOM(html, {
    url: BASE + '/canvas', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u) => {
        const isAccounts = String(u).includes('/api/accounts');
        const body = isAccounts ? [{ id: 1, name: '主号', status: 'ready' },
                                   { id: 7, name: '小号', status: 'ready' }] : [];
        return { ok: true, status: 200, text: async () => JSON.stringify(body),
                 json: async () => body };
      };
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'x';
      w.console.error = (...a) => errors.push('console.error: ' + a.join(' ').slice(0, 200));
    },
  });
  const w = dom.window, d = w.document;
  const toasts = [];
  await new Promise(r => setTimeout(r, 500));
  w.toast = (...a) => toasts.push(a.join(' '));          // 抓提示信息
  w.eval('toast = (...a) => {}');                        // 页面内部的 toast 走 window.toast

  const mk = (type, cx, cy) => new w.MouseEvent(type, { bubbles: true, cancelable: true, clientX: cx, clientY: cy });
  // 只数真正的连线：defs 里的箭头 marker 也是 <path>，不能算进去
  const paths = () => d.querySelectorAll('#connLayer path.conn').length;
  const out = [];

  // —— 场景 1：按住输出圆点，拖到输入圆点松开 ——
  const a = w.eval("createNode('text_input', 80, 120)");
  const b = w.eval("createNode('ai_reason', 460, 120)");
  const outA = d.querySelector('#' + a.id + ' .port.output');
  const inB = d.querySelector('#' + b.id + ' .port.input');
  out.push(['节点有输出/输入圆点', !!outA && !!inB, '']);
  outA.dispatchEvent(mk('mousedown', 200, 180));
  d.dispatchEvent(mk('mousemove', 400, 180));
  const tempDuringDrag = d.querySelectorAll('#connectionsSvg path.temp').length;
  inB.dispatchEvent(mk('mouseup', 470, 180));
  out.push(['拖动时有临时线（看得见在连）', tempDuringDrag === 1, '临时线 ' + tempDuringDrag + ' 条']);
  out.push(['拖拽连线成功', paths() === 1, '连线 ' + paths() + ' 条']);
  out.push(['临时线松手后清掉了', d.querySelectorAll('#connectionsSvg path.temp').length === 0, '']);

  // —— 场景 2：点一下输出圆点，再点输入圆点（不用拖） ——
  const c = w.eval("createNode('text_input', 80, 400)");
  const e2 = w.eval("createNode('video_gen', 460, 400)");
  const outC = d.querySelector('#' + c.id + ' .port.output');
  const inE = d.querySelector('#' + e2.id + ' .port.input');
  outC.dispatchEvent(mk('mousedown', 200, 460));
  outC.dispatchEvent(mk('mouseup', 200, 460));           // 原地松开 = 点一下
  const armed = d.querySelectorAll('.port.armed').length;
  inE.dispatchEvent(mk('mousedown', 520, 460));
  inE.dispatchEvent(mk('mouseup', 520, 460));
  out.push(['点一下输出圆点后端口高亮（有反馈）', armed === 1, '高亮 ' + armed + ' 个']);
  out.push(['两次点击也能连上', paths() === 2, '连线 ' + paths() + ' 条']);

  // —— 场景 3：落点在目标节点的身上（没精确点中圆点）也能连 ——
  const f = w.eval("createNode('text_input', 80, 700)");
  const g = w.eval("createNode('merge', 460, 700)");
  const outF = d.querySelector('#' + f.id + ' .port.output');
  const bodyG = d.querySelector('#' + g.id + ' .node-body');
  outF.dispatchEvent(mk('mousedown', 200, 760));
  d.dispatchEvent(mk('mousemove', 480, 760));
  bodyG.dispatchEvent(mk('mouseup', 480, 760));
  out.push(['松手在节点身上（没点中圆点）也能连', paths() === 3, '连线 ' + paths() + ' 条']);

  // —— 场景 4：从输入圆点反向拖到输出圆点（ai_reason 有输入端口） ——
  const h = w.eval("createNode('text_input', 80, 1000)");
  const i2 = w.eval("createNode('ai_reason', 460, 1000)");
  const inH = d.querySelector('#' + i2.id + ' .port.input');     // 从 ai_reason 的输入开始拖
  const outH = d.querySelector('#' + h.id + ' .port.output');    // 拖到 text_input 的输出
  inH.dispatchEvent(mk('mousedown', 470, 1060));
  d.dispatchEvent(mk('mousemove', 300, 1060));
  outH.dispatchEvent(mk('mouseup', 200, 1060));
  out.push(['从输入端口反向拖也能连', paths() === 4, '连线 ' + paths() + ' 条']);

  // —— 场景 5：防呆 —— 自己连自己 / 重复连 ——
  const before = paths();
  w.eval("connectPorts({nodeId:'" + a.id + "',port:'text',type:'output'},{nodeId:'" + a.id + "',port:'text',type:'input'})");
  const noSelf = paths() === before;
  w.eval("connectPorts({nodeId:'" + a.id + "',port:'text',type:'output'},{nodeId:'" + b.id + "',port:'text',type:'input'})");
  const noDup = paths() === before;
  out.push(['不能连自己', noSelf, '']);
  out.push(['不能重复连同一对端口', noDup, '']);

  // —— 场景 6：Esc 取消待连线 ——
  outH.dispatchEvent(mk('mousedown', 200, 1060));
  outH.dispatchEvent(mk('mouseup', 200, 1060));
  const armedBeforeEsc = d.querySelectorAll('.port.armed').length;
  d.dispatchEvent(new w.KeyboardEvent('keydown', { code: 'Escape', bubbles: true }));
  out.push(['Esc 能取消待连线', armedBeforeEsc === 1 && d.querySelectorAll('.port.armed').length === 0,
            armedBeforeEsc + ' → ' + d.querySelectorAll('.port.armed').length]);

  // —— 场景 7：连线「看得见」—— 亮色 class、箭头 marker、中点标签 ——
  const connPaths = d.querySelectorAll('#connLayer path.conn');
  const marker = d.querySelector('#connectionsSvg marker#connArrow');
  const labels = d.querySelectorAll('#connLayer text.conn-label');
  out.push(['连线不再是深灰线（用 conn 类，CSS 里是亮青色 + 发光）',
            connPaths.length === 4 && Array.from(connPaths).every(x => x.classList.contains('conn')),
            '带 conn 类的 path ' + connPaths.length + ' 条']);
  out.push(['连线有箭头（marker 定义在 defs 里）',
            !!marker && marker.getAttribute('markerWidth') !== null, marker ? 'connArrow 在' : '没有']);
  const arrowAttrs = Array.from(connPaths).map(p => p.getAttribute('marker-end') || '');
  out.push(['每条线真的挂了箭头（marker-end），且按目标端口换色',
            arrowAttrs.length === 4 && arrowAttrs.every(a => /^url\(#connArrow/.test(a)) &&
            arrowAttrs.some(a => a.includes('connArrow')),
            arrowAttrs.join(' ')]);
  out.push(['每条连线都有 18px 的透明点击区（细线也点得中）',
            d.querySelectorAll('#connLayer path.conn-hit').length === connPaths.length,
            d.querySelectorAll('#connLayer path.conn-hit').length + ' 条']);
  out.push(['每条连线中点有「from → to」标签（一眼看出连的哪个端口）',
            labels.length === connPaths.length &&
            Array.from(labels).some(t => /text\s*→\s*text|→/.test(t.textContent)),
            labels.length + ' 个标签，例：' + (labels[0] ? labels[0].textContent : '')]);

  // —— 场景 8：三种连线方式之「双击端口自动连」 ——
  w.eval("clearCanvas(false)");
  const dblA = w.eval("createNode('text_input', 60, 40)");
  const dblB = w.eval("createNode('ai_reason', 520, 60)");
  const outDbl = d.querySelector('#' + dblA.id + ' .port.output');
  outDbl.dispatchEvent(new w.MouseEvent('dblclick', {bubbles:true, cancelable:true, clientX:200, clientY:100}));
  out.push(['双击输出端口 → 自动连到最近的可连端口',
            paths() === 1 && d.querySelector('#' + dblB.id + ' .port.input').classList.contains('connected'),
            '连线 ' + paths() + ' 条']);

  // —— 场景 9：三种连线方式之「⚡ 自动连线」 ——
  w.eval("clearCanvas(false)");
  w.eval("createNode('text_input', 40, 40)");
  w.eval("createNode('image_input', 40, 320)");
  w.eval("createNode('ai_reason', 420, 40)");
  w.eval("createNode('merge', 420, 320)");
  w.eval("autoConnect()");
  const auto = w.eval("connections.map(c => c.from_port + '->' + c.to_port).join(',')");
  out.push(['「自动连线」按端口类型配对（text→text、image→image 等）',
            paths() >= 3 && /text->text/.test(auto) && /image->image/.test(auto), auto]);

  // —— 场景 10：连线管理面板（列出 / 定位 / 断开） ——
  w.eval("toggleConnPanel()");
  const items = d.querySelectorAll('#connList .conn-item');
  out.push(['连线管理面板列出全部连线（带两端节点名）',
            items.length === w.eval('connections.length') && /文本输入/.test(d.getElementById('connList').textContent),
            items.length + ' 条']);
  const firstId = w.eval('connections[0].id');
  w.eval("deleteConnection('" + firstId + "')");
  out.push(['面板里「断开」能删掉一条连线',
            items.length === w.eval('connections.length') + 1, '现在 ' + w.eval('connections.length') + ' 条']);
  out.push(['顶栏「◨ 连线 N」计数跟着变',
            /连线 \d+/.test(d.getElementById('connToggle').textContent),
            d.getElementById('connToggle').textContent]);

  // —— 场景 11：连线中高亮「可连端口」 ——
  w.eval("clearCanvas(false)");
  const hiA = w.eval("createNode('text_input', 40, 40)");
  const hiB = w.eval("createNode('video_gen', 480, 40)");
  const hiOut = d.querySelector('#' + hiA.id + ' .port.output');
  hiOut.dispatchEvent(mk('mousedown', 200, 100));
  out.push(['开始连线时，可连的输入端口会高亮（知道能连哪儿）',
            d.querySelectorAll('.port.compatible').length >= 1,
            d.querySelectorAll('.port.compatible').length + ' 个可连端口']);
  d.dispatchEvent(new w.KeyboardEvent('keydown', { code: 'Escape', bubbles: true }));
  out.push(['取消后高亮消失', d.querySelectorAll('.port.compatible').length === 0, '']);
  const hiIn = d.querySelector('#' + hiB.id + ' .port.input');
  hiOut.dispatchEvent(mk('click', 200, 100));
  hiOut.dispatchEvent(mk('click', 200, 100));   // 第一次选中，第二次取消
  hiOut.dispatchEvent(mk('click', 200, 100));   // 再选中
  hiIn.dispatchEvent(mk('click', 900, 120));    // 点另一端 → 连上
  out.push(['取消后还能正常连上', paths() === 1, '连线 ' + paths() + ' 条']);

  // —— 场景 12：刷新页面后自动恢复上次画布（连线不丢） ——
  const beforeReload = w.eval('connections.length');
  w.eval('scheduleAutosave()');
  await new Promise(r => setTimeout(r, 700));
  const saved = JSON.parse(w.localStorage.getItem('dola_node_workflow') || '{}');
  out.push(['画布自动存档到浏览器（不用点保存，刷新能恢复）',
            (saved.connections || []).length === beforeReload && (saved.nodes || []).length > 0,
            (saved.nodes || []).length + ' 节点 / ' + (saved.connections || []).length + ' 连线']);

  // —— 场景 13：连线跟随节点拖动 / 保存加载后还在 ——
  w.eval("applyCanvasWorkflow(JSON.parse(localStorage.getItem('dola_node_workflow')))");
  out.push(['保存/加载后连线还在', paths() === beforeReload && paths() > 0, '连线 ' + paths() + ' 条']);

  // —— 场景 14：自动布局（按连线分层，左 → 右） ——
  w.eval('clearCanvas(false)');
  const layA = w.eval("createNode('text_input', 40, 300)");
  const layB = w.eval("createNode('ai_reason', 400, 40)");
  const layC = w.eval("createNode('video_gen', 800, 300)");
  w.eval('autoConnect()');
  w.eval('autoLayout()');
  const px = id => w.eval("nodes.find(n => n.id === '" + id + "').x");
  out.push(['自动布局按连线方向排成左→右（上游在左、下游在右）',
            px(layA.id) < px(layB.id) && px(layB.id) < px(layC.id),
            [px(layA.id), px(layB.id), px(layC.id)].join(' → ')]);

  // —— 场景 15：看全图（缩略图式概览） ——
  w.eval('fitView()');
  const z = w.eval('zoom');
  out.push(['「看全图」会把缩放调到能装下所有节点', z > 0.2 && z <= 1.2, 'zoom=' + z.toFixed(2)]);

  // —— 场景 16：撤销 / 重做 ——
  const beforeUndo = w.eval('connections.length');
  w.eval("deleteConnection(connections[0].id)");
  const afterDel = w.eval('connections.length');
  w.eval('undo()');
  const afterUndo = w.eval('connections.length');
  w.eval('redo()');
  const afterRedo = w.eval('connections.length');
  out.push(['撤销能恢复被断开的连线', afterDel === beforeUndo - 1 && afterUndo === beforeUndo,
            beforeUndo + ' → 删 ' + afterDel + ' → 撤销 ' + afterUndo]);
  out.push(['重做能再删一次', afterRedo === afterDel, '重做后 ' + afterRedo + ' 条']);

  // —— 场景 17：清空按钮本身可用（以前参数名遮蔽了 window.confirm，点了会报错） ——
  let clearErr = '';
  try { w.eval('clearCanvas()'); } catch (e) { clearErr = e.message; }
  out.push(['点「清空」不会报错，并清掉节点', !clearErr && w.eval('nodes.length') === 0,
            clearErr || '节点 0 个']);
  w.eval('undo()');
  out.push(['清空也能撤销回来', w.eval('nodes.length') > 0, w.eval('nodes.length') + ' 个节点']);

  // —— 场景 18：拖动节点吸附到 10px 网格 ——
  w.eval('clearCanvas(false)');
  const snapN = w.eval("createNode('text_input', 30, 30)");
  const header = d.querySelector('#' + snapN.id + ' .node-header');
  header.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 100, clientY: 100 }));
  d.dispatchEvent(new w.MouseEvent('mousemove', { bubbles: true, clientX: 137, clientY: 133 }));
  d.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: 137, clientY: 133 }));
  const sx = w.eval("nodes[0].x"), sy = w.eval("nodes[0].y");
  out.push(['拖动时节点吸附到 10px 网格（不会歪）',
            Number.isFinite(sx) && sx % 10 === 0 && sy % 10 === 0, 'x=' + sx + ' y=' + sy]);

  // —— 场景 18b：视频节点的账号选择 + 30 秒走扩展内核 ——
  w.eval('clearCanvas(false)');
  const accV = w.eval("createNode('video_gen', 300, 300)");
  w.eval("updateNodeData('" + accV.id + "','model','sd2.5')");
  w.eval("updateVideoHint('" + accV.id + "')");
  const accSel = d.getElementById('acc_' + accV.id);
  await new Promise(r => setTimeout(r, 300));
  out.push(['视频节点有「账号」下拉（直接读账号池，不用记 ID）', !!accSel,
            accSel ? '有 ' + accSel.options.length + ' 个选项' : '没有']);
  out.push(['账号下拉里真能选到账号池的账号',
            !!accSel && Array.from(accSel.options).some(o => o.value === '7' && /小号/.test(o.textContent)),
            accSel ? Array.from(accSel.options).map(o => o.value + ':' + o.textContent).join(' | ') : '']);
  out.push(['选 sd2.5 时节点上写明走扩展内核',
            /扩展内核/.test((d.getElementById('vhint_' + accV.id) || {}).textContent || ''),
            (d.getElementById('vhint_' + accV.id) || {}).textContent]);
  // 执行节点：抓 POST /api/tasks 的 body，确认 account_id 带上了
  const posts = [];
  const origFetch = w.fetch;
  w.fetch = async (u, o) => {
    if (String(u).includes('/api/tasks') && o && o.method === 'POST') {
      posts.push(JSON.parse(o.body));
      return { ok: true, status: 200, json: async () => ({ id: 'task-x' }), text: async () => '{}' };
    }
    return origFetch(u, o);
  };
  w.eval("updateNodeData('" + accV.id + "','account_id','7')");
  const txt = w.eval("createNode('text_input', 40, 300)");
  w.eval("connections.push({id:'c-acc', from_node:'" + txt.id + "', from_port:'output', from_node_port:'output', to_node:'" + accV.id + "', to_port:'input'}); updateConnections();");
  await w.eval("executeNode(nodes.find(n=>n.id==='" + accV.id + "'), (function(){var o={}; o['" + txt.id + "']={output:{prompt:'雨夜街头'}}; return o;})())");
  out.push(['执行视频节点时把选中的账号带给后端',
            posts.length === 1 && posts[0].account_id === 7,
            posts.length ? JSON.stringify(posts[0]) : '没发请求']);
  out.push(['sd2.5 节点提交的是 seedance_v2.5 + 30 秒',
            posts.length === 1 && posts[0].model === 'seedance_v2.5' && posts[0].duration === 30,
            posts.length ? posts[0].model + ' / ' + posts[0].duration + 's' : '没发请求']);
  w.fetch = origFetch;

  // —— 场景 19：使用说明 + 30 秒演示 ——
  d.getElementById('guideBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const guide = d.getElementById('guideModal');
  out.push(['点「使用说明」会弹出图文说明',
            guide.classList.contains('show') &&
            /双击端口/.test(guide.textContent) && /自动连线/.test(guide.textContent) &&
            /Ctrl/.test(guide.textContent),
            guide.classList.contains('show') ? '弹窗已打开' : '没打开']);

  w.eval('playDemo()');
  await new Promise(r => setTimeout(r, 6500));
  out.push(['「播放演示」会自动搭出 4 个节点 / 3 条连线',
            w.eval('nodes.length') === 4 && w.eval('connections.length') === 3,
            w.eval('nodes.length') + ' 节点 / ' + w.eval('connections.length') + ' 连线']);
  out.push(['演示搭出来的连线是可见的（带标签/箭头）',
            d.querySelectorAll('#connLayer path.conn').length === 3 &&
            d.querySelectorAll('#connLayer text.conn-label').length === 3,
            d.querySelectorAll('#connLayer path.conn').length + ' 条']);
  out.push(['演示结束后自动收起说明弹窗', !guide.classList.contains('show'), '']);
  out.push(['画布不为空时，欢迎条自动隐藏', d.getElementById('canvasWelcome').style.display === 'none', '']);
  out.push(['无 JS 错误', errors.length === 0, errors.slice(0, 2).join(' | ')]);
  void toasts;

  let bad = 0;
  for (const [name, ok, detail] of out) {
    if (!ok) bad++;
    console.log((ok ? '✅ ' : '❌ ') + name + (detail ? '  → ' + detail : ''));
  }
  console.log(bad ? `\n❌ 画布连线自检 ${bad} 项失败` : '\n✅ 画布连线自检全部通过（拖拽 / 两次点击 / 反向 / 容错落点都行）');
  process.exit(bad ? 1 : 0);
})();
