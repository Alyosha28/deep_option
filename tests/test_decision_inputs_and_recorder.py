from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.decision_inputs import DecisionInputService
from src.gateway import DataEnvelope, DataMode, EnvelopeStatus, FreshnessStatus
from src.snapshot_recorder import SnapshotRecorder, iter_envelopes


def envelope(operation: str, data):
    return DataEnvelope(
        mode=DataMode.REPLAY,
        origin_source="FUTU",
        captured_at_utc="2026-08-08T03:54:35+00:00",
        source_time_utc="2026-08-08T03:54:35+00:00",
        freshness_status=FreshnessStatus.FROZEN,
        request={"operation": operation},
        status=EnvelopeStatus.OK,
        data=data,
        entitlements={"recorded": True},
        warnings=[],
        typed_error=None,
    )


class StubGateway:
    def health(self):
        return envelope("health", {"ready": True})

    def capabilities(self):
        return envelope("capabilities", {"market_data": True})

    def get_market_state(self, codes):
        return envelope("get_market_state", [{"code": code, "market_state": "MORNING"} for code in codes])

    def get_market_snapshot(self, codes):
        return envelope("get_market_snapshot", [{"code": code, "last_price": 500.0} for code in codes])

    def get_expiration_dates(self, underlying):
        return envelope("get_expiration_dates", [{"expiry": "2026-08-28"}])

    def get_option_chain(self, request):
        return envelope("get_option_chain", [{"code": "HK.CALL", "expiry": request.start}])


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
