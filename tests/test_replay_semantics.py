from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayErrorCode,
    OptionChainRequest,
    OptionLeg,
)
from src.replay_adapter import ReplayGateway


def _envelope(
    request: dict[str, Any],
    data: Any,
    *,
    captured_at: str = "2026-08-12T02:00:00+00:00",
    status: EnvelopeStatus = EnvelopeStatus.OK,
) -> DataEnvelope:
    return DataEnvelope(
        mode=DataMode.LIVE,
        origin_source="FUTU",
        captured_at_utc=captured_at,
        source_time_utc=captured_at,
        freshness_status=FreshnessStatus.FRESH,
        request=request,
        status=status,
        data=data,
        entitlements={"recorded": True},
        warnings=[],
        typed_error=None,
    )


class ReplayPayloadSemanticsTests(unittest.TestCase):
    def _replay(self, request: dict[str, Any], data: Any, invoke: Any) -> DataEnvelope:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "fixture.jsonl")
            path.write_text(_envelope(request, data).to_json_line() + "\n", encoding="utf-8")
            return invoke(ReplayGateway(temp_dir))

    def assert_schema_mismatch(self, result: DataEnvelope) -> None:
        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        error = result.typed_error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertIsNone(result.data)

    def test_option_chain_rejects_empty_or_incomplete_ok_payloads(self):
        request = {
            "operation": "get_option_chain",
            "underlying": "HK.00700",
            "start": "2026-08-28",
            "end": "2026-08-28",
            "option_type": "ALL",
            "option_cond_type": "ALL",
        }
        selector = OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28")

        empty = self._replay(request, [], lambda gateway: gateway.get_option_chain(selector))
        incomplete = self._replay(
            request,
            [
                {
                    "code": "HK.TCH260828C500000",
                    "underlying": "HK.00700",
                    "option_type": "CALL",
                    "expiry": "2026-08-28",
                }
            ],
            lambda gateway: gateway.get_option_chain(selector),
        )

        self.assert_schema_mismatch(empty)
        self.assert_schema_mismatch(incomplete)

    def test_option_quotes_require_one_usable_row_per_leg(self):
        legs = [OptionLeg("HK.CALL", "BUY", 1), OptionLeg("HK.PUT", "SELL", 1)]
        request = {
            "operation": "get_option_quotes",
            "legs": [leg.to_dict() for leg in legs],
        }

        result = self._replay(
            request,
            [{"code": "HK.CALL", "price": 12.0}],
            lambda gateway: gateway.get_option_quotes(legs),
        )

        self.assert_schema_mismatch(result)

    def test_strategy_quote_requires_exactly_one_bid_ask_row(self):
        legs = [OptionLeg("HK.CALL", "BUY", 1)]
        request = {
            "operation": "get_strategy_quote",
            "legs": [leg.to_dict() for leg in legs],
        }

        result = self._replay(
            request,
            [{"bid1": 11.0}],
            lambda gateway: gateway.get_strategy_quote(legs),
        )

        self.assert_schema_mismatch(result)

    def test_account_risk_requires_a_core_funding_fact(self):
        request = {
            "operation": "get_account_risk_summary",
            "account_ref": "demo",
        }

        result = self._replay(
            request,
            {"account_ref": "demo", "positions": []},
            lambda gateway: gateway.get_account_risk_summary("demo"),
        )

        self.assert_schema_mismatch(result)

    def test_empty_positions_are_valid_when_account_facts_exist(self):
        request = {
            "operation": "get_account_risk_summary",
            "account_ref": "demo",
        }

        result = self._replay(
            request,
            {
                "account_ref": "demo",
                "currency": "HKD",
                "total_assets": 100_000.0,
                "available_funds": 75_000.0,
                "positions": [],
            },
            lambda gateway: gateway.get_account_risk_summary("demo"),
        )

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.data["positions"], [])

    def test_other_public_read_operations_reject_malformed_payloads(self):
        cases = [
            (
                {"operation": "get_market_state", "codes": ["HK.00700"]},
                [{"code": "HK.00700"}],
                lambda gateway: gateway.get_market_state(["HK.00700"]),
            ),
            (
                {"operation": "get_market_snapshot", "codes": ["HK.00700"]},
                [{"code": "HK.00700"}],
                lambda gateway: gateway.get_market_snapshot(["HK.00700"]),
            ),
            (
                {
                    "operation": "get_trading_days",
                    "market": "HK",
                    "start": "2026-08-12",
                    "end": "2026-08-13",
                },
                [{"date": "not-a-date"}],
                lambda gateway: gateway.get_trading_days(
                    "HK", "2026-08-12", "2026-08-13"
                ),
            ),
            (
                {"operation": "get_expiration_dates", "underlying": "HK.00700"},
                [{"expiry": "not-a-date"}],
                lambda gateway: gateway.get_expiration_dates("HK.00700"),
            ),
            (
                {
                    "operation": "resolve_option_code",
                    "underlying": "HK.00700",
                    "expiry": "2026-08-28",
                    "strike": 500.0,
                    "option_type": "CALL",
                },
                {"code": "HK.CALL", "expiry": "2026-08-28"},
                lambda gateway: gateway.resolve_option_code(
                    "HK.00700", "2026-08-28", 500.0, "CALL"
                ),
            ),
        ]

        for request, data, invoke in cases:
            with self.subTest(operation=request["operation"]):
                self.assert_schema_mismatch(self._replay(request, data, invoke))

    def test_partial_and_stale_data_payloads_are_also_validated(self):
        request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        for status in (EnvelopeStatus.PARTIAL, EnvelopeStatus.STALE):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as temp_dir:
                    Path(temp_dir, "fixture.jsonl").write_text(
                        _envelope(
                            request,
                            [{"code": "HK.00700"}],
                            status=status,
                        ).to_json_line()
                        + "\n",
                        encoding="utf-8",
                    )
                    result = ReplayGateway(temp_dir).get_market_snapshot(["HK.00700"])
                self.assert_schema_mismatch(result)


class ReplayCaptureCoherenceTests(unittest.TestCase):
    def _write(self, directory: str, *envelopes: DataEnvelope) -> None:
        Path(directory, "fixtures.jsonl").write_text(
            "".join(envelope.to_json_line() + "\n" for envelope in envelopes),
            encoding="utf-8",
        )

    def test_health_and_capabilities_do_not_change_explicit_as_of(self):
        market_request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        snapshot_request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                temp_dir,
                _envelope(
                    market_request,
                    [{"code": "HK.00700", "market_state": "MORNING"}],
                    captured_at="2026-08-12T02:00:00+00:00",
                ),
                _envelope(
                    snapshot_request,
                    [{"code": "HK.00700", "last_price": 500.0}],
                    captured_at="2026-08-12T02:05:00+00:00",
                ),
            )
            gateway = ReplayGateway(temp_dir, as_of_utc="2026-08-12T02:00:00+00:00")

            self.assertEqual(gateway.health().status, EnvelopeStatus.OK)
            self.assertEqual(gateway.capabilities().status, EnvelopeStatus.OK)
            first = gateway.get_market_state(["HK.00700"])
            incoherent = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.OK)
        self.assert_schema_mismatch(incoherent)

    def test_lookup_prefers_fixture_within_the_configured_capture_window(self):
        market_request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        snapshot_request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                temp_dir,
                _envelope(
                    market_request,
                    [{"code": "HK.00700", "market_state": "MORNING"}],
                    captured_at="2026-08-12T02:00:00+00:00",
                ),
                _envelope(
                    snapshot_request,
                    [{"code": "HK.00700", "last_price": 500.0}],
                    captured_at="2026-08-12T02:00:30+00:00",
                ),
                _envelope(
                    snapshot_request,
                    [{"code": "HK.00700", "last_price": 510.0}],
                    captured_at="2026-08-12T02:05:00+00:00",
                ),
            )
            gateway = ReplayGateway(temp_dir, as_of_utc="2026-08-12T02:00:30+00:00")

            first = gateway.get_market_state(["HK.00700"])
            coherent = gateway.get_market_snapshot(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.OK)
        self.assertEqual(coherent.status, EnvelopeStatus.OK)
        self.assertEqual(coherent.data[0]["last_price"], 500.0)
        self.assertEqual(coherent.captured_at_utc, "2026-08-12T02:00:30+00:00")

    def test_default_as_of_is_immutable_and_order_independent(self):
        market_request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        snapshot_request = {"operation": "get_market_snapshot", "codes": ["HK.00700"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(
                temp_dir,
                _envelope(
                    market_request,
                    [{"code": "HK.00700", "market_state": "MORNING"}],
                    captured_at="2026-08-12T02:00:00+00:00",
                ),
                _envelope(
                    snapshot_request,
                    [{"code": "HK.00700", "last_price": 500.0}],
                    captured_at="2026-08-12T02:05:00+00:00",
                ),
            )
            forward = ReplayGateway(temp_dir)
            reverse = ReplayGateway(temp_dir)

            forward_results = (
                forward.get_market_state(["HK.00700"]),
                forward.get_market_snapshot(["HK.00700"]),
            )
            reverse_results = (
                reverse.get_market_snapshot(["HK.00700"]),
                reverse.get_market_state(["HK.00700"]),
            )

        self.assert_schema_mismatch(forward_results[0])
        self.assertEqual(forward_results[1].status, EnvelopeStatus.OK)
        self.assertEqual(reverse_results[0].status, EnvelopeStatus.OK)
        self.assert_schema_mismatch(reverse_results[1])

    def assert_schema_mismatch(self, result: DataEnvelope) -> None:
        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        error = result.typed_error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, GatewayErrorCode.SCHEMA_MISMATCH)


class ReplayMalformedFixtureTests(unittest.TestCase):
    def test_deeply_nested_canonical_jsonl_is_a_typed_schema_error(self):
        request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        template = _envelope(request, "DEEP_PAYLOAD").to_json_line()
        nested = "[" * 2_000 + "0" + "]" * 2_000
        line = template.replace('"DEEP_PAYLOAD"', nested)

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "deep.jsonl").write_text(line + "\n", encoding="utf-8")
            result = ReplayGateway(temp_dir).health()

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertIsNotNone(result.typed_error)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_health_and_capabilities_semantically_validate_fixture_inventory(self):
        malformed = _envelope(
            {"operation": "get_market_state", "codes": ["HK.00700"]},
            [{"code": "HK.00700"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "malformed.jsonl").write_text(
                malformed.to_json_line() + "\n",
                encoding="utf-8",
            )
            gateway = ReplayGateway(temp_dir)
            health = gateway.health()
            capabilities = gateway.capabilities()

        self.assertEqual(health.status, EnvelopeStatus.ERROR)
        self.assertEqual(health.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)
        self.assertEqual(capabilities.status, EnvelopeStatus.ERROR)
        self.assertEqual(capabilities.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_invalid_legacy_log_timestamp_is_a_typed_schema_error(self):
        payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.CALL",
                    "option_type": "CALL",
                    "strike_time": "2026-08-28",
                    "strike_price": 500.0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "legacy.json").write_text(
                "2026-99-99 25:61:61,999 | connected\n"
                + json.dumps(payload),
                encoding="utf-8",
            )
            result = ReplayGateway(temp_dir, allow_legacy=True).health()

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_legacy_decode_attempts_are_bounded(self):
        payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.CALL",
                    "option_type": "CALL",
                    "strike_time": "2026-08-28",
                    "strike_price": 500.0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "legacy.json").write_text(
                ("{invalid\n" * 257) + json.dumps(payload),
                encoding="utf-8",
            )
            result = ReplayGateway(temp_dir, allow_legacy=True).health()

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)


class ReplayBoundaryLimitTests(unittest.TestCase):
    def test_resolver_rejects_nonfinite_strikes_and_normalizes_type_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway = ReplayGateway(temp_dir)
            invalid = [
                gateway.resolve_option_code("HK.00700", "2026-08-28", strike, "CALL")
                for strike in (float("nan"), float("inf"), 0.0, -1.0)
            ]
            alias = gateway.resolve_option_code(
                "HK.00700", "2026-08-28", 500.0, "认购"
            )

        self.assertTrue(
            all(item.typed_error.code is GatewayErrorCode.INVALID_REQUEST for item in invalid)
        )
        self.assertEqual(alias.request["option_type"], "CALL")
        self.assertEqual(alias.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING)

    def test_none_chain_request_and_oversized_inputs_are_typed_invalid_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway = ReplayGateway(temp_dir)

            chain = gateway.get_option_chain(None)  # type: ignore[arg-type]
            codes = gateway.get_market_state([f"HK.{index:05d}" for index in range(801)])
            legs = gateway.get_option_quotes(
                [OptionLeg(f"HK.OPT{index}", "BUY", 1) for index in range(9)]
            )

        for result in (chain, codes, legs):
            self.assertEqual(result.status, EnvelopeStatus.ERROR)
            self.assertEqual(result.typed_error.code, GatewayErrorCode.INVALID_REQUEST)

    def test_aggregate_record_and_byte_limits_fail_closed(self):
        request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        records = [
            _envelope(
                request,
                [{"code": "HK.00700", "market_state": "MORNING"}],
                captured_at=f"2026-08-12T02:00:0{index}+00:00",
            )
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "records.jsonl").write_text(
                "".join(item.to_json_line() + "\n" for item in records),
                encoding="utf-8",
            )
            with patch("src.replay_adapter._MAX_INVENTORY_RECORDS", 2):
                too_many = ReplayGateway(temp_dir).health()

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "large.jsonl").write_text(records[0].to_json_line(), encoding="utf-8")
            with patch("src.replay_adapter._MAX_TOTAL_FIXTURE_BYTES", 10):
                too_large = ReplayGateway(temp_dir).health()

        for result in (too_many, too_large):
            self.assertEqual(result.status, EnvelopeStatus.ERROR)
            self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_validated_inventory_is_cached_once_per_gateway_instance(self):
        request = {"operation": "get_market_state", "codes": ["HK.00700"]}
        envelope = _envelope(
            request,
            [{"code": "HK.00700", "market_state": "MORNING"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "fixture.jsonl")
            path.write_text(envelope.to_json_line() + "\n", encoding="utf-8")
            gateway = ReplayGateway(temp_dir)
            path.write_text("corrupt after index construction\n", encoding="utf-8")

            first = gateway.get_market_state(["HK.00700"])
            second = gateway.get_market_state(["HK.00700"])

        self.assertEqual(first.status, EnvelopeStatus.OK)
        self.assertEqual(second.status, EnvelopeStatus.OK)
        self.assertEqual(first.content_sha256, second.content_sha256)


if __name__ == "__main__":
    unittest.main()
