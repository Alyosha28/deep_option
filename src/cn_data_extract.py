"""中国官方数值页解析：从数据发布正文提取可核验数值，升级为 VERIFIED 事实。

`cn_html_list` 只拿到列表页标题（PENDING）。本模块把候选条目的正文页
抓回来后，用规则从可见文本中提取「同比上涨/下降 X%」「LPR 为 X%」
「进出口总值 X 亿元」等数值声明：
- 提取结果必须是页面原文中的真实子串（正则匹配 + 原句片段），
  提取不到就返回空，绝不补造；
- 这些数值来自官方页面直接抓取与规则解析，fact 核验状态标记 VERIFIED，
  并保留原文片段供复核；
- 仅允许抓取中国官方域名下的详情页，域名白名单与解析逻辑可测试。
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from src.cn_source_parsers import CnFeedItem

CN_DETAIL_HOSTS = {
    "www.pbc.gov.cn",
    "pbc.gov.cn",
    "www.stats.gov.cn",
    "stats.gov.cn",
    "www.customs.gov.cn",
    "customs.gov.cn",
}

MAX_CLAIMS_PER_TOPIC = 6
MAX_CLAIMS_PER_METRIC = 2
SNIPPET_WINDOW = 70

_NUMERIC_PATTERNS: dict[str, list[tuple[str, re.Pattern[str], str]]] = {
    "inflation": [
        (
            "CPI 同比",
            re.compile(
                r"居民消费价格(?:指数)?(?:（CPI）)?"
                r"[^。！？\n]{0,20}?同比(?P<direction>上涨|上升|下降|回落)"
                r"\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
        (
            "PPI 同比",
            re.compile(
                r"工业生产者出厂价格(?:指数)?(?:（PPI）)?"
                r"[^。！？\n]{0,20}?同比(?P<direction>上涨|上升|下降|回落)"
                r"\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
    ],
    "rates": [
        (
            "1年期LPR",
            re.compile(
                r"1年期(?:贷款市场报价利率|LPR)(?:为|报|是)?"
                r"\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
        (
            "5年期以上LPR",
            re.compile(
                r"5年期以上(?:贷款市场报价利率|LPR)(?:为|报|是)?"
                r"\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
    ],
    "trade": [
        (
            "进出口总值",
            re.compile(
                r"进出口总值\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>万亿|亿)?元"
            ),
            "元",
        ),
        (
            "进出口同比",
            re.compile(
                r"进出口[^。！？\n]{0,30}?同比(?P<direction>增长|上升|下降|减少)"
                r"\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
    ],
    "jobs": [
        (
            "城镇调查失业率",
            re.compile(
                r"城镇调查失业率(?:为|是)?\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
            ),
            "%",
        ),
    ],
}

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
}


class _TextCollector(HTMLParser):
    """收集正文可见文本，跳过 script/style 等非内容标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def extract_page_text(html_text: str) -> str:
    """把 HTML 转为仅含可见文本的字符串（块级标签处换行）。"""

    parser = _TextCollector()
    parser.feed(html_text)
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"[ \t\u3000]+", " ", text)


def extract_numeric_claims(
    text: str,
    topic: str,
) -> list[dict[str, str]]:
    """从正文原文提取数值声明；无匹配返回空列表，不补造。"""

    claims: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    per_metric: dict[str, int] = {}
    for metric, pattern, unit in _NUMERIC_PATTERNS.get(topic, []):
        for match in pattern.finditer(text):
            if per_metric.get(metric, 0) >= MAX_CLAIMS_PER_METRIC:
                break
            value = match.group("value")
            direction = match.groupdict().get("direction", "")
            if direction in {"下降", "回落", "减少"}:
                value = f"-{value}"
            key = (metric, value)
            if key in seen:
                continue
            unit_prefix = match.groupdict().get("unit") or ""
            claim_unit = f"{unit_prefix}{unit}"
            start = max(0, match.start() - SNIPPET_WINDOW)
            end = min(len(text), match.end() + SNIPPET_WINDOW)
            snippet = text[start:end].strip()
            if not snippet:
                continue
            seen.add(key)
            per_metric[metric] = per_metric.get(metric, 0) + 1
            claims.append(
                {
                    "metric": metric,
                    "value": value,
                    "unit": claim_unit,
                    "snippet": snippet,
                }
            )
            if len(claims) >= MAX_CLAIMS_PER_TOPIC:
                return claims
    return claims


def enrich_cn_item(
    item: CnFeedItem,
    source: Mapping[str, Any],
    fetcher: Callable[[str, str], str],
    user_agent: str,
    now: datetime,
) -> CnFeedItem:
    """抓取候选条目正文页并附加 VERIFIED 数值声明；失败时原样返回。

    只允许抓取中国官方域名下的详情页；域名不在白名单直接跳过。
    """

    host = (urlparse(item.link).hostname or "").lower()
    if host not in CN_DETAIL_HOSTS:
        return item
    try:
        html_text = fetcher(item.link, user_agent)
    except Exception:
        return item
    text = extract_page_text(html_text)
    claims = extract_numeric_claims(text, item.topic)
    if not claims:
        return item
    value_claims = tuple(
        {
            **claim,
            "source_url": item.link,
            "verification": "VERIFIED",
            "retrieved_at": now.isoformat(timespec="seconds"),
        }
        for claim in claims
    )
    return dataclasses.replace(item, value_claims=value_claims)
