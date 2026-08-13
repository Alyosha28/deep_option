"""宏观研判层测试（不写审计日志、不落文件、不联网）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.decision_pipeline import DEFAULT_INPUT, load_frozen_snapshot, run_pipeline
from src.macro_assessment import (
    DEFAULT_ITEMS,
    DEFAULT_POLICY,
    analyze_policy_event,
    assess_iv_emotion,
    build_macro_assessment,
    load_policy_event,
    quantify_sentiment,
)
from src.policy_library import DEFAULT_POLICY_DIR
from src.research_evidence import load_research_items

CURATED_IDS = {
    "us-cn-tariff-2025-04",
    "fed-fomc-2025-05",
    "bls-nfp-2025-04",
    "us-chip-controls-2025-05",
}


def _curated_library() -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    for path in sorted(DEFAULT_POLICY_DIR.glob("*.json")):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event["id"] in CURATED_IDS:
            Path(temp_dir.name, path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return temp_dir


class SentimentQuantificationTests(unittest.TestCase):
    def test_hero_items_produce_bounded_index(self):
        items = load_research_items(DEFAULT_ITEMS)
        result = quantify_sentiment(items)

        self.assertGreaterEqual(result["index"], -100)
        self.assertLessEqual(result["index"], 100)
        self.assertIn(
            result["verdict"],
            {"BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"},
        )
        self.assertEqual(len(result["per_item"]), 5)
        self.assertEqual(sum(result["counts"].values()), 5)

    def test_empty_items_are_unknown(self):
        result = quantify_sentiment([])

        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIsNone(result["index"])


class IvEmotionTests(unittest.TestCase):
    def test_hero_snapshot_produces_full_readout(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        result = assess_iv_emotion(
            data["payload"]["earnings"],
            data["payload"]["legs"],
        )

        self.assertIn(
            result["state"],
            {"CALM", "ELEVATED", "HIGH", "UNKNOWN"},
        )
        self.assertEqual(result["iv"], 37.392)
        self.assertEqual(result["iv_rank"], 72.918)
        self.assertEqual(result["skew_verdict"], "NEUTRAL")
        self.assertEqual(len(result["mechanisms"]), 5)
        self.assertIsNotNone(result["event_days_until"])


class PolicyEventTests(unittest.TestCase):
    def test_tariff_case_loads_and_validates(self):
        event = load_policy_event(DEFAULT_POLICY)

        self.assertEqual(event["id"], "us-cn-tariff-2025-04")
        self.assertEqual(len(event["facts"]), 6)
        self.assertEqual(len(event["tensions"]), 3)
        self.assertEqual(len(event["verdict_reads"]), 2)
        self.assertFalse(event.get("case_study", False))

    def test_missing_principal_is_rejected(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        for tension in event["tensions"]:
            tension["principal"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = Path(temp_dir, "bad_policy.json")
            bad_path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly one principal"):
                load_policy_event(bad_path)

    def test_analysis_identifies_principal_contradiction(self):
        analysis = analyze_policy_event(load_policy_event(DEFAULT_POLICY))

        self.assertEqual(len(analysis["contradictions"]), 3)
        self.assertEqual(analysis["principal_contradiction"]["id"], "t2")
        self.assertEqual(len(analysis["verdict_reads"]), 2)
        self.assertEqual(len(analysis["political_economy"]["checks"]), 4)
        self.assertEqual(analysis["qiu_shi"]["facts_first"]["status"], "VERIFYING")
        self.assertEqual(analysis["qiu_shi"]["facts_first"]["verification"], "UNVERIFIED")
        self.assertGreaterEqual(len(analysis["qiu_shi"]["falsification"]), 3)
        self.assertGreaterEqual(len(analysis["qiu_shi"]["monitor"]), 3)


class MacroAssessmentTests(unittest.TestCase):
    def test_full_assessment_is_complete_and_json_safe(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        assessment = build_macro_assessment(
            data["payload"],
            DEFAULT_ITEMS,
            DEFAULT_POLICY,
        )

        self.assertEqual(assessment["underlying"], "HK.00700")
        self.assertIn("sentiment", assessment)
        self.assertIn("iv_emotion", assessment)
        self.assertIn("policy_analysis", assessment)
        self.assertEqual(len(assessment["macro_judgment"]["scenarios"]), 3)
        self.assertIn(
            assessment["macro_judgment"]["confidence"],
            {"HIGH", "MEDIUM", "LOW"},
        )
        self.assertIn("非投资建议", assessment["disclaimer"])
        json.dumps(assessment, ensure_ascii=False)


class PipelineIntegrationTests(unittest.TestCase):
    def test_macro_section_is_off_by_default(self):
        card = run_pipeline(DEFAULT_INPUT, audit_enabled=False, write_card=False)

        self.assertFalse(card["macro_assessment"]["available"])

    def test_macro_section_attaches_when_requested(self):
        card = run_pipeline(
            DEFAULT_INPUT,
            research_items_path=DEFAULT_ITEMS,
            macro_policy_path=DEFAULT_POLICY,
            audit_enabled=False,
            write_card=False,
        )

        self.assertTrue(card["macro_assessment"]["available"])
        self.assertEqual(card["macro_assessment"]["policy_analysis"]["event_id"], "us-cn-tariff-2025-04")
        self.assertEqual(card["verdict"], "NO_TRADE")

    def test_macro_library_mode_attaches_with_health_report(self):
        with _curated_library() as temp_dir:
            card = run_pipeline(
                DEFAULT_INPUT,
                research_items_path=DEFAULT_ITEMS,
                macro_policy_path=temp_dir,
                audit_enabled=False,
                write_card=False,
            )

            self.assertTrue(card["macro_assessment"]["available"])
            policy = card["macro_assessment"]["policy_analysis"]
            self.assertEqual(policy["event_id"], "us-chip-controls-2025-05")
            self.assertEqual(policy["library"]["event_count"], 4)
            self.assertEqual(policy["library"]["selection"], "latest-by-date")
            self.assertIn("health_report", policy["library"])
            self.assertGreaterEqual(
                policy["library"]["health_report"]["verification"]["VERIFIED"], 1
            )

    def test_macro_library_filter_by_policy_id(self):
        with _curated_library() as temp_dir:
            card = run_pipeline(
                DEFAULT_INPUT,
                research_items_path=DEFAULT_ITEMS,
                macro_policy_path=temp_dir,
                macro_policy_id="fed-fomc-2025-05",
                audit_enabled=False,
                write_card=False,
            )

            policy = card["macro_assessment"]["policy_analysis"]
            self.assertEqual(policy["event_id"], "fed-fomc-2025-05")
            self.assertEqual(policy["library"]["selection"], "policy-id-filter")
            self.assertEqual(len(policy["library"]["additional_events"]), 3)

    def test_macro_requires_research_items(self):
        with self.assertRaisesRegex(ValueError, "research-items"):
            run_pipeline(
                DEFAULT_INPUT,
                macro_policy_path=DEFAULT_POLICY,
                audit_enabled=False,
                write_card=False,
            )

    def test_policy_id_requires_macro_policy_path(self):
        with self.assertRaisesRegex(ValueError, "policy-id requires --macro-policy"):
            run_pipeline(
                DEFAULT_INPUT,
                macro_policy_id="fed-fomc-2025-05",
                audit_enabled=False,
                write_card=False,
            )


if __name__ == "__main__":
    unittest.main()
