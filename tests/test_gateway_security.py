from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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
from src.snapshot_recorder import SnapshotRecorder, iter_envelopes
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
    requests = {
        "get_market_state": {"operation": operation, "codes": ["HK.00700"]},
        "get_market_snapshot": {"operation": operation, "codes": ["HK.00700"]},
        "get_expiration_dates": {"operation": operation, "underlying": "HK.00700"},
        "get_option_chain": {
            "operation": operation,
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "ALL",
            "option_cond_type": "ALL",
        },
    }
    if operation == "health" and isinstance(data, dict):
        data = {"server_version": None, **data}
    elif operation == "capabilities" and data == {}:
        data = {
            "market_data": True,
            "account_read": False,
            "strategy_combination_quote": True,
            "execution": False,
            "real_trading": False,
        }
    elif operation == "get_market_state" and data == []:
        data = [{"code": "HK.00700", "market_state": "MORNING"}]
    elif operation == "get_market_snapshot" and data == []:
        data = [{"code": "HK.00700", "last_price": 500.0}]
    elif operation == "get_expiration_dates" and data == []:
        data = [{"expiry": "2026-08-28"}]
    elif operation == "get_option_chain" and data == []:
        data = [
            {
                "code": "HK.CALL",
                "underlying": "HK.00700",
                "expiry": "2026-08-28",
                "option_type": "CALL",
                "strike": 500.0,
            }
        ]
    return DataEnvelope(
        mode=mode,
        origin_source="FUTU",
        captured_at_utc="2026-08-12T02:00:01+00:00",
        source_time_utc="2026-08-12T02:00:00+00:00",
        freshness_status=(
            FreshnessStatus.FROZEN if mode is DataMode.REPLAY else FreshnessStatus.FRESH
        ),
        request=requests.get(operation, {"operation": operation}),
        status=status,
        data=data,
        entitlements={},
        warnings=[],
        typed_error=None,
    )


class LiveSchemaAndLimitTests(unittest.TestCase):
    def test_default_account_worker_has_a_hard_parent_deadline(self):
        live = gateway(
            FakeQuoteContext(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )
        with patch(
            "src.futu_adapter.subprocess.run",
            side_effect=subprocess.TimeoutExpired("worker", 20.0),
        ):
            result = live.get_account_risk_summary("demo")

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)
        self.assertTrue(result.typed_error.retryable)

    def test_closed_gateway_never_launches_default_account_worker(self):
        live = gateway(
            FakeQuoteContext(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )
        live.close()

        with patch("src.futu_adapter.subprocess.run") as launch:
            result = live.get_account_risk_summary("demo")

        launch.assert_not_called()
        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)

    def test_default_account_worker_success_is_schema_checked_and_redacted(self):
        live = gateway(
            FakeQuoteContext(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )
        worker_payload = {
            "ok": True,
            "info": [
                {
                    "total_assets": 100_000.0,
                    "available_funds": 75_000.0,
                    "currency": "HKD",
                }
            ],
            "positions": [
                {
                    "code": "HK.00700",
                    "quantity": 100.0,
                    "market_value": 50_000.0,
                    "currency": "HKD",
                }
            ],
        }
        completed = subprocess.CompletedProcess(
            args=["worker"],
            returncode=0,
            stdout=json.dumps(worker_payload),
            stderr="",
        )
        with patch("src.futu_adapter.subprocess.run", return_value=completed) as launch:
            result = live.get_account_risk_summary("demo", ["HK.00700"])

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data["positions"][0]["code"], "HK.00700")
        self.assertNotIn("123456", result.to_json_line())
        command = launch.call_args.args[0]
        self.assertNotIn("123456", " ".join(command))

    def test_sdk_import_error_is_constant_and_redacted(self):
        def missing_sdk():
            raise ImportError("/home/alice/private/sdk.py bearer top-secret")

        live = FutuLiveGateway(
            quote_context_factory=missing_sdk,
            opend_probe=lambda *_args: True,
            clock=lambda: NOW,
        )

        result = live.health()

        self.assertEqual(result.typed_error.code, GatewayErrorCode.SDK_UNAVAILABLE)
        self.assertNotIn("/home", result.typed_error.message)
        self.assertNotIn("top-secret", result.typed_error.message)

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

    def test_crossed_strategy_market_fails_closed(self):
        class Crossed(FakeQuoteContext):
            def get_option_strategy_analysis(self, _legs):
                return 0, FakeFrame([{"bid1": 21.0, "ask1": 20.0}])

        result = gateway(Crossed()).get_strategy_quote(
            [OptionLeg("HK.CALL", "BUY", 1)]
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_option_quote_cardinality_must_match_requested_legs(self):
        class DuplicateRows(FakeQuoteContext):
            def get_option_quote(self, _legs):
                row = {
                    "price": 12.0,
                    "option_type": "CALL",
                    "expire_time": "2026-08-28",
                    "strike_price": 500.0,
                    "contract_size": 100.0,
                }
                return 0, FakeFrame([row, dict(row)])

        live = gateway(DuplicateRows())

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

    def test_any_future_timestamp_makes_multisymbol_snapshot_unknown(self):
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
                        "update_time": "2026-08-12T03:00:00+00:00",
                        "last_price": 100.0,
                    },
                ]
            )
        )

        result = gateway(quote).get_market_snapshot(["HK.00700", "HK.09988"])

        self.assertEqual(result.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(result.freshness_status, FreshnessStatus.UNKNOWN)

    def test_snapshot_requires_exact_code_and_timestamp_coverage(self):
        missing_code = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {
                        "code": "HK.00700",
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": 500.0,
                    }
                ]
            )
        )
        missing_time = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {"code": "HK.00700", "last_price": 500.0},
                    {
                        "code": "HK.09988",
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": 100.0,
                    },
                ]
            )
        )

        incomplete = gateway(missing_code).get_market_snapshot(["HK.00700", "HK.09988"])
        unknown_time = gateway(missing_time).get_market_snapshot(["HK.00700", "HK.09988"])

        self.assertEqual(incomplete.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(unknown_time.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(unknown_time.freshness_status, FreshnessStatus.UNKNOWN)

    def test_live_payloads_reject_wrong_financial_types_and_contract_identity(self):
        class BadPayload(FakeQuoteContext):
            def get_option_chain(self, _code, **_kwargs):
                return 0, FakeFrame(
                    [
                        {
                            "code": "HK.BAD",
                            "stock_owner": "HK.09988",
                            "strike_time": "2026-09-30",
                            "option_type": "CALL",
                            "strike_price": "NaN",
                        }
                    ]
                )

        bad_snapshot = FakeQuoteContext(
            snapshot_data=FakeFrame(
                [
                    {
                        "code": "HK.00700",
                        "update_time": "2026-08-12T02:00:00+00:00",
                        "last_price": "not-a-price",
                    }
                ]
            )
        )
        request = OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28", "CALL")

        snapshot = gateway(bad_snapshot).get_market_snapshot(["HK.00700"])
        chain = gateway(BadPayload()).get_option_chain(request)

        self.assertEqual(snapshot.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(chain.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_provider_rows_are_bounded_before_dataframe_materialization(self):
        class OversizedFrame:
            def __len__(self):
                return 10_001

            def to_dict(self, *_args, **_kwargs):
                raise AssertionError("oversized frames must not be materialized")

        class OversizedChain(FakeQuoteContext):
            def get_option_chain(self, _code, **_kwargs):
                return 0, OversizedFrame()

        result = gateway(OversizedChain()).get_option_chain(
            OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28")
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

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

    def test_account_risk_requires_one_complete_finite_summary(self):
        cases = (
            [{"total_assets": "unknown", "available_funds": 10.0}],
            [{"total_assets": 100.0}],
            [
                {"total_assets": 100.0, "available_funds": 90.0},
                {"total_assets": 200.0, "available_funds": 180.0},
            ],
        )
        for records in cases:
            with self.subTest(records=records):
                class AccountWithRecords(FakeAccountContext):
                    def accinfo_query(self, **kwargs):
                        self.calls.append(("accinfo_query", dict(kwargs)))
                        return 0, FakeFrame(records)

                account = AccountWithRecords()
                live = gateway(
                    FakeQuoteContext(),
                    account_context_factory=lambda _binding, account=account: account,
                    account_bindings={"demo": AccountBinding("demo", 123456)},
                )

                result = live.get_account_risk_summary("demo")

                self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_account_risk_rejects_malformed_positions_before_filtering(self):
        class MalformedPosition(FakeAccountContext):
            def position_list_query(self, **kwargs):
                self.calls.append(("position_list_query", dict(kwargs)))
                return 0, FakeFrame([{"quantity": "unknown"}])

        live = gateway(
            FakeQuoteContext(),
            account_context_factory=lambda _binding: MalformedPosition(),
            account_bindings={"demo": AccountBinding("demo", 123456)},
        )

        result = live.get_account_risk_summary("demo", codes=["HK.00700"])

        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_close_is_terminal_and_does_not_reconnect(self):
        contexts = []

        def factory():
            context = FakeQuoteContext()
            contexts.append(context)
            return context

        live = FutuLiveGateway(
            quote_context_factory=factory,
            opend_probe=lambda *_args: True,
            clock=lambda: NOW,
        )
        self.assertEqual(live.health().status, EnvelopeStatus.OK)
        live.close()

        after_close = live.health()

        self.assertEqual(len(contexts), 1)
        self.assertEqual(after_close.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)

    def test_close_failure_is_reported_and_retried(self):
        class FailOnceClose(FakeQuoteContext):
            def __init__(self):
                super().__init__()
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError("close failed")
                self.closed = True

        quote = FailOnceClose()
        live = gateway(quote)
        self.assertEqual(live.health().status, EnvelopeStatus.OK)

        with self.assertRaisesRegex(RuntimeError, "failed to close"):
            live.close()
        live.close()

        self.assertEqual(quote.close_calls, 2)
        self.assertTrue(quote.closed)

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
    def test_unavailable_capability_stops_before_market_calls(self):
        class NoMarketData:
            mode = DataMode.REPLAY

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return envelope(DataMode.REPLAY, "health", {"ready": True})

            def capabilities(self):
                self.calls.append("capabilities")
                return DataEnvelope(
                    mode=DataMode.REPLAY,
                    origin_source="FUTU",
                    captured_at_utc=NOW,
                    source_time_utc=None,
                    freshness_status=FreshnessStatus.FROZEN,
                    request={"operation": "capabilities"},
                    status=EnvelopeStatus.OK,
                    data={
                        "market_data": False,
                        "account_read": False,
                        "strategy_combination_quote": False,
                        "execution": False,
                        "real_trading": False,
                    },
                    entitlements={},
                    warnings=[],
                    typed_error=None,
                )

            def get_market_state(self, _codes):
                self.calls.append("market_state")
                raise AssertionError

        gateway_stub = NoMarketData()
        result = DecisionInputService(gateway_stub).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(gateway_stub.calls, ["health", "capabilities"])
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ENTITLEMENT_DENIED)

    def test_explicit_empty_position_codes_are_invalid(self):
        class NeverCalled:
            mode = DataMode.REPLAY

            def health(self):
                raise AssertionError

        service = DecisionInputService(NeverCalled())
        for value in ([], False, 0, ""):
            with self.subTest(value=value):
                result = service.refresh_decision_inputs(
                    {
                        "underlying": "HK.00700",
                        "expiry": "2026-08-28",
                        "account_ref": "demo",
                        "position_codes": value,
                    }
                )
                self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)

    def test_sensitive_invalid_scenario_values_return_constant_typed_errors(self):
        class NeverCalled:
            mode = DataMode.LIVE

            def health(self):
                raise AssertionError("invalid scenarios must fail before gateway access")

        service = DecisionInputService(NeverCalled())
        for unsafe_value in ("/root/private", "123456"):
            with self.subTest(value=unsafe_value):
                result = service.refresh_decision_inputs(
                    {
                        "underlying": "HK.00700",
                        "expiry": "2026-08-28",
                        "option_type": unsafe_value,
                    }
                )
                encoded = result.to_json_line()
                self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)
                self.assertNotIn(unsafe_value, encoded)

    def test_live_account_scenario_stops_when_trade_session_is_logged_out(self):
        class LoggedOut:
            mode = DataMode.LIVE

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return envelope(
                    DataMode.LIVE,
                    "health",
                    {"ready": True, "account_logged_in": 0},
                )

            def capabilities(self):
                self.calls.append("capabilities")
                return envelope(DataMode.LIVE, "capabilities", {})

        logged_out = LoggedOut()
        result = DecisionInputService(logged_out).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28", "account_ref": "demo"}
        )

        self.assertEqual(logged_out.calls, ["health"])
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)

    def test_refresh_rate_limit_fails_before_calling_gateway(self):
        class Ledger:
            mode = DataMode.REPLAY

            def __init__(self):
                self.calls = 0

            def health(self):
                self.calls += 1
                return envelope(DataMode.REPLAY, "health", {"ready": False})

        ledger = Ledger()
        service = DecisionInputService(
            ledger,
            refresh_limit=(2, 30.0),
            monotonic=lambda: 100.0,
        )
        scenario = {"underlying": "HK.00700", "expiry": "2026-08-28"}

        results = [service.refresh_decision_inputs(scenario) for _ in range(3)]

        self.assertEqual(ledger.calls, 2)
        self.assertEqual(results[-1].typed_error.code, GatewayErrorCode.RATE_LIMITED)

    def test_refresh_deadline_is_checked_after_the_final_component(self):
        current = [100.0]

        class SlowFinal:
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
                current[0] += 31.0
                return envelope(DataMode.REPLAY, "get_option_chain", [])

        result = DecisionInputService(
            SlowFinal(),
            monotonic=lambda: current[0],
        ).refresh_decision_inputs({"underlying": "HK.00700", "expiry": "2026-08-28"})

        self.assertEqual(result.typed_error.code, GatewayErrorCode.UPSTREAM_ERROR)

    def test_invalid_live_scenario_keeps_live_mode(self):
        class Live:
            mode = DataMode.LIVE

        result = DecisionInputService(Live()).refresh_decision_inputs({})

        self.assertEqual(result.mode, DataMode.LIVE)

    def test_wrong_operation_or_parameters_are_rejected_before_later_calls(self):
        class WrongResponse:
            mode = DataMode.REPLAY

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return envelope(DataMode.REPLAY, "health", {"ready": True})

            def capabilities(self):
                self.calls.append("capabilities")
                return envelope(DataMode.REPLAY, "get_market_state", {})

            def get_market_state(self, _codes):
                self.calls.append("market_state")
                return envelope(DataMode.REPLAY, "get_market_state", [])

        wrong = WrongResponse()
        result = DecisionInputService(wrong).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(wrong.calls, ["health", "capabilities"])
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
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
        for raw_ref in (123456, 0, False, [], {}, ""):
            with self.subTest(raw_ref=raw_ref):
                result = DecisionInputService(ledger).refresh_decision_inputs(
                    {
                        "underlying": "HK.00700",
                        "expiry": "2026-08-28",
                        "account_ref": raw_ref,
                    }
                )

                self.assertFalse(ledger.called)
                self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)


class ReplayInputTests(unittest.TestCase):
    def test_deeply_nested_snapshot_is_reported_as_schema_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested.jsonl"
            path.write_text(
                '{"data":' + "[" * 2000 + "0" + "]" * 2000 + "}",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid DataEnvelope"):
                next(iter_envelopes(path))

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
                origin_source="REPLAY",
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
