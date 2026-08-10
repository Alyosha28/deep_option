"""腾讯 0700 业绩跨式回测（决策支持，模拟盘基准）。

口径 A（引擎）：get_financials_earnings_price_move 的 11 个历史财报期，
  业绩前 1 日 S_pre + 当日期权 IV，用自研引擎定价 ATM 跨式（T=6 天、K=5 元取整），
  加 5% ask 滑点，业绩后第 1/2/5 个交易日按内在价值平仓（周选到期代理）。
口径 B（市场预期代理）：get_financials_earnings_price_history 的 19 个财报期，
  成本 = 市场预期波动 predict_vola_val_newest × 1.05，平仓 = |S_post - S_pre|。

无未来函数：入场只用业绩前已知的 IV / 市场预期波动；平仓只取业绩后收盘。
输出：data/backtest_tencent_straddle.json + research/audit/audit_log.jsonl
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "backtest_tencent_straddle.json"
MOVE_FILE = ROOT / "data" / "snapshots" / "2026-08-08_backtest_earnings_move.json"
HIST_FILE = ROOT / "data" / "snapshots" / "2026-08-08_backtest_earnings_history.json"
AUDIT_LOG_SCRIPT = ROOT / ".agents" / "skills" / "futu-options-agent" / "scripts" / "audit_log.py"

from src.pricing_engine import price  # noqa: E402

R = 0.035
Q = 0.0
T_DAYS = 6.0 / 365.0
SLIPPAGE = 0.05
HORIZONS = (1, 2, 5)


def load_json_lines(path: Path) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("{"):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    continue
    raise RuntimeError(f"no JSON object in {path}")


def straddle_mid(S: float, K: float, T: float, iv_pct: float) -> float:
    sigma = iv_pct / 100.0
    return price(S, K, T, R, Q, sigma, "CALL", american=True) + price(
        S, K, T, R, Q, sigma, "PUT", american=True
    )


def summarize(rois: list, labels: list) -> dict:
    n = len(rois)
    if n == 0:
        return {"n": 0}
    mean = statistics.fmean(rois)
    std = statistics.stdev(rois) if n > 1 else 0.0
    se = std / math.sqrt(n)
    wins = sum(1 for x in rois if x > 0)
    return {
        "n": n,
        "mean_roi_pct": mean * 100,
        "median_roi_pct": statistics.median(rois) * 100,
        "std_roi_pct": std * 100,
        "se_roi_pct": se * 100,
        "t_stat": (mean / se) if se > 0 else None,
        "win_rate_pct": wins / n * 100,
        "best_pct": max(rois) * 100,
        "worst_pct": min(rois) * 100,
        "best_label": labels[rois.index(max(rois))],
        "worst_label": labels[rois.index(min(rois))],
    }


def run_engine_backtest() -> dict:
    obj = load_json_lines(MOVE_FILE)
    by_period: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in obj["data"]["items"]:
        by_period[r["period_text"]][r["day_offset"]] = r

    rows = []
    for period in sorted(by_period):
        if period == "2026/Q2":
            continue
        d = by_period[period]
        pre = d.get(-1)
        if pre is None or pre.get("option_iv") is None:
            continue
        S_pre = float(pre["close_price"])
        iv = float(pre["option_iv"])
        K = 5.0 * round(S_pre / 5.0)
        cost_mid = straddle_mid(S_pre, K, T_DAYS, iv)
        cost_ask = cost_mid * (1 + SLIPPAGE)
        cost_short = cost_mid * (1 - SLIPPAGE)
        event = {
            "period": period,
            "pub_date": pre["pub_trading_day_str"],
            "s_pre": S_pre,
            "atm_strike": K,
            "entry_iv_pct": iv,
            "cost_mid": cost_mid,
            "cost_ask": cost_ask,
            "cost_short": cost_short,
            "horizons": {},
        }
        for h in HORIZONS:
            post = d.get(h)
            if post is None:
                continue
            S_post = float(post["close_price"])
            payoff = abs(S_post - K)
            event["horizons"][str(h)] = {
                "s_post": S_post,
                "payoff": payoff,
                "pnl": payoff - cost_ask,
                "roi": (payoff - cost_ask) / cost_ask,
                "pnl_short": cost_short - payoff,
                "roi_short": (cost_short - payoff) / cost_short,
            }
        rows.append(event)

    stats = {}
    for h in HORIZONS:
        rois, labels = [], []
        for ev in rows:
            if str(h) in ev["horizons"]:
                rois.append(ev["horizons"][str(h)]["roi"])
                labels.append(ev["period"])
        stats[f"d{h}"] = summarize(rois, labels)
        rois_s, labels_s = [], []
        for ev in rows:
            if str(h) in ev["horizons"]:
                rois_s.append(ev["horizons"][str(h)]["roi_short"])
                labels_s.append(ev["period"])
        stats[f"d{h}_short"] = summarize(rois_s, labels_s)

    return {
        "method": "engine + 历史期权 IV（T=6 天，K=5 元取整，滑点 5%）",
        "periods": rows,
        "stats": stats,
    }


def run_proxy_backtest() -> dict:
    obj = load_json_lines(HIST_FILE)
    by_period: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in obj["data"]:
        delta = r.get("schedule_delta")
        if delta is not None:
            by_period[r["period_text"]][int(delta)] = r

    rows = []
    for period in sorted(by_period):
        d = by_period[period]
        pre = d.get(-1)
        if pre is None or pre.get("predict_vola_val_newest") is None:
            continue
        if period == "2026/Q2":
            continue
        S_pre = float(pre["schedule_close_price"])
        cost = float(pre["predict_vola_val_newest"]) * (1 + SLIPPAGE)
        cost_short = float(pre["predict_vola_val_newest"]) * (1 - SLIPPAGE)
        event = {
            "period": period,
            "pub_date": pre["pub_trading_day_str"],
            "s_pre": S_pre,
            "implied_move_pct": pre["predict_vola_ratio_newest"],
            "cost_ask": cost,
            "cost_short": cost_short,
            "horizons": {},
        }
        for h in HORIZONS:
            post = d.get(h)
            if post is None or post.get("schedule_close_price") is None:
                continue
            S_post = float(post["schedule_close_price"])
            payoff = abs(S_post - S_pre)
            event["horizons"][str(h)] = {
                "s_post": S_post,
                "payoff": payoff,
                "pnl": payoff - cost,
                "roi": (payoff - cost) / cost,
                "pnl_short": cost_short - payoff,
                "roi_short": (cost_short - payoff) / cost_short,
            }
        rows.append(event)

    stats = {}
    for h in HORIZONS:
        rois, labels = [], []
        for ev in rows:
            if str(h) in ev["horizons"]:
                rois.append(ev["horizons"][str(h)]["roi"])
                labels.append(ev["period"])
        stats[f"d{h}"] = summarize(rois, labels)
        rois_s, labels_s = [], []
        for ev in rows:
            if str(h) in ev["horizons"]:
                rois_s.append(ev["horizons"][str(h)]["roi_short"])
                labels_s.append(ev["period"])
        stats[f"d{h}_short"] = summarize(rois_s, labels_s)

    return {
        "method": "市场预期波动代理（predict_vola_val × 1.05 滑点，K≈S_pre）",
        "periods": rows,
        "stats": stats,
    }


def year_split(rows: list, horizon: int = 2, side: str = "long") -> dict:
    groups: dict[str, list] = defaultdict(list)
    key = "roi" if side == "long" else "roi_short"
    for ev in rows:
        if str(horizon) in ev["horizons"]:
            year = ev["pub_date"][:4]
            groups[year].append(ev["horizons"][str(horizon)][key])
    out = {}
    for year in sorted(groups):
        rois = groups[year]
        out[year] = {
            "n": len(rois),
            "mean_roi_pct": statistics.fmean(rois) * 100,
            "win_rate_pct": sum(1 for x in rois if x > 0) / len(rois) * 100,
        }
    return out


def audit(payload: dict) -> None:
    cmd = [
        sys.executable,
        str(AUDIT_LOG_SCRIPT),
        "--event",
        "backtest",
    ]
    subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False), text=True, check=True)


def main() -> None:
    engine_bt = run_engine_backtest()
    proxy_bt = run_proxy_backtest()
    result = {
        "underlying": "HK.00700",
        "strategy": "业绩前 1 日买入 ATM 跨式，持有至业绩后第 d 个交易日按内在价值平仓",
        "lookahead": "无未来函数：入场只用业绩前已知 IV / 市场预期波动",
        "engine_backtest": engine_bt,
        "proxy_backtest": proxy_bt,
        "year_split_engine_d2": year_split(engine_bt["periods"], 2),
        "year_split_proxy_d2": year_split(proxy_bt["periods"], 2),
        "year_split_engine_d2_short": year_split(engine_bt["periods"], 2, "short"),
        "year_split_proxy_d2_short": year_split(proxy_bt["periods"], 2, "short"),
        "caveats": [
            "历史期权链 bid/ask 不可得，引擎成本按 IV 定价 + 5% 滑点近似",
            "平仓按到期内在价值，忽略业绩后到期的剩余时间价值（偏保守/偏乐观视行情而定）",
            "样本为单标的 11~19 个财报期，t 统计量仅供量级参考",
            "当前 2026/Q2 为在途模拟订单，属于样本外前瞻验证",
        ],
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    audit({
        "underlying": "HK.00700",
        "engine_d2": engine_bt["stats"].get("d2", {}),
        "proxy_d2": proxy_bt["stats"].get("d2", {}),
        "engine_d2_short": engine_bt["stats"].get("d2_short", {}),
        "proxy_d2_short": proxy_bt["stats"].get("d2_short", {}),
    })

    print("=" * 82)
    print("腾讯 0700 业绩跨式回测（决策支持 | 模拟盘基准）")
    print(f"策略：业绩前 1 日买入 ATM 跨式，持有至业绩后第 d 个交易日按内在价值平仓")
    print(f"引擎口径：T=6 天、K=5 元取整、滑点 {SLIPPAGE:.0%}；无未来函数")
    print("=" * 82)

    for name, bt in (("口径A 引擎+历史IV", engine_bt), ("口径B 市场预期代理", proxy_bt)):
        print(f"\n[{name}]  n={len(bt['periods'])}")
        print(f"  样本：{', '.join(ev['period'] for ev in bt['periods'])}")
        for h in HORIZONS:
            s = bt["stats"].get(f"d{h}", {})
            ss = bt["stats"].get(f"d{h}_short", {})
            if s.get("n", 0) == 0:
                continue
            print(
                f"  d+{h}: 平均ROI {s['mean_roi_pct']:+.1f}% ± {s['se_roi_pct']:.1f}% "
                f"(t={s['t_stat']:.2f}) | 中位 {s['median_roi_pct']:+.1f}% | "
                f"胜率 {s['win_rate_pct']:.0f}% | 最好 {s['best_pct']:+.0f}%({s['best_label']}) | "
                f"最差 {s['worst_pct']:+.0f}%({s['worst_label']})"
            )
            if ss.get("n", 0):
                print(
                    f"   卖跨式 d+{h}: 平均ROI {ss['mean_roi_pct']:+.1f}% ± {ss['se_roi_pct']:.1f}% "
                    f"(t={ss['t_stat']:.2f}) | 中位 {ss['median_roi_pct']:+.1f}% | "
                    f"胜率 {ss['win_rate_pct']:.0f}% | 最差 {ss['worst_pct']:+.0f}%({ss['worst_label']})"
                )

    print("\n按年度拆分（持有 d+2）")
    for name, split in (("口径A", result["year_split_engine_d2"]), ("口径B", result["year_split_proxy_d2"])):
        print(f"  {name}: " + ", ".join(f"{y} n={v['n']} avg={v['mean_roi_pct']:+.0f}% win={v['win_rate_pct']:.0f}%" for y, v in split.items()))
    print("\n卖跨式按年度拆分（持有 d+2）")
    for name, split in (("口径A", result["year_split_engine_d2_short"]), ("口径B", result["year_split_proxy_d2_short"])):
        print(f"  {name}: " + ", ".join(f"{y} n={v['n']} avg={v['mean_roi_pct']:+.0f}% win={v['win_rate_pct']:.0f}%" for y, v in split.items()))

    print(f"\n结论文件：{OUT}")
    print("免责声明：历史回测不代表未来；模拟盘基准，非投资建议。")


if __name__ == "__main__":
    main()
