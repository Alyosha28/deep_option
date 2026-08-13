"""对话场景解析测试（离线，不写文件、不联网）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.scenario_parser import parse_message

SNAPSHOT = json.loads(Path("data/hero_inputs.json").read_text(encoding="utf-8"))


class ParseMessageTests(unittest.TestCase):
    def test_hero_message_parses_full_scenario(self):
        result = parse_message(
            "腾讯业绩快到了，方向不确定。账户 10 万港币，单笔最多亏 5%，帮我看看跨式。",
            SNAPSHOT,
        )

        scenario = result["scenario"]
        self.assertEqual(scenario["underlying"], "HK.00700")
        self.assertEqual(scenario["view"], "uncertain")
        self.assertEqual(scenario["account_cash_hkd"], 100_000.0)
        self.assertEqual(scenario["risk_budget_pct"], 5.0)
        self.assertIn("2026-08-12", scenario["horizon"])
        self.assertEqual(scenario["constraints"], ["单笔最多亏损 5%"])
        self.assertEqual(result["assumed"], [])

    def test_missing_fields_assumed_from_snapshot(self):
        result = parse_message("腾讯业绩，帮我看看", SNAPSHOT)

        scenario = result["scenario"]
        self.assertEqual(scenario["account_cash_hkd"], 100_000.0)
        self.assertEqual(scenario["risk_budget_pct"], 5.0)
        self.assertEqual(
            set(result["assumed"]),
            {"account_cash_hkd", "risk_budget_pct"},
        )
        self.assertTrue(any("跨式" in note for note in result["notes"]))

    def test_bullish_view_is_recorded_with_note(self):
        result = parse_message("我看多腾讯，账户 20 万港币，最多亏 8%", SNAPSHOT)

        self.assertEqual(result["scenario"]["view"], "bullish")
        self.assertEqual(result["scenario"]["account_cash_hkd"], 200_000.0)
        self.assertEqual(result["scenario"]["risk_budget_pct"], 8.0)
        self.assertTrue(any("方向中性" in note for note in result["notes"]))

    def test_market_code_and_days_horizon(self):
        result = parse_message("HK.00700 持有 5 天，单笔最多亏 3%", SNAPSHOT)

        self.assertEqual(result["scenario"]["underlying"], "HK.00700")
        self.assertEqual(result["scenario"]["horizon"], "5 天")

    def test_alias_maps_to_symbol(self):
        result = parse_message("英伟达财报前怎么看", SNAPSHOT)

        self.assertEqual(result["scenario"]["underlying"], "US.NVDA")
        self.assertTrue(any("不在当前冻结快照" in note for note in result["notes"]))

    def test_missing_underlying_raises(self):
        with self.assertRaisesRegex(ValueError, "无法识别标的"):
            parse_message("帮我看看期权", SNAPSHOT)

    def test_empty_message_raises(self):
        with self.assertRaisesRegex(ValueError, "消息为空"):
            parse_message("   ", SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
