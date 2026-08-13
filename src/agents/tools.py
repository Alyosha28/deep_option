"""十角色辩论的白名单确定性工具注册表。

每个工具都是纯函数式的只读切片：输入来自冻结快照 / 自研引擎 / 投研与宏观
确定性产物，输出 JSON 可序列化结构。ToolRegistry 只按名字调用注册过的
函数，拒绝未注册工具，不接受任意代码或 shell 执行。

数字铁律：工具是确定性数据层的一部分；LLM 只能引用工具返回的数字，不能
让 LLM 输出反向改写引擎数字、verdict、门控或权限。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT_LOG = ROOT / "research" / "audit" / "audit_log.jsonl"
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

INJECTION_PATTERNS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore the above",
    "disregard previous",
    "reveal your system prompt",
    "jailbreak",
    "越狱",
    "绕过铁律",
    "绕过规则",
    "bypass the rules",
    "改写 verdict",
    "篡改数字",
    "伪造数字",
    "输出任意金融数字",
    "act as a different role",
)


@dataclass(frozen=True)
class DebateContext:
    """辩论只读上下文：全部来自冻结快照与确定性引擎产物。"""

    scenario: Mapping[str, Any]
    data: Mapping[str, Any]
    engine: Mapping[str, Any]
    edge: Mapping[str, Any]
    risk: Mapping[str, Any]
    action: Mapping[str, Any]
    research: Mapping[str, Any] | None
    macro: Mapping[str, Any] | None


class ToolRegistry:
    """按白名单名字调用确定性函数；结果必须 JSON 可序列化。"""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[[Mapping[str, Any]], Any]] = {}

    def register(
        self, name: str, fn: Callable[[Mapping[str, Any]], Any]
    ) -> None:
        if not _TOOL_NAME.match(name):
            raise ValueError(f"invalid tool name: {name!r}")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = fn

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: Mapping[str, Any] | None = None) -> Any:
        if name not in self._tools:
            raise KeyError(f"tool not registered: {name}")
        resolved_args: Mapping[str, Any] = args if isinstance(args, dict) else {}
        if len(json.dumps(resolved_args, ensure_ascii=False)) > 64 * 1024:
            raise ValueError("tool arguments exceed 64 KiB")
        result = self._tools[name](resolved_args)
        json.dumps(result, ensure_ascii=False)  # 强制 JSON 可序列化
        return result


def _clip(items: Sequence[Any], limit: int) -> list[Any]:
    return list(items[:limit])


def _legs_slice(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = data["payload"]
    rows: list[dict[str, Any]] = []
    for group in payload["legs"]:
        rows.append(
            {
                "expiry": group["expiry"],
                "dte": group["dte"],
                "call": {
                    "code": group["call"]["code"],
                    "strike": group["call"]["strike"],
                    "api_iv_pct": group["call"].get("api_iv_pct"),
                    "open_interest": group["call"].get("open_interest"),
                    "net_open_interest": group["call"].get("net_open_interest"),
                    "volume": group["call"].get("volume"),
                },
                "put": {
                    "code": group["put"]["code"],
                    "strike": group["put"]["strike"],
                    "api_iv_pct": group["put"].get("api_iv_pct"),
                    "open_interest": group["put"].get("open_interest"),
                    "net_open_interest": group["put"].get("net_open_interest"),
                    "volume": group["put"].get("volume"),
                },
            }
        )
    return rows


def _snapshot_summary(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    data = context.data
    payload = data["payload"]
    earnings = payload["earnings"]
    account = payload["account"]
    return {
        "mode": data["mode"],
        "origin": data["origin"],
        "freshness": data["freshness"],
        "captured_at": data["captured_at"],
        "source": data["source"],
        "market_state": payload.get("market_state", ""),
        "snapshot_sha256": data["snapshot_sha256"],
        "underlying": data["underlying"],
        "spot": data["spot"],
        "prev_close": payload.get("prev_close"),
        "earnings": {
            "date": earnings.get("date"),
            "quarter": earnings.get("quarter"),
            "expected_move_pct": earnings.get("expected_move_pct"),
            "iv": earnings.get("iv"),
            "iv_rank": earnings.get("iv_rank"),
            "iv_percentile": earnings.get("iv_percentile"),
            "hv_30d": earnings.get("hv_30d"),
            "last_report_iv_crush": earnings.get("last_report_iv_crush"),
            "history_report_iv_crush": earnings.get("history_report_iv_crush"),
            "estimate_eps_yoy": earnings.get("estimate_eps_yoy"),
            "estimate_revenue_yoy": earnings.get("estimate_revenue_yoy"),
        },
        "account": {
            "cash_hkd": account.get("cash_hkd"),
            "risk_budget_pct": account.get("risk_budget_pct"),
            "contract_multiplier": account.get("contract_multiplier"),
        },
        "note": "全部数字来自冻结 Futu 快照与自研引擎；LLM 不得补造或改写。",
    }


def _research_items(
    context: DebateContext, kinds: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    if context.research is None:
        return []
    digest = context.research.get("digest")
    if not isinstance(digest, dict):
        return []
    selected = kinds if kinds is not None else ()
    rows = []
    for item in digest.get("items", []):
        if selected and item.get("kind") not in selected:
            continue
        rows.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "source": item.get("source"),
                "sentiment": item.get("sentiment"),
                "relevant_to_options": item.get("relevant_to_options"),
                "summary": item.get("summary"),
            }
        )
    return rows


def _news_digest(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    if context.research is None:
        return {
            "available": False,
            "note": "未提供投研条目输入，新闻公告切片不可用。",
        }
    digest = context.research["digest"]
    return {
        "available": True,
        "synthetic_only": digest.get("synthetic_only"),
        "items": _clip(
            _research_items(context, ("earnings", "announcement", "news")), 20
        ),
        "stock_price_impact": {
            "verdict": context.research["stock_price_impact"].get("verdict"),
            "confidence": context.research["stock_price_impact"].get("confidence"),
            "counts": context.research["stock_price_impact"].get("counts"),
        },
        "option_impact": {
            "verdict": context.research["option_impact"].get("verdict"),
            "iv_crush_risk": context.research["option_impact"].get("iv_crush_risk"),
            "checks": context.research["option_impact"].get("checks", []),
        },
    }


def _report_comparison(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    if context.research is None:
        return {
            "available": False,
            "note": "未提供研报/行业条目输入，研报对比不可用。",
        }
    digest = context.research["digest"]
    research_rows = _research_items(context, ("research",))
    industry_rows = _research_items(context, ("industry",))
    news_sentiment = context.research.get("stock_price_impact", {}).get("counts", {})
    return {
        "available": True,
        "by_kind": digest.get("by_kind"),
        "research_items": _clip(research_rows, 12),
        "industry_items": _clip(industry_rows, 12),
        "news_sentiment_counts": news_sentiment,
        "stock_impact_verdict": context.research["stock_price_impact"].get("verdict"),
        "note": "研报与新闻共识的对比为文字分析输入；深度估值引擎留待后续阶段接入。",
    }


def _macro_policy(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    if context.macro is None:
        return {
            "available": False,
            "note": "未提供政策事件输入，宏观研判切片不可用。",
        }
    macro = context.macro
    policy = macro["policy_analysis"]
    library = policy.get("library") or {}
    return {
        "available": True,
        "sentiment": {
            "index": macro["sentiment"].get("index"),
            "verdict": macro["sentiment"].get("verdict"),
            "dispersion": macro["sentiment"].get("dispersion"),
        },
        "iv_emotion": {
            "state": macro["iv_emotion"].get("state"),
            "iv": macro["iv_emotion"].get("iv"),
            "iv_rank": macro["iv_emotion"].get("iv_rank"),
            "iv_hv_premium_pp": macro["iv_emotion"].get("iv_hv_premium_pp"),
            "skew_verdict": macro["iv_emotion"].get("skew_verdict"),
            "event_days_until": macro["iv_emotion"].get("event_days_until"),
        },
        "policy_analysis": {
            "event_id": policy.get("event_id"),
            "event_name": policy.get("event_name"),
            "event_date": policy.get("event_date"),
            "principal_contradiction": policy.get("principal_contradiction"),
            "game_rounds": _clip(policy.get("game_rounds", []), 6),
            "signals": policy.get("signals"),
            "verdict_reads": policy.get("verdict_reads", []),
            "political_economy": {
                "summary": policy.get("political_economy", {}).get("summary")
            },
            "qiu_shi": {
                "facts_first": policy.get("qiu_shi", {}).get("facts_first"),
                "falsification": _clip(policy.get("qiu_shi", {}).get("falsification", []), 6),
                "monitor": _clip(policy.get("qiu_shi", {}).get("monitor", []), 8),
                "unknowns": _clip(policy.get("qiu_shi", {}).get("unknowns", []), 8),
            },
            "library": {
                "event_count": library.get("event_count"),
                "selection": library.get("selection"),
                "verification": (library.get("health_report") or {}).get(
                    "verification"
                ),
            },
        },
        "macro_judgment": {
            "mood": macro["macro_judgment"].get("mood"),
            "scenarios": macro["macro_judgment"].get("scenarios", []),
            "confidence": macro["macro_judgment"].get("confidence"),
            "contrarian_note": macro["macro_judgment"].get("contrarian_note"),
        },
    }


def _sentiment_iv(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    if context.macro is None:
        return {
            "available": False,
            "note": "未提供宏观研判输入，情绪/IV 晴雨表不可用。",
        }
    macro = context.macro
    return {
        "available": True,
        "sentiment": {
            "index": macro["sentiment"].get("index"),
            "verdict": macro["sentiment"].get("verdict"),
            "dispersion": macro["sentiment"].get("dispersion"),
            "counts": macro["sentiment"].get("counts"),
        },
        "iv_emotion": {
            "state": macro["iv_emotion"].get("state"),
            "iv": macro["iv_emotion"].get("iv"),
            "iv_rank": macro["iv_emotion"].get("iv_rank"),
            "iv_hv_premium_pp": macro["iv_emotion"].get("iv_hv_premium_pp"),
            "skew_verdict": macro["iv_emotion"].get("skew_verdict"),
            "skew_primary_pp": macro["iv_emotion"].get("skew_primary_pp"),
            "event_days_until": macro["iv_emotion"].get("event_days_until"),
            "mechanisms": macro["iv_emotion"].get("mechanisms", []),
        },
    }


def _technical_flow(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    data = context.data
    payload = data["payload"]
    legs: list[dict[str, Any]] = []
    for group in payload["legs"]:
        call = group["call"]
        put = group["put"]
        legs.append(
            {
                "expiry": group["expiry"],
                "dte": group["dte"],
                "call": {
                    "code": call["code"],
                    "open_interest": call.get("open_interest"),
                    "net_open_interest": call.get("net_open_interest"),
                    "volume": call.get("volume"),
                    "bid_ask_spread_pct": _spread_pct(call),
                },
                "put": {
                    "code": put["code"],
                    "open_interest": put.get("open_interest"),
                    "net_open_interest": put.get("net_open_interest"),
                    "volume": put.get("volume"),
                    "bid_ask_spread_pct": _spread_pct(put),
                },
            }
        )
    return {
        "availability": "frozen_slice",
        "spot": data["spot"],
        "prev_close": payload.get("prev_close"),
        "iv": payload["earnings"].get("iv"),
        "iv_rank": payload["earnings"].get("iv_rank"),
        "iv_percentile": payload["earnings"].get("iv_percentile"),
        "hv_30d": payload["earnings"].get("hv_30d"),
        "legs": legs,
        "note": (
            "当前仅覆盖冻结快照切片；实时资金流/买卖经纪商/卖空异动接口"
            "（futu 异动技能）留待 P0b 接入，未在此虚构。"
        ),
    }


def _spread_pct(leg: Mapping[str, Any]) -> float | None:
    mid = leg.get("mid")
    bid = leg.get("bid")
    ask = leg.get("ask")
    if (
        isinstance(mid, (int, float))
        and isinstance(bid, (int, float))
        and isinstance(ask, (int, float))
        and mid > 0
    ):
        return round((ask - bid) / mid * 100.0, 2)
    return None


def _option_chain(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    data = context.data
    engine = context.engine
    proposal = engine["proposal"]

    def expiry_slice(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "expiry": item["expiry"],
            "dte": item["dte"],
            "strike": item["strike"],
            "lots": item["lots"],
            "cost_per_lot_ask": item.get("cost_lot_ask"),
            "cost_per_lot_exec": item.get("cost_lot_exec"),
            "max_loss_exec": item.get("max_loss_exec"),
            "breakeven": [item.get("breakeven_low"), item.get("breakeven_high")],
            "straddle_greeks": item.get("straddle_greeks"),
            "call": {
                "code": item["call"]["code"],
                "iv_solved_pct": round(item["call"]["iv"] * 100.0, 4),
            },
            "put": {
                "code": item["put"]["code"],
                "iv_solved_pct": round(item["put"]["iv"] * 100.0, 4),
            },
        }

    return {
        "underlying": data["underlying"],
        "spot": data["spot"],
        "primary": expiry_slice(engine["primary"]),
        "secondary": expiry_slice(engine["secondary"]),
        "pnl_at_expiry": proposal["legs"][0].get("pnl_at_expiry"),
        "pnl_after_iv_crush": proposal["legs"][0].get("pnl_after_iv_crush"),
        "cost_model": engine.get("cost_model"),
        "note": "数字来自自研引擎（BS/二叉树 + IV 二分 + bump-and-reprice）。",
    }


def _risk_gate(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "risk": {
            "decision": context.risk["decision"],
            "blocked": context.risk["blocked"],
            "findings": context.risk["findings"],
        },
        "edge": {
            "verdict": context.edge["verdict"],
            "recommendation": context.edge["recommendation"],
            "checks": context.edge["checks"],
        },
        "action": {
            "action": context.action["action"],
            "blocked": context.action["blocked"],
            "next_step": context.action["next_step"],
        },
        "account": {
            "cash_hkd": context.data["payload"]["account"].get("cash_hkd"),
            "risk_budget_pct": context.data["payload"]["account"].get(
                "risk_budget_pct"
            ),
            "contract_multiplier": context.data["payload"]["account"].get(
                "contract_multiplier"
            ),
        },
        "note": "风控门一票否决；门结果由确定性引擎计算，LLM 不得修改。",
    }


def _audit_chain_summary() -> dict[str, Any]:
    log_path = AUDIT_LOG
    if not log_path.is_file():
        return {
            "available": False,
            "records": 0,
            "last_hash": None,
            "note": "本地审计日志尚未生成（research/audit/audit_log.jsonl 不入库）。",
        }
    records = 0
    last_hash = ""
    try:
        for line in log_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            records += 1
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("hash"):
                last_hash = str(payload["hash"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "available": False,
            "records": 0,
            "last_hash": None,
            "note": "审计日志读取失败，未伪造健康状态。",
        }
    return {
        "available": True,
        "records": records,
        "last_hash": last_hash,
        "note": "JSONL + SHA-256 哈希链，只提供篡改迹象检测，不替代签名与外部锚定。",
    }


def _audit_health(
    context: DebateContext, _args: Mapping[str, Any]
) -> dict[str, Any]:
    verification: dict[str, Any] | None = None
    library_status = None
    if context.macro is not None:
        library = context.macro["policy_analysis"].get("library") or {}
        health = library.get("health_report") or {}
        verification = health.get("verification")
        library_status = health.get("library_status")
    return {
        "snapshot_mode": context.data["mode"],
        "snapshot_sha256": context.data["snapshot_sha256"],
        "verification": verification,
        "library_status": library_status,
        "audit_chain": _audit_chain_summary(),
        "injection_policy": (
            "LLM 输出仅文本与证据引用；数字/verdict/权限与门控由自研引擎维护，"
            "不可被外部文本改写。"
        ),
    }


def _injection_check(
    _context: DebateContext, args: Mapping[str, Any]
) -> dict[str, Any]:
    texts = args.get("texts")
    if not isinstance(texts, list):
        return {"verdict": "safe", "hits": [], "note": "无待检文本。"}
    hits: list[dict[str, Any]] = []
    for index, value in enumerate(texts):
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        matched = [
            pattern
            for pattern in INJECTION_PATTERNS
            if pattern.lower() in lowered
        ]
        if matched:
            hits.append({"index": index, "patterns": matched})
    return {
        "verdict": "unsafe" if hits else "safe",
        "hits": hits,
        "note": "确定性模式扫描是辅助信号；审计官仍需结合上下文复核，未发现不代表安全。",
    }


def build_allowed_refs(context: DebateContext) -> set[str]:
    """辩论输出可引用的证据 id 白名单（防编造来源）。"""

    refs = {
        "frozen_snapshot",
        "snapshot_sha256",
        "self_built_engine",
        "backtest_summary",
        "risk_gate",
        "edge_gate",
        "action_gate",
        "audit_chain",
        "macro_assessment",
        "sentiment_index",
        "iv_emotion",
        "policy_library",
        "library_health",
        "injection_check",
    }
    if context.research is not None:
        for item in context.research.get("digest", {}).get("items", []):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                refs.add(item_id)
    if context.macro is not None:
        policy = context.macro.get("policy_analysis", {})
        for event in (policy.get("library") or {}).get("event_ids", []):
            if isinstance(event, str) and event:
                refs.add(f"policy_event:{event}")
        primary = policy.get("event_id")
        if isinstance(primary, str) and primary:
            refs.add(f"policy_event:{primary}")
    for group in context.data["payload"].get("legs", []):
        for side in ("call", "put"):
            code = group.get(side, {}).get("code")
            if isinstance(code, str) and code:
                refs.add(code)
    return refs


def build_default_registry(context: DebateContext) -> ToolRegistry:
    """把只读确定性工具绑定到当前辩论上下文。"""

    registry = ToolRegistry()
    registry.register("snapshot_summary", lambda args: _snapshot_summary(context, args))
    registry.register("news_digest", lambda args: _news_digest(context, args))
    registry.register(
        "report_comparison", lambda args: _report_comparison(context, args)
    )
    registry.register("macro_policy", lambda args: _macro_policy(context, args))
    registry.register("sentiment_iv", lambda args: _sentiment_iv(context, args))
    registry.register("technical_flow", lambda args: _technical_flow(context, args))
    registry.register("option_chain", lambda args: _option_chain(context, args))
    registry.register("risk_gate", lambda args: _risk_gate(context, args))
    registry.register("audit_health", lambda args: _audit_health(context, args))
    registry.register("injection_check", lambda args: _injection_check(context, args))
    return registry


__all__ = [
    "DebateContext",
    "ToolRegistry",
    "build_allowed_refs",
    "build_default_registry",
]
