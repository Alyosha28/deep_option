"""政策事件库测试（不联网、不写正式库、不落文件）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.macro_assessment import DEFAULT_POLICY
from src.policy_library import (
    DEFAULT_POLICY_DIR,
    event_verification_summary,
    load_policy_library,
    policy_health_report,
    select_policy_events,
    upsert_policy_event,
    validate_policy_event,
)

CURATED_IDS = {
    "us-cn-tariff-2025-04",
    "fed-fomc-2025-05",
    "bls-nfp-2025-04",
    "us-chip-controls-2025-05",
}


def _curated_library() -> tempfile.TemporaryDirectory[str]:
    """复制 4 个精选 ACTIVE 事件到临时目录，隔离自动入库的 DRAFT。"""

    temp_dir = tempfile.TemporaryDirectory()
    for path in sorted(DEFAULT_POLICY_DIR.glob("*.json")):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event["id"] in CURATED_IDS:
            Path(temp_dir.name, path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return temp_dir


class PolicyLibraryTests(unittest.TestCase):
    def test_library_loads_four_events_with_unique_ids(self):
        with _curated_library() as temp_dir:
            library = load_policy_library(temp_dir)

            self.assertEqual(library["event_count"], 4)
            ids = [event["id"] for event in library["events"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(set(ids), CURATED_IDS)

    def test_events_have_status_and_updated_at(self):
        library = load_policy_library(DEFAULT_POLICY_DIR)

        for event in library["events"]:
            self.assertIn(event["status"], {"ACTIVE", "ARCHIVED", "DRAFT", "FAILED"})
            self.assertTrue(str(event["updated_at"]).strip())

    def test_missing_directory_raises(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_policy_library(Path(tempfile.gettempdir()) / "no-such-policy-library-xyz")

    def test_empty_directory_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "empty"):
                load_policy_library(temp_dir)

    def test_missing_status_is_rejected(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        del event["status"]
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = Path(temp_dir, "bad.json")
            bad_path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "status"):
                load_policy_library(temp_dir)

    def test_invalid_verification_state_is_rejected(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        event["facts"][0]["verification"] = "CONFIRMED"
        with self.assertRaisesRegex(ValueError, "verification"):
            validate_policy_event(event)

    def test_duplicate_ids_are_rejected(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            Path(temp_dir, "b.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_policy_library(temp_dir)

    def test_health_report_counts_verification_states(self):
        with _curated_library() as temp_dir:
            library = load_policy_library(temp_dir)
            report = policy_health_report(library)

            self.assertEqual(report["event_count"], 4)
            self.assertEqual(report["fact_count"], 14)
            self.assertGreaterEqual(report["verification"]["VERIFIED"], 1)
            self.assertGreaterEqual(report["verification"]["PENDING"], 10)
            self.assertEqual(report["verification"]["FAILED"], 0)
            self.assertEqual(report["library_status"], "PARTIALLY_VERIFIED")
            self.assertEqual(report["facts_without_source"], [])
            self.assertEqual(len(report["facts_without_url"]), 1)
            self.assertEqual(report["facts_without_url"][0]["event_id"], "bls-nfp-2025-04")

    def test_health_report_lists_recently_promoted(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        event["id"] = "auto-promoted-test"
        event["promoted_at"] = "2026-08-13T09:00:00+00:00"
        event["promoted_by"] = "manual-review"
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            report = policy_health_report(load_policy_library(temp_dir))

            self.assertEqual(len(report["recently_promoted"]), 1)
            self.assertEqual(report["recently_promoted"][0]["id"], "auto-promoted-test")
            self.assertEqual(report["recently_promoted"][0]["promoted_by"], "manual-review")

    def test_tariff_event_is_unverified_pending(self):
        library = load_policy_library(DEFAULT_POLICY_DIR)
        event = next(item for item in library["events"] if item["id"] == "us-cn-tariff-2025-04")

        summary = event_verification_summary(event)
        self.assertEqual(summary["status"], "UNVERIFIED")
        self.assertEqual(summary["counts"]["PENDING"], 6)
        self.assertEqual(summary["counts"]["VERIFIED"], 0)

    def test_select_by_policy_id(self):
        with _curated_library() as temp_dir:
            library = load_policy_library(temp_dir)

            primary, others, selection = select_policy_events(library, "fed-fomc-2025-05")
            self.assertEqual(primary["id"], "fed-fomc-2025-05")
            self.assertEqual(len(others), 3)
            self.assertEqual(selection, "policy-id-filter")

    def test_select_unknown_policy_id_raises(self):
        library = load_policy_library(DEFAULT_POLICY_DIR)
        with self.assertRaisesRegex(ValueError, "not found"):
            select_policy_events(library, "no-such-event")

    def test_select_default_uses_latest_active_event(self):
        with _curated_library() as temp_dir:
            library = load_policy_library(temp_dir)

            primary, _, selection = select_policy_events(library)
            self.assertEqual(selection, "latest-by-date")
            self.assertEqual(primary["id"], "us-chip-controls-2025-05")

    def test_draft_events_are_not_selected_by_default(self):
        library = load_policy_library(DEFAULT_POLICY_DIR)
        draft = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        draft["id"] = "auto-draft-sample"
        draft["status"] = "DRAFT"
        draft["date"] = "2099-01-01"
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "draft.json").write_text(
                json.dumps(draft, ensure_ascii=False), encoding="utf-8"
            )
            for path in sorted(DEFAULT_POLICY_DIR.glob("*.json")):
                Path(temp_dir, path.name).write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            library = load_policy_library(temp_dir)
            primary, _, _ = select_policy_events(library)
            self.assertNotEqual(primary["id"], "auto-draft-sample")

    def test_upsert_roundtrip(self):
        event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            target = upsert_policy_event(event, temp_dir)
            self.assertEqual(target.name, "us-cn-tariff-2025-04.json")
            reloaded = load_policy_library(temp_dir)
            self.assertEqual(reloaded["event_count"], 1)
            self.assertEqual(reloaded["events"][0]["id"], event["id"])


if __name__ == "__main__":
    unittest.main()
