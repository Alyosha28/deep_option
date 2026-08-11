from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.futu_adapter import FutuLiveGateway
from src.gateway import (
    AccountBinding,
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayErrorCode,
    OptionChainRequest,
    OptionLeg,
)
from src.replay_adapter import ReplayGateway
from src.snapshot_recorder import iter_envelopes
from tests.fakes import FakeAccountContext, FakeFrame, FakeQuoteContext


FIXED_NOW = "2026-08-12T02:00:01+00:00"


def live_gateway(quote, **kwargs):
    return FutuLiveGateway(
        quote_context_factory=lambda: quote,
        opend_probe=lambda *_args: True,
        clock=lambda: FIXED_NOW,
        **kwargs,
    )


class LiveNormalizationTests(unittest.TestCase):
    def test_option_snapshot_keeps_contract_fields_but_not_vendor_greeks(self):
        quote = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {
                        "code": "HK.TCH260828C500000",
                        "name": "Tencent Call",
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": 12.0,
                        "option_type": "CALL",
                        "stock_owner": "HK.00700",
                        "strike_time": "2026-08-28",
                        "option_strike_price": 500.0,
                        "option_contract_size": 100,
                        "option_open_interest": 1234,
                        "option_net_open_interest": 1200,
                        "option_implied_volatility": 31.2,
                        "option_area_type": "AMERICAN",
                        "option_delta": 0.5,
                    }
                ]
            )
        )
        gateway = live_gateway(quote)

        result = gateway.get_market_snapshot(["HK.TCH260828C500000"])

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data[0]["underlying"], "HK.00700")
        self.assertEqual(result.data[0]["contract_size"], 100)
        self.assertEqual(result.data[0]["open_interest"], 1234)
        self.assertEqual(result.data[0]["implied_volatility"], 31.2)
        self.assertEqual(result.data[0]["exercise_type"], "AMERICAN")
        self.assertNotIn("delta", result.data[0])

    def test_naive_vendor_timestamp_uses_market_timezone(self):
        quote = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [{"code": "HK.00700", "update_time": "2026-08-12 10:00:00", "last_price": 500.0}]
            )
        )
        gateway = live_gateway(quote)

        result = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(result.source_time_utc, "2026-08-12T02:00:00+00:00")

    def test_us_naive_vendor_timestamp_observes_dst(self):
        quote = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [{"code": "US.AAPL", "update_time": "2026-08-12 16:00:00", "last_price": 220.0}]
            )
        )
        gateway = FutuLiveGateway(
            quote_context_factory=lambda: quote,
            opend_probe=lambda *_args: True,
            clock=lambda: "2026-08-12T20:00:01+00:00",
        )

        result = gateway.get_market_snapshot(["US.AAPL"])

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.source_time_utc, "2026-08-12T20:00:00+00:00")

    def test_option_quotes_preserve_and_verify_provider_leg_identity(self):
        class QuoteContext(FakeQuoteContext):
            def get_option_quote(self, legs):
                self.calls.append(("get_option_quote", list(legs)))
                option_type = "PUT" if "PUT" in legs[0].code else "CALL"
                return 0, FakeFrame(
                    [
                        {
                            "price": 10.0 if option_type == "PUT" else 12.0,
                            "mid_price": 9.9 if option_type == "PUT" else 11.9,
                            "implied_volatility": 31.2,
                            "open_interest": 1234,
                            "expire_time": "2026-08-28",
                            "strike_price": 500.0,
                            "contract_size": 100,
                            "exercise_type": "AMERICAN",
                            "option_type": option_type,
                            "delta": -0.5 if option_type == "PUT" else 0.5,
                        },
                    ]
                )

        gateway = live_gateway(QuoteContext())
        legs = [OptionLeg("HK.CALL", "BUY", 1), OptionLeg("HK.PUT", "BUY", 1)]

        result = gateway.get_option_quotes(legs)

        self.assertEqual([row["code"] for row in result.data], ["HK.CALL", "HK.PUT"])
        self.assertEqual(result.data[0]["contract_size"], 100)
        self.assertEqual(result.data[0]["implied_volatility"], 31.2)
        self.assertNotIn("delta", result.data[0])

        class ReorderedQuote(FakeQuoteContext):
            def get_option_quote(self, legs):
                wrong_type = "PUT" if "CALL" in legs[0].code else "CALL"
                return 0, FakeFrame(
                    [
                        {
                            "price": 10.0,
                            "option_type": wrong_type,
                            "expire_time": "2026-08-28",
                            "strike_price": 500.0,
                            "contract_size": 100.0,
                        },
                    ]
                )

        class MissingContractFacts(FakeQuoteContext):
            def get_option_quote(self, _legs):
                return 0, FakeFrame([{"price": 12.0}])

        reordered = live_gateway(ReorderedQuote()).get_option_quotes(legs)
        missing = live_gateway(MissingContractFacts()).get_option_quotes(legs)

        self.assertEqual(reordered.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(missing.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_empty_critical_chain_is_not_reported_as_ok(self):
        class EmptyChain(FakeQuoteContext):
            def get_option_chain(self, code, **kwargs):
                return 0, FakeFrame([])

        gateway = live_gateway(EmptyChain())

        result = gateway.get_option_chain(
            OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28")
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.NOT_FOUND)

    def test_resolve_option_code_is_expiry_strict(self):
        gateway = live_gateway(FakeQuoteContext())

        result = gateway.resolve_option_code("HK.00700", "2026-08-28", 500.0, "CALL")

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data["code"], "HK.TCH260828C500000")
        self.assertEqual(result.data["expiry"], "2026-08-28")

    def test_rate_limit_fails_before_second_sdk_call(self):
        quote = FakeQuoteContext()
        gateway = live_gateway(
            quote,
            rate_limits={"get_market_snapshot": (1, 30.0)},
            monotonic=lambda: 100.0,
        )

        first = gateway.get_market_snapshot(["HK.00700"])
        second = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.OK)
        self.assertEqual(second.typed_error.code, GatewayErrorCode.RATE_LIMITED)
        calls = [name for name, _ in quote.calls if name == "get_market_snapshot"]
        self.assertEqual(len(calls), 1)

    def test_default_account_rate_limit_matches_futu_refresh_limit(self):
        account = FakeAccountContext()
        binding = AccountBinding("demo", 123456, trd_env="SIMULATE")
        gateway = FutuLiveGateway(
            quote_context_factory=lambda: FakeQuoteContext(),
            account_context_factory=lambda _binding: account,
            account_bindings={"demo": binding},
            opend_probe=lambda *_args: True,
            clock=lambda: FIXED_NOW,
            monotonic=lambda: 100.0,
        )

        results = [gateway.get_account_risk_summary("demo") for _ in range(11)]

        self.assertTrue(all(result.status is EnvelopeStatus.OK for result in results[:10]))
        self.assertEqual(results[10].typed_error.code, GatewayErrorCode.RATE_LIMITED)
        self.assertEqual(len(account.calls), 20)

    def test_recording_failure_is_visible(self):
        class BrokenRecorder:
            def record(self, *_args, **_kwargs):
                raise OSError("disk full")

        gateway = live_gateway(FakeQuoteContext(), recorder=BrokenRecorder())

        result = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.INTERNAL_ERROR)
        self.assertIsNone(result.data)
        self.assertTrue(any("record" in warning.lower() for warning in result.warnings))


class SdkBoundaryTests(unittest.TestCase):
    def test_default_sdk_path_uses_none_market_and_sdk_enums(self):
        captured: dict[str, object] = {}
        buy_action = object()
        simulate_env = object()
        hkd_currency = object()
        none_market = object()

        class OptionStrategyLeg:
            code = None
            action = None
            quantity = None

        quote_context = FakeQuoteContext()
        account_context = FakeAccountContext()

        def make_quote(**kwargs):
            captured["quote_kwargs"] = kwargs
            return quote_context

        def make_account(**kwargs):
            captured["account_kwargs"] = kwargs
            return account_context

        fake_futu = types.ModuleType("futu")
        fake_futu.OpenQuoteContext = make_quote
        fake_futu.OpenSecTradeContext = make_account
        fake_futu.OptionStrategyLeg = OptionStrategyLeg
        fake_futu.OptionStrategyAction = types.SimpleNamespace(BUY=buy_action, SELL=object())
        fake_futu.TrdEnv = types.SimpleNamespace(SIMULATE=simulate_env)
        fake_futu.Currency = types.SimpleNamespace(HKD=hkd_currency)
        fake_futu.TrdMarket = types.SimpleNamespace(NONE=none_market, HK=object())
        fake_futu.SecurityFirm = types.SimpleNamespace(FUTUSECURITIES=object())

        binding = AccountBinding("demo", 123456, trd_env="SIMULATE")
        with patch.dict(sys.modules, {"futu": fake_futu}):
            gateway = FutuLiveGateway(
                account_bindings={"demo": binding},
                account_context_factory=lambda _binding: account_context,
                opend_probe=lambda *_args: True,
                clock=lambda: FIXED_NOW,
            )
            gateway.get_strategy_quote([OptionLeg("HK.CALL", "BUY", 1)])
            account_result = gateway.get_account_risk_summary("demo")

        self.assertTrue(captured["quote_kwargs"]["is_async_connect"])
        self.assertEqual(account_result.status, EnvelopeStatus.OK)
        strategy_call = next(args for name, args in quote_context.calls if name == "get_option_strategy_analysis")
        self.assertIs(strategy_call[0].action, buy_action)
        for name, kwargs in account_context.calls:
            if name == "set_sync_query_connect_timeout":
                continue
            self.assertEqual(kwargs["trd_env"], "SIMULATE")
            self.assertEqual(kwargs["currency"], "HKD")

    def test_account_worker_uses_none_market_and_sdk_enums(self):
        from src.futu_account_worker import query_account

        captured: dict[str, object] = {}
        simulate_env = object()
        hkd_currency = object()
        none_market = object()
        security_firm = object()
        account_context = FakeAccountContext()

        def make_account(**kwargs):
            captured["account_kwargs"] = kwargs
            return account_context

        fake_futu = types.ModuleType("futu")
        fake_futu.OpenSecTradeContext = make_account
        fake_futu.TrdEnv = types.SimpleNamespace(SIMULATE=simulate_env)
        fake_futu.Currency = types.SimpleNamespace(HKD=hkd_currency)
        fake_futu.TrdMarket = types.SimpleNamespace(NONE=none_market)
        fake_futu.SecurityFirm = types.SimpleNamespace(FUTUSECURITIES=security_firm)
        with patch.dict(sys.modules, {"futu": fake_futu}):
            result = query_account(
                {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "acc_id": 123456,
                    "trd_env": "SIMULATE",
                    "currency": "HKD",
                    "security_firm": "FUTUSECURITIES",
                }
            )

        self.assertTrue(result["ok"])
        self.assertIs(captured["account_kwargs"]["filter_trdmarket"], none_market)
        timeout_call = next(
            kwargs
            for name, kwargs in account_context.calls
            if name == "set_sync_query_connect_timeout"
        )
        self.assertEqual(timeout_call["timeout"], 5.0)
        for name, kwargs in account_context.calls:
            if name == "set_sync_query_connect_timeout":
                continue
            self.assertIs(kwargs["trd_env"], simulate_env)
            self.assertIs(kwargs["currency"], hkd_currency)


class ReplayHardeningTests(unittest.TestCase):
    def test_health_and_capabilities_are_synthesized_from_local_fixtures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = {"code": "HK.00700", "data": []}
            Path(temp_dir, "2026-08-08_hero_chain.json").write_text(
                "2026-08-08 11:54:35,202 | connected\n" + json.dumps(payload),
                encoding="utf-8",
            )
            gateway = ReplayGateway(temp_dir, allow_legacy=True)

            health = gateway.health()
            capabilities = gateway.capabilities()

            self.assertEqual(health.status, EnvelopeStatus.PARTIAL)
            self.assertFalse(health.data["ready"])
            self.assertEqual(capabilities.status, EnvelopeStatus.PARTIAL)
            self.assertFalse(capabilities.data["execution"])

    def test_corrupt_fixture_returns_schema_error_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            envelope = DataEnvelope(
                mode=DataMode.LIVE,
                origin_source="FUTU",
                captured_at_utc="2026-08-08T03:54:35+00:00",
                source_time_utc="2026-08-08T03:54:35+00:00",
                freshness_status=FreshnessStatus.FRESH,
                request={"operation": "get_market_snapshot", "codes": ["HK.00700"]},
                status=EnvelopeStatus.OK,
                data=[{"code": "HK.00700", "last_price": 500.0}],
                entitlements={},
                warnings=[],
                typed_error=None,
            ).to_dict()
            envelope["data"][0]["last_price"] = 1.0
            Path(temp_dir, "corrupt.jsonl").write_text(json.dumps(envelope) + "\n", encoding="utf-8")

            result = ReplayGateway(temp_dir).get_market_snapshot(["HK.00700"])

            self.assertEqual(result.status, EnvelopeStatus.ERROR)
            self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_jsonl_reader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "duplicate.jsonl")
            payload = DataEnvelope(
                mode=DataMode.REPLAY,
                origin_source="FUTU",
                captured_at_utc="2026-08-08T03:54:35+00:00",
                source_time_utc="2026-08-08T03:54:35+00:00",
                freshness_status=FreshnessStatus.FROZEN,
                request={"operation": "health"},
                status=EnvelopeStatus.OK,
                data={"ready": True},
                entitlements={},
                warnings=[],
                typed_error=None,
            ).to_json_line()
            duplicated = payload.replace('"schema_version":"1.0"', '"schema_version":"1.0","schema_version":"1.0"')
            path.write_text(duplicated + "\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                list(iter_envelopes(path))
