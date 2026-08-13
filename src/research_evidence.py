"""GOAI 期权智能终端 - 投研证据整理与事件影响研判层。

功能边界：
- 把公告 / 财报 / 新闻 / 研报 / 行业数据整理成结构化、可溯源的研究摘要；
- 与历史财报日实际波动、隐含事件波动、IV 水位等期权数据交叉分析；
- 输出对“股价方向”与“期权波动影响”的研判（决策支持，不构成投资建议）。

铁律：
- 所有数字来自冻结快照、回测输出或本模块的可复算规则；LLM 不参与数字生成；
- 示例数据必须显式标记 synthetic=True，不得冒充真实市场证据；
- 规则化情绪分类是演示级启发式，正式版本须接入带来源链接的人工/LLM 复核。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ITEMS = ROOT / "data" / "research_items_hero.json"
DEFAULT_BACKTEST = ROOT / "data" / "backtest_tencent_straddle.json"
OUT_DIR = ROOT / "data"
AUDIT_LOG_SCRIPT = (
    ROOT / ".agents" / "skills" / "futu-options-agent" / "scripts" / "audit_log.py"
)

KINDS = {"announcement", "earnings", "news", "research", "industry"}
MAX_ITEMS = 500
MAX_ITEM_BYTES = 64 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024

_SECRET_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "private_key",
    "acc_id",
    "account_id",
    "order_id",
    "card_num",
)

# 演示级关键词规则：只用于把投研信息整理成可解释的摘要，不用于预测股价。
POSITIVE_KEYWORDS = (
    "超预期",
    "增长",
    "提升",
    "上调",
    "盈利",
    "回购",
    "增持",
    "优于预期",
    "beat",
    "raise",
    "buyback",
    "growth",
    "profit",
    "upgrade",
)
NEGATIVE_KEYWORDS = (
    "不及预期",
    "下滑",
    "下调",
    "减持",
    "亏损",
    "监管",
    "审查",
    "罚款",
    "风险",
    "miss",
    "cut",
    "downside",
    "weak",
    "regulation",
)
EVENT_KEYWORDS = (
    "业绩",
    "财报",
    "公告",
    "股息",
    "派息",
    "回购",
    "拆分",
    "监管",
    "审查",
    "earnings",
    "dividend",
    "buyback",
    "announcement",
)


def _reject_sensitive(raw: Mapping[str, Any]) -> None:
    stack = list(raw.items())
    visited = 0
    while stack:
        key, value = stack.pop()
        visited += 1
        if visited > 10_000:
            raise ValueError("research item exceeds the metadata traversal limit")
        normalized = str(key).strip().lower()
        if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
            raise ValueError(f"research item contains a forbidden sensitive field: {key}")
        if isinstance(value, dict):
            stack.extend(value.items())
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    stack.append((f"{key}[{index}]", item))


def _require_text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"research item field {key!r} is invalid")
    return value.strip()


def _item_fingerprint(item: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in item.items() if key != "sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_and_normalize_items(
    raw_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """校验并规范化研究条目（幂等，可复用于各数据源适配器）。"""

    if len(raw_items) > MAX_ITEMS:
        raise ValueError(f"research items exceed the {MAX_ITEMS} item limit")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ValueError(f"research item {index} must be an object")
        _reject_sensitive(raw_item)
        item_id = _require_text(raw_item, "id")
        if item_id in seen:
            raise ValueError(f"duplicate research item id: {item_id}")
        seen.add(item_id)
        kind = _require_text(raw_item, "kind").lower()
        if kind not in KINDS:
            raise ValueError(f"research item {item_id!r} has unsupported kind: {kind}")
        title = _require_text(raw_item, "title")
        source = _require_text(raw_item, "source")
        url = raw_item.get("url")
        body = raw_item.get("body")
        tags = raw_item.get("tags")
        if not isinstance(url, str):
            url = ""
        url = url.strip()
        if not isinstance(body, str):
            body = ""
        if tags is None:
            tags = []
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"research item {item_id!r} tags must be a list of strings")
        published_at_raw = raw_item.get("published_at")
        if not isinstance(published_at_raw, str):
            raise ValueError(f"research item {item_id!r} published_at must be a string")
        published_at = published_at_raw.strip()
        if not published_at and not url:
            raise ValueError(
                f"research item {item_id!r} published_at is empty and no URL anchors its capture time"
            )
        publish_time_unknown = not published_at
        if not isinstance(raw_item.get("synthetic", False), bool):
            raise ValueError(f"research item {item_id!r} synthetic must be a boolean")
        encoded = json.dumps(
            {
                "id": item_id,
                "kind": kind,
                "title": title,
                "published_at": published_at,
                "publish_time_unknown": publish_time_unknown,
                "source": source,
                "url": url,
                "body": body,
                "tags": list(tags),
                "synthetic": bool(raw_item.get("synthetic", False)),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > MAX_ITEM_BYTES:
            raise ValueError(f"research item {item_id!r} exceeds the 64 KiB limit")
        items.append(
            {
                "id": item_id,
                "kind": kind,
                "title": title,
                "published_at": published_at,
                "publish_time_unknown": publish_time_unknown,
                "source": source,
                "url": url,
                "body": body,
                "tags": list(tags),
                "synthetic": bool(raw_item.get("synthetic", False)),
                "sha256": _item_fingerprint(
                    {
                        "id": item_id,
                        "kind": kind,
                        "title": title,
                        "published_at": published_at,
                        "source": source,
                        "url": url,
                        "body": body,
                        "tags": list(tags),
                        "synthetic": bool(raw_item.get("synthetic", False)),
                    }
                ),
            }
        )
    return items


def load_research_items(path: str | Path = DEFAULT_ITEMS) -> list[dict[str, Any]]:
    """读取 canonical JSON/JSONL 研究条目并校验。

    支持两种形态：
    - JSON 对象：{"meta": {...}, "items": [...]}
    - JSON 数组：[...]
    每条必须包含 id / kind / title / published_at / source；
    synthetic=True 的条目只用于演示，不得当作真实市场证据。
    """

    items_path = Path(path)
    try:
        raw_bytes = items_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read research items {items_path}") from exc
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise ValueError("research items file exceeds the 8 MiB limit")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("research items file is not valid UTF-8 JSON") from exc

    if isinstance(raw, dict):
        raw_items = raw.get("items")
        meta = raw.get("meta", {})
        if not isinstance(raw_items, list):
            raise ValueError("research items object must contain an items list")
    elif isinstance(raw, list):
        raw_items = raw
        meta = {}
    else:
        raise ValueError("research items file must be a JSON object or array")
    if not isinstance(meta, dict):
        raise ValueError("research items meta must be an object")
    return validate_and_normalize_items(raw_items)


def _keywords_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    haystack = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in haystack]


def classify_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """对单条投研信息做确定性整理：情绪倾向 + 期权决策相关性。"""

    text = " ".join([str(item.get("title", "")), str(item.get("body", ""))])
    positive = _keywords_hits(text, POSITIVE_KEYWORDS)
    negative = _keywords_hits(text, NEGATIVE_KEYWORDS)
    if positive and negative:
        sentiment = "mixed"
    elif positive:
        sentiment = "bullish"
    elif negative:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    kind = str(item.get("kind", "")).lower()
    event_hits = _keywords_hits(text, EVENT_KEYWORDS)
    relevant = kind in {"announcement", "earnings"} or bool(event_hits)
    return {
        "id": item["id"],
        "kind": kind,
        "sentiment": sentiment,
        "positive_hits": positive,
        "negative_hits": negative,
        "event_hits": event_hits,
        "relevant": relevant,
    }


def summarize_items(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把研究条目整理成结构化摘要（不去重原文，只输出可展示摘要）。"""

    by_kind: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    synthetic_count = 0
    for item in items:
        kind = str(item["kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if item.get("synthetic"):
            synthetic_count += 1
        classification = classify_item(item)
        entries.append(
            {
                "id": item["id"],
                "kind": kind,
                "title": item["title"],
                "published_at": item["published_at"],
                "source": item["source"],
                "url": item.get("url", ""),
                "synthetic": bool(item.get("synthetic", False)),
                "sha256": item.get("sha256", ""),
                "summary": item.get("body", "")[:240] or item["title"],
                "sentiment": classification["sentiment"],
                "matched_keywords": {
                    "positive": classification["positive_hits"],
                    "negative": classification["negative_hits"],
                    "event": classification["event_hits"],
                },
                "relevant_to_options": classification["relevant"],
            }
        )
    entries.sort(key=lambda entry: (entry["published_at"], entry["id"]))
    return {
        "item_count": len(items),
        "by_kind": by_kind,
        "synthetic_count": synthetic_count,
        "synthetic_only": synthetic_count == len(items) > 0,
        "items": entries,
    }


def _aggregate_sentiment(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    classifications = [classify_item(item) for item in items]
    bull = sum(1 for item in classifications if item["sentiment"] == "bullish")
    bear = sum(1 for item in classifications if item["sentiment"] == "bearish")
    mixed = sum(1 for item in classifications if item["sentiment"] == "mixed")
    neutral = sum(1 for item in classifications if item["sentiment"] == "neutral")
    directional = bull + bear
    if directional == 0:
        verdict = "NEUTRAL" if neutral else "UNKNOWN"
    elif mixed > 0 or (bull > 0 and bear > 0):
        verdict = "MIXED"
    elif bull > bear:
        verdict = "BULLISH"
    elif bear > bull:
        verdict = "BEARISH"
    else:
        verdict = "MIXED"

    if directional >= 5 and max(bull, bear) / directional >= 0.7:
        confidence = "HIGH"
    elif directional >= 3:
        confidence = "MEDIUM"
    elif directional == 0:
        confidence = "LOW"
    else:
        confidence = "LOW"

    evidence = [item["id"] for item in classifications if item["sentiment"] != "neutral"]
    return {
        "verdict": verdict,
        "confidence": confidence,
        "counts": {
            "bullish": bull,
            "bearish": bear,
            "mixed": mixed,
            "neutral": neutral,
        },
        "evidence_ids": evidence,
    }


def load_earnings_move_stats(
    backtest_path: str | Path = DEFAULT_BACKTEST,
    earnings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从既有回测输出提取历史财报日实际波动，并与隐含事件波动对比。"""

    try:
        raw = json.loads(Path(backtest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load backtest summary {backtest_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("backtest summary must be a JSON object")

    engine_section = raw.get("engine_backtest")
    periods = engine_section.get("periods") if isinstance(engine_section, dict) else None
    if not isinstance(periods, list):
        periods = []

    realized_d1: list[float] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        s_pre = period.get("s_pre")
        horizons = period.get("horizons")
        d1 = horizons.get("1") if isinstance(horizons, dict) else None
        s_post = d1.get("s_post") if isinstance(d1, dict) else None
        if (
            isinstance(s_pre, (int, float))
            and not isinstance(s_pre, bool)
            and isinstance(s_post, (int, float))
            and not isinstance(s_post, bool)
            and math.isfinite(float(s_pre))
            and math.isfinite(float(s_post))
            and float(s_pre) > 0
        ):
            realized_d1.append(abs(float(s_post) - float(s_pre)) / float(s_pre) * 100.0)

    stats: dict[str, Any] = {
        "source": str(Path(backtest_path)),
        "n_periods": len(periods),
        "realized_d1_pct": [round(value, 2) for value in sorted(realized_d1)],
        "realized_d1_median_pct": (
            round(statistics.median(realized_d1), 2) if realized_d1 else None
        ),
        "realized_d1_mean_pct": (
            round(statistics.fmean(realized_d1), 2) if realized_d1 else None
        ),
    }
    if earnings is not None:
        for key in (
            "expected_move_pct",
            "iv",
            "iv_rank",
            "iv_percentile",
            "hv_30d",
            "last_report_iv_crush",
            "history_report_iv_crush",
        ):
            value = earnings.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stats[key] = float(value)
            else:
                stats[key] = None
    return stats


def assess_option_impact(
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    """期权影响研判：事件波动溢价、IV crush 风险与 IV 水位。"""

    checks: list[dict[str, Any]] = []
    verdict_levels: list[int] = []
    implied = stats.get("expected_move_pct")
    realized_median = stats.get("realized_d1_median_pct")
    if isinstance(implied, (int, float)) and isinstance(realized_median, (int, float)):
        if realized_median > 0:
            ratio = float(implied) / float(realized_median)
            if ratio >= 1.2:
                level = 2
                result = "HIGH"
                detail = (
                    f"隐含事件波动 {implied:.2f}% 是历史财报日实际波动中位数 "
                    f"{realized_median:.2f}% 的 {ratio:.2f} 倍，事件溢价偏高"
                )
            elif ratio >= 1.0:
                level = 1
                result = "MEDIUM"
                detail = (
                    f"隐含事件波动 {implied:.2f}% 接近历史实际波动中位数 "
                    f"{realized_median:.2f}%（比值 {ratio:.2f}）"
                )
            else:
                level = 0
                result = "LOW"
                detail = (
                    f"隐含事件波动 {implied:.2f}% 低于历史实际波动中位数 "
                    f"{realized_median:.2f}%"
                )
        else:
            level, result, detail = 1, "UNKNOWN", "历史实际波动样本缺失"
        checks.append(
            {
                "check": "隐含事件波动 vs 历史实际波动",
                "result": result,
                "detail": detail,
            }
        )
        verdict_levels.append(level)

    iv_rank = stats.get("iv_rank")
    if isinstance(iv_rank, (int, float)):
        if iv_rank >= 70:
            level, result = 2, "HIGH"
            detail = f"IV Rank {iv_rank:.1f} ≥ 70，事件前波动率处于历史高位"
        elif iv_rank >= 30:
            level, result = 1, "MEDIUM"
            detail = f"IV Rank {iv_rank:.1f} 处于中位区间"
        else:
            level, result = 0, "LOW"
            detail = f"IV Rank {iv_rank:.1f} 处于低位"
        checks.append({"check": "IV 水位", "result": result, "detail": detail})
        verdict_levels.append(level)

    crush_ref = stats.get("last_report_iv_crush")
    history_crush = stats.get("history_report_iv_crush")
    if isinstance(crush_ref, (int, float)) or isinstance(history_crush, (int, float)):
        detail = (
            f"最近一期业绩后 IV crush 参考 "
            f"{crush_ref if isinstance(crush_ref, (int, float)) else 'N/A'}pp；"
            f"历史参考 {history_crush if isinstance(history_crush, (int, float)) else 'N/A'}pp"
        )
        checks.append({"check": "历史 IV crush 参考", "result": "NOTE", "detail": detail})

    if verdict_levels:
        worst = max(verdict_levels)
        verdict = (
            "COMPRESSION_RISK_HIGH"
            if worst >= 2
            else "COMPRESSION_RISK_MODERATE"
            if worst == 1
            else "COMPRESSION_NEUTRAL"
        )
    else:
        verdict = "UNKNOWN"
    return {
        "verdict": verdict,
        "iv_crush_risk": "HIGH" if verdict == "COMPRESSION_RISK_HIGH" else "MODERATE" if verdict == "COMPRESSION_RISK_MODERATE" else "LOW",
        "checks": checks,
        "note": (
            "跨式等方向中性策略主要受波动影响；财报日方向性涨跌由实际内容决定，"
            "本层只整理证据与波动口径，不预测股价。"
        ),
    }


def build_research_evidence(
    underlying: str,
    earnings: Mapping[str, Any],
    backtest_path: str | Path = DEFAULT_BACKTEST,
    items_path: str | Path = DEFAULT_ITEMS,
) -> dict[str, Any]:
    """端到端生成投研证据包：整理摘要 -> 期权数据交叉 -> 影响研判。"""

    items = load_research_items(items_path)
    digest = summarize_items(items)
    sentiment = _aggregate_sentiment(items)
    stats = load_earnings_move_stats(backtest_path, earnings)
    option_impact = assess_option_impact(stats)

    stock_rationale = (
        f"整理 {digest['item_count']} 条投研信息：看多 {sentiment['counts']['bullish']}、"
        f"看空 {sentiment['counts']['bearish']}、混合 {sentiment['counts']['mixed']}、"
        f"中性/未知 {sentiment['counts']['neutral']}。"
        "该倾向只反映对公告/新闻/研报的结构化整理，不构成股价预测。"
    )
    if digest["synthetic_only"]:
        stock_rationale += " 当前输入为显式标记的示例数据（synthetic），仅用于演示证据链路。"

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "underlying": underlying,
        "mode": "REPLAY",
        "digest": digest,
        "earnings_move_stats": stats,
        "stock_price_impact": {
            "verdict": sentiment["verdict"],
            "confidence": sentiment["confidence"],
            "rationale": stock_rationale,
            "counts": sentiment["counts"],
            "evidence_ids": sentiment["evidence_ids"],
        },
        "option_impact": option_impact,
        "disclaimer": (
            "研究/决策支持用途，非投资建议；示例数据不代表真实市场事实；"
            "不替代专业机构与用户的最终判断。"
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
    parser = argparse.ArgumentParser(description="GOAI 投研证据整理与影响研判")
    parser.add_argument(
        "--snapshot",
        default=str(ROOT / "data" / "hero_inputs.json"),
        help="冻结快照 JSON 路径（用于读取业绩/IV 上下文）",
    )
    parser.add_argument("--items", default=str(DEFAULT_ITEMS), help="研究条目 JSON 路径")
    parser.add_argument("--backtest", default=str(DEFAULT_BACKTEST), help="回测摘要 JSON 路径")
    parser.add_argument("--out", default=str(OUT_DIR / "research_evidence_hero.json"))
    parser.add_argument("--no-audit", action="store_true", help="跳过审计留痕")
    args = parser.parse_args()

    earnings: dict[str, Any] = {"expected_move_pct": 3.916}
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        if isinstance(snapshot, dict) and isinstance(snapshot.get("earnings"), dict):
            earnings = snapshot["earnings"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        earnings = {"expected_move_pct": 3.916}
    evidence = build_research_evidence(
        "HK.00700",
        earnings,
        backtest_path=args.backtest,
        items_path=args.items,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not args.no_audit:
        audit(
            "research_evidence",
            {
                "underlying": evidence["underlying"],
                "item_count": evidence["digest"]["item_count"],
                "synthetic_only": evidence["digest"]["synthetic_only"],
                "stock_verdict": evidence["stock_price_impact"]["verdict"],
                "option_verdict": evidence["option_impact"]["verdict"],
                "output_path": str(out_path),
            },
        )

    print("=" * 78)
    print(f"GOAI 投研证据包 | {evidence['underlying']} | synthetic={evidence['digest']['synthetic_only']}")
    print(f"条目：{evidence['digest']['item_count']} | 分类：{evidence['digest']['by_kind']}")
    print(f"股价影响：{evidence['stock_price_impact']['verdict']} "
          f"({evidence['stock_price_impact']['confidence']})")
    print(f"期权影响：{evidence['option_impact']['verdict']}")
    for check in evidence["option_impact"]["checks"]:
        print(f"  [{check['result']}] {check['check']}：{check['detail']}")
    print(f"\n输出 JSON：{out_path}")
    print("免责声明：研究/决策支持用途，非投资建议；示例数据不代表真实市场事实。")


if __name__ == "__main__":
    main()
