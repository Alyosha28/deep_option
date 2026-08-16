"""P0c 模拟提交编排测试：门控、人机确认、订单构建、回执与审计链。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from src.decision_pipeline import DEFAULT_INPUT
from src.gateway import (
    AccountBinding,
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
)
from src.order_submission import (
    CONFIRMATION_PHRASE,
    SubmissionError,
    build_straddle_orders,
    submit_simulated_straddle,
)
from src.trade_gateway import SimulatedTradeGateway

REPO = Path(__file__).resolve().parent.parent


def _hero_payload() -> dict[str, Any]:
    return json.loads((REPO / "data" / "hero_inputs.json").read_text(encoding="utf-8"))


def _ready_card() -> dict[str, Any]:
    return {
        "action_gate": {"action": "READY_FOR_CONFIRMATION"},
        "data_evidence": {
            "mode": "LIVE",
            "freshness": "FRESH",
            "captured_at": "2026-08-16T00:00:00+00:00",
            "source": "futuapi/OpenD live",
            "snapshot_sha256": "x" * 64,
        },
    }


def _engine(lots: int = 2) -> dict[str, Any]:
    payload = _hero_payload()
    primary_expiry = sorted(payload["legs"], key=lambda g: g["dte"])[0]["expiry"]
    return {
        "primary": {
            "lots": lots,
            "expiry": primary_expiry,
            "call": {"code": "HK.TCH260814C480000"},
            "put": {"code": "HK.TCH260814P480000"},
        }
    }


class _FakeTradeGateway:
    def __init__(self, envelope: DataEnvelope):
        self.envelope = envelope
        self.calls: list[Any] = []

    def place_order(self, orders: list[Any]) -> DataEnvelope:
        self.calls.append([order.to_dict() for order in orders])
        return self.envelope

    def close(self) -> None:
        pass


def _success_envelope(orders: list[dict[str, Any]]) -> DataEnvelope:
    return DataEnvelope(
        mode=DataMode.LIVE,
        origin_source="FUTU_SIMULATE",
        captured_at_utc="2026-08-16T00:00:01+00:00",
        source_time_utc=None,
        freshness_status=FreshnessStatus.FRESH,
        request={"operation": "place_order"},
        status=EnvelopeStatus.OK,
        data=[
            {
                "order_id": f"SIM-{index + 1}",
                "code": order["code"],
                "qty": order["qty"],
                "price": order["price"],
                "status": "SUBMITTED",
            }
            for index, order in enumerate(orders)
        ],
        entitlements={"execution": "simulate"},
        warnings=[],
        typed_error=None,
    )


class BuildStraddleOrdersTests(unittest.TestCase):
    def test_ready_card_builds_two_buy_legs(self) -> None:
        orders = build_straddle_orders(_ready_card(), _engine(), _hero_payload())

        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0].code, "HK.TCH260814C480000")
        self.assertEqual(orders[1].code, "HK.TCH260814P480000")
        self.assertEqual(orders[0].qty, 2)
        self.assertEqual(orders[0].side, "BUY")
        self.assertGreater(orders[0].price, 0)

    def test_non_ready_cards_are_rejected(self) -> None:
        for action in ("NO_TRADE", "BLOCK", "DRAFT_ONLY", None):
            card = _ready_card()
            card["action_gate"]["action"] = action
            with self.assertRaises(SubmissionError) as raised:
                build_straddle_orders(card, _engine(), _hero_payload())
            self.assertEqual(
                raised.exception.code, GatewayErrorCode.INVALID_REQUEST, action
            )

    def test_replay_evidence_is_rejected(self) -> None:
        card = _ready_card()
        card["data_evidence"] = {"mode": "REPLAY", "freshness": "FROZEN"}
        with self.assertRaises(SubmissionError) as raised:
            build_straddle_orders(card, _engine(), _hero_payload())
        self.assertEqual(raised.exception.code, GatewayErrorCode.STALE_DATA)

    def test_zero_lots_rejected(self) -> None:
        with self.assertRaises(SubmissionError) as raised:
            build_straddle_orders(_ready_card(), _engine(lots=0), _hero_payload())
        self.assertEqual(raised.exception.code, GatewayErrorCode.INVALID_REQUEST)


class SubmitSimulatedStraddleTests(unittest.TestCase):
    def test_confirmation_phrase_is_required(self) -> None:
        gateway = _FakeTradeGateway(_success_envelope([]))
        for confirmed, text in ((False, CONFIRMATION_PHRASE), (True, "确认"), (True, "")):
            with self.assertRaises(SubmissionError) as raised:
                submit_simulated_straddle(
                    _ready_card(),
                    _engine(),
                    _hero_payload(),
                    gateway=gateway,  # type: ignore[arg-type]
                    human_confirmed=confirmed,
                    confirmation_text=text,
                    audit_enabled=False,
                )
            self.assertEqual(
                raised.exception.code, GatewayErrorCode.INVALID_REQUEST, (confirmed, text)
            )
        self.assertEqual(gateway.calls, [], "未确认时绝不能触达下单边界")

    def test_happy_path_returns_receipt_and_audit(self) -> None:
        orders_expected = [
            {"code": "HK.TCH260814C480000", "qty": 2, "price": 10.75},
            {"code": "HK.TCH260814P480000", "qty": 2, "price": 11.32},
        ]
        gateway = _FakeTradeGateway(_success_envelope(orders_expected))
        events: list[tuple[str, dict[str, Any]]] = []

        def fake_audit(event: str, payload: dict[str, Any]) -> dict[str, Any]:
            events.append((event, payload))
            return {"seq": len(events), "hash": f"h{len(events)}"}

        result = submit_simulated_straddle(
            _ready_card(),
            _engine(),
            _hero_payload(),
            gateway=gateway,  # type: ignore[arg-type]
            human_confirmed=True,
            confirmation_text=CONFIRMATION_PHRASE,
            audit_enabled=True,
            audit_fn=fake_audit,
        )

        self.assertTrue(result["submitted"])
        self.assertEqual(result["environment"], "SIMULATE")
        self.assertEqual(result["lots"], 2)
        self.assertEqual(len(result["receipts"]), 2)
        self.assertEqual(result["receipts"][0]["order_id"], "SIM-1")
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            gateway.calls[0],
            [
                {"code": "HK.TCH260814C480000", "qty": 2, "price": 10.75},
                {"code": "HK.TCH260814P480000", "qty": 2, "price": 11.32},
            ],
        )
        self.assertEqual([event for event, _ in events], ["order_submitted", "order_receipt"])
        self.assertEqual(events[0][1]["environment"], "SIMULATE")
        self.assertEqual(events[0][1]["lots"], 2)
        self.assertEqual(len(events[1][1]["receipts"]), 2)
        self.assertEqual(result["audit_refs"][0]["event"], "order_submitted")

    def test_gateway_error_surfaces_as_typed_submission_error(self) -> None:
        envelope = DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="APPLICATION",
            captured_at_utc="2026-08-16T00:00:01+00:00",
            source_time_utc=None,
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "place_order"},
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=GatewayError(
                GatewayErrorCode.TRADE_UNLOCK_REQUIRED,
                "模拟盘交易未解锁：请在 OpenD GUI 手动完成交易解锁后重试",
                False,
            ),
        )
        gateway = _FakeTradeGateway(envelope)
        with self.assertRaises(SubmissionError) as raised:
            submit_simulated_straddle(
                _ready_card(),
                _engine(),
                _hero_payload(),
                gateway=gateway,  # type: ignore[arg-type]
                human_confirmed=True,
                confirmation_text=CONFIRMATION_PHRASE,
                audit_enabled=False,
            )
        self.assertEqual(raised.exception.code, GatewayErrorCode.TRADE_UNLOCK_REQUIRED)
        self.assertFalse(raised.exception.retryable)


class TradeGatewayIntegrationSmokeTests(unittest.TestCase):
    """真实 SimulatedTradeGateway 与 fake worker runner 的端到端（无 OpenD）。"""

    def test_submission_through_real_gateway_class(self) -> None:
        captured: dict[str, Any] = {}

        def runner(args: Any, stdin_text: str, timeout: float) -> Any:
            captured["payload"] = json.loads(stdin_text)
            orders = captured["payload"]["orders"]

            class Done:
                pass

            done = Done()
            done.returncode = 0
            done.stdout = json.dumps(
                {
                    "ok": True,
                    "orders": [
                        {
                            "order_id": f"SIM-{index + 1}",
                            "code": order["code"],
                            "qty": order["qty"],
                            "price": order["price"],
                            "status": "SUBMITTED",
                        }
                        for index, order in enumerate(orders)
                    ],
                }
            )
            return done

        gateway = SimulatedTradeGateway(
            AccountBinding(account_ref="demo", _acc_id=123),
            worker_runner=runner,
        )
        result = submit_simulated_straddle(
            _ready_card(),
            _engine(),
            _hero_payload(),
            gateway=gateway,
            human_confirmed=True,
            confirmation_text=CONFIRMATION_PHRASE,
            audit_enabled=False,
        )
        self.assertTrue(result["submitted"])
        self.assertEqual(result["receipts"][0]["order_id"], "SIM-1")
        self.assertEqual(len(result["receipts"]), 2)


if __name__ == "__main__":
    unittest.main()
