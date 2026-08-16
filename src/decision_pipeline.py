"""GOAI 期权智能终端 - 端到端决策管线。

五阶段：场景解析 -> 数据获取 -> 自研引擎 -> Edge/Risk/Action 门控 -> 决策卡 + 审计留痕。

铁律：
- LLM 只做场景解析与解释；所有金融数字来自冻结 Futu 快照或自研引擎；
- 允许 NO_TRADE / BLOCK / DRAFT_ONLY，不为展示下单而调低门槛；
- 模拟动作必须由用户独立确认；比赛版本不注册实盘工具。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.hero_tencent_straddle import (
    AUDIT_LOG_SCRIPT,
    OUT_DIR,
    build_proposal,
    expiry_analysis,
)
from src.payload_validation import reject_sensitive_fields
from src.macro_assessment import build_macro_assessment
from src.research_evidence import build_research_evidence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "hero_inputs.json"
DEFAULT_BACKTEST = ROOT / "data" / "backtest_tencent_straddle.json"

VIEWS = {"bullish", "bearish", "uncertain"}
_MAX_DIVIDEND_ENTRIES = 12
_MARKET_SYMBOL_RE = re.compile(
    r"[A-Z][A-Z0-9_-]{1,15}\.[A-Z0-9][A-Z0-9._-]{0,46}"
)


def _reject_sensitive(raw: Mapping[str, Any]) -> None:
    # 敏感键拒绝逻辑收敛到 payload_validation.reject_sensitive_fields
    reject_sensitive_fields(raw, label="scenario")


def parse_scenario(raw: Mapping[str, Any]) -> dict[str, Any]:
    """阶段 1：场景解析。未知字段不补造，敏感字段直接拒绝。"""

    if not isinstance(raw, dict):
        raise ValueError("scenario must be an object")
    _reject_sensitive(raw)

    underlying = raw.get("underlying")
    if not isinstance(underlying, str) or not underlying.strip():
        raise ValueError("underlying must be a non-empty market-qualified symbol")
    normalized_underlying = underlying.strip().upper()
    if not _MARKET_SYMBOL_RE.fullmatch(normalized_underlying):
        raise ValueError(
            "underlying must include a valid market prefix, e.g. HK.00700 or SSE.600519"
        )

    view = raw.get("view")
    if not isinstance(view, str) or view.strip().lower() not in VIEWS:
        raise ValueError("view must be one of bullish / bearish / uncertain")

    horizon = raw.get("horizon")
    if not isinstance(horizon, str) or not horizon.strip():
        raise ValueError("horizon must be a non-empty string")

    cash = raw.get("account_cash_hkd")
    if (
        isinstance(cash, bool)
        or not isinstance(cash, (int, float))
        or not math.isfinite(float(cash))
        or cash <= 0
    ):
        raise ValueError("account_cash_hkd must be a positive finite number")

    pct = raw.get("risk_budget_pct")
    if (
        isinstance(pct, bool)
        or not isinstance(pct, (int, float))
        or not math.isfinite(float(pct))
        or pct <= 0
        or pct > 100
    ):
        raise ValueError("risk_budget_pct must be in (0, 100]")

    constraints = raw.get("constraints", [])
    if not isinstance(constraints, list) or not all(
        isinstance(item, str) for item in constraints
    ):
        raise ValueError("constraints must be a list of strings")

    return {
        "underlying": normalized_underlying,
        "view": view.strip().lower(),
        "horizon": horizon.strip(),
        "account_cash_hkd": float(cash),
        "risk_budget_pct": float(pct),
        "constraints": list(constraints),
    }


def _require_number(payload: Mapping[str, Any], key: str, *, positive: bool = False) -> float:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (positive and value <= 0)
    ):
        raise ValueError(f"frozen snapshot field {key!r} is invalid")
    return float(value)


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"frozen snapshot field {key!r} is invalid")
    return value.strip()


def _validate_leg(leg: Mapping[str, Any], name: str) -> None:
    if not isinstance(leg, dict):
        raise ValueError(f"frozen snapshot leg {name!r} must be an object")
    _require_text(leg, "code")
    _require_number(leg, "strike", positive=True)
    _require_number(leg, "bid")
    _require_number(leg, "ask")
    _require_number(leg, "mid", positive=True)
    _require_number(leg, "open_interest")


def load_frozen_snapshot(
    path: str | Path = DEFAULT_INPUT,
) -> dict[str, Any]:
    """阶段 2：数据获取。加载冻结 Futu 快照并计算内容哈希。"""

    snapshot_path = Path(path)
    try:
        raw_bytes = snapshot_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read frozen snapshot {snapshot_path}") from exc
    if len(raw_bytes) > 4 * 1024 * 1024:
        raise ValueError("frozen snapshot exceeds the 4 MiB limit")
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen snapshot is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("frozen snapshot must be a JSON object")
    _reject_sensitive(payload)

    captured_at = _require_text(payload, "captured_at")
    source = _require_text(payload, "source")
    underlying = _require_text(payload, "underlying")
    spot = _require_number(payload, "spot", positive=True)

    earnings = payload.get("earnings")
    if not isinstance(earnings, dict):
        raise ValueError("frozen snapshot earnings section is missing")
    _require_text(earnings, "date")
    _require_number(earnings, "expected_move_pct", positive=True)
    _require_number(earnings, "iv", positive=True)
    _require_number(earnings, "iv_rank", positive=True)
    _require_number(earnings, "iv_percentile", positive=True)

    account = payload.get("account")
    if not isinstance(account, dict):
        raise ValueError("frozen snapshot account section is missing")
    _require_number(account, "cash_hkd", positive=True)
    _require_number(account, "risk_budget_pct", positive=True)
    _require_number(account, "contract_multiplier", positive=True)

    model = payload.get("model")
    if not isinstance(model, dict):
        raise ValueError("frozen snapshot model section is missing")
    _require_number(model, "riskfree_rate")
    _require_number(model, "div_yield")
    _validate_dividend_declarations(model)

    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("frozen snapshot must contain at least one expiry leg")
    for index, group in enumerate(legs):
        if not isinstance(group, dict):
            raise ValueError(f"frozen snapshot leg group {index} must be an object")
        _require_text(group, "expiry")
        _require_number(group, "dte", positive=True)
        call_leg = group.get("call")
        put_leg = group.get("put")
        if not isinstance(call_leg, dict) or not isinstance(put_leg, dict):
            raise ValueError(f"frozen snapshot leg group {index} must contain call and put")
        _validate_leg(call_leg, "call")
        _validate_leg(put_leg, "put")

    return {
        "mode": "REPLAY",
        "origin": "FUTU",
        "freshness": "FROZEN",
        "captured_at": captured_at,
        "source": source,
        "underlying": underlying,
        "spot": spot,
        "snapshot_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "payload": payload,
    }


def _validate_dividend_declarations(
    model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Structurally validate optional ``model.dividends`` and normalise entries.

    Each entry is ``{"ex_date": "YYYY-MM-DD", "amount": float}``；格式非法、
    重复除息日、非正股息额均显式报错。键缺失时返回空列表（不改变既有口径）。
    """

    raw = model.get("dividends")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("frozen snapshot model.dividends must be a list")
    if len(raw) > _MAX_DIVIDEND_ENTRIES:
        raise ValueError(
            f"frozen snapshot model.dividends exceeds {_MAX_DIVIDEND_ENTRIES} entries"
        )
    entries: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"model.dividends[{index}] must be an object")
        ex_date_raw = item.get("ex_date")
        if not isinstance(ex_date_raw, str) or not ex_date_raw.strip():
            raise ValueError(f"model.dividends[{index}].ex_date is required")
        try:
            ex_date = date.fromisoformat(ex_date_raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"model.dividends[{index}].ex_date must be a canonical ISO date"
            ) from exc
        if ex_date.isoformat() != ex_date_raw.strip():
            raise ValueError(
                f"model.dividends[{index}].ex_date must be a canonical ISO date"
            )
        amount = item.get("amount")
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or float(amount) <= 0
        ):
            raise ValueError(
                f"model.dividends[{index}].amount must be a positive finite number"
            )
        if ex_date_raw.strip() in seen_dates:
            raise ValueError(
                f"model.dividends contains duplicate ex_date {ex_date_raw.strip()}"
            )
        seen_dates.add(ex_date_raw.strip())
        entries.append({"ex_date": ex_date_raw.strip(), "amount": float(amount)})
    return entries


def _parse_dividend_schedule(
    payload: Mapping[str, Any],
) -> tuple[list[tuple[float, float]], list[dict[str, Any]]]:
    """Parse optional discrete-dividend facts into an engine schedule.

    Returns ``(schedule, summary)``：schedule 是引擎消费的
    ``(tau_years, amount)`` 列表（只含快照捕获日之后除息的股息），summary 是
    全部声明股息的展示/审计记录（含 ``applied`` 状态，已除息的如实标注）。
    """

    model = payload.get("model")
    declarations = _validate_dividend_declarations(
        model if isinstance(model, Mapping) else {}
    )
    captured_raw = str(payload.get("captured_at") or "")
    try:
        captured = datetime.fromisoformat(captured_raw).date()
    except ValueError as exc:
        raise ValueError(
            "snapshot captured_at must be a valid ISO timestamp to price discrete dividends"
        ) from exc
    schedule: list[tuple[float, float]] = []
    summary: list[dict[str, Any]] = []
    for entry in sorted(declarations, key=lambda item: item["ex_date"]):
        ex_date = date.fromisoformat(entry["ex_date"])
        days = (ex_date - captured).days
        tau = days / 365.0
        summary.append(
            {
                "ex_date": entry["ex_date"],
                "amount": entry["amount"],
                "tau_years": round(max(tau, 0.0), 6),
                # 是否进入任一到期定价窗口由 compute_engine 在知道 dte 后判定
                "applied": False,
            }
        )
        if days > 0:
            schedule.append((tau, entry["amount"]))
    return schedule, summary


def compute_engine(
    data: dict[str, Any],
    cost_model: Mapping[str, Any] | None = None,
    scenario: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """阶段 3：自研引擎。IV 二分求解、Greeks bump-and-reprice、成本与情景损益。"""

    payload = data["payload"]
    spot = data["spot"]
    r = payload["model"]["riskfree_rate"]
    q = payload["model"]["div_yield"]
    dividends, dividend_summary = _parse_dividend_schedule(payload)
    # applied = 除息日落在至少一个到期（含最远到期）的定价窗口内
    max_t = max(group["dte"] for group in payload["legs"]) / 365.0
    for item in dividend_summary:
        item["applied"] = 0.0 < item["tau_years"] < max_t
    account = payload["account"]
    groups = {group["expiry"]: group for group in payload["legs"]}
    expiries = sorted(groups, key=lambda item: groups[item]["dte"])
    if len(expiries) < 2:
        raise ValueError("frozen snapshot requires at least two expiry legs")
    primary = expiry_analysis(
        spot,
        groups[expiries[0]],
        groups[expiries[0]]["call"],
        groups[expiries[0]]["put"],
        r,
        q,
        account,
        dividends,
    )
    secondary = expiry_analysis(
        spot,
        groups[expiries[1]],
        groups[expiries[1]]["call"],
        groups[expiries[1]]["put"],
        r,
        q,
        account,
        dividends,
    )

    model = {
        "fees_hkd_per_lot": 0.0,
        "slippage_bps": 0.0,
        "status": "UNVERIFIED",
        "note": "费用与滑点 policy 尚未冻结，当前以 ask 为可成交成本；正式发布前需补齐",
    }
    if cost_model is not None:
        if not isinstance(cost_model, dict):
            raise ValueError("cost_model must be an object")
        fees = cost_model.get("fees_hkd_per_lot", 0.0)
        slippage_bps = cost_model.get("slippage_bps", 0.0)
        if (
            isinstance(fees, bool)
            or not isinstance(fees, (int, float))
            or not math.isfinite(float(fees))
            or fees < 0
        ):
            raise ValueError("cost_model.fees_hkd_per_lot must be a non-negative number")
        if (
            isinstance(slippage_bps, bool)
            or not isinstance(slippage_bps, (int, float))
            or not math.isfinite(float(slippage_bps))
            or slippage_bps < 0
        ):
            raise ValueError("cost_model.slippage_bps must be a non-negative number")
        model = {
            "fees_hkd_per_lot": float(fees),
            "slippage_bps": float(slippage_bps),
            "status": "VERIFIED",
            "note": "调用方提供的已验证成本模型",
        }

    for item in (primary, secondary):
        slippage = item["cost_lot_ask"] * model["slippage_bps"] / 10_000.0
        item["cost_lot_exec"] = item["cost_lot_ask"] + model["fees_hkd_per_lot"] + slippage
        item["max_loss_exec"] = item["cost_lot_exec"] * item["lots"]

    proposal = build_proposal(payload, primary, secondary, scenario=scenario, dividends=dividends)
    return {
        "primary": primary,
        "secondary": secondary,
        "proposal": proposal,
        "cost_model": model,
        "dividends": dividend_summary,
    }


def _load_backtest_summary(path: str | Path = DEFAULT_BACKTEST) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"available": False}
    if not isinstance(raw, dict):
        return {"available": False}
    engine_section = raw.get("engine_backtest")
    proxy_section = raw.get("proxy_backtest")
    if not isinstance(engine_section, dict) or not isinstance(proxy_section, dict):
        return {"available": False}
    engine_stats = engine_section.get("stats")
    proxy_stats = proxy_section.get("stats")
    if not isinstance(engine_stats, dict) or not isinstance(proxy_stats, dict):
        return {"available": False}
    engine_d2 = engine_stats.get("d2")
    proxy_d2 = proxy_stats.get("d2")
    if not isinstance(engine_d2, dict) or not isinstance(proxy_d2, dict):
        return {"available": False}
    return {
        "available": True,
        "engine_d2_mean_roi_pct": engine_d2.get("mean_roi_pct"),
        "engine_d2_win_rate_pct": engine_d2.get("win_rate_pct"),
        "engine_d2_n": engine_d2.get("n"),
        "proxy_d2_mean_roi_pct": proxy_d2.get("mean_roi_pct"),
        "proxy_d2_win_rate_pct": proxy_d2.get("win_rate_pct"),
    }


def edge_gate(
    engine: dict[str, Any],
    earnings: Mapping[str, Any],
    spot: float,
    backtest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """阶段 4a：Edge 门。预期波动 vs 盈亏平衡、历史回测、IV crush。"""

    primary = engine["primary"]
    breakeven_low = primary["breakeven_low"]
    strike = primary["strike"]
    breakeven_move_pct = abs(strike - breakeven_low) / spot * 100.0 if spot > 0 else 0.0
    expected_move_pct = float(earnings["expected_move_pct"])
    checks = []

    if expected_move_pct < breakeven_move_pct:
        checks.append(
            {
                "check": "预期波动 vs 盈亏平衡",
                "result": "FAIL",
                "detail": (
                    f"预期波动 {expected_move_pct:.2f}% < 盈亏平衡所需 {breakeven_move_pct:.2f}%"
                ),
            }
        )
    else:
        checks.append(
            {
                "check": "预期波动 vs 盈亏平衡",
                "result": "PASS",
                "detail": (
                    f"预期波动 {expected_move_pct:.2f}% >= 盈亏平衡所需 {breakeven_move_pct:.2f}%"
                ),
            }
        )

    backtest_summary = dict(backtest) if backtest is not None else _load_backtest_summary()
    if backtest_summary.get("available"):
        mean_roi = backtest_summary["engine_d2_mean_roi_pct"]
        win_rate = backtest_summary["engine_d2_win_rate_pct"]
        if isinstance(mean_roi, (int, float)) and isinstance(win_rate, (int, float)):
            if mean_roi < 0 and win_rate < 50:
                checks.append(
                    {
                        "check": "历史业绩跨式回测",
                        "result": "FAIL",
                        "detail": (
                            f"d+2 平均 ROI {mean_roi:.1f}%、胜率 {win_rate:.1f}%，"
                            "买入跨式历史为负期望"
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "check": "历史业绩跨式回测",
                        "result": "PASS",
                        "detail": "近期样本未显示系统性负期望",
                    }
                )
        else:
            checks.append(
                {
                    "check": "历史业绩跨式回测",
                    "result": "UNKNOWN",
                    "detail": "回测摘要字段缺失",
                }
            )
    else:
        checks.append(
            {
                "check": "历史业绩跨式回测",
                "result": "UNKNOWN",
                "detail": "回测摘要不可用，不参与 Edge 结论",
            }
        )

    legs = engine["proposal"]["legs"]
    primary_leg = next(
        (leg for leg in legs if leg.get("expiry") == engine["primary"].get("expiry")),
        legs[0],
    )
    crush = next(
        (
            row
            for row in primary_leg.get("pnl_after_iv_crush", [])
            if row.get("iv_crush") == "-20%"
        ),
        None,
    )
    if crush is None or not crush.get("rows"):
        checks.append(
            {
                "check": "IV crush 情景",
                "result": "UNKNOWN",
                "detail": "IV crush 情景数据缺失，不参与 Edge 结论",
            }
        )
    else:
        worst_crush_pnl = min(row["pnl"] for row in crush["rows"])
        if worst_crush_pnl < 0:
            checks.append(
                {
                    "check": "IV crush 情景",
                    "result": "FAIL",
                    "detail": f"预期波动 + IV -20% 后最差情景亏损 {worst_crush_pnl:,.0f} HKD",
                }
            )
        else:
            checks.append(
                {
                    "check": "IV crush 情景",
                    "result": "PASS",
                    "detail": "预期波动下 IV crush 情景未转负",
                }
            )

    failed = any(item["result"] == "FAIL" for item in checks)
    return {
        "verdict": "LOW_EDGE" if failed else "ADEQUATE",
        "checks": checks,
        "recommendation": "NO_TRADE" if failed else "EVALUATE",
    }


def risk_gate(engine: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """阶段 4b：Risk 门。预算、现金、乘数、流动性与可验证性，一票否决。"""

    payload = data["payload"]
    account = payload["account"]
    primary = engine["primary"]
    primary_input = next(item for item in payload["legs"] if item["expiry"] == primary["expiry"])
    secondary_input = next(item for item in payload["legs"] if item["expiry"] == engine["secondary"]["expiry"])

    cash = float(account["cash_hkd"])
    budget = cash * float(account["risk_budget_pct"]) / 100.0
    blocked: list[str] = []
    findings: list[str] = []

    max_loss = primary["max_loss_exec"]
    if int(primary.get("lots", 0)) < 1:
        blocked.append(
            f"风险预算 {budget:,.0f} HKD 不足以覆盖最小 1 张方案成本 {primary['cost_lot_exec']:,.0f} HKD"
        )
    elif max_loss <= budget:
        findings.append(
            f"PASS 最大亏损（executable 口径）{max_loss:,.0f} <= 预算 {budget:,.0f} HKD"
        )
    else:
        budget_pct = float(account["risk_budget_pct"])
        blocked.append(
            f"最大亏损 {max_loss:,.0f} 超过 {budget_pct:g}% 风险预算 {budget:,.0f} HKD"
        )

    if primary["cost_lot_exec"] * max(primary["lots"], 1) <= cash:
        findings.append(f"PASS 权利金占用不超可用现金 {cash:,.0f} HKD")
    else:
        blocked.append("权利金超过可用现金")

    multiplier = float(account["contract_multiplier"])
    if multiplier > 0 and multiplier % 1 == 0:
        findings.append(f"PASS 合约乘数 {multiplier:.0f} 股/张（冻结快照；HKEX 规格页复核待办）")
    else:
        blocked.append("合约乘数无效或无法验证")

    def spread_pct(leg: Mapping[str, Any]) -> float:
        mid = float(leg["mid"])
        return (float(leg["ask"]) - float(leg["bid"])) / mid * 100.0 if mid > 0 else float("inf")

    call_spread = spread_pct(primary_input["call"])
    put_spread = spread_pct(primary_input["put"])
    if max(call_spread, put_spread) <= 15.0:
        findings.append(
            f"PASS 主到期盘口价差 call {call_spread:.1f}% / put {put_spread:.1f}%"
        )
    else:
        findings.append(
            f"WARN 主到期盘口价差 call {call_spread:.1f}% / put {put_spread:.1f}%，建议限价单"
        )

    low_oi = [
        f"{label} {group['call']['open_interest']:,.0f}/{group['put']['open_interest']:,.0f}"
        for label, group in (("主到期", primary_input), ("次到期", secondary_input))
    ]
    if min(primary_input["call"]["open_interest"], primary_input["put"]["open_interest"]) >= 500:
        findings.append("PASS 主到期 OI 高于 500 张（流动性参考）")
    else:
        findings.append("WARN 主到期 OI 偏低，流动性不足风险")
    findings.append("NOTE OI：" + "；".join(low_oi))

    findings.append(
        "WARN 持仓限额/LOP 与 get_max_trd_qtys 未在本次冻结快照中验证；"
        "进入模拟提交前必须用 Live 账户复核"
    )
    findings.append(
        "WARN 费用/滑点 policy 未冻结，executable 成本当前以 ask 计（状态 UNVERIFIED）"
    )
    findings.append(
        "WARN 美式个股期权可提前行权 + 实物交割，存在 pin/assignment 风险"
    )

    return {
        "decision": "PASS" if not blocked else "BLOCK",
        "blocked": blocked,
        "findings": findings,
    }


def action_gate(
    data: dict[str, Any],
    edge: dict[str, Any],
    risk: dict[str, Any],
    *,
    human_confirmed: bool = False,
) -> dict[str, Any]:
    """阶段 4c：Action 门。先 Edge，再 Risk，再数据模式，最后人工确认。"""

    if edge["recommendation"] == "NO_TRADE":
        return {
            "action": "NO_TRADE",
            "blocked": ["Edge 门未过：预期波动不足以覆盖跨式成本"],
            "next_step": "无需进入下单步骤；可调整期限/行权价后重新评估",
        }
    if risk["decision"] == "BLOCK":
        return {
            "action": "BLOCK",
            "blocked": risk["blocked"],
            "next_step": "修正风险违规项后重新审计",
        }
    if data["mode"] != "LIVE" or data["freshness"] != "FRESH":
        return {
            "action": "DRAFT_ONLY",
            "blocked": ["数据为冻结快照/回放，非 Live 新鲜数据"],
            "next_step": "接入 Live 行情并复核规格后重新评估",
        }
    if not human_confirmed:
        return {
            "action": "DRAFT_ONLY",
            "blocked": ["未获得用户独立确认"],
            "next_step": "展示方案摘要，等待用户独立确认",
        }
    return {
        "action": "READY_FOR_CONFIRMATION",
        "blocked": [],
        "next_step": "提交前复核 + Futu SIMULATE（比赛版本仅模拟盘）",
    }


_VIEW_LABELS = {
    "bullish": "看涨",
    "bearish": "看跌",
    "uncertain": "方向不确定",
}
_ACTION_LABELS = {
    "NO_TRADE": "不交易",
    "BLOCK": "风险阻断",
    "DRAFT_ONLY": "仅草案（待 Live 数据/人工确认）",
    "READY_FOR_CONFIRMATION": "可进入用户确认流程",
}


def _summary_text(
    scenario: Mapping[str, Any] | None,
    edge: Mapping[str, Any],
    risk: Mapping[str, Any],
    action: Mapping[str, Any],
) -> str:
    """由 Edge/Risk/Action 三门控的实际结果生成 summary，不写死任何结论。"""

    view = str(scenario.get("view", "uncertain")).lower() if scenario else "uncertain"
    view_label = _VIEW_LABELS.get(view, view)
    horizon = scenario.get("horizon") if scenario else None
    head = f"{view_label}观点、{horizon}场景" if horizon else f"{view_label}观点场景"

    fail_count = sum(1 for item in edge.get("checks", []) if item.get("result") == "FAIL")
    edge_part = f"Edge 门 {edge['verdict']}"
    if fail_count:
        edge_part += f"（{fail_count} 项 FAIL）"

    blocked = list(risk.get("blocked") or [])
    risk_part = f"Risk 门 {risk['decision']}"
    if blocked:
        risk_part += f"（{len(blocked)} 项一票否决）"

    verdict = str(action["action"])
    verdict_label = _ACTION_LABELS.get(verdict, verdict)
    tail = f"最终判定 {verdict}（{verdict_label}）"
    reasons = "; ".join(str(item) for item in (action.get("blocked") or []))
    tail += f"：{reasons}" if reasons else "。"
    return f"{head}：{edge_part}，{risk_part}，{tail}"


def build_decision_card(
    data: dict[str, Any],
    engine: dict[str, Any],
    edge: dict[str, Any],
    risk: dict[str, Any],
    action: dict[str, Any],
    scenario: Mapping[str, Any] | None = None,
    research_evidence: Mapping[str, Any] | None = None,
    macro_assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """阶段 5a：可溯源决策卡。"""

    primary = engine["primary"]
    proposal = engine["proposal"]
    payload = data["payload"]
    earnings = payload["earnings"]
    account = payload["account"]
    spot = data["spot"]
    breakeven_move_pct = abs(primary["strike"] - primary["breakeven_low"]) / spot * 100.0

    key_evidence = [
        {
            "claim": f"市场预期业绩波动 {earnings['expected_move_pct']:.2f}%",
            "source": data["source"],
            "captured_at": data["captured_at"],
        },
        {
            "claim": (
                f"主方案跨式盈亏平衡需要 {breakeven_move_pct:.2f}% 变动"
                f"（预期波动 {earnings['expected_move_pct']:.2f}%，由 Edge 门对比判定）"
            ),
            "source": "self-built engine (BS/二叉树 + IV 二分 + bump-and-reprice)",
            "captured_at": data["captured_at"],
        },
        {
            "claim": f"IV {earnings['iv']:.1f}% / IV Rank {earnings['iv_rank']:.1f} / IV Pct {earnings['iv_percentile']:.1f}",
            "source": data["source"],
            "captured_at": data["captured_at"],
        },
    ]
    applied_dividends = [
        item for item in engine.get("dividends", []) if item.get("applied")
    ]
    if applied_dividends:
        latest = applied_dividends[-1]
        key_evidence.append(
            {
                "claim": (
                    f"已计入港股离散股息 {len(applied_dividends)} 笔"
                    f"（最近除息日 {latest['ex_date']}，每股 {latest['amount']:g} HKD；"
                    "escrowed-spot 口径：S* = S - PV(除息日前现金股息)）"
                ),
                "source": "snapshot model（操作员核验的股息事实）",
                "captured_at": data["captured_at"],
            }
        )

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "underlying": data["underlying"],
        "scenario": {
            "view": str(scenario.get("view", "uncertain")) if scenario else "uncertain",
            "horizon": (
                str(scenario["horizon"])
                if scenario and scenario.get("horizon")
                else f"{earnings['date']} 业绩"
            ),
            "account_cash_hkd": (
                float(scenario["account_cash_hkd"])
                if scenario and scenario.get("account_cash_hkd") is not None
                else account["cash_hkd"]
            ),
            "risk_budget_pct": (
                float(scenario["risk_budget_pct"])
                if scenario and scenario.get("risk_budget_pct") is not None
                else account["risk_budget_pct"]
            ),
            "constraints": (
                list(scenario.get("constraints", []))
                if scenario and isinstance(scenario.get("constraints"), list)
                else []
            ),
        },
        "verdict": action["action"],
        "summary": _summary_text(scenario, edge, risk, action),
        "key_evidence": key_evidence,
        "numbers": {
            "spot": spot,
            "primary_expiry": primary["expiry"],
            "strike": primary["strike"],
            "lots": primary["lots"],
            "cost_per_lot_ask": primary["cost_lot_ask"],
            "cost_per_lot_exec": primary["cost_lot_exec"],
            "max_loss": primary["max_loss_exec"],
            "breakeven": [primary["breakeven_low"], primary["breakeven_high"]],
            "straddle_greeks_per_lot": {
                key: round(value, 4) for key, value in primary["straddle_greeks"].items()
            },
        },
        "edge_gate": {
            "verdict": edge["verdict"],
            "recommendation": edge["recommendation"],
            "checks": edge["checks"],
        },
        "risk_gate": {
            "decision": risk["decision"],
            "blocked": risk["blocked"],
            "findings": risk["findings"],
        },
        "action_gate": {
            "action": action["action"],
            "blocked": action["blocked"],
            "next_step": action["next_step"],
        },
        "conditions_that_change": [
            "正股在业绩日实际波动超过盈亏平衡所需幅度",
            (
                f"切换到 {engine['secondary']['expiry'][5:].replace('-', '/').lstrip('0')} "
                "到期或更低价差结构，成本显著下降"
            ),
            "Live 行情 + 已验证费用/滑点 policy 后 executable 成本变化",
            "历史回测样本更新后 Edge 结论变化",
        ],
        "next_step": action["next_step"],
        "data_evidence": {
            "mode": data["mode"],
            "origin": data["origin"],
            "freshness": data["freshness"],
            "captured_at": data["captured_at"],
            "source": data["source"],
            "snapshot_sha256": data["snapshot_sha256"],
        },
        "research_evidence": (
            research_evidence
            if research_evidence is not None
            else {
                "available": False,
                "note": "未提供研究条目输入，本决策卡不包含投研整理与影响研判段",
            }
        ),
        "macro_assessment": (
            macro_assessment
            if macro_assessment is not None
            else {
                "available": False,
                "note": "未提供政策事件输入，本决策卡不包含宏观研判段",
            }
        ),
        "proposal_ref": {
            "primary_expiry": proposal["primary_expiry"],
            "secondary_expiry": proposal["secondary_expiry"],
            "pnl_at_expiry": proposal["legs"][0]["pnl_at_expiry"],
            "pnl_after_iv_crush": proposal["legs"][0]["pnl_after_iv_crush"],
        },
        "disclaimer": (
            "决策支持/研究用途，非投资建议，不构成任何交易要约；"
            "比赛版本仅支持 Futu 模拟盘且必须由用户独立确认。"
        ),
    }


def audit(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """阶段 5b：JSONL + SHA-256 哈希链审计留痕。"""

    try:
        result = subprocess.run(
            [sys.executable, str(AUDIT_LOG_SCRIPT), "--event", event],
            input=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"audit subprocess timed out after 30s (event={event})") from exc
    return json.loads(result.stdout.decode("utf-8"))


def run_pipeline(
    input_path: str | Path = DEFAULT_INPUT,
    *,
    scenario: Mapping[str, Any] | None = None,
    cost_model: Mapping[str, Any] | None = None,
    research_items_path: str | Path | None = None,
    macro_policy_path: str | Path | None = None,
    macro_policy_id: str | None = None,
    human_confirmed: bool = False,
    audit_enabled: bool = True,
    write_card: bool = True,
    snapshot_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """端到端执行五阶段管线并返回决策卡。

    ``snapshot_data`` 允许调用方传入一个已经加载好的快照字典（LIVE/REPLAY
    皆可），从而复用同一套五阶段管线；未提供时保持原有行为：从
    ``input_path`` 读取冻结快照文件。
    """

    data = dict(snapshot_data) if snapshot_data is not None else load_frozen_snapshot(input_path)
    payload = data["payload"]
    if scenario is None:
        scenario = {
            "underlying": data["underlying"],
            "view": "uncertain",
            "horizon": f"{payload['earnings']['date']} 业绩",
            "account_cash_hkd": payload["account"]["cash_hkd"],
            "risk_budget_pct": payload["account"]["risk_budget_pct"],
            "constraints": [
                f"单笔最多亏损 {float(payload['account']['risk_budget_pct']):g}%"
            ],
        }
    parsed = parse_scenario(scenario)
    if parsed["underlying"] != data["underlying"]:
        raise ValueError("scenario underlying does not match frozen snapshot underlying")

    # 场景中的账户现金与风险上限是研究条件，不应只停留在决策卡文案里。
    # 用浅层副本把它们注入本次计算上下文，保留原始冻结快照和来源证据不变。
    scenario_payload = dict(data["payload"])
    scenario_account = dict(scenario_payload["account"])
    scenario_account["cash_hkd"] = parsed["account_cash_hkd"]
    scenario_account["risk_budget_pct"] = parsed["risk_budget_pct"]
    scenario_payload["account"] = scenario_account
    scenario_data = {**data, "payload": scenario_payload}

    engine = compute_engine(scenario_data, cost_model=cost_model, scenario=parsed)
    edge = edge_gate(engine, scenario_payload["earnings"], scenario_data["spot"])
    risk = risk_gate(engine, scenario_data)
    action = action_gate(scenario_data, edge, risk, human_confirmed=human_confirmed)
    research_evidence = None
    if research_items_path is not None:
        research_evidence = build_research_evidence(
            data["underlying"],
            payload["earnings"],
            DEFAULT_BACKTEST,
            research_items_path,
        )
        research_evidence = {"available": True, **research_evidence}
    macro = None
    if macro_policy_path is not None:
        if research_items_path is None:
            raise ValueError("macro assessment requires --research-items")
        macro = build_macro_assessment(
            payload,
            research_items_path,
            macro_policy_path,
            policy_id=macro_policy_id,
        )
        macro = {"available": True, **macro}
    elif macro_policy_id is not None:
        raise ValueError("--policy-id requires --macro-policy")
    card = build_decision_card(
        scenario_data,
        engine,
        edge,
        risk,
        action,
        scenario=parsed,
        research_evidence=research_evidence,
        macro_assessment=macro,
    )

    data_evidence = {
        "mode": data["mode"],
        "origin": data["origin"],
        "freshness": data["freshness"],
        "captured_at": data["captured_at"],
        "source": data["source"],
        "snapshot_sha256": data["snapshot_sha256"],
    }
    audit_refs = []
    if audit_enabled:
        events = [
            ("scenario_parsed", parsed),
            ("data_loaded", data_evidence),
            ("engine_computed", {
                "primary_expiry": engine["primary"]["expiry"],
                "cost_model": engine["cost_model"],
                "cost_per_lot_exec": engine["primary"]["cost_lot_exec"],
                "max_loss_exec": engine["primary"]["max_loss_exec"],
                "discrete_dividend_count": sum(
                    1 for item in engine.get("dividends", []) if item.get("applied")
                ),
                "straddle_greeks_per_lot": {
                    key: round(value, 4)
                    for key, value in engine["primary"]["straddle_greeks"].items()
                },
            }),
            ("edge_gate", edge),
            ("risk_gate", {"decision": risk["decision"], "blocked": risk["blocked"], "findings": risk["findings"]}),
            ("action_gate", action),
            (
                "research_evidence",
                {
                    "available": research_evidence is not None,
                    "item_count": (
                        research_evidence["digest"]["item_count"]
                        if research_evidence is not None
                        else 0
                    ),
                    "synthetic_only": (
                        research_evidence["digest"]["synthetic_only"]
                        if research_evidence is not None
                        else None
                    ),
                    "stock_verdict": (
                        research_evidence["stock_price_impact"]["verdict"]
                        if research_evidence is not None
                        else None
                    ),
                    "option_verdict": (
                        research_evidence["option_impact"]["verdict"]
                        if research_evidence is not None
                        else None
                    ),
                },
            ),
            (
                "macro_assessment",
                {
                    "available": macro is not None,
                    "sentiment_index": (
                        macro["sentiment"]["index"] if macro is not None else None
                    ),
                    "iv_state": macro["iv_emotion"]["state"] if macro is not None else None,
                    "skew_verdict": (
                        macro["iv_emotion"]["skew_verdict"] if macro is not None else None
                    ),
                    "confidence": (
                        macro["macro_judgment"]["confidence"] if macro is not None else None
                    ),
                },
            ),
            ("decision_card", {
                "underlying": card["underlying"],
                "verdict": card["verdict"],
                "summary": card["summary"],
                "next_step": card["next_step"],
            }),
        ]
        for event, item in events:
            audit_refs.append({"event": event, **audit(event, item)})

    if write_card:
        today = datetime.now(timezone.utc).date().isoformat()
        out_path = OUT_DIR / f"decision_card_{today}.json"
        text = json.dumps(
            {**card, "audit_refs": audit_refs}, ensure_ascii=False, indent=2
        ) + "\n"
        # 原子写：先写唯一临时文件再 os.replace，避免 ThreadingHTTPServer
        # 并发 POST 竞态写坏 decision_card_{today}.json
        fd, temp_name = tempfile.mkstemp(
            dir=OUT_DIR, prefix=".decision_card_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(temp_name, out_path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        card["output_path"] = str(out_path)
    card["audit_refs"] = audit_refs
    return card


def _fmt_hkd(value: float) -> str:
    return f"{value:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI 期权端到端决策管线")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="冻结快照 JSON 路径")
    parser.add_argument(
        "--research-items",
        default=None,
        help="研究条目 JSON 路径（可选，启用投研证据段）",
    )
    parser.add_argument(
        "--macro-policy",
        default=None,
        help=(
            "政策事件 JSON 路径或 policy_events 目录（可选，需同时提供 "
            "--research-items，启用宏观研判段）"
        ),
    )
    parser.add_argument(
        "--policy-id",
        default=None,
        help="从政策事件库选择事件 id（缺省取 ACTIVE 中 date 最新者）",
    )
    parser.add_argument("--no-audit", action="store_true", help="跳过审计留痕")
    args = parser.parse_args()

    card = run_pipeline(
        args.input,
        research_items_path=args.research_items,
        macro_policy_path=args.macro_policy,
        macro_policy_id=args.policy_id,
        audit_enabled=not args.no_audit,
    )
    primary = card["numbers"]
    print("=" * 78)
    print(f"GOAI 决策卡 | {card['underlying']} | 结论：{card['verdict']}")
    print(f"快照：{card['data_evidence']['captured_at']} | {card['data_evidence']['source']}")
    print(f"SHA-256：{card['data_evidence']['snapshot_sha256'][:20]}...")
    print("=" * 78)
    print(card["summary"])
    print(
        f"\n主方案：{primary['primary_expiry']} {primary['strike']:g} ATM 跨式 x "
        f"{primary['lots']} 张"
    )
    print(f"  成本 ask {_fmt_hkd(primary['cost_per_lot_ask'])} / executable {_fmt_hkd(primary['cost_per_lot_exec'])} HKD 每张")
    print(f"  最大亏损 {_fmt_hkd(primary['max_loss'])} HKD | 盈亏平衡 {primary['breakeven'][0]:.2f} / {primary['breakeven'][1]:.2f}")
    print("\nEdge 门：")
    for item in card["edge_gate"]["checks"]:
        print(f"  [{item['result']}] {item['check']}：{item['detail']}")
    print("\nRisk 门：")
    for item in card["risk_gate"]["findings"]:
        print(f"  {item}")
    print(f"\nAction 门：{card['action_gate']['action']} -> {card['action_gate']['next_step']}")
    if card["research_evidence"].get("available"):
        digest = card["research_evidence"]["digest"]
        print(
            f"\n投研证据：{digest['item_count']} 条（synthetic={digest['synthetic_only']}）| "
            f"股价 {card['research_evidence']['stock_price_impact']['verdict']} | "
            f"期权 {card['research_evidence']['option_impact']['verdict']}"
        )
    if card["macro_assessment"].get("available"):
        macro = card["macro_assessment"]
        policy = macro["policy_analysis"]
        library = policy.get("library")
        if library is not None:
            verification = library["health_report"]["verification"]
            policy_label = (
                f"政策事件库 {library['event_count']} 个 | "
                f"主事件 {policy['event_id']} | VERIFIED "
                f"{verification['VERIFIED']}/PENDING {verification['PENDING']}"
            )
        else:
            policy_label = f"政策事件 {policy['event_name']}（{policy['event_id']}）"
        print(
            f"\n宏观研判：情绪 {macro['sentiment']['verdict']}（{macro['sentiment']['index']}）| "
            f"IV {macro['iv_emotion']['state']} | {policy_label} | "
            f"主要矛盾 {policy['principal_contradiction']['pair']}"
        )
        if library is not None:
            promoted = library["health_report"].get("recently_promoted", [])
            if promoted:
                latest = promoted[0]
                print(
                    f"  最近激活：{len(promoted)} 个事件，最新 {latest['id']} "
                    f"（{latest['promoted_at']}，{latest['promoted_by'] or 'manual-review'}）"
                )
    print(f"\n决策卡 JSON：{card.get('output_path')}")
    print("免责声明：决策支持/研究用途，非投资建议；默认模拟盘，任何订单须人机确认。")


if __name__ == "__main__":
    main()
