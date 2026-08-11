from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.decision_inputs import DecisionInputService
from src.futu_adapter import FutuAdapter, FutuLiveGateway
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
from src.models import Snapshot
from src.replay_adapter import ReplayGateway
from src.snapshot_recorder import SnapshotRecorder, iter_records
from tests.fakes import FakeAccountContext, FakeQuoteContext


FIXED_NOW = "2026-08-12T02:00:01+00:00"


def replay_envelope(request, data, *, mode=DataMode.REPLAY):
    return DataEnvelope(
        mode=mode,
        origin_source="FUTU",
        captured_at_utc="2026-08-12T02:00:00+00:00",
        source_time_utc="2026-08-12T02:00:00+00:00",
        freshness_status=(
            FreshnessStatus.FROZEN if mode is DataMode.REPLAY else FreshnessStatus.FRESH
        ),
        request=request,
        status=EnvelopeStatus.OK,
        data=data,
        entitlements={"recorded": True},
        warnings=[],
        typed_error=None,
    )


def live_gateway(quote=None, **kwargs):
    return FutuLiveGateway(
        quote_context_factory=lambda: quote or FakeQuoteContext(),
        opend_probe=lambda *_args: True,
        clock=lambda: FIXED_NOW,
        **kwargs,
    )


class LiveGatewayAdditionalTests(unittest.TestCase):
    def test_compatibility_adapter_has_no_raw_context_or_subscription_escape(self):
        adapter = FutuAdapter(
            quote_context_factory=lambda: FakeQuoteContext(),
            opend_probe=lambda *_args: True,
            clock=lambda: FIXED_NOW,
        )

        self.assertFalse(hasattr(adapter, "connect"))
        self.assertFalse(hasattr(adapter, "subscribe"))
        self.assertFalse(hasattr(adapter, "unsubscribe"))

    def test_read_workflow_methods_normalize_without_sdk_import(self):
        quote = FakeQuoteContext()
        gateway = live_gateway(quote)

        health = gateway.health()
        capabilities = gateway.capabilities()
        market_state = gateway.get_market_state(["hk.00700"])
        days = gateway.get_trading_days("hk", "2026-08-12", "2026-08-13")
        expirations = gateway.get_expiration_dates("hk.00700")
        quote_result = gateway.get_option_quote([OptionLeg("hk.call", "buy", 1)])

        self.assertTrue(health.data["ready"])
        self.assertFalse(capabilities.data["execution"])
        self.assertEqual(market_state.data[0]["code"], "HK.00700")
        self.assertEqual(days.data[0]["date"], "2026-08-12")
        self.assertEqual(expirations.data[0]["expiry"], "2026-08-28")
        self.assertEqual(quote_result.data[0]["code"], "HK.CALL")

    def test_invalid_requests_fail_before_context_creation(self):
        created = []
        gateway = FutuLiveGateway(
            quote_context_factory=lambda: created.append(True),
            opend_probe=lambda *_args: True,
            clock=lambda: FIXED_NOW,
        )

        results = [
            gateway.get_market_state([]),
            gateway.get_market_snapshot([]),
            gateway.get_expiration_dates("tencent"),
            gateway.get_option_chain(object()),
            gateway.get_option_quotes([]),
            gateway.get_strategy_quote([]),
        ]

        self.assertTrue(
            all(result.typed_error.code is GatewayErrorCode.INVALID_REQUEST for result in results)
        )
        self.assertEqual(created, [])

    def test_probe_and_sdk_factory_failures_are_typed_and_lazy(self):
        created = []
        unavailable = FutuLiveGateway(
            quote_context_factory=lambda: created.append(True),
            opend_probe=lambda *_args: False,
            clock=lambda: FIXED_NOW,
        ).health()

        def missing_sdk():
            raise ImportError("futu is missing")

        missing = FutuLiveGateway(
            quote_context_factory=missing_sdk,
            opend_probe=lambda *_args: True,
            clock=lambda: FIXED_NOW,
        ).health()

        self.assertEqual(unavailable.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)
        self.assertEqual(missing.typed_error.code, GatewayErrorCode.SDK_UNAVAILABLE)
        self.assertEqual(created, [])

    def test_health_rejects_explicit_logged_out_quote_session(self):
        class LoggedOutQuote(FakeQuoteContext):
            def get_global_state(self):
                return 0, {
                    "server_ver": "10.9.6918",
                    "timestamp": "2026-08-12T02:00:00+00:00",
                    "qot_logined": 0,
                    "trd_logined": 0,
                }

        result = live_gateway(LoggedOutQuote()).health()

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.AUTH_FAILED)

    def test_stale_vendor_timestamp_is_not_marked_fresh(self):
        quote = FakeQuoteContext(
            snapshot_data=type("Frame", (), {
                "to_dict": lambda self, orient=None: [
                    {
                        "code": "HK.00700",
                        "update_time": "2026-08-12T01:00:00+00:00",
                        "last_price": 500.0,
                    }
                ]
            })()
        )

        result = live_gateway(quote).get_market_snapshot(["HK.00700"])

        self.assertEqual(result.status, EnvelopeStatus.STALE)
        self.assertEqual(result.freshness_status, FreshnessStatus.STALE)

    def test_upstream_entitlement_and_rate_errors_are_typed(self):
        class EntitlementQuote(FakeQuoteContext):
            def get_market_state(self, codes):
                return -1, "no quota for LV1 market data"

        class RateQuote(FakeQuoteContext):
            def get_market_state(self, codes):
                return -1, "request frequency rate limit"

        entitlement = live_gateway(EntitlementQuote()).get_market_state(["HK.00700"])
        rate = live_gateway(RateQuote()).get_market_state(["HK.00700"])

        self.assertEqual(entitlement.typed_error.code, GatewayErrorCode.ENTITLEMENT_DENIED)
        self.assertEqual(entitlement.entitlements["market_data"], "denied")
        self.assertEqual(rate.typed_error.code, GatewayErrorCode.RATE_LIMITED)
        self.assertTrue(rate.typed_error.retryable)

    def test_account_entitlement_error_is_redacted(self):
        class DeniedAccount(FakeAccountContext):
            def accinfo_query(self, **kwargs):
                return -1, "permission denied for acc_id=987654321 card=secret-card"

        binding = AccountBinding("demo", 123456)
        result = live_gateway(
            FakeQuoteContext(),
            account_context_factory=lambda _binding: DeniedAccount(),
            account_bindings={"demo": binding},
        ).get_account_risk_summary("demo")

        self.assertEqual(result.typed_error.code, GatewayErrorCode.ENTITLEMENT_DENIED)
        self.assertNotIn("987654321", result.typed_error.message)
        self.assertNotIn("secret-card", result.typed_error.message)

    def test_account_currency_is_sent_to_both_sdk_queries(self):
        account = FakeAccountContext()
        binding = AccountBinding("usd_demo", 123456, currency="USD")
        result = live_gateway(
            FakeQuoteContext(),
            account_context_factory=lambda _binding: account,
            account_bindings={"usd_demo": binding},
        ).get_account_risk_summary("usd_demo")

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertTrue(all(kwargs["currency"] == "USD" for _, kwargs in account.calls))
        self.assertEqual(result.data["currency"], "USD")

    def test_snapshot_batches_and_context_close_are_deterministic(self):
        class BatchQuote(FakeQuoteContext):
            def get_market_snapshot(self, codes):
                self.calls.append(("get_market_snapshot", list(codes)))
                return 0, [
                    {
                        "code": code,
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": 500.0,
                    }
                    for code in codes
                ]

        quote = BatchQuote()
        gateway = live_gateway(quote)
        codes = [f"HK.{index:05d}" for index in range(401)]

        result = gateway.get_market_snapshot(codes)
        gateway.close()
        gateway.close()

        batches = [args for name, args in quote.calls if name == "get_market_snapshot"]
        self.assertEqual([len(batch) for batch in batches], [400, 1])
        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertTrue(quote.closed)

    def test_rate_window_expires(self):
        times = iter([100.0, 131.0])
        quote = FakeQuoteContext()
        gateway = live_gateway(
            quote,
            rate_limits={"get_market_state": (1, 30.0)},
            monotonic=lambda: next(times),
        )

        first = gateway.get_market_state(["HK.00700"])
        second = gateway.get_market_state(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.OK)
        self.assertEqual(second.status, EnvelopeStatus.OK)


class ReplayGatewayAdditionalTests(unittest.TestCase):
    def test_exact_read_surface_replays_recorded_envelopes(self):
        requests = [
            (
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "HK.00700", "market_state": "MORNING"}],
            ),
            (
                {
                    "operation": "get_trading_days",
                    "market": "HK",
                    "start": "2026-08-12",
                    "end": "2026-08-13",
                },
                [{"date": "2026-08-12"}],
            ),
            (
                {"operation": "get_expiration_dates", "underlying": "HK.00700"},
                [{"expiry": "2026-08-28"}],
            ),
            (
                {
                    "operation": "get_option_quotes",
                    "legs": [{"code": "HK.CALL", "action": "BUY", "quantity": 1}],
                },
                [{"code": "HK.CALL", "price": 12.0}],
            ),
            (
                {
                    "operation": "get_strategy_quote",
                    "legs": [{"code": "HK.CALL", "action": "BUY", "quantity": 1}],
                },
                [{"bid1": 11.0, "ask1": 12.0}],
            ),
            (
                {
                    "operation": "get_account_risk_summary",
                    "account_ref": "demo",
                    "codes": ["HK.00700"],
                },
                {
                    "account_ref": "demo",
                    "currency": "HKD",
                    "total_assets": 100_000.0,
                    "available_funds": 75_000.0,
                    "positions": [],
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SnapshotRecorder(temp_dir)
            for request, data in requests:
                recorder.record(replay_envelope(request, data), "surface")
            gateway = ReplayGateway(temp_dir)
            leg = OptionLeg("HK.CALL", "BUY", 1)

            results = [
                gateway.get_market_state(["hk.00700"]),
                gateway.get_trading_days("hk", "2026-08-12", "2026-08-13"),
                gateway.get_expiration_dates("hk.00700"),
                gateway.get_option_quote([leg]),
                gateway.get_strategy_quote([leg]),
                gateway.get_account_risk_summary("demo", ["hk.00700"]),
            ]

            self.assertTrue(all(result.status is EnvelopeStatus.OK for result in results))
            self.assertTrue(all(result.mode is DataMode.REPLAY for result in results))

    def test_empty_directory_health_and_capabilities_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway = ReplayGateway(temp_dir)

            health = gateway.health()
            capabilities = gateway.capabilities()

        self.assertEqual(health.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING)
        self.assertEqual(capabilities.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING)

    def test_replay_code_inputs_match_live_normalization(self):
        request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            SnapshotRecorder(temp_dir).record(
                replay_envelope(
                    request,
                    [{"code": "HK.00700", "last_price": 500.0}],
                ),
                "codes",
            )
            gateway = ReplayGateway(temp_dir)

            single = gateway.get_market_snapshot(" hk.00700 ")
            duplicate = gateway.get_market_snapshot(["hk.00700", " HK.00700 "])

        self.assertEqual(single.status, EnvelopeStatus.OK)
        self.assertEqual(duplicate.status, EnvelopeStatus.OK)

    def test_malformed_legacy_fixture_fails_health_and_capabilities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "bad.json").write_text("not JSON", encoding="utf-8")
            gateway = ReplayGateway(temp_dir, allow_legacy=True)

            health = gateway.health()
            capabilities = gateway.capabilities()

        self.assertEqual(health.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(capabilities.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_resolver_derives_exact_contract_from_recorded_all_chain(self):
        chain_request = {
            "operation": "get_option_chain",
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "ALL",
            "option_cond_type": "ALL",
        }
        rows = [
            {
                "code": "HK.CALL",
                "underlying": "HK.00700",
                "expiry": "2026-08-28",
                "strike": 500.0,
                "option_type": "CALL",
            },
            {
                "code": "HK.PUT",
                "underlying": "HK.00700",
                "expiry": "2026-08-28",
                "strike": 500.0,
                "option_type": "PUT",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            SnapshotRecorder(temp_dir).record(replay_envelope(chain_request, rows), "chain")

            result = ReplayGateway(temp_dir).resolve_option_code(
                "hk.00700", "2026-08-28", 500.0, "call"
            )

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data["code"], "HK.CALL")
        self.assertEqual(result.request["operation"], "resolve_option_code")

    def test_resolver_preserves_not_found_and_ambiguous_semantics(self):
        chain_request = {
            "operation": "get_option_chain",
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "ALL",
            "option_cond_type": "ALL",
        }
        duplicates = [
            {
                "code": "HK.A",
                "underlying": "HK.00700",
                "expiry": "2026-08-28",
                "strike": 500.0,
                "option_type": "CALL",
            },
            {
                "code": "HK.B",
                "underlying": "HK.00700",
                "expiry": "2026-08-28",
                "strike": 500.0,
                "option_type": "CALL",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            SnapshotRecorder(temp_dir).record(
                replay_envelope(chain_request, duplicates), "ambiguous"
            )
            gateway = ReplayGateway(temp_dir)

            ambiguous = gateway.resolve_option_code(
                "HK.00700", "2026-08-28", 500.0, "CALL"
            )
            missing = gateway.resolve_option_code(
                "HK.00700", "2026-08-28", 600.0, "CALL"
            )

        self.assertEqual(ambiguous.typed_error.code, GatewayErrorCode.AMBIGUOUS_MATCH)
        self.assertEqual(missing.typed_error.code, GatewayErrorCode.NOT_FOUND)

    def test_legacy_fixture_uses_filename_timestamp_after_malformed_prefix(self):
        payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.CALL",
                    "option_type": "CALL",
                    "strike_time": "2026-08-28",
                    "strike_price": 500.0,
                    "option_standard_type": "STANDARD",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "2026-08-12_chain.json")
            path.write_text("broken { prefix\n" + json.dumps(payload), encoding="utf-8")
            request = OptionChainRequest(
                "HK.00700",
                "2026-08-28",
                "2026-08-28",
                option_type="CALL",
            )

            result = ReplayGateway(temp_dir, allow_legacy=True).get_option_chain(request)

        self.assertEqual(result.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(result.captured_at_utc, "2026-08-11T16:00:00+00:00")
        self.assertEqual(result.data[0]["standard_type"], "STANDARD")


class DecisionAndRecorderAdditionalTests(unittest.TestCase):
    def test_decision_service_validates_scenario_shape_and_fields(self):
        service = DecisionInputService(object())

        results = [
            service.refresh_decision_inputs(None),
            service.refresh_decision_inputs({}),
            service.refresh_decision_inputs({"underlying": "HK.00700"}),
            service.refresh_decision_inputs(
                {"underlying": "HK.00700", "expiry": "2026-08-28", "option_type": "BAD"}
            ),
        ]

        self.assertTrue(
            all(result.typed_error.code is GatewayErrorCode.INVALID_REQUEST for result in results)
        )

    def test_decision_service_reports_unconfigured_account_capability(self):
        class Gateway:
            mode = DataMode.REPLAY

            def health(self):
                return replay_envelope(
                    {"operation": "health"}, {"ready": True, "server_version": None}
                )

            def capabilities(self):
                return replay_envelope(
                    {"operation": "capabilities"},
                    {
                        "market_data": True,
                        "account_read": False,
                        "strategy_combination_quote": True,
                        "execution": False,
                        "real_trading": False,
                    },
                )

            def get_market_state(self, codes):
                return replay_envelope(
                    {"operation": "get_market_state", "codes": codes},
                    [{"code": codes[0], "market_state": "MORNING"}],
                )

            def get_market_snapshot(self, codes):
                return replay_envelope(
                    {"operation": "get_market_snapshot", "codes": codes},
                    [{"code": codes[0], "last_price": 500.0}],
                )

            def get_expiration_dates(self, underlying):
                return replay_envelope(
                    {"operation": "get_expiration_dates", "underlying": underlying},
                    [{"expiry": "2026-08-28"}],
                )

            def get_option_chain(self, request):
                return replay_envelope(
                    {"operation": "get_option_chain", **request.to_dict()},
                    [
                        {
                            "code": "HK.CALL",
                            "underlying": request.underlying,
                            "expiry": request.start,
                            "option_type": "CALL",
                            "strike": 500.0,
                        }
                    ],
                )

        result = DecisionInputService(Gateway(), account_gateway=object()).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28", "account_ref": "demo"}
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)

    def test_live_decision_service_preserves_unconfigured_account_error(self):
        class LiveGateway:
            mode = DataMode.LIVE

            def health(self):
                return replay_envelope(
                    {"operation": "health"},
                    {"ready": True, "server_version": None, "account_logged_in": 1},
                    mode=DataMode.LIVE,
                )

            def capabilities(self):
                return replay_envelope(
                    {"operation": "capabilities"},
                    {
                        "market_data": True,
                        "account_read": False,
                        "strategy_combination_quote": True,
                        "execution": False,
                        "real_trading": False,
                    },
                    mode=DataMode.LIVE,
                )

            def get_market_state(self, codes):
                return replay_envelope(
                    {"operation": "get_market_state", "codes": codes},
                    [{"code": codes[0], "market_state": "MORNING"}],
                    mode=DataMode.LIVE,
                )

            def get_market_snapshot(self, codes):
                return replay_envelope(
                    {"operation": "get_market_snapshot", "codes": codes},
                    [{"code": codes[0], "last_price": 500.0}],
                    mode=DataMode.LIVE,
                )

            def get_expiration_dates(self, underlying):
                return replay_envelope(
                    {"operation": "get_expiration_dates", "underlying": underlying},
                    [{"expiry": "2026-08-28"}],
                    mode=DataMode.LIVE,
                )

            def get_option_chain(self, request):
                return replay_envelope(
                    {"operation": "get_option_chain", **request.to_dict()},
                    [
                        {
                            "code": "HK.CALL",
                            "underlying": request.underlying,
                            "expiry": request.start,
                            "option_type": "CALL",
                            "strike": 500.0,
                        }
                    ],
                    mode=DataMode.LIVE,
                )

        result = DecisionInputService(
            LiveGateway(), account_gateway=object()
        ).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28", "account_ref": "demo"}
        )

        self.assertEqual(result.mode, DataMode.LIVE)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)

    def test_decision_service_rejects_mixed_live_and_replay_outputs(self):
        class MixedGateway:
            def health(self):
                return replay_envelope({"operation": "health"}, {"ready": True})

            def capabilities(self):
                return replay_envelope({"operation": "capabilities"}, {}, mode=DataMode.LIVE)

            def get_market_state(self, _codes):
                return replay_envelope({"operation": "get_market_state"}, [])

            def get_market_snapshot(self, _codes):
                return replay_envelope({"operation": "get_market_snapshot"}, [])

            def get_expiration_dates(self, _underlying):
                return replay_envelope({"operation": "get_expiration_dates"}, [])

            def get_option_chain(self, _request):
                return replay_envelope({"operation": "get_option_chain"}, [])

        result = DecisionInputService(MixedGateway()).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_legacy_snapshot_recording_and_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SnapshotRecorder(temp_dir)
            snapshot = Snapshot(
                source="fixture",
                payload={"value": 1},
                captured_at="2026-08-12T02:00:00+00:00",
            )
            path = recorder.record(snapshot, "legacy")
            path.write_text("\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

            records = list(iter_records(path))
            listed = recorder.list_files("legacy")

            self.assertEqual(records, [snapshot])
            self.assertEqual(listed, [path])
            with self.assertRaises(ValueError):
                recorder.record(snapshot, "../escape")
            with self.assertRaises(TypeError):
                recorder.record(object())

    def test_legacy_reader_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "bad.jsonl")
            path.write_text("[]\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                list(iter_records(path))


if __name__ == "__main__":
    unittest.main()
