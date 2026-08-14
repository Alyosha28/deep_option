/* GOAI 期权智能终端 — 前端渲染与交互（Terminal UI）
 * 数据来源：/api/state（本地只读服务）；服务不可用时回退 data.js 静态快照。
 * 前端零计算：只做格式化与渲染，所有数字来自后端；动态文本一律 textContent，防注入。
 */
(function () {
  "use strict";

  var DEFAULT = window.GOAI_DATA || null;
  var state = { mode: "static", expiry: null, view: "overview" };

  function el(id) { return document.getElementById(id); }
  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function clear(id) { el(id).innerHTML = ""; }

  function fmt(n, digits) {
    var value = Number(n);
    if (!isFinite(value)) return "--";
    return value.toLocaleString("zh-CN", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0,
    });
  }
  function fmtSigned(n) {
    var value = Number(n);
    if (!isFinite(value)) return "--";
    return (value > 0 ? "+" : "") + fmt(value);
  }
  function signClass(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }

  /* ── 时钟 ── */
  function tick() {
    var now = new Date();
    var t = el("clock");
    if (t) t.textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }
  tick(); setInterval(tick, 1000);

  function note(text) {
    var chip = el("footnote-chip");
    if (chip) chip.textContent = text;
  }

  /* ── 顶栏 + 行情带 ── */
  function renderTopbar(D) {
    var meta = D.meta || {};
    var chip = el("mode-chip");
    chip.textContent = (state.mode === "api" ? "API · " : "STATIC · ") + meta.mode + " · " + meta.freshness;
    chip.className = "chip " + (meta.freshness === "FRESH" ? "chip-ok" : meta.freshness === "STALE" ? "chip-warn" : "chip-accent");

    var u = D.underlying || {};
    var e = D.earnings || {};
    var spot = Number(u.spot);
    var prev = Number(u.prevClose);
    el("t-spot").textContent = fmt(spot, 2);
    var chg = el("t-chg");
    chg.textContent = isFinite(prev) && prev ? fmtSigned((spot - prev) / prev * 100) + "%" : "--";
    chg.className = "tape-value " + signClass(spot - prev);
    el("t-iv").textContent = e.iv != null ? fmt(e.iv, 1) + "%" : "--";
    el("t-ivrank").textContent = e.ivRank != null ? fmt(e.ivRank, 1) : "--";
    el("t-move").textContent = e.expectedMovePct != null ? "±" + fmt(e.expectedMovePct, 2) + "%" : "--";
    el("t-earnings").textContent = e.date || "--";
    el("t-prev").textContent = fmt(prev, 2);

    var lc = el("llm-chip");
    var badge = D.llm || {};
    if (badge.available) {
      lc.textContent = "LLM · " + (badge.provider || "--") + " · " + (badge.model || "--");
      lc.className = "chip chip-ok";
    } else {
      lc.textContent = "LLM · offline 确定性回退";
      lc.className = "chip chip-warn";
    }
  }

  /* ── verdict banner 公共渲染 ── */
  function verdictClass(action) {
    if (action === "NO_TRADE") return "NO_TRADE";
    if (action === "BLOCK") return "BLOCK";
    if (action === "DRAFT_ONLY") return "DRAFT_ONLY";
    if (action === "READY_FOR_CONFIRMATION") return "READY_FOR_CONFIRMATION";
    return "NO_TRADE";
  }

  function renderVerdict(ids, card) {
    var v = String(card.verdict || "--");
    var vClass = verdictClass(v);
    el(ids.v).textContent = v;
    el(ids.s).textContent = card.summary || "--";
    el(ids.b).className = "verdict-banner " + vClass;
    el(ids.v).className = "verdict-code " + vClass;
  }

  /* ── 三门控（概览） ── */
  function renderGates(card) {
    var eg = card.edgeGate || {};
    var ev = el("edge-verdict");
    ev.textContent = eg.verdict || "--";
    ev.className = "tag " + (eg.verdict === "ADEQUATE" ? "tag-pass" : "tag-fail");
    clear("edge-checks");
    (eg.checks || []).forEach(function (check) {
      var li = document.createElement("li");
      li.appendChild(make("span", "result " + String(check.result).toLowerCase(), check.result));
      li.appendChild(make("span", null, check.check + " — " + check.detail));
      el("edge-checks").appendChild(li);
    });

    var rg = card.riskGate || {};
    var rv = el("risk-verdict");
    rv.textContent = rg.decision || "--";
    rv.className = "tag " + (rg.decision === "BLOCK" ? "tag-fail" : "tag-pass");
    clear("risk-checks");
    (rg.findings || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", "result " + String(item.kind).toLowerCase(), item.kind));
      li.appendChild(make("span", null, item.text));
      el("risk-checks").appendChild(li);
    });

    var ag = card.actionGate || {};
    var av = el("action-verdict");
    av.textContent = ag.action || "--";
    av.className = "tag " + (ag.action === "NO_TRADE" || ag.action === "BLOCK" ? "tag-fail" : ag.action === "READY_FOR_CONFIRMATION" ? "tag-pass" : "tag-info");
    el("action-next").textContent = ag.nextStep || "--";
    clear("action-blocked");
    (ag.blocked || []).forEach(function (reason) {
      var li = document.createElement("li");
      li.appendChild(make("span", "result fail", "✕"));
      li.appendChild(make("span", null, reason));
      el("action-blocked").appendChild(li);
    });
  }

  /* ── Greeks 复用 ── */
  function renderGreeksInto(prefix, g) {
    var map = { delta: "Δ", gamma: "Γ", vega: "ν", theta: "Θ", rho: "ρ" };
    Object.keys(map).forEach(function (k) {
      var n = el(prefix + "-" + k);
      if (!n) return;
      if (!g) { n.textContent = "--"; return; }
      var v = Number(g[k]);
      n.textContent = fmt(v, 2);
      n.className = "g-val " + signClass(v);
    });
  }

  function currentExpiry(D) {
    var match = D.expiries.filter(function (item) { return item.expiry === state.expiry; })[0];
    return match || D.expiries[0];
  }

  /* ── 01 总览 ── */
  function renderOverview(D) {
    var card = D.decisionCard;
    el("ov-meta").textContent = (D.meta.capturedAt || "") + " · " + (D.meta.source || "");
    renderVerdict({ v: "verdict", s: "summary", b: "verdict-banner" }, card);
    renderGates(card);

    var expiry = currentExpiry(D);
    var s = expiry.strategy;
    el("ov-expiry").textContent = expiry.expiry.slice(5) + " · " + expiry.dte + " DTE";
    el("ov-cost-ask").textContent = fmt(s.costPerLotAsk);
    el("ov-cost-exec").textContent = fmt(s.costPerLotExec);
    el("ov-maxloss").textContent = fmt(s.maxLoss);
    el("ov-budget").textContent = "预算 " + fmt(D.account.cashHkd * D.account.riskBudgetPct / 100) + " HKD";
    el("ov-lots").textContent = s.lots + " 张 @ " + fmt(expiry.strike, 0);
    renderGreeksInto("g", s.greeks);
    el("ov-breakeven").textContent = "盈亏平衡 " + fmt(s.breakeven[0], 2) + " / " + fmt(s.breakeven[1], 2) + "（现价 " + fmt(D.underlying.spot, 2) + "）";
    el("ov-snapshot").textContent =
      "sha256 " + String(D.meta.snapshotSha256 || "").slice(0, 16) + "…" +
      "\n" + "origin " + (D.meta.origin || "--") + " · market " + (D.meta.marketState || "--");
    el("ov-freshness").textContent = "freshness " + (D.meta.freshness || "--") + " · mode " + (D.meta.mode || "--");

    clear("evidence-list");
    (card.keyEvidence || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", "claim", item.claim));
      li.appendChild(make("span", "src", item.source + " · " + item.capturedAt));
      el("evidence-list").appendChild(li);
    });
  }

  /* ── 02 决策卡 ── */
  function renderDecisionCard(D) {
    var card = D.decisionCard;
    el("card-meta").textContent = (D.meta.generatedAt || "--") + " · " + (D.meta.source || "");
    renderVerdict({ v: "verdict-2", s: "summary-2", b: "verdict-banner-2" }, card);

    clear("evidence-list-2");
    (card.keyEvidence || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", "claim", item.claim));
      li.appendChild(make("span", "src", item.source));
      el("evidence-list-2").appendChild(li);
    });

    clear("conditions-list");
    (card.conditionsThatChange || []).forEach(function (text) {
      el("conditions-list").appendChild(make("li", null, text));
    });

    var e = D.earnings || {};
    el("earnings-date").textContent = e.date ? e.date + (e.quarter ? " · " + e.quarter : "") : "--";
    el("eps-yoy").textContent = e.estimateEpsYoy == null ? "--" : fmtSigned(e.estimateEpsYoy) + "%";
    el("rev-yoy").textContent = e.estimateRevenueYoy == null ? "--" : fmtSigned(e.estimateRevenueYoy) + "%";
    el("crush-history").textContent = e.lastReportIvCrush == null ? "--" : fmt(e.lastReportIvCrush, 2) + " / " + fmt(e.historyReportIvCrush, 2) + " pp";
    el("captured-at").textContent = D.meta.capturedAt + " · " + D.meta.source;

    clear("audit-list");
    var audit = card.auditTrail || [];
    if (!audit.length) {
      el("audit-list").appendChild(make("li", null, "审计哈希链见 research/audit/audit_log.jsonl（本地）"));
    }
    audit.forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", null, item.event));
      li.appendChild(make("span", "hash", item.hash));
      el("audit-list").appendChild(li);
    });
  }

  /* ── 03 期权链 ── */
  function spreadPct(leg) { return ((leg.ask - leg.bid) / leg.mid) * 100; }
  function liquidityClass(pct) {
    if (pct <= 8) return "good";
    if (pct <= 15) return "warn";
    return "bad";
  }

  function renderChain(D) {
    el("chain-meta").textContent = "冻结快照 · 仅 ATM 合约";
    var body = el("chain-body");
    body.innerHTML = "";
    D.expiries.forEach(function (expiry) {
      var callSpread = spreadPct(expiry.call);
      var putSpread = spreadPct(expiry.put);
      var worst = Math.max(callSpread, putSpread);
      var tr = document.createElement("tr");
      var cells = [
        { t: expiry.expiry.slice(5) + (expiry.primary ? " ★" : ""), l: true },
        { t: fmt(expiry.call.bid, 2) },
        { t: fmt(expiry.call.ask, 2) },
        { t: fmt(expiry.call.apiIvPct, 1) + "%" },
        { t: fmt(expiry.call.openInterest) },
        { t: fmt(expiry.strike, 0), strike: true },
        { t: fmt(expiry.put.apiIvPct, 1) + "%" },
        { t: fmt(expiry.put.openInterest) },
        { t: fmt(expiry.put.bid, 2) },
        { t: fmt(expiry.put.ask, 2) },
      ];
      cells.forEach(function (cell) {
        var td = document.createElement("td");
        if (cell.l) td.className = "l";
        if (cell.strike) td.className = "strike";
        td.textContent = cell.t;
        tr.appendChild(td);
      });
      var spreadTd = document.createElement("td");
      spreadTd.appendChild(make("span", "liquidity " + liquidityClass(worst)));
      spreadTd.appendChild(document.createTextNode(" " + worst.toFixed(1) + "%"));
      tr.appendChild(spreadTd);
      body.appendChild(tr);
    });
  }

  function renderStrategy(D) {
    var expiry = currentExpiry(D);
    var s = expiry.strategy;
    el("strategy-expiry-label").textContent = expiry.expiry.slice(5) + " · " + expiry.dte + " DTE";
    if (!s) {
      el("strategy-lots").textContent = "— 未纳入策略";
      ["cost-ask", "cost-exec", "max-loss"].forEach(function (id) { el(id).textContent = "—"; });
      renderGreeksInto("g-2", null);
      el("breakeven").textContent = "该到期未纳入策略计算。";
      return;
    }
    el("strategy-lots").textContent = s.lots + " 张 @ " + fmt(expiry.strike, 0);
    el("cost-ask").textContent = fmt(s.costPerLotAsk);
    el("cost-exec").textContent = fmt(s.costPerLotExec);
    el("max-loss").textContent = fmt(s.maxLoss);
    renderGreeksInto("g-2", s.greeks);
    el("breakeven").textContent = "盈亏平衡 " + fmt(s.breakeven[0], 2) + " / " + fmt(s.breakeven[1], 2);
  }

  function drawPayoff(D) {
    var svg = el("payoff-chart");
    var ns = "http://www.w3.org/2000/svg";
    svg.innerHTML = "";
    var primary = D.expiries[0];
    if (!primary || !primary.strategy.pnlAtExpiry) return;

    var w = 560, h = 250, padL = 56, padR = 14, padT = 14, padB = 30;
    var xMin = 425, xMax = 525, yMin = -1600, yMax = 4200;
    function x(v) { return padL + ((v - xMin) / (xMax - xMin)) * (w - padL - padR); }
    function y(v) { return h - padB - ((v - yMin) / (yMax - yMin)) * (h - padT - padB); }
    function node(name, attrs) {
      var n = document.createElementNS(ns, name);
      Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
      return n;
    }

    svg.appendChild(node("line", { x1: x(xMin), y1: y(0), x2: x(xMax), y2: y(0), stroke: "rgba(139,152,169,0.4)", "stroke-dasharray": "4 4" }));
    [-1000, 0, 1000, 2000, 3000, 4000].forEach(function (v) {
      svg.appendChild(node("line", { x1: x(xMin), y1: y(v), x2: x(xMax), y2: y(v), stroke: "rgba(139,152,169,0.07)" }));
      var label = node("text", { x: x(xMin) - 8, y: y(v) + 4, "text-anchor": "end" });
      label.setAttribute("fill", "#59687b"); label.setAttribute("font-size", "10");
      label.textContent = v >= 0 ? "+" + fmt(v) : fmt(v);
      svg.appendChild(label);
    });

    var points = [];
    primary.strategy.pnlAtExpiry.forEach(function (group) {
      group.rows.forEach(function (row) { points.push({ spot: row.spot, pnl: row.pnl }); });
    });
    points.sort(function (a, b) { return a.spot - b.spot; });

    var dPath = points.map(function (p, i) {
      return (i === 0 ? "M" : "L") + x(p.spot).toFixed(1) + " " + y(p.pnl).toFixed(1);
    }).join(" ");
    var areaPath = dPath + " L " + x(points[points.length - 1].spot).toFixed(1) + " " + y(0).toFixed(1) + " L " + x(points[0].spot).toFixed(1) + " " + y(0).toFixed(1) + " Z";
    svg.appendChild(node("path", { d: areaPath, fill: "rgba(255,180,84,0.08)" }));
    svg.appendChild(node("path", { d: dPath, fill: "none", stroke: "#ffb454", "stroke-width": 2, "stroke-linejoin": "round" }));

    points.forEach(function (p) {
      svg.appendChild(node("circle", { cx: x(p.spot), cy: y(p.pnl), r: 3.5, fill: p.pnl >= 0 ? "#4db250" : "#f05143" }));
    });

    primary.strategy.breakeven.forEach(function (b) {
      svg.appendChild(node("line", { x1: x(b), y1: y(yMin), x2: x(b), y2: y(0), stroke: "rgba(255,180,84,0.5)", "stroke-dasharray": "3 4" }));
    });
    svg.appendChild(node("line", { x1: x(D.underlying.spot), y1: y(yMin), x2: x(D.underlying.spot), y2: y(yMax), stroke: "rgba(90,169,255,0.5)", "stroke-dasharray": "2 5" }));
    var spotLabel = node("text", { x: x(D.underlying.spot) + 4, y: padT + 8, fill: "#5aa9ff", "font-size": "10" });
    spotLabel.textContent = "现价 " + fmt(D.underlying.spot, 2);
    svg.appendChild(spotLabel);
    [440, 460, 480, 500, 520].forEach(function (v) {
      var label = node("text", { x: x(v), y: h - 8, "text-anchor": "middle", fill: "#59687b", "font-size": "10" });
      label.textContent = v;
      svg.appendChild(label);
    });
  }

  function drawIvCrush(D) {
    var wrap = el("iv-crush");
    wrap.innerHTML = "";
    var primary = D.expiries[0];
    if (!primary || !primary.strategy.ivCrush) return;
    var maxAbs = 0;
    primary.strategy.ivCrush.forEach(function (g) {
      g.rows.forEach(function (r) { maxAbs = Math.max(maxAbs, Math.abs(r.pnl)); });
    });
    primary.strategy.ivCrush.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "crush-row";
      row.appendChild(make("span", "crush-label", "IV " + (g.iv_crush || g.label || "")));
      var bars = document.createElement("div");
      bars.className = "crush-bars";
      g.rows.forEach(function (r) {
        var bar = document.createElement("div");
        bar.className = "crush-bar";
        var inner = document.createElement("i");
        inner.className = r.pnl >= 0 ? "gain" : "loss";
        inner.style.width = Math.max(2, (Math.abs(r.pnl) / maxAbs) * 100) + "%";
        bar.appendChild(inner);
        bars.appendChild(bar);
      });
      row.appendChild(bars);
      row.appendChild(make("span", "crush-val", g.rows.map(function (r) {
        return (r.direction === "up" ? "↑" : "↓") + " " + fmtSigned(r.pnl);
      }).join("  ")));
      wrap.appendChild(row);
    });
  }

  /* ── 04 宏观 ── */
  function renderMacro(D) {
    var macro = D.macro || {};
    var library = D.policyLibrary || {};
    if (!macro.available) {
      el("macro-confidence").textContent = "未启用";
      ["sentiment-index", "sentiment-verdict", "iv-state", "skew-verdict", "principal-pair"].forEach(function (id) { el(id).textContent = "--"; });
      el("macro-mood").textContent = "未提供宏观研判输入";
      clear("scenario-list"); clear("library-health"); clear("promoted-list");
      el("macro-note").textContent = "启动本地服务（python -m src.ui_server）以加载宏观研判。";
      return;
    }
    var judgment = macro.macro_judgment || {};
    el("macro-confidence").textContent = "置信度 " + (judgment.confidence || "--");
    var sentiment = macro.sentiment || {};
    el("sentiment-index").textContent = fmt(sentiment.index, 1);
    el("sentiment-verdict").textContent = sentiment.verdict || "--";
    var iv = macro.iv_emotion || {};
    el("iv-state").textContent = iv.state || "--";
    el("skew-verdict").textContent = "Skew " + (iv.skew_verdict || "--");
    var policy = macro.policy_analysis || {};
    var principal = policy.principal_contradiction;
    el("principal-pair").textContent = principal ? principal.pair : "--";
    el("macro-mood").textContent = "情绪基调：" + (judgment.mood || "--") + (judgment.contrarian_note ? "。" + judgment.contrarian_note : "");

    clear("scenario-list");
    (judgment.scenarios || []).forEach(function (scenario) {
      var li = document.createElement("li");
      li.appendChild(make("span", "scenario-name", scenario.name));
      li.appendChild(make("span", "scenario-level", "定性可能性 " + (scenario.likelihood_level || "--")));
      li.appendChild(make("span", "scenario-implication", (scenario.market_implication || "") + "；期权：" + (scenario.option_implication || "")));
      el("scenario-list").appendChild(li);
    });

    renderHealthPills("library-health", library, true);
    clear("promoted-list");
    var promoted = (library.health || {}).recently_promoted || [];
    if (!promoted.length) el("promoted-list").appendChild(make("li", null, "暂无自动激活事件"));
    promoted.forEach(function (item) {
      el("promoted-list").appendChild(make("li", null, "最近激活 " + item.id + " · " + item.promoted_at + (item.promoted_by ? " · " + item.promoted_by : "")));
    });
    el("macro-note").textContent = macro.disclaimer || "";
  }

  function renderHealthPills(id, library, withCount) {
    var wrap = el(id);
    wrap.innerHTML = "";
    var health = (library.health || {}).verification || {};
    ["VERIFIED", "PENDING", "FAILED", "UNKNOWN"].forEach(function (kind) {
      if (health[kind] === undefined) return;
      wrap.appendChild(make("span", "health-pill " + kind.toLowerCase(), kind + " " + health[kind]));
    });
    if (withCount) wrap.appendChild(make("span", "health-pill", "事件 " + (library.eventCount || 0)));
  }

  /* ── 05 投研 ── */
  function renderResearch(D) {
    var research = D.research || {};
    if (!research.available) {
      el("research-meta").textContent = "未提供投研输入";
      ["rs-item-count", "rs-synthetic", "rs-stock-verdict", "rs-option-verdict"].forEach(function (id) { el(id).textContent = "--"; });
      clear("research-items");
      el("research-note").textContent = "启动本地服务（python -m src.ui_server）以加载投研证据；data/research_items_hero.json 为 synthetic 示例。";
      return;
    }
    var digest = research.digest || {};
    el("research-meta").textContent = digest.item_count != null ? digest.item_count + " 条条目" : "--";
    el("rs-item-count").textContent = digest.item_count != null ? String(digest.item_count) : "--";
    el("rs-synthetic").textContent = "synthetic-only: " + (digest.synthetic_only != null ? String(digest.synthetic_only) : "--");
    var stock = research.stock_price_impact || {};
    var option = research.option_impact || {};
    el("rs-stock-verdict").textContent = stock.verdict || "--";
    el("rs-option-verdict").textContent = "期权影响： " + (option.verdict || "--");

    var rows = [];
    ["announcements", "earnings_items", "news", "reports", "industry"].forEach(function (kind) {
      (research[kind] || []).forEach(function (item) {
        if (item && typeof item === "object") rows.push(item);
      });
    });
    var tbody = el("research-items");
    tbody.innerHTML = "";
    if (!rows.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 5; td.className = "l";
      td.textContent = "条目明细未在 API 中展开（见 research/ 证据文件）。";
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    rows.slice(0, 50).forEach(function (item) {
      var tr = document.createElement("tr");
      [item.title || item.headline || item.id || "--",
       item.kind || item.type || "--",
       item.sentiment || "--",
       item.date || "--",
       item.source || item.publisher || "--"].forEach(function (text, idx) {
        var td = document.createElement("td");
        td.className = idx === 0 || idx === 1 || idx === 2 || idx === 4 ? "l" : "";
        td.textContent = String(text);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    el("research-note").textContent = research.disclaimer || "";
  }

  /* ── 06 政策库 ── */
  function renderLibrary(D) {
    var library = D.policyLibrary || {};
    if (!library.eventCount && !(library.events && library.events.length)) {
      el("library-meta").textContent = "不可用";
      renderHealthPills("library-health-2", library, false);
      clear("library-events");
      el("library-note").textContent = "启动本地服务以加载政策事件库。";
      return;
    }
    el("library-meta").textContent = library.eventCount + " 个事件 · " + (library.path || "");
    renderHealthPills("library-health-2", library, false);
    var tbody = el("library-events");
    tbody.innerHTML = "";
    (library.events || []).forEach(function (event) {
      var tr = document.createElement("tr");
      var statusClass = event.status === "ACTIVE" ? "pos" : event.status === "FAILED" ? "neg" : "";
      var cells = [
        { t: event.id, l: true },
        { t: event.name, l: true },
        { t: event.date },
        { t: event.type, l: true },
        { t: event.status, l: true, c: statusClass },
        { t: (event.verdictReads || []).join(", "), l: true },
      ];
      cells.forEach(function (cell) {
        var td = document.createElement("td");
        if (cell.l) td.className = "l";
        if (cell.c) td.classList.add(cell.c);
        td.textContent = cell.t;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    el("library-note").textContent = "来源核验状态只是可审计标记；PENDING/FAILED 不代表事实错误，正式使用前必须逐条复核。";
  }

  /* ── 07 辩论 ── */
  var STANCE_LABEL = { favor: "赞成", oppose: "反对", neutral: "中性" };
  var CONFIDENCE_LABEL = { high: "高置信", medium: "中置信", low: "低置信" };
  var ROLE_STATUS_LABEL = { ok: "完成", error: "失败", timeout: "超时", parse_error: "输出非法", skipped: "跳过" };

  function debateStatusText(trace) {
    if (!trace) return "未运行（仅确定性管线）";
    if (trace.status === "offline") return "离线回退 · 未配置 API Key";
    if (trace.status === "failed") return "辩论失败 · 已回退确定性管线";
    if (trace.status === "degraded") return "部分角色降级 · 确定性结论不变";
    if (trace.status === "complete") return "两轮辩论完成";
    return String(trace.status || "未知");
  }

  function appendRefs(container, refs, dropped) {
    (refs || []).forEach(function (ref) { container.appendChild(make("span", "ref-pill", ref)); });
    (dropped || []).forEach(function (ref) { container.appendChild(make("span", "ref-pill ref-pill-dropped", "✕ " + ref)); });
  }

  function renderDebateEntry(roundWrap, entry) {
    var card = document.createElement("div");
    card.className = "debate-entry";
    var head = document.createElement("div");
    head.className = "debate-entry-head";
    head.appendChild(make("span", "debate-role", entry.name + " · " + entry.title));
    head.appendChild(make("span", "debate-status st-" + String(entry.status || "error"), ROLE_STATUS_LABEL[entry.status] || String(entry.status || "未知")));
    if (entry.stance) head.appendChild(make("span", "stance-pill stance-" + entry.stance, STANCE_LABEL[entry.stance] || entry.stance));
    if (entry.confidence) head.appendChild(make("span", "debate-conf", CONFIDENCE_LABEL[entry.confidence] || entry.confidence));
    card.appendChild(head);
    if (entry.conclusion) card.appendChild(make("p", "debate-conclusion", entry.conclusion));
    if (entry.counterpoint) card.appendChild(make("p", "debate-counterpoint", "回辩：" + entry.counterpoint));
    if (entry.error) card.appendChild(make("p", "debate-error", "错误：" + entry.error));
    var refs = document.createElement("div");
    refs.className = "debate-refs";
    appendRefs(refs, entry.evidence_refs, entry.dropped_refs);
    if (refs.childNodes.length) card.appendChild(refs);
    if (entry.duration_ms || entry.tokens || entry.model) {
      var metaLine = document.createElement("p");
      metaLine.className = "debate-entry-meta";
      metaLine.textContent = (entry.model ? entry.model + " · " : "") + (entry.duration_ms ? (entry.duration_ms / 1000).toFixed(1) + "s" : "") + (entry.tokens ? " · " + entry.tokens + " tokens" : "");
      card.appendChild(metaLine);
    }
    roundWrap.appendChild(card);
  }

  function renderDebate(D) {
    var trace = D.debateTrace || null;
    var consensus = D.researchConsensus || (trace ? trace.research_consensus : null) || null;
    el("debate-summary").textContent = debateStatusText(trace);

    var meta = el("debate-meta");
    meta.innerHTML = "";
    if (trace) {
      var metrics = trace.metrics || {};
      var cells = [
        ["状态", debateStatusText(trace)],
        ["引擎 verdict", trace.verdict || "--"],
        ["提供商", trace.provider || "无（离线回退）"],
        ["耗时", metrics.elapsed_ms != null ? (metrics.elapsed_ms / 1000).toFixed(1) + " s" : "--"],
        ["token", metrics.total_tokens != null ? String(metrics.total_tokens) : "--"],
        ["有效角色", metrics.ok_roles != null ? String(metrics.ok_roles) : "--"],
      ];
      cells.forEach(function (pair) {
        var cell = document.createElement("div");
        cell.className = "debate-meta-cell";
        cell.appendChild(make("span", null, pair[0]));
        cell.appendChild(make("strong", null, pair[1]));
        meta.appendChild(cell);
      });
    } else {
      meta.appendChild(make("p", "note-line", "十角色辩论需配置 DeepSeek（OpenAI 兼容）API Key；无 key 时终端自动回退确定性管线。"));
    }

    var rounds = el("debate-rounds");
    rounds.innerHTML = "";
    var roundList = (trace && trace.rounds) || [];
    if (!roundList.length) rounds.appendChild(make("p", "note-line", "尚未运行十角色辩论。"));
    roundList.forEach(function (round) {
      var section = document.createElement("div");
      section.className = "debate-round-block";
      section.appendChild(make("h4", "debate-round-head", "第 " + round.round + " 轮"));
      (round.entries || []).forEach(function (entry) { renderDebateEntry(section, entry); });
      rounds.appendChild(section);
    });

    var disputes = el("debate-disputes");
    disputes.innerHTML = "";
    var disputeList = (trace && trace.disputes) || [];
    if (!disputeList.length) disputes.appendChild(make("p", "note-line", "无分歧点。"));
    disputeList.forEach(function (dispute, index) {
      var card = document.createElement("div");
      card.className = "debate-dispute";
      card.appendChild(make("h4", null, "分歧 " + (index + 1) + " · " + dispute.topic));
      card.appendChild(make("p", null, dispute.question));
      if (dispute.roles && dispute.roles.length) card.appendChild(make("span", "dispute-roles", "相关角色：" + dispute.roles.join(" / ")));
      disputes.appendChild(card);
    });

    var consensusWrap = el("debate-consensus");
    consensusWrap.innerHTML = "";
    if (!consensus) { consensusWrap.appendChild(make("p", "note-line", "暂无研究共识。")); return; }
    var head = document.createElement("div");
    head.className = "consensus-head";
    head.appendChild(make("span", "consensus-source", consensus.source === "llm" ? "LLM 汇总" : "确定性回退"));
    if (consensus.stance) head.appendChild(make("span", "stance-pill stance-" + consensus.stance, "共识：" + (STANCE_LABEL[consensus.stance] || consensus.stance)));
    if (consensus.confidence) head.appendChild(make("span", "debate-conf", CONFIDENCE_LABEL[consensus.confidence] || consensus.confidence));
    consensusWrap.appendChild(head);
    consensusWrap.appendChild(make("p", "consensus-summary", consensus.summary || ""));
    var refs = document.createElement("div");
    refs.className = "debate-refs";
    appendRefs(refs, consensus.evidence_refs, []);
    if (refs.childNodes.length) consensusWrap.appendChild(refs);
    var questions = consensus.open_questions || [];
    if (questions.length) {
      var list = document.createElement("ul");
      list.className = "open-questions";
      questions.forEach(function (question) { list.appendChild(make("li", null, question)); });
      consensusWrap.appendChild(list);
    }
    el("debate-disclaimer").textContent = trace && trace.disclaimer ? trace.disclaimer : "";
  }

  /* ── 渲染入口 ── */
  function render(D) {
    if (!state.expiry && D.expiries && D.expiries.length) state.expiry = D.expiries[0].expiry;
    renderTopbar(D);
    renderOverview(D);
    renderDecisionCard(D);
    renderChain(D);
    renderStrategy(D);
    drawPayoff(D);
    drawIvCrush(D);
    renderMacro(D);
    renderResearch(D);
    renderLibrary(D);
    renderDebate(D);
  }

  /* ── 视图切换 ── */
  function bindViews() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll(".view-tab"));
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
        var target = el("view-" + tab.getAttribute("data-view"));
        if (target) target.classList.add("active");
      });
    });
  }

  /* ── 到期页签（动态生成） ── */
  function bindTabs(D) {
    var wrap = el("expiry-tabs");
    wrap.innerHTML = "";
    D.expiries.forEach(function (expiry) {
      var tab = document.createElement("button");
      tab.type = "button";
      tab.className = "expiry-tab" + (expiry.primary ? " active" : "");
      tab.setAttribute("data-expiry", expiry.expiry);
      tab.appendChild(document.createTextNode(expiry.expiry.slice(5) + (expiry.primary ? " 主到期" : "")));
      tab.appendChild(make("span", "dte", " " + expiry.dte + " DTE"));
      tab.addEventListener("click", function () {
        wrap.querySelectorAll(".expiry-tab").forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        state.expiry = expiry.expiry;
        renderStrategy(D);
      });
      wrap.appendChild(tab);
    });
  }

  /* ── 对话抽屉 ── */
  function bindChatToggle() {
    var btn = el("chat-toggle");
    var drawer = el("chat-drawer");
    btn.addEventListener("click", function () {
      var open = drawer.classList.toggle("open");
      btn.textContent = open ? "对话 ▴" : "对话 ▾";
      btn.setAttribute("aria-expanded", String(open));
    });
  }

  function openChat() {
    var drawer = el("chat-drawer");
    if (!drawer.classList.contains("open")) {
      drawer.classList.add("open");
      el("chat-toggle").textContent = "对话 ▴";
      el("chat-toggle").setAttribute("aria-expanded", "true");
    }
  }

  function appendChat(message, data) {
    var log = el("chat-log");
    var userMsg = document.createElement("div");
    userMsg.className = "msg msg-user";
    userMsg.appendChild(make("p", null, message));
    log.appendChild(userMsg);

    var chat = data.chat || {};
    var agentMsg = document.createElement("div");
    agentMsg.className = "msg msg-agent";
    agentMsg.appendChild(make("p", null, "已按你的描述完成场景解析与五阶段管线："));
    var block = document.createElement("pre");
    block.className = "json-block";
    block.textContent = JSON.stringify(chat.scenario || {}, null, 2);
    agentMsg.appendChild(block);
    (chat.notes || []).forEach(function (text) { agentMsg.appendChild(make("p", null, "· " + text)); });
    agentMsg.appendChild(make("p", null, "结论：" + data.decisionCard.verdict + " — " + data.decisionCard.summary));
    log.appendChild(agentMsg);
    log.scrollTop = log.scrollHeight;
  }

  function animateStages() {
    var stages = Array.prototype.slice.call(document.querySelectorAll(".stage"));
    stages.forEach(function (s) { s.classList.remove("running", "done"); });
    stages.forEach(function (s, i) {
      setTimeout(function () { s.classList.add("running"); }, i * 220);
      setTimeout(function () { s.classList.remove("running"); s.classList.add("done"); }, i * 220 + 380);
    });
  }

  function bindRun(currentData) {
    var btn = el("run-btn");
    var input = el("scenario-input");
    function run() {
      var message = input.value.trim();
      openChat();
      animateStages();
      if (state.mode !== "api") {
        note("静态回退模式：启动 python -m src.ui_server 后可真实解析场景并运行管线。");
        return;
      }
      btn.disabled = true;
      btn.textContent = "运行中…";
      fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message || "腾讯业绩前方向不确定，单笔最多亏 5%" }),
      })
        .then(function (resp) {
          return resp.json().then(function (body) {
            if (!resp.ok) throw new Error((body && body.error) || "HTTP " + resp.status);
            return body;
          });
        })
        .then(function (data) {
          if (data && data.error) throw new Error(data.error);
          appendChat(message || "（默认场景）", data);
          state.expiry = data.expiries && data.expiries[0] ? data.expiries[0].expiry : state.expiry;
          bindTabs(data);
          render(data);
          note("已解析场景并用冻结快照重跑五阶段管线，决策卡已刷新。");
        })
        .catch(function (err) {
          var agentMsg = document.createElement("div");
          agentMsg.className = "msg msg-agent";
          agentMsg.appendChild(make("p", null, "解析失败：" + err.message));
          el("chat-log").appendChild(agentMsg);
          note("运行失败：" + err.message);
        })
        .finally(function () {
          btn.disabled = false;
          btn.textContent = "RUN ▸";
        });
    }
    btn.addEventListener("click", run);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); run(); }
    });
  }

  function boot() {
    if (!DEFAULT) { note("缺少 data.js 静态回退数据。"); return; }
    state.expiry = DEFAULT.expiries && DEFAULT.expiries[0] ? DEFAULT.expiries[0].expiry : null;
    bindViews();
    bindChatToggle();
    bindRun(DEFAULT);
    fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data || data.error || !data.expiries) throw new Error("bad state");
        state.mode = "api";
        bindTabs(data);
        render(data);
        note("已连接本地服务：数据来自冻结快照与自研引擎，非投资建议。");
      })
      .catch(function () {
        state.mode = "static";
        bindTabs(DEFAULT);
        render(DEFAULT);
        note("静态回退：data.js 快照。启动 python -m src.ui_server 以接入真实管线。");
      });
  }

  boot();
})();
