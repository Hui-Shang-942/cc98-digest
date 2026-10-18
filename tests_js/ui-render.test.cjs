/* 前端渲染冒烟测试（Node 内建测试运行器，无第三方依赖）。
 *
 * 目的：在不开浏览器的情况下，用真实 API 数据驱动 app.js 的渲染逻辑，
 * 断言页面骨架与内容确实被写进 DOM —— 补上"无法自动截图"留下的空白。
 *
 * 运行： node --test tests_js/
 *      （本文件用 .cjs，Node 会按 CommonJS 解析；app.js 是 IIFE 无模块语法，可直接 require）
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const STATIC = path.join(__dirname, '..', 'cc98digest', 'static');
const APP_JS = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');

/* ---------- 极简 DOM / BOM 替身 ---------- */

class FakeElement {
  constructor(tag, id) {
    this.tagName = (tag || 'div').toUpperCase();
    this.id = id || '';
    this.children = [];
    this.childNodes = this.children;
    this.dataset = {};
    this.style = {};
    this._html = '';
    this._text = '';
    this.listeners = {};
    this.classList = {
      _set: new Set(),
      add: (...c) => c.forEach((x) => this.classList._set.add(x)),
      remove: (...c) => c.forEach((x) => this.classList._set.delete(x)),
      contains: (c) => this.classList._set.has(c),
    };
  }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  appendChild(node) { this.children.push(node); return node; }
  removeChild(node) { this.children = this.children.filter((c) => c !== node); return node; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  querySelector() { return null; }
  closest() { return null; }
  focus() { this._focused = true; }
  select() { this._selected = true; }
  get firstChild() { return this.children[0] || null; }
  get scrollHeight() { return 0; }
  set scrollTop(v) { this._scrollTop = v; }
  get scrollTop() { return this._scrollTop || 0; }
}

const elements = new Map();

function ensure(id) {
  if (!elements.has(id)) elements.set(id, new FakeElement('div', id));
  return elements.get(id);
}

// 先把 index.html 里出现的 id 都注册上，模拟页面初始 DOM
const INDEX_HTML = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
for (const m of INDEX_HTML.matchAll(/id="([^"]+)"/g)) ensure(m[1]);

global.document = {
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new FakeElement(tag),
};

let lastEventSource = null;

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    lastEventSource = this;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, data) { (this.listeners[type] || []).forEach((fn) => fn({ data: JSON.stringify(data) })); }
}

global.EventSource = FakeEventSource;

/* ---------- 真实感的 API 夹具 ---------- */

const REPORT = {
  schema: 1,
  runAt: '2026-10-09T15:00:00+08:00',
  runLabel: '2026-10-09 15:00:00 +0800',
  windowLabel: '2026-10-08 15:00 → 2026-10-09 15:00',
  keywords: ['AI', '黑客松', '考研搭子'],
  totalTopics: 3,
  totalScanned: 113,
  host: 'api.cc98.org',
  dryRun: false,
  parseFailures: 0,
  partialKeywords: [],
  cappedKeywords: ['AI'],
  warnings: ['关键词「考研搭子」抓取失败：测试告警'],
  groups: [
    {
      keyword: 'AI', count: 2, pages: 2, scanned: 40, status: '正常', abnormal: false,
      topics: [
        { id: 1, title: 'AI 有竖线|与换行\n的标题', url: 'https://www.cc98.org/topic/1',
          createdLabel: '10-09 14:50', repeated: true, board: '求职广场', author: '某甲' },
        { id: 2, title: '<script>alert(1)</script> 注入测试', url: 'https://www.cc98.org/topic/2',
          createdLabel: '10-09 11:08', repeated: false, board: '学习天地', author: '某乙' },
      ],
    },
    { keyword: '黑客松', count: 1, pages: 2, scanned: 33, status: '正常', abnormal: false,
      topics: [{ id: 3, title: '支付宝 AI 付黑客松', url: 'https://www.cc98.org/topic/3',
        createdLabel: '10-09 10:24', repeated: false }] },
    { keyword: '考研搭子', count: 0, pages: 0, scanned: 0, status: '抓取失败', abnormal: true, topics: [] },
  ],
  topics: [],
  config: {
    keywords: ['AI', '黑客松', '考研搭子'], hours: 24, pageSize: 20, maxPages: 2,
    matchStyle: 'auto', matchMode: 'any', rateLimit: 1, reportsDir: 'D:\\dev\\cc98-digest\\reports',
    projectRoot: 'D:\\dev\\cc98-digest', apiBase: 'https://api.cc98.org', account: 'tester',
  },
  status: { running: false, status: 'ok', message: '完成：命中 3 条', elapsed: 8.5 },
};

const STATUS = {
  status: { running: false, status: 'ok', message: '完成：命中 3 条', elapsed: 8.5, error: null },
  config: REPORT.config,
  task: { available: true, installed: true, name: 'CC98TopicDigest', state: 'Ready', nextRun: '2026/10/9 23:00:00', lastResult: '0' },
  lastLogSeq: 2,
};

const HISTORY = {
  items: [{ file: 'cc98-digest-2026-10-09-1500.json', runLabel: '2026-10-09 15:00:00 +0800',
    windowLabel: 'w', totalTopics: 3, totalScanned: 113, dryRun: false, keywords: ['AI'] }],
};

function installFetch() {
  global.fetch = async (url) => {
    const path_ = String(url);
    let payload = {};
    if (path_.startsWith('/api/report')) payload = REPORT;
    else if (path_.startsWith('/api/status')) payload = STATUS;
    else if (path_.startsWith('/api/history')) payload = HISTORY;
    else if (path_.startsWith('/api/logs')) payload = { lines: [] };
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
  };
}

/* ---------- 加载 app.js（IIFE 会立刻打一次 fetch） ---------- */

installFetch();
const pending = [];
for (const id of elements.keys()) pending.push(id);
new Function(APP_JS)(); // 在全局作用域执行，等价于浏览器加载脚本

const tick = () => new Promise((r) => setTimeout(r, 30));

// app.js 在浏览器里会挂定时器轮询；测试里不需要，挂住进程就杀掉
test.after(() => {
  if (global.window && global.window.__cc98FallbackTimer) {
    clearInterval(global.window.__cc98FallbackTimer);
  }
  setTimeout(() => process.exit(process.exitCode || 0), 60).unref?.();
});

/* ---------- 断言 ---------- */

test('页面骨架：顶部标题与统计卡被渲染', async () => {
  await tick();
  const html = ensure('masthead-meta').innerHTML;
  assert.match(html, /运行时间/);
  assert.match(html, /2026-10-09 15:00:00 \+0800/);

  const stats = ensure('stats').innerHTML;
  assert.match(stats, /命中篇数/);
  assert.match(stats, /扫描条数/);
  assert.match(stats, /总请求页数/);
  assert.match(stats, />3</); // 命中 3 篇
});

test('分组：每个主题词一张表，标题是链接，时间为发布时间', async () => {
  await tick();
  const html = ensure('groups').innerHTML;
  assert.match(html, /class="section__kw">AI</);
  assert.match(html, /class="section__kw">黑客松</);
  // 标题渲染成外链
  assert.match(html, /<a class="topic-title" href="https:\/\/www\.cc98\.org\/topic\/1"[^>]*>/);
  // 发布时间列
  assert.match(html, /10-09 14:50/);
  // 已报告标记
  assert.match(html, /↻ 已报/);
  // 0 命中的词要给出说明而不是空表
  assert.match(html, /该词在本窗口内没有命中的主题帖/);
  // 异常词要打标
  assert.match(html, /class="section__flag">抓取失败</);
});

test('转义：标题里的尖括号不会变成真实标签', async () => {
  await tick();
  const html = ensure('groups').innerHTML;
  assert.ok(!html.includes('<script>'), 'HTML 未转义，存在 XSS 风险');
  assert.match(html, /&lt;script&gt;/);
});

test('侧栏：账本、配置、主题词编辑区、历史留档都有内容', async () => {
  await tick();
  assert.match(ensure('ledger').innerHTML, /命中篇数/);
  assert.match(ensure('ledger').innerHTML, /计划任务/);
  assert.match(ensure('ledger').innerHTML, /2026\/10\/9 23:00:00/);
  assert.match(ensure('config').innerHTML, /https:\/\/api\.cc98\.org/);
  // 主题词管理区：每个词一行，带改名/删除按钮
  const kwHtml = ensure('kw-list').innerHTML;
  assert.equal((kwHtml.match(/class="kw-item"/g) || []).length, 3);
  assert.match(kwHtml, /data-kw="AI"/);
  assert.match(kwHtml, /data-act="edit"/);
  assert.match(kwHtml, /data-act="del"/);
  assert.match(ensure('history').innerHTML, /cc98-digest-2026-10-09-1500\.json/);
});

test('文末：检索方式、扫描统计、告警、🔄 图例', async () => {
  await tick();
  const html = ensure('colophon').innerHTML;
  assert.match(html, /每个关键词最多 2 页/);
  assert.match(html, /AI：2 页 \/ 扫 40 条 \/ 命中 2 条/);
  assert.match(html, /class="warn">关键词「考研搭子」抓取失败/);
  assert.match(html, /↻ 已报 = 上一次运行时已报告过/);
});

test('印鉴状态随运行状态变化', async () => {
  await tick();
  assert.equal(ensure('seal-big').textContent, '毕');
  assert.equal(ensure('seal-small').textContent, '已完成');
});

test('工具栏：不在运行时不禁用按钮', async () => {
  await tick();
  assert.equal(ensure('btn-run').disabled, false);
  assert.equal(ensure('btn-run').textContent, '立即抓取');
  assert.match(ensure('toolbar-note').textContent, /完成：命中 3 条/);
});

test('日志流：EventSource 收到行后写入日志面板', async () => {
  await tick();
  assert.ok(lastEventSource, '未建立 SSE 连接');
  assert.match(lastEventSource.url, /\/api\/logs\/stream/);
  lastEventSource.emit('log', { seq: 1, text: '15:00:01 INFO  hello' });
  const body = ensure('logs-body');
  assert.equal(body.children.length, 1);
  assert.equal(body.children[0].textContent, '15:00:01 INFO  hello');
  assert.match(body.children[0].className, /INFO/);
});

test('主题词：点击词本身只显示该词，点「全部显示」恢复', async () => {
  await tick();
  const list = ensure('kw-list');
  const clickHandler = list.listeners.click[0];
  clickHandler({ target: { dataset: {}, closest: () => ({ dataset: { kw: 'AI' } }) } });
  let html = ensure('groups').innerHTML;
  assert.ok(html.includes('section__kw">AI<'));
  assert.ok(!html.includes('section__kw">黑客松<'), '筛选失效');
  assert.match(ensure('kw-filter-note').textContent, /筛选：AI/);

  ensure('kw-all').listeners.click[0]();
  html = ensure('groups').innerHTML;
  assert.ok(html.includes('section__kw">黑客松<'), '恢复全部失败');
});

test('主题词编辑器：能发出新增请求并刷新列表', async () => {
  await tick();
  const calls = [];
  const originalFetch = global.fetch;
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), method: (options && options.method) || 'GET', body: options && options.body });
    if (String(url).startsWith('/api/keywords')) {
      return {
        ok: true, status: 200,
        text: async () => JSON.stringify({ ok: true, keywords: ['AI', '黑客松', '考研搭子', '实习'] }),
      };
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(REPORT) };
  };
  try {
    const form = ensure('kw-form');
    const submit = form.listeners.submit[0];
    ensure('kw-new').value = '实习';
    await submit({ preventDefault() {} });
    const post = calls.find((c) => c.method === 'POST' && c.url.startsWith('/api/keywords'));
    assert.ok(post, '没有发出新增请求');
    assert.equal(JSON.parse(post.body).value, '实习');
    // 服务端返回的新列表应立刻反映到界面上
    assert.match(ensure('kw-list').innerHTML, /data-kw="实习"/);
    assert.equal(ensure('kw-new').value, '');
  } finally {
    global.fetch = originalFetch;
    REPORT.config = Object.assign({}, REPORT.config, { keywords: ['AI', '黑客松', '考研搭子'] });
  }
});

test('主题词编辑器：删除走 DELETE 并带上 value', async () => {
  await tick();
  const calls = [];
  const originalFetch = global.fetch;
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), method: (options && options.method) || 'GET' });
    if (String(url).startsWith('/api/keywords')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, keywords: ['AI', '黑客松'] }) };
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(REPORT) };
  };
  const originalConfirm = global.confirm;
  global.confirm = () => true;
  try {
    const item = { dataset: { kw: '考研搭子' } };
    await ensure('kw-list').listeners.click[0]({
      target: { dataset: { act: 'del' }, closest: () => item },
    });
    const del = calls.find((c) => c.method === 'DELETE');
    assert.ok(del, '没有发出删除请求');
    assert.match(del.url, /value=%E8%80%83%E7%A0%94%E6%90%AD%E5%AD%90/);
  } finally {
    global.fetch = originalFetch;
    global.confirm = originalConfirm;
  }
});

test('登录状态：已登录时显示账号并露出退出按钮', async () => {
  await tick();
  assert.match(ensure('login-status').textContent, /tester/);
  assert.equal(ensure('logout-btn').hidden, false);
});

test('登录：提交表单发出 POST /api/login 并携带账号密码', async () => {
  await tick();
  const calls = [];
  const originalFetch = global.fetch;
  global.fetch = async (url, options) => {
    const u = String(url);
    calls.push({ url: u, method: (options && options.method) || 'GET', body: options && options.body });
    if (u.startsWith('/api/login')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, account: 'newuser', expiresIn: 3599 }) };
    }
    if (u.startsWith('/api/report')) return { ok: true, status: 200, text: async () => JSON.stringify(REPORT) };
    if (u.startsWith('/api/status')) return { ok: true, status: 200, text: async () => JSON.stringify(STATUS) };
    if (u.startsWith('/api/history')) return { ok: true, status: 200, text: async () => JSON.stringify(HISTORY) };
    if (u.startsWith('/api/logs')) return { ok: true, status: 200, text: async () => JSON.stringify({ lines: [] }) };
    return { ok: true, status: 200, text: async () => JSON.stringify({}) };
  };
  try {
    const form = ensure('login-form');
    const submit = form.listeners.submit[0];
    ensure('login-username').value = 'newuser';
    ensure('login-password').value = 'secret';
    await submit({ preventDefault() {} });
    const post = calls.find((c) => c.method === 'POST' && c.url.startsWith('/api/login'));
    assert.ok(post, '没有发出登录请求');
    const body = JSON.parse(post.body);
    assert.equal(body.username, 'newuser');
    assert.equal(body.password, 'secret');
  } finally {
    global.fetch = originalFetch;
  }
});

test('退出登录：点击退出按钮发出 POST /api/logout', async () => {
  await tick();
  const calls = [];
  const originalFetch = global.fetch;
  const originalConfirm = global.confirm;
  global.confirm = () => true;
  global.fetch = async (url, options) => {
    const u = String(url);
    calls.push({ url: u, method: (options && options.method) || 'GET' });
    if (u.startsWith('/api/logout')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, account: '' }) };
    }
    if (u.startsWith('/api/report')) return { ok: true, status: 200, text: async () => JSON.stringify(REPORT) };
    if (u.startsWith('/api/status')) return { ok: true, status: 200, text: async () => JSON.stringify(STATUS) };
    if (u.startsWith('/api/history')) return { ok: true, status: 200, text: async () => JSON.stringify(HISTORY) };
    return { ok: true, status: 200, text: async () => JSON.stringify({}) };
  };
  try {
    const btn = ensure('logout-btn');
    const click = btn.listeners.click[0];
    await click();
    const post = calls.find((c) => c.method === 'POST' && c.url.startsWith('/api/logout'));
    assert.ok(post, '没有发出退出请求');
  } finally {
    global.fetch = originalFetch;
    global.confirm = originalConfirm;
  }
});
