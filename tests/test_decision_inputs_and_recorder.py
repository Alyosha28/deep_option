from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.decision_inputs import DecisionInputService
from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
)
from src.snapshot_recorder import SnapshotRecorder, iter_envelopes


def envelope(operation: str, data, request=None):
    return DataEnvelope(
        mode=DataMode.REPLAY,
        origin_source="FUTU",
        captured_at_utc="2026-08-08T03:54:35+00:00",
        source_time_utc="2026-08-08T03:54:35+00:00",
        freshness_status=FreshnessStatus.FROZEN,
        request=request or {"operation": operation},
        status=EnvelopeStatus.OK,
        data=data,
        entitlements={"recorded": True},
        warnings=[],
        typed_error=None,
    )


class StubGateway:
    mode = DataMode.REPLAY

    def health(self):
        return envelope("health", {"ready": True, "server_version": None})

    def capabilities(self):
        return envelope(
            "capabilities",
            {
                "market_data": True,
                "account_read": False,
                "strategy_combination_quote": True,
                "execution": False,
                "real_trading": False,
            },
        )

    def get_market_state(self, codes):
        return envelope(
            "get_market_state",
            [{"code": code, "market_state": "MORNING"} for code in codes],
            {"operation": "get_market_state", "codes": codes},
        )

    def get_market_snapshot(self, codes):
        return envelope(
            "get_market_snapshot",
            [{"code": code, "last_price": 500.0} for code in codes],
            {"operation": "get_market_snapshot", "codes": codes},
        )

    def get_expiration_dates(self, underlying):
        return envelope(
            "get_expiration_dates",
            [{"expiry": "2026-08-28"}],
            {"operation": "get_expiration_dates", "underlying": underlying},
        )

    def get_option_chain(self, request):
        return envelope(
            "get_option_chain",
            [
                {
                    "code": "HK.CALL",
                    "underlying": request.underlying,
                    "expiry": request.start,
                    "option_type": "CALL",
                    "strike": 500.0,
                }
            ],
            {"operation": "get_option_chain", **request.to_dict()},
        )


class DecisionInputServiceTests(unittest.TestCase):
    def test_agent_registry_is_coarse_and_read_only(self):
        service = DecisionInputService(StubGateway())

        tools = service.registered_tools()

        self.assertEqual(set(tools), {"refresh_decision_inputs"})
        self.assertIs(tools["refresh_decision_inputs"], service.refresh_decision_inputs)
        forbidden = ("place", "modify", "cancel", "unlock", "submit", "invoke", "bash")
        self.assertFalse(any(word in name.lower() for name in tools for word in forbidden))

    def test_refresh_returns_only_tool_outputs(self):
        service = DecisionInputService(StubGateway())

        result = service.refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.status, EnvelopeStatus.OK)
        self.assertEqual(result.mode, DataMode.REPLAY)
        self.assertIn("option_chain", result.data)
        self.assertEqual(result.data["underlying_quote"][0]["last_price"], 500.0)

    def test_aggregate_orders_equivalent_utc_encodings_chronologically(self):
        def retime(item, captured_at, source_time):
            return DataEnvelope(
                mode=item.mode,
                origin_source=item.origin_source,
                captured_at_utc=captured_at,
                source_time_utc=source_time,
                freshness_status=item.freshness_status,
                request=item.request,
                status=item.status,
                data=item.data,
                entitlements=item.entitlements,
                warnings=item.warnings,
                typed_error=item.typed_error,
            )

        class TimestampGateway(StubGateway):
            def health(self):
                return retime(
                    super().health(),
                    "2026-08-12T02:00:00Z",
                    "2026-08-12T02:00:00Z",
                )

            def capabilities(self):
                return retime(
                    super().capabilities(),
                    "2026-08-12T02:00:00.100000+00:00",
                    "2026-08-12T02:00:00.100000+00:00",
                )

            def get_market_state(self, codes):
                return retime(
                    super().get_market_state(codes),
                    "2026-08-12T02:00:00.100000+00:00",
                    "2026-08-12T02:00:00.100000+00:00",
                )

            def get_market_snapshot(self, codes):
                return retime(
                    super().get_market_snapshot(codes),
                    "2026-08-12T02:00:00.100000+00:00",
                    "2026-08-12T02:00:00.100000+00:00",
                )

            def get_expiration_dates(self, underlying):
                return retime(
                    super().get_expiration_dates(underlying),
                    "2026-08-12T02:00:00.100000+00:00",
                    "2026-08-12T02:00:00.100000+00:00",
                )

            def get_option_chain(self, request):
                return retime(
                    super().get_option_chain(request),
                    "2026-08-12T02:00:00.100000+00:00",
                    "2026-08-12T02:00:00.100000+00:00",
                )

        result = DecisionInputService(TimestampGateway()).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.captured_at_utc, "2026-08-12T02:00:00.100000+00:00")
        self.assertEqual(result.source_time_utc, "2026-08-12T02:00:00Z")

    def test_conflicting_expiration_and_chain_evidence_is_rejected(self):
        class ConflictingGateway(StubGateway):
            def get_expiration_dates(self, underlying):
                return envelope(
                    "get_expiration_dates",
                    [{"expiry": "2026-09-25"}],
                    {"operation": "get_expiration_dates", "underlying": underlying},
                )

        result = DecisionInputService(ConflictingGateway()).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.SCHEMA_MISMATCH)

    def test_unhealthy_gateway_fails_fast_without_more_live_calls(self):
        class UnhealthyGateway(StubGateway):
            mode = DataMode.LIVE

            def __init__(self):
                self.calls = []

            def health(self):
                self.calls.append("health")
                return DataEnvelope(
                    mode=DataMode.LIVE,
                    origin_source="FUTU",
                    captured_at_utc="2026-08-12T02:00:00+00:00",
                    source_time_utc=None,
                    freshness_status=FreshnessStatus.UNKNOWN,
                    request={"operation": "health"},
                    status=EnvelopeStatus.ERROR,
                    data=None,
                    entitlements={},
                    warnings=[],
                    typed_error=GatewayError(
                        code=GatewayErrorCode.OPEND_UNAVAILABLE,
                        message="OpenD is unavailable",
                        retryable=True,
                    ),
                )

            def capabilities(self):
                self.calls.append("capabilities")
                return super().capabilities()

            def get_market_state(self, codes):
                self.calls.append("get_market_state")
                return super().get_market_state(codes)

            def get_market_snapshot(self, codes):
                self.calls.append("get_market_snapshot")
                return super().get_market_snapshot(codes)

            def get_expiration_dates(self, underlying):
                self.calls.append("get_expiration_dates")
                return super().get_expiration_dates(underlying)

            def get_option_chain(self, request):
                self.calls.append("get_option_chain")
                return super().get_option_chain(request)

        gateway = UnhealthyGateway()
        result = DecisionInputService(gateway).refresh_decision_inputs(
            {"underlying": "HK.00700", "expiry": "2026-08-28"}
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)
        self.assertEqual(gateway.calls, ["health"])


class SnapshotRecorderTests(unittest.TestCase):
    def test_records_envelopes_as_valid_jsonl_under_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SnapshotRecorder(temp_dir)
            item = envelope("get_market_snapshot", [{"code": "HK.00700"}])
            threads = [threading.Thread(target=recorder.record, args=(item, "contract")) for _ in range(8)]

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            files = list(Path(temp_dir).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 8)
            self.assertTrue(all(json.loads(line)["schema_version"] for line in lines))
            restored = list(iter_envelopes(files[0]))
            self.assertEqual(len(restored), 8)
            self.assertTrue(all(record.verify_integrity() for record in restored))

    def test_deep_json_reader_reports_a_line_qualified_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "deep.jsonl")
            path.write_text("[" * 2_000 + "0" + "]" * 2_000 + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"deep\.jsonl:1"):
                list(iter_envelopes(path))
