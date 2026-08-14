/* GOAI 期权智能终端 - 前端渲染与交互。
 * 数据来源：/api/state（本地只读服务）；服务不可用时回退到 data.js 静态快照。
 * 前端零计算：只做格式化与渲染，所有数字来自后端。动态文本一律 textContent，防注入。
 */
(function () {
  "use strict";

  var DEFAULT = window.GOAI_DATA || null;
  var state = { mode: "static", expiry: "2026-08-14" };

  function el(id) {
    return document.getElementById(id);
  }

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

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(id) {
    el(id).innerHTML = "";
  }

  function note(text) {
    el("footnote").textContent = text;
  }

  /* 时钟 */
  function tick() {
    var now = new Date();
    el("clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }
  tick();
  setInterval(tick, 1000);

  function renderTopbar(D) {
    var meta = D.meta;
    el("mode-chip").textContent =
      (state.mode === "api" ? "API · " : "STATIC · ") + meta.mode + " · " + meta.freshness;
    el("captured-at").textContent = meta.capturedAt + " · " + meta.source;
  }

  function renderDecisionCard(D) {
    var card = D.decisionCard;
    el("verdict").textContent = card.verdict;
    el("summary").textContent = card.summary;

    clear("evidence-list");
    (card.keyEvidence || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", null, item.claim));
      li.appendChild(
        make("span", "src", item.source + " · " + item.capturedAt)
      );
      el("evidence-list").appendChild(li);
    });

    clear("conditions-list");
    (card.conditionsThatChange || []).forEach(function (text) {
      el("conditions-list").appendChild(make("li", null, text));
    });

    clear("edge-checks");
    (card.edgeGate.checks || []).forEach(function (check) {
      var li = document.createElement("li");
      li.appendChild(
        make("span", "result " + String(check.result).toLowerCase(), check.result)
      );
      li.appendChild(make("strong", null, check.check + "："));
      li.appendChild(document.createTextNode(check.detail));
      el("edge-checks").appendChild(li);
    });

    clear("risk-checks");
    (card.riskGate.findings || []).forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(
        make("span", "result " + String(item.kind).toLowerCase(), item.kind)
      );
      li.appendChild(document.createTextNode(item.text));
      el("risk-checks").appendChild(li);
    });

    el("action-next").textContent =
      card.actionGate.action + " → " + card.actionGate.nextStep;

    clear("audit-list");
    var audit = card.auditTrail || [];
    if (!audit.length) {
      el("audit-list").appendChild(
        make("li", null, "审计哈希链见 research/audit/audit_log.jsonl（本地）")
      );
    }
    audit.forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(make("span", null, item.event));
      li.appendChild(make("span", "hash", item.hash));
      el("audit-list").appendChild(li);
    });
  }

  function renderEvents(D) {
    var earnings = D.earnings || {};
    el("earnings-date").textContent = earnings.date || "--";
    el("eps-yoy").textContent =
      earnings.estimateEpsYoy == null ? "--" : fmtSigned(earnings.estimateEpsYoy) + "%";
    el("rev-yoy").textContent =
      earnings.estimateRevenueYoy == null ? "--" : fmtSigned(earnings.estimateRevenueYoy) + "%";
    el("crush-history").textContent =
      earnings.lastReportIvCrush == null || earnings.historyReportIvCrush == null
        ? "--"
        : fmt(earnings.lastReportIvCrush, 2) + " / " + fmt(earnings.historyReportIvCrush, 2) + " pp";
  }

  function spreadPct(leg) {
    return ((leg.ask - leg.bid) / leg.mid) * 100;
  }

  function liquidityClass(pct) {
    if (pct <= 8) return "good";
    if (pct <= 15) return "warn";
    return "bad";
  }

  function renderChain(D) {
    var body = el("chain-body");
    body.innerHTML = "";
    D.expiries.forEach(function (expiry) {
      var callSpread = spreadPct(expiry.call);
      var putSpread = spreadPct(expiry.put);
      var worst = Math.max(callSpread, putSpread);
      var tr = document.createElement("tr");
      var cells = [
        expiry.expiry.slice(5) + (expiry.primary ? "（主）" : ""),
        fmt(expiry.call.bid, 2),
        fmt(expiry.call.ask, 2),
        fmt(expiry.call.apiIvPct, 1) + "%",
        fmt(expiry.call.openInterest),
        fmt(expiry.strike, 0),
        fmt(expiry.put.apiIvPct, 1) + "%",
        fmt(expiry.put.openInterest),
        fmt(expiry.put.bid, 2),
        fmt(expiry.put.ask, 2),
      ];
      cells.forEach(function (text, index) {
        var td = document.createElement("td");
        if (index === 5) td.className = "strike";
        td.textContent = text;
        tr.appendChild(td);
      });
      var spreadTd = document.createElement("td");
      spreadTd.appendChild(make("span", "liquidity " + liquidityClass(worst)));
      spreadTd.appendChild(document.createTextNode(" " + worst.toFixed(1) + "%"));
      tr.appendChild(spreadTd);
      body.appendChild(tr);
    });
  }

  function currentExpiry(D) {
    var match = D.expiries.filter(function (item) {
      return item.expiry === state.expiry;
    })[0];
    return match || D.expiries[0];
  }

  function renderStrategy(D) {
    var expiry = currentExpiry(D);
    var s = expiry.strategy;
    el("strategy-expiry-label").textContent =
      expiry.expiry.slice(5) + " · " + expiry.dte + " DTE";
    if (!s) {
      el("strategy-lots").textContent = "— 该到期未纳入策略计算";
      ["cost-ask", "cost-exec", "max-loss", "breakeven",
       "g-delta", "g-gamma", "g-vega", "g-theta", "g-rho"].forEach(function (id) {
        el(id).textContent = "—";
      });
      return;
    }
    el("strategy-lots").textContent =
      s.lots + " 张 @ " + fmt(expiry.strike, 0);
    el("cost-ask").textContent = fmt(s.costPerLotAsk);
    el("cost-exec").textContent = fmt(s.costPerLotExec);
    el("max-loss").textContent = fmt(s.maxLoss) + " HKD";
    el("breakeven").textContent =
      fmt(s.breakeven[0], 2) + " / " + fmt(s.breakeven[1], 2);
    el("g-delta").textContent = fmt(s.greeks.delta, 2);
    el("g-gamma").textContent = fmt(s.greeks.gamma, 2);
    el("g-vega").textContent = fmt(s.greeks.vega, 2);
    el("g-theta").textContent = fmt(s.greeks.theta, 2);
    el("g-rho").textContent = fmt(s.greeks.rho, 2);
  }

  /* 到期损益曲线（主到期数据） */
  function drawPayoff(D) {
    var svg = el("payoff-chart");
    var ns = "http://www.w3.org/2000/svg";
    svg.innerHTML = "";
    var primary = D.expiries[0];
    if (!primary || !primary.strategy.pnlAtExpiry) return;

    var defs = document.createElementNS(ns, "defs");
    var gradient = document.createElementNS(ns, "linearGradient");
    gradient.setAttribute("id", "area");
    gradient.setAttribute("x1", "0");
    gradient.setAttribute("y1", "0");
    gradient.setAttribute("x2", "0");
    gradient.setAttribute("y2", "1");
    var stopA = document.createElementNS(ns, "stop");
    stopA.setAttribute("offset", "0%");
    stopA.setAttribute("stop-color", "rgba(245,185,66,0.28)");
    var stopB = document.createElementNS(ns, "stop");
    stopB.setAttribute("offset", "100%");
    stopB.setAttribute("stop-color", "rgba(245,185,66,0)");
    gradient.appendChild(stopA);
    gradient.appendChild(stopB);
    defs.appendChild(gradient);
    svg.appendChild(defs);

    var w = 560;
    var h = 250;
    var padL = 56;
    var padR = 14;
    var padT = 14;
    var padB = 30;
    var xMin = 425;
    var xMax = 525;
    var yMin = -1600;
    var yMax = 4200;

    function x(v) {
      return padL + ((v - xMin) / (xMax - xMin)) * (w - padL - padR);
    }
    function y(v) {
      return h - padB - ((v - yMin) / (yMax - yMin)) * (h - padT - padB);
    }
    function node(name, attrs) {
      var n = document.createElementNS(ns, name);
      Object.keys(attrs).forEach(function (k) {
        n.setAttribute(k, attrs[k]);
      });
      return n;
    }

    svg.appendChild(node("line", {
      x1: x(xMin), y1: y(0), x2: x(xMax), y2: y(0),
      stroke: "rgba(148,163,184,0.35)", "stroke-dasharray": "4 4",
    }));

    [-1000, 0, 1000, 2000, 3000, 4000].forEach(function (v) {
      svg.appendChild(node("line", {
        x1: x(xMin), y1: y(v), x2: x(xMax), y2: y(v),
        stroke: "rgba(148,163,184,0.08)",
      }));
      var label = node("text", { x: x(xMin) - 8, y: y(v) + 4, "text-anchor": "end" });
      label.setAttribute("fill", "#5c6b7a");
      label.setAttribute("font-size", "10");
      label.textContent = v >= 0 ? "+" + fmt(v) : fmt(v);
      svg.appendChild(label);
    });

    var points = [];
    primary.strategy.pnlAtExpiry.forEach(function (group) {
      group.rows.forEach(function (row) {
        points.push({ spot: row.spot, pnl: row.pnl });
      });
    });
    points.sort(function (a, b) {
      return a.spot - b.spot;
    });

    var dPath = points
      .map(function (p, i) {
        return (i === 0 ? "M" : "L") + x(p.spot).toFixed(1) + " " + y(p.pnl).toFixed(1);
      })
      .join(" ");
    var areaPath =
      dPath +
      " L " + x(points[points.length - 1].spot).toFixed(1) + " " + y(0).toFixed(1) +
      " L " + x(points[0].spot).toFixed(1) + " " + y(0).toFixed(1) + " Z";

    svg.appendChild(node("path", { d: areaPath, fill: "url(#area)" }));
    svg.appendChild(node("path", {
      d: dPath, fill: "none", stroke: "#f5b942",
      "stroke-width": 2, "stroke-linejoin": "round",
    }));

    points.forEach(function (p) {
      svg.appendChild(node("circle", {
        cx: x(p.spot), cy: y(p.pnl), r: 3.5,
        fill: p.pnl >= 0 ? "#16c784" : "#f6465d",
      }));
    });

    primary.strategy.breakeven.forEach(function (b) {
      svg.appendChild(node("line", {
        x1: x(b), y1: y(yMin), x2: x(b), y2: y(0),
        stroke: "rgba(245,185,66,0.5)", "stroke-dasharray": "3 4",
      }));
    });

    svg.appendChild(node("line", {
      x1: x(D.underlying.spot), y1: y(yMin), x2: x(D.underlying.spot), y2: y(yMax),
      stroke: "rgba(88,166,255,0.5)", "stroke-dasharray": "2 5",
    }));
    var spotLabel = node("text", {
      x: x(D.underlying.spot) + 4, y: padT + 8, fill: "#58a6ff", "font-size": "10",
    });
    spotLabel.textContent = "现价 " + fmt(D.underlying.spot, 2);
    svg.appendChild(spotLabel);

    [440, 460, 480, 500, 520].forEach(function (v) {
      var label = node("text", {
        x: x(v), y: h - 8, "text-anchor": "middle", fill: "#5c6b7a", "font-size": "10",
      });
      label.textContent = v;
      svg.appendChild(label);
    });
  }

  /* IV crush 条形（主到期数据） */
  function drawIvCrush(D) {
    var wrap = el("iv-crush");
    wrap.innerHTML = "";
    var primary = D.expiries[0];
    if (!primary || !primary.strategy.ivCrush) return;
    var maxAbs = 0;
    primary.strategy.ivCrush.forEach(function (g) {
      g.rows.forEach(function (r) {
        maxAbs = Math.max(maxAbs, Math.abs(r.pnl));
      });
    });
    primary.strategy.ivCrush.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "crush-row";
      row.appendChild(make("span", null, "IV " + g.iv_crush));
      var bars = document.createElement("div");
      bars.style.display = "grid";
      bars.style.gridTemplateColumns = "1fr 1fr";
      bars.style.gap = "6px";
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
      row.appendChild(
        make(
          "span",
          "crush-val",
          g.rows
            .map(function (r) {
              return (r.direction === "up" ? "↑ " : "↓ ") + fmtSigned(r.pnl);
            })
            .join("  ")
        )
      );
      wrap.appendChild(row);
    });
  }

  function renderMacro(D) {
    var macro = D.macro || {};
    var library = D.policyLibrary || {};
    if (!macro.available) {
      el("macro-confidence").textContent = "未启用";
      el("sentiment-index").textContent = "--";
      el("sentiment-verdict").textContent = "--";
      el("iv-state").textContent = "--";
      el("skew-verdict").textContent = "--";
      el("principal-pair").textContent = "--";
      el("macro-mood").textContent = "未提供宏观研判输入（启动本地服务或传入 --macro-policy）。";
      clear("scenario-list");
      clear("library-health");
      clear("promoted-list");
      el("macro-note").textContent = "";
      return;
    }

    var judgment = macro.macro_judgment || {};
    el("macro-confidence").textContent =
      "置信度 " + (judgment.confidence || "--");
    var sentiment = macro.sentiment || {};
    el("sentiment-index").textContent = fmt(sentiment.index, 1);
    el("sentiment-verdict").textContent = sentiment.verdict || "--";
    var iv = macro.iv_emotion || {};
    el("iv-state").textContent = iv.state || "--";
    el("skew-verdict").textContent = "Skew " + (iv.skew_verdict || "--");
    var policy = macro.policy_analysis || {};
    var principal = policy.principal_contradiction;
    el("principal-pair").textContent = principal ? principal.pair : "--";
    el("macro-mood").textContent =
      "情绪基调：" + (judgment.mood || "--") +
      (judgment.contrarian_note ? "。" + judgment.contrarian_note : "");

    clear("scenario-list");
    (judgment.scenarios || []).forEach(function (scenario) {
      var li = document.createElement("li");
      li.appendChild(make("span", "scenario-name", scenario.name));
      li.appendChild(
        make(
          "span",
          "scenario-level",
          "定性可能性 " + (scenario.likelihood_level || "--")
        )
      );
      li.appendChild(
        make(
          "span",
          "scenario-implication",
          (scenario.market_implication || "") +
            "；期权：" +
            (scenario.option_implication || "")
        )
      );
      el("scenario-list").appendChild(li);
    });

    clear("library-health");
    var health = (library.health || {}).verification || {};
    ["VERIFIED", "PENDING", "FAILED", "UNKNOWN"].forEach(function (kind) {
      if (health[kind] === undefined) return;
      el("library-health").appendChild(
        make("span", "health-pill " + kind.toLowerCase(), kind + " " + health[kind])
      );
    });
    el("library-health").appendChild(
      make("span", "health-pill", "事件 " + (library.eventCount || 0))
    );

    clear("promoted-list");
    var promoted = (library.health || {}).recently_promoted || [];
    if (!promoted.length) {
      el("promoted-list").appendChild(
        make("li", null, "暂无自动激活事件")
      );
    }
    promoted.forEach(function (item) {
      el("promoted-list").appendChild(
        make(
          "li",
          null,
          "最近激活 " + item.id + " · " + item.promoted_at +
            (item.promoted_by ? " · " + item.promoted_by : "")
        )
      );
    });

    el("macro-note").textContent = macro.disclaimer || "";
  }

  /* ---------- 十角色辩论运行时（第五面板） ---------- */
  var STANCE_LABEL = { favor: "赞成", oppose: "反对", neutral: "中性" };
  var CONFIDENCE_LABEL = { high: "高置信", medium: "中置信", low: "低置信" };
  var ROLE_STATUS_LABEL = {
    ok: "完成",
    error: "失败",
    timeout: "超时",
    parse_error: "输出非法",
    skipped: "跳过",
  };

  function debateStatusText(trace) {
    if (!trace) return "未运行（仅确定性管线）";
    if (trace.status === "offline") return "离线回退 · 未配置 API Key";
    if (trace.status === "failed") return "辩论失败 · 已回退确定性管线";
    if (trace.status === "degraded") return "部分角色降级 · 确定性结论不变";
    if (trace.status === "complete") return "两轮辩论完成";
    return String(trace.status || "未知");
  }

  function debateStatusClass(trace) {
    if (!trace) return "st-off";
    if (trace.status === "complete") return "st-complete";
    if (trace.status === "degraded") return "st-degraded";
    if (trace.status === "failed") return "st-fail";
    return "st-off";
  }

  function appendRefs(container, refs, dropped) {
    (refs || []).forEach(function (ref) {
      container.appendChild(make("span", "ref-pill", ref));
    });
    (dropped || []).forEach(function (ref) {
      container.appendChild(make("span", "ref-pill ref-pill-dropped", "✕ " + ref));
    });
  }

  function renderDebateEntry(roundWrap, entry) {
    var card = document.createElement("div");
    card.className = "debate-entry";
    var head = document.createElement("div");
    head.className = "debate-entry-head";
    head.appendChild(make("span", "debate-role", entry.name + " · " + entry.title));
    head.appendChild(
      make(
        "span",
        "debate-status st-" + String(entry.status || "error"),
        ROLE_STATUS_LABEL[entry.status] || String(entry.status || "未知")
      )
    );
    if (entry.stance) {
      head.appendChild(
        make(
          "span",
          "stance-pill stance-" + entry.stance,
          STANCE_LABEL[entry.stance] || entry.stance
        )
      );
    }
    if (entry.confidence) {
      head.appendChild(
        make("span", "debate-conf", CONFIDENCE_LABEL[entry.confidence] || entry.confidence)
      );
    }
    card.appendChild(head);

    if (entry.conclusion) {
      card.appendChild(make("p", "debate-conclusion", entry.conclusion));
    }
    if (entry.counterpoint) {
      card.appendChild(make("p", "debate-counterpoint", "回辩：" + entry.counterpoint));
    }
    if (entry.error) {
      card.appendChild(make("p", "debate-error", "错误：" + entry.error));
    }

    var refs = document.createElement("div");
    refs.className = "debate-refs";
    appendRefs(refs, entry.evidence_refs, entry.dropped_refs);
    if (refs.childNodes.length) card.appendChild(refs);

    if (entry.duration_ms || entry.tokens || entry.model) {
      var metaLine = document.createElement("p");
      metaLine.className = "debate-entry-meta";
      metaLine.textContent =
        (entry.model ? entry.model + " · " : "") +
        (entry.duration_ms ? (entry.duration_ms / 1000).toFixed(1) + "s" : "") +
        (entry.tokens ? " · " + entry.tokens + " tokens" : "");
      card.appendChild(metaLine);
    }
    roundWrap.appendChild(card);
  }

  function renderDebateRounds(trace) {
    var wrap = el("debate-rounds");
    wrap.innerHTML = "";
    var rounds = (trace && trace.rounds) || [];
    if (!rounds.length) {
      wrap.appendChild(make("p", "note", "尚未运行十角色辩论。"));
      return;
    }
    rounds.forEach(function (round) {
      var section = document.createElement("div");
      section.className = "debate-round-block";
      section.appendChild(
        make("h4", "debate-round-head", "第 " + round.round + " 轮")
      );
      (round.entries || []).forEach(function (entry) {
        renderDebateEntry(section, entry);
      });
      wrap.appendChild(section);
    });
  }

  function renderDebateDisputes(trace) {
    var wrap = el("debate-disputes");
    wrap.innerHTML = "";
    var disputes = (trace && trace.disputes) || [];
    if (!disputes.length) {
      wrap.appendChild(make("p", "note", "无分歧点。"));
      return;
    }
    disputes.forEach(function (dispute, index) {
      var card = document.createElement("div");
      card.className = "debate-dispute";
      card.appendChild(make("h4", null, "分歧 " + (index + 1) + " · " + dispute.topic));
      card.appendChild(make("p", null, dispute.question));
      if (dispute.roles && dispute.roles.length) {
        card.appendChild(
          make("span", "dispute-roles", "相关角色：" + dispute.roles.join(" / "))
        );
      }
      wrap.appendChild(card);
    });
  }

  function renderDebateConsensus(consensus) {
    var wrap = el("debate-consensus");
    wrap.innerHTML = "";
    if (!consensus) {
      wrap.appendChild(make("p", "note", "暂无研究共识。"));
      return;
    }
    var head = document.createElement("div");
    head.className = "consensus-head";
    head.appendChild(
      make(
        "span",
        "consensus-source",
        consensus.source === "llm" ? "LLM 汇总" : "确定性回退"
      )
    );
    if (consensus.stance) {
      head.appendChild(
        make(
          "span",
          "stance-pill stance-" + consensus.stance,
          "共识：" + (STANCE_LABEL[consensus.stance] || consensus.stance)
        )
      );
    }
    if (consensus.confidence) {
      head.appendChild(
        make("span", "debate-conf", CONFIDENCE_LABEL[consensus.confidence] || consensus.confidence)
      );
    }
    wrap.appendChild(head);
    wrap.appendChild(make("p", "consensus-summary", consensus.summary || ""));

    var refs = document.createElement("div");
    refs.className = "debate-refs";
    appendRefs(refs, consensus.evidence_refs, []);
    if (refs.childNodes.length) wrap.appendChild(refs);

    var questions = consensus.open_questions || [];
    if (questions.length) {
      var list = document.createElement("ul");
      list.className = "open-questions";
      questions.forEach(function (question) {
        list.appendChild(make("li", null, question));
      });
      wrap.appendChild(list);
    }
  }

  function renderDebate(D) {
    var badge = D.llm || {};
    var chip = el("llm-chip");
    if (chip) {
      if (badge.available) {
        chip.textContent =
          "LLM · " + (badge.provider || "--") + " · " + (badge.model || "--");
        chip.classList.add("llm-on");
        chip.classList.remove("llm-off");
      } else {
        chip.textContent = "LLM · offline 确定性回退";
        chip.classList.add("llm-off");
        chip.classList.remove("llm-on");
      }
    }

    var trace = D.debateTrace || null;
    var consensus =
      D.researchConsensus || (trace ? trace.research_consensus : null) || null;

    var dot = el("debate-dot");
    if (dot) {
      dot.className = "debate-status-dot " + debateStatusClass(trace);
    }
    el("debate-summary").textContent = debateStatusText(trace);

    var meta = el("debate-meta");
    meta.innerHTML = "";
    if (trace) {
      var metrics = trace.metrics || {};
      var cells = [
        ["状态", debateStatusText(trace)],
        ["引擎 verdict", trace.verdict || "--"],
        ["提供商", trace.provider || "无（离线回退）"],
        [
          "耗时",
          metrics.elapsed_ms != null ? (metrics.elapsed_ms / 1000).toFixed(1) + " s" : "--",
        ],
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
      meta.appendChild(
        make(
          "p",
          "note",
          "十角色辩论需配置 DeepSeek（OpenAI 兼容）API Key；无 key 时终端自动回退确定性管线。"
        )
      );
    }

    renderDebateRounds(trace);
    renderDebateDisputes(trace);
    renderDebateConsensus(consensus);
    el("debate-disclaimer").textContent =
      trace && trace.disclaimer ? trace.disclaimer : "";
  }

  function bindDebateToggle() {
    var toggle = el("debate-toggle");
    var body = el("debate-body");
    var caret = el("debate-caret");
    if (!toggle || !body) return;
    toggle.addEventListener("click", function () {
      var expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      body.hidden = expanded;
      if (caret) caret.textContent = expanded ? "▸" : "▾";
    });
  }

  function render(D) {
    renderTopbar(D);
    renderDecisionCard(D);
    renderEvents(D);
    renderChain(D);
    renderStrategy(D);
    drawPayoff(D);
    drawIvCrush(D);
    renderMacro(D);
    renderDebate(D);
  }

  /* 到期页签（委托一次） */
  function bindTabs(D) {
    var tabs = document.querySelectorAll(".expiry-tab");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) {
          t.classList.remove("active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        state.expiry = tab.getAttribute("data-expiry");
        renderStrategy(D);
      });
    });
  }

  /* 重跑阶段动画 */
  function animateStages() {
    var stages = Array.prototype.slice.call(document.querySelectorAll(".stage"));
    stages.forEach(function (s) {
      s.classList.remove("running", "done");
    });
    stages.forEach(function (s, i) {
      setTimeout(function () {
        s.classList.add("running");
      }, i * 260);
      setTimeout(function () {
        s.classList.remove("running");
        s.classList.add("done");
      }, i * 260 + 420);
    });
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
    (chat.notes || []).forEach(function (text) {
      agentMsg.appendChild(make("p", null, "· " + text));
    });
    agentMsg.appendChild(
      make(
        "p",
        null,
        "结论：" + data.decisionCard.verdict + " — " + data.decisionCard.summary
      )
    );
    log.appendChild(agentMsg);
    log.scrollTop = log.scrollHeight;
  }

  function bindRun(currentData) {
    var btn = el("run-btn");
    btn.textContent = "运行管线";
    btn.addEventListener("click", function () {
      animateStages();
      var message = el("scenario-input").value.trim();
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
            if (!resp.ok) {
              throw new Error((body && body.error) || "HTTP " + resp.status);
            }
            return body;
          });
        })
        .then(function (data) {
          if (data && data.error) throw new Error(data.error);
          appendChat(message || "（默认场景）", data);
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
          btn.textContent = "运行管线";
        });
    });
  }

  function boot() {
    if (!DEFAULT) {
      note("缺少 data.js 静态回退数据。");
      return;
    }
    bindTabs(DEFAULT);
    bindRun(DEFAULT);
    bindDebateToggle();
    fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data || data.error || !data.expiries) throw new Error("bad state");
        state.mode = "api";
        render(data);
        note("已连接本地服务：数据来自冻结快照与自研引擎，非投资建议。");
      })
      .catch(function () {
        state.mode = "static";
        render(DEFAULT);
        note("静态回退：data.js 快照。启动 python -m src.ui_server 以接入真实管线。");
      });
  }

  boot();
})();
