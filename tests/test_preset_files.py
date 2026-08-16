"""GOAI DSH agent preset packaging invariants.

These tests lock the product-safety defaults of the shipped preset template
(harness/preset/) so a regression in the YAML is caught by the ordinary
Python test baseline instead of only by the live DSH mount smoke test.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PRESET = REPO / "harness" / "preset"
AGENT_YML = PRESET / "agent.cordis.yml"
PRESET_YML = PRESET / "preset.yml"


def _agent_text() -> str:
    return AGENT_YML.read_text(encoding="utf-8")


def _row_block(text: str, row_id: str) -> str:
    """Return the YAML text starting at a top-level row id."""
    pattern = r"(?m)^- id: " + re.escape(row_id) + r"\s*$"
    match = re.search(pattern, text)
    if match is None:
        return ""
    next_row = re.search(r"(?m)^- id: \S", text[match.end():])
    end = match.end() + next_row.start() if next_row else len(text)
    return text[match.start():end]


class PresetMetadataTests(unittest.TestCase):
    def test_preset_metadata_is_deliverable(self) -> None:
        meta = yaml.safe_load(PRESET_YML.read_text(encoding="utf-8"))
        self.assertEqual(meta["name"], "GOAI Options Terminal")
        desc = meta["description"]
        self.assertIn("goai_state", desc)
        self.assertIn("python -m src", desc)
        self.assertIn("tool-cordis 默认禁用", desc)
        self.assertIn("腾讯 0700", desc)

    def test_description_is_machine_portable(self) -> None:
        meta = yaml.safe_load(PRESET_YML.read_text(encoding="utf-8"))
        self.assertNotIn("VISION_API_KEY 已配", meta["description"])


class PresetCompositionTests(unittest.TestCase):
    def test_goai_persona_and_iron_rules_present(self) -> None:
        text = _agent_text()
        self.assertIn("name: '@deepseek-ai/dsh-persona'", text)
        self.assertIn("数字铁律", text)
        self.assertIn("NO_TRADE", text)

    def test_vision_tool_present(self) -> None:
        text = _agent_text()
        self.assertIn("- id: tool-vision", text)
        self.assertIn("name: '@dsh-external/dsh-vision'", text)

    def test_tool_cordis_disabled_by_default(self) -> None:
        block = _row_block(_agent_text(), "tool-cordis")
        self.assertIn("disabled: true", block)

    def test_delegation_group_disabled_by_default(self) -> None:
        block = _row_block(_agent_text(), "delegation")
        self.assertIn("disabled: true", block)

    def test_preset_skills_are_shipped(self) -> None:
        for skill in ("cordis-plugin-development", "editing-cordis-compositions"):
            self.assertTrue((PRESET / "skills" / skill / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
