"""中国官方来源 HTML 列表解析（央行 / 统计局 / 海关）。

新增 kind `cn_html_list`：从官方 HTML 列表页提取文章标题、链接与日期，
按主题粗筛后交给 watcher 以 DRAFT 状态入库。

纪律：
- 只解析公开页面，不做登录、不绕过任何访问控制或反爬校验；
- HTML 标题/链接只是「事件草稿」，核验状态一律 PENDING；
- 日期优先从链接路径中的 YYYYMMDD 提取；提取不到留空，不虚构；
- 单个来源失败由 watcher 记录，不中断整轮抓取。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from src.topic_classifier import TOPIC_EVENT_TYPES, classify_topic

MIN_ANCHOR_CHARS = 8
MAX_ANCHOR_CHARS = 160
MAX_LINKS = 200


@dataclass(frozen=True)
class CnFeedItem:
    """与 watcher 的 FeedItem 字段对齐，保证 build_event 可直接使用。"""

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

_DATE_IN_HREF = re.compile(
    r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})"
)
_META_CHARSET = re.compile(
    rb'<meta[^>]+charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)',
    re.IGNORECASE,
)


def _valid_date_parts(year: str, month: str, day: str) -> bool:
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def extract_date_from_href(href: str) -> str:
    """从链接路径提取 YYYY-MM-DD；失败返回空串，不猜测。"""

    for match in _DATE_IN_HREF.finditer(href):
        parts = match.groupdict()
        if _valid_date_parts(parts["year"], parts["month"], parts["day"]):
            return f"{parts['year']}-{parts['month']}-{parts['day']}"
    return ""


def decode_html(raw: bytes, declared_charset: str | None = None) -> str:
    """按声明/探测编码解码；UTF-8 出现大量替换符时回退 GBK。"""

    if not isinstance(raw, bytes):
        return raw
    detected: str | None = None
    meta = _META_CHARSET.search(raw[:8192])
    if meta is not None:
        detected = meta.group(1).decode("ascii", errors="ignore").strip().lower()
    candidates: list[str] = []
    for name in (declared_charset, detected):
        if name:
            normalized = name.replace("gb2312", "gbk").replace("gb_2312", "gbk")
            if normalized not in candidates:
                candidates.append(normalized)
    for name in ("utf-8", "utf-8-sig", "gbk"):
        if name not in candidates:
            candidates.append(name)
    for name in candidates:
        try:
            text = raw.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
        if name in ("gbk", "gb2312") or "\ufffd" not in text:
            return text
        replacement_ratio = text.count("\ufffd") / max(len(text), 1)
        if replacement_ratio < 0.01:
            return text
    return raw.decode("utf-8", errors="replace")


class _AnchorParser(HTMLParser):
    """收集 `<a href>` 与可见文本（忽略 script/style 内文本）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if tag != "a" or self._skip_depth:
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        self._current_href = href or ""
        self._current_parts = []

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "a":
            self._flush_anchor()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            self._flush_anchor()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._current_href is not None:
            self._current_parts.append(data)

    def _flush_anchor(self) -> None:
        if self._current_href is None:
            return
        text = " ".join(" ".join(self._current_parts).split())
        self.anchors.append((self._current_href.strip(), text))
        self._current_href = None
        self._current_parts = []


def extract_links_from_html(
    text: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """返回 (绝对链接, 去空白后的可见文本)；只保留 .html 文章链接。"""

    parser = _AnchorParser()
    parser.feed(text)
    parser.close()
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, anchor in parser.anchors:
        anchor = anchor.strip()
        if len(anchor) < MIN_ANCHOR_CHARS or len(anchor) > MAX_ANCHOR_CHARS:
            continue
        if href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if not parsed.scheme.startswith("http") or not parsed.hostname:
            continue
        if not parsed.path.lower().endswith(".html"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append((absolute, anchor))
        if len(links) >= MAX_LINKS:
            break
    return links


def parse_cn_html_list(
    raw: str | bytes,
    source: Mapping[str, Any],
    now: datetime,
) -> list[CnFeedItem]:
    """解析 `cn_html_list` 官方列表页，返回 FeedItem 候选（PENDING）。

    raw 为 str 时直接解析；为 bytes 时先按页面声明编码解码。
    """

    text = decode_html(raw) if isinstance(raw, bytes) else raw
    base_url = str(source["url"])
    allowed_topics = [str(item) for item in source.get("topics", [])]
    items: list[Any] = []
    for link, anchor in extract_links_from_html(text, base_url):
        topic = classify_topic(anchor, allowed_topics)
        if topic is None:
            continue
        published = extract_date_from_href(link)
        event_id = "auto-cn-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
        items.append(
            CnFeedItem(
                event_id=event_id,
                title=anchor,
                link=link,
                published_date=published,
                summary=f"中国官方来源 {source['label']} 列表页自动提取的条目。",
                topic=topic,
                event_type=TOPIC_EVENT_TYPES[topic],
                verification="PENDING",
                dedupe_key=link,
            )
        )
    return items
