from __future__ import annotations

import json
import unittest

from src.futu_adapter import FutuLiveGateway
from src.gateway import (
    AccountBinding,
    DataMode,
    EnvelopeStatus,
    GatewayErrorCode,
    OptionLeg,
)

from tests.fakes import FakeAccountContext, FakeQuoteContext, RecordingSink


class FutuLiveGatewayTests(unittest.TestCase):
    def _gateway(self, quote=None, account=None, recorder=None, bindings=None):
        quote = quote or FakeQuoteContext()
        account = account or FakeAccountContext()
        counts = {"quote": 0, "account": 0}

        def quote_factory():
            counts["quote"] += 1
            return quote

        def account_factory(_binding):
            counts["account"] += 1
            return account

        gateway = FutuLiveGateway(
            quote_context_factory=quote_factory,
            account_context_factory=account_factory,
            account_bindings=bindings or {},
            recorder=recorder,
            opend_probe=lambda *_args: True,
            clock=lambda: "2026-08-12T02:00:01+00:00",
        )
        return gateway, quote, account, counts

    def test_only_loopback_hosts_are_allowed(self):
        with self.assertRaises(ValueError):
            FutuLiveGateway(host="192.168.1.5")

    def test_quote_context_is_lazy_reused_and_normalized(self):
        gateway, quote, _, counts = self._gateway()

        first = gateway.get_market_snapshot(["hk.00700"])
        second = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(counts["quote"], 1)
        self.assertEqual(first.mode, DataMode.LIVE)
        self.assertEqual(first.data[0]["code"], "HK.00700")
        self.assertEqual(first.data[0]["open"], 498.0)
        self.assertEqual(first.data[0]["bid"], 499.8)
        self.assertNotIn("delta", first.data[0])
        self.assertNotIn("acc_id", json.dumps(first.to_dict()))
        self.assertEqual(first.request, second.request)

        gateway.close()
        self.assertTrue(quote.closed)

    def test_sdk_error_is_typed_and_does_not_fall_back_to_replay(self):
        quote = FakeQuoteContext(snapshot_ret=-1, snapshot_data="permission denied for quote")
        gateway, _, _, _ = self._gateway(quote=quote)

        result = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(result.mode, DataMode.LIVE)
        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ENTITLEMENT_DENIED)

    def test_strategy_quote_uses_combination_api(self):
        gateway, quote, _, _ = self._gateway()
        legs = [
            OptionLeg("HK.CALL", "BUY", 1),
            OptionLeg("HK.PUT", "BUY", 1),
        ]

        result = gateway.get_strategy_quote(legs)

        self.assertEqual(result.data[0]["bid1"], 19.5)
        self.assertEqual(result.data[0]["ask1"], 20.5)
        call_names = [name for name, _ in quote.calls]
        self.assertIn("get_option_strategy_analysis", call_names)
        self.assertNotIn("get_market_snapshot", call_names)

    def test_account_summary_is_alias_only_fresh_and_redacted(self):
        binding = AccountBinding("sim_100k_hkd", 987654321, trd_env="SIMULATE", currency="HKD")
        gateway, _, account, counts = self._gateway(bindings={binding.account_ref: binding})

        result = gateway.get_account_risk_summary("sim_100k_hkd", codes=["HK.00700"])
        encoded = json.dumps(result.to_dict())

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data["account_ref"], "sim_100k_hkd")
        self.assertNotIn("987654321", encoded)
        self.assertNotIn("secret-card", encoded)
        self.assertNotIn("position_id", encoded)
        self.assertIsNone(result.data["initial_margin"])
        self.assertEqual(counts["account"], 1)
        for name, kwargs in account.calls:
            self.assertTrue(kwargs["refresh_cache"], name)
            self.assertEqual(kwargs["acc_id"], 987654321, name)

    def test_unknown_account_fails_before_context_creation(self):
        gateway, _, _, counts = self._gateway()

        result = gateway.get_account_risk_summary("missing")

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)
        self.assertEqual(counts["account"], 0)

    def test_read_only_surface_and_recording(self):
        recorder = RecordingSink([])
        gateway, _, _, _ = self._gateway(recorder=recorder)

        result = gateway.get_market_snapshot(["HK.00700"])

        self.assertIs(recorder.envelopes[0], result)
        forbidden = ("place", "modify", "cancel", "unlock", "submit", "invoke")
        public_names = [name.lower() for name in dir(gateway) if not name.startswith("_")]
        self.assertFalse(any(word in name for name in public_names for word in forbidden))

    def test_connection_failure_drops_cached_context_and_reconnects(self):
        class RaisingQuoteContext(FakeQuoteContext):
            def get_market_snapshot(self, codes):
                raise ConnectionError("Connection refused")

        dead = RaisingQuoteContext()
        healthy = FakeQuoteContext()
        created = []
        contexts = iter([dead, healthy])

        def factory():
            context = next(contexts)
            created.append(context)
            return context

        gateway = FutuLiveGateway(
            quote_context_factory=factory,
            opend_probe=lambda *_args: True,
            clock=lambda: "2026-08-12T02:00:01+00:00",
        )

        first = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.ERROR)
        self.assertEqual(first.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)
        self.assertTrue(first.typed_error.retryable)
        self.assertTrue(dead.closed)

        second = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(second.status, EnvelopeStatus.OK)
        self.assertEqual(second.data[0]["code"], "HK.00700")
        self.assertEqual(len(created), 2)
