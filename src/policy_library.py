"""GOAI 政策事件库：目录加载、校验、来源健康检查与入库更新。

从单个「案例研究」文件升级为带实时来源的可审计事件库：
- 每个事件文件必带 `status` / `updated_at`，每条事实带 `source` /
  `source_url` / `verification` / `retrieved_at`；
- 库级校验保证 id 唯一与最小字段完整；
- 健康报告给出 VERIFIED / PENDING / FAILED 计数、无来源/无 URL/无抓取时间
  条目、过期事件标记，供评审与 Agent 决策前审计；
- `upsert_policy_event` 作为入库/更新插件，校验通过后写入
  `data/policy_events/<id>.json`（UTF-8，无 BOM）。

铁律：
- 来源核验状态只是可审计标记；PENDING/FAILED 不代表事实错误，
  正式使用前必须逐条复核；
- 本模块不产生投资建议，只负责事件库的加载、校验与健康报告。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_DIR = ROOT / "data" / "policy_events"

REQUIRED_EVENT_FIELDS = (
    "id",
    "name",
    "date",
    "type",
    "description",
    "status",
    "updated_at",
)
REQUIRED_FACT_FIELDS = ("id", "date", "fact", "source")
VERIFICATION_STATES = {"VERIFIED", "PENDING", "FAILED", "UNKNOWN"}
EVENT_STATUSES = {"ACTIVE", "ARCHIVED", "DRAFT", "FAILED"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STALE_DAYS = 30
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


def _reject_sensitive(raw: Mapping[str, Any]) -> None:
    stack = list(raw.items())
    visited = 0
    while stack:
        key, value = stack.pop()
        visited += 1
        if visited > 10_000:
            raise ValueError("policy event exceeds the metadata traversal limit")
        normalized = str(key).strip().lower()
        if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
            raise ValueError(f"policy event contains a forbidden sensitive field: {key}")
        if isinstance(value, dict):
            stack.extend(value.items())
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    stack.append((f"{key}[{index}]", item))


def _require_text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"policy event field {key!r} is invalid")
    return value.strip()


def _parse_datetime(text: str) -> datetime | None:
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


def validate_policy_event(event: Mapping[str, Any]) -> None:
    """校验单事件的最小库级契约（不含博弈分析段的完整性要求）。"""

    if not isinstance(event, dict):
        raise ValueError("policy event must be a JSON object")
    _reject_sensitive(event)
    for key in REQUIRED_EVENT_FIELDS:
        _require_text(event, key)
    event_id = event["id"].strip()
    if ID_PATTERN.fullmatch(event_id) is None:
        raise ValueError(
            f"policy event id {event_id!r} must match {ID_PATTERN.pattern}"
        )
    status = event["status"].strip().upper()
    if status not in EVENT_STATUSES:
        raise ValueError(
            f"policy event status {event['status']!r} is invalid; "
            f"expected one of {sorted(EVENT_STATUSES)}"
        )
    if not isinstance(event.get("facts"), list) or not event["facts"]:
        raise ValueError("policy event 'facts' must be a non-empty list")
    for index, fact in enumerate(event["facts"]):
        if not isinstance(fact, dict):
            raise ValueError(f"policy event fact {index} must be an object")
        for key in REQUIRED_FACT_FIELDS:
            _require_text(fact, key)
        verification = str(fact.get("verification", "UNKNOWN")).strip().upper() or "UNKNOWN"
        if verification not in VERIFICATION_STATES:
            raise ValueError(
                f"policy event fact {index} verification {fact.get('verification')!r} "
                f"is invalid; expected one of {sorted(VERIFICATION_STATES)}"
            )


def _load_event_file(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read policy event file {path}") from exc
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise ValueError(f"policy event file exceeds the 8 MiB limit: {path}")
    try:
        event = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"policy event file is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(event, dict):
        raise ValueError(f"policy event file must contain a JSON object: {path}")
    try:
        validate_policy_event(event)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return event


def load_policy_library(
    directory: str | Path = DEFAULT_POLICY_DIR,
) -> dict[str, Any]:
    """扫描目录下的 JSON 事件，校验 id 唯一与最小字段，返回库对象。"""

    library_dir = Path(directory)
    if not library_dir.exists():
        raise ValueError(f"policy library directory does not exist: {library_dir}")
    if not library_dir.is_dir():
        raise ValueError(f"policy library path is not a directory: {library_dir}")
    events: list[dict[str, Any]] = []
    for path in sorted(library_dir.glob("*.json")):
        events.append(_load_event_file(path))
    if not events:
        raise ValueError(f"policy library is empty (no JSON events under {library_dir})")
    seen: set[str] = set()
    for event in events:
        event_id = event["id"]
        if event_id in seen:
            raise ValueError(f"duplicate policy event id {event_id!r} in library")
        seen.add(event_id)
    return {
        "path": str(library_dir.resolve()),
        "event_count": len(events),
        "event_ids": [event["id"] for event in events],
        "events": events,
    }


def event_verification_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    """单事件来源核验汇总：VERIFIED / PENDING / FAILED / UNKNOWN 计数与总状态。"""

    counts = {state: 0 for state in sorted(VERIFICATION_STATES)}
    for fact in event.get("facts", []):
        if not isinstance(fact, dict):
            continue
        verification = str(fact.get("verification", "UNKNOWN")).strip().upper() or "UNKNOWN"
        counts[verification] = counts.get(verification, 0) + 1
    if counts["FAILED"]:
        status = "FAILED"
    elif counts["VERIFIED"] and not any(
        counts[state] for state in ("PENDING", "UNKNOWN")
    ):
        status = "VERIFIED"
    elif counts["VERIFIED"]:
        status = "PARTIALLY_VERIFIED"
    elif counts["PENDING"] or counts["UNKNOWN"]:
        status = "UNVERIFIED"
    else:
        status = "UNKNOWN"
    return {"status": status, "counts": counts}


def policy_health_report(library: Mapping[str, Any]) -> dict[str, Any]:
    """来源健康检查：核验状态计数、无来源/无 URL/无抓取时间、过期事件。"""

    events = list(library.get("events", []))
    now = datetime.now(timezone.utc)
    verification_counts = {state: 0 for state in sorted(VERIFICATION_STATES)}
    event_status_counts: dict[str, int] = {}
    fact_total = 0
    facts_without_source: list[dict[str, str]] = []
    facts_without_url: list[dict[str, str]] = []
    facts_without_retrieved_at: list[dict[str, str]] = []
    stale_events: list[dict[str, str]] = []
    recently_promoted: list[dict[str, str]] = []

    for event in events:
        status = str(event.get("status", "")).strip().upper()
        event_status_counts[status] = event_status_counts.get(status, 0) + 1
        promoted_at = str(event.get("promoted_at", "")).strip()
        if promoted_at:
            recently_promoted.append(
                {
                    "id": event["id"],
                    "name": event["name"],
                    "promoted_at": promoted_at,
                    "promoted_by": str(event.get("promoted_by", "")),
                }
            )
        updated = _parse_datetime(str(event.get("updated_at", "")))
        if updated is not None and (now - updated).days > STALE_DAYS:
            stale_events.append(
                {
                    "id": event["id"],
                    "updated_at": str(event.get("updated_at", "")),
                    "reason": f"updated_at older than {STALE_DAYS} days",
                }
            )
        for fact in event.get("facts", []):
            if not isinstance(fact, dict):
                continue
            fact_total += 1
            verification = (
                str(fact.get("verification", "UNKNOWN")).strip().upper() or "UNKNOWN"
            )
            verification_counts[verification] = verification_counts.get(verification, 0) + 1
            ref = {"event_id": event["id"], "fact_id": str(fact.get("id", ""))}
            if not str(fact.get("source", "")).strip():
                facts_without_source.append(ref)
            if not str(fact.get("source_url", "")).strip():
                facts_without_url.append(ref)
            if not str(fact.get("retrieved_at", "")).strip():
                facts_without_retrieved_at.append(ref)

    recently_promoted.sort(key=lambda item: item["promoted_at"], reverse=True)
    verified = verification_counts["VERIFIED"]
    if fact_total and verified == fact_total:
        library_status = "VERIFIED"
    elif verification_counts["FAILED"]:
        library_status = "FAILED"
    elif verified:
        library_status = "PARTIALLY_VERIFIED"
    else:
        library_status = "UNVERIFIED"

    return {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "event_count": len(events),
        "fact_count": fact_total,
        "event_status": event_status_counts,
        "verification": verification_counts,
        "verified_share_pct": (
            round(verified / fact_total * 100.0, 1) if fact_total else None
        ),
        "library_status": library_status,
        "facts_without_source": facts_without_source,
        "facts_without_url": facts_without_url,
        "facts_without_retrieved_at": facts_without_retrieved_at,
        "stale_events": stale_events,
        "recently_promoted": recently_promoted[:10],
        "note": (
            "来源核验状态仅为可审计标记；PENDING/FAILED 不代表事实错误，"
            "正式使用前需逐条复核链接与原文。"
        ),
    }


def select_policy_events(
    library: Mapping[str, Any],
    policy_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """选择主要分析事件：指定 policy_id，或缺省在 ACTIVE 事件中取 date 最新者。

    自动入库的 DRAFT 事件只出现在 additional_events（监控清单），
    不会在未完成博弈分析前被当作主要分析对象。
    """

    events = list(library.get("events", []))
    if not events:
        raise ValueError("cannot select from an empty policy library")
    if policy_id is not None:
        primary = next((event for event in events if event["id"] == policy_id), None)
        if primary is None:
            available = ", ".join(event["id"] for event in events)
            raise ValueError(
                f"policy event id {policy_id!r} not found in library; "
                f"available ids: {available}"
            )
        others = [event for event in events if event["id"] != policy_id]
        return primary, others, "policy-id-filter"

    active = [
        event
        for event in events
        if str(event.get("status", "")).strip().upper() == "ACTIVE"
    ]
    if not active:
        raise ValueError(
            "policy library has no ACTIVE events; promote an event to ACTIVE "
            "or pass policy_id explicitly"
        )

    def date_key(event: Mapping[str, Any]) -> tuple[datetime, str]:
        parsed = _parse_datetime(str(event.get("date", "")))
        return (parsed if parsed is not None else datetime.min), event["id"]

    primary = max(active, key=date_key)
    others = [event for event in events if event["id"] != primary["id"]]
    return primary, others, "latest-by-date"


def upsert_policy_event(
    event: Mapping[str, Any],
    directory: str | Path = DEFAULT_POLICY_DIR,
) -> Path:
    """入库/更新插件：校验后以 <id>.json 写入（UTF-8 无 BOM，先写临时文件再替换）。"""

    validate_policy_event(event)
    library_dir = Path(directory)
    library_dir.mkdir(parents=True, exist_ok=True)
    target = library_dir / f"{event['id']}.json"
    temp = target.with_suffix(".json.tmp")
    try:
        temp.write_text(
            json.dumps(dict(event), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def set_event_status(
    library_dir: str | Path,
    event_id: str,
    status: str,
) -> dict[str, Any]:
    """把事件库中指定事件更新为合法 status，写回 UTF-8 无 BOM，返回更新后事件。"""

    status_upper = status.strip().upper()
    if status_upper not in EVENT_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(EVENT_STATUSES)}"
        )
    library = load_policy_library(library_dir)
    event = next(
        (item for item in library["events"] if item["id"] == event_id),
        None,
    )
    if event is None:
        available = ", ".join(item["id"] for item in library["events"])
        raise ValueError(
            f"policy event id {event_id!r} not found in library; "
            f"available ids: {available}"
        )
    path = Path(library_dir) / f"{event_id}.json"
    updated = dict(event)
    updated["status"] = status_upper
    updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI 政策事件库（加载/健康报告/入库更新）")
    parser.add_argument("--library", default=str(DEFAULT_POLICY_DIR), help="政策事件库目录")
    parser.add_argument("--policy-id", default=None, help="只看单个事件 id")
    parser.add_argument(
        "--add",
        default=None,
        help="把单个政策事件 JSON 校验后入库（写入 <library>/<id>.json）",
    )
    parser.add_argument("--out", default=None, help="健康报告 JSON 输出路径（可选）")
    args = parser.parse_args()

    if args.add is not None:
        source = Path(args.add)
        try:
            event = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load policy event {source}") from exc
        target = upsert_policy_event(event, args.library)
        print(f"已入库：{source} -> {target}")

    library = load_policy_library(args.library)
    report = policy_health_report(library)
    verification = report["verification"]
    print("=" * 78)
    print(f"政策事件库：{library['path']}")
    print(f"事件数：{report['event_count']} | 事实数：{report['fact_count']} | "
          f"库状态：{report['library_status']}")
    print(
        f"来源核验：VERIFIED {verification['VERIFIED']} / "
        f"PENDING {verification['PENDING']} / FAILED {verification['FAILED']} / "
        f"UNKNOWN {verification['UNKNOWN']}（占比 {report['verified_share_pct']}% VERIFIED）"
    )
    if report["facts_without_source"] or report["facts_without_url"]:
        print(
            f"缺口：无来源 {len(report['facts_without_source'])} 条，"
            f"无 URL {len(report['facts_without_url'])} 条，"
            f"无抓取时间 {len(report['facts_without_retrieved_at'])} 条"
        )
    if report["stale_events"]:
        print(f"过期事件：{', '.join(item['id'] for item in report['stale_events'])}")
    print("-" * 78)
    for event in library["events"]:
        if args.policy_id is not None and event["id"] != args.policy_id:
            continue
        summary = event_verification_summary(event)
        print(
            f"{event['id']} | {event['name']} | {event['date']} | "
            f"{event['status']} | 核验 {summary['status']} "
            f"({summary['counts']['VERIFIED']}V/{summary['counts']['PENDING']}P/"
            f"{summary['counts']['FAILED']}F)"
        )
    if args.out is not None:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n健康报告 JSON：{out_path}")


if __name__ == "__main__":
    main()
