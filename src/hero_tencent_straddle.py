"""腾讯 0700.HK 业绩跨式 Hero 用例（决策支持，默认模拟盘，不下单）。

输入：data/hero_inputs.json（futuapi 快照）
计算：src/pricing_engine.py（自研引擎，IV 二分求解，Greeks bump-and-reprice）
输出：控制台方案表 + data/hero_proposal_*.json + research/audit/audit_log.jsonl
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "data" / "hero_inputs.json"
OUT_DIR = ROOT / "data"
AUDIT_LOG_SCRIPT = (
    ROOT / ".agents" / "skills" / "futu-options-agent" / "scripts" / "audit_log.py"
)

from src.pricing_engine import greeks, implied_volatility, price  # noqa: E402


def load_inputs() -> dict:
    with open(INPUTS, "r", encoding="utf-8") as fh:
        return json.load(fh)


def leg_analysis(spot, strike, dte, mid, r, q, opt_type):
    T = dte / 365.0
    iv = implied_volatility(mid, spot, strike, T, r, q, opt_type, american=True)
    g = greeks(spot, strike, T, r, q, iv, opt_type, american=True)
    return iv, g


def expiry_analysis(spot, expiry, call, put, r, q, account):
    dte = expiry["dte"]
    mult = account["contract_multiplier"]
    iv_c, g_c = leg_analysis(spot, call["strike"], dte, call["mid"], r, q, "CALL")
    iv_p, g_p = leg_analysis(spot, put["strike"], dte, put["mid"], r, q, "PUT")

    premium_mid_share = call["mid"] + put["mid"]
    premium_ask_share = call["ask"] + put["ask"]
    cost_lot_mid = premium_mid_share * mult
    cost_lot_ask = premium_ask_share * mult

    cash = account["cash_hkd"]
    budget = cash * account["risk_budget_pct"] / 100.0
    lots = math.floor(min(cash / cost_lot_ask, budget / cost_lot_ask)) if cost_lot_ask > 0 else 0
    lots = max(lots, 0)

    return {
        "expiry": expiry["expiry"],
        "dte": dte,
        "strike": call["strike"],
        "call": {"code": call["code"], "iv": iv_c, "greeks": g_c},
        "put": {"code": put["code"], "iv": iv_p, "greeks": g_p},
        "premium_mid_share": premium_mid_share,
        "premium_ask_share": premium_ask_share,
        "cost_lot_mid": cost_lot_mid,
        "cost_lot_ask": cost_lot_ask,
        "budget_hkd": budget,
        "lots": lots,
        "max_loss_ask": cost_lot_ask * lots,
        "straddle_greeks": {
            "delta": (g_c["delta"] + g_p["delta"]) * mult,
            "gamma": (g_c["gamma"] + g_p["gamma"]) * mult,
            "vega": (g_c["vega"] + g_p["vega"]) * mult,
            "theta": (g_c["theta"] + g_p["theta"]) * mult,
            "rho": (g_c["rho"] + g_p["rho"]) * mult,
        },
        "breakeven_low": call["strike"] - premium_mid_share,
        "breakeven_high": call["strike"] + premium_mid_share,
    }


def expiry_pnl_at_expiry(spot, strike, move_pct, lots, cost_lot_ask, mult, direction=1):
    S = spot * (1 + direction * move_pct / 100.0)
    intrinsic = abs(S - strike)
    value = intrinsic * mult * lots
    return S, value - cost_lot_ask * lots


def post_earnings_value(spot, strike, move_pct, direction, T_after, sigma, mult, lots):
    S = spot * (1 + direction * move_pct / 100.0)
    call_v = price(S, strike, T_after, 0.035, 0.0, sigma, "CALL", american=True)
    put_v = price(S, strike, T_after, 0.035, 0.0, sigma, "PUT", american=True)
    return S, (call_v + put_v) * mult * lots


def fmt_hkd(x: float) -> str:
    return f"{x:,.0f}"


def build_proposal(data, primary, secondary):
    spot = data["spot"]
    mult = data["account"]["contract_multiplier"]
    exp_move = data["earnings"]["expected_move_pct"]

    primary_pnl = []
    for factor, label in ((1.0, "±预期波动"), (1.5, "±1.5×预期"), (2.0, "±2×预期")):
        rows = []
        for d in (1, -1):
            S, pnl = expiry_pnl_at_expiry(
                spot, primary["strike"], exp_move * factor, primary["lots"],
                primary["cost_lot_ask"], mult, d,
            )
            rows.append({"direction": "up" if d > 0 else "down", "spot": round(S, 2), "pnl": round(pnl, 2)})
        primary_pnl.append({"label": label, "rows": rows})

    crush_rows = []
    avg_iv = (primary["call"]["iv"] + primary["put"]["iv"]) / 2.0
    for crush in (0.20, 0.35, 0.50):
        rows = []
        for d in (1, -1):
            S, value = post_earnings_value(
                spot, primary["strike"], exp_move, d, 2.0 / 365.0,
                avg_iv * (1 - crush), mult, primary["lots"],
            )
            rows.append({
                "direction": "up" if d > 0 else "down",
                "spot": round(S, 2),
                "value": round(value, 2),
                "pnl": round(value - primary["max_loss_ask"], 2),
            })
        crush_rows.append({"iv_crush": f"-{int(crush*100)}%", "rows": rows})

    return {
        "captured_at": data["captured_at"],
        "underlying": data["underlying"],
        "scenario": {
            "view": "uncertain",
            "horizon": f"{data['earnings']['date']} 业绩",
            "account_hkd": data["account"]["cash_hkd"],
            "risk_budget_pct": data["account"]["risk_budget_pct"],
            "contract_multiplier": mult,
        },
        "primary_expiry": primary["expiry"],
        "secondary_expiry": secondary["expiry"],
        "legs": [
            {
                "expiry": primary["expiry"],
                "call": {"code": primary["call"]["code"], "iv_solved": round(primary["call"]["iv"], 4)},
                "put": {"code": primary["put"]["code"], "iv_solved": round(primary["put"]["iv"], 4)},
                "lots": primary["lots"],
                "cost_per_lot_mid": round(primary["cost_lot_mid"], 2),
                "cost_per_lot_ask": round(primary["cost_lot_ask"], 2),
                "max_loss": round(primary["max_loss_ask"], 2),
                "breakeven": [round(primary["breakeven_low"], 2), round(primary["breakeven_high"], 2)],
                "straddle_greeks_per_lot": {k: round(v, 4) for k, v in primary["straddle_greeks"].items()},
                "pnl_at_expiry": primary_pnl,
                "pnl_after_iv_crush": crush_rows,
            },
            {
                "expiry": secondary["expiry"],
                "call": {"code": secondary["call"]["code"], "iv_solved": round(secondary["call"]["iv"], 4)},
                "put": {"code": secondary["put"]["code"], "iv_solved": round(secondary["put"]["iv"], 4)},
                "lots": secondary["lots"],
                "cost_per_lot_mid": round(secondary["cost_lot_mid"], 2),
                "cost_per_lot_ask": round(secondary["cost_lot_ask"], 2),
                "max_loss": round(secondary["max_loss_ask"], 2),
                "breakeven": [round(secondary["breakeven_low"], 2), round(secondary["breakeven_high"], 2)],
                "straddle_greeks_per_lot": {k: round(v, 4) for k, v in secondary["straddle_greeks"].items()},
            },
        ],
        "sources": ["futuapi: option_quote/snapshot/earnings_screener", "data/hero_inputs.json"],
        "disclaimer": "决策支持/研究用途，非投资建议；默认模拟盘，任何订单须人机确认。",
    }


def risk_audit(data, primary, secondary):
    findings = []
    blocked = []

    if primary["max_loss_ask"] <= primary["budget_hkd"]:
        findings.append(f"PASS 单腿组合最大亏损 {fmt_hkd(primary['max_loss_ask'])} ≤ 预算 {fmt_hkd(primary['budget_hkd'])}")
    else:
        blocked.append("主方案最大亏损超过 5% 风险预算")

    if primary["cost_lot_ask"] * max(primary["lots"], 1) <= data["account"]["cash_hkd"]:
        findings.append(f"PASS 权利金占用不超过可用现金 {fmt_hkd(data['account']['cash_hkd'])}")
    else:
        blocked.append("权利金超过可用现金")

    primary_input = next(item for item in data["legs"] if item["expiry"] == primary["expiry"])
    secondary_input = next(item for item in data["legs"] if item["expiry"] == secondary["expiry"])

    def spread_pct(leg):
        return (leg["ask"] - leg["bid"]) / leg["mid"] * 100 if leg["mid"] > 0 else float("inf")

    findings.append(
        "WARN 主到期盘口价差占 mid："
        f"call {spread_pct(primary_input['call']):.1f}% / "
        f"put {spread_pct(primary_input['put']):.1f}%，建议限价单"
    )
    breakeven_move_pct = abs(primary["strike"] - primary["breakeven_low"]) / data["spot"] * 100
    findings.append(
        f"WARN 市场预期波动 {data['earnings']['expected_move_pct']:.2f}% 与跨式盈亏平衡变动 "
        f"{breakeven_move_pct:.2f}% 对比；不足时到期损益为负"
    )
    findings.append(
        "WARN IV crush：最近/历史参考分别为 "
        f"{data['earnings']['last_report_iv_crush']:.2f}/"
        f"{data['earnings']['history_report_iv_crush']:.2f}pp；另测 -20%/-35%/-50% 相对回落"
    )
    findings.append("WARN 美式个股期权可提前行权 + 实物交割，存在 pin/assignment 风险；持仓到期前注意处理")
    findings.append(
        f"NOTE 主到期 OI（call {primary_input['call']['open_interest']} / "
        f"put {primary_input['put']['open_interest']}）；次到期 OI（call "
        f"{secondary_input['call']['open_interest']} / put {secondary_input['put']['open_interest']}）"
    )

    return {"decision": "PASS" if not blocked else "BLOCK", "blocked": blocked, "findings": findings}


def audit(event: str, payload: dict) -> None:
    cmd = [sys.executable, str(AUDIT_LOG_SCRIPT), "--event", event]
    subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False), text=True, check=True)


def main() -> None:
    data = load_inputs()
    spot = data["spot"]
    r = data["model"]["riskfree_rate"]
    q = data["model"]["div_yield"]
    account = data["account"]

    groups = {leg["expiry"]: leg for leg in data["legs"]}
    a14 = expiry_analysis(spot, groups["2026-08-14"], groups["2026-08-14"]["call"],
                          groups["2026-08-14"]["put"], r, q, account)
    a28 = expiry_analysis(spot, groups["2026-08-28"], groups["2026-08-28"]["call"],
                          groups["2026-08-28"]["put"], r, q, account)

    proposal = build_proposal(data, a14, a28)
    audit_payload = proposal
    out_path = OUT_DIR / "hero_proposal_2026-08-08.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(proposal, fh, ensure_ascii=False, indent=2)

    ra = risk_audit(data, a14, a28)
    audit("scenario_parsed", proposal["scenario"])
    audit("proposal", audit_payload)
    audit("risk_audit", ra)

    print("=" * 78)
    print("腾讯 0700.HK 业绩跨式方案（决策支持，默认模拟盘）")
    print(f"数据快照：{data['captured_at']} | {data['source']} | 市场状态：{data['market_state']}")
    print(f"正股：{data['name']} {spot} | 业绩：{data['earnings']['date']} 2026Q2 | 预期波动 ±{data['earnings']['expected_move_pct']}%")
    print(f"IV {data['earnings']['iv']:.1f}% | IV Rank {data['earnings']['iv_rank']:.1f} | IV Pct {data['earnings']['iv_percentile']:.1f} | HV30 {data['earnings']['hv_30d']:.1f}%")
    print("=" * 78)

    for a in (a14, a28):
        print(f"\n[{a['expiry']}  DTE {a['dte']}]")
        print(f"  买入 {a['call']['code']} + {a['put']['code']}  x {a['lots']} 张")
        print(f"  自研 IV：CALL {a['call']['iv']*100:.2f}% / PUT {a['put']['iv']*100:.2f}%")
        print(f"  权利金 mid {a['premium_mid_share']:.2f}/股（{fmt_hkd(a['cost_lot_mid'])}/张）| ask {a['premium_ask_share']:.2f}/股（{fmt_hkd(a['cost_lot_ask'])}/张）")
        print(f"  预算 {fmt_hkd(a['budget_hkd'])} | 最大亏损（ask）{fmt_hkd(a['max_loss_ask'])}")
        print(f"  盈亏平衡 {a['breakeven_low']:.2f} / {a['breakeven_high']:.2f}")
        print(f"  每张 Greeks：Δ {a['straddle_greeks']['delta']:.4f} | Γ {a['straddle_greeks']['gamma']:.4f} | ν {a['straddle_greeks']['vega']:.4f} | Θ {a['straddle_greeks']['theta']:.4f} | ρ {a['straddle_greeks']['rho']:.4f}")

    print("\n[8/14 主方案到期损益（含 2 张，ask 成本）]")
    for row in proposal["legs"][0]["pnl_at_expiry"]:
        for r_ in row["rows"]:
            print(f"  {row['label']} {r_['direction']}: spot {r_['spot']} -> {fmt_hkd(r_['pnl'])}")

    print("\n[8/14 业绩后 2 日、IV crush 情景（含 2 张，相对 ask 成本）]")
    for row in proposal["legs"][0]["pnl_after_iv_crush"]:
        for r_ in row["rows"]:
            print(f"  crush {row['iv_crush']} {r_['direction']}: spot {r_['spot']} -> {fmt_hkd(r_['pnl'])}")

    print(f"\n风险审计：{ra['decision']}")
    for f_ in ra["findings"]:
        print(f"  - {f_}")
    print(f"\n方案 JSON：{out_path}")
    print("免责声明：决策支持/研究用途，非投资建议；默认模拟盘，任何订单须人机确认。")


if __name__ == "__main__":
    main()
