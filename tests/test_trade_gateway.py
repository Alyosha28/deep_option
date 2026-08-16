"""P0c 模拟交易网关合同测试（fake worker runner，不起 OpenD、不写审计）。"""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from src.gateway import (
    AccountBinding,
    EnvelopeStatus,
    GatewayErrorCode,
)
from src.trade_gateway import SimulatedOrderRequest, SimulatedTradeGateway


def _binding() -> AccountBinding:
    return AccountBinding(account_ref="demo", _acc_id=987654321)


def _runner_factory(
    response: dict[str, Any],
    *,
    returncode: int = 0,
    captured: dict[str, Any] | None = None,
) -> Any:
    def run(args: Any, stdin_text: str, timeout: float) -> Any:
        if captured is not None:
            captured["args"] = list(args)
            captured["payload"] = json.loads(stdin_text)
            captured["timeout"] = timeout

        class Done:
            pass

        done = Done()
        done.stdout = json.dumps(response)
        done.returncode = returncode
        return done

    return run


class SimulatedOrderRequestTests(unittest.TestCase):
    def test_valid_order_normalises(self) -> None:
        order = SimulatedOrderRequest(code="hk.00700", qty=2, price=1.5)
        self.assertEqual(order.code, "HK.00700")
        self.assertEqual(order.side, "BUY")
        self.assertEqual(order.qty, 2)
        self.assertEqual(order.price, 1.5)

    def test_invalid_orders_raise(self) -> None:
        for kwargs in (
            {"code": "not-a-code", "qty": 1, "price": 1.0},
            {"code": "HK.00700", "qty": 0, "price": 1.0},
            {"code": "HK.00700", "qty": -1, "price": 1.0},
            {"code": "HK.00700", "qty": True, "price": 1.0},
            {"code": "HK.00700", "qty": 10_001, "price": 1.0},
            {"code": "HK.00700", "qty": 1, "price": 0.0},
            {"code": "HK.00700", "qty": 1, "price": -1.0},
            {"code": "HK.00700", "qty": 1, "price": float("nan")},
            {"code": "HK.00700", "qty": 1, "price": 1.0, "side": "SELL"},
        ):
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                SimulatedOrderRequest(**kwargs)


class SimulatedTradeGatewayTests(unittest.TestCase):
    def test_binding_must_be_simulate(self) -> None:
        with self.assertRaises(ValueError):
            AccountBinding(account_ref="demo", _acc_id=1, trd_env="REAL")
        gateway = SimulatedTradeGateway(_binding())
        self.assertEqual(gateway.binding.trd_env, "SIMULATE")

    def test_happy_path_returns_typed_receipts(self) -> None:
        captured: dict[str, Any] = {}
        gateway = SimulatedTradeGateway(
            _binding(), worker_runner=_runner_factory(
                {
                    "ok": True,
                    "orders": [
                        {
                            "order_id": "SIM-1001",
                            "code": "HK.TCH260814C480000",
                            "qty": 2,
                            "price": 12.0,
                            "status": "SUBMITTED",
                        },
                        {
                            "order_id": "SIM-1002",
                            "code": "HK.TCH260814P480000",
                            "qty": 2,
                            "price": 12.0,
                            "status": "SUBMITTED",
                        },
                    ],
                },
                captured=captured,
            )
        )
        orders = [
            SimulatedOrderRequest(code="HK.TCH260814C480000", qty=2, price=12.0),
            SimulatedOrderRequest(code="HK.TCH260814P480000", qty=2, price=12.0),
        ]
        envelope = gateway.place_order(orders)

        self.assertEqual(envelope.status, EnvelopeStatus.OK)
        self.assertEqual(envelope.entitlements, {"execution": "simulate"})
        rows = envelope.data
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["order_id"], "SIM-1001")
        self.assertEqual(rows[0]["status"], "SUBMITTED")
        self.assertIn("submitted_at_utc", rows[0])
        # worker 收到的 payload 必须显式 SIMULATE 语义（worker 内硬编码）
        self.assertEqual(captured["payload"]["acc_id"], 987654321)
        self.assertEqual(len(captured["payload"]["orders"]), 2)
        self.assertEqual(captured["timeout"], 20.0)
        self.assertTrue(str(captured["args"][1]).endswith("futu_trade_worker.py"))

    def test_worker_error_codes_map_to_typed_errors(self) -> None:
        cases = {
            "TRADE_UNLOCK_REQUIRED": GatewayErrorCode.TRADE_UNLOCK_REQUIRED,
            "ENTITLEMENT_DENIED": GatewayErrorCode.ENTITLEMENT_DENIED,
            "ACCOUNT_UNAVAILABLE": GatewayErrorCode.ACCOUNT_UNAVAILABLE,
            "SDK_INCOMPATIBLE": GatewayErrorCode.SDK_INCOMPATIBLE,
            "UNKNOWN_CODE": GatewayErrorCode.UPSTREAM_ERROR,
        }
        for error_code, expected in cases.items():
            gateway = SimulatedTradeGateway(
                _binding(),
                worker_runner=_runner_factory({"ok": False, "error_code": error_code}),
            )
            envelope = gateway.place_order(
                [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
            )
            self.assertEqual(envelope.status, EnvelopeStatus.ERROR, error_code)
            self.assertEqual(envelope.typed_error.code, expected, error_code)

    def test_worker_timeout_and_start_failures(self) -> None:
        def timeout_runner(args: Any, stdin_text: str, timeout: float) -> Any:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)

        gateway = SimulatedTradeGateway(_binding(), worker_runner=timeout_runner)
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.UPSTREAM_ERROR)
        self.assertTrue(envelope.typed_error.retryable)

        def os_runner(args: Any, stdin_text: str, timeout: float) -> Any:
            raise OSError("cannot start")

        gateway = SimulatedTradeGateway(_binding(), worker_runner=os_runner)
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)

    def test_malformed_worker_responses_are_schema_mismatch(self) -> None:
        bad_responses = (
            None,  # returncode 非 0 + 空 stdout 由 runner 组合控制，见下
            {"ok": True, "orders": [{"order_id": None, "code": "HK.00700"}]},
            {"ok": True, "orders": []},
            {"ok": True, "orders": "not-a-list"},
            {"ok": True, "orders": [{"order_id": "1", "code": "US.AAPL", "qty": 1, "price": 1.0}]},
        )
        gateway = SimulatedTradeGateway(
            _binding(), worker_runner=_runner_factory(bad_responses[1])
        )
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        for response in bad_responses[2:]:
            gateway = SimulatedTradeGateway(
                _binding(), worker_runner=_runner_factory(response)
            )
            envelope = gateway.place_order(
                [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
            )
            self.assertEqual(
                envelope.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH, response
            )

        def empty_stdout(args: Any, stdin_text: str, timeout: float) -> Any:
            class Done:
                stdout = ""
                returncode = 1

            return Done()

        gateway = SimulatedTradeGateway(_binding(), worker_runner=empty_stdout)
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.UPSTREAM_ERROR)

        def non_json(args: Any, stdin_text: str, timeout: float) -> Any:
            class Done:
                stdout = "not json"
                returncode = 0

            return Done()

        gateway = SimulatedTradeGateway(_binding(), worker_runner=non_json)
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code="HK.00700", qty=1, price=1.0)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_leg_count_and_empty_orders_rejected(self) -> None:
        gateway = SimulatedTradeGateway(_binding())
        envelope = gateway.place_order([])
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.INVALID_REQUEST)
        envelope = gateway.place_order(
            [SimulatedOrderRequest(code=f"HK.00{i:03d}", qty=1, price=1.0) for i in range(3)]
        )
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
