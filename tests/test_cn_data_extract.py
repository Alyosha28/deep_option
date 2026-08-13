"""中国官方数值页解析测试：全部离线，不联网、不写正式库。"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timezone

from src.cn_data_extract import (
    enrich_cn_item,
    extract_numeric_claims,
    extract_page_text,
)
from src.cn_source_parsers import CnFeedItem, parse_cn_html_list
from src.macro_source_watcher import run_once
from src.policy_library import load_policy_library

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

LIST_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
  <a href="./202608/t20260809_1965008.html">2026年7月份居民消费价格同比上涨0.5%</a>
  <a href="./202608/t20260809_1965007.html">2026年7月份工业生产者出厂价格同比上涨3.5%</a>
</body></html>
"""

STATS_DETAIL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<div class="content">
  <p>2026年7月份，全国居民消费价格同比上涨0.5%。其中，城市上涨0.5%，农村上涨0.6%。</p>
  <p>全国工业生产者出厂价格同比下降1.6%，环比下降0.2%。</p>
  <script>var mock="居民消费价格同比上涨99.9%";</script>
</div>
</body></html>
"""

PBC_DETAIL = """<html><body>
<p>2026年7月20日贷款市场报价利率（LPR）为：1年期LPR为3.0%，5年期以上LPR为3.5%。</p>
</body></html>
"""

TRADE_DETAIL = """<html><body>
<p>据海关统计，今年前7个月我国货物贸易进出口总值25.24万亿元人民币，同比增长5.6%。</p>
</body></html>
"""

JOBS_DETAIL = """<html><body>
<p>7月份，全国城镇调查失业率为5.0%，比上月下降0.1个百分点。</p>
</body></html>
"""


def _cn_item(source: dict) -> CnFeedItem:
    return parse_cn_html_list(LIST_HTML, source, NOW)[0]


class PageTextTests(unittest.TestCase):
    def test_scripts_are_excluded(self):
        text = extract_page_text(STATS_DETAIL)

        self.assertIn("居民消费价格同比上涨0.5%", text)
        self.assertNotIn("99.9%", text)


class NumericClaimTests(unittest.TestCase):
    def test_inflation_claims_extract_cpi_and_ppi(self):
        claims = extract_numeric_claims(extract_page_text(STATS_DETAIL), "inflation")
        by_metric = {claim["metric"]: claim for claim in claims}

        self.assertEqual(by_metric["CPI 同比"]["value"], "0.5")
        self.assertEqual(by_metric["CPI 同比"]["unit"], "%")
        self.assertEqual(by_metric["PPI 同比"]["value"], "-1.6")
        self.assertIn("同比上涨0.5%", by_metric["CPI 同比"]["snippet"])

    def test_lpr_claims_extract_both_tenors(self):
        claims = extract_numeric_claims(extract_page_text(PBC_DETAIL), "rates")
        by_metric = {claim["metric"]: claim for claim in claims}

        self.assertEqual(by_metric["1年期LPR"]["value"], "3.0")
        self.assertEqual(by_metric["5年期以上LPR"]["value"], "3.5")

    def test_trade_claims_extract_value_and_growth(self):
        claims = extract_numeric_claims(extract_page_text(TRADE_DETAIL), "trade")
        by_metric = {claim["metric"]: claim for claim in claims}

        self.assertEqual(by_metric["进出口总值"]["value"], "25.24")
        self.assertEqual(by_metric["进出口总值"]["unit"], "万亿元")
        self.assertEqual(by_metric["进出口同比"]["value"], "5.6")

    def test_jobs_claim_extracts_unemployment(self):
        claims = extract_numeric_claims(extract_page_text(JOBS_DETAIL), "jobs")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["metric"], "城镇调查失业率")
        self.assertEqual(claims[0]["value"], "5.0")

    def test_no_match_returns_empty(self):
        claims = extract_numeric_claims("本页没有可提取的数值声明。", "inflation")
        self.assertEqual(claims, [])


class EnrichTests(unittest.TestCase):
    def _source(self) -> dict:
        return {
            "id": "stats",
            "kind": "cn_html_list",
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "label": "国家统计局数据发布",
            "topics": ["inflation"],
            "enrich_numeric": True,
        }

    def test_enrich_appends_verified_claims(self):
        item = _cn_item(self._source())
        enriched = enrich_cn_item(
            item,
            self._source(),
            lambda url, user_agent: STATS_DETAIL,
            "test-agent",
            NOW,
        )

        self.assertGreaterEqual(len(enriched.value_claims), 1)
        for claim in enriched.value_claims:
            self.assertEqual(claim["verification"], "VERIFIED")
            self.assertIn("stats.gov.cn", claim["source_url"])
            self.assertTrue(claim["retrieved_at"])

    def test_non_allowlisted_detail_host_is_skipped(self):
        item = _cn_item(self._source())
        bad_item = dataclasses.replace(
            item, link="https://evil.example.com/detail.html"
        )
        enriched = enrich_cn_item(
            bad_item,
            self._source(),
            lambda url, user_agent: "",
            "test-agent",
            NOW,
        )
        self.assertEqual(enriched.value_claims, ())

    def test_fetch_failure_returns_original(self):
        item = _cn_item(self._source())

        def fetcher(url: str, user_agent: str) -> str:
            raise ConnectionError("detail page unreachable")

        enriched = enrich_cn_item(item, self._source(), fetcher, "test-agent", NOW)
        self.assertEqual(enriched, item)


class WatcherIntegrationTests(unittest.TestCase):
    def test_run_once_enriches_cn_items(self):
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
                    "enrich_numeric": True,
                }
            ],
        }

        def fetcher(url: str, user_agent: str) -> str:
            if url.endswith("/sj/zxfb/"):
                return LIST_HTML
            return STATS_DETAIL

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(config, temp_dir, fetcher=fetcher, now=NOW)
            by_id = {source["id"]: source for source in report["sources"]}
            self.assertEqual(by_id["stats"]["enrich_attempts"], 2)
            self.assertEqual(by_id["stats"]["enriched"], 2)
            library = load_policy_library(temp_dir)
            self.assertEqual(len(library["events"]), 2)
            for event in library["events"]:
                self.assertEqual(event["status"], "DRAFT")
                self.assertEqual(event["facts"][0]["verification"], "PENDING")
                verified = [
                    fact for fact in event["facts"] if fact["verification"] == "VERIFIED"
                ]
                self.assertGreaterEqual(len(verified), 1)
                self.assertTrue(
                    any("同比" in fact["fact"] for fact in verified)
                )


if __name__ == "__main__":
    unittest.main()
