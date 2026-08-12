/* GOAI 期权智能终端 - 参考 UI 交互与渲染（静态演示，不产生真实下单） */
(function () {
  "use strict";

  var D = window.GOAI_DATA;
  var state = { expiry: "2026-08-14" };

  function fmt(n, digits) {
    return Number(n).toLocaleString("zh-CN", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0,
    });
  }

  function fmtSigned(n) {
    var sign = n > 0 ? "+" : "";
    return sign + fmt(n);
  }

  function el(id) {
    return document.getElementById(id);
  }

  /* 时钟 */
  function tick() {
    var now = new Date();
    var text = now.toLocaleTimeString("zh-CN", { hour12: false });
    el("clock").textContent = text;
  }
  tick();
  setInterval(tick, 1000);

  /* 顶栏快照信息 */
  el("captured-at").textContent = D.meta.capturedAt + " · " + D.meta.source;

  /* 决策卡 */
  el("verdict").textContent = D.decisionCard.verdict;
  el("summary").textContent = D.decisionCard.summary;

  D.decisionCard.keyEvidence.forEach(function (item) {
    var li = document.createElement("li");
    li.innerHTML =
      '<span>' + item.claim + '</span>' +
      '<span class="src">' + item.source + " · " + item.capturedAt + "</span>";
    el("evidence-list").appendChild(li);
  });

  D.decisionCard.conditionsThatChange.forEach(function (text) {
    var li = document.createElement("li");
    li.textContent = text;
    el("conditions-list").appendChild(li);
  });

  /* 门控 */
  D.decisionCard.edgeGate.checks.forEach(function (check) {
    var li = document.createElement("li");
    li.innerHTML =
      '<span class="result ' + check.result.toLowerCase() + '">' + check.result + "</span>" +
      "<strong>" + check.check + "</strong>：" + check.detail;
    el("edge-checks").appendChild(li);
  });

  D.decisionCard.riskGate.findings.forEach(function (item) {
    var li = document.createElement("li");
    var kind = item.kind.toLowerCase();
    li.innerHTML =
      '<span class="result ' + kind + '">' + item.kind + "</span>" + item.text;
    el("risk-checks").appendChild(li);
  });

  el("action-next").textContent =
    D.decisionCard.actionGate.action + " → " + D.decisionCard.actionGate.nextStep;

  /* 审计链 */
  D.decisionCard.auditTrail.forEach(function (item) {
    var li = document.createElement("li");
    var span = document.createElement("span");
    span.textContent = item.event;
    var hash = document.createElement("span");
    hash.className = "hash";
    hash.textContent = item.hash;
    li.appendChild(span);
    li.appendChild(hash);
    el("audit-list").appendChild(li);
  });

  /* 期权链 */
  function spreadPct(leg) {
    return ((leg.ask - leg.bid) / leg.mid) * 100;
  }

  function liquidityClass(pct) {
    if (pct <= 8) return "good";
    if (pct <= 15) return "warn";
    return "bad";
  }

  function renderChain() {
    var body = el("chain-body");
    body.innerHTML = "";
    D.expiries.forEach(function (expiry) {
      var tr = document.createElement("tr");
      var callSpread = spreadPct(expiry.call);
      var putSpread = spreadPct(expiry.put);
      var worst = Math.max(callSpread, putSpread);
      tr.innerHTML =
        "<td>" + expiry.expiry.slice(5) + (expiry.primary ? "（主）" : "") + "</td>" +
        "<td>" + expiry.call.bid.toFixed(2) + "</td>" +
        "<td>" + expiry.call.ask.toFixed(2) + "</td>" +
        "<td>" + expiry.call.apiIvPct.toFixed(1) + "%</td>" +
        "<td>" + fmt(expiry.call.openInterest) + "</td>" +
        '<td class="strike">' + expiry.strike.toFixed(0) + "</td>" +
        "<td>" + expiry.put.apiIvPct.toFixed(1) + "%</td>" +
        "<td>" + fmt(expiry.put.openInterest) + "</td>" +
        "<td>" + expiry.put.bid.toFixed(2) + "</td>" +
        "<td>" + expiry.put.ask.toFixed(2) + "</td>" +
        '<td><span class="liquidity ' + liquidityClass(worst) + '" title="价差 ' +
        Math.max(callSpread, putSpread).toFixed(1) + '%"></span> ' +
        worst.toFixed(1) + "%</td>";
      body.appendChild(tr);
    });
  }

  /* 策略卡 */
  function renderStrategy() {
    var expiry = D.expiries.filter(function (item) {
      return item.expiry === state.expiry;
    })[0];
    var s = expiry.strategy;
    el("strategy-expiry-label").textContent =
      expiry.expiry.slice(5) + " · " + expiry.dte + " DTE";
    el("strategy-lots").textContent =
      s.lots + " 张 @ " + expiry.strike.toFixed(0);
    el("cost-ask").textContent = fmt(s.costPerLotAsk);
    el("cost-exec").textContent = fmt(s.costPerLotExec);
    el("max-loss").textContent = fmt(s.maxLoss) + " HKD";
    el("breakeven").textContent =
      s.breakeven[0].toFixed(2) + " / " + s.breakeven[1].toFixed(2);
    el("g-delta").textContent = s.greeks.delta.toFixed(2);
    el("g-gamma").textContent = s.greeks.gamma.toFixed(2);
    el("g-vega").textContent = s.greeks.vega.toFixed(2);
    el("g-theta").textContent = s.greeks.theta.toFixed(2);
    el("g-rho").textContent = s.greeks.rho.toFixed(2);
  }

  /* 损益曲线（主到期数据） */
  function drawPayoff() {
    var svg = el("payoff-chart");
    var ns = "http://www.w3.org/2000/svg";
    svg.innerHTML = "";
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

    var primary = D.expiries[0];
    if (!primary.strategy.pnlAtExpiry) return;

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

    /* 零轴 */
    svg.appendChild(node("line", {
      x1: x(xMin), y1: y(0), x2: x(xMax), y2: y(0),
      stroke: "rgba(148,163,184,0.35)", "stroke-dasharray": "4 4",
    }));

    /* 网格 */
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

    /* 数据点（按 spot 排序，连成一条到期损益曲线） */
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

    svg.appendChild(node("path", {
      d: areaPath, fill: "url(#area)",
    }));
    svg.appendChild(node("path", {
      d: dPath, fill: "none", stroke: "#f5b942",
      "stroke-width": 2, "stroke-linejoin": "round",
    }));

    /* 点 */
    points.forEach(function (p) {
      var dot = node("circle", {
        cx: x(p.spot), cy: y(p.pnl), r: 3.5,
        fill: p.pnl >= 0 ? "#16c784" : "#f6465d",
      });
      svg.appendChild(dot);
    });

    /* 盈亏平衡与现价 */
    primary.strategy.breakeven.forEach(function (b) {
      svg.appendChild(node("line", {
        x1: x(b), y1: y(yMin), x2: x(b), y2: y(0),
        stroke: "rgba(245,185,66,0.5)", "stroke-dasharray": "3 4",
      }));
    });

    var spotLine = node("line", {
      x1: x(D.underlying.spot), y1: y(yMin), x2: x(D.underlying.spot), y2: y(yMax),
      stroke: "rgba(88,166,255,0.5)", "stroke-dasharray": "2 5",
    });
    svg.appendChild(spotLine);
    var spotLabel = node("text", {
      x: x(D.underlying.spot) + 4, y: padT + 8, fill: "#58a6ff", "font-size": "10",
    });
    spotLabel.textContent = "现价 478.80";
    svg.appendChild(spotLabel);

    /* X 轴标签 */
    [440, 460, 480, 500, 520].forEach(function (v) {
      var label = node("text", {
        x: x(v), y: h - 8, "text-anchor": "middle", fill: "#5c6b7a", "font-size": "10",
      });
      label.textContent = v;
      svg.appendChild(label);
    });
  }

  /* IV crush 条形（主到期数据） */
  function drawIvCrush() {
    var wrap = el("iv-crush");
    wrap.innerHTML = "";
    var primary = D.expiries[0];
    if (!primary.strategy.ivCrush) return;
    var maxAbs = 0;
    primary.strategy.ivCrush.forEach(function (g) {
      g.rows.forEach(function (r) {
        maxAbs = Math.max(maxAbs, Math.abs(r.pnl));
      });
    });
    primary.strategy.ivCrush.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "crush-row";
      var label = document.createElement("span");
      label.textContent = "IV " + g.label;
      row.appendChild(label);
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
      var vals = document.createElement("span");
      vals.className = "crush-val";
      vals.textContent = g.rows
        .map(function (r) {
          return (r.direction === "up" ? "↑ " : "↓ ") + fmtSigned(r.pnl);
        })
        .join("  ");
      row.appendChild(vals);
      wrap.appendChild(row);
    });
  }

  /* 到期页签 */
  function bindTabs() {
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
        renderStrategy();
      });
    });
  }

  /* 重跑演示：阶段动画 */
  function bindRun() {
    var stages = Array.prototype.slice.call(document.querySelectorAll(".stage"));
    el("run-btn").addEventListener("click", function () {
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
    });
  }

  renderChain();
  renderStrategy();
  drawPayoff();
  drawIvCrush();
  bindTabs();
  bindRun();
})();
