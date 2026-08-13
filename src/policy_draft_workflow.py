"""GOAI 政策事件 DRAFT 工作流：摘要、就绪检查与提升 ACTIVE。

自动监控程序（`macro_source_watcher.py`）抓取的新事件以 DRAFT 入库，
只有事实与来源，没有博弈分析字段。本模块提供：
- `summarize_draft`：把 DRAFT 整理成可读摘要卡片（纯规则文本处理，
  不产生金融数字）；
- `completeness_check`：逐项检查提升 ACTIVE 的前置条件（事实有来源、
  恰好一个主要矛盾、可落地性分级、证伪与监控点、无 FAILED 核验）；
- `promote_draft` + `set_event_status`：条件全部满足才提升并写回，
  否则显式返回拒绝原因，绝不静默通过。

CLI：
    python -m src.policy_draft_workflow --library data/policy_events
    python -m src.policy_draft_workflow --library data/policy_events --promote <event_id>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.policy_library import (
    DEFAULT_POLICY_DIR,
    event_verification_summary,
    load_policy_library,
    upsert_policy_event,
)

REQUIRED_ANALYSIS_FIELDS = (
    "tensions",
    "verdict_reads",
    "falsification",
    "monitor",
)
REQUIRED_CONTEXT_FIELDS = ("stakeholders", "political_economy")

ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG_SCRIPT = (
    ROOT / ".agents" / "skills" / "futu-options-agent" / "scripts" / "audit_log.py"
)


def _missing_analysis_fields(event: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_ANALYSIS_FIELDS + REQUIRED_CONTEXT_FIELDS:
        value = event.get(field)
        if not isinstance(value, list) or not value:
            missing.append(f"{field}（非空列表）")
    return missing


def summarize_draft(event: Mapping[str, Any]) -> dict[str, Any]:
    """把 DRAFT 事件整理成结构化摘要卡片（纯规则文本处理，不产生金融数字）。"""

    facts = list(event.get("facts", []))
    links = sorted(
        {
            str(fact.get("source_url", "")).strip()
            for fact in facts
            if isinstance(fact, dict) and str(fact.get("source_url", "")).strip()
        }
    )
    verification = event_verification_summary(event)
    missing = _missing_analysis_fields(event)
    return {
        "event_id": event["id"],
        "event_name": event["name"],
        "event_date": event["date"],
        "event_type": event["type"],
        "status": str(event.get("status", "DRAFT")),
        "description": str(event.get("description", "")),
        "source_links": links,
        "verification": verification,
        "missing_analysis_fields": missing,
        "ready_for_promotion": (
            not missing and verification["status"] != "FAILED"
        ),
    }


def completeness_check(event: Mapping[str, Any]) -> dict[str, Any]:
    """提升 ACTIVE 的前置条件逐项检查，返回缺失清单。"""

    issues: list[str] = []
    facts = event.get("facts")
    if not isinstance(facts, list) or not facts:
        issues.append("facts 为空")
    else:
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not str(fact.get("source", "")).strip():
                issues.append(f"facts[{index}] 缺少 source")

    tensions = event.get("tensions")
    if not isinstance(tensions, list) or not tensions:
        issues.append("tensions 为空")
    else:
        principal_count = sum(
            1
            for tension in tensions
            if isinstance(tension, dict) and tension.get("principal") is True
        )
        if principal_count != 1:
            issues.append(
                f"tensions 中 principal=true 的数量必须恰好为 1（当前 {principal_count}）"
            )

    verdict_reads = event.get("verdict_reads")
    if not isinstance(verdict_reads, list) or not verdict_reads:
        issues.append("verdict_reads 为空（政策可落地性分级）")

    falsification = event.get("falsification")
    if not isinstance(falsification, list) or not falsification:
        issues.append("falsification 为空（证伪条件）")

    monitor = event.get("monitor")
    if not isinstance(monitor, list) or not monitor:
        issues.append("monitor 为空（监控点）")

    verification = event_verification_summary(event)
    if verification["counts"].get("FAILED", 0):
        issues.append("存在 FAILED 核验的事实，禁止提升")

    return {
        "ready": not issues,
        "missing": issues,
        "verification": verification["status"],
    }


def promote_draft(event: Mapping[str, Any]) -> dict[str, Any]:
    """仅当 completeness 全过时返回提升建议，否则返回拒绝原因。"""

    check = completeness_check(event)
    if not check["ready"]:
        return {"promoted": False, "reasons": check["missing"]}
    return {
        "promoted": True,
        "status": "ACTIVE",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "completeness": check,
    }


def build_promotion_notification(event: Mapping[str, Any]) -> dict[str, Any]:
    """构造提升事件的可审计通知载荷（不含敏感字段）。"""

    return {
        "event_id": event["id"],
        "event_name": event["name"],
        "event_date": event["date"],
        "event_type": event["type"],
        "status": "ACTIVE",
        "promoted_at": event.get("promoted_at", ""),
        "promoted_by": event.get("promoted_by", "manual-review"),
    }


def audit(event_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """写入 JSONL + SHA-256 哈希链审计日志。"""

    result = subprocess.run(
        [sys.executable, str(AUDIT_LOG_SCRIPT), "--event", event_name],
        input=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout.decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI 政策事件 DRAFT 工作流")
    parser.add_argument(
        "--library",
        default=str(DEFAULT_POLICY_DIR),
        help="政策事件库目录",
    )
    parser.add_argument(
        "--promote",
        default=None,
        metavar="EVENT_ID",
        help="尝试把指定事件提升为 ACTIVE（前置条件不满足时拒绝并列出缺失项）",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="跳过审计留痕（仅用于离线测试）",
    )
    args = parser.parse_args()

    library = load_policy_library(args.library)
    if args.promote is not None:
        event = next(
            (item for item in library["events"] if item["id"] == args.promote),
            None,
        )
        if event is None:
            available = ", ".join(item["id"] for item in library["events"])
            raise ValueError(
                f"policy event id {args.promote!r} not found; "
                f"available ids: {available}"
            )
        result = promote_draft(event)
        if not result["promoted"]:
            print(f"拒绝提升 {args.promote}：")
            for reason in result["reasons"]:
                print(f"  - {reason}")
            return
        updated = dict(event)
        updated["status"] = "ACTIVE"
        updated["promoted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updated["promoted_by"] = "manual-review"
        upsert_policy_event(updated, args.library)
        notification = build_promotion_notification(updated)
        if not args.no_audit:
            audit("policy_event_promoted", notification)
        print(f"已提升：{args.promote} -> ACTIVE")
        print(f"updated_at：{updated['updated_at']} | promoted_at：{updated['promoted_at']}")
        print(f"审计事件：policy_event_promoted（{json.dumps(notification, ensure_ascii=False)}）")
        return

    drafts = [
        event
        for event in library["events"]
        if str(event.get("status", "")).strip().upper() == "DRAFT"
    ]
    print(f"DRAFT 事件（{len(drafts)}）：")
    if not drafts:
        print("  当前库中没有 DRAFT 事件。")
        return
    for event in drafts:
        summary = summarize_draft(event)
        ready = "就绪" if summary["ready_for_promotion"] else "未就绪"
        verification = summary["verification"]
        print(
            f"  {event['id']} | {event['name']} | {event['date']} | {ready} | "
            f"核验 {verification['status']} "
            f"({verification['counts']['VERIFIED']}V/"
            f"{verification['counts']['PENDING']}P/{verification['counts']['FAILED']}F)"
        )
        if summary["description"]:
            print(f"    摘要：{summary['description'][:200]}")
        if summary["source_links"]:
            print(f"    来源：{summary['source_links'][0]}")
        for missing in summary["missing_analysis_fields"]:
            print(f"    缺：{missing}")


if __name__ == "__main__":
    main()
