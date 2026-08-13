"""DRAFT 政策事件工作流测试（离线、临时目录，不写正式库）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.macro_assessment import DEFAULT_POLICY
from src.policy_draft_workflow import (
    build_promotion_notification,
    completeness_check,
    promote_draft,
    summarize_draft,
)
from src.policy_library import load_policy_library, set_event_status


def _draft_with_facts_only() -> dict:
    return {
        "id": "auto-draft-test",
        "name": "测试 DRAFT 事件",
        "date": "2026-08-13",
        "type": "macro-data",
        "status": "DRAFT",
        "updated_at": "2026-08-13T00:00:00+08:00",
        "description": "自动抓取的事件草稿。",
        "facts": [
            {
                "id": "f1",
                "date": "2026-08-13",
                "fact": "官方序列最新观测。",
                "source": "测试来源",
                "source_url": "https://example.com/official",
                "verification": "PENDING",
                "retrieved_at": "2026-08-13T00:00:00+08:00",
            }
        ],
        "sources": [],
        "ingest": {"automated": True},
    }


def _complete_draft() -> dict:
    event = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
    event["id"] = "auto-complete-draft"
    event["status"] = "DRAFT"
    return event


class SummarizeTests(unittest.TestCase):
    def test_summarize_lists_missing_analysis_fields(self):
        summary = summarize_draft(_draft_with_facts_only())

        self.assertEqual(summary["event_id"], "auto-draft-test")
        self.assertEqual(summary["source_links"], ["https://example.com/official"])
        self.assertEqual(summary["verification"]["status"], "UNVERIFIED")
        self.assertIn("tensions（非空列表）", summary["missing_analysis_fields"])
        self.assertIn("verdict_reads（非空列表）", summary["missing_analysis_fields"])
        self.assertFalse(summary["ready_for_promotion"])

    def test_summarize_complete_event_is_ready(self):
        summary = summarize_draft(_complete_draft())

        self.assertEqual(summary["missing_analysis_fields"], [])
        self.assertTrue(summary["ready_for_promotion"])


class CompletenessTests(unittest.TestCase):
    def test_complete_draft_is_ready(self):
        check = completeness_check(_complete_draft())

        self.assertTrue(check["ready"])
        self.assertEqual(check["missing"], [])

    def test_missing_tensions_is_flagged(self):
        event = _draft_with_facts_only()
        check = completeness_check(event)

        self.assertFalse(check["ready"])
        self.assertIn("tensions 为空", check["missing"])

    def test_principal_count_must_be_one(self):
        event = _complete_draft()
        for tension in event["tensions"]:
            tension["principal"] = False
        check = completeness_check(event)

        self.assertFalse(check["ready"])
        self.assertTrue(
            any("principal=true 的数量必须恰好为 1" in item for item in check["missing"])
        )

    def test_empty_verdict_reads_is_flagged(self):
        event = _complete_draft()
        event["verdict_reads"] = []
        check = completeness_check(event)

        self.assertFalse(check["ready"])
        self.assertTrue(
            any("verdict_reads 为空" in item for item in check["missing"])
        )

    def test_failed_verification_blocks_promotion(self):
        event = _complete_draft()
        event["facts"][0]["verification"] = "FAILED"
        check = completeness_check(event)

        self.assertFalse(check["ready"])
        self.assertTrue(any("FAILED" in item for item in check["missing"]))


class PromoteTests(unittest.TestCase):
    def test_promote_rejects_incomplete_draft(self):
        result = promote_draft(_draft_with_facts_only())

        self.assertFalse(result["promoted"])
        self.assertGreaterEqual(len(result["reasons"]), 1)

    def test_promote_accepts_complete_draft(self):
        result = promote_draft(_complete_draft())

        self.assertTrue(result["promoted"])
        self.assertEqual(result["status"], "ACTIVE")

    def test_promotion_notification_payload(self):
        event = _complete_draft()
        event["promoted_at"] = "2026-08-13T09:00:00+00:00"
        event["promoted_by"] = "manual-review"
        notification = build_promotion_notification(event)

        self.assertEqual(notification["event_id"], "auto-complete-draft")
        self.assertEqual(notification["status"], "ACTIVE")
        self.assertEqual(notification["promoted_at"], "2026-08-13T09:00:00+00:00")


class SetEventStatusTests(unittest.TestCase):
    def test_roundtrip_preserves_other_fields(self):
        event = _complete_draft()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, f"{event['id']}.json")
            path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")

            updated = set_event_status(temp_dir, event["id"], "ACTIVE")
            self.assertEqual(updated["status"], "ACTIVE")
            self.assertEqual(updated["name"], event["name"])
            self.assertEqual(updated["facts"], event["facts"])

            reloaded = load_policy_library(temp_dir)["events"][0]
            self.assertEqual(reloaded["status"], "ACTIVE")
            self.assertTrue(str(reloaded["updated_at"]).strip())

    def test_invalid_status_is_rejected(self):
        event = _complete_draft()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "invalid status"):
                set_event_status(temp_dir, event["id"], "PROMOTED")

    def test_unknown_event_id_is_rejected(self):
        event = _complete_draft()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "not found"):
                set_event_status(temp_dir, "no-such-id", "ACTIVE")


class CliTests(unittest.TestCase):
    def test_cli_lists_drafts_without_touching_real_library(self):
        event = _draft_with_facts_only()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "draft.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.policy_draft_workflow",
                    "--library",
                    temp_dir,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertIn("auto-draft-test", result.stdout)
            self.assertIn("未就绪", result.stdout)

    def test_cli_promote_rejects_incomplete_draft(self):
        event = _draft_with_facts_only()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "draft.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.policy_draft_workflow",
                    "--library",
                    temp_dir,
                    "--promote",
                    "auto-draft-test",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertIn("拒绝提升", result.stdout)
            self.assertIn("tensions 为空", result.stdout)

    def test_cli_promote_succeeds_and_writes_active(self):
        event = _complete_draft()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, f"{event['id']}.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.policy_draft_workflow",
                    "--library",
                    temp_dir,
                    "--promote",
                    "auto-complete-draft",
                    "--no-audit",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertIn("已提升", result.stdout)
            self.assertIn("policy_event_promoted", result.stdout)
            reloaded = load_policy_library(temp_dir)["events"][0]
            self.assertEqual(reloaded["status"], "ACTIVE")
            self.assertEqual(reloaded["promoted_by"], "manual-review")
            self.assertTrue(str(reloaded["promoted_at"]).strip())


if __name__ == "__main__":
    unittest.main()
