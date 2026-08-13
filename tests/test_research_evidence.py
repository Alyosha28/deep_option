"""投研证据整理与影响研判层测试（不写审计日志、不落文件）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.decision_pipeline import DEFAULT_INPUT, load_frozen_snapshot, run_pipeline
from src.research_evidence import (
    DEFAULT_BACKTEST,
    DEFAULT_ITEMS,
    build_research_evidence,
    classify_item,
    load_earnings_move_stats,
    load_research_items,
)


class ResearchItemLoadingTests(unittest.TestCase):
    def test_hero_fixture_loads_with_hashes(self):
        items = load_research_items(DEFAULT_ITEMS)

        self.assertEqual(len(items), 5)
        self.assertTrue(all(item["synthetic"] for item in items))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in items))
        kinds = {item["kind"] for item in items}
        self.assertEqual(
            kinds,
            {"announcement", "earnings", "news", "research", "industry"},
        )

    def test_invalid_items_are_rejected(self):
        cases = [
            {"id": "a", "kind": "news", "published_at": "2026-08-01", "source": "s"},
            {"id": "a", "kind": "unknown", "title": "t", "published_at": "2026-08-01", "source": "s"},
            {
                "id": "a",
                "kind": "news",
                "title": "t",
                "published_at": "2026-08-01",
                "source": "s",
                "order_id": "123",
            },
        ]
        for item in cases:
            with self.subTest(item=item):
                with tempfile.TemporaryDirectory() as temp_dir:
                    bad_path = Path(temp_dir, "bad.json")
                    bad_path.write_text(
                        json.dumps({"items": [item]}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        load_research_items(bad_path)

    def test_duplicate_ids_are_rejected(self):
        item = {
            "id": "a",
            "kind": "news",
            "title": "t",
            "published_at": "2026-08-01",
            "source": "s",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = Path(temp_dir, "dup.json")
            bad_path.write_text(
                json.dumps({"items": [item, dict(item)]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_research_items(bad_path)

    def test_empty_publish_time_requires_url_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            no_url = Path(temp_dir, "no_url.json")
            no_url.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "a",
                                "kind": "news",
                                "title": "t",
                                "published_at": "",
                                "source": "s",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "URL anchors"):
                load_research_items(no_url)

            with_url = Path(temp_dir, "with_url.json")
            with_url.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "b",
                                "kind": "news",
                                "title": "t",
                                "published_at": "",
                                "source": "s",
                                "url": "https://example.com/x",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = load_research_items(with_url)

        self.assertTrue(items[0]["publish_time_unknown"])


class ClassificationTests(unittest.TestCase):
    def test_sentiment_rules_are_deterministic(self):
        bullish = classify_item(
            {"id": "a", "kind": "news", "title": "业绩超预期，增长强劲", "body": ""}
        )
        bearish = classify_item(
            {"id": "b", "kind": "news", "title": "监管审查与增速下滑担忧", "body": ""}
        )
        mixed = classify_item(
            {
                "id": "c",
                "kind": "research",
                "title": "上调目标价与下调盈利预测并存",
                "body": "",
            }
        )

        self.assertEqual(bullish["sentiment"], "bullish")
        self.assertEqual(bearish["sentiment"], "bearish")
        self.assertEqual(mixed["sentiment"], "mixed")
        self.assertTrue(bullish["relevant"])
        self.assertTrue(bearish["relevant"])

    def test_neutral_item_without_keywords(self):
        neutral = classify_item(
            {"id": "d", "kind": "industry", "title": "板块成交数据更新", "body": ""}
        )
        self.assertEqual(neutral["sentiment"], "neutral")
        self.assertFalse(neutral["relevant"])


class EarningsMoveStatsTests(unittest.TestCase):
    def test_stats_are_extracted_from_backtest(self):
        stats = load_earnings_move_stats(DEFAULT_BACKTEST)

        self.assertEqual(stats["n_periods"], 11)
        self.assertGreater(stats["realized_d1_median_pct"], 0)
        self.assertLess(stats["realized_d1_median_pct"], 20)
        self.assertEqual(len(stats["realized_d1_pct"]), 11)

    def test_earnings_fields_are_merged(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        stats = load_earnings_move_stats(DEFAULT_BACKTEST, data["payload"]["earnings"])

        self.assertEqual(stats["expected_move_pct"], 3.916)
        self.assertIsNotNone(stats["last_report_iv_crush"])
        self.assertIsNotNone(stats["history_report_iv_crush"])


class ImpactAssessmentTests(unittest.TestCase):
    def test_hero_bundle_is_complete_and_honest(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        evidence = build_research_evidence(
            data["underlying"],
            data["payload"]["earnings"],
            DEFAULT_BACKTEST,
            DEFAULT_ITEMS,
        )

        self.assertEqual(evidence["underlying"], "HK.00700")
        self.assertTrue(evidence["digest"]["synthetic_only"])
        self.assertIn(
            evidence["stock_price_impact"]["verdict"],
            {"BULLISH", "BEARISH", "MIXED", "NEUTRAL", "UNKNOWN"},
        )
        self.assertIn(
            evidence["option_impact"]["verdict"],
            {
                "COMPRESSION_RISK_HIGH",
                "COMPRESSION_RISK_MODERATE",
                "COMPRESSION_NEUTRAL",
                "UNKNOWN",
            },
        )
        self.assertTrue(evidence["option_impact"]["checks"])
        self.assertIn("非投资建议", evidence["disclaimer"])
        json.dumps(evidence, ensure_ascii=False)


class PipelineIntegrationTests(unittest.TestCase):
    def test_pipeline_without_research_keeps_evidence_off(self):
        card = run_pipeline(DEFAULT_INPUT, audit_enabled=False, write_card=False)

        self.assertFalse(card["research_evidence"]["available"])

    def test_pipeline_with_research_items_attaches_evidence(self):
        card = run_pipeline(
            DEFAULT_INPUT,
            research_items_path=DEFAULT_ITEMS,
            audit_enabled=False,
            write_card=False,
        )

        self.assertTrue(card["research_evidence"]["available"])
        self.assertEqual(card["research_evidence"]["digest"]["item_count"], 5)
        self.assertEqual(card["verdict"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
