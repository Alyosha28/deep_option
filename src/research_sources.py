"""Futu 新闻/公告/研报数据源适配器。

把 futu-news-search / futu-stock-digest 的真实输出转成研究证据层的
canonical 条目（src.research_evidence.validate_and_normalize_items 校验）。

支持两种输入：
- API 原始 JSON：https://ai-news-search.futunn.com/news_search 的响应
  （code=0 时 data 为条目数组，news_type 1=新闻 2=公告 3=研报）；
- 技能 Markdown 输出：futu-news-search 的编号列表（标题 + Publish time + URL）
  或 futu-stock-digest 的 Key evidence 块（标题 + URL）。

边界：
- 只做格式适配与来源留痕，不改写标题、时间或链接；
- 缺少发布时间但有原文 URL 的条目，标记 publish_time_unknown=True，不虚构时间；
- 转换产物默认 synthetic=False；测试/演示数据必须显式传入 synthetic=True。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.research_evidence import (
    validate_and_normalize_items,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "research_items_futu.json"

NEWS_TYPE_KIND = {1: "news", 2: "announcement", 3: "research"}
MAX_API_ITEMS = 50

_NUMBERED_ITEM = re.compile(r"^\s*\d+[.、)）]\s*(.+?)\s*$")
_PUBLISH_TIME = re.compile(r"^\s*(?:Publish time|发布时间|时间)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_URL_LINE = re.compile(r"^\s*(?:URL|链接|原文)\s*[:：]\s*(\S+?)\s*$", re.IGNORECASE)
_HTTP_LINE = re.compile(r"^\s*(https?://\S+)\s*$", re.IGNORECASE)


def _format_publish_time(value: Any) -> str:
    """把 API 的秒/毫秒时间戳或字符串转成朴素 ISO 时间；不做时区推断。"""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not math.isfinite(float(value)):
        return ""
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _entry_id(news_type: Any, news_id: Any, title: str, url: str) -> str:
    if isinstance(news_id, (str, int)) and str(news_id).strip():
        return f"futu-{news_type}-{str(news_id).strip()}"
    anchor = f"{title}|{url}".encode("utf-8")
    return f"futu-{news_type}-{hashlib.sha1(anchor).hexdigest()[:16]}"


def parse_news_api_response(
    raw: Mapping[str, Any],
    *,
    keyword: str,
) -> list[dict[str, Any]]:
    """解析 /news_search API 原始 JSON 为证据层条目（尚未校验）。"""

    code = raw.get("code")
    if code not in (0, "0"):
        message = raw.get("message") or "unknown API error"
        raise ValueError(f"futu news API error code={code}: {message}")
    data = raw.get("data")
    if not isinstance(data, list):
        raise ValueError("futu news API data must be a list")
    if len(data) > MAX_API_ITEMS:
        raise ValueError(f"futu news API returned more than {MAX_API_ITEMS} items")

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"futu news API item {index} must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"futu news API item {index} is missing title")
        title = title.strip()
        url = item.get("url")
        if not isinstance(url, str):
            url = ""
        url = url.strip()
        news_type = item.get("news_type", 1)
        try:
            kind = NEWS_TYPE_KIND[int(news_type)]
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"futu news API item {index} has unsupported news_type") from exc
        published_at = _format_publish_time(item.get("publish_time"))
        entries.append(
            {
                "id": _entry_id(news_type, item.get("news_id"), title, url),
                "kind": kind,
                "title": title,
                "published_at": published_at,
                "source": f"futu-news-search|{keyword}",
                "url": url,
                "body": "",
                "tags": ["futu", kind, keyword],
                "synthetic": False,
            }
        )
    return entries


def parse_news_markdown(
    text: str,
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    """解析技能 Markdown 输出（编号列表）为证据层条目。

    支持两种排版：
    - news-search：1. 标题 / Publish time: ... / URL: ...
    - stock-digest Key evidence：1. 标题 / https://...
    """

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        numbered = _NUMBERED_ITEM.match(line)
        if numbered:
            if current is not None:
                entries.append(current)
            current = {
                "id": f"futu-md-{len(entries) + 1}",
                "kind": "news",
                "title": numbered.group(1).strip(),
                "published_at": "",
                "source": f"futu-news-search-markdown|{symbol}",
                "url": "",
                "body": "",
                "tags": ["futu", "markdown", symbol],
                "synthetic": False,
            }
            continue
        if current is None:
            continue
        publish = _PUBLISH_TIME.match(line)
        if publish:
            current["published_at"] = publish.group(1).strip()
            continue
        url = _URL_LINE.match(line)
        if url:
            current["url"] = url.group(1).strip()
            continue
        http = _HTTP_LINE.match(line)
        if http and not current["url"]:
            current["url"] = http.group(1).strip()
            continue
        if current["title"] and not current["url"] and not current["published_at"]:
            current["title"] += " " + line
    if current is not None:
        entries.append(current)
    return entries


def _dedupe_entries(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = (str(entry.get("title", "")).strip(), str(entry.get("url", "")).strip())
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(entry))
    return unique


def adapt_futu_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    fetched_at: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    """把适配器条目转成 canonical 研究证据包（含 meta 来源说明）。"""

    unique = _dedupe_entries(entries)
    for entry in unique:
        entry["synthetic"] = bool(synthetic)
    items = validate_and_normalize_items(unique)
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "meta": {
            "platform": "futu-news-search/futu-stock-digest",
            "symbol": symbol,
            "fetched_at": fetched_at,
            "synthetic": bool(synthetic),
            "note": (
                "由 futu-news-search / futu-stock-digest 输出适配而来；"
                "标题、时间与原文链接保持原样。"
                "synthetic=True 时仅用于演示，不构成真实市场证据。"
            ),
        },
        "items": items,
    }


def _read_input(path: str | Path) -> dict[str, Any]:
    input_path = Path(path)
    try:
        raw_bytes = input_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read input {input_path}") from exc
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise ValueError(f"input exceeds the 8 MiB limit: {input_path}")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"input is not valid UTF-8 JSON: {input_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"input must be a JSON object: {input_path}")
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description="Futu 新闻/公告/研报 -> 研究证据 canonical 条目")
    parser.add_argument("--keyword", required=True, help="检索关键词/标的，如 Tencent")
    parser.add_argument("--api-json", help="/news_search 原始响应 JSON 文件")
    parser.add_argument("--markdown", help="futu-news-search / futu-stock-digest 输出文本文件")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 canonical JSON 路径")
    parser.add_argument("--synthetic", action="store_true", help="标记为演示数据（synthetic=True）")
    args = parser.parse_args()

    if not args.api_json and not args.markdown:
        parser.error("至少提供 --api-json 或 --markdown 之一")

    entries: list[dict[str, Any]] = []
    if args.api_json:
        entries.extend(parse_news_api_response(_read_input(args.api_json), keyword=args.keyword))
    if args.markdown:
        text = Path(args.markdown).read_text(encoding="utf-8")
        entries.extend(parse_news_markdown(text, symbol=args.keyword))

    bundle = adapt_futu_entries(entries, symbol=args.keyword, synthetic=args.synthetic)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Futu 新闻适配完成：{len(bundle['items'])} 条 -> {out_path}")
    print(f"  synthetic={bundle['meta']['synthetic']} | fetched_at={bundle['meta']['fetched_at']}")
    for item in bundle["items"]:
        flags = " [publish_time_unknown]" if item.get("publish_time_unknown") else ""
        print(f"  [{item['kind']}] {item['title']}{flags}")


if __name__ == "__main__":
    main()
