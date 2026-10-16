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
      w.fetch = async () => ({ ok: true, status: 200, text: async () => '[]', json: async () => [] });
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
  const paths = () => d.querySelectorAll('#connectionsSvg path:not(.temp)').length;
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

  // —— 场景 7：连线跟随节点拖动 / 保存加载后还在 ——
  const idB = b.id;
  d.dispatchEvent(new w.KeyboardEvent('keydown', { code: 'Escape', bubbles: true }));
  w.eval("applyCanvasWorkflow({nodes: nodes, connections: connections, zoom: 1, panX: 0, panY: 0})");
  out.push(['保存/加载后连线还在', paths() === 4, '连线 ' + paths() + ' 条']);
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
