"""GOAI 期权智能终端 - 宏观研判层。

把“情绪量化 + IV 情绪晴雨表 + 政策/消息博弈 + 政治经济学 + 求是检验”
落成可运行、可审计的分析模块。

方法论（参考 qiushi-skill · 矛盾分析法 + 实事求是）：
- 先立事实，再下判断；事实不清时显式标注未知；
- 识别矛盾并区分主要矛盾/次要矛盾、对抗性/非对抗性；
- 分析谁获利、谁吃亏、吃亏方是否允许，判断政策可落地性；
- 每个判断附带证伪条件与监控点，允许被事实推翻。

铁律：
- 本模块不产生确定性概率和投资建议；可能性只用定性级别（HIGH/MEDIUM/LOW）；
- 案例研究数据（case_study=true）用于演示框架，不代表实时事实；
- IV 只度量“震幅”而非方向；Skew 仅作方向性参考，不作预测。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.research_evidence import (
    classify_item,
    load_research_items,
)
from src.payload_validation import reject_sensitive_fields
from src.policy_library import (
    DEFAULT_POLICY_DIR,
    event_verification_summary,
    load_policy_library,
    policy_health_report,
    select_policy_events,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = DEFAULT_POLICY_DIR / "us-cn-tariff-2025-04.json"
DEFAULT_ITEMS = ROOT / "data" / "research_items_hero.json"
DEFAULT_SNAPSHOT = ROOT / "data" / "hero_inputs.json"
OUT_DIR = ROOT / "data"
AUDIT_LOG_SCRIPT = (
    ROOT / ".agents" / "skills" / "futu-options-agent" / "scripts" / "audit_log.py"
)

MAX_FILE_BYTES = 8 * 1024 * 1024
_REVERSAL_HINTS = ("不可持续", "暂停", "反制", "成本显性")
_PERSISTENCE_HINTS = ("维持高位", "战略", "韧性")
_ESCALATION_HINTS = ("升级", "螺旋", "失控")


def _reject_sensitive(raw: Mapping[str, Any]) -> None:
    # 敏感键拒绝逻辑收敛到 payload_validation.reject_sensitive_fields
    reject_sensitive_fields(raw, label="policy event")


def _require_text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"policy event field {key!r} is invalid")
    return value.strip()


def _parse_date(text: str) -> datetime | None:
    cleaned = text.strip().replace("T", " ").split(".")[0]
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def quantify_sentiment(items: list[dict[str, Any]]) -> dict[str, Any]:
    """情绪量化：极性 + 强度 + 时效加权 -> 情绪指数（-100..100）。"""

    if not items:
        return {
            "index": None,
            "verdict": "UNKNOWN",
            "counts": {"bullish": 0, "bearish": 0, "mixed": 0, "neutral": 0},
            "per_item": [],
            "note": "无研究条目输入，无法量化情绪",
        }

    now = datetime.now(timezone.utc)
    scores: list[float] = []
    weights: list[float] = []
    polarities: list[float] = []
    per_item: list[dict[str, Any]] = []
    counts = {"bullish": 0, "bearish": 0, "mixed": 0, "neutral": 0}
    for item in items:
        classification = classify_item(item)
        counts[classification["sentiment"]] = counts.get(classification["sentiment"], 0) + 1
        positive = len(classification["positive_hits"])
        negative = len(classification["negative_hits"])
        polarity = (positive - negative) / (positive + negative + 1)
        intensity = min(1.0, (positive + negative) / 3.0)
        published = _parse_date(str(item.get("published_at", "")))
        recency_weight = (
            math.exp(-max((now - published.replace(tzinfo=timezone.utc)).days, 0) / 30.0)
            if published is not None
            else 1.0
        )
        weight = max(recency_weight, 0.05)
        scores.append(polarity * intensity * weight)
        weights.append(weight)
        polarities.append(polarity)
        per_item.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "sentiment": classification["sentiment"],
                "polarity": round(polarity, 3),
                "intensity": round(intensity, 3),
                "recency_weight": round(recency_weight, 3),
                "score": round(polarity * intensity * weight, 4),
            }
        )

    total_weight = sum(weights)
    index = round(sum(score for score in scores) / total_weight * 100.0, 1) if total_weight > 0 else 0.0
    dispersion = round(statistics.pstdev(polarities), 3) if len(polarities) >= 2 else 0.0
    if index >= 20:
        verdict = "BULLISH"
    elif index <= -20:
        verdict = "BEARISH"
    else:
        verdict = "NEUTRAL"
    return {
        "index": index,
        "verdict": verdict,
        "dispersion": dispersion,
        "counts": counts,
        "per_item": per_item,
        "note": "情绪指数为消息面规则化量化的参考，不代表市场整体情绪的唯一度量",
    }


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return float(value)


def assess_iv_emotion(
    earnings: Mapping[str, Any],
    legs: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """IV 情绪晴雨表：水位、IV/HV 溢价、同执行价 put/call IV 差、事件临近。"""

    iv = _num(earnings.get("iv"))
    iv_rank = _num(earnings.get("iv_rank"))
    hv = _num(earnings.get("hv_30d"))
    last_crush = _num(earnings.get("last_report_iv_crush"))
    history_crush = _num(earnings.get("history_report_iv_crush"))
    premium = iv - hv if iv is not None and hv is not None else None

    if iv_rank is not None and iv_rank >= 70:
        state = "HIGH"
    elif premium is not None and premium >= 15:
        state = "HIGH"
    elif iv_rank is not None and iv_rank >= 30:
        state = "ELEVATED"
    elif premium is not None and premium >= 5:
        state = "ELEVATED"
    elif iv_rank is not None or premium is not None:
        state = "CALM"
    else:
        state = "UNKNOWN"

    skews: list[dict[str, Any]] = []
    for group in legs:
        expiry = str(group.get("expiry", ""))
        call_iv = _num(group.get("call", {}).get("api_iv_pct"))
        put_iv = _num(group.get("put", {}).get("api_iv_pct"))
        if call_iv is not None and put_iv is not None:
            skews.append({"expiry": expiry, "put_minus_call_pp": round(put_iv - call_iv, 2)})
    primary_skew = skews[0]["put_minus_call_pp"] if skews else None
    if primary_skew is None:
        skew_verdict = "UNKNOWN"
    elif primary_skew >= 3:
        skew_verdict = "PUT_BIAS"
    elif primary_skew <= -3:
        skew_verdict = "CALL_BIAS"
    else:
        skew_verdict = "NEUTRAL"

    event_days = None
    event_date = _parse_date(str(earnings.get("date", "")))
    if event_date is not None:
        event_days = (event_date.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days

    mechanisms = [
        {
            "name": "预期性消息提前计入",
            "observation": (
                f"IV {iv:.1f}%" if iv is not None else "IV 缺失"
            ) + (
                f" / IV Rank {iv_rank:.1f}" if iv_rank is not None else ""
            ),
            "reading": "事件临近时 IV 上升属正常风险定价，不直接代表方向",
        },
        {
            "name": "落地后 IV Crush",
            "observation": (
                f"最近一期 crush 参考 {last_crush:.2f}pp / 历史 {history_crush:.2f}pp"
                if last_crush is not None or history_crush is not None
                else "crush 参考缺失"
            ),
            "reading": "消息落地后悬念消除，IV 存在回归压力",
        },
        {
            "name": "突发黑天鹅",
            "observation": f"当前 IV 状态：{state}",
            "reading": "恐慌性买 Put 会推高整体 IV；本指标只反映不确定性程度",
        },
        {
            "name": "IV 是震幅而非方向",
            "observation": (
                f"IV/HV30 溢价 {premium:+.1f}pp" if premium is not None else "IV/HV 溢价缺失"
            ),
            "reading": "高 IV 不等于看跌，也不等于看涨",
        },
        {
            "name": "Skew 方向性参考",
            "observation": (
                f"主到期 put-call IV {primary_skew:+.2f}pp -> {skew_verdict}"
                if primary_skew is not None
                else "skew 数据缺失"
            ),
            "reading": "Put IV 异常高于 Call IV 时市场偏防御；反之偏进攻（参考口径）",
        },
    ]

    return {
        "state": state,
        "iv": iv,
        "iv_rank": iv_rank,
        "iv_hv_premium_pp": round(premium, 2) if premium is not None else None,
        "skew_verdict": skew_verdict,
        "skew_primary_pp": primary_skew,
        "skews_by_expiry": skews,
        "event_days_until": event_days,
        "mechanisms": mechanisms,
    }


def load_policy_event(path: str | Path = DEFAULT_POLICY) -> dict[str, Any]:
    """读取并校验政策事件（含博弈矩阵与政治经济学素材）。"""

    event_path = Path(path)
    try:
        raw_bytes = event_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read policy event {event_path}") from exc
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise ValueError("policy event file exceeds the 8 MiB limit")
    try:
        event = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("policy event file is not valid UTF-8 JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("policy event must be a JSON object")
    _reject_sensitive(event)

    for key in ("id", "name", "date", "type", "description"):
        _require_text(event, key)
    for key in ("facts", "stakeholders", "tensions", "verdict_reads"):
        if not isinstance(event.get(key), list) or not event[key]:
            raise ValueError(f"policy event {key!r} must be a non-empty list")
    for index, fact in enumerate(event["facts"]):
        if not isinstance(fact, dict):
            raise ValueError(f"policy event fact {index} must be an object")
        for key in ("id", "date", "fact", "source"):
            _require_text(fact, key)
    for index, tension in enumerate(event["tensions"]):
        if not isinstance(tension, dict):
            raise ValueError(f"policy event tension {index} must be an object")
        for key in ("id", "left", "right", "nature"):
            _require_text(tension, key)
        if not isinstance(tension.get("principal"), bool):
            raise ValueError(f"policy event tension {index} principal must be a boolean")
    principal_count = sum(1 for tension in event["tensions"] if tension.get("principal"))
    if principal_count != 1:
        raise ValueError("policy event must have exactly one principal tension")
    for index, read in enumerate(event["verdict_reads"]):
        if not isinstance(read, dict):
            raise ValueError(f"policy event verdict_reads {index} must be an object")
        for key in ("scope", "verdict", "reason"):
            _require_text(read, key)
        if not isinstance(read.get("evidence_fact_ids"), list):
            raise ValueError(f"policy event verdict_reads {index} evidence_fact_ids must be a list")
    return event


def analyze_policy_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """政策博弈 + 政治经济学 + 求是检验。"""

    facts = list(event.get("facts", []))
    unsourced = [fact["id"] for fact in facts if not str(fact.get("source", "")).strip()]
    verification = event_verification_summary(event)["status"]
    lessons = " ".join(
        str(round_item.get("lesson", "")) for round_item in event.get("game_rounds", [])
    )
    signals = {
        "reversal_pressure": any(hint in lessons for hint in _REVERSAL_HINTS),
        "strategic_persistence": any(hint in lessons for hint in _PERSISTENCE_HINTS),
        "escalation_risk": any(hint in lessons for hint in _ESCALATION_HINTS),
    }

    contradictions = [
        {
            "id": tension["id"],
            "pair": f"{tension['left']} vs {tension['right']}",
            "nature": tension["nature"],
            "principal": tension.get("principal", False),
            "note": tension.get("note", ""),
        }
        for tension in event.get("tensions", [])
    ]
    political_economy = [
        {
            "check": item["check"],
            "result": item["result"],
            "detail": item.get("detail", ""),
        }
        for item in event.get("political_economy", [])
    ]
    assumptions = [
        {
            "factor": item["factor"],
            "observation": item["observation"],
            "needs_verification": True,
            "source": item.get("source", "需核验"),
        }
        for item in event.get("macro_fundamentals", [])
    ]

    return {
        "event_id": event["id"],
        "event_name": event["name"],
        "event_date": event["date"],
        "event_type": event["type"],
        "case_study": bool(event.get("case_study", False)),
        "facts": {
            "total": len(facts),
            "sourced_count": len(facts) - len(unsourced),
            "unsourced_ids": unsourced,
            "items": facts,
        },
        "contradictions": contradictions,
        "principal_contradiction": next(
            item for item in contradictions if item["principal"]
        ),
        "game_rounds": list(event.get("game_rounds", [])),
        "signals": signals,
        "verdict_reads": list(event.get("verdict_reads", [])),
        "political_economy": {
            "checks": political_economy,
            "summary": (
                "政策是否可持续，取决于吃亏方的成本显性化速度与反制能力；"
                "主要矛盾决定政策底线，次要矛盾决定边际调整。"
            ),
        },
        "qiu_shi": {
            "facts_first": {
                "status": (
                    "OK"
                    if verification == "VERIFIED"
                    else "FAILED" if verification == "FAILED" else "VERIFYING"
                ),
                "sourced_count": len(facts) - len(unsourced),
                "unsourced_ids": unsourced,
                "verification": verification,
            },
            "assumptions": assumptions,
            "falsification": list(event.get("falsification", [])),
            "monitor": list(event.get("monitor", [])),
            "unknowns": [
                "政策内部决策依据不可直接观测",
                "来源核验状态以事件文件 verification 与健康报告为准；"
                "PENDING/未核验条目正式使用前需逐条复核",
            ],
        },
    }


def _analyze_policy_input(
    policy_path: str | Path,
    policy_id: str | None,
) -> dict[str, Any]:
    """加载政策输入：单文件保持兼容；目录走事件库（健康报告 + 附加事件）。"""

    policy_path = Path(policy_path)
    if policy_path.is_dir():
        library = load_policy_library(policy_path)
        primary, others, selection = select_policy_events(library, policy_id)
        policy = analyze_policy_event(primary)
        policy["library"] = {
            "mode": "library",
            "event_count": library["event_count"],
            "event_ids": library["event_ids"],
            "selection": selection,
            "primary_event_id": primary["id"],
            "health_report": policy_health_report(library),
            "additional_events": [
                {
                    "event_id": event["id"],
                    "event_name": event["name"],
                    "event_date": event["date"],
                    "event_type": event["type"],
                    "status": event["status"],
                    "verification": event_verification_summary(event),
                    "verdict_reads": event.get("verdict_reads", []),
                }
                for event in others
            ],
        }
        return policy
    return analyze_policy_event(load_policy_event(policy_path))


def _scenarios(
    sentiment: Mapping[str, Any],
    iv: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    reads = {item["scope"]: item["verdict"] for item in policy.get("verdict_reads", [])}
    global_read = next((v for k, v in reads.items() if "全球" in k), "UNKNOWN")
    core_read = next(
        (v for k, v in reads.items() if "中美" in k or "战略" in k),
        "UNKNOWN",
    )
    scenarios = [
        {
            "name": "政策转向/降级",
            "conditions": [
                "非对抗性矛盾（国内成本、企业与通胀）上升为主要矛盾",
                "谈判取得实质进展，关税边际下调",
            ],
            "likelihood_level": "HIGH" if global_read == "LIKELY_UNSUSTAINABLE" else "MEDIUM",
            "market_implication": "恐慌溢价回落，风险资产修复，出口链估值修复",
            "option_implication": "IV crush 概率上升，Put 防御溢价回落，Skew 回归中性",
        },
        {
            "name": "战略关税长期化",
            "conditions": [
                "主要矛盾（战略竞争）继续主导",
                "对华高关税与谈判并行，无实质降级",
            ],
            "likelihood_level": (
                "HIGH" if core_read == "CREDIBLE_IN_SHORT_TERM" else "MEDIUM"
            ),
            "market_implication": "结构性波动与板块分化持续，出口链承压但自主可控受益",
            "option_implication": "高 IV 持续，Skew 维持偏斜，事件型 crush 反复",
        },
        {
            "name": "升级螺旋",
            "conditions": [
                "反制失控或博弈从经济域扩展至金融/军事域",
                "原框架失效（证伪条件触发）",
            ],
            "likelihood_level": "LOW",
            "market_implication": "波动率进一步飙升，跨资产联动下跌",
            "option_implication": "双买与长 Gamma 环境（仅描述市场状态，不构成建议）",
        },
    ]
    mood = "恐慌情绪占优" if sentiment.get("verdict") == "BEARISH" and iv.get("state") in ("HIGH", "ELEVATED") else (
        "情绪中性、波动率偏高" if iv.get("state") == "HIGH" else "情绪中性"
    )
    return [
        {
            "mood": mood,
            "policy_signals": policy.get("signals"),
            "scenarios": scenarios,
        }
    ]


def build_macro_assessment(
    snapshot_payload: Mapping[str, Any],
    items_path: str | Path,
    policy_path: str | Path = DEFAULT_POLICY_DIR,
    policy_id: str | None = None,
) -> dict[str, Any]:
    """端到端宏观研判：情绪 -> IV 晴雨表 -> 政策博弈/政治经济学 -> 情景与期权含义。"""

    items = load_research_items(items_path)
    sentiment = quantify_sentiment(items)
    iv = assess_iv_emotion(
        snapshot_payload.get("earnings", {}),
        list(snapshot_payload.get("legs", [])),
    )
    policy = _analyze_policy_input(policy_path, policy_id)
    scenario_block = _scenarios(sentiment, iv, policy)[0]

    facts_status = policy["qiu_shi"]["facts_first"]["status"]
    confidence_points = 0
    confidence_points += 1 if facts_status == "OK" else 0
    confidence_points += 1 if len(items) >= 3 else 0
    confidence_points += 1 if policy.get("verdict_reads") else 0
    confidence = "HIGH" if confidence_points == 3 else "MEDIUM" if confidence_points >= 1 else "LOW"

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "underlying": str(snapshot_payload.get("underlying", "")),
        "sentiment": sentiment,
        "iv_emotion": iv,
        "policy_analysis": policy,
        "macro_judgment": {
            "mood": scenario_block["mood"],
            "policy_signals": scenario_block["policy_signals"],
            "scenarios": scenario_block["scenarios"],
            "confidence": confidence,
            "contrarian_note": (
                "逆情绪操作的前提：事件性质可逆、事实与证据支持、自身风险预算允许；"
                "本模块只提供分析框架与证据链，不构成操作建议。"
            ),
        },
        "disclaimer": (
            "宏观研判为研究/教育用途，非投资建议；可能性为定性级别而非概率；"
            "案例研究不代表实时事实；不替代专业机构与用户的最终判断。"
        ),
    }


def audit(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(AUDIT_LOG_SCRIPT), "--event", event],
        input=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout.decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI 宏观研判层")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT), help="冻结快照 JSON 路径")
    parser.add_argument("--items", default=str(DEFAULT_ITEMS), help="研究条目 JSON 路径")
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_DIR),
        help="政策事件 JSON 路径或 policy_events 目录",
    )
    parser.add_argument(
        "--policy-id",
        default=None,
        help="从事件库选择事件（缺省取 ACTIVE 中 date 最新者作为主要分析对象）",
    )
    parser.add_argument("--out", default=str(OUT_DIR / "macro_assessment_hero.json"))
    parser.add_argument("--no-audit", action="store_true", help="跳过审计留痕")
    args = parser.parse_args()

    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load snapshot {args.snapshot}") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a JSON object")

    assessment = build_macro_assessment(snapshot, args.items, args.policy, args.policy_id)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(assessment, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not args.no_audit:
        audit(
            "macro_assessment",
            {
                "underlying": assessment["underlying"],
                "sentiment_index": assessment["sentiment"]["index"],
                "sentiment_verdict": assessment["sentiment"]["verdict"],
                "iv_state": assessment["iv_emotion"]["state"],
                "skew_verdict": assessment["iv_emotion"]["skew_verdict"],
                "policy_verdict_reads": assessment["policy_analysis"]["verdict_reads"],
                "confidence": assessment["macro_judgment"]["confidence"],
                "output_path": str(out_path),
            },
        )

    print("=" * 78)
    print(f"GOAI 宏观研判 | {assessment['underlying']} | {assessment['macro_judgment']['mood']}")
    print(f"情绪指数：{assessment['sentiment']['index']} ({assessment['sentiment']['verdict']})")
    print(
        f"IV 晴雨表：{assessment['iv_emotion']['state']} | "
        f"Skew {assessment['iv_emotion']['skew_verdict']}"
    )
    principal = assessment["policy_analysis"]["principal_contradiction"]
    print(f"主要矛盾：{principal['pair']} [{principal['nature']}]")
    library = assessment["policy_analysis"].get("library")
    if library is not None:
        verification = library["health_report"]["verification"]
        print(
            f"政策事件库：{library['event_count']} 个事件 | 主事件 "
            f"{library['primary_event_id']} | 来源核验 VERIFIED "
            f"{verification['VERIFIED']} / PENDING {verification['PENDING']}"
        )
    for read in assessment["policy_analysis"]["verdict_reads"]:
        print(f"  政策可落地性 [{read['scope']}]：{read['verdict']} - {read['reason']}")
    print(f"置信度：{assessment['macro_judgment']['confidence']}")
    print(f"\n输出 JSON：{out_path}")
    print("免责声明：宏观研判为研究/教育用途，非投资建议；案例研究不代表实时事实。")


if __name__ == "__main__":
    main()
