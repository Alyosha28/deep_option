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

    def test_model_section_is_validated(self):
        original = json.loads(Path(DEFAULT_INPUT).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            malformed = dict(original)
            malformed["model"] = {"riskfree_rate": "not-a-number", "div_yield": 0.0}
            bad_path = Path(temp_dir, "bad_model.json")
            bad_path.write_text(
                json.dumps(malformed, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_frozen_snapshot(bad_path)

            missing = dict(original)
            del missing["model"]
            missing_path = Path(temp_dir, "no_model.json")
            missing_path.write_text(
                json.dumps(missing, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_frozen_snapshot(missing_path)


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

    def test_card_summary_and_scenario_follow_provided_scenario(self):
        card = run_pipeline(
            DEFAULT_INPUT,
            scenario={
                "underlying": "HK.00700",
                "view": "bullish",
                "horizon": "2026-08-28 到期前",
                "account_cash_hkd": 250000,
                "risk_budget_pct": 2,
                "constraints": ["只用模拟盘"],
            },
            audit_enabled=False,
            write_card=False,
        )

        self.assertEqual(card["scenario"]["view"], "bullish")
        self.assertEqual(card["scenario"]["horizon"], "2026-08-28 到期前")
        self.assertEqual(card["scenario"]["account_cash_hkd"], 250000.0)
        self.assertEqual(card["scenario"]["risk_budget_pct"], 2.0)
        self.assertEqual(card["scenario"]["constraints"], ["只用模拟盘"])
        self.assertIn("看涨观点、2026-08-28 到期前场景", card["summary"])
        self.assertIn(card["verdict"], card["summary"])

    def test_risk_gate_blocked_message_uses_configured_budget(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        engine = compute_engine(
            data, cost_model={"fees_hkd_per_lot": 5000.0, "slippage_bps": 0.0}
        )

        result = risk_gate(engine, data)

        self.assertEqual(result["decision"], "BLOCK")
        budget_pct = float(data["payload"]["account"]["risk_budget_pct"])
        self.assertTrue(
            any(f"超过 {budget_pct:g}% 风险预算" in item for item in result["blocked"])
        )


class DiscreteDividendPipelineTests(unittest.TestCase):
    """港股离散股息（escrowed-spot 口径）管线测试。

    在 hero 快照副本上加 `model.dividends`（快照捕获日 2026-08-08，
    主/次到期 2026-08-14/08-28），验证校验、引擎方向性与决策卡证据。
    """

    def _snapshot_with_dividends(self, dividends) -> tuple[dict, Path]:
        original = json.loads(Path(DEFAULT_INPUT).read_text(encoding="utf-8"))
        modified = dict(original)
        modified["model"] = {
            **original["model"],
            "dividends": dividends,
        }
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name, "hero_dividends.json")
        path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
        return modified, path

    def test_model_dividends_are_validated(self):
        for bad in (
            [{"ex_date": "not-a-date", "amount": 4.0}],
            [{"ex_date": "2026-8-11", "amount": 4.0}],
            [{"ex_date": "2026-08-11", "amount": -4.0}],
            [{"ex_date": "2026-08-11", "amount": "4.0"}],
            [{"ex_date": "2026-08-11", "amount": 4.0}, {"ex_date": "2026-08-11", "amount": 5.0}],
            {"ex_date": "2026-08-11", "amount": 4.0},
        ):
            _payload, path = self._snapshot_with_dividends(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                load_frozen_snapshot(path)

    def test_discrete_dividend_shifts_iv_directionally(self):
        _payload, path = self._snapshot_with_dividends(
            [{"ex_date": "2026-08-11", "amount": 4.0}]
        )
        base = load_frozen_snapshot(DEFAULT_INPUT)
        with_dividend = load_frozen_snapshot(path)

        engine_base = compute_engine(base)
        engine_div = compute_engine(with_dividend)

        # escrowed S* 更低：同一市场价下 call 的模型价更低 → IV 更高；
        # put 相反。两个到期（除息日在两者之前）都应生效。
        for expiry in ("primary", "secondary"):
            self.assertGreater(
                engine_div[expiry]["call"]["iv"],
                engine_base[expiry]["call"]["iv"],
                msg=expiry,
            )
            self.assertLess(
                engine_div[expiry]["put"]["iv"],
                engine_base[expiry]["put"]["iv"],
                msg=expiry,
            )

        summary = engine_div["dividends"]
        self.assertEqual(len(summary), 1)
        self.assertTrue(summary[0]["applied"])
        self.assertEqual(summary[0]["ex_date"], "2026-08-11")
        self.assertAlmostEqual(summary[0]["tau_years"], 3 / 365, places=5)

    def test_pipeline_card_records_dividend_evidence(self):
        _payload, path = self._snapshot_with_dividends(
            [{"ex_date": "2026-08-11", "amount": 4.0}]
        )
        data = load_frozen_snapshot(path)
        card = run_pipeline(
            path,
            audit_enabled=False,
            write_card=False,
            snapshot_data=data,
        )

        self.assertEqual(card["verdict"], "NO_TRADE")
        self.assertEqual(len(card["key_evidence"]), 4)
        dividend_claim = card["key_evidence"][-1]["claim"]
        self.assertIn("离散股息", dividend_claim)
        self.assertIn("2026-08-11", dividend_claim)
        self.assertIn("escrowed-spot", dividend_claim)

    def test_dividend_after_expiry_is_declared_but_not_applied(self):
        _payload, path = self._snapshot_with_dividends(
            [{"ex_date": "2026-09-15", "amount": 4.0}]
        )
        base = load_frozen_snapshot(DEFAULT_INPUT)
        with_dividend = load_frozen_snapshot(path)

        engine_base = compute_engine(base)
        engine_div = compute_engine(with_dividend)

        self.assertFalse(engine_div["dividends"][0]["applied"])
        self.assertAlmostEqual(
            engine_div["primary"]["call"]["iv"],
            engine_base["primary"]["call"]["iv"],
            places=10,
        )
        card = run_pipeline(
            path,
            audit_enabled=False,
            write_card=False,
            snapshot_data=with_dividend,
        )
        self.assertEqual(len(card["key_evidence"]), 3, "到期后除息不进入定价证据")

    def test_hero_snapshot_without_dividends_is_unchanged(self):
        # hero 快照没有股息声明：引擎输出不出现 dividends 摘要
        base = load_frozen_snapshot(DEFAULT_INPUT)
        engine = compute_engine(base)
        self.assertEqual(engine["dividends"], [])


if __name__ == "__main__":
    unittest.main()
