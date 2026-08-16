/* GOAI / 研究终端
 * 前端只负责格式化、状态切换和键盘工作流；数字与结论来自 Python 服务。
 * 内部枚举只保留在 data-system-code 属性中，不直接作为用户界面文案。
 */
(function () {
  "use strict";

  var DEFAULT = window.GOAI_DATA || null;
  var state = {
    mode: "static",
    view: "overview",
    chartMode: "pnl",
    expiry: null,
    data: DEFAULT,
    projectBusy: false,
    agent: { sequence: 0, busy: false, history: [], context: null, restored: false },
    stream: { es: null, key: "", connected: false, closing: false, refreshTimer: null, lastError: null, liveQuotes: {} },
    submitReceipt: null,
  };
  var AGENT_SESSION_KEY = "goai-agent-session-v2";
  var PROJECT_STORAGE_KEY = "goai-active-project-v1";
  var THEME_STORAGE_KEY = "goai-theme-v1";
  var CONTRAST_STORAGE_KEY = "goai-contrast-v1";
  var THEME_PRESETS = [
    { id: "monokai-dimmed", name: "Monokai Dimmed", description: "低饱和石墨 / 经典终端色", canvas: "#1e1e1e", swatches: ["#1e1e1e", "#2a2b2b", "#a6e22e"] },
    { id: "monokai-classic", name: "Monokai Classic", description: "暖灰底 / 绿紫语义色", canvas: "#272822", swatches: ["#272822", "#3b3c33", "#a6e22e"] },
    { id: "graphite-contrast", name: "高对比石墨", description: "更亮文字 / 最清晰", canvas: "#0f1112", swatches: ["#0f1112", "#202426", "#7ee787"] },
    { id: "cool-cyan", name: "冷青终端", description: "冷色工作台 / 青色图表", canvas: "#101820", swatches: ["#101820", "#1d2d35", "#7dd3fc"] },
    { id: "amber-crt", name: "Amber CRT", description: "暖琥珀 / 老式研究台", canvas: "#19130e", swatches: ["#19130e", "#3a291c", "#f4b860"] },
    { id: "rose-pulse", name: "Rose Pulse", description: "深酒红 / 玫瑰焦点", canvas: "#1b1117", swatches: ["#1b1117", "#38202c", "#ff8fa3"] },
    { id: "violet-night", name: "Violet Night", description: "蓝紫夜色 / 冷静聚焦", canvas: "#11101c", swatches: ["#11101c", "#27233b", "#c4b5fd"] },
    { id: "forest-moss", name: "Forest Moss", description: "深林青苔 / 低刺激", canvas: "#101912", swatches: ["#101912", "#23362a", "#a3d977"] },
    { id: "electric-blue", name: "Electric Blue", description: "深海蓝 / 明快图表", canvas: "#0b1422", swatches: ["#0b1422", "#1c2f47", "#7db7ff"] },
    { id: "solarized-night", name: "Solarized Night", description: "靛蓝底 / 金黄重点", canvas: "#002b36", swatches: ["#002b36", "#073642", "#e4bf55"] },
  ];

  function el(id) { return document.getElementById(id); }
  function clear(id) { var node = el(id); if (node) node.innerHTML = ""; }
  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function findTheme(id) {
    return THEME_PRESETS.filter(function (theme) { return theme.id === id; })[0] || THEME_PRESETS[0];
  }
  function storedSetting(key, fallback) {
    try { return window.localStorage.getItem(key) || fallback; } catch (error) { return fallback; }
  }
  function syncThemeControls(themeId) {
    document.querySelectorAll(".theme-option").forEach(function (button) {
      var active = button.dataset.theme === themeId;
      button.classList.toggle("active", active);
      button.setAttribute("aria-checked", String(active));
    });
  }
  function applyTheme(themeId, announce) {
    var theme = findTheme(themeId);
    document.documentElement.dataset.theme = theme.id;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme.canvas);
    try { window.localStorage.setItem(THEME_STORAGE_KEY, theme.id); } catch (error) { /* local fallback */ }
    syncThemeControls(theme.id);
    if (el("settings-theme-current")) el("settings-theme-current").textContent = theme.name;
    if (announce) note("已切换主题：" + theme.name);
  }
  function applyContrast(enabled, announce) {
    var high = Boolean(enabled);
    document.documentElement.dataset.contrast = high ? "high" : "normal";
    try { window.localStorage.setItem(CONTRAST_STORAGE_KEY, high ? "high" : "normal"); } catch (error) { /* local fallback */ }
    var checkbox = el("contrast-toggle");
    if (checkbox) checkbox.checked = high;
    if (el("settings-contrast-status")) el("settings-contrast-status").textContent = high ? "已强化" : "标准";
    if (announce) note(high ? "已强化文字对比度。" : "已恢复标准文字对比度。 ");
  }
  function renderThemeOptions() {
    var wrap = el("theme-options");
    if (!wrap) return;
    clear("theme-options");
    THEME_PRESETS.forEach(function (theme) {
      var button = make("button", "theme-option");
      button.type = "button";
      button.dataset.theme = theme.id;
      button.setAttribute("role", "radio");
      button.setAttribute("aria-label", theme.name + "：" + theme.description);
      var swatch = make("span", "theme-swatch");
      theme.swatches.forEach(function (color) {
        var chip = make("i", "theme-swatch-chip");
        chip.style.backgroundColor = color;
        swatch.appendChild(chip);
      });
      button.appendChild(swatch);
      var copy = make("span", "theme-option-copy");
      copy.appendChild(make("strong", null, theme.name));
      copy.appendChild(make("small", null, theme.description));
      button.appendChild(copy);
      button.addEventListener("click", function () { applyTheme(theme.id, true); });
      wrap.appendChild(button);
    });
    syncThemeControls(document.documentElement.dataset.theme || THEME_PRESETS[0].id);
  }
  function setSettingsOpen(open) {
    var drawer = el("settings-drawer");
    var toggle = el("settings-toggle");
    if (!drawer) return;
    drawer.classList.toggle("open", open);
    drawer.setAttribute("aria-hidden", String(!open));
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      var first = drawer.querySelector(".theme-option");
      if (first) first.focus();
    }
  }
  function bindSettings() {
    renderThemeOptions();
    applyTheme(storedSetting(THEME_STORAGE_KEY, "monokai-dimmed"), false);
    applyContrast(storedSetting(CONTRAST_STORAGE_KEY, "high") !== "normal", false);
    if (el("settings-toggle")) el("settings-toggle").addEventListener("click", function () { setSettingsOpen(!el("settings-drawer").classList.contains("open")); });
    if (el("settings-close")) el("settings-close").addEventListener("click", function () { setSettingsOpen(false); });
    if (el("contrast-toggle")) el("contrast-toggle").addEventListener("change", function () { applyContrast(el("contrast-toggle").checked, true); });
  }
  function fmt(value, digits) {
    var n = Number(value);
    if (!isFinite(n)) return "--";
    return n.toLocaleString("zh-CN", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0,
    });
  }
  function fmtSigned(value, digits) {
    var n = Number(value);
    if (!isFinite(n)) return "--";
    return (n > 0 ? "+" : "") + fmt(n, digits);
  }
  function fmtPct(value, digits) {
    var n = Number(value);
    return isFinite(n) ? fmtSigned(n, digits === undefined ? 1 : digits) + "%" : "--";
  }
  function signClass(value) { return Number(value) > 0 ? "pos" : Number(value) < 0 ? "neg" : ""; }
  function humanMode(value) { return { LIVE: "实时", REPLAY: "回放", STATIC: "本地" }[value] || value || "未知"; }
  function humanFreshness(value) { return { FRESH: "最新", FROZEN: "冻结", STALE: "过期" }[value] || value || "未知"; }
  function humanVerdict(value) {
    return {
      NO_TRADE: "这次先不交易",
      BLOCK: "被风险拦截",
      DRAFT_ONLY: "暂存为草案",
      READY_FOR_CONFIRMATION: "可以进入确认",
    }[value] || "暂未形成结论";
  }
  function humanGate(value, kind) {
    if (kind === "edge") return value === "ADEQUATE" ? "机会成立" : value === "LOW_EDGE" ? "机会不足" : "待判断";
    if (kind === "risk") return value === "BLOCK" ? "风险拦截" : value === "PASS" ? "风险可控" : "待判断";
    return humanVerdict(value);
  }
  function humanCheck(value) { return { PASS: "已通过", FAIL: "未通过", WARN: "需留意", NOTE: "说明", BLOCK: "已拦截" }[value] || "待核验"; }
  function humanSymbol(value) {
    var symbol = String(value || "").trim().toUpperCase();
    var match = symbol.match(/^([A-Z]{2})\.(.+)$/);
    return match ? match[2] + "." + match[1] : symbol || "--";
  }
  function humanSource(value) { return String(value || "快照来源").replace(/futuapi\/OpenD 127\.0\.0\.1:11111/g, "行情连接").replace(/(?:self-built engine|本地计算引擎)\s*[（(]BS\/二叉树 \+ IV 二分 \+ bump-and-reprice[）)]/g, "本地计算引擎（期权定价与压力重估）").replace(/self-built engine/g, "本地计算引擎").replace(/\bIV Rank\b/g, "IV 分位").replace(/\bIV Pct\b/g, "IV 百分位"); }
  function humanCoverage(value) { return String(value || "").replace(/\bATM\b/g, "平值").replace(/\bOTM\b/g, "虚值").replace(/\bITM\b/g, "实值").replace(/仅录制\s+平值\s+合约/g, "仅录制平值合约"); }
  function humanHealth(value) { return { VERIFIED: "已核验", PENDING: "待核验", FAILED: "核验失败", UNKNOWN: "未知" }[value] || value || "未知"; }
  function humanEventStatus(value) { return { ACTIVE: "已启用", FAILED: "核验失败", PENDING: "待核验", DRAFT: "草案" }[value] || value || "未知"; }
  function humanTraceStatus(value) { return { ok: "已完成", complete: "已完成", error: "失败", timeout: "超时", pending: "进行中" }[String(value || "").toLowerCase()] || "待核验"; }
  function humanSummary(text) {
    var raw = String(text || "--");
    var match = raw.match(/^方向不确定观点、(.+?)：Edge 门 LOW_EDGE（(\d+) 项 FAIL），Risk 门 PASS，最终判定 NO_TRADE（不交易）：Edge 门未过：(.+)$/);
    if (match) return "方向未定，" + match[1] + "：机会不足（" + match[2] + " 项未通过）；风险可控。原因：" + match[3] + "。";
    return raw.replace(/\bNO_TRADE\b/g, "先不交易").replace(/\bLOW_EDGE\b/g, "机会不足").replace(/\bDRAFT_ONLY\b/g, "暂存为草案").replace(/\bREADY_FOR_CONFIRMATION\b/g, "可以进入确认").replace(/\bBLOCK\b/g, "风险拦截").replace(/\bFAIL\b/g, "未通过").replace(/\bPASS\b/g, "已通过").replace(/Edge 门/g, "机会判断").replace(/Risk 门/g, "风险判断").replace(/Action 门/g, "下一步").replace(/\bEdge\b/g, "机会").replace(/\bRisk\b/g, "风险").replace(/\bLive\b/g, "实时").replace(/\bpolicy\b/g, "规则").replace(/\bexecutable\b/g, "可执行").replace(/\bIV Rank\b/g, "IV 分位").replace(/\bIV Pct\b/g, "IV 百分位").replace(/bump-and-reprice/g, "压力重估");
  }
  function humanReason(text) {
    return humanSummary(String(text || "")).replace(/Edge 门未过/g, "机会不足").replace(/Risk 门未过/g, "风险判断未通过").replace(/数据为冻结快照\/回放，非 Live 新鲜数据/g, "当前是冻结快照，不是实时数据").replace(/未获得用户独立确认/g, "还没有得到你的独立确认").replace(/futuapi\/OpenD 127\.0\.0\.1:11111/g, "行情连接").replace(/(?:self-built engine|本地计算引擎)\s*[（(]BS\/二叉树 \+ IV 二分 \+ bump-and-reprice[）)]/g, "本地计算引擎（期权定价与压力重估）").replace(/self-built engine/g, "本地计算引擎").replace(/\bUNVERIFIED\b/g, "未核验").replace(/\bget_max_trd_qtys\b/g, "账户可交易数量").replace(/机会判断未过/g, "机会不足").replace(/由 机会判断对比判定/g, "由机会与盈亏平衡的对比判定").replace(/最大亏损（可执行 口径）/g, "最大亏损（按可执行成本估算）").replace(/费用\/滑点 规则 未冻结，可执行 成本当前以 ask 计（状态 未核验）/g, "费用与滑点规则尚未冻结；当前按卖方报价估算").replace(/持仓限额\/LOP 与 账户可交易数量 未在本次冻结快照中验证；进入模拟提交前必须用 实时 账户复核/g, "持仓限额尚未在本次冻结快照中核验；提交模拟动作前需用实时账户复核").replace(/实时 行情/g, "实时行情").replace(/规则 后/g, "规则后").replace(/可执行 成本/g, "可执行成本").replace(/机会 结论/g, "机会结论");
  }
  function setHuman(id, text, systemCode) {
    var node = el(id);
    if (!node) return;
    node.textContent = text;
    if (systemCode && systemCode !== text) node.setAttribute("data-system-code", systemCode);
    else node.removeAttribute("data-system-code");
  }
  function note(text) { if (el("footnote-chip")) el("footnote-chip").textContent = text; }

  // ---- Live stream (SSE) 客户端 -------------------------------------------
  // LIVE 模式订阅 /api/stream：quote 事件就地更新报价条并防抖重拉 /api/state，
  // refresh 事件（POST 重跑后）立即重拉；断线由 EventSource 自动重连。
  function setStreamChip(text, tone) {
    var chip = el("stream-chip");
    if (!chip) return;
    chip.textContent = text || "";
    chip.dataset.tone = tone || "";
    if (!text) chip.classList.add("sr-only");
    else chip.classList.remove("sr-only");
  }
  function scheduleLiveStateRefresh(delay) {
    if (state.stream.refreshTimer) clearTimeout(state.stream.refreshTimer);
    state.stream.refreshTimer = setTimeout(function () {
      state.stream.refreshTimer = null;
      fetch("/api/state", { headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) { if (data) render(data); })
        .catch(function () { /* 保持上一次渲染结果 */ });
    }, delay == null ? 700 : delay);
  }
  function applyQuotePayload(payload) {
    var quotes = (payload && payload.quotes) || [];
    var underlying = state.data && state.data.underlying && String(state.data.underlying.code || "").toUpperCase();
    var changed = false;
    quotes.forEach(function (row) {
      var code = String(row.code || "").toUpperCase();
      state.stream.liveQuotes[code] = row;
      if (code === underlying && row.last != null) {
        changed = true;
        if (el("t-spot")) el("t-spot").textContent = fmt(row.last, 2);
        if (el("t-chg") && row.prevClose) {
          var changePct = (Number(row.last) - Number(row.prevClose)) / Number(row.prevClose) * 100;
          if (isFinite(changePct)) {
            el("t-chg").textContent = fmtPct(changePct, 2);
            el("t-chg").className = signClass(changePct);
          }
        }
      }
    });
    if (changed) scheduleLiveStateRefresh(700);
  }
  function connectLiveStream() {
    if (!("EventSource" in window)) { setStreamChip("不支持实时推送", "bad"); return; }
    var es = new EventSource("/api/stream");
    state.stream.es = es;
    setStreamChip("实时推送连接中", "");
    es.onopen = function () {
      state.stream.connected = true;
      state.stream.lastError = null;
      setStreamChip("实时推送已连接", "good");
    };
    es.addEventListener("hello", function () { state.stream.connected = true; });
    es.addEventListener("quote", function (event) {
      try { applyQuotePayload(JSON.parse(event.data)); } catch (error) { /* 忽略坏帧 */ }
    });
    es.addEventListener("refresh", function () { scheduleLiveStateRefresh(0); });
    es.addEventListener("warning", function (event) {
      setStreamChip("推送已回退轮询", "warn");
      try {
        var payload = JSON.parse(event.data);
        if (payload && payload.message) note(payload.message);
      } catch (error) { /* 忽略坏帧 */ }
    });
    es.addEventListener("error", function (event) {
      try {
        var payload = JSON.parse(event.data);
        setStreamChip("实时行情不可用", "bad");
        if (payload && payload.message && state.stream.lastError !== payload.message) {
          state.stream.lastError = payload.message;
          note(payload.message);
        }
      } catch (error) { /* 连接级错误走 onerror，不重复提示 */ }
    });
    es.onerror = function () {
      if (!state.stream.closing) {
        state.stream.connected = false;
        setStreamChip("实时推送重连中", "warn");
      }
    };
  }
  function stopLiveStream() {
    state.stream.closing = true;
    if (state.stream.es) { try { state.stream.es.close(); } catch (error) { /* no-op */ } state.stream.es = null; }
    state.stream.closing = false;
    state.stream.key = "";
    state.stream.connected = false;
    if (state.stream.refreshTimer) { clearTimeout(state.stream.refreshTimer); state.stream.refreshTimer = null; }
    setStreamChip("", "");
  }
  function syncLiveStream(D) {
    var meta = (D && D.meta) || {};
    var underlying = (D && D.underlying) || {};
    var workspace = (D && D.workspace) || {};
    if (meta.mode !== "LIVE") { stopLiveStream(); return; }
    var key = "LIVE|" + (String(underlying.code || "").toUpperCase()) + "|" + (workspace.activeProjectId || "");
    if (state.stream.key === key && state.stream.es) return;
    stopLiveStream();
    state.stream.key = key;
    state.stream.liveQuotes = {};
    connectLiveStream();
  }
  function saveAgentSession() {
    try {
      window.sessionStorage.setItem(AGENT_SESSION_KEY, JSON.stringify({
        history: state.agent.history.slice(-40),
        context: state.agent.context,
      }));
    } catch (error) { /* sessionStorage may be disabled in a local file fallback */ }
  }
  function restoreAgentSession() {
    if (state.agent.restored) return;
    state.agent.restored = true;
    try {
      var raw = window.sessionStorage.getItem(AGENT_SESSION_KEY);
      var saved = raw ? JSON.parse(raw) : null;
      if (saved && Array.isArray(saved.history)) state.agent.history = saved.history.filter(function (item) { return item && item.role && item.text; }).slice(-40);
      if (saved && saved.context && saved.context.scenario) state.agent.context = saved.context;
    } catch (error) { state.agent.history = []; state.agent.context = null; }
  }
  function clearAgentSession() {
    state.agent.history = [];
    try { window.sessionStorage.removeItem(AGENT_SESSION_KEY); } catch (error) { /* no-op */ }
    clear("chat-log");
    updateAgentSessionMeta(state.data);
    note("对话已清空，当前行情工作集保留。");
  }
  function tick() {
    var now = new Date();
    if (el("clock")) el("clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }
  tick();
  setInterval(tick, 1000);

  function currentExpiry(D) {
    var list = D && D.expiries ? D.expiries : [];
    return list.filter(function (item) { return item.expiry === state.expiry; })[0] || list[0] || null;
  }
  function setTone(id, tone) {
    var node = el(id);
    if (!node) return;
    node.classList.remove("good", "bad", "pos", "neg");
    if (tone) node.classList.add(tone);
  }

  function renderTopbar(D) {
    var meta = D.meta || {};
    var terminal = D.terminal || {};
    var modeChip = el("mode-chip");
    if (modeChip) {
      var topMode = meta.mode === "LIVE" || meta.mode === "REPLAY" ? humanMode(meta.mode) : "回放";
      var topFreshness = meta.freshness ? humanFreshness(meta.freshness) : "冻结";
      modeChip.textContent = topMode + " / " + topFreshness;
      modeChip.dataset.tone = meta.freshness === "FRESH" ? "good" : meta.freshness === "STALE" ? "warn" : "";
    }
    var llm = D.llm || {};
    var llmChip = el("llm-chip");
    if (llmChip) {
      llmChip.textContent = llm.available ? "说明 / 在线" : "说明 / 本地";
      llmChip.dataset.tone = llm.available ? "good" : "warn";
    }
    var connection = el("connection-state");
    if (connection) connection.textContent = state.mode === "api" ? "已连接" : "静态回退";
    var u = D.underlying || {};
    var e = D.earnings || {};
    var t = D.terminal || {};
    var q = t.quote || {};
    var m = t.quoteMetrics || {};
    if (el("instrument-name")) el("instrument-name").textContent = q.name || u.name || "标的";
    var displaySymbol = humanSymbol(q.symbol || u.code);
    if (el("instrument-code")) el("instrument-code").textContent = displaySymbol;
    if (el("terminal-command") && document.activeElement !== el("terminal-command") && displaySymbol !== "--") el("terminal-command").value = displaySymbol + " <GO>";
    if (el("quote-currency")) el("quote-currency").textContent = q.currency || "--";
    var changePct = q.changePct != null ? q.changePct : ((u.spot - u.prevClose) / u.prevClose * 100);
    if (el("t-spot")) el("t-spot").textContent = fmt(q.spot != null ? q.spot : u.spot, 2);
    if (el("t-chg")) { el("t-chg").textContent = fmtPct(changePct, 2); el("t-chg").className = signClass(changePct); }
    if (el("t-iv")) el("t-iv").textContent = fmt(m.iv != null ? m.iv : e.iv, 1) + "%";
    if (el("t-ivrank")) el("t-ivrank").textContent = fmt(m.ivRank != null ? m.ivRank : e.ivRank, 1);
    if (el("t-hv30")) el("t-hv30").textContent = fmt(m.historicalHv30d != null ? m.historicalHv30d : (m.hv30d != null ? m.hv30d : e.hv30d), 1) + "%";
    if (el("t-move")) el("t-move").textContent = "±" + fmt(e.expectedMovePct, 2) + "%";
    if (el("t-earnings")) el("t-earnings").textContent = e.date || "--";
    if (el("t-prev")) el("t-prev").textContent = fmt(q.prevClose != null ? q.prevClose : u.prevClose, 2);
    if (el("ov-meta")) el("ov-meta").textContent = (meta.capturedAt || terminal.session && terminal.session.capturedAt || "--") + " / " + humanSource(meta.source || terminal.session && terminal.session.source);
  }

  function renderRail(D) {
    var terminal = D.terminal || {};
    var decision = terminal.decision || D.decisionCard || {};
    var risk = terminal.risk || {};
    var event = terminal.event || {};
    var expiry = currentExpiry(D);
    var strategy = expiry && expiry.strategy || {};
    var account = D.account || {};
    setHuman("rail-verdict", humanVerdict(decision.verdict), decision.verdict);
    if (el("rail-summary")) el("rail-summary").textContent = humanSummary(decision.summary);
    if (el("rail-plan")) el("rail-plan").textContent = strategy.lots != null ? strategy.lots + " 张 @ " + fmt(expiry.strike, 0) : "--";
    if (el("rail-risk")) el("rail-risk").textContent = fmt(risk.budgetHkd != null ? risk.budgetHkd : account.cashHkd * account.riskBudgetPct / 100) + " HKD";
    if (el("rail-expiry")) el("rail-expiry").textContent = expiry ? expiry.expiry.slice(5) + " / " + expiry.dte + " 天" : "--";
    var edge = decision.edge || decision.edgeGate || {};
    var riskGate = decision.risk || decision.riskGate || {};
    var action = decision.action || decision.actionGate || {};
    setHuman("rail-edge", humanGate(edge.verdict, "edge"), edge.verdict);
    setHuman("rail-risk-state", humanGate(riskGate.decision, "risk"), riskGate.decision);
    setHuman("rail-action", humanGate(action.action, "action"), action.action);
    setTone("rail-edge", edge.verdict === "ADEQUATE" ? "good" : "bad");
    setTone("rail-risk-state", riskGate.decision === "PASS" ? "good" : "bad");
    setTone("rail-action", action.action === "READY_FOR_CONFIRMATION" ? "good" : "bad");
    if (el("rail-event-date")) el("rail-event-date").textContent = event.date || (D.earnings || {}).date || "--";
    if (el("rail-event-label")) el("rail-event-label").textContent = event.label || "业绩事件";
    if (el("rail-event-move")) el("rail-event-move").textContent = event.expectedMovePct != null ? "±" + fmt(event.expectedMovePct, 2) + "%" : "--";
    if (el("rail-symbol")) el("rail-symbol").textContent = humanSymbol((D.underlying || {}).code);
    if (el("rail-case-event")) el("rail-case-event").textContent = event.date || "--";
    if (el("rail-account")) el("rail-account").textContent = fmt(account.cashHkd) + " HKD";
    if (el("rail-budget")) el("rail-budget").textContent = fmt(account.riskBudgetPct, 1) + "%";
    clear("rail-evidence");
    (decision.evidence || decision.keyEvidence || []).slice(0, 3).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", "claim", humanReason(item.claim)));
      li.appendChild(make("span", "src", humanSource(item.source)));
      el("rail-evidence").appendChild(li);
    });
  }

  function renderGates(D) {
    var card = D.decisionCard || {};
    var edge = card.edgeGate || {};
    var risk = card.riskGate || {};
    var action = card.actionGate || {};
    setHuman("edge-verdict", humanGate(edge.verdict, "edge"), edge.verdict);
    setHuman("risk-verdict", humanGate(risk.decision, "risk"), risk.decision);
    setHuman("action-verdict", humanGate(action.action, "action"), action.action);
    setTone("edge-verdict", edge.verdict === "ADEQUATE" ? "good" : "bad");
    setTone("risk-verdict", risk.decision === "PASS" ? "good" : "bad");
    setTone("action-verdict", action.action === "READY_FOR_CONFIRMATION" ? "good" : "bad");
    var edgeChecks = el("edge-checks");
    if (edgeChecks) {
      edgeChecks.innerHTML = "";
      (edge.checks || []).forEach(function (check) {
        var li = document.createElement("li");
        li.appendChild(make("span", "result " + String(check.result || "").toLowerCase(), humanCheck(check.result)));
        li.appendChild(make("span", null, humanReason((check.check || "") + ": " + (check.detail || ""))));
        edgeChecks.appendChild(li);
      });
    }
    var riskChecks = el("risk-checks");
    if (riskChecks) {
      riskChecks.innerHTML = "";
      (risk.findings || []).forEach(function (finding) {
        var li = document.createElement("li");
        li.appendChild(make("span", "result " + String(finding.kind || "").toLowerCase(), humanCheck(finding.kind)));
        li.appendChild(make("span", null, humanReason(finding.text)));
        riskChecks.appendChild(li);
      });
    }
    if (el("action-next")) el("action-next").textContent = action.nextStep || "未提供下一步";
    var submitBlock = el("submit-block");
    if (submitBlock) {
      var ready = action.action === "READY_FOR_CONFIRMATION";
      submitBlock.hidden = !ready;
      if (ready && !state.submitReceipt) {
        if (el("submit-status")) { el("submit-status").textContent = ""; el("submit-status").dataset.tone = ""; }
        if (el("submit-confirm-text")) el("submit-confirm-text").value = "";
      }
    }
  }

  function renderCapability(D) {
    var meta = D.meta || {};
    var underlying = D.underlying || {};
    var slice = meta.mode === "LIVE" ? "P0b · 连接只读" : "P0a · Replay 只读";
    if (el("cap-slice")) el("cap-slice").textContent = slice;
    if (el("cap-mode")) {
      var modeLabel = meta.mode === "LIVE" || meta.mode === "REPLAY" ? humanMode(meta.mode) : "回放";
      var freshnessLabel = meta.freshness ? humanFreshness(meta.freshness) : "冻结";
      el("cap-mode").textContent = modeLabel + " / " + freshnessLabel + " 快照";
      var frozen = meta.freshness === "FROZEN" || meta.mode !== "LIVE";
      el("cap-mode").dataset.tone = frozen ? "warn" : "good";
    }
    if (el("cap-timestamp")) {
      var liveStamp = meta.mode === "LIVE";
      el("cap-timestamp").textContent = meta.capturedAt
        ? "快照时间 " + String(meta.capturedAt).slice(0, 16).replace("T", " ") + (liveStamp ? " · 实时行情" : " · 非实时行情")
        : (liveStamp ? "实时行情" : "非实时行情");
    }
    if (el("cap-scope")) {
      var code = underlying.code || "--";
      var name = underlying.name || "";
      el("cap-scope").textContent = (code + (name ? " · " + name : "")) + "（P0 支持范围以当前快照为准，港美股为产品方向）";
    }
    var run = el("cap-run");
    if (run) {
      run.disabled = state.agent.busy;
      run.textContent = meta.mode === "LIVE" ? "运行管线（Live）" : "运行管线（Replay）";
    }
  }

  function renderOverview(D) {
    renderCapability(D);
    var expiry = currentExpiry(D);
    var strategy = expiry && expiry.strategy || {};
    var account = D.account || {};
    var terminal = D.terminal || {};
    var risk = terminal.risk || {};
    var card = D.decisionCard || {};
    setHuman("verdict", humanVerdict(card.verdict), card.verdict);
    if (el("summary")) el("summary").textContent = humanSummary(card.summary);
    if (el("ov-lots")) el("ov-lots").textContent = strategy.lots != null ? strategy.lots + " 张 @ " + fmt(expiry.strike, 0) : "--";
    if (el("ov-expiry")) el("ov-expiry").textContent = expiry ? expiry.expiry.slice(5) + " / " + expiry.dte + " 天" : "--";
    if (el("ov-cost-ask")) el("ov-cost-ask").textContent = fmt(strategy.costPerLotAsk);
    if (el("ov-cost-exec")) {
      var execCost = strategy.costPerLotExec != null ? strategy.costPerLotExec : risk.execCostPerLot;
      el("ov-cost-exec").textContent = fmt(execCost);
      var status = risk.costStatus;
      el("ov-cost-exec").dataset.tone = status === "UNVERIFIED" ? "warn" : status === "VERIFIED" || status === "SNAPSHOT_DECLARED" ? "good" : "";
    }
    if (el("ov-maxloss")) el("ov-maxloss").textContent = fmt(strategy.maxLoss != null ? strategy.maxLoss : risk.maxLoss);
    if (el("ov-budget")) el("ov-budget").textContent = fmt(risk.budgetHkd != null ? risk.budgetHkd : account.cashHkd * account.riskBudgetPct / 100) + " HKD";
    if (el("ov-breakeven")) el("ov-breakeven").textContent = strategy.breakeven ? fmt(strategy.breakeven[0], 2) + " / " + fmt(strategy.breakeven[1], 2) : "--";
    var event = terminal.event || D.earnings || {};
    if (el("desk-event-date")) el("desk-event-date").textContent = event.date || "--";
    if (el("desk-event-name")) el("desk-event-name").textContent = event.label || "业绩事件";
    if (el("desk-event-impact")) el("desk-event-impact").textContent = event.expectedMovePct == null ? "--" : "预期波动 ±" + fmt(event.expectedMovePct, 2) + "%";
    renderMarketSidecar(D);
    renderGates(D);
  }

  function renderMarketSidecar(D) {
    var expiry = currentExpiry(D);
    var body = el("snapshot-book-body");
    if (el("book-expiry")) el("book-expiry").textContent = expiry ? expiry.expiry.slice(5) + " / " + expiry.dte + " 天" : "--";
    if (body) {
      body.innerHTML = "";
      if (expiry) {
        [["看涨", expiry.call], ["看跌", expiry.put]].forEach(function (row) {
          var tr = document.createElement("tr");
          [row[0], fmt(row[1].bid, 2), fmt(row[1].ask, 2), fmt(row[1].openInterest)].forEach(function (value, index) {
            var td = make("td", index === 0 ? "l" : index === 1 ? "bid" : index === 2 ? "ask" : "", value);
            tr.appendChild(td);
          });
          body.appendChild(tr);
        });
      }
    }
    var strategy = expiry && expiry.strategy || {};
    var greeks = strategy.greeks || {};
    [["greek-delta", greeks.delta], ["greek-gamma", greeks.gamma], ["greek-vega", greeks.vega], ["greek-theta", greeks.theta], ["greek-rho", greeks.rho]].forEach(function (pair) {
      if (el(pair[0])) el(pair[0]).textContent = pair[1] == null ? "--" : fmtSigned(pair[1], 3);
    });
    var callSpread = expiry ? spreadPct(expiry.call) : null;
    var putSpread = expiry ? spreadPct(expiry.put) : null;
    if (el("greek-spread")) el("greek-spread").textContent = callSpread == null || putSpread == null ? "--" : fmt((callSpread + putSpread) / 2, 1) + "%";
  }

  function renderExpiryTabs(D) {
    var wrap = el("expiry-tabs");
    if (!wrap) return;
    wrap.innerHTML = "";
    (D.expiries || []).forEach(function (expiry) {
      var button = make("button", "expiry-tab" + (expiry.expiry === state.expiry ? " active" : ""));
      button.type = "button";
      button.dataset.expiry = expiry.expiry;
      button.appendChild(document.createTextNode(expiry.expiry.slice(5)));
      button.appendChild(make("span", "dte", " / " + expiry.dte + "天"));
      button.addEventListener("click", function () {
        selectExpiry(expiry.expiry, "到期标签");
      });
      wrap.appendChild(button);
    });
  }

  function spreadPct(leg) {
    if (!leg || leg.bid == null || leg.ask == null || leg.mid == null || Number(leg.mid) === 0) return null;
    return (Number(leg.ask) - Number(leg.bid)) / Number(leg.mid) * 100;
  }
  function spreadTone(value) { return value == null ? "muted-cell" : value <= 8 ? "pos" : value <= 15 ? "" : "neg"; }
  function chainRow(expiry, call, put, primary) {
    var tr = document.createElement("tr");
    tr.className = (primary ? "primary-row " : "") + (expiry.expiry === state.expiry ? "selected-row" : "");
    tr.dataset.expiry = expiry.expiry;
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.setAttribute("aria-label", "选择 " + expiry.expiry + " 到期");
    tr.addEventListener("click", function () { selectExpiry(expiry.expiry, "期权链"); });
    tr.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectExpiry(expiry.expiry, "期权链"); }
    });
    var cells = [
      [fmt(call.apiIvPct, 1) + "%", ""], ["--", "muted-cell"], [fmt(call.bid, 2), "bid"], [fmt(call.ask, 2), "ask"], [fmt(call.openInterest), ""],
      [fmt(expiry.strike, 0), "strike"], [fmt(put.openInterest), ""], [fmt(put.bid, 2), "bid"], [fmt(put.ask, 2), "ask"], ["--", "muted-cell"], [fmt(put.apiIvPct, 1) + "%", ""],
    ];
    cells.forEach(function (pair, index) {
      var td = make("td", pair[1], pair[0]);
      if (index === 5) td.className = "strike";
      tr.appendChild(td);
    });
    return tr;
  }
  function renderChain(D) {
    var terminal = D.terminal || {};
    var chain = terminal.chain || {};
    if (el("chain-meta")) el("chain-meta").textContent = humanCoverage(chain.coverage) || "平值快照";
    if (el("chain-coverage")) el("chain-coverage").textContent = humanCoverage(chain.coverage) || "当前快照覆盖范围有限";
    if (el("coverage-note")) el("coverage-note").textContent = chain.note || "未录制的行权价不做推断。";
    var body = el("chain-body");
    if (body) {
      body.innerHTML = "";
      (D.expiries || []).forEach(function (expiry) { body.appendChild(chainRow(expiry, expiry.call, expiry.put, expiry.expiry === state.expiry || expiry.primary)); });
    }
    var viewBody = el("chain-view-body");
    if (viewBody) {
      viewBody.innerHTML = "";
      (D.expiries || []).forEach(function (expiry) {
        [
          ["CALL", expiry.call, spreadPct(expiry.call)],
          ["PUT", expiry.put, spreadPct(expiry.put)],
        ].forEach(function (row) {
          var tr = document.createElement("tr");
          tr.dataset.expiry = expiry.expiry;
          tr.tabIndex = 0;
          tr.setAttribute("role", "button");
          tr.setAttribute("aria-label", "选择 " + expiry.expiry + " " + row[0] + " 合约");
          if (expiry.expiry === state.expiry) tr.classList.add("selected-row");
          tr.addEventListener("click", function () { selectExpiry(expiry.expiry, "期权链详情"); });
          tr.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectExpiry(expiry.expiry, "期权链详情"); }
          });
          [expiry.expiry.slice(5) + (expiry.primary ? " 主到期" : ""), row[0], row[1].code, fmt(expiry.strike, 0), fmt(row[1].bid, 2), fmt(row[1].ask, 2), fmt(row[1].apiIvPct, 1) + "%", fmt(row[1].openInterest), fmt(row[1].volume), row[2] == null ? "--" : fmt(row[2], 1) + "%"].forEach(function (value, index) {
            var td = make("td", index === 0 || index === 1 || index === 2 ? "l" : "", value);
            if (index === 4) td.classList.add("bid");
            if (index === 5) td.classList.add("ask");
            if (index === 9) {
              var tone = spreadTone(row[2]);
              if (tone) td.classList.add(tone);
            }
            tr.appendChild(td);
          });
          viewBody.appendChild(tr);
        });
      });
    }
    if (el("chain-view-meta")) el("chain-view-meta").textContent = (D.expiries || []).length + " 个到期 / " + (humanCoverage(chain.coverage) || "平值");
    if (el("chain-view-note")) el("chain-view-note").textContent = chain.note || "未录制的行权价不做推断。";
  }

  function svgNode(name, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }
  function renderChart(D) {
    var svg = el("terminal-chart");
    var empty = el("chart-empty");
    if (!svg) return;
    svg.innerHTML = "";
    var isPnl = state.chartMode === "pnl";
    var isPrice = state.chartMode === "price";
    var isVol = state.chartMode === "vol";
    var terminalChart = (D.terminal || {}).chart || {};
    var quote = (D.terminal || {}).quote || {};
    var currency = quote.currency || "--";
    if (isPnl && !(terminalChart.points || []).length && D.expiries && D.expiries[0] && D.expiries[0].strategy) {
      terminalChart = Object.assign({}, terminalChart, { points: [] });
      (D.expiries[0].strategy.pnlAtExpiry || []).forEach(function (group) {
        (group.rows || []).forEach(function (row) { terminalChart.points.push({ spot: row.spot, pnl: row.pnl, label: group.label, direction: row.direction }); });
      });
      terminalChart.breakeven = D.expiries[0].strategy.breakeven || [];
    }
    var series = isPnl ? terminalChart : isPrice ? (terminalChart.price || {}) : (terminalChart.volatility || {});
    var points = (series.points || []).filter(function (point) {
      if (isPnl) return isFinite(Number(point.spot)) && isFinite(Number(point.pnl));
      if (!point) return false;
      var value = isPrice ? point.close : point.hv30d;
      return point.date && isFinite(Number(value));
    }).slice();
    if (isPnl) points.sort(function (a, b) { return Number(a.spot) - Number(b.spot); });
    else points.sort(function (a, b) { return String(a.date).localeCompare(String(b.date)); });
    if (el("chart-source")) el("chart-source").textContent = series.source || "--";
    if (el("chart-coverage")) el("chart-coverage").textContent = series.coverage || "--";
    if (el("chart-underlying")) el("chart-underlying").textContent = ((quote.name || (D.underlying || {}).name || "标的") + " " + humanSymbol(quote.symbol || (D.underlying || {}).code));
    if (el("chart-context-title")) el("chart-context-title").textContent = isPnl ? "主到期 / 策略损益" : isPrice ? "历史价格 / 日线收盘" : "30 日实现波动 / 年化收益";
    if (el("chart-axis-note")) el("chart-axis-note").textContent = isPnl ? "横轴：到期正股价 / 纵轴：" + currency : isPrice ? "横轴：日期 / 纵轴：" + currency : "横轴：日期 / 纵轴：年化 %";
    if (el("terminal-chart")) el("terminal-chart").setAttribute("aria-label", isPnl ? "主到期损益路径" : isPrice ? "正股历史价格" : "30 日实现历史波动率");
    if (!points.length) {
      if (empty) {
        empty.hidden = false;
        empty.textContent = isPnl ? "等待研究数据。" : isPrice ? "未启用历史价格 provider，或没有可用日线行情。" : "未启用历史价格 provider，或历史样本不足以计算 30 日实现波动率。";
      }
      return;
    }
    if (empty) empty.hidden = true;
    var plotPoints = points.map(function (point, index) {
      return {
        xValue: isPnl ? Number(point.spot) : index,
        yValue: isPnl ? Number(point.pnl) : Number(isPrice ? point.close : point.hv30d),
        label: isPnl ? String(point.label || "到期路径") : String(point.date),
      };
    });
    var w = 960, h = 310, left = 54, right = 20, top = 17, bottom = 35;
    var xs = plotPoints.map(function (p) { return p.xValue; });
    var ys = plotPoints.map(function (p) { return p.yValue; }).concat(isPnl ? [0] : []);
    var xMin = Math.min.apply(null, xs), xMax = Math.max.apply(null, xs), yMin = Math.min.apply(null, ys), yMax = Math.max.apply(null, ys);
    var xPad = Math.max((xMax - xMin) * 0.08, isPnl ? 1 : 0.5), yPad = isPnl ? Math.max((yMax - yMin) * 0.12, 100) : Math.max((yMax - yMin) * 0.12, Math.abs(yMax) * 0.005, 0.01);
    xMin -= xPad; xMax += xPad; yMin -= yPad; yMax += yPad;
    function x(value) { return left + (Number(value) - xMin) / (xMax - xMin || 1) * (w - left - right); }
    function y(value) { return h - bottom - (Number(value) - yMin) / (yMax - yMin) * (h - top - bottom); }
    [0, 0.25, 0.5, 0.75, 1].forEach(function (fraction) {
      var yy = top + fraction * (h - top - bottom);
      svg.appendChild(svgNode("line", { x1: left, y1: yy, x2: w - right, y2: yy, class: "chart-grid" }));
      var label = svgNode("text", { x: left - 8, y: yy + 3, "text-anchor": "end" });
      label.textContent = fmt(yMax - fraction * (yMax - yMin), isPnl ? 0 : isVol ? 1 : 2) + (isVol ? "%" : "");
      svg.appendChild(label);
    });
    if (isPnl) svg.appendChild(svgNode("line", { x1: left, y1: y(0), x2: w - right, y2: y(0), class: "chart-zero" }));
    svg.appendChild(svgNode("line", { x1: left, y1: top, x2: left, y2: h - bottom, class: "chart-axis" }));
    svg.appendChild(svgNode("line", { x1: left, y1: h - bottom, x2: w - right, y2: h - bottom, class: "chart-axis" }));
    (isPnl ? (terminalChart.breakeven || []) : []).forEach(function (breakeven) {
      var xx = x(breakeven);
      svg.appendChild(svgNode("line", { x1: xx, y1: top, x2: xx, y2: h - bottom, class: "chart-breakeven" }));
      var label = svgNode("text", { x: xx, y: top + 11, "text-anchor": "middle", class: "chart-value" });
      label.textContent = "盈亏平衡 " + fmt(breakeven, 1);
      svg.appendChild(label);
    });
    var path = plotPoints.map(function (point, index) { return (index ? "L" : "M") + x(point.xValue).toFixed(1) + " " + y(point.yValue).toFixed(1); }).join(" ");
    var baseline = isPnl ? 0 : yMin;
    var area = path + " L " + x(plotPoints[plotPoints.length - 1].xValue).toFixed(1) + " " + y(baseline).toFixed(1) + " L " + x(plotPoints[0].xValue).toFixed(1) + " " + y(baseline).toFixed(1) + " Z";
    svg.appendChild(svgNode("path", { d: area, class: "chart-area" }));
    svg.appendChild(svgNode("path", { d: path, class: "chart-line" }));
    plotPoints.forEach(function (point, index) {
      var positive = isPnl ? point.yValue >= 0 : isVol ? true : point.yValue >= plotPoints[0].yValue;
      svg.appendChild(svgNode("circle", { cx: x(point.xValue), cy: y(point.yValue), r: 3, class: positive ? "chart-point-positive" : "chart-point-negative" }));
      var showLabel = isPnl || index === 0 || index === Math.floor((plotPoints.length - 1) / 2) || index === plotPoints.length - 1;
      if (showLabel) {
        var label = svgNode("text", { x: x(point.xValue), y: y(point.yValue) + (positive ? -9 : 17), "text-anchor": "middle", class: "chart-value" });
        label.textContent = isPnl ? fmtSigned(point.yValue) : fmt(point.yValue, isVol ? 1 : 2) + (isVol ? "%" : "");
        svg.appendChild(label);
      }
    });
    var first = svgNode("text", { x: left, y: h - 9 }); first.textContent = isPnl ? fmt(xMin, 1) : String(plotPoints[0].label).slice(5, 10); svg.appendChild(first);
    var last = svgNode("text", { x: w - right, y: h - 9, "text-anchor": "end" }); last.textContent = isPnl ? fmt(xMax, 1) : String(plotPoints[plotPoints.length - 1].label).slice(5, 10); svg.appendChild(last);
  }

  function renderDecisionCard(D) {
    var card = D.decisionCard || {};
    if (el("card-meta")) el("card-meta").textContent = (D.meta || {}).generatedAt || "--";
    setHuman("verdict-2", humanVerdict(card.verdict), card.verdict);
    if (el("summary-2")) el("summary-2").textContent = humanSummary(card.summary);
    clear("evidence-list-2");
    (card.keyEvidence || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", "claim", humanReason(item.claim)));
      li.appendChild(make("span", "src", humanSource(item.source)));
      el("evidence-list-2").appendChild(li);
    });
    clear("conditions-list");
    (card.conditionsThatChange || []).forEach(function (item) { el("conditions-list").appendChild(make("li", null, humanReason(item))); });
    var earnings = D.earnings || {};
    if (el("earnings-date")) el("earnings-date").textContent = earnings.date || "--";
    if (el("eps-yoy")) el("eps-yoy").textContent = earnings.estimateEpsYoy == null ? "--" : fmtPct(earnings.estimateEpsYoy, 1);
    if (el("rev-yoy")) el("rev-yoy").textContent = earnings.estimateRevenueYoy == null ? "--" : fmtPct(earnings.estimateRevenueYoy, 1);
    if (el("crush-history")) el("crush-history").textContent = earnings.lastReportIvCrush == null ? "--" : fmt(earnings.lastReportIvCrush, 2) + " / " + fmt(earnings.historyReportIvCrush, 2) + " pp";
    if (el("captured-at")) el("captured-at").textContent = (D.meta || {}).capturedAt || "--";
    renderNextStep(card.verdict);
  }

  function nextStepCopy(verdict) {
    var copy = {
      NO_TRADE: ["先不交易", "当前成本与证据不支持交易。可修改条件重算，或查看「什么会改变结论」清单。"],
      BLOCK: ["风险拦截", "数据、规格、账户或风险硬门未通过。展开决策检查栏查看拦截原因，调整条件后重算。"],
      DRAFT_ONLY: ["暂存为草案", "当前可研究并生成草稿，但不能进入确认。补充或修正条件后重算。"],
      READY_FOR_CONFIRMATION: ["可以进入确认", "客观门已通过。当前切片为 P0a 只读研究，模拟提交未启用（P0c 未毕业），不提供下单入口。"],
    };
    return copy[verdict] || ["结论形成中", "等待引擎结果。"];
  }

  function renderNextStep(verdict) {
    var copy = nextStepCopy(verdict);
    if (el("next-step-title")) el("next-step-title").textContent = copy[0];
    if (el("next-step-detail")) el("next-step-detail").textContent = copy[1];
    if (el("next-step-boundary")) {
      el("next-step-boundary").textContent = "当前切片：P0a Replay 只读 · 数据来自冻结快照 · 模拟提交未启用（P0c 未毕业）· 研究用途，非投资建议。";
    }
  }

  function bindCardActions() {
    var exportButton = el("card-export");
    if (exportButton) exportButton.addEventListener("click", function () {
      exportButton.disabled = true;
      fetch("/api/decision-card").then(function (r) { return r.json(); }).then(function (payload) {
        if (!payload || !payload.found) { note((payload && payload.error) || "未找到决策卡，请先运行一次管线。"); return; }
        var blob = new Blob([JSON.stringify(payload.card, null, 2)], { type: "application/json" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "decision_card_" + new Date().toISOString().slice(0, 10) + ".json";
        document.body.appendChild(a); a.click();
        URL.revokeObjectURL(a.href); a.remove();
        note("已导出决策卡（sha256 " + String(payload.sha256).slice(0, 12) + "…，路径 " + payload.path + "）");
      }).catch(function () {
        note("本地引擎未连接（静态回退），无法导出引擎决策卡。");
      }).finally(function () { exportButton.disabled = false; });
    });
    var recalc = el("card-recalc");
    if (recalc) recalc.addEventListener("click", function () { handleAgentAction({ type: "open_controls" }); });
    var submitButton = el("submit-order");
    if (submitButton) submitButton.addEventListener("click", submitSimulatedOrder);
    var confirmInput = el("submit-confirm-text");
    if (confirmInput) confirmInput.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); submitSimulatedOrder(); } });
  }

  function submitSimulatedOrder() {
    var input = el("submit-confirm-text");
    var status = el("submit-status");
    var phrase = String(input && input.value || "").trim();
    if (phrase !== "提交模拟盘") {
      if (status) { status.textContent = "请先键入「提交模拟盘」确认（原文，模拟盘仅支持研究演示）。"; status.dataset.tone = "warn"; }
      return;
    }
    if (el("submit-order")) el("submit-order").disabled = true;
    if (status) { status.textContent = "提交中…"; status.dataset.tone = ""; }
    fetch("/api/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true, confirmText: phrase }) })
      .then(function (response) { return response.json().then(function (payload) { return { ok: response.ok, payload: payload }; }); })
      .then(function (result) {
        if (el("submit-order")) el("submit-order").disabled = false;
        if (!status) return;
        if (result.ok && result.payload.submitted) {
          state.submitReceipt = result.payload;
          var orders = (result.payload.receipts || []).map(function (row) {
            return String(row.code).split(".").slice(-1)[0] + " #" + row.order_id + "（" + row.status + "）";
          }).join("；");
          status.textContent = "模拟盘订单已提交：" + orders;
          status.dataset.tone = "good";
          note("模拟盘订单已提交，回执已写入审计链。");
          renderAudit();
        } else {
          var error = (result.payload && (result.payload.typedError || {}).message) || (result.payload && result.payload.error) || "提交失败";
          status.textContent = String(error);
          status.dataset.tone = "bad";
        }
      })
      .catch(function () {
        if (el("submit-order")) el("submit-order").disabled = false;
        if (status) { status.textContent = "提交请求失败（本地服务不可用）。"; status.dataset.tone = "bad"; }
      });
  }

  function renderMacro(D) {
    var macro = D.macro || {};
    var judgment = macro.judgment || macro.assessment || {};
    var policy = macro.policy_analysis || {};
    if (el("macro-confidence")) el("macro-confidence").textContent = macro.available ? (judgment.confidence || "已提供") : "当前快照未提供";
    var sentiment = judgment.sentiment || judgment.sentiment_index;
    if (el("sentiment-index")) el("sentiment-index").textContent = sentiment == null ? "--" : fmt(sentiment, 1);
    if (el("sentiment-verdict")) el("sentiment-verdict").textContent = judgment.verdict || macro.note || "--";
    if (el("iv-state")) el("iv-state").textContent = judgment.iv_state || "--";
    if (el("skew-verdict")) el("skew-verdict").textContent = judgment.skew_verdict || "--";
    var contradiction = policy.principal_contradiction || {};
    if (el("principal-pair")) el("principal-pair").textContent = contradiction.pair || "--";
    if (el("macro-mood")) el("macro-mood").textContent = judgment.mood || macro.note || "--";
    clear("scenario-list");
    (judgment.scenarios || []).forEach(function (scenario) {
      var li = document.createElement("li");
      li.appendChild(make("span", "scenario-name", scenario.name));
      li.appendChild(make("span", "scenario-level", "可能性 " + (scenario.likelihood_level || "--")));
      li.appendChild(make("span", "scenario-implication", (scenario.market_implication || "") + "；期权：" + (scenario.option_implication || "")));
      el("scenario-list").appendChild(li);
    });
    if (!el("scenario-list").childNodes.length) el("scenario-list").appendChild(make("li", null, macro.note || "暂无宏观情景输入。"));
    renderHealthPills("library-health", D.policyLibrary || {}, true);
    if (el("macro-note")) el("macro-note").textContent = macro.disclaimer || "宏观段落只展示当前快照提供的内容。";
  }

  function renderHealthPills(id, library, withCount) {
    var wrap = el(id);
    if (!wrap) return;
    wrap.innerHTML = "";
    var health = (library.health || {}).verification || {};
    ["VERIFIED", "PENDING", "FAILED", "UNKNOWN"].forEach(function (kind) {
      if (health[kind] === undefined) return;
      wrap.appendChild(make("span", "health-pill " + kind.toLowerCase(), humanHealth(kind) + " " + health[kind]));
    });
    if (withCount) wrap.appendChild(make("span", "health-pill", "事件 " + (library.eventCount || 0)));
  }

  function renderResearch(D) {
    var research = D.research || {};
    var digest = research.digest || {};
    if (el("research-meta")) el("research-meta").textContent = research.available ? (digest.item_count || 0) + " 条 · " + (digest.synthetic_only ? "演示输入" : "已核验输入") : "未提供投研输入";
    if (el("rs-item-count")) el("rs-item-count").textContent = research.available ? String(digest.item_count || 0) : "--";
    if (el("rs-synthetic")) el("rs-synthetic").textContent = research.available ? (digest.synthetic_only ? "演示资料" : "来源已标注") : "--";
    if (el("research-source")) el("research-source").textContent = research.sourcePath || "data/research_items_hero.json";
    if (el("research-add-hint")) el("research-add-hint").textContent = research.sourceMode ? research.sourceMode + " · 替换 GOAI_RESEARCH_ITEMS_PATH 后刷新" : "将 canonical JSON 配置到 GOAI_RESEARCH_ITEMS_PATH";
    var stock = research.stock_price_impact || {};
    var option = research.option_impact || {};
    if (el("rs-stock-verdict")) el("rs-stock-verdict").textContent = stock.verdict || "--";
    if (el("rs-option-verdict")) el("rs-option-verdict").textContent = option.verdict ? "期权影响：" + option.verdict : "--";
    var rows = [];
    ["announcements", "earnings_items", "news", "reports", "industry"].forEach(function (kind) { (research[kind] || []).forEach(function (item) { if (item && typeof item === "object") rows.push(item); }); });
    var tbody = el("research-items");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!rows.length) { var empty = document.createElement("tr"); empty.appendChild(make("td", "l", research.note || "当前快照未展开投研条目。")); empty.firstChild.colSpan = 5; tbody.appendChild(empty); }
    rows.slice(0, 50).forEach(function (item) {
      var tr = document.createElement("tr");
      [item.title || item.headline || item.id || "--", item.kind || item.type || "--", item.sentiment || "--", item.date || "--", item.source || item.publisher || "--"].forEach(function (value, index) { tr.appendChild(make("td", index === 3 ? "" : "l", value)); });
      tbody.appendChild(tr);
    });
    if (el("research-note")) el("research-note").textContent = research.disclaimer || research.note || "";
  }

  function renderLibrary(D) {
    var library = D.policyLibrary || {};
    if (el("library-meta")) el("library-meta").textContent = library.eventCount ? library.eventCount + " 个事件" : "不可用";
    renderHealthPills("library-health-2", library, false);
    var tbody = el("library-events");
    if (!tbody) return;
    tbody.innerHTML = "";
    (library.events || []).forEach(function (event) {
      var tr = document.createElement("tr");
      [event.id, event.name, event.date, event.type, humanEventStatus(event.status), (event.verdictReads || []).join(", ")].forEach(function (value, index) { var td = make("td", index === 0 || index === 1 || index === 3 || index === 4 || index === 5 ? "l" : "", value); if (event.status === "ACTIVE" && index === 4) td.classList.add("pos"); if (event.status === "FAILED" && index === 4) td.classList.add("neg"); tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    if (!tbody.childNodes.length) { var empty = document.createElement("tr"); empty.appendChild(make("td", "l", "暂无政策事件。")); empty.firstChild.colSpan = 6; tbody.appendChild(empty); }
    if (el("library-note")) el("library-note").textContent = "来源核验状态是可审计标记，正式使用前仍需逐条复核。";
  }

  var STANCE_LABEL = { favor: "赞成", oppose: "反对", neutral: "中性" };
  function renderDebate(D) {
    var trace = D.debateTrace || null;
    var consensus = D.researchConsensus || (trace && trace.research_consensus) || null;
    var status = !trace ? "未运行 / 本地结论" : trace.status === "offline" ? "未接入外部服务" : trace.status === "complete" ? "两轮记录完成" : humanTraceStatus(trace.status);
    if (el("debate-summary")) el("debate-summary").textContent = status;
    var meta = el("debate-meta");
    if (meta) {
      meta.innerHTML = "";
      if (trace) {
        var metrics = trace.metrics || {};
        [["状态", status], ["结论", humanVerdict(trace.verdict)], ["来源", trace.status === "complete" ? "外部解释" : "本地规则"], ["耗时", metrics.elapsed_ms == null ? "--" : (metrics.elapsed_ms / 1000).toFixed(1) + " s"], ["有效角色", metrics.ok_roles == null ? "--" : metrics.ok_roles]].forEach(function (pair) { var cell = make("div", "debate-meta-cell"); cell.appendChild(make("span", null, pair[0])); cell.appendChild(make("strong", null, pair[1])); meta.appendChild(cell); });
      } else meta.appendChild(make("p", "coverage-note", "尚未接入外部解释服务；本地结论仍然有效。"));
    }
    var rounds = el("debate-rounds");
    if (rounds) { rounds.innerHTML = ""; ((trace && trace.rounds) || []).forEach(function (round) { var block = make("div", "debate-round-block"); block.appendChild(make("h3", "debate-round-head", "第 " + round.round + " 轮")); (round.entries || []).forEach(function (entry) { var row = make("div", "debate-entry"); var head = make("div", "debate-entry-head", (entry.name || "角色") + " / " + (entry.title || "")); head.appendChild(make("span", "debate-status", humanTraceStatus(entry.status))); if (entry.stance) head.appendChild(make("span", "stance-pill stance-" + entry.stance, STANCE_LABEL[entry.stance] || "中性")); row.appendChild(head); if (entry.conclusion) row.appendChild(make("p", null, entry.conclusion)); block.appendChild(row); }); rounds.appendChild(block); }); if (!rounds.childNodes.length) rounds.appendChild(make("p", "coverage-note", "尚未运行分歧记录。")); }
    var disputes = el("debate-disputes");
    if (disputes) { disputes.innerHTML = ""; ((trace && trace.disputes) || []).forEach(function (item, index) { var card = make("div", "debate-dispute"); card.appendChild(make("strong", null, "分歧 " + (index + 1) + " · " + item.topic)); card.appendChild(make("p", null, item.question)); disputes.appendChild(card); }); if (!disputes.childNodes.length) disputes.appendChild(make("p", "coverage-note", "无分歧点。")); }
    var consensusWrap = el("debate-consensus");
    if (consensusWrap) { consensusWrap.innerHTML = ""; consensusWrap.appendChild(make("p", "coverage-note", consensus ? humanSummary(consensus.summary || "") : "暂无研究共识。")); }
    if (el("debate-disclaimer")) el("debate-disclaimer").textContent = trace && trace.disclaimer ? trace.disclaimer : "";
  }

  function workspaceMeta(D) {
    var workspace = (D && D.workspace) || {};
    var projects = Array.isArray(workspace.projects) ? workspace.projects.slice() : [];
    var underlying = (D && D.underlying) || {};
    if (!projects.length && underlying.code) {
      projects = [{ id: "static-current", name: underlying.name || "当前标的", symbol: underlying.code, description: "静态快照", available: true, researchAvailable: true }];
    }
    return { projects: projects, active: workspace.activeProjectId || (projects[0] && projects[0].id) || "" };
  }
  function projectStatus(project) {
    if (project.available === false) return "缺少快照";
    if (project.researchAvailable === false) return "未配置资料";
    return "可研究";
  }
  function makeProjectEntry(project, active, variant) {
    var item = make("button", variant === "rail" ? "workspace-symbol project-switch" : "project-item");
    item.type = "button";
    item.dataset.projectId = project.id;
    item.setAttribute("aria-pressed", String(project.id === active));
    item.classList.toggle("active", project.id === active);
    if (project.available === false) {
      item.disabled = true;
      item.setAttribute("aria-disabled", "true");
    } else {
      item.addEventListener("click", function () { selectProject(project.id); });
    }
    var copy = make("span", variant === "rail" ? "project-switch-copy" : "project-item-copy");
    copy.appendChild(make("strong", null, project.name || "未命名项目"));
    copy.appendChild(make("small", null, project.description || projectStatus(project)));
    var status = projectStatus(project);
    if (status !== "可研究") copy.appendChild(make("small", variant === "rail" ? "project-switch-state" : "project-item-status", status));
    item.appendChild(copy);
    item.appendChild(make("span", variant === "rail" ? "project-switch-symbol" : "project-item-symbol", humanSymbol(project.symbol)));
    return item;
  }
  function renderWorkspace(D) {
    var meta = workspaceMeta(D);
    var active = meta.active;
    var rail = el("workspace-projects");
    if (rail) {
      rail.innerHTML = "";
      meta.projects.forEach(function (project) { rail.appendChild(makeProjectEntry(project, active, "rail")); });
    }
    var empty = el("workspace-empty");
    if (empty) {
      empty.textContent = meta.projects.length ? "" : "暂无已注册项目。";
      empty.hidden = Boolean(meta.projects.length);
    }
    var list = el("project-list");
    if (list) {
      list.innerHTML = "";
      if (!meta.projects.length) list.appendChild(make("p", "project-list-empty", "还没有研究项目。可以从下方导入一个冻结快照。"));
      meta.projects.forEach(function (project) { list.appendChild(makeProjectEntry(project, active, "drawer")); });
    }
    if (el("project-count")) el("project-count").textContent = meta.projects.length + " 个项目";
    if (state.mode === "api" && active) {
      try { window.localStorage.setItem(PROJECT_STORAGE_KEY, active); } catch (error) { /* local fallback */ }
    }
  }
  function setProjectOpen(open) {
    var drawer = el("project-drawer");
    if (!drawer) return;
    drawer.classList.toggle("open", open);
    drawer.setAttribute("aria-hidden", String(!open));
    if (open) {
      var first = drawer.querySelector(".project-item:not([disabled]), #project-name");
      if (first) first.focus();
    }
  }
  function setProjectFormNote(text, tone) {
    var noteNode = el("project-form-note");
    if (!noteNode) return;
    noteNode.textContent = text || "";
    noteNode.classList.toggle("error", tone === "error");
    noteNode.classList.toggle("success", tone === "success");
  }
  function setProjectBusy(busy) {
    state.projectBusy = Boolean(busy);
    document.querySelectorAll(".project-switch, .project-item").forEach(function (button) {
      if (!button.hasAttribute("aria-disabled")) button.disabled = state.projectBusy;
    });
    if (el("workspace-add")) el("workspace-add").disabled = state.projectBusy;
    if (el("project-submit")) el("project-submit").disabled = state.projectBusy;
  }
  function applyProjectData(data) {
    var payload = data && data.state && data.state.expiries ? data.state : data;
    if (!payload || payload.error || !payload.expiries) throw new Error((payload && payload.error) || "项目返回的数据不完整");
    var previous = state.data && state.data.workspace && state.data.workspace.activeProjectId;
    var next = payload.workspace && payload.workspace.activeProjectId;
    if (previous && next && previous !== next) {
      state.agent.context = null;
      state.agent.history = [];
      saveAgentSession();
      clear("chat-log");
    }
    state.mode = "api";
    state.expiry = payload.terminal && payload.terminal.selection ? payload.terminal.selection.expiry : null;
    rememberAgentContext(payload, true);
    render(payload);
  }
  function selectProject(projectId) {
    if (!projectId || state.projectBusy) return;
    if (state.mode !== "api") { note("静态回退模式：启动桌面终端服务后才能切换研究项目。"); return; }
    var current = state.data && state.data.workspace && state.data.workspace.activeProjectId;
    if (current === projectId) { setProjectOpen(false); note("当前项目已经是 " + humanSymbol((state.data.underlying || {}).code) + "。"); return; }
    setProjectBusy(true);
    fetch("/api/projects/select?no_audit=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ projectId: projectId }) })
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.error) || "HTTP " + response.status); return body; }); })
      .then(function (data) { applyProjectData(data); setProjectOpen(false); note("已切换研究项目：" + ((data.underlying || {}).name || humanSymbol((data.underlying || {}).code))); })
      .catch(function (error) { note("切换项目失败：" + error.message); })
      .finally(function () { setProjectBusy(false); });
  }
  function addProject(event) {
    event.preventDefault();
    if (state.mode !== "api") { setProjectFormNote("请先启动桌面终端服务，再导入项目。", "error"); return; }
    var form = el("project-form");
    var submit = el("project-submit");
    var payload = {
      name: el("project-name") && el("project-name").value.trim(),
      symbol: el("project-symbol") && el("project-symbol").value.trim(),
      inputPath: el("project-input-path") && el("project-input-path").value.trim(),
      researchItemsPath: el("project-research-path") && el("project-research-path").value.trim(),
    };
    if (!payload.name || !payload.symbol) { setProjectFormNote("请填写项目名称和标的代码；路径由 Agent 自动寻找。", "error"); return; }
    setProjectBusy(true);
    setProjectFormNote("Agent 正在扫描受控资料目录并校验文件归属…", "");
    fetch("/api/projects?no_audit=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.error) || "HTTP " + response.status); return body; }); })
      .then(function (data) {
        if (data && data.registered && !data.expiries) {
          var liveError = (data.stateError && data.stateError.code) || "OPEND_UNAVAILABLE";
          if (form) form.reset();
          setProjectOpen(false);
          setProjectFormNote("项目已加入工作区，但实时行情暂不可用（" + liveError + "）。", "success");
          note("已添加研究项目：" + ((data.underlying || {}).name || humanSymbol((data.underlying || {}).code)) + "；OpenD 启动后刷新即可查看实时状态。");
          return;
        }
        applyProjectData(data); if (form) form.reset(); setProjectOpen(false); setProjectFormNote("项目已加入工作区。", "success"); note("已添加并打开研究项目：" + ((data.underlying || {}).name || humanSymbol((data.underlying || {}).code)));
      })
      .catch(function (error) { setProjectFormNote("自动发现失败：" + error.message, "error"); note("项目没有添加：" + error.message); })
      .finally(function () { setProjectBusy(false); if (submit) submit.disabled = false; });
  }
  function bindWorkspace() {
    if (el("workspace-add")) el("workspace-add").addEventListener("click", function () { setProjectOpen(true); });
    if (el("project-close")) el("project-close").addEventListener("click", function () { setProjectOpen(false); });
    if (el("project-form")) el("project-form").addEventListener("submit", addProject);
  }

  function render(D) {
    if (!D) return;
    var selection = D.terminal && D.terminal.selection;
    if (selection && selection.expiry && !state.expiry) state.expiry = selection.expiry;
    if ((!state.expiry || !(D.expiries || []).some(function (item) { return item.expiry === state.expiry; })) && D.expiries && D.expiries.length) state.expiry = D.expiries[0].expiry;
    state.data = D;
    renderWorkspace(D);
    renderTopbar(D);
    renderOverview(D);
    renderRail(D);
    renderExpiryTabs(D);
    renderChain(D);
    renderChart(D);
    renderDecisionCard(D);
    renderMacro(D);
    renderResearch(D);
    renderLibrary(D);
    renderDebate(D);
    renderAudit();
    renderMetrics();
    renderAgent(D);
    activateView(state.view, false);
    syncLiveStream(D);
  }

  function activateView(view, announce) {
    state.view = view || "overview";
    document.querySelectorAll(".view-tab").forEach(function (tab) {
      var active = tab.dataset.view === state.view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".terminal-view").forEach(function (panel) { panel.classList.toggle("active", panel.id === "view-" + state.view); });
    if (announce && state.agent.context) {
      state.agent.context.view = state.view;
      saveAgentSession();
    }
    updateAgentSessionMeta(state.data);
    if (announce) note("已切换至 " + ({ overview: "总览", card: "结论", chain: "期权链", macro: "事件环境", research: "资料", library: "事件库", debate: "分歧", audit: "审计" }[state.view] || state.view));
  }
  function bindViews() {
    document.querySelectorAll(".view-tab").forEach(function (tab) { tab.addEventListener("click", function () { activateView(tab.dataset.view, true); }); });
  }
  function bindChartControls() {
    document.querySelectorAll(".chart-tab").forEach(function (tab) { tab.addEventListener("click", function () { state.chartMode = tab.dataset.chartMode; document.querySelectorAll(".chart-tab").forEach(function (item) { item.classList.toggle("active", item === tab); }); renderChart(state.data); }); });
    if (el("chart-refresh")) el("chart-refresh").addEventListener("click", function () { renderChart(state.data); note("损益曲线已更新，数据未改变。"); });
  }
  function bindDebateToggle() {
    var button = el("debate-toggle");
    var body = el("debate-body");
    if (!button || !body) return;
    button.addEventListener("click", function () {
      var expanded = button.getAttribute("aria-expanded") !== "false";
      body.hidden = expanded;
      button.setAttribute("aria-expanded", String(!expanded));
      button.textContent = expanded ? "展开详情" : "收起详情";
    });
  }
  function updateAgentSessionMeta(D) {
    var terminal = (D && D.terminal) || {};
    var quote = terminal.quote || {};
    var remembered = state.agent.context || {};
    var selected = remembered.selectedExpiry || state.expiry || (terminal.selection || {}).expiry || "--";
    var rememberedScenario = remembered.scenario || {};
    var symbol = rememberedScenario.underlying || quote.symbol || (D && D.underlying && D.underlying.code) || "工作集";
    var rememberedView = remembered.view || state.view;
    if (el("agent-context-meta")) el("agent-context-meta").textContent = humanSymbol(symbol) + " / " + selected + " / " + ({ overview: "总览", card: "风险", chain: "期权链", macro: "事件", research: "资料", library: "事件库", debate: "分歧" }[rememberedView] || rememberedView);
    if (el("agent-turn-count")) el("agent-turn-count").textContent = Math.floor(state.agent.history.filter(function (item) { return item.role === "agent"; }).length) + " 轮";
  }

  function rememberAgentContext(D, preferLive) {
    var current = state.agent.context || {};
    var existingScenario = current.scenario || {};
    var liveScenario = (D && D.scenario) || (D && D.chat && D.chat.scenario) || {};
    var scenario = preferLive ? Object.assign({}, existingScenario, liveScenario) : Object.assign({}, liveScenario, existingScenario);
    if (!scenario.underlying && D && D.underlying) scenario.underlying = D.underlying.code;
    if (!scenario.view) scenario.view = "uncertain";
    if (!scenario.horizon && D && D.earnings) scenario.horizon = (D.earnings.date || "当前") + " 业绩";
    if (scenario.account_cash_hkd == null && D && D.account) scenario.account_cash_hkd = D.account.cashHkd;
    if (scenario.risk_budget_pct == null && D && D.account) scenario.risk_budget_pct = D.account.riskBudgetPct;
    if (!Array.isArray(scenario.constraints)) scenario.constraints = [];
    var terminal = (D && D.terminal) || {};
    var selection = terminal.selection || {};
    state.agent.context = {
      scenario: scenario,
      selectedExpiry: (D && D.agent && D.agent.selectedExpiry) || current.selectedExpiry || state.expiry || selection.expiry,
      view: state.view || current.view || "overview",
    };
    saveAgentSession();
  }
  function renderAgentTrace(trace) {
    var stages = document.querySelectorAll("#stages .stage");
    var items = Array.isArray(trace) ? trace : [];
    stages.forEach(function (stage, index) {
      var item = items[index] || { label: stage.textContent, status: "pending" };
      stage.textContent = item.label || stage.textContent;
      stage.classList.remove("running", "done", "pending", "failed");
      stage.classList.add(item.status === "complete" ? "done" : item.status === "error" ? "failed" : item.status === "running" ? "running" : "pending");
    });
  }
  function beginAgentTrace() {
    renderAgentTrace([
      { label: "理解请求", status: "running" },
      { label: "读取数据", status: "pending" },
      { label: "重算损益", status: "pending" },
      { label: "检查风险", status: "pending" },
      { label: "更新工作台", status: "pending" },
    ]);
  }
  function handleAgentAction(action) {
    if (!action) return;
    var kind = action.type || action.action;
    if (kind === "open_view" && action.view) {
      activateView(action.view, true);
      note(action.label || "已切换工作区。");
    } else if (kind === "select_expiry" && action.expiry) {
      selectExpiry(action.expiry, "研究助理");
    } else if (kind === "open_controls") {
      openChat(false);
      var controls = el("scenario-controls");
      if (controls) controls.open = true;
      if (el("agent-budget")) el("agent-budget").focus();
    } else if (kind === "refresh") {
      requestAgent({ action: "refresh" }, action.label || "刷新冻结快照");
    } else if (kind === "debate") {
      requestAgent({ action: "debate", message: "围绕当前工作集生成分歧记录" }, action.label || "生成分歧记录");
    }
  }
  function renderAgentHistory() {
    var log = el("chat-log");
    if (!log) return;
    clear("chat-log");
    state.agent.history.forEach(function (item) {
      var node = make("div", "msg " + (item.role === "user" ? "msg-user" : "msg-agent"));
      node.appendChild(make("p", null, item.text));
      if (item.detail) node.appendChild(make("p", "agent-detail", item.detail));
      if (item.actions && item.actions.length) {
        var actions = make("div", "agent-action-row");
        item.actions.forEach(function (action) {
          var button = make("button", null, action.label || "执行");
          button.type = "button";
          button.addEventListener("click", function () { handleAgentAction(action); });
          actions.appendChild(button);
        });
        node.appendChild(actions);
      }
      log.appendChild(node);
    });
    log.scrollTop = log.scrollHeight;
    updateAgentSessionMeta(state.data);
  }
  function appendAgentLine(role, text, actions, detail) {
    state.agent.history.push({ role: role, text: String(text || ""), actions: actions || [], detail: detail || "" });
    state.agent.history = state.agent.history.slice(-40);
    saveAgentSession();
    renderAgentHistory();
  }
  function setAgentBusy(busy) {
    state.agent.busy = busy;
    var drawer = el("chat-drawer");
    if (drawer) drawer.classList.toggle("busy", busy);
    if (el("agent-status")) el("agent-status").textContent = busy ? "处理中" : state.mode === "api" ? "本地引擎已连接" : "静态回退";
    ["agent-send", "agent-apply", "run-btn", "command-go"].forEach(function (id) { if (el(id)) el(id).disabled = busy; });
    document.querySelectorAll(".agent-suggestions button").forEach(function (button) { button.disabled = busy; });
  }
  function openChat(focusInput) {
    var drawer = el("chat-drawer");
    if (!drawer) return;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    if (el("chat-toggle")) { el("chat-toggle").textContent = "隐藏助理"; el("chat-toggle").setAttribute("aria-expanded", "true"); }
    if (focusInput && el("agent-input")) el("agent-input").focus();
  }
  function closeChat() {
    var drawer = el("chat-drawer");
    if (!drawer) return;
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    if (el("chat-toggle")) { el("chat-toggle").textContent = "打开助理"; el("chat-toggle").setAttribute("aria-expanded", "false"); }
  }
  function bindChat() {
    if (el("chat-toggle")) el("chat-toggle").addEventListener("click", function () { if (el("chat-drawer").classList.contains("open")) closeChat(); else openChat(true); });
    if (el("chat-close")) el("chat-close").addEventListener("click", closeChat);
    if (el("agent-clear")) el("agent-clear").addEventListener("click", clearAgentSession);
    if (el("agent-form")) el("agent-form").addEventListener("submit", function (event) { event.preventDefault(); sendAgentMessage(el("agent-input").value); });
    if (el("agent-apply")) el("agent-apply").addEventListener("click", applyScenario);
    if (el("agent-expiry")) el("agent-expiry").addEventListener("change", function () { selectExpiry(el("agent-expiry").value, "研究条件"); });
  }
  function appendChat(command, data) {
    var chat = data.chat || {};
    var agent = data.agent || {};
    var replyText = agent.message || (chat.scenario ? "条件已解析，快照已重算。" : "命令已执行，快照已刷新。");
    var detail = "";
    if (chat.scenario) {
      detail = "现金 " + fmt(chat.scenario.account_cash_hkd) + " HKD / 风险上限 " + fmt(chat.scenario.risk_budget_pct, 1) + "% / " + (chat.scenario.horizon || "当前期限");
    }
    var conclusion = "结论：" + humanVerdict((data.decisionCard || {}).verdict) + "。" + humanSummary((data.decisionCard || {}).summary);
    if (command) state.agent.history.push({ role: "user", text: String(command), actions: [], detail: "" });
    state.agent.history.push({ role: "agent", text: replyText, actions: agent.actions || [], detail: (detail ? detail + " · " : "") + conclusion });
    state.agent.history = state.agent.history.slice(-40);
    saveAgentSession();
    renderAgentHistory();
    if (agent.trace) renderAgentTrace(agent.trace);
  }
  var auditCache = { at: 0, entries: [], meta: null };
  function humanAuditEvent(event) {
    if (String(event).indexOf("agent_output:") === 0) return "角色输出 · " + String(event.split(":")[1] || "");
    var map = {
      debate_consensus: "辩论共识", scenario_parsed: "场景解析", decision_card: "决策卡",
      edge_gate: "机会判断", risk_gate: "风险判断", action_gate: "下一步",
      research_evidence: "投研证据", macro_assessment: "宏观研判", policy_event_promoted: "政策事件提升",
      data_loaded: "数据加载", engine_computed: "引擎计算", smoke_test: "冒烟测试",
      proposal: "方案", policy_library: "政策库",
    };
    return map[event] || event;
  }
  function paintAudit(entries, meta) {
    if (el("audit-meta")) el("audit-meta").textContent = "共 " + (meta.total || 0) + " 条 · 显示尾部 " + entries.length;
    var chain = el("audit-chain-note");
    if (chain) {
      chain.textContent = meta.chainOk ? "链完整（每条 prev_hash 均衔接前一条 hash）" : "链异常：存在断链，请核对审计文件";
      chain.dataset.tone = meta.chainOk ? "good" : "warn";
    }
    if (el("next-step-note")) {
      el("next-step-note").textContent = (meta.chainOk ? "审计链完整" : "审计链异常") + " · 共 " + (meta.total || 0) + " 条";
    }
    var wrap = el("audit-health");
    if (wrap) {
      wrap.innerHTML = "";
      wrap.appendChild(make("span", "health-pill " + (meta.chainOk ? "verified" : "failed"), meta.chainOk ? "哈希链完整" : "哈希链异常"));
      wrap.appendChild(make("span", "health-pill", "尾部 " + entries.length + " 条"));
    }
    var body = el("audit-entries");
    if (!body) return;
    body.innerHTML = "";
    entries.slice().reverse().forEach(function (item) {
      var tr = document.createElement("tr");
      tr.appendChild(make("td", null, String(item.seq)));
      tr.appendChild(make("td", "l", String(item.ts || "").slice(5, 19).replace("T", " ")));
      tr.appendChild(make("td", "l", humanAuditEvent(item.event)));
      tr.appendChild(make("td", "l", humanReason(item.summary)));
      var droppedTd = make("td", "l");
      if (item.droppedRefs && item.droppedRefs.length) {
        droppedTd.appendChild(make("span", "dropped-badge", item.droppedRefs.length + " 条"));
        droppedTd.appendChild(make("span", null, item.droppedRefs.join(", ")));
      } else {
        droppedTd.textContent = "—";
      }
      tr.appendChild(droppedTd);
      tr.appendChild(make("td", null, String(item.hash)));
      body.appendChild(tr);
    });
    if (el("audit-tail-note")) el("audit-tail-note").textContent = "哈希前缀 12 位；完整哈希与 payload 见 " + (meta.path || "research/audit/audit_log.jsonl");
  }
  function renderAudit() {
    if (Date.now() - auditCache.at < 5000 && auditCache.meta) { paintAudit(auditCache.entries, auditCache.meta); return; }
    fetch("/api/audit?limit=80").then(function (r) { return r.json(); }).then(function (payload) {
      if (!payload || !payload.found) throw new Error((payload && payload.error) || "审计不可用");
      auditCache = { at: Date.now(), entries: payload.entries || [], meta: payload };
      paintAudit(auditCache.entries, auditCache.meta);
    }).catch(function (err) {
      if (el("audit-meta")) el("audit-meta").textContent = "本地引擎未连接";
      if (el("audit-chain-note")) el("audit-chain-note").textContent = String(err && err.message || err);
      if (el("audit-entries")) el("audit-entries").innerHTML = "";
    });
  }

  var metricsCache = { at: 0, entries: [], meta: null };
  function paintMetrics(entries, meta) {
    if (el("metrics-meta")) {
      var stats = meta.stats || {};
      el("metrics-meta").textContent = "共 " + (meta.total || 0) + " 条 · 平均耗时 " + (stats.avgDurationMs != null ? fmt(stats.avgDurationMs / 1000, 1) + "s" : "--");
    }
    if (el("cap-latency")) {
      var last = entries.length ? entries[entries.length - 1] : null;
      el("cap-latency").textContent = last && last.durationMs ? fmt(last.durationMs / 1000, 1) + "s（" + last.event + "）" : "--";
    }
    var wrap = el("metrics-stats");
    if (wrap) {
      wrap.innerHTML = "";
      var verdicts = (meta.stats || {}).byVerdict || {};
      ["NO_TRADE", "BLOCK", "DRAFT_ONLY", "READY_FOR_CONFIRMATION"].forEach(function (kind) {
        if (verdicts[kind] === undefined) return;
        wrap.appendChild(make("span", "health-pill", humanVerdict(kind) + " " + verdicts[kind]));
      });
      var events = (meta.stats || {}).byEvent || {};
      Object.keys(events).forEach(function (event) { wrap.appendChild(make("span", "health-pill", event + " " + events[event])); });
    }
    var body = el("metrics-entries");
    if (!body) return;
    body.innerHTML = "";
    entries.slice().reverse().forEach(function (item) {
      var tr = document.createElement("tr");
      tr.appendChild(make("td", "l", String(item.ts || "").slice(5, 19).replace("T", " ")));
      tr.appendChild(make("td", "l", item.event));
      tr.appendChild(make("td", "l", humanReason(item.input) || "—"));
      tr.appendChild(make("td", null, item.verdict ? humanVerdict(item.verdict) : "—"));
      tr.appendChild(make("td", null, item.durationMs ? fmt(item.durationMs / 1000, 1) + "s" : "—"));
      tr.appendChild(make("td", "l", item.mode || "—"));
      body.appendChild(tr);
    });
  }
  function renderMetrics() {
    if (Date.now() - metricsCache.at < 10000 && metricsCache.meta) { paintMetrics(metricsCache.entries, metricsCache.meta); return; }
    fetch("/api/metrics?limit=20").then(function (r) { return r.json(); }).then(function (payload) {
      if (!payload || !payload.found) throw new Error((payload && payload.error) || "度量不可用");
      metricsCache = { at: Date.now(), entries: payload.entries || [], meta: payload };
      paintMetrics(metricsCache.entries, metricsCache.meta);
    }).catch(function () {
      if (el("metrics-meta")) el("metrics-meta").textContent = "本地引擎未连接";
    });
  }

  function renderAgent(D) {
    var terminal = D.terminal || {};
    var selection = terminal.selection || {};
    var rememberedScenario = (state.agent.context && state.agent.context.scenario) || {};
    var selected = state.expiry || (state.agent.context && state.agent.context.selectedExpiry) || selection.expiry;
    var current = currentExpiry(D);
    var agent = D.agent || {};
    var context = agent.message || (state.agent.context
      ? "已恢复当前工作集：现金 " + fmt(rememberedScenario.account_cash_hkd) + " HKD，风险上限 " + fmt(rememberedScenario.risk_budget_pct, 1) + "%；可以继续追问或直接重算。"
      : (current ? "当前选中 " + current.expiry + "；可以直接追问结论、风险预算，或展开研究条件重算。" : "连接本地引擎后，可直接追问当前结论。"));
    if (el("agent-context")) el("agent-context").textContent = context;
    if (el("agent-status") && !state.agent.busy) el("agent-status").textContent = state.mode === "api" ? "本地引擎已连接" : "静态回退";
    var scenario = Object.assign({}, D.scenario || (D.chat || {}).scenario || {}, rememberedScenario);
    if (el("agent-view") && scenario.view && document.activeElement !== el("agent-view")) el("agent-view").value = scenario.view;
    if (el("agent-cash") && scenario.account_cash_hkd != null && document.activeElement !== el("agent-cash")) el("agent-cash").value = scenario.account_cash_hkd;
    if (el("agent-budget") && scenario.risk_budget_pct != null && document.activeElement !== el("agent-budget")) el("agent-budget").value = scenario.risk_budget_pct;
    var expirySelect = el("agent-expiry");
    if (expirySelect) {
      clear("agent-expiry");
      (D.expiries || []).forEach(function (expiry) {
        var option = make("option", null, expiry.expiry + " / " + expiry.dte + " 天" + (expiry.primary ? " / 主到期" : ""));
        option.value = expiry.expiry;
        expirySelect.appendChild(option);
      });
      if (selected) expirySelect.value = selected;
    }
    var suggestions = el("agent-suggestions");
    if (suggestions) {
      clear("agent-suggestions");
      var items = agent.suggestions || [
        { label: "解释当前结论", message: "解释一下当前结论和主要原因" },
        { label: "检查风险预算", message: "检查当前方案的风险预算和最大亏损" },
        { label: "打开期权链", action: "open_view", view: "chain" },
        { label: "生成分歧记录", action: "debate" },
        { label: "刷新冻结快照", action: "refresh" },
        { label: "研究其他公司", message: "研究另一家公司的期权" },
      ];
      items.forEach(function (item) {
        var button = make("button", null, item.label || item.message || "执行");
        button.type = "button";
        button.addEventListener("click", function () {
          if (item.action === "refresh") requestAgent({ action: "refresh" }, item.label);
          else if (item.action === "select_expiry") requestAgent({ action: "select_expiry", expiry: item.expiry }, item.label);
          else if (item.action === "open_view") handleAgentAction({ type: "open_view", view: item.view, label: item.label });
          else if (item.action === "debate") handleAgentAction({ type: "debate", label: item.label });
          else sendAgentMessage(item.message || item.label);
        });
        suggestions.appendChild(button);
      });
    }
    updateAgentSessionMeta(D);
    if (agent.trace) renderAgentTrace(agent.trace);
  }
  function selectExpiry(expiry, source) {
    var D = state.data || DEFAULT;
    var item = (D && D.expiries || []).filter(function (candidate) { return candidate.expiry === expiry; })[0];
    if (!item) return;
    state.expiry = expiry;
    if (D.agent) {
      D.agent.selectedExpiry = expiry;
      D.agent.suggestions = null;
    }
    if (state.agent.context) {
      state.agent.context.selectedExpiry = expiry;
      saveAgentSession();
    }
    renderExpiryTabs(D);
    renderChain(D);
    renderOverview(D);
    renderRail(D);
    renderChart(D);
    renderAgent(D);
    note("已选择 " + expiry + " / " + (item.primary ? "主到期" : "次到期") + (source ? " · " + source : ""));
  }
  function applyData(data, command) {
    var payload = data && data.state && data.state.expiries ? data.state : data;
    if (!payload || payload.error || !payload.expiries) throw new Error((payload && payload.error) || "服务返回的数据不完整");
    var previousProject = state.data && state.data.workspace && state.data.workspace.activeProjectId;
    var nextProject = payload.workspace && payload.workspace.activeProjectId;
    if (previousProject && nextProject && previousProject !== nextProject) {
      state.agent.context = null;
      state.agent.history = [];
      saveAgentSession();
      clear("chat-log");
    }
    state.mode = "api";
    var selection = payload.terminal && payload.terminal.selection;
    if (selection && selection.expiry) state.expiry = selection.expiry;
    if (!state.expiry || !(payload.expiries || []).some(function (item) { return item.expiry === state.expiry; })) state.expiry = payload.expiries[0] ? payload.expiries[0].expiry : state.expiry;
    rememberAgentContext(payload, true);
    render(payload);
    appendChat(command, payload);
    if (payload.agent && payload.agent.intent === "navigation") (payload.agent.actions || []).forEach(handleAgentAction);
  }
  function currentAgentContext() {
    var D = state.data || DEFAULT || {};
    var remembered = state.agent.context || {};
    var scenario = Object.assign({}, D.scenario || {}, remembered.scenario || {});
    var account = D.account || {};
    var underlying = D.underlying || {};
    scenario.underlying = scenario.underlying || underlying.code || "标的";
    scenario.view = scenario.view || "uncertain";
    scenario.horizon = scenario.horizon || (((D.earnings || {}).date || "当前") + " 业绩");
    scenario.account_cash_hkd = scenario.account_cash_hkd != null ? scenario.account_cash_hkd : account.cashHkd;
    scenario.risk_budget_pct = scenario.risk_budget_pct != null ? scenario.risk_budget_pct : account.riskBudgetPct;
    scenario.constraints = Array.isArray(scenario.constraints) ? scenario.constraints : [];
    return { scenario: scenario, selectedExpiry: remembered.selectedExpiry || state.expiry, view: remembered.view || state.view };
  }
  function requestAgent(payload, command) {
    if (state.mode !== "api") { note("静态回退模式：启动桌面终端服务后才能执行助理动作。"); return; }
    if (state.agent.busy) return;
    var sequence = ++state.agent.sequence;
    var requestLabel = command || payload.message || payload.action;
    var requestBody = Object.assign({}, payload, { context: payload.context || currentAgentContext() });
    openChat(false);
    appendAgentLine("user", requestLabel);
    beginAgentTrace();
    setAgentBusy(true);
    fetch("/api/agent?no_audit=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody) })
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.error) || "HTTP " + response.status); return body; }); })
      .then(function (data) {
        if (sequence !== state.agent.sequence) return;
        applyData(data);
        note((data.agent && data.agent.message) || "助理动作已完成。");
      })
      .catch(function (error) {
        if (sequence !== state.agent.sequence) return;
        var drawer = el("chat-drawer");
        if (drawer) drawer.classList.add("error");
        note("助理动作失败：" + error.message);
        appendAgentLine("agent", "这次没有完成：" + error.message + "。可以修改条件后重试。");
      })
      .finally(function () {
        if (sequence === state.agent.sequence) {
          setAgentBusy(false);
          var drawer = el("chat-drawer");
          if (drawer) drawer.classList.remove("error");
        }
      });
  }
  function requestTerminalCommand(command) {
    if (state.mode !== "api") { note("静态回退模式：启动桌面终端服务后才能运行标的命令。"); return; }
    if (state.agent.busy || state.projectBusy) return;
    setAgentBusy(true);
    fetch("/api/command?no_audit=1", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: command }) })
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error((body && body.error) || "HTTP " + response.status); return body; }); })
      .then(function (data) { applyData(data, command); note("已执行终端命令：" + command); })
      .catch(function (error) { note("终端命令失败：" + error.message); })
      .finally(function () { setAgentBusy(false); });
  }
  function sendAgentMessage(message) {
    var value = String(message || "").trim();
    if (!value) { openChat(true); note("先输入一个研究问题或条件。"); return; }
    if (el("agent-input")) el("agent-input").value = "";
    if (el("scenario-input")) el("scenario-input").value = "";
    requestAgent({ action: "ask", message: value }, value);
  }
  function scenarioFromControls() {
    var D = state.data || DEFAULT || {};
    var underlying = D.underlying || {};
    var cash = Number(el("agent-cash") && el("agent-cash").value);
    var budget = Number(el("agent-budget") && el("agent-budget").value);
    if (!isFinite(cash) || cash <= 0) throw new Error("现金必须是大于 0 的数字");
    if (!isFinite(budget) || budget <= 0 || budget > 100) throw new Error("风险上限必须在 0 到 100% 之间");
    var earnings = D.earnings || {};
    return {
      scenario: {
        underlying: underlying.code || "标的",
        view: el("agent-view") ? el("agent-view").value : "uncertain",
        horizon: (earnings.date || "当前") + " 业绩",
        account_cash_hkd: cash,
        risk_budget_pct: budget,
        constraints: ["单笔最多亏损 " + budget + "%"],
      },
      expiry: el("agent-expiry") ? el("agent-expiry").value : state.expiry,
    };
  }
  function applyScenario() {
    try {
      var values = scenarioFromControls();
      requestAgent({ action: "run_scenario", scenario: values.scenario, expiry: values.expiry }, "按条件重算");
    } catch (error) {
      note("研究条件无效：" + error.message);
      appendAgentLine("agent", "条件没有提交：" + error.message + "。");
    }
  }
  function runCommand(command, triggerChat) {
    var value = String(command || "").trim();
    if (!value) value = ((state.data && state.data.underlying && humanSymbol(state.data.underlying.code)) || "标的") + " <GO>";
    if (triggerChat) openChat(false);
    var normalized = value.replace(/\s+/g, " ").toUpperCase();
    if (normalized === "REFRESH" || normalized === "F5" || normalized === "RUN" || normalized === "RUN <GO>") requestAgent({ action: "refresh" }, value);
    else if (/<GO>\s*$/.test(normalized)) requestTerminalCommand(value);
    else sendAgentMessage(value);
  }
  function bindCommands() {
    if (el("cap-run")) el("cap-run").addEventListener("click", function () { requestAgent({ action: "refresh" }, "运行管线（Replay）"); });
    if (el("command-go")) el("command-go").addEventListener("click", function () { runCommand(el("terminal-command").value, false); });
    if (el("terminal-command")) el("terminal-command").addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); runCommand(el("terminal-command").value, false); } });
    if (el("run-btn")) el("run-btn").addEventListener("click", function () { sendAgentMessage(el("scenario-input").value); });
    if (el("scenario-input")) el("scenario-input").addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); sendAgentMessage(el("scenario-input").value); } });
    document.addEventListener("keydown", function (event) {
      if (event.key === "F5") { event.preventDefault(); requestAgent({ action: "refresh" }, "F5 刷新"); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openChat(true); }
      if (event.key === "Escape") { setSettingsOpen(false); setProjectOpen(false); closeChat(); }
    });
  }

  function boot() {
    if (!DEFAULT) { note("缺少本地快照。"); return; }
    restoreAgentSession();
    state.expiry = (state.agent.context && state.agent.context.selectedExpiry) || (DEFAULT.expiries && DEFAULT.expiries[0] ? DEFAULT.expiries[0].expiry : null);
    state.view = (state.agent.context && state.agent.context.view) || "overview";
    bindSettings(); bindWorkspace(); bindViews(); bindChartControls(); bindDebateToggle(); bindChat(); bindCommands(); bindCardActions(); render(DEFAULT); renderAgentHistory();
    fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
      .then(function (data) {
        state.mode = "api";
        render(data);
        var savedProject = storedSetting(PROJECT_STORAGE_KEY, "");
        var projects = data.workspace && data.workspace.projects || [];
        if (savedProject && savedProject !== (data.workspace && data.workspace.activeProjectId) && projects.some(function (project) { return project.id === savedProject && project.available !== false; })) selectProject(savedProject);
        else note(data.meta && data.meta.mode === "LIVE" ? "已连接本地引擎：实时行情推送。" : "已连接本地引擎：回放快照。");
      })
      .catch(function () { state.mode = "static"; render(DEFAULT); note("静态回退：data.js 快照。桌面服务未连接。"); });
  }
  boot();
})();
