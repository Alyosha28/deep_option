"""GOAI 宏观来源自动监控与入库程序。

定时抓取重大金融事件 / 重大金融政策 / 宏观数据（通胀、利率、贸易、就业），
按主题过滤、去重后写入政策事件库（`data/policy_events/`），状态为 DRAFT，
并输出抓取报告。用户或 Agent 后续补全博弈分析后，再把 DRAFT 提升为 ACTIVE。

设计：
- 来源由 `data/sources_config.json` 配置，kind 支持：
  `rss`（RSS/Atom）、`fred_csv`、`bls_json`、`sec_atom`、
  `cn_html_list`（中国官方 HTML 列表页：央行/统计局/海关）；
- 只允许 https + 白名单域名；失败不伪造数据，报告标记 unreachable；
- FRED/BLS 直接抓取并解析的数值标记 VERIFIED；RSS 标题/摘要只标记 PENDING，
  需打开原文链接复核后才可提升核验状态；
- 去重基于事件 id 与 source_url，同一来源不会重复入库；
- DRAFT 事件不参与宏观研判的主要矛盾分析（`select_policy_events` 只选 ACTIVE），
  但会出现在附加事件清单中供监控。
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import hashlib
import io
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from src.cn_source_parsers import CnFeedItem, decode_html, parse_cn_html_list
from src.cn_data_extract import enrich_cn_item
from src.policy_library import (
    DEFAULT_POLICY_DIR,
    EmptyPolicyLibrary,
    load_policy_library,
    policy_health_report,
    upsert_policy_event,
)
from src.topic_classifier import TOPIC_EVENT_TYPES, TOPIC_KEYWORDS, classify_topic

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "data" / "sources_config.json"

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_TITLE_CHARS = 140
MAX_DESCRIPTION_CHARS = 800

ALLOWED_HOSTS: dict[str, set[str]] = {
    "rss": {"www.federalreserve.gov", "www.bls.gov", "www.ecb.europa.eu"},
    "fred_csv": {"fred.stlouisfed.org"},
    "bls_json": {"api.bls.gov"},
    "sec_atom": {"www.sec.gov"},
    "cn_html_list": {
        "www.pbc.gov.cn",
        "pbc.gov.cn",
        "www.stats.gov.cn",
        "stats.gov.cn",
        "www.customs.gov.cn",
        "customs.gov.cn",
    },
}
SUPPORTED_KINDS = frozenset(ALLOWED_HOSTS)


@dataclass(frozen=True)
class FeedItem:
    event_id: str
    title: str
    link: str
    published_date: str
    summary: str
    topic: str
    event_type: str
    verification: str
    dedupe_key: str
    value_note: str | None = None
    value_claims: tuple[dict[str, str], ...] = ()


def http_get(
    url: str,
    user_agent: str,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> str:
    """抓取 https 文本，限制响应大小，并按页面声明编码解码。"""

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/xml, application/json, text/csv, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        declared = response.headers.get_content_charset()
    if len(raw) > max_bytes:
        raise ValueError(f"response from {url} exceeds {max_bytes} bytes")
    return decode_html(raw, declared)


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """校验来源配置：只允许 https + 白名单域名 + 已实现 kind。"""

    if not isinstance(raw, dict):
        raise ValueError("sources config must be a JSON object")
    user_agent = raw.get("user_agent")
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError("sources config user_agent is invalid")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources config must contain a non-empty sources list")
    validated: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"sources config source {index} must be an object")
        for key in ("id", "kind", "url", "label"):
            value = source.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"sources config source {index} field {key!r} is invalid")
        kind = source["kind"].strip()
        if kind not in SUPPORTED_KINDS:
            raise ValueError(
                f"sources config source {index} kind {kind!r} is unsupported; "
                f"expected one of {sorted(SUPPORTED_KINDS)}"
            )
        parsed = urlparse(source["url"])
        if parsed.scheme != "https":
            raise ValueError(f"sources config source {index} url must use https")
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_HOSTS[kind]:
            raise ValueError(
                f"sources config source {index} host {host!r} is not allowlisted "
                f"for kind {kind}"
            )
        if kind in ("rss", "sec_atom", "cn_html_list"):
            topics = source.get("topics")
            if (
                not isinstance(topics, list)
                or not topics
                or any(not isinstance(item, str) or item not in TOPIC_KEYWORDS for item in topics)
            ):
                raise ValueError(
                    f"sources config source {index} topics must be a non-empty "
                    f"list of known topics"
                )
        if kind in ("fred_csv", "bls_json"):
            series = source.get("series")
            if not isinstance(series, list) or not series:
                raise ValueError(f"sources config source {index} series must be non-empty")
            for series_index, item in enumerate(series):
                if not isinstance(item, dict):
                    raise ValueError(f"sources config source {index} series {series_index} must be an object")
                for key in ("id", "topic", "label"):
                    if not isinstance(item.get(key), str) or not item[key].strip():
                        raise ValueError(
                            f"sources config source {index} series {series_index} "
                            f"field {key!r} is invalid"
                        )
                if item["topic"] not in TOPIC_KEYWORDS:
                    raise ValueError(
                        f"sources config source {index} series {series_index} "
                        f"topic {item['topic']!r} is unknown"
                    )
        validated.append(
            {
                "id": source["id"].strip(),
                "kind": kind,
                "url": source["url"].strip(),
                "label": source["label"].strip(),
                "enabled": bool(source.get("enabled", True)),
                "enrich_numeric": bool(source.get("enrich_numeric", False)),
                "topics": [str(item) for item in source.get("topics", [])],
                "series": list(source.get("series", [])),
            }
        )
    ids = [source["id"] for source in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("sources config source ids must be unique")
    return {
        "user_agent": user_agent.strip(),
        "max_items_per_source": int(raw.get("max_items_per_source", 20)),
        "sources": validated,
    }


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load sources config {path}") from exc
    return validate_config(raw)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_published_date(text: str) -> str:
    """返回 YYYY-MM-DD；无法解析时返回空串。"""

    text = text.strip()
    if not text:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _child_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _child_link(item: ET.Element) -> str:
    for child in item:
        if _local_name(child.tag) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        href = child.get("href")
        if href:
            return href.strip()
    return ""


def _rss_item_to_feed_item(
    element: ET.Element,
    source: Mapping[str, Any],
    now: datetime,
) -> FeedItem | None:
    title = _child_text(element, "title")
    link = _child_link(element)
    if not title or not link:
        return None
    published = _normalize_published_date(
        _child_text(element, "pubDate")
        or _child_text(element, "published")
        or _child_text(element, "updated")
    )
    summary = _child_text(element, "description") or _child_text(element, "summary")
    topic = classify_topic(f"{title} {summary}", list(source.get("topics", [])))
    if topic is None:
        return None
    event_id = "auto-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
    return FeedItem(
        event_id=event_id,
        title=title,
        link=link,
        published_date=published,
        summary=summary,
        topic=topic,
        event_type=TOPIC_EVENT_TYPES[topic],
        verification="PENDING",
        dedupe_key=link,
    )


def parse_rss_feed(
    text: str,
    source: Mapping[str, Any],
    now: datetime,
) -> list[FeedItem]:
    """解析 RSS 2.0 / Atom，按来源 topics 过滤后返回事件候选。"""

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"feed is not valid XML: {exc}") from exc
    items: list[FeedItem] = []
    for element in root.iter():
        if _local_name(element.tag) not in ("item", "entry"):
            continue
        item = _rss_item_to_feed_item(element, source, now)
        if item is not None:
            items.append(item)
    return items


def parse_fred_csv(
    text: str,
    series_configs: list[Mapping[str, Any]],
    now: datetime,
) -> list[FeedItem]:
    """解析 FRED fredgraph.csv：取每个序列的最新非空观测，标记 VERIFIED。"""

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("fred csv is empty") from exc
    header = [cell.strip() for cell in header]
    if not header or header[0] != "observation_date":
        raise ValueError("fred csv header is invalid")
    columns: dict[str, int] = {}
    for series in series_configs:
        series_id = str(series["id"]).strip()
        if series_id in header:
            columns[series_id] = header.index(series_id)
    if not columns:
        raise ValueError("fred csv contains none of the configured series")

    latest: dict[str, tuple[str, str]] = {}
    for row in reader:
        if len(row) < 2:
            continue
        obs_date = row[0].strip()
        for series_id, index in columns.items():
            if index >= len(row):
                continue
            value = row[index].strip()
            if value and (series_id not in latest or obs_date >= latest[series_id][1]):
                latest[series_id] = (value, obs_date)

    items: list[FeedItem] = []
    for series in series_configs:
        series_id = str(series["id"]).strip()
        if series_id not in latest:
            continue
        value, obs_date = latest[series_id]
        topic = str(series["topic"])
        label = str(series.get("label", series_id))
        value_note = f"最新观测 {value}（{obs_date}）"
        event_id = f"auto-fred-{series_id}-{obs_date}"
        items.append(
            FeedItem(
                event_id=event_id,
                title=f"{label}：{value_note}",
                link=f"https://fred.stlouisfed.org/series/{series_id}",
                published_date=obs_date,
                summary=f"FRED 官方序列 {series_id}（{label}），{value_note}。",
                topic=topic,
                event_type=TOPIC_EVENT_TYPES[topic],
                verification="VERIFIED",
                dedupe_key=f"fred:{series_id}:{obs_date}",
                value_note=value_note,
            )
        )
    return items


def parse_bls_json(
    text: str,
    series_configs: list[Mapping[str, Any]],
    now: datetime,
) -> list[FeedItem]:
    """解析 BLS v2 API JSON：取每个序列最新观测，标记 VERIFIED。"""

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bls json is invalid: {exc}") from exc
    results = raw.get("Results")
    if not isinstance(results, dict) or not isinstance(results.get("series"), list):
        raise ValueError("bls json Results.series is missing")
    by_id = {
        str(item.get("seriesID", "")): item for item in results["series"] if isinstance(item, dict)
    }
    items: list[FeedItem] = []
    for series in series_configs:
        series_id = str(series["id"]).strip()
        payload = by_id.get(series_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            continue
        latest = payload["data"][0]
        if not isinstance(latest, dict):
            continue
        year = str(latest.get("year", ""))
        period = str(latest.get("period", ""))
        value = str(latest.get("value", ""))
        obs_date = f"{year}-{period[1:]}" if period.startswith("M") else year
        topic = str(series["topic"])
        label = str(series.get("label", series_id))
        value_note = f"最新观测 {value}（{obs_date}）"
        event_id = f"auto-bls-{series_id}-{obs_date}"
        items.append(
            FeedItem(
                event_id=event_id,
                title=f"{label}：{value_note}",
                link=f"https://data.bls.gov/timeseries/{series_id}",
                published_date=obs_date,
                summary=f"BLS 官方序列 {series_id}（{label}），{value_note}。",
                topic=topic,
                event_type=TOPIC_EVENT_TYPES[topic],
                verification="VERIFIED",
                dedupe_key=f"bls:{series_id}:{obs_date}",
                value_note=value_note,
            )
        )
    return items


def build_event(
    item: FeedItem | CnFeedItem,
    source: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """把候选条目构造成可入库的 DRAFT 事件。"""

    iso_now = now.isoformat(timespec="seconds")
    published = item.published_date or now.date().isoformat()
    description = item.summary.strip() or f"来源 {source['label']} 自动抓取的事件草稿。"
    fact = item.title
    if item.value_note:
        fact = f"{item.title}；{item.value_note}"
    facts: list[dict[str, str]] = [
        {
            "id": "f1",
            "date": published,
            "fact": fact[:MAX_DESCRIPTION_CHARS],
            "source": source["label"],
            "source_url": item.link,
            "verification": item.verification,
            "retrieved_at": iso_now,
        }
    ]
    for index, claim in enumerate(item.value_claims, start=2):
        claim_text = f"{claim['metric']}：{claim['snippet']}"
        facts.append(
            {
                "id": f"f{index}",
                "date": published,
                "fact": claim_text[:MAX_DESCRIPTION_CHARS],
                "source": source["label"],
                "source_url": str(claim.get("source_url", item.link)),
                "verification": str(claim.get("verification", "VERIFIED")),
                "retrieved_at": str(claim.get("retrieved_at", iso_now)),
            }
        )
    return {
        "id": item.event_id,
        "name": item.title[:MAX_TITLE_CHARS],
        "date": published,
        "type": item.event_type,
        "status": "DRAFT",
        "updated_at": iso_now,
        "description": description[:MAX_DESCRIPTION_CHARS],
        "facts": facts,
        "sources": [
            {
                "id": source["id"],
                "label": source["label"],
                "url": item.link,
                "status": item.verification,
                "retrieved_at": iso_now,
            }
        ],
        "ingest": {
            "automated": True,
            "kind": source["kind"],
            "fetched_at": iso_now,
        },
    }


def _existing_keys(library_dir: Path) -> tuple[set[str], set[str]]:
    try:
        library = load_policy_library(library_dir)
        events = list(library["events"])
    except EmptyPolicyLibrary:
        events = []
    event_ids = {event["id"] for event in events}
    urls: set[str] = set()
    for event in events:
        for fact in event.get("facts", []):
            if isinstance(fact, dict) and str(fact.get("source_url", "")).strip():
                urls.add(str(fact["source_url"]).strip())
    return event_ids, urls


def run_once(
    config: Mapping[str, Any],
    library_dir: str | Path = DEFAULT_POLICY_DIR,
    *,
    fetcher: Callable[[str, str], str] | None = None,
    dry_run: bool = False,
    max_items: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """执行一轮抓取：每个来源失败只记录不中断，新事件去重后 DRAFT 入库。"""

    now = now or datetime.now(timezone.utc)
    fetcher = fetcher or http_get
    library_path = Path(library_dir)
    event_ids, seen_urls = _existing_keys(library_path)
    user_agent = str(config["user_agent"])
    limit = max_items if max_items is not None else int(config.get("max_items_per_source", 20))
    source_reports: list[dict[str, Any]] = []
    new_event_ids: list[str] = []

    for source in config["sources"]:
        source_report: dict[str, Any] = {
            "id": source["id"],
            "kind": source["kind"],
            "reachable": False,
            "skipped": False,
            "items_fetched": 0,
            "filtered_out": 0,
            "new_events": 0,
            "skipped_duplicates": 0,
            "enrich_attempts": 0,
            "enriched": 0,
            "error": None,
        }
        if not source.get("enabled", True):
            source_report["skipped"] = True
            source_report["error"] = "disabled in sources config"
            source_reports.append(source_report)
            continue
        try:
            text = fetcher(str(source["url"]), user_agent)
            source_report["reachable"] = True
            items: list[Any] = []
            if source["kind"] == "fred_csv":
                items = parse_fred_csv(text, source["series"], now)
            elif source["kind"] == "bls_json":
                items = parse_bls_json(text, source["series"], now)
            elif source["kind"] == "cn_html_list":
                items = parse_cn_html_list(text, source, now)
            else:
                items = parse_rss_feed(text, source, now)
            if source["kind"] == "cn_html_list" and source.get("enrich_numeric"):
                source_report["enrich_attempts"] = len(items)
                enriched_items: list[Any] = []
                for item in items:
                    if not isinstance(item, CnFeedItem):
                        enriched_items.append(item)
                        continue
                    try:
                        enriched = enrich_cn_item(
                            item, source, fetcher, user_agent, now
                        )
                    except Exception:
                        enriched = item
                    if enriched.value_claims:
                        source_report["enriched"] += 1
                    enriched_items.append(enriched)
                items = enriched_items
            items.sort(key=lambda item: item.published_date, reverse=True)
            source_report["items_fetched"] = len(items)
            for item in items[:limit]:
                if item.event_id in event_ids or item.link in seen_urls:
                    source_report["skipped_duplicates"] += 1
                    continue
                source_report["new_events"] += 1
                new_event_ids.append(item.event_id)
                if not dry_run:
                    upsert_policy_event(build_event(item, source, now), library_path)
                event_ids.add(item.event_id)
                seen_urls.add(item.link)
        except Exception as exc:  # 单来源失败不中断整轮抓取
            source_report["error"] = str(exc)
        source_reports.append(source_report)

    report: dict[str, Any] = {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "library": str(library_path.resolve()),
        "new_event_count": len(new_event_ids),
        "new_event_ids": new_event_ids,
        "sources": source_reports,
    }
    if not dry_run:
        try:
            report["library_health"] = policy_health_report(
                load_policy_library(library_path)
            )
        except ValueError as exc:
            # 全部来源失败、库仍为空时，健康报告降级为错误说明，不中断整轮报告。
            report["library_health"] = None
            report["library_health_error"] = str(exc)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GOAI 宏观来源自动监控：定时抓取并 DRAFT 入库"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="来源配置 JSON 路径")
    parser.add_argument("--library", default=str(DEFAULT_POLICY_DIR), help="政策事件库目录")
    parser.add_argument("--run-once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--daemon", action="store_true", help="按间隔循环监控")
    parser.add_argument("--interval-minutes", type=int, default=60, help="轮询间隔（分钟）")
    parser.add_argument("--dry-run", action="store_true", help="只报告候选，不写库")
    parser.add_argument("--max-items", type=int, default=None, help="每来源最多入库条数")
    parser.add_argument("--report-out", default=None, help="本轮报告 JSON 输出路径（可选）")
    args = parser.parse_args()

    config = load_config(args.config)
    if not args.run_once and not args.daemon:
        args.run_once = True

    def run_and_print() -> dict[str, Any]:
        report = run_once(
            config,
            args.library,
            dry_run=args.dry_run,
            max_items=args.max_items,
        )
        print("=" * 78)
        print(f"宏观来源监控 | {report['generated_at_utc']} | "
              f"新入库 {report['new_event_count']} 条"
              + ("（dry-run，未写库）" if report["dry_run"] else ""))
        for source in report["sources"]:
            if source.get("skipped"):
                print(f"  [SKIP] {source['id']}：配置中已停用")
                continue
            marker = "OK" if source["reachable"] else "FAIL"
            print(
                f"  [{marker}] {source['id']}：抓取 {source['items_fetched']} | "
                f"新 {source['new_events']} | 重复 {source['skipped_duplicates']}"
                + (
                    f" | 数值富化 {source['enriched']}/{source['enrich_attempts']}"
                    if source.get("enrich_attempts")
                    else ""
                )
                + (f" | 错误：{source['error']}" if source["error"] else "")
            )
        if report.get("library_health"):
            verification = report["library_health"]["verification"]
            print(
                f"库状态：{report['library_health']['library_status']} | "
                f"VERIFIED {verification['VERIFIED']} / "
                f"PENDING {verification['PENDING']} / "
                f"FAILED {verification['FAILED']}"
            )
        if args.report_out is not None:
            out_path = Path(args.report_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"报告 JSON：{out_path}")
        return report

    if args.run_once:
        run_and_print()
        return

    print(f"进入 daemon 模式：每 {args.interval_minutes} 分钟轮询一次（Ctrl+C 停止）")
    while True:
        try:
            run_and_print()
            time.sleep(max(args.interval_minutes, 1) * 60)
        except KeyboardInterrupt:
            print("\n已停止。")
            return


if __name__ == "__main__":
    main()
