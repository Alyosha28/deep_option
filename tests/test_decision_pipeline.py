"""GOAI 端到端决策管线测试（不写审计日志、不落决策卡文件）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.decision_pipeline import (
    DEFAULT_INPUT,
    action_gate,
    compute_engine,
    edge_gate,
    load_frozen_snapshot,
    parse_scenario,
    risk_gate,
    run_pipeline,
)


class ScenarioParseTests(unittest.TestCase):
    def test_valid_scenario_normalizes(self):
        parsed = parse_scenario(
            {
                "underlying": " hk.00700 ",
                "view": "UNCERTAIN",
                "horizon": "2026-08-12 业绩",
                "account_cash_hkd": 100000,
                "risk_budget_pct": 5,
                "constraints": ["单笔最多亏损 5%"],
            }
        )

        self.assertEqual(parsed["underlying"], "HK.00700")
        self.assertEqual(parsed["view"], "uncertain")
        self.assertEqual(parsed["account_cash_hkd"], 100000.0)

    def test_missing_and_invalid_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_scenario({"view": "uncertain"})
        with self.assertRaises(ValueError):
            parse_scenario(
                {
                    "underlying": "HK.00700",
                    "view": "sideways",
                    "horizon": "event",
                    "account_cash_hkd": 100000,
                    "risk_budget_pct": 5,
                }
            )
        with self.assertRaises(ValueError):
            parse_scenario(
                {
                    "underlying": "HK.00700",
                    "view": "uncertain",
                    "horizon": "event",
                    "account_cash_hkd": float("nan"),
                    "risk_budget_pct": 5,
                }
            )

    def test_sensitive_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "sensitive"):
            parse_scenario(
                {
                    "underlying": "HK.00700",
                    "view": "uncertain",
                    "horizon": "event",
                    "account_cash_hkd": 100000,
                    "risk_budget_pct": 5,
                    "order_id": "9536671",
                }
            )


class FrozenSnapshotTests(unittest.TestCase):
    def test_load_returns_hash_and_payload(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)

        self.assertEqual(data["mode"], "REPLAY")
        self.assertEqual(data["underlying"], "HK.00700")
        self.assertEqual(len(data["snapshot_sha256"]), 64)
        self.assertIn("legs", data["payload"])

    def test_tampered_snapshot_is_rejected(self):
        original = json.loads(Path(DEFAULT_INPUT).read_text(encoding="utf-8"))
        original["spot"] = -1
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = Path(temp_dir, "bad.json")
            bad_path.write_text(
                json.dumps(original, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_frozen_snapshot(bad_path)


class GateTests(unittest.TestCase):
    def test_edge_gate_is_low_edge_when_expected_move_below_breakeven(self):
        engine = {
            "primary": {"strike": 480.0, "breakeven_low": 458.9},
            "proposal": {
                "legs": [
                    {
                        "pnl_after_iv_crush": [
                            {"rows": [{"pnl": -711.0}, {"pnl": -340.0}]}
                        ]
                    }
                ]
            },
        }
        earnings = {"expected_move_pct": 3.916}

        result = edge_gate(engine, earnings, spot=478.8, backtest={"available": False})

        self.assertEqual(result["verdict"], "LOW_EDGE")
        self.assertEqual(result["recommendation"], "NO_TRADE")
        self.assertTrue(any(item["result"] == "FAIL" for item in result["checks"]))

    def test_risk_gate_blocks_when_unverified_fees_push_loss_over_budget(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        engine = compute_engine(data, cost_model={"fees_hkd_per_lot": 5000.0, "slippage_bps": 0.0})

        result = risk_gate(engine, data)

        self.assertEqual(result["decision"], "BLOCK")
        self.assertTrue(any("预算" in item for item in result["blocked"]))

    def test_action_gate_requires_live_fresh_data_and_confirmation(self):
        replay = {"mode": "REPLAY", "freshness": "FROZEN"}
        live = {"mode": "LIVE", "freshness": "FRESH"}
        edge_no = {"recommendation": "NO_TRADE"}
        edge_ok = {"recommendation": "EVALUATE"}
        risk_ok = {"decision": "PASS", "blocked": []}
        risk_block = {"decision": "BLOCK", "blocked": ["预算不足"]}

        self.assertEqual(
            action_gate(replay, edge_ok, risk_ok)["action"], "DRAFT_ONLY"
        )
        self.assertEqual(
            action_gate(live, edge_ok, risk_ok)["action"], "DRAFT_ONLY"
        )
        self.assertEqual(
            action_gate(live, edge_ok, risk_ok, human_confirmed=True)["action"],
            "READY_FOR_CONFIRMATION",
        )
        self.assertEqual(
            action_gate(live, edge_no, risk_ok, human_confirmed=True)["action"],
            "NO_TRADE",
        )
        self.assertEqual(
            action_gate(live, edge_ok, risk_block, human_confirmed=True)["action"],
            "BLOCK",
        )


class FullPipelineTests(unittest.TestCase):
    def test_run_pipeline_returns_no_trade_card_without_side_effects(self):
        card = run_pipeline(DEFAULT_INPUT, audit_enabled=False, write_card=False)

        self.assertEqual(card["verdict"], "NO_TRADE")
        self.assertEqual(card["underlying"], "HK.00700")
        self.assertEqual(card["edge_gate"]["recommendation"], "NO_TRADE")
        self.assertEqual(len(card["key_evidence"]), 3)
        self.assertEqual(card["audit_refs"], [])
        self.assertNotIn("output_path", card)


if __name__ == "__main__":
    unittest.main()
