#!/usr/bin/env node
/**
 * 「🌐 网页」注入脚本的行为测试。
 *
 * 脚本本身内嵌在 app.py 的 _PROXY_JS 常量里，这里直接从 app.py 抠出来再跑，
 * 保证「测的就是上线的那一份」，不会出现两边各改各的。
 *
 * 覆盖 SPA 白屏最常见的原因：页面用 JS 动态拼地址时，根路径请求打到了本机服务
 * 而不是 Dola（el.src='/static/chunk.js'、setAttribute、innerHTML、动态 import 前的
 * script 标签、fetch/XHR 等）。
 *
 * 用法：node tools/inject_test.js          （需要 jsdom；没装会提示并跳过）
 *      npm i jsdom && node tools/inject_test.js
 */

const fs = require('fs');
const path = require('path');

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.log('⚠️  未安装 jsdom，跳过注入脚本行为测试');
  console.log('   需要时执行：npm i jsdom');
  process.exit(0);
}

const root = path.join(__dirname, '..');
const appSrc = fs.readFileSync(path.join(root, 'app.py'), 'utf8');
const m = appSrc.match(/_PROXY_JS = r"""([\s\S]*?)"""/);
if (!m) {
  console.log('❌ 没能从 app.py 里找到 _PROXY_JS 常量');
  process.exit(1);
}
const src = m[1];

const P = '/dola-web/2';
const errs = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errs.push('jsdomError: ' + ((e.detail && e.detail.message) || e.message)));

const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
  url: 'http://127.0.0.1:9999' + P + '/chat/',
  runScripts: 'dangerously',
  virtualConsole: vc,
  beforeParse(w) {
    w.__DLP__ = { prefix: P, label: '账号 #2 测试', real: 'https://www.dola.com/' };
    const reqs = [];
    w.__reqs = reqs;
    w.fetch = function (u) { reqs.push('fetch:' + u); return Promise.resolve({ status: 200, url: String(u) }); };
    // 用 prototype 形式模拟真实 XHR，否则覆写 XMLHttpRequest.prototype.open 测不到
    function FakeXHR() {}
    FakeXHR.prototype.open = function (mm, u) { reqs.push('xhr:' + u); };
    FakeXHR.prototype.send = function () {};
    FakeXHR.prototype.addEventListener = function () {};
    FakeXHR.prototype.status = 200;
    w.XMLHttpRequest = FakeXHR;
  },
});

const w = dom.window, d = w.document;
w.eval(src);

const out = [];
const t = (name, got, want) => out.push([name, got === want, got, want]);

// 元素属性赋值：SPA 加载 chunk / 图片的主流方式
const img = d.createElement('img'); img.src = '/static/a.png';
t('img.src = "/static/a.png"', img.getAttribute('src'), P + '/static/a.png');

const sc = d.createElement('script'); sc.src = '/static/chunk.js';
t('script.src = "/static/chunk.js"', sc.getAttribute('src'), P + '/static/chunk.js');

const link = d.createElement('link'); link.href = '/static/app.css';
t('link.href = "/static/app.css"', link.getAttribute('href'), P + '/static/app.css');

const a = d.createElement('a'); a.href = '/chat/123';
t('a.href = "/chat/123"', a.getAttribute('href'), P + '/chat/123');

// setAttribute
const img2 = d.createElement('img'); img2.setAttribute('src', '/img/b.png');
t('setAttribute("src", "/img/b.png")', img2.getAttribute('src'), P + '/img/b.png');

// innerHTML
const box = d.createElement('div');
box.innerHTML = '<img src="/img/c.png"><script src="/s/d.js"><\/script>';
t('innerHTML 里的 img', box.querySelector('img').getAttribute('src'), P + '/img/c.png');
t('innerHTML 里的 script', box.querySelector('script').getAttribute('src'), P + '/s/d.js');

// DocumentFragment + appendChild
const frag = d.createDocumentFragment();
const img3 = d.createElement('img');
img3.setAttribute('src', '/img/d.png');
frag.appendChild(img3);
d.body.appendChild(frag);
t('Fragment 内的元素', img3.getAttribute('src'), P + '/img/d.png');

// 网络层
w.fetch('/api/me');
w.fetch('https://www.dola.com/api/me2');
t('fetch 根路径', w.__reqs[0], 'fetch:' + P + '/api/me');
t('fetch 绝对地址', w.__reqs[1], 'fetch:' + P + '/api/me2');
const xhr = new w.XMLHttpRequest();
xhr.open('GET', '/api/xhr');
t('XHR 根路径', w.__reqs[2], 'xhr:' + P + '/api/xhr');

// 幂等（重复处理不会叠加前缀）
const img4 = d.createElement('img');
img4.src = '/img/e.png';
img4.src = img4.getAttribute('src');
t('幂等：重复赋值', img4.getAttribute('src'), P + '/img/e.png');
const img5 = d.createElement('img');
img5.setAttribute('src', P + '/img/f.png');
t('幂等：已带前缀', img5.getAttribute('src'), P + '/img/f.png');

// 不该动的
const a2 = d.createElement('a'); a2.href = '#anchor';
t('锚点不动', a2.getAttribute('href'), '#anchor');
const a3 = d.createElement('a'); a3.href = 'javascript:void 0';
t('javascript: 不动', a3.getAttribute('href'), 'javascript:void 0');
const img6 = d.createElement('img'); img6.src = 'data:image/png;base64,AAA';
t('data: 不动', img6.getAttribute('src'), 'data:image/png;base64,AAA');
const img7 = d.createElement('img'); img7.src = '//cdn.example.com/x.png';
t('第三方协议相对地址不动', img7.getAttribute('src'), '//cdn.example.com/x.png');
const img8 = d.createElement('img'); img8.src = 'https://cdn.example.com/x.png';
t('第三方绝对地址不动', img8.getAttribute('src'), 'https://cdn.example.com/x.png');

// 提示条
d.dispatchEvent(new w.Event('DOMContentLoaded'));
const bar = d.getElementById('dlp-bar');
const barOk = !!bar;
const btnOk = barOk && bar.textContent.includes('用系统浏览器打开');
const logOk = barOk && !!d.getElementById('dlp-log');
t('提示条已生成', barOk, true);
t('提示条含「用系统浏览器打开」', btnOk, true);
t('提示条含错误日志面板（展开能看到白屏原因）', logOk, true);

console.log('===== 注入脚本行为测试 =====');
let bad = 0;
for (const [name, ok, got, want] of out) {
  if (!ok) bad++;
  console.log((ok ? '✅' : '❌') + ' ' + name + (ok ? '  → ' + got : ('\n     得到: ' + got + '\n     期望: ' + want)));
}
console.log('\nJS 错误：' + (errs.length ? errs.join('\n') : '无'));
console.log(bad === 0 ? `\n✅ 全部通过（${out.length} 项）` : `\n❌ ${bad} 项失败`);
process.exit(bad === 0 ? 0 : 1);
