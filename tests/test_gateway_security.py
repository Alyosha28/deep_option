from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.decision_inputs import DecisionInputService
from src.futu_adapter import FutuLiveGateway
from src.gateway import (
    AccountBinding,
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    OptionChainRequest,
    OptionLeg,
)
from src.replay_adapter import ReplayGateway
from src.snapshot_recorder import SnapshotRecorder
from tests.fakes import FakeAccountContext, FakeFrame, FakeQuoteContext


NOW = "2026-08-12T02:00:01+00:00"


def gateway(quote, **kwargs):
    return FutuLiveGateway(
        quote_context_factory=lambda: quote,
        opend_probe=lambda *_args: True,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
        **kwargs,
    )


def envelope(mode, operation, data, *, status=EnvelopeStatus.OK):
    return DataEnvelope(
        mode=mode,
        origin_source="FUTU",
        captured_at_utc="2026-08-12T02:00:01+00:00",
        source_time_utc="2026-08-12T02:00:00+00:00",
        freshness_status=(
            FreshnessStatus.FROZEN if mode is DataMode.REPLAY else FreshnessStatus.FRESH
        ),
        request={"operation": operation},
        status=status,
        data=data,
        entitlements={},
        warnings=[],
        typed_error=None,
    )


class LiveSchemaAndLimitTests(unittest.TestCase):
    def test_health_requires_login_field_and_parses_unix_timestamp(self):
        source_time = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)

        class UnixState(FakeQuoteContext):
            def get_global_state(self):
                return 0, {
                    "server_ver": "10.9.6918",
                    "timestamp": str(int(source_time.timestamp())),
                    "qot_logined": 1,
                    "trd_logined": 1,
                }

        class MissingLogin(FakeQuoteContext):
            def get_global_state(self):
                return 0, {"server_ver": "10.9.6918", "timestamp": source_time.isoformat()}

        healthy = gateway(UnixState()).health()
        unknown = gateway(MissingLogin()).health()

        self.assertEqual(healthy.source_time_utc, source_time.isoformat())
        self.assertEqual(healthy.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(unknown.typed_error.code, GatewayErrorCode.AUTH_FAILED)

    def test_default_expiration_rate_limit_is_enforced(self):
        quote = FakeQuoteContext()
        live = gateway(quote)

        results = [live.get_expiration_dates("HK.00700") for _ in range(61)]

        self.assertTrue(all(item.status is EnvelopeStatus.OK for item in results[:60]))
        self.assertEqual(results[60].typed_error.code, GatewayErrorCode.RATE_LIMITED)

    def test_custom_rate_map_cannot_disable_safety_defaults(self):
        account = FakeAccountContext()
        live = gateway(
            FakeQuoteContext(),
            rate_limits={},
            account_context_factory=lambda _binding: account,
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )

        results = [live.get_account_risk_summary("demo") for _ in range(11)]

        self.assertEqual(results[10].typed_error.code, GatewayErrorCode.RATE_LIMITED)

    def test_empty_and_malformed_critical_option_results_fail_closed(self):
        class BadQuote(FakeQuoteContext):
            def get_option_quote(self, _legs):
                return 0, FakeFrame([])

            def get_option_strategy_analysis(self, _legs):
                return 0, FakeFrame([])

            def get_option_chain(self, _code, **_kwargs):
                return 0, FakeFrame([{"renamed_code": "HK.CALL"}])

        live = gateway(BadQuote())
        leg = OptionLeg("HK.CALL", "BUY", 1)

        quote = live.get_option_quotes([leg])
        strategy = live.get_strategy_quote([leg])
        chain = live.get_option_chain(
            OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28")
        )

        self.assertEqual(quote.typed_error.code, GatewayErrorCode.NOT_FOUND)
        self.assertEqual(strategy.typed_error.code, GatewayErrorCode.NOT_FOUND)
        self.assertEqual(chain.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_option_quote_cardinality_must_match_requested_legs(self):
        live = gateway(FakeQuoteContext())

        result = live.get_option_quotes(
            [OptionLeg("HK.CALL", "BUY", 1), OptionLeg("HK.PUT", "BUY", 1)]
        )

        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_wrong_shaped_inputs_return_typed_errors(self):
        live = gateway(FakeQuoteContext())

        market = live.get_market_snapshot(None)
        quotes = live.get_option_quotes(None)
        resolution = live.resolve_option_code("HK.00700", "2026-08-28", "bad", "CALL")

        self.assertTrue(
            all(
                item.typed_error.code is GatewayErrorCode.INVALID_REQUEST
                for item in (market, quotes, resolution)
            )
        )

    def test_oldest_source_timestamp_controls_freshness(self):
        quote = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {
                        "code": "HK.00700",
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": 500.0,
                    },
                    {
                        "code": "HK.09988",
                        "update_time": "2026-08-12T01:00:00+00:00",
                        "last_price": 100.0,
                    },
                ]
            )
        )

        result = gateway(quote).get_market_snapshot(["HK.00700", "HK.09988"])

        self.assertEqual(result.source_time_utc, "2026-08-12T01:00:00+00:00")
        self.assertEqual(result.freshness_status, FreshnessStatus.STALE)

    def test_future_source_timestamp_is_not_marked_fresh(self):
        quote = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {
                        "code": "HK.00700",
                        "update_time": "2026-08-12T03:00:00+00:00",
                        "last_price": 500.0,
                    }
                ]
            )
        )

        result = gateway(quote).get_market_snapshot(["HK.00700"])

        self.assertEqual(result.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(result.freshness_status, FreshnessStatus.UNKNOWN)

    def test_empty_account_risk_payload_fails_closed(self):
        class EmptyAccount(FakeAccountContext):
            def accinfo_query(self, **_kwargs):
                return 0, FakeFrame([])

            def position_list_query(self, **_kwargs):
                return 0, FakeFrame([])

        live = gateway(
            FakeQuoteContext(),
            account_context_factory=lambda _binding: EmptyAccount(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )

        result = live.get_account_risk_summary("demo")

        self.assertEqual(result.typed_error.code, GatewayErrorCode.NOT_FOUND)

    def test_account_entitlement_is_labeled_as_account_read(self):
        class Denied(FakeAccountContext):
            def accinfo_query(self, **_kwargs):
                return -1, "permission denied"

        live = gateway(
            FakeQuoteContext(),
            account_context_factory=lambda _binding: Denied(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )

        result = live.get_account_risk_summary("demo")

        self.assertEqual(result.entitlements, {"account_read": "denied"})

    def test_public_errors_do_not_echo_provider_paths_tokens_or_ids(self):
        class Leaky(FakeQuoteContext):
            def get_market_state(self, _codes):
                return -1, r"C:\Users\Admin\secret token=abc acc_id=123456789"

        result = gateway(Leaky()).get_market_state(["HK.00700"])

        self.assertNotIn("C:\\Users", result.typed_error.message)
        self.assertNotIn("abc", result.typed_error.message)
        self.assertNotIn("123456789", result.typed_error.message)

    def test_option_condition_contract_matches_futu_enum(self):
        with self.assertRaises(ValueError):
            OptionChainRequest(
                "HK.00700",
                "2026-08-28",
                "2026-08-28",
                option_cond_type="STANDARD",
            )


class OrchestrationIsolationTests(unittest.TestCase):
    def test_component_error_or_bad_integrity_stops_later_calls(self):
        class Broken:
            mode = DataMode.REPLAY

            def __init__(self, tamper=False):
                self.calls = []
                self.tamper = tamper

            def health(self):
                self.calls.append("health")
                return envelope(DataMode.REPLAY, "health", {"ready": True})

            def capabilities(self):
                self.calls.append("capabilities")
                if self.tamper:
                    item = envelope(DataMode.REPLAY, "capabilities", {})
                    item.data["tampered"] = True
                    return item
                return DataEnvelope(
                    mode=DataMode.REPLAY,
                    origin_source="FUTU",
                    captured_at_utc="2026-08-12T02:00:01+00:00",
                    source_time_utc=None,
                    freshness_status=FreshnessStatus.FROZEN,
                    request={"operation": "capabilities"},
                    status=EnvelopeStatus.ERROR,
                    data=None,
                    entitlements={},
                    warnings=[],
                    typed_error=GatewayError(
                        code=GatewayErrorCode.UPSTREAM_ERROR,
                        message="unavailable",
                        retryable=True,
                    ),
                )

            def get_market_state(self, _codes):
                self.calls.append("market_state")
                return envelope(DataMode.REPLAY, "get_market_state", [])

        errored = Broken()
        tampered = Broken(tamper=True)
        scenario = {"underlying": "HK.00700", "expiry": "2026-08-28"}

        error_result = DecisionInputService(errored).refresh_decision_inputs(scenario)
        tampered_result = DecisionInputService(tampered).refresh_decision_inputs(scenario)

        self.assertEqual(errored.calls, ["health", "capabilities"])
        self.assertEqual(tampered.calls, ["health", "capabilities"])
        self.assertEqual(error_result.typed_error.code, GatewayErrorCode.UPSTREAM_ERROR)
        self.assertEqual(tampered_result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
    def test_stops_at_first_mode_mismatch(self):
        class Mixed:
            mode = DataMode.REPLAY

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return envelope(DataMode.REPLAY, "health", {"ready": True})

            def capabilities(self):
                self.calls.append("capabilities")
                return envelope(DataMode.LIVE, "capabilities", {})

            def get_market_state(self, _codes):
                self.calls.append("market_state")
                return envelope(DataMode.REPLAY, "get_market_state", [])

            def get_market_snapshot(self, _codes):
                self.calls.append("snapshot")
                return envelope(DataMode.REPLAY, "get_market_snapshot", [])

            def get_expiration_dates(self, _underlying):
                self.calls.append("expirations")
                return envelope(DataMode.REPLAY, "get_expiration_dates", [])

            def get_option_chain(self, _request):
                self.calls.append("chain")
                return envelope(DataMode.REPLAY, "get_option_chain", [])

        mixed = Mixed()
        result = DecisionInputService(mixed).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(mixed.calls, ["health", "capabilities"])

    def test_health_ready_false_stops_all_follow_up_calls(self):
        class NotReady:
            mode = DataMode.LIVE

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return envelope(DataMode.LIVE, "health", {"ready": False})

            def capabilities(self):
                self.calls.append("capabilities")
                return envelope(DataMode.LIVE, "capabilities", {})

        gateway_stub = NotReady()
        result = DecisionInputService(gateway_stub).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(gateway_stub.calls, ["health"])
        self.assertNotEqual(result.status, EnvelopeStatus.OK)

    def test_known_mixed_account_mode_never_calls_live_account_gateway(self):
        class ReplayMarket:
            mode = DataMode.REPLAY

            def health(self):
                return envelope(DataMode.REPLAY, "health", {"ready": True})

            def capabilities(self):
                return envelope(DataMode.REPLAY, "capabilities", {})

            def get_market_state(self, _codes):
                return envelope(DataMode.REPLAY, "get_market_state", [])

            def get_market_snapshot(self, _codes):
                return envelope(DataMode.REPLAY, "get_market_snapshot", [])

            def get_expiration_dates(self, _underlying):
                return envelope(DataMode.REPLAY, "get_expiration_dates", [])

            def get_option_chain(self, _request):
                return envelope(DataMode.REPLAY, "get_option_chain", [])

        class LiveAccount:
            mode = DataMode.LIVE

            def __init__(self):
                self.called = False

            def get_account_risk_summary(self, *_args, **_kwargs):
                self.called = True
                return envelope(DataMode.LIVE, "get_account_risk_summary", {})

        account = LiveAccount()
        result = DecisionInputService(ReplayMarket(), account).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28", "account_ref": "demo"}
        )

        self.assertFalse(account.called)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_raw_account_identifier_is_rejected_before_any_gateway_call(self):
        class Ledger:
            def __init__(self):
                self.called = False

            def health(self):
                self.called = True
                return envelope(DataMode.LIVE, "health", {"ready": True})

        ledger = Ledger()
        result = DecisionInputService(ledger).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28", "account_ref": 123456}
        )

        self.assertFalse(ledger.called)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)


class ReplayInputTests(unittest.TestCase):
    def test_wrong_shaped_replay_inputs_are_typed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            replay = ReplayGateway(temp_dir)

            state = replay.get_market_state(None)
            resolution = replay.resolve_option_code(
                "HK.00700", "2026-08-28", "bad", "CALL"
            )

        self.assertEqual(state.typed_error.code, GatewayErrorCode.INVALID_REQUEST)
        self.assertEqual(resolution.typed_error.code, GatewayErrorCode.INVALID_REQUEST)

    def test_legacy_fixtures_require_explicit_migration_mode(self):
        payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.PUT",
                    "option_type": "PUT",
                    "strike_time": "2026-08-28",
                    "strike_price": 500.0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "legacy.json").write_text(json.dumps(payload), encoding="utf-8")

            normal = ReplayGateway(temp_dir).health()
            migration = ReplayGateway(temp_dir, allow_legacy=True).health()

        self.assertEqual(normal.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING)
        self.assertEqual(migration.status, EnvelopeStatus.PARTIAL)
        self.assertFalse(migration.data["ready"])

    def test_legacy_duplicate_keys_and_empty_filters_fail_closed(self):
        duplicate = (
            '{"code":"HK.00700","code":"HK.09988",'
            '"data":[{"code":"HK.PUT","option_type":"PUT",'
            '"strike_time":"2026-08-28","strike_price":500.0}]}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "2026-08-12_legacy.json")
            path.write_text(duplicate, encoding="utf-8")
            corrupt = ReplayGateway(temp_dir, allow_legacy=True).health()

            path.write_text(
                json.dumps(
                    {
                        "code": "HK.00700",
                        "data": [
                            {
                                "code": "HK.PUT",
                                "option_type": "PUT",
                                "strike_time": "2026-08-28",
                                "strike_price": 500.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            missing = ReplayGateway(temp_dir, allow_legacy=True).get_option_chain(
                OptionChainRequest(
                    "HK.00700", "2026-08-28", "2026-08-28", option_type="CALL"
                )
            )

        self.assertEqual(corrupt.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(missing.typed_error.code, GatewayErrorCode.NOT_FOUND)

    def test_capabilities_are_derived_from_validated_operation_inventory(self):
        recorded = DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source="FUTU",
            captured_at_utc="2026-08-12T02:00:01+00:00",
            source_time_utc="2026-08-12T02:00:00+00:00",
            freshness_status=FreshnessStatus.FROZEN,
            request={"operation": "get_market_snapshot", "codes": ["HK.00700"]},
            status=EnvelopeStatus.OK,
            data=[{"code": "HK.00700", "last_price": 500.0}],
            entitlements={"recorded": True},
            warnings=[],
            typed_error=None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            SnapshotRecorder(temp_dir).record(recorded, "market")

            capabilities = ReplayGateway(temp_dir).capabilities()

        self.assertFalse(capabilities.data["account_read"])
        self.assertFalse(capabilities.data["execution"])

    def test_replay_and_recorder_enforce_bounded_files_and_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SnapshotRecorder(temp_dir)
            oversized = DataEnvelope(
                mode=DataMode.REPLAY,
                origin_source="TEST",
                captured_at_utc="2026-08-12T02:00:01+00:00",
                source_time_utc=None,
                freshness_status=FreshnessStatus.FROZEN,
                request={"operation": "oversized"},
                status=EnvelopeStatus.OK,
                data={"payload": "x" * (5 * 1024 * 1024)},
                entitlements={},
                warnings=[],
                typed_error=None,
            )

            with self.assertRaises(ValueError):
                recorder.record(oversized, "large")
            with self.assertRaises(ValueError):
                recorder.record(oversized, "x" * 65)

            for index in range(101):
                Path(temp_dir, f"fixture_{index:03d}.jsonl").write_text("", encoding="utf-8")
            health = ReplayGateway(temp_dir).health()

        self.assertEqual(health.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)


if __name__ == "__main__":
    unittest.main()
