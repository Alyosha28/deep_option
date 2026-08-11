from __future__ import annotations

import builtins
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayErrorCode,
    OptionChainRequest,
)
from src.replay_adapter import ReplayGateway


class ReplayGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.snapshot_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_envelope(self, envelope: DataEnvelope, filename: str = "fixture.jsonl"):
        (self.snapshot_dir / filename).write_text(envelope.to_json_line() + "\n", encoding="utf-8")

    def _chain_envelope(self) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            captured_at_utc="2026-08-08T03:54:35+00:00",
            source_time_utc="2026-08-08T03:54:35+00:00",
            freshness_status=FreshnessStatus.FRESH,
            request={
                "operation": "get_option_chain",
                "underlying": "HK.00700",
                "start": "2026-08-28",
                "end": "2026-08-28",
                "option_type": "ALL",
                "option_cond_type": "ALL",
            },
            status=EnvelopeStatus.OK,
            data=[
                {
                    "code": "HK.TCH260828C500000",
                    "underlying": "HK.00700",
                    "option_type": "CALL",
                    "strike": 500.0,
                    "expiry": "2026-08-28",
                    "lot_size": 100,
                    "standard_type": "STANDARD",
                }
            ],
            entitlements={"recorded": True},
            warnings=[],
            typed_error=None,
        )

    def test_replay_is_stable_and_never_imports_futu(self):
        self._write_envelope(self._chain_envelope())
        gateway = ReplayGateway(self.snapshot_dir)
        request = OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28")
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "futu" or name.startswith("futu."):
                raise AssertionError("Replay must not import futu")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            first = gateway.get_option_chain(request)
            second = gateway.get_option_chain(request)

        self.assertEqual(first.mode, DataMode.REPLAY)
        self.assertEqual(first.freshness_status, FreshnessStatus.FROZEN)
        self.assertEqual(first.captured_at_utc, "2026-08-08T03:54:35+00:00")
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_request_mismatch_returns_typed_missing_fixture(self):
        self._write_envelope(self._chain_envelope())
        gateway = ReplayGateway(self.snapshot_dir)

        result = gateway.get_option_chain(
            OptionChainRequest("HK.00700", "2026-09-25", "2026-09-25")
        )

        self.assertEqual(result.status, EnvelopeStatus.ERROR)
        self.assertEqual(result.typed_error.code, GatewayErrorCode.REPLAY_FIXTURE_MISSING)

    def test_legacy_json_with_log_lines_is_filtered_by_expiry_and_type(self):
        payload = {
            "code": "HK.00700",
            "data": [
                {
                    "code": "HK.CALL",
                    "option_type": "CALL",
                    "stock_owner": "HK.00700",
                    "strike_time": "2026-08-28",
                    "strike_price": 500,
                    "lot_size": 100,
                },
                {
                    "code": "HK.PUT",
                    "option_type": "PUT",
                    "stock_owner": "HK.00700",
                    "strike_time": "2026-08-28",
                    "strike_price": 500,
                    "lot_size": 100,
                },
                {
                    "code": "HK.OLD",
                    "option_type": "CALL",
                    "stock_owner": "HK.00700",
                    "strike_time": "2026-08-14",
                    "strike_price": 500,
                    "lot_size": 100,
                },
            ],
        }
        legacy = (
            "2026-08-08 11:54:35,202 | INFO | connected\n"
            + json.dumps(payload)
            + "\n2026-08-08 11:54:36,100 | INFO | disconnected\n"
        )
        (self.snapshot_dir / "2026-08-08_hero_chain.json").write_text(legacy, encoding="utf-8")
        gateway = ReplayGateway(self.snapshot_dir, allow_legacy=True)

        result = gateway.get_option_chain(
            OptionChainRequest("HK.00700", "2026-08-28", "2026-08-28", option_type="CALL")
        )
        resolved = gateway.resolve_option_code(
            "HK.00700", "2026-08-28", 500.0, "CALL"
        )

        self.assertEqual(result.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(result.mode, DataMode.REPLAY)
        self.assertEqual(result.origin_source, "FUTU")
        self.assertEqual(result.freshness_status, FreshnessStatus.FROZEN)
        self.assertEqual([row["code"] for row in result.data], ["HK.CALL"])
        self.assertEqual(result.captured_at_utc, "2026-08-08T03:54:35.202000+00:00")
        self.assertEqual(resolved.status, EnvelopeStatus.PARTIAL)
        self.assertEqual(resolved.data["code"], "HK.CALL")
        self.assertTrue(any("legacy" in warning.lower() for warning in resolved.warnings))
