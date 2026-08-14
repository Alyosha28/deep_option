from __future__ import annotations

import json
import math
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
    normalize_symbol,
)
from src.payload_validation import PayloadValidationError, validate_operation_payload
from src.replay_adapter import ReplayGateway
from src.models import Snapshot
from src.snapshot_recorder import SnapshotRecorder, iter_envelopes
from tests.fakes import FakeAccountContext, FakeQuoteContext


NOW = "2026-08-12T02:00:01+00:00"


def make_envelope(
    request: dict[str, object],
    data: object,
    *,
    mode: DataMode = DataMode.REPLAY,
    status: EnvelopeStatus = EnvelopeStatus.OK,
    typed_error: GatewayError | None = None,
    origin: str = "REPLAY",
    captured_at: str = NOW,
) -> DataEnvelope:
    return DataEnvelope(
        mode=mode,
        origin_source=origin,
        captured_at_utc=captured_at,
        source_time_utc=(None if mode is DataMode.REPLAY else "2026-08-12T02:00:00+00:00"),
        freshness_status=(
            FreshnessStatus.FROZEN if mode is DataMode.REPLAY else FreshnessStatus.FRESH
        ),
        request=request,
        status=status,
        data=data,
        entitlements={},
        warnings=[],
        typed_error=typed_error,
    )


class ContractDefensiveCoverageTests(unittest.TestCase):
    def test_gateway_error_rejects_untrusted_shapes_and_text(self):
        cases = (
            (TypeError, lambda: GatewayError(1, "safe", False)),
            (ValueError, lambda: GatewayError("NOPE", "safe", False)),
            (ValueError, lambda: GatewayError(GatewayErrorCode.INTERNAL_ERROR, "", False)),
            (ValueError, lambda: GatewayError(GatewayErrorCode.INTERNAL_ERROR, "token=secret", False)),
            (TypeError, lambda: GatewayError(GatewayErrorCode.INTERNAL_ERROR, "safe", 1)),
            (TypeError, lambda: GatewayError(GatewayErrorCode.INTERNAL_ERROR, "safe", False, [])),
            (TypeError, lambda: GatewayError.from_dict([])),
            (ValueError, lambda: GatewayError.from_dict({"code": "INTERNAL_ERROR"})),
        )
        for error_type, build in cases:
            with self.subTest(build=build), self.assertRaises(error_type):
                build()

        restored = GatewayError.from_dict(
            {"code": "INTERNAL_ERROR", "message": "safe", "retryable": False}
        )
        self.assertEqual(restored.code, GatewayErrorCode.INTERNAL_ERROR)

    def test_envelope_rejects_invalid_metadata_and_state_combinations(self):
        base = {
            "mode": DataMode.REPLAY,
            "origin_source": "REPLAY",
            "captured_at_utc": NOW,
            "source_time_utc": None,
            "freshness_status": FreshnessStatus.FROZEN,
            "request": {"operation": "health"},
            "status": EnvelopeStatus.OK,
            "data": {"ready": True, "server_version": None},
            "entitlements": {},
            "warnings": [],
            "typed_error": None,
        }
        changes = (
            {"schema_version": "2"},
            {"origin_source": "PRIVATE"},
            {"captured_at_utc": "bad"},
            {"captured_at_utc": "2026-08-12T02:00:01"},
            {"request": []},
            {"entitlements": []},
            {"warnings": [1]},
            {"warnings": ["C:\\secret\\file"]},
            {"typed_error": "bad"},
            {"status": EnvelopeStatus.ERROR},
            {
                "status": EnvelopeStatus.ERROR,
                "data": {"bad": True},
                "typed_error": GatewayError(GatewayErrorCode.INTERNAL_ERROR, "safe", False),
            },
            {"typed_error": GatewayError(GatewayErrorCode.INTERNAL_ERROR, "safe", False)},
            {"request": {"operation": "health", "acc_id": 1}},
            {"data": {"ready": True, "server_version": None, "token": "x"}},
            {"entitlements": {"market_data": "maybe"}},
            {"data": math.nan},
            {"data": {1: "bad"}},
            {"data": object()},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                DataEnvelope(**{**base, **change})

    def test_strict_deserialization_and_request_value_objects(self):
        item = make_envelope(
            {"operation": "health"},
            {"ready": True, "server_version": None},
        )
        payload = item.to_dict()
        cases = (
            (TypeError, lambda: DataEnvelope.from_dict([])),
            (ValueError, lambda: DataEnvelope.from_dict({**payload, "extra": 1})),
            (TypeError, lambda: DataEnvelope.from_dict({**payload, "snapshot_id": 1})),
            (ValueError, lambda: DataEnvelope.from_dict({**payload, "content_sha256": "0" * 64})),
            (ValueError, lambda: DataEnvelope.from_dict({**payload, "snapshot_id": "snap_wrong"})),
            (ValueError, lambda: DataEnvelope.from_json_line("")),
            (ValueError, lambda: DataEnvelope.from_json_line('{"x":1,"x":2}')),
        )
        for error_type, load in cases:
            with self.subTest(load=load), self.assertRaises(error_type):
                load()

        invalid_builders = (
            lambda: normalize_symbol(None),
            lambda: normalize_symbol("x" * 65 + ".A"),
            lambda: OptionChainRequest("HK.00700", "bad", "2026-08-28"),
            lambda: OptionChainRequest("HK.00700", "2026-08-29", "2026-08-28"),
            lambda: OptionChainRequest("HK.00700", "2026-08-01", "2026-09-01"),
            lambda: OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28", "BAD"),
            lambda: OptionChainRequest(
                "HK.00700", "2026-08-28", "2026-08-28", option_cond_type="BAD"
            ),
            lambda: OptionLeg("HK.CALL", "HOLD", 1),
            lambda: OptionLeg("HK.CALL", "BUY", True),
            lambda: OptionLeg("HK.CALL", "BUY", 0),
            lambda: OptionLeg("HK.CALL", "BUY", 1_000_001),
            lambda: AccountBinding("bad alias", 1),
            lambda: AccountBinding("demo", 0),
            lambda: AccountBinding("demo", 1, trd_env="REAL"),
            lambda: AccountBinding("demo", 1, currency="US"),
        )
        for build in invalid_builders:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()


class PayloadSemanticCoverageTests(unittest.TestCase):
    def assert_invalid(self, request: dict[str, object], data: object) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_operation_payload(request, data)

    def test_common_and_market_payload_failures(self):
        cases = (
            ({"operation": "unknown"}, {}),
            ({"operation": "health"}, {"ready": True, "server_version": None, "token": "x"}),
            ({"operation": "health"}, {}),
            ({"operation": "health"}, {"ready": True, "server_version": []}),
            (
                {"operation": "capabilities"},
                {"market_data": True},
            ),
            (
                {"operation": "capabilities"},
                {
                    "market_data": True,
                    "account_read": False,
                    "strategy_combination_quote": True,
                    "execution": True,
                    "real_trading": False,
                },
            ),
            ({"operation": "get_market_state", "codes": []}, []),
            ({"operation": "get_market_state", "codes": ["HK.00700"]}, [1]),
            (
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "bad", "market_state": "OPEN"}],
            ),
            (
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "HK.09988", "market_state": "OPEN"}],
            ),
            (
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "HK.00700", "market_state": ""}],
            ),
            (
                {"operation": "get_market_snapshot", "codes": ["HK.00700"]},
                [{"code": "HK.00700", "last_price": -1}],
            ),
            (
                {
                    "operation": "get_trading_days",
                    "start": "bad",
                    "end": "2026-08-12",
                },
                [],
            ),
            (
                {
                    "operation": "get_trading_days",
                    "start": "2026-08-12",
                    "end": "2026-08-13",
                },
                [{"date": "2026-08-14"}],
            ),
            ({"operation": "get_expiration_dates", "underlying": "bad"}, []),
            (
                {"operation": "get_expiration_dates", "underlying": "HK.00700"},
                [1],
            ),
            (
                {"operation": "get_expiration_dates", "underlying": "HK.00700"},
                [{"expiry": "bad"}],
            ),
        )
        for request, data in cases:
            with self.subTest(request=request, data=data):
                self.assert_invalid(request, data)


def make_live(
    quote: object | None = None,
    account: object | None = None,
    **kwargs: Any,
) -> FutuLiveGateway:
    quote_context = quote or FakeQuoteContext()
    account_context = account or FakeAccountContext()
    return FutuLiveGateway(
        quote_context_factory=lambda: quote_context,
        account_context_factory=lambda _binding: account_context,
        opend_probe=lambda *_args: True,
        clock=lambda: NOW,
        **kwargs,
    )


class FutuBoundaryCoverageTests(unittest.TestCase):
    def assert_invalid(self, request: dict[str, object], data: object) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_operation_payload(request, data)

    def test_constructor_rejects_unsafe_configuration(self):
        binding = AccountBinding("demo", 1)
        cases = (
            {"host": "localhost"},
            {"host": "8.8.8.8"},
            {"port": True},
            {"port": 70_000},
            {"freshness_max_age_seconds": math.nan},
            {"freshness_max_age_seconds": 0},
            {"account_bindings": {"other": binding}},
            {"account_bindings": {"demo": object()}},
            {"rate_limits": {"health": (11, 30.0)}},
            {"rate_limits": {"health": (10, 1.0)}},
            {"rate_limits": {"": (1, 30.0)}},
            {"rate_limits": {"x": (1,)}},
            {"rate_limits": {"x": (True, 30.0)}},
            {"rate_limits": {"x": (1, math.inf)}},
        )
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                FutuLiveGateway(**options)

    def test_probe_and_factory_failures_remain_typed(self):
        def exploding_probe(*_args: object) -> bool:
            raise RuntimeError("probe exploded")

        cases = (
            FutuLiveGateway(opend_probe=lambda *_args: False, clock=lambda: NOW),
            FutuLiveGateway(opend_probe=exploding_probe, clock=lambda: NOW),
            FutuLiveGateway(
                quote_context_factory=lambda: (_ for _ in ()).throw(ImportError("missing")),
                opend_probe=lambda *_args: True,
                clock=lambda: NOW,
            ),
            FutuLiveGateway(
                quote_context_factory=lambda: (_ for _ in ()).throw(
                    ConnectionRefusedError("refused")
                ),
                opend_probe=lambda *_args: True,
                clock=lambda: NOW,
            ),
        )
        expected = (
            GatewayErrorCode.OPEND_UNAVAILABLE,
            GatewayErrorCode.OPEND_UNAVAILABLE,
            GatewayErrorCode.SDK_UNAVAILABLE,
            GatewayErrorCode.OPEND_UNAVAILABLE,
        )
        self.assertEqual([item.health().typed_error.code for item in cases], list(expected))

        binding = AccountBinding("demo", 1)
        account_import = FutuLiveGateway(
            quote_context_factory=FakeQuoteContext,
            account_context_factory=lambda _binding: (_ for _ in ()).throw(ImportError()),
            account_bindings={"demo": binding},
            opend_probe=lambda *_args: True,
            clock=lambda: NOW,
        ).get_account_risk_summary("demo")
        self.assertEqual(account_import.typed_error.code, GatewayErrorCode.SDK_UNAVAILABLE)

    def test_context_manager_preserves_body_error_and_reports_explicit_close_failure(self):
        class BadClose(FakeQuoteContext):
            def close(self):
                raise RuntimeError("close failed")

        live = make_live(BadClose())
        self.assertEqual(live.health().status, EnvelopeStatus.OK)
        with self.assertRaises(RuntimeError):
            live.close()
        with self.assertRaises(RuntimeError):
            live.close()

        with self.assertRaisesRegex(ValueError, "body"):
            with make_live(BadClose()) as managed:
                managed.health()
                raise ValueError("body")

    def test_default_sdk_factories_and_enum_conversion_cover_the_read_surface(self):
        quote = FakeQuoteContext()
        account = FakeAccountContext()

        class Enum:
            pass

        class OptionStrategyLeg:
            pass

        security_firm = Enum()
        security_firm.FUTUSECURITIES = "firm"
        trade_market = Enum()
        trade_market.NONE = "none"
        trade_env = Enum()
        trade_env.SIMULATE = "simulate"
        currency = Enum()
        currency.HKD = "hkd"
        trade_date_market = Enum()
        trade_date_market.HK = "hk"
        option_type = Enum()
        option_type.CALL = "call"
        option_cond_type = Enum()
        option_cond_type.WITHIN = "within"
        action = Enum()
        action.BUY = "buy"
        action.SELL = "sell"

        fake_futu = types.ModuleType("futu")
        fake_futu.OpenQuoteContext = lambda **_kwargs: quote
        fake_futu.OpenSecTradeContext = lambda **_kwargs: account
        fake_futu.SecurityFirm = security_firm
        fake_futu.TrdMarket = trade_market
        fake_futu.TrdEnv = trade_env
        fake_futu.Currency = currency
        fake_futu.TradeDateMarket = trade_date_market
        fake_futu.OptionType = option_type
        fake_futu.OptionCondType = option_cond_type
        fake_futu.OptionStrategyLeg = OptionStrategyLeg
        fake_futu.OptionStrategyAction = action

        binding = AccountBinding("demo", 1)
        with patch.dict(sys.modules, {"futu": fake_futu}):
            live = FutuLiveGateway(
                account_bindings={"demo": binding},
                opend_probe=lambda *_args: True,
                clock=lambda: NOW,
            )
            with patch.object(
                live,
                "_query_account_worker",
                return_value=(
                    [{"total_assets": 100_000.0, "available_funds": 75_000.0}],
                    [{"code": "HK.00700", "quantity": 100.0, "market_value": 50_000.0}],
                ),
            ):
                results = (
                    live.health(),
                    live.capabilities(),
                    live.get_market_state(["HK.00700"]),
                    live.get_trading_days("HK", "2026-08-12", "2026-08-12"),
                    live.get_market_snapshot(["HK.00700"]),
                    live.get_expiration_dates("HK.00700"),
                    live.get_option_chain(
                        OptionChainRequest(
                            "HK.00700",
                            "2026-08-28",
                            "2026-08-28",
                            option_type="CALL",
                            option_cond_type="WITHIN",
                        )
                    ),
                    live.resolve_option_code("HK.00700", "2026-08-28", 500, "C"),
                    live.get_option_quotes([OptionLeg("HK.CALL", "BUY", 1)]),
                    live.get_strategy_quote([OptionLeg("HK.CALL", "BUY", 1)]),
                    live.get_account_risk_summary("demo", ["HK.00700"]),
                )
            live.close()

        self.assertTrue(
            all(result.status is EnvelopeStatus.OK for result in results),
            [
                (
                    result.request.get("operation"),
                    result.status.value,
                    result.typed_error.code.value if result.typed_error else None,
                )
                for result in results
            ],
        )
        self.assertTrue(quote.closed)

    def test_sdk_shape_incompatibilities_are_typed(self):
        class NoTimeout:
            closed = False

            def close(self):
                self.closed = True

        fake_futu = types.ModuleType("futu")
        context = NoTimeout()
        fake_futu.OpenQuoteContext = lambda **_kwargs: context
        with patch.dict(sys.modules, {"futu": fake_futu}):
            result = FutuLiveGateway(
                opend_probe=lambda *_args: True,
                clock=lambda: NOW,
            ).health()
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SDK_INCOMPATIBLE)
        self.assertTrue(context.closed)

        with patch.dict(sys.modules, {"futu": types.ModuleType("futu")}):
            with self.assertRaises(Exception) as caught:
                FutuLiveGateway._sdk_enum("MissingEnum", "VALUE")
        self.assertEqual(caught.exception.code, GatewayErrorCode.SDK_INCOMPATIBLE)

    def test_public_validation_and_upstream_error_mappings(self):
        live = make_live()
        invalid = (
            live.get_trading_days("", "2026-08-12", "2026-08-12"),
            live.get_trading_days("HK", "bad", "2026-08-12"),
            live.get_trading_days("HK", "2026-08-13", "2026-08-12"),
            live.get_expiration_dates(None),
            live.get_option_chain(None),
            live.get_option_quotes([]),
            live.get_strategy_quote([]),
            live.get_account_risk_summary("123456"),
            live.get_account_risk_summary("demo", codes=[]),
        )
        self.assertTrue(
            all(item.typed_error.code is GatewayErrorCode.INVALID_REQUEST for item in invalid)
        )

        for raw, account, expected in (
            ("rate limit", False, GatewayErrorCode.RATE_LIMITED),
            ("ordinary failure", False, GatewayErrorCode.UPSTREAM_ERROR),
            ("ordinary failure", True, GatewayErrorCode.ACCOUNT_UNAVAILABLE),
        ):
            with self.subTest(raw=raw, account=account), self.assertRaises(Exception) as caught:
                live._ensure_ok(-1, raw, account=account)
            self.assertEqual(caught.exception.code, expected)

    def test_normalization_helpers_cover_vendor_shape_edges(self):
        live = make_live()

        class Scalar:
            def item(self):
                return 7

        class BadScalar:
            def item(self):
                raise ValueError

            name = "ENUM_VALUE"

        class OddFrame:
            def to_dict(self, orient=None):
                if orient == "records":
                    raise ValueError
                return {"x": 1}

        self.assertEqual(live._records(None), [])
        self.assertEqual(live._records(OddFrame()), [{"x": 1}])
        self.assertEqual(live._records({"x": 1}), [{"x": 1}])
        self.assertEqual(live._records([1, {"x": 2}]), [{"value": 1}, {"x": 2}])
        self.assertEqual(live._records("x"), [{"value": "x"}])
        self.assertEqual(live._normal_value(Scalar()), 7)
        self.assertEqual(live._normal_value(BadScalar()), "ENUM_VALUE")
        self.assertIsNone(live._normal_value(" N/A "))
        self.assertIsNone(live._normal_value(float("inf")))
        self.assertEqual(live._normal_value({"x": (1, "NA")}), {"x": [1, None]})
        self.assertEqual(
            live._normal_value(datetime(2026, 8, 12, tzinfo=timezone.utc)),
            "2026-08-12T00:00:00+00:00",
        )
        self.assertEqual(live._object_mapping({"x": 1}), {"x": 1})
        self.assertEqual(live._object_mapping(OddFrame()), {"x": 1})
        self.assertEqual(live._object_mapping(object()), {})
        self.assertEqual(live._first_time([{"x": None}, {"x": 2}], "x"), 2)
        self.assertIsNone(live._first_time([], "x"))

        self.assertIsNone(live._source_time(True))
        self.assertIsNone(live._source_time(-1))
        self.assertIsNone(live._source_time("bad"))
        self.assertIsNone(live._source_time("2026-08-12T02:00:00"))
        self.assertEqual(
            live._snapshot_source_time("2026-08-12 10:00:00", "HK.00700"),
            "2026-08-12T02:00:00+00:00",
        )
        self.assertIsNone(live._snapshot_source_time("bad", "XX.1"))
        self.assertTrue(live._is_explicit_true("yes"))
        self.assertTrue(live._is_explicit_false("not_logged_in"))
        self.assertFalse(live._same_option_contract({}, {}))
        self.assertFalse(
            live._same_option_contract(
                {"option_type": "CALL", "expiry": "x", "strike": 1, "contract_size": 1},
                {"option_type": "PUT", "expiry": "x", "strike": 1, "contract_size": 1},
            )
        )
        redacted = live._safe_message("token=secret C:\\private\\x 1234567")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("1234567", redacted)

    def test_option_and_account_payload_failures(self):
        chain_request = {
            "operation": "get_option_chain",
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "CALL",
        }
        valid_row = {
            "code": "HK.CALL",
            "underlying": "HK.00700",
            "expiry": "2026-08-28",
            "option_type": "CALL",
            "strike": 500.0,
        }
        quote_request = {
            "operation": "get_option_quotes",
            "legs": [{"code": "HK.CALL", "action": "BUY", "quantity": 1}],
        }
        account_request = {"operation": "get_account_risk_summary", "account_ref": "demo"}
        valid_account = {
            "account_ref": "demo",
            "currency": "HKD",
            "total_assets": 100.0,
            "available_funds": 50.0,
            "positions": [],
        }
        cases = (
            ({**chain_request, "underlying": "bad"}, [valid_row]),
            (chain_request, [1]),
            (chain_request, [{**valid_row, "code": "bad"}]),
            (chain_request, [valid_row, valid_row]),
            (chain_request, [{**valid_row, "underlying": "HK.09988"}]),
            (chain_request, [{**valid_row, "option_type": "PUT"}]),
            (chain_request, [{**valid_row, "expiry": "2026-08-29"}]),
            (chain_request, [{**valid_row, "strike": 0}]),
            ({"operation": "get_option_quotes", "legs": []}, []),
            (quote_request, [1]),
            (quote_request, [{"code": "HK.PUT", "price": 1.0}]),
            (quote_request, [{"code": "HK.CALL"}]),
            ({"operation": "get_strategy_quote"}, [{"bid1": 1.0}]),
            (
                {
                    "operation": "resolve_option_code",
                    "underlying": "HK.00700",
                    "expiry": "2026-08-28",
                    "option_type": "CALL",
                    "strike": 500.0,
                },
                [],
            ),
            (account_request, []),
            (account_request, {**valid_account, "account_ref": "other"}),
            (account_request, {**valid_account, "currency": "usd"}),
            (
                {**account_request, "codes": []},
                valid_account,
            ),
            (account_request, {**valid_account, "positions": [1]}),
            (
                account_request,
                {
                    **valid_account,
                    "positions": [{"code": "bad", "quantity": 1, "market_value": 1}],
                },
            ),
            (account_request, {**valid_account, "total_assets": -1}),
            (
                account_request,
                {**valid_account, "available_funds": None},
            ),
        )
        for request, data in cases:
            with self.subTest(request=request, data=data):
                self.assert_invalid(request, data)


class ReplayBoundaryCoverageTests(unittest.TestCase):
    def test_canonical_inventory_replays_the_complete_read_surface(self):
        legs = [{"code": "HK.CALL", "action": "BUY", "quantity": 1}]
        chain_request = {
            "operation": "get_option_chain",
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "CALL",
            "option_cond_type": "ALL",
        }
        chain_row = {
            "code": "HK.CALL",
            "underlying": "HK.00700",
            "expiry": "2026-08-28",
            "option_type": "CALL",
            "strike": 500.0,
        }
        fixtures = (
            make_envelope(
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "HK.00700", "market_state": "MORNING"}],
            ),
            make_envelope(
                {
                    "operation": "get_trading_days",
                    "market": "HK",
                    "start": "2026-08-12",
                    "end": "2026-08-12",
                },
                [{"date": "2026-08-12", "trade_date_type": "WHOLE"}],
            ),
            make_envelope(
                {"operation": "get_market_snapshot", "codes": ["HK.00700"]},
                [{"code": "HK.00700", "last_price": 500.0}],
            ),
            make_envelope(
                {"operation": "get_expiration_dates", "underlying": "HK.00700"},
                [{"expiry": "2026-08-28"}],
            ),
            make_envelope(chain_request, [chain_row]),
            make_envelope(
                {
                    "operation": "resolve_option_code",
                    "underlying": "HK.00700",
                    "expiry": "2026-08-28",
                    "strike": 500.0,
                    "option_type": "CALL",
                },
                chain_row,
            ),
            make_envelope(
                {"operation": "get_option_quotes", "legs": legs},
                [{"code": "HK.CALL", "mid_price": 2.5}],
            ),
            make_envelope(
                {"operation": "get_strategy_quote", "legs": legs},
                [{"bid1": 2.0, "ask1": 3.0}],
            ),
            make_envelope(
                {"operation": "get_account_risk_summary", "account_ref": "demo"},
                {
                    "account_ref": "demo",
                    "currency": "HKD",
                    "total_assets": 100_000.0,
                    "available_funds": 75_000.0,
                    "positions": [],
                },
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            recorder = SnapshotRecorder(directory)
            for fixture in fixtures:
                recorder.record(fixture, tag="bundle")
            gateway = ReplayGateway(directory)
            option_leg = OptionLeg("HK.CALL", "BUY", 1)
            results = (
                gateway.health(),
                gateway.capabilities(),
                gateway.get_market_state(["hk.00700", "HK.00700"]),
                gateway.get_trading_days("hk", "2026-08-12", "2026-08-12"),
                gateway.get_market_snapshot("hk.00700"),
                gateway.get_expiration_dates("hk.00700"),
                gateway.get_option_chain(
                    OptionChainRequest(
                        "HK.00700", "2026-08-28", "2026-08-28", option_type="CALL"
                    )
                ),
                gateway.resolve_option_code("HK.00700", "2026-08-28", 500, "C"),
                gateway.get_option_quotes([option_leg]),
                gateway.get_option_quote([option_leg]),
                gateway.get_strategy_quote([option_leg]),
                gateway.get_account_risk_summary("demo"),
            )
            gateway.close()

        self.assertTrue(all(result.status is EnvelopeStatus.OK for result in results))
        self.assertTrue(all(result.verify_integrity() for result in results))
        self.assertTrue(results[1].data["market_data"])
        self.assertTrue(results[1].data["account_read"])
        self.assertEqual(results[-1].data["positions"], [])

    def test_as_of_window_is_fixed_and_order_independent(self):
        request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        fixture = make_envelope(
            request,
            [{"code": "HK.00700", "last_price": 500.0}],
            captured_at="2026-08-12T02:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            SnapshotRecorder(directory).record(fixture)
            exact = ReplayGateway(directory, as_of_utc="2026-08-12T02:00:00+00:00")
            future_only = ReplayGateway(directory, as_of_utc="2026-08-12T01:59:59+00:00")
            stale = ReplayGateway(
                directory,
                as_of_utc="2026-08-12T02:01:01+00:00",
                max_capture_skew_seconds=60,
            )

            exact_result = exact.get_market_snapshot(["HK.00700"])
            future_result = future_only.get_market_snapshot(["HK.00700"])
            stale_health = stale.health()
            stale_result = stale.get_market_snapshot(["HK.00700"])

        self.assertEqual(exact_result.status, EnvelopeStatus.OK)
        self.assertEqual(
            future_result.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING
        )
        self.assertEqual(stale_result.typed_error.code, GatewayErrorCode.STALE_DATA)
        self.assertEqual(stale_health.status, EnvelopeStatus.PARTIAL)
        self.assertFalse(stale_health.data["ready"])

    def test_constructor_and_public_request_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            for kwargs, error_type in (
                ({"allow_legacy": 1}, TypeError),
                ({"max_capture_skew_seconds": True}, ValueError),
                ({"max_capture_skew_seconds": -1}, ValueError),
                ({"max_capture_skew_seconds": math.inf}, ValueError),
                ({"as_of_utc": "2026-08-12T02:00:00"}, ValueError),
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(error_type):
                    ReplayGateway(directory, **kwargs)

            gateway = ReplayGateway(directory)
            too_many_codes = [f"HK.X{index}" for index in range(801)]
            too_many_legs = [OptionLeg(f"HK.C{index}", "BUY", 1) for index in range(9)]
            invalid = (
                gateway.get_market_state([]),
                gateway.get_market_snapshot(too_many_codes),
                gateway.get_trading_days("", "2026-08-12", "2026-08-12"),
                gateway.get_trading_days("HK", "2026-08-13", "2026-08-12"),
                gateway.get_trading_days("HK", "2026-01-01", "2027-01-02"),
                gateway.get_expiration_dates("bad"),
                gateway.get_option_chain(None),  # type: ignore[arg-type]
                gateway.resolve_option_code("HK.00700", "2026-08-28", math.nan, "CALL"),
                gateway.resolve_option_code("HK.00700", "2026-08-28", 0, "CALL"),
                gateway.get_option_quotes([]),
                gateway.get_strategy_quote(too_many_legs),
                gateway.get_account_risk_summary("bad alias"),
                gateway.get_account_risk_summary("demo", []),
            )

        self.assertTrue(
            all(
                result.status is EnvelopeStatus.ERROR
                and result.typed_error.code is GatewayErrorCode.INVALID_REQUEST
                for result in invalid
            )
        )

    def test_corrupt_and_bounded_legacy_inventory_report_schema_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "capture.jsonl"
            corrupt.write_text('{"not":"an envelope"}\n', encoding="utf-8")
            self.assertEqual(
                ReplayGateway(directory).health().typed_error.code,
                GatewayErrorCode.SCHEMA_MISMATCH,
            )

        legacy_payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.CALL",
                    "option_type": "CALL",
                    "strike_price": 500.0,
                    "strike_time": "2026-08-28",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "2026-08-12_chain.json"
            legacy.write_text(
                "2026-08-12 10:00:00,000 " + json.dumps(legacy_payload),
                encoding="utf-8",
            )
            gateway = ReplayGateway(directory, allow_legacy=True)
            call = gateway.get_option_chain(
                OptionChainRequest(
                    "HK.00700", "2026-08-28", "2026-08-28", option_type="CALL"
                )
            )
            put = gateway.get_option_chain(
                OptionChainRequest(
                    "HK.00700", "2026-08-28", "2026-08-28", option_type="PUT"
                )
            )
            conditioned = gateway.get_option_chain(
                OptionChainRequest(
                    "HK.00700",
                    "2026-08-28",
                    "2026-08-28",
                    option_cond_type="WITHIN",
                )
            )
            capabilities = gateway.capabilities()

        self.assertEqual(call.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(put.typed_error.code, GatewayErrorCode.NOT_FOUND)
        self.assertEqual(
            conditioned.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING
        )
        self.assertEqual(capabilities.status, EnvelopeStatus.PARTIAL)
        self.assertTrue(capabilities.data["market_data"])

        with tempfile.TemporaryDirectory() as directory:
            excessive = Path(directory) / "legacy.json"
            excessive.write_text("{" * 257, encoding="utf-8")
            result = ReplayGateway(directory, allow_legacy=True).health()
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)


class SnapshotRecorderBoundaryCoverageTests(unittest.TestCase):
    def test_recording_round_trip_and_bounded_failures(self):
        fixture = make_envelope(
            {"operation": "get_market_snapshot", "codes": ["HK.00700"]},
            [{"code": "HK.00700", "last_price": 500.0}],
        )
        with tempfile.TemporaryDirectory() as directory:
            recorder = SnapshotRecorder(directory)
            path = recorder.record(fixture, tag="audit")
            self.assertEqual(list(iter_envelopes(path)), [fixture])
            self.assertEqual(recorder.list_files("audit"), [path])
            self.assertEqual(recorder.list_files("other"), [])

            with self.assertRaises(TypeError):
                recorder.record(object())  # type: ignore[arg-type]
            for bad_tag in ("", "../escape", "x" * 65, 1):
                with self.subTest(tag=bad_tag), self.assertRaises(ValueError):
                    recorder.record(fixture, tag=bad_tag)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                recorder.record(Snapshot("test", {}, captured_at="invalid"))
            with patch("src.snapshot_recorder._MAX_RECORD_BYTES", 1):
                with self.assertRaises(ValueError):
                    recorder.record(fixture)
                with self.assertRaises(ValueError):
                    list(iter_envelopes(path))
            with patch("src.snapshot_recorder._MAX_RECORDS_PER_FILE", 1):
                with self.assertRaises(ValueError):
                    recorder.record(fixture, tag="audit")

            tampered = fixture.to_dict()
            tampered["content_sha256"] = "0" * 64
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n" + json.dumps(tampered) + "\n")
            with self.assertRaisesRegex(ValueError, "Invalid DataEnvelope"):
                list(iter_envelopes(path))
            with self.assertRaises(ValueError):
                list(iter_envelopes(Path(directory) / "missing.jsonl"))


class DecisionGatewayStub:
    mode = DataMode.REPLAY

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_on: str | None = None
        self.ready = True
        self.market_data = True
        self.account_read = True
        self.expirations = ["2026-08-28"]

    def _load(self, name: str, request: dict[str, object], data: object) -> DataEnvelope:
        self.calls.append(name)
        if self.raise_on == name:
            raise RuntimeError(f"{name} failed")
        return make_envelope(request, data)

    def health(self) -> DataEnvelope:
        return self._load(
            "health",
            {"operation": "health"},
            {"ready": self.ready, "server_version": None},
        )

    def capabilities(self) -> DataEnvelope:
        return self._load(
            "capabilities",
            {"operation": "capabilities"},
            {
                "market_data": self.market_data,
                "account_read": self.account_read,
                "strategy_combination_quote": True,
                "execution": False,
                "real_trading": False,
            },
        )

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        return self._load(
            "market_state",
            {"operation": "get_market_state", "codes": codes},
            [{"code": code, "market_state": "MORNING"} for code in codes],
        )

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        return self._load(
            "snapshot",
            {"operation": "get_market_snapshot", "codes": codes},
            [{"code": code, "last_price": 500.0} for code in codes],
        )

    def get_expiration_dates(self, underlying: str) -> DataEnvelope:
        return self._load(
            "expirations",
            {"operation": "get_expiration_dates", "underlying": underlying},
            [{"expiry": expiry} for expiry in self.expirations],
        )

    def get_option_chain(self, request: OptionChainRequest) -> DataEnvelope:
        return self._load(
            "chain",
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

    def get_account_risk_summary(
        self, account_ref: str, *, codes: list[str]
    ) -> DataEnvelope:
        return self._load(
            "account",
            {
                "operation": "get_account_risk_summary",
                "account_ref": account_ref,
                "codes": codes,
            },
            {
                "account_ref": account_ref,
                "currency": "HKD",
                "total_assets": 100_000.0,
                "available_funds": 75_000.0,
                "positions": [],
            },
        )


class DecisionInputCoverageTests(unittest.TestCase):
    scenario = {"underlying": "HK.00700", "expiry": "2026-08-28"}

    def test_constructor_and_scenario_validation_boundaries(self):
        for kwargs in (
            {"refresh_limit": (0, 30.0)},
            {"refresh_limit": (11, 30.0)},
            {"refresh_limit": (1, 29.0)},
            {"max_refresh_seconds": 0},
            {"max_refresh_seconds": math.inf},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DecisionInputService(DecisionGatewayStub(), **kwargs)

        invalid = (
            None,
            {},
            {"underlying": "HK.00700"},
            {**self.scenario, "option_type": "BAD"},
            {**self.scenario, "account_ref": 123},
            {**self.scenario, "account_ref": "bad alias"},
            {**self.scenario, "account_ref": "demo", "position_codes": object()},
            {**self.scenario, "account_ref": "demo", "position_codes": []},
            {
                **self.scenario,
                "account_ref": "demo",
                "position_codes": [f"HK.{index:05d}" for index in range(801)],
            },
            {**self.scenario, "account_ref": "demo", "position_codes": ["bad"]},
        )
        service = DecisionInputService(DecisionGatewayStub())
        for scenario in invalid:
            with self.subTest(scenario=scenario):
                result = service.refresh_decision_inputs(scenario)
                self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)

    def test_happy_account_refresh_aggregates_auditable_evidence(self):
        gateway = DecisionGatewayStub()
        result = DecisionInputService(gateway).refresh_decision_inputs(
            {
                **self.scenario,
                "account_ref": "demo",
                "position_codes": "hk.00700",
            }
        )

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertIn("account_risk", result.data)
        self.assertEqual(result.data["evidence"]["option_chain"]["origin_source"], "REPLAY")
        self.assertEqual(gateway.calls[-1], "account")

    def test_health_component_and_deadline_fail_fast_paths(self):
        health_error = DecisionGatewayStub()
        health_error.raise_on = "health"
        result = DecisionInputService(health_error).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.INTERNAL_ERROR)

        not_ready = DecisionGatewayStub()
        not_ready.ready = False
        result = DecisionInputService(not_ready).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)
        self.assertEqual(not_ready.calls, ["health"])

        component_error = DecisionGatewayStub()
        component_error.raise_on = "market_state"
        result = DecisionInputService(component_error).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.INTERNAL_ERROR)

        times = iter((0.0, 0.0, 31.0))
        result = DecisionInputService(
            DecisionGatewayStub(), monotonic=lambda: next(times)
        ).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.UPSTREAM_ERROR)

    def test_capability_expiration_account_and_mode_short_circuits(self):
        no_market = DecisionGatewayStub()
        no_market.market_data = False
        result = DecisionInputService(no_market).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ENTITLEMENT_DENIED)

        missing_expiry = DecisionGatewayStub()
        missing_expiry.expirations = ["2026-09-25"]
        result = DecisionInputService(missing_expiry).refresh_decision_inputs(self.scenario)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

        no_account_capability = DecisionGatewayStub()
        no_account_capability.account_read = False
        result = DecisionInputService(no_account_capability).refresh_decision_inputs(
            {**self.scenario, "account_ref": "demo"}
        )
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)

        class NoAccountMethod:
            mode = DataMode.REPLAY

        gateway = DecisionGatewayStub()
        result = DecisionInputService(gateway, account_gateway=NoAccountMethod()).refresh_decision_inputs(
            {**self.scenario, "account_ref": "demo"}
        )
        self.assertEqual(result.typed_error.code, GatewayErrorCode.ACCOUNT_UNAVAILABLE)

        class LiveAccount(NoAccountMethod):
            mode = DataMode.LIVE

        result = DecisionInputService(gateway, account_gateway=LiveAccount()).refresh_decision_inputs(
            {**self.scenario, "account_ref": "demo"}
        )
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_account_exception_refresh_limit_and_validation_helpers(self):
        gateway = DecisionGatewayStub()
        gateway.raise_on = "account"
        result = DecisionInputService(gateway).refresh_decision_inputs(
            {**self.scenario, "account_ref": "demo"}
        )
        self.assertEqual(result.typed_error.code, GatewayErrorCode.INTERNAL_ERROR)

        limited = DecisionInputService(
            DecisionGatewayStub(), refresh_limit=(1, 30.0), monotonic=lambda: 0.0
        )
        self.assertEqual(limited.refresh_decision_inputs(self.scenario).status, EnvelopeStatus.OK)
        self.assertEqual(
            limited.refresh_decision_inputs(self.scenario).typed_error.code,
            GatewayErrorCode.RATE_LIMITED,
        )

        error_request = {"operation": "refresh_decision_inputs"}
        invalid = DecisionInputService._validate_envelope(
            object(),
            expected_mode=DataMode.REPLAY,
            expected_request={"operation": "health"},
            error_request=error_request,
        )
        wrong_mode = DecisionInputService._validate_envelope(
            make_envelope(
                {"operation": "health"},
                {"ready": True, "server_version": None},
                mode=DataMode.LIVE,
                origin="FUTU",
            ),
            expected_mode=DataMode.REPLAY,
            expected_request={"operation": "health"},
            error_request=error_request,
        )
        wrong_request = DecisionInputService._validate_envelope(
            make_envelope(
                {"operation": "health"},
                {"ready": True, "server_version": None},
            ),
            expected_mode=DataMode.REPLAY,
            expected_request={"operation": "capabilities"},
            error_request=error_request,
        )
        self.assertTrue(
            all(
                item is not None and item.typed_error.code is GatewayErrorCode.SCHEMA_MISMATCH
                for item in (invalid, wrong_mode, wrong_request)
            )
        )


if __name__ == "__main__":
    unittest.main()
