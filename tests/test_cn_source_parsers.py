"""中国官方来源（cn_html_list）解析测试：全部离线，不联网、不写正式库。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from src.cn_source_parsers import (
    decode_html,
    extract_date_from_href,
    extract_links_from_html,
    parse_cn_html_list,
)
from src.macro_source_watcher import run_once, validate_config
from src.policy_library import load_policy_library

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

STATS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>数据发布</title></head>
<body>
  <div class="list">
    <a href="./202608/t20260809_1965008.html">2026年7月份居民消费价格同比上涨0.5%</a>
    <a href="./202608/t20260809_1965008.html">2026年7月份居民消费价格同比上涨0.5%</a>
    <a href="./202608/t20260809_1965007.html">2026年7月份工业生产者出厂价格同比上涨3.5%</a>
    <a href="./202608/t20260803_1964273.html">2026年7月下旬流通领域重要生产资料市场价格变动情况</a>
    <a href="./202607/t20260731_1964253.html">2026年7月中国采购经理指数运行情况</a>
    <a href="./202608/t20260801_1964000.html">某无关日常公告</a>
  </div>
</body></html>
"""

PBC_GBK_HTML = """<!DOCTYPE html>
<html><head><meta charset="GBK"><title>新闻发布</title></head>
<body>
  <a href="/goutongjiaoliu/113456/113469/2026081218034520348/index.html">2026年第二季度中国货币政策执行报告</a>
  <a href="/goutongjiaoliu/113456/113469/2026081015473613597/index.html">中国人民银行公告〔2026〕第20号</a>
  <a href="/goutongjiaoliu/113456/113469/2026080708481628617/index.html">中国人民银行与马来西亚国家银行续签双边本币互换协议</a>
  <a href="javascript:void(0)">站内导航链接</a>
</body></html>
""".encode("gbk")


class DecodeTests(unittest.TestCase):
    def test_gbk_bytes_decode_without_replacement_chars(self):
        text = decode_html(PBC_GBK_HTML)
        self.assertIn("2026年第二季度中国货币政策执行报告", text)
        self.assertNotIn("\ufffd", text)

    def test_utf8_bytes_with_declared_charset(self):
        text = decode_html(STATS_HTML.encode("utf-8"), declared_charset="utf-8")
        self.assertIn("居民消费价格", text)


class LinkExtractionTests(unittest.TestCase):
    def test_relative_links_normalized_and_deduplicated(self):
        links = extract_links_from_html(STATS_HTML, "https://www.stats.gov.cn/sj/zxfb/")
        hrefs = [href for href, _ in links]
        self.assertEqual(len(hrefs), len(set(hrefs)))
        self.assertIn(
            "https://www.stats.gov.cn/sj/zxfb/202608/t20260809_1965008.html",
            hrefs,
        )
        self.assertNotIn("javascript:void(0)", [href for href, _ in links])

    def test_absolute_links_kept(self):
        links = extract_links_from_html(
            PBC_GBK_HTML.decode("gbk"),
            "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
        )
        self.assertTrue(
            any(href.startswith("https://www.pbc.gov.cn/goutongjiaoliu/") for href, _ in links)
        )


class DateExtractionTests(unittest.TestCase):
    def test_date_from_href(self):
        self.assertEqual(
            extract_date_from_href(
                "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/2026081218034520348/index.html"
            ),
            "2026-08-12",
        )
        self.assertEqual(
            extract_date_from_href(
                "https://www.stats.gov.cn/sj/zxfb/202608/t20260809_1965008.html"
            ),
            "2026-08-09",
        )

    def test_invalid_date_returns_empty(self):
        self.assertEqual(extract_date_from_href("https://example.com/20261340/x.html"), "")


class ParseCnHtmlListTests(unittest.TestCase):
    def _source(self, topics: list[str]) -> dict:
        return {
            "id": "stats",
            "kind": "cn_html_list",
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "label": "国家统计局数据发布",
            "topics": topics,
        }

    def test_parses_filters_and_dedupes(self):
        items = parse_cn_html_list(STATS_HTML, self._source(["inflation"]), NOW)

        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual(item.verification, "PENDING")
            self.assertEqual(item.event_type, "macro-data")
            self.assertEqual(item.topic, "inflation")
        self.assertTrue(any("居民消费价格" in item.title for item in items))
        self.assertTrue(any("工业生产者出厂价格" in item.title for item in items))
        self.assertTrue(any("生产资料市场价格" in item.title for item in items))

    def test_date_and_event_id(self):
        items = parse_cn_html_list(STATS_HTML, self._source(["inflation"]), NOW)
        cpi = next(item for item in items if "居民消费价格" in item.title)

        self.assertEqual(cpi.published_date, "2026-08-09")
        self.assertTrue(cpi.event_id.startswith("auto-cn-"))
        self.assertTrue(cpi.dedupe_key.startswith("https://www.stats.gov.cn/"))

    def test_gbk_source_parses(self):
        items = parse_cn_html_list(
            PBC_GBK_HTML,
            {
                "id": "pbc",
                "kind": "cn_html_list",
                "url": "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
                "label": "中国人民银行新闻发布",
                "topics": ["rates"],
            },
            NOW,
        )

        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0].topic, "rates")
        self.assertEqual(items[0].event_type, "monetary-policy")
        self.assertEqual(items[0].published_date, "2026-08-12")

    def test_unrelated_titles_filtered_out(self):
        items = parse_cn_html_list(STATS_HTML, self._source(["policy"]), NOW)
        self.assertEqual(items, [])


class ValidateConfigTests(unittest.TestCase):
    def _config(self, **source_overrides: object) -> dict:
        source: dict[str, object] = {
            "id": "stats",
            "kind": "cn_html_list",
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "label": "统计局",
            "topics": ["inflation"],
        }
        source.update(source_overrides)
        return {
            "user_agent": "test-agent/1.0",
            "max_items_per_source": 10,
            "sources": [source],
        }

    def test_valid_cn_html_list_passes(self):
        validated = validate_config(self._config())
        self.assertEqual(validated["sources"][0]["kind"], "cn_html_list")

    def test_non_allowlisted_host_rejected(self):
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            validate_config(
                self._config(url="https://example.com/goutongjiaoliu/index.html")
            )

    def test_http_url_rejected(self):
        with self.assertRaisesRegex(ValueError, "https"):
            validate_config(
                self._config(url="http://www.stats.gov.cn/sj/zxfb/")
            )

    def test_missing_topics_rejected(self):
        config = self._config()
        config["sources"][0].pop("topics")  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ValueError, "topics"):
            validate_config(config)

    def test_project_config_includes_cn_sources(self):
        from src.macro_source_watcher import load_config

        config = load_config("data/sources_config.json")
        cn_sources = [
            source for source in config["sources"] if source["kind"] == "cn_html_list"
        ]
        self.assertGreaterEqual(len(cn_sources), 3)
        enabled = {source["id"] for source in cn_sources if source["enabled"]}
        self.assertIn("pbc-news", enabled)
        self.assertIn("stats-releases", enabled)


class RunOnceIntegrationTests(unittest.TestCase):
    def test_cn_source_ingests_draft_event(self):
        config = {
            "user_agent": "test-agent/1.0",
            "max_items_per_source": 10,
            "sources": [
                {
                    "id": "stats",
                    "kind": "cn_html_list",
                    "url": "https://www.stats.gov.cn/sj/zxfb/",
                    "label": "国家统计局数据发布",
                    "topics": ["inflation"],
                }
            ],
        }

        def fetcher(url: str, user_agent: str) -> str:
            return STATS_HTML

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(config, temp_dir, fetcher=fetcher, now=NOW)
            self.assertEqual(report["new_event_count"], 3)
            by_id = {source["id"]: source for source in report["sources"]}
            self.assertTrue(by_id["stats"]["reachable"])
            library = load_policy_library(temp_dir)
            self.assertEqual(len(library["events"]), 3)
            for event in library["events"]:
                self.assertEqual(event["status"], "DRAFT")
                self.assertEqual(event["facts"][0]["verification"], "PENDING")
                self.assertIn("stats.gov.cn", event["facts"][0]["source_url"])

    def test_cn_source_failure_recorded_not_fatal(self):
        config = {
            "user_agent": "test-agent/1.0",
            "max_items_per_source": 10,
            "sources": [
                {
                    "id": "pbc",
                    "kind": "cn_html_list",
                    "url": "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
                    "label": "央行",
                    "topics": ["rates"],
                }
            ],
        }

        def fetcher(url: str, user_agent: str) -> str:
            raise ConnectionError("cn site unreachable")

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(config, temp_dir, fetcher=fetcher, now=NOW)
            by_id = {source["id"]: source for source in report["sources"]}
            self.assertFalse(by_id["pbc"]["reachable"])
            self.assertIn("unreachable", by_id["pbc"]["error"])
            self.assertEqual(report["new_event_count"], 0)
            self.assertIsNone(report["library_health"])
            self.assertIn("empty", report["library_health_error"])


if __name__ == "__main__":
    unittest.main()
