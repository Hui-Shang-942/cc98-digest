/* CC98 主题词日报 · 抓取台
   纯原生 JS，无构建步骤、无外部依赖。数据来自 /api/*，日志走 SSE。 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    report: null,
    status: null,
    config: null,
    task: null,
    selected: new Set(),
    pollTimer: null,
    logSource: null,
    logOpen: false,
  };

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function toast(msg, kind) {
    const el = $("toast");
    el.textContent = msg;
    el.dataset.kind = kind || "info";
    el.dataset.show = "true";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.dataset.show = "false"; }, kind === "error" ? 6000 : 3200);
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
    if (!res.ok && data && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  }

  /* ---------------- 渲染：顶部 ---------------- */

  function renderMasthead() {
    const r = state.report;
    const meta = $("masthead-meta");
    if (!r || r.empty) {
      meta.innerHTML = `<span>尚无报告数据 · 点「立即抓取」</span>`;
      return;
    }
    meta.innerHTML = [
      `<span>运行时间 <b>${esc(r.runLabel)}</b></span>`,
      `<span>窗口 <b>${esc(r.windowLabel)}</b></span>`,
    ].join("");
  }

  function renderSeal() {
    const st = (state.status && state.status.status) || "idle";
    const seal = $("seal");
    const big = $("seal-big");
    const small = $("seal-small");
    seal.classList.remove("seal--busy", "seal--ok");
    if (st === "running") {
      seal.classList.add("seal--busy");
      big.textContent = "抓";
      small.textContent = "进行中";
    } else if (st === "ok") {
      seal.classList.add("seal--ok");
      big.textContent = "毕";
      small.textContent = "已完成";
    } else if (st === "error") {
      big.textContent = "误";
      small.textContent = "出错";
    } else {
      big.textContent = "待";
      small.textContent = "未抓取";
    }
  }

  function renderToolbar() {
    const st = state.status || {};
    const running = !!st.running;
    $("btn-run").disabled = running;
    $("btn-auth").disabled = running;
    $("btn-run").textContent = running ? "抓取中…" : "立即抓取";
    let note = "";
    if (st.status === "running") {
      note = `进行中 · 已 ${st.elapsed ?? 0}s`;
    } else if (st.status === "ok") {
      note = `${st.message || ""} · 用时 ${st.elapsed ?? "-"}s`;
    } else if (st.status === "error") {
      note = st.error || "上次抓取失败";
    }
    $("toolbar-note").textContent = note;
    $("logs-dot").dataset.live = running ? "true" : "false";
  }

  /* ---------------- 渲染：侧栏 ---------------- */

  function ledgerRows(rows) {
    return rows.map(([k, v, cls]) =>
      `<div class="ledger__row"><span class="ledger__key">${esc(k)}</span>` +
      `<span class="ledger__val ${cls || ""}">${v}</span></div>`).join("");
  }

  function renderLedger() {
    const r = state.report || {};
    const c = state.config || (r.config || {}) || {};
    const t = state.task || {};
    const rows = [
      ["命中篇数", `<span class="ledger__val--big">${r.totalTopics ?? "—"}</span>`],
      ["扫描条数", `${r.totalScanned ?? "—"}`],
      ["主题词", `${(c.keywords || r.keywords || []).length || "—"}`],
      ["每词页数", `${c.maxPages ?? "—"}`],
      ["窗口", `<span class="ledger__val--mono">${esc((c.hours ?? "—") + " h")}</span>`],
      ["账号", `<span class="ledger__val--mono">${esc(c.account || "—")}</span>`],
    ];
    if (t && t.available) {
      rows.push(["计划任务", `<span class="ledger__val--mono">${esc(t.state || "-")}</span>`]);
      rows.push(["下次运行", `<span class="ledger__val--mono">${esc((t.nextRun || "-").replace(" ", " "))}</span>`]);
    } else if (t && t.installed === false) {
      rows.push(["计划任务", `<span class="ledger__val--mono">未安装</span>`]);
    } else if (t && t.checking) {
      rows.push(["计划任务", `<span class="ledger__val--mono">查询中…</span>`]);
    }
    $("ledger").innerHTML = ledgerRows(rows);
  }

  function renderLogin() {
    const c = state.config || {};
    const account = c.account || "";
    const status = $("login-status");
    const logout = $("logout-btn");
    if (status) status.textContent = account ? `当前账号：${account}` : "未登录，请输入账号密码";
    if (logout) logout.hidden = !account;
  }

  function renderConfig() {
    const c = state.config || {};
    $("config").innerHTML = ledgerRows([
      ["接口", `<span class="ledger__val--mono">${esc(c.apiBase || "-")}</span>`],
      ["匹配", `${esc(c.matchStyle || "-")} / ${esc(c.matchMode || "-")}`],
      ["限速", `${esc(c.rateLimit ?? "-")} s`],
      ["输出", `<span class="ledger__val--mono">${esc((c.reportsDir || "-").split("\\").pop())}</span>`],
    ]);
  }

  function renderChips() {
    const kws = state.config ? (state.config.keywords || []) : ((state.report && state.report.keywords) || []);
    const limits = (state.config || {}).keywordLimits || { maxLen: 64, maxCount: 50 };
    const list = $("kw-list");

    if (!kws.length) {
      list.innerHTML = `<li class="kw-list__empty">还没有主题词，先在下面加一个。</li>`;
    } else {
      list.innerHTML = kws.map((k) => `
        <li class="kw-item" data-kw="${esc(k)}" data-active="${state.selected.has(k)}">
          <span class="kw-item__name">${esc(k)}</span>
          <span class="kw-item__tools">
            <button class="iconbtn" data-act="edit" title="改名">✎</button>
            <button class="iconbtn iconbtn--danger" data-act="del" title="删除">✕</button>
          </span>
        </li>`).join("");
    }

    $("kw-new").maxLength = limits.maxLen || 64;
    $("kw-add-btn").disabled = kws.length >= (limits.maxCount || 50);
    $("kw-hint").textContent =
      `点「✎」改名 · 点「✕」删除 · 点词本身可只看该词；上限 ${limits.maxCount} 个 / 每个 ${limits.maxLen} 字`;

    // 报告里有、但配置里已经删掉的词 → 明确提示，避免误解为"报告没更新"
    const reportKw = (state.report && !state.report.empty) ? (state.report.keywords || []) : [];
    const drifted = reportKw.filter((k) => !kws.includes(k));
    const note = $("kw-filter-note");
    note.textContent = state.selected.size ? `筛选：${[...state.selected].join("、")}` : "";
    const drift = $("kw-drift");
    if (drift) {
      drift.innerHTML = drifted.length
        ? `<div class="drift">报告里还有已从配置移除的词：${esc(drifted.join("、"))}（重新抓取后消失）</div>`
        : "";
    }
  }

  function renderHistory(items) {
    const el = $("history");
    if (!items || !items.length) {
      el.innerHTML = `<li class="history__meta">暂无留档</li>`;
      return;
    }
    el.innerHTML = items.map((it) => `
      <li>
        <button data-file="${esc(it.file)}" title="${esc(it.windowLabel || "")}">
          <span class="history__time">${esc((it.runLabel || "").slice(5, 16))}</span>
          <span class="history__meta">${it.totalTopics} 篇 / 扫 ${it.totalScanned}</span>
        </button>
      </li>`).join("");
  }

  /* ---------------- 渲染：统计与分组 ---------------- */

  function renderStats() {
    const r = state.report || {};
    const g = r.groups || [];
    const totalPages = g.reduce((a, x) => a + (x.pages || 0), 0);
    const cards = [
      ["命中篇数", r.empty ? "0" : (r.totalTopics ?? 0), "篇"],
      ["扫描条数", r.totalScanned ?? 0, "条"],
      ["覆盖关键词", g.filter((x) => x.count > 0).length, `/ ${g.length || 0}`],
      ["总请求页数", totalPages, "页"],
    ];
    $("stats").innerHTML = cards.map(([k, v, unit]) =>
      `<div class="stat"><div class="stat__k">${k}</div>` +
      `<div class="stat__v">${v}<small>${unit}</small></div></div>`).join("");
  }

  function renderGroups() {
    const r = state.report || {};
    const host = $("groups");
    if (r.empty) {
      host.innerHTML = `<div class="section"><div class="section__head">` +
        `<h3 class="section__kw">还没有报告</h3></div>` +
        `<p class="empty-note">${esc(r.hint || "点上方「立即抓取」跑一次。")}</p></div>`;
      return;
    }
    const groups = (r.groups || []).filter((g) => !state.selected.size || state.selected.has(g.keyword));
    if (!groups.length) {
      host.innerHTML = `<div class="section"><p class="empty-note">当前筛选没有可显示的分组。</p></div>`;
      return;
    }
    host.innerHTML = groups.map((g) => {
      const rows = (g.topics || []).map((t, i) => `
        <tr>
          <td class="idx">${String(i + 1).padStart(2, "0")}</td>
          <td><a class="topic-title" href="${esc(t.url)}" target="_blank" rel="noreferrer">${esc(t.title || "（无标题）")}</a></td>
          <td class="time">${esc(t.createdLabel)}</td>
          <td class="mark">${t.repeated ? "↻ 已报" : ""}</td>
        </tr>`).join("");
      const body = rows
        ? `<table class="list"><thead><tr><th>#</th><th>标题</th><th>发布时间</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="empty-note">该词在本窗口内没有命中的主题帖。</p>`;
      return `
        <section class="section">
          <div class="section__head">
            <h3 class="section__kw">${esc(g.keyword)}</h3>
            <span class="section__count">${g.count}</span>
            ${g.abnormal ? `<span class="section__flag">${esc(g.status)}</span>` : ""}
            <span class="section__scan">扫描 ${g.scanned} 条 · ${g.pages} 页</span>
          </div>
          ${body}
        </section>`;
    }).join("");
  }

  function renderColophon() {
    const r = state.report || {};
    const lines = [];
    if (!r.empty) {
      lines.push(`<p>检索方式：按关键词抓取 CC98 搜索结果（每个关键词最多 ${(state.config || r.config || {}).maxPages ?? "-"} 页），再在本地按发布时间筛出窗口内的帖子。</p>`);
      const scan = (r.groups || []).map((g) => `${g.keyword}：${g.pages} 页 / 扫 ${g.scanned} 条 / 命中 ${g.count} 条`).join("　·　");
      if (scan) lines.push(`<p>扫描统计：${esc(scan)}</p>`);
      if (r.parseFailures) lines.push(`<p>有 ${r.parseFailures} 条结果缺少 id 或时间字段，已跳过。</p>`);
      (r.warnings || []).forEach((w) => lines.push(`<div class="warn">${esc(w)}</div>`));
      lines.push(`<p>↻ 已报 = 上一次运行时已报告过该帖。数据源：${esc(r.host || "")} 只读接口。</p>`);
    }
    $("colophon").innerHTML = lines.join("");
  }

  /* ---------------- 数据加载 ---------------- */

  async function reload(reportFile) {
    const query = reportFile ? `?file=${encodeURIComponent(reportFile)}` : "";
    const [report, status] = await Promise.all([
      api("/api/report" + query),
      api("/api/status"),
    ]);
    state.report = report;
    state.status = status.status || status;
    state.config = (report && report.config) || status.config || null;
    state.task = status.task || null;
    renderAll();
  }

  function renderAll() {
    renderMasthead();
    renderSeal();
    renderToolbar();
    renderLedger();
    renderLogin();
    renderConfig();
    renderChips();
    renderStats();
    renderGroups();
    renderColophon();
  }

  async function loadHistory() {
    const data = await api("/api/history");
    renderHistory(data.items || []);
  }

  /* ---------------- 日志 ---------------- */

  function appendLog(line) {
    const body = $("logs-body");
    const level = (line.text.match(/\b(WARNING|ERROR|INFO|DEBUG)\b/) || [, "INFO"])[1];
    const div = document.createElement("div");
    div.className = `logs__line--${level}`;
    div.textContent = line.text;
    body.appendChild(div);
    while (body.childNodes.length > 500) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
  }

  function startLogStream() {
    if (state.logSource) return;
    const src = new EventSource("/api/logs/stream");
    src.addEventListener("log", (ev) => appendLog(JSON.parse(ev.data)));
    src.onerror = () => { /* 浏览器会自动重连 */ };
    state.logSource = src;
  }

  /* ---------------- 操作 ---------------- */

  async function triggerRun() {
    const body = {};
    if ($("opt-hours").checked) body.hours = 6;
    const res = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res && res.started) {
      state.logOpen = true;
      $("logs").dataset.open = "true";
      startLogStream();
      toast("已开始抓取，日志在下方滚动");
      pollFast();
    } else {
      toast((res && res.reason) || "无法启动抓取", "error");
    }
  }

  async function authTest() {
    toast("正在检查登录…");
    const res = await api("/api/auth-test", { method: "POST" });
    if (res && res.ok) toast(`登录正常 · ${res.account} · ${res.probe}`);
    else {
      toast(`登录异常：${(res && res.error) || "未知错误"}`, "error");
      // 登录失败多半是网络/代理问题，顺手把自检结果展开
      netCheck();
    }
  }

  /* ---------------- 网络自检 ---------------- */

  async function netCheck() {
    const panel = $("netcheck-panel");
    panel.hidden = false;
    panel.dataset.ok = "pending";
    panel.innerHTML = `<div class="netcheck__head"><span class="netcheck__title">网络自检</span>
      <span class="netcheck__badge">检测中…</span></div>`;
    toast("正在检测网络（DNS / 代理 / 连通性）…");

    const res = await api("/api/netcheck");
    if (!res || res.ok !== true) {
      panel.dataset.ok = "false";
      panel.innerHTML = `<div class="netcheck__head"><span class="netcheck__title">网络自检</span>
        <span class="netcheck__badge" data-ok="false">失败</span></div>
        <div class="netcheck__row"><span class="netcheck__key">错误</span>
        <span class="netcheck__val netcheck__val--bad">${esc((res && res.error) || "接口不可用")}</span></div>`;
      return;
    }

    const sysProxy = (res.systemProxy && (res.systemProxy.https || res.systemProxy.http)) || "（未设置）";
    const dnsRows = Object.entries(res.dns || {}).map(([label, info]) => {
      if (info.error) {
        return `<div class="netcheck__row"><span class="netcheck__key">${esc(label)}</span>
          <span class="netcheck__val netcheck__val--bad">${esc(info.host || "")} 解析失败：${esc(info.error)}</span></div>`;
      }
      const ips = (info.ips || []).join(", ") || "-";
      return `<div class="netcheck__row"><span class="netcheck__key">${esc(label)}</span>
        <span class="netcheck__val ${info.fake_ip ? "netcheck__val--warn" : ""}">
          ${esc(info.host || "")} → ${esc(ips)}${info.fake_ip ? "（代理 fake-ip 网段，不是真实地址）" : ""}
        </span></div>`;
    }).join("");

    const probeRows = (res.probes || []).map((p) => `
      <div class="netcheck__row">
        <span class="netcheck__key">${p.ok ? "OK" : "FAIL"} · ${esc(p.label)}</span>
        <span class="netcheck__val ${p.ok ? "" : "netcheck__val--bad"}">${esc(p.detail)} <small>(${p.elapsedMs}ms)</small></span>
      </div>`).join("");

    const verdict = res.allOk
      ? `<div class="netcheck__row"><span class="netcheck__key">结论</span>
           <span class="netcheck__val">网络可达，可以登录/抓取。</span></div>`
      : `<div class="netcheck__row"><span class="netcheck__key">结论</span>
           <span class="netcheck__val netcheck__val--bad">连不上 CC98，按下面顺序排查：</span></div>
         <ul class="netcheck__list">
           <li>必须处于校园网 / RVPN / 校内 IPv6——CC98 只在浙大内网提供。</li>
           <li>关掉 Clash / v2ray 等代理，或把 <code>*.cc98.org</code> 设为 DIRECT；代理的 fake-ip 会把域名解析成 198.18.x.x。</li>
           <li>确认 <code>config.json</code> 里 <code>runtime.proxy</code> 是 <code>direct</code>（本项目默认）。</li>
           <li>防火墙放行 python；真实地址应为 <code>10.10.98.98</code>。</li>
         </ul>`;

    panel.dataset.ok = res.allOk ? "true" : "false";
    panel.innerHTML = `
      <div class="netcheck__head">
        <span class="netcheck__title">网络自检</span>
        <span class="netcheck__badge" data-ok="${res.allOk}">${res.allOk ? "可用" : "不可用"}</span>
        <button class="netcheck__close" id="netcheck-close" title="收起">✕</button>
      </div>
      <div class="netcheck__row"><span class="netcheck__key">代理策略</span>
        <span class="netcheck__val">${esc(res.proxyMode)}${res.proxyMode === "direct" ? "（忽略系统代理）" : ""}</span></div>
      <div class="netcheck__row"><span class="netcheck__key">系统代理</span>
        <span class="netcheck__val">${esc(sysProxy)}</span></div>
      ${dnsRows}
      ${probeRows}
      ${verdict}`;

    const close = $("netcheck-close");
    if (close) close.addEventListener("click", () => { panel.hidden = true; });
    if (!res.allOk) {
      toast("网络不可达：请检查是否在校园网/RVPN，以及是否开了代理", "error");
    }
  }

  function pollFast() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      const status = await api("/api/status");
      state.status = status.status || null;
      state.task = status.task || state.task;
      renderSeal();
      renderToolbar();
      renderLedger();
      if (state.status && !state.status.running) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        await reload();
        await loadHistory();
        toast(state.status.status === "ok" ? "抓取完成，已刷新" : `抓取失败：${state.status.error || ""}`,
              state.status.status === "ok" ? "info" : "error");
      }
    }, 1500);
  }

  async function keywordOp(method, payload, query) {
    const res = await api("/api/keywords" + (query || ""), {
      method,
      headers: { "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    if (res && res.ok) {
      state.config = Object.assign({}, state.config, { keywords: res.keywords });
      const names = new Set(res.keywords);
      [...state.selected].forEach((k) => { if (!names.has(k)) state.selected.delete(k); });
      renderChips();
      renderGroups();
      renderLedger();
      toast("已保存到 config.json");
    } else {
      toast((res && res.error) || "操作失败", "error");
    }
    return res;
  }

  /* ---------------- 事件绑定 ---------------- */

  $("btn-run").addEventListener("click", () => triggerRun());
  $("btn-refresh").addEventListener("click", async () => { await reload(); await loadHistory(); toast("已刷新"); });
  $("btn-auth").addEventListener("click", authTest);
  $("btn-netcheck").addEventListener("click", netCheck);

  // 账号登录 / 退出
  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const username = $("login-username").value.trim();
    const password = $("login-password").value;
    if (!username || !password) { toast("请输入用户名和密码", "error"); return; }
    const btn = $("login-btn");
    btn.disabled = true;
    try {
      const res = await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (res && res.ok) {
        $("login-password").value = "";
        toast(`登录成功 · ${res.account}`);
        await reload();
        await loadHistory();
      } else {
        toast((res && res.error) || "登录失败", "error");
      }
    } finally {
      btn.disabled = false;
    }
  });

  $("logout-btn").addEventListener("click", async () => {
    if (!confirm("退出登录？将清除本机保存的账号密码。")) return;
    const res = await api("/api/logout", { method: "POST" });
    if (res && res.ok) {
      $("login-username").value = "";
      $("login-password").value = "";
      toast("已退出登录");
      await reload();
      await loadHistory();
    } else {
      toast((res && res.error) || "退出失败", "error");
    }
  });

  // 主题词：增
  $("kw-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("kw-new");
    const value = input.value.trim();
    if (!value) return;
    const res = await keywordOp("POST", { value });
    if (res && res.ok) input.value = "";
    input.focus();
  });

  // 主题词：删 / 改 / 筛选
  $("kw-list").addEventListener("click", async (ev) => {
    const item = ev.target.closest(".kw-item");
    if (!item) return;
    const kw = item.dataset.kw;
    const act = ev.target.dataset ? ev.target.dataset.act : null;

    if (act === "del") {
      if (!confirm(`删除主题词「${kw}」？`)) return;
      await keywordOp("DELETE", null, `?value=${encodeURIComponent(kw)}`);
      return;
    }
    if (act === "edit") {
      startRename(item, kw);
      return;
    }
    // 点词本身 = 筛选
    if (state.selected.has(kw)) state.selected.delete(kw); else state.selected.add(kw);
    renderChips();
    renderGroups();
  });

  function startRename(item, kw) {
    item.dataset.editing = "true";
    item.innerHTML = `
      <input class="kw-item__input" value="${esc(kw)}" maxlength="64" aria-label="新名称">
      <span class="kw-item__tools">
        <button class="iconbtn" data-act="save" title="保存">✓</button>
        <button class="iconbtn" data-act="cancel" title="取消">✕</button>
      </span>`;
    const input = item.querySelector(".kw-item__input");
    input.focus();
    input.select();

    const save = async () => {
      const next = input.value.trim();
      if (!next || next === kw) { renderChips(); return; }
      await keywordOp("PATCH", { target: kw, newValue: next });
    };
    item.querySelector('[data-act="save"]').addEventListener("click", save);
    item.querySelector('[data-act="cancel"]').addEventListener("click", () => renderChips());
    input.addEventListener("keydown", (ev2) => {
      if (ev2.key === "Enter") { ev2.preventDefault(); save(); }
      if (ev2.key === "Escape") { ev2.preventDefault(); renderChips(); }
    });
    input.addEventListener("blur", () => {
      // 失焦时不自动保存，避免误改；但要点在保存/取消上也算失焦，交给上面的处理器
      const still = item.dataset.editing === "true";
      if (still) setTimeout(() => { if (item.dataset.editing === "true") renderChips(); }, 160);
    });
  }

  $("kw-all").addEventListener("click", () => {
    state.selected.clear();
    renderChips();
    renderGroups();
  });

  $("history").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-file]");
    if (!btn) return;
    await reload(btn.dataset.file.replace(/\.json$/, ".json"));
    toast("已载入历史留档");
  });

  $("logs-bar").addEventListener("click", () => {
    state.logOpen = !state.logOpen;
    $("logs").dataset.open = state.logOpen ? "true" : "false";
    if (state.logOpen) startLogStream();
  });

  /* ---------------- 启动 ---------------- */

  (async function boot() {
    startLogStream();
    try {
      await reload();
      await loadHistory();
    } catch (err) {
      toast(`初始化失败：${err}`, "error");
    }
    // 兜底轮询：抓取中每 1.5s，其它情况每 20s 对齐一次服务端状态
    // （Node 下跑渲染单测时没有 window，跳过定时器，避免进程无法退出）
    if (typeof window === "undefined") return;
    window.__cc98FallbackTimer = setInterval(async () => {
      if (state.pollTimer) return;
      const status = await api("/api/status");
      state.status = status.status || null;
      state.task = status.task || null;
      renderSeal();
      renderToolbar();
      if (state.status && state.status.running) pollFast();
    }, 20000);
  })();
})();
