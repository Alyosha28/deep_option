"""宏观来源自动监控测试（全部离线：本地 fixture，不联网、不写正式库）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone

from src.macro_source_watcher import (
    build_event,
    classify_topic,
    load_config,
    parse_bls_json,
    parse_fred_csv,
    parse_rss_feed,
    run_once,
    validate_config,
)
from src.policy_library import load_policy_library

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Federal Reserve</title>
    <item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20250917a.htm</link>
      <pubDate>Tue, 17 Sep 2025 14:00:00 GMT</pubDate>
      <description>FOMC decided to maintain the target range for the federal funds rate.</description>
    </item>
    <item>
      <title>Banking supervision report published</title>
      <link>https://www.federalreserve.gov/supervision/report.htm</link>
      <pubDate>Mon, 15 Sep 2025 09:00:00 GMT</pubDate>
      <description>Quarterly supervision highlights.</description>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Form 8-K filing</title>
    <link href="https://www.sec.gov/Archives/edgar/data/0001/0000001.htm"/>
    <updated>2026-01-05T10:00:00Z</updated>
    <summary>Form 8-K for Tencent Holdings Limited.</summary>
  </entry>
</feed>
"""

FRED_FIXTURE = """observation_date,CPIAUCSL,DFF
2026-01-01,320.0,4.33
2026-02-01,321.5,4.25
"""

BLS_FIXTURE = json.dumps(
    {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CUUR0000SA0",
                    "data": [{"year": "2026", "period": "M02", "value": "320.5"}],
                }
            ]
        },
    }
)

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _base_config() -> dict:
    return {
        "user_agent": "test-agent/1.0",
        "max_items_per_source": 10,
        "sources": [],
    }


class ConfigTests(unittest.TestCase):
    def test_http_url_is_rejected(self):
        config = _base_config()
        config["sources"] = [
            {
                "id": "bad",
                "kind": "rss",
                "url": "http://www.federalreserve.gov/feed.xml",
                "label": "bad",
                "topics": ["rates"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "https"):
            validate_config(config)

    def test_non_allowlisted_host_is_rejected(self):
        config = _base_config()
        config["sources"] = [
            {
                "id": "bad",
                "kind": "rss",
                "url": "https://example.com/feed.xml",
                "label": "bad",
                "topics": ["rates"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            validate_config(config)

    def test_unknown_kind_is_rejected(self):
        config = _base_config()
        config["sources"] = [
            {
                "id": "bad",
                "kind": "html",
                "url": "https://www.federalreserve.gov/",
                "label": "bad",
                "topics": ["rates"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_config(config)

    def test_valid_config_roundtrip(self):
        config = _base_config()
        config["sources"] = [
            {
                "id": "fred",
                "kind": "fred_csv",
                "url": "https://fred.stlouisfed.org/graph/fredgraph.csv",
                "label": "FRED",
                "series": [{"id": "CPIAUCSL", "topic": "inflation", "label": "CPI"}],
            }
        ]
        validated = validate_config(config)
        self.assertEqual(validated["sources"][0]["id"], "fred")


class ClassificationTests(unittest.TestCase):
    def test_topic_keywords(self):
        self.assertEqual(classify_topic("CPI inflation report", ["inflation"]), "inflation")
        self.assertEqual(classify_topic("FOMC statement", ["rates"]), "rates")
        self.assertEqual(classify_topic("关税升级", ["trade"]), "trade")
        self.assertIsNone(classify_topic("unrelated", ["rates"]))


class ParserTests(unittest.TestCase):
    def test_rss_feed_filters_by_topic(self):
        source = {"id": "fed", "kind": "rss", "label": "Fed", "topics": ["rates"]}
        items = parse_rss_feed(RSS_FIXTURE, source, NOW)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].event_type, "monetary-policy")
        self.assertEqual(items[0].verification, "PENDING")
        self.assertEqual(items[0].published_date, "2025-09-17")
        self.assertIn("federalreserve.gov", items[0].link)

    def test_atom_feed_parses_sec_filing(self):
        source = {"id": "sec", "kind": "sec_atom", "label": "SEC", "topics": ["corporate"]}
        items = parse_rss_feed(ATOM_FIXTURE, source, NOW)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].event_type, "corporate-events")
        self.assertEqual(items[0].published_date, "2026-01-05")

    def test_fred_csv_takes_latest_observation(self):
        series = [
            {"id": "CPIAUCSL", "topic": "inflation", "label": "美国 CPI"},
            {"id": "DFF", "topic": "rates", "label": "联邦基金利率"},
        ]
        items = parse_fred_csv(FRED_FIXTURE, series, NOW)

        self.assertEqual(len(items), 2)
        cpi = next(item for item in items if "CPIAUCSL" in item.event_id)
        self.assertEqual(cpi.verification, "VERIFIED")
        self.assertEqual(cpi.published_date, "2026-02-01")
        self.assertEqual(cpi.event_id, "auto-fred-CPIAUCSL-2026-02-01")
        self.assertIn("321.5", cpi.value_note)

    def test_bls_json_parses_latest_value(self):
        series = [{"id": "CUUR0000SA0", "topic": "inflation", "label": "CPI-U"}]
        items = parse_bls_json(BLS_FIXTURE, series, NOW)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].verification, "VERIFIED")
        self.assertEqual(items[0].event_id, "auto-bls-CUUR0000SA0-2026-02")


class BuildEventTests(unittest.TestCase):
    def test_build_event_produces_draft_with_fact(self):
        source = {"id": "fed", "kind": "rss", "label": "美联储", "topics": ["rates"]}
        items = parse_rss_feed(RSS_FIXTURE, source, NOW)
        event = build_event(items[0], source, NOW)

        self.assertEqual(event["status"], "DRAFT")
        self.assertEqual(event["type"], "monetary-policy")
        self.assertEqual(event["facts"][0]["verification"], "PENDING")
        self.assertEqual(event["facts"][0]["source_url"], items[0].link)
        self.assertTrue(event["ingest"]["automated"])


class RunOnceTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "user_agent": "test-agent/1.0",
            "max_items_per_source": 10,
            "sources": [
                {
                    "id": "fred",
                    "kind": "fred_csv",
                    "url": "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    "label": "FRED",
                    "series": [{"id": "CPIAUCSL", "topic": "inflation", "label": "CPI"}],
                },
                {
                    "id": "fed",
                    "kind": "rss",
                    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
                    "label": "美联储",
                    "topics": ["rates"],
                },
            ],
        }

    def _fake_fetcher(self) -> dict:
        return {
            "https://fred.stlouisfed.org/graph/fredgraph.csv": FRED_FIXTURE,
            "https://www.federalreserve.gov/feeds/press_all.xml": RSS_FIXTURE,
        }

    def test_run_once_ingests_then_dedupes(self):
        config = self._config()
        responses = self._fake_fetcher()

        def fetcher(url: str, user_agent: str) -> str:
            return responses[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(
                config,
                temp_dir,
                fetcher=fetcher,
                now=NOW,
            )
            self.assertEqual(report["new_event_count"], 2)
            self.assertEqual(report["library_health"]["fact_count"], 2)
            library = load_policy_library(temp_dir)
            self.assertEqual(len(library["events"]), 2)
            self.assertEqual(
                {event["status"] for event in library["events"]},
                {"DRAFT"},
            )

            second = run_once(
                config,
                temp_dir,
                fetcher=fetcher,
                now=NOW,
            )
            self.assertEqual(second["new_event_count"], 0)
            self.assertEqual(
                sum(item["skipped_duplicates"] for item in second["sources"]),
                2,
            )

    def test_dry_run_does_not_write(self):
        config = self._config()
        responses = self._fake_fetcher()

        def fetcher(url: str, user_agent: str) -> str:
            return responses[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(
                config,
                temp_dir,
                fetcher=fetcher,
                dry_run=True,
                now=NOW,
            )
            self.assertEqual(report["new_event_count"], 2)
            self.assertTrue(report["dry_run"])
            with self.assertRaisesRegex(ValueError, "empty"):
                load_policy_library(temp_dir)

    def test_source_failure_is_recorded_not_fatal(self):
        config = self._config()
        responses = self._fake_fetcher()

        def fetcher(url: str, user_agent: str) -> str:
            if "fred" in url:
                raise ConnectionError("network down")
            return responses[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(
                config,
                temp_dir,
                fetcher=fetcher,
                now=NOW,
            )
            by_id = {source["id"]: source for source in report["sources"]}
            self.assertFalse(by_id["fred"]["reachable"])
            self.assertIn("network down", by_id["fred"]["error"])
            self.assertTrue(by_id["fed"]["reachable"])
            self.assertEqual(report["new_event_count"], 1)

    def test_disabled_source_is_skipped(self):
        config = self._config()
        config["sources"][1]["enabled"] = False

        def fetcher(url: str, user_agent: str) -> str:
            if "fred" in url:
                return FRED_FIXTURE
            raise AssertionError("disabled source should not be fetched")

        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_once(config, temp_dir, fetcher=fetcher, now=NOW)
            by_id = {source["id"]: source for source in report["sources"]}
            self.assertTrue(by_id["fed"]["skipped"])
            self.assertIn("disabled", by_id["fed"]["error"])
            self.assertEqual(report["new_event_count"], 1)


class LoadConfigTests(unittest.TestCase):
    def test_project_config_validates(self):
        config = load_config("data/sources_config.json")
        self.assertGreaterEqual(len(config["sources"]), 4)
        kinds = {source["kind"] for source in config["sources"]}
        self.assertIn("fred_csv", kinds)
        self.assertIn("rss", kinds)


if __name__ == "__main__":
    unittest.main()
