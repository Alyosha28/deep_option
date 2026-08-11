from __future__ import annotations

import copy
import unittest

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


class DataEnvelopeTests(unittest.TestCase):
    def _envelope(self) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            captured_at_utc="2026-08-12T02:00:01+00:00",
            source_time_utc="2026-08-12T02:00:00+00:00",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "get_market_snapshot", "codes": ["HK.00700"]},
            status=EnvelopeStatus.OK,
            data=[{"code": "HK.00700", "last_price": 500.0}],
            entitlements={"market_data": "available"},
            warnings=[],
            typed_error=None,
        )

    def test_hash_and_snapshot_id_are_deterministic(self):
        first = self._envelope()
        second = self._envelope()

        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(len(first.content_sha256), 64)
        self.assertTrue(first.snapshot_id.startswith("snap_"))
        self.assertTrue(first.verify_integrity())

    def test_round_trip_is_strict_and_json_safe(self):
        original = self._envelope()
        payload = original.to_dict()

        self.assertEqual(payload["mode"], "LIVE")
        self.assertEqual(payload["status"], "OK")
        restored = DataEnvelope.from_dict(payload)
        self.assertEqual(restored, original)

        tampered = copy.deepcopy(payload)
        tampered["data"][0]["last_price"] = 1.0
        with self.assertRaises(ValueError):
            DataEnvelope.from_dict(tampered)

    def test_integrity_check_fails_closed_on_nested_cycles(self):
        envelope = self._envelope()
        envelope.data[0]["cycle"] = envelope.data

        self.assertFalse(envelope.verify_integrity())

    def test_error_envelope_carries_typed_error(self):
        error = GatewayError(
            code=GatewayErrorCode.OPEND_UNAVAILABLE,
            message="OpenD is unavailable",
            retryable=True,
        )
        envelope = DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            captured_at_utc="2026-08-12T02:00:01+00:00",
            source_time_utc=None,
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "health"},
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=error,
        )

        self.assertEqual(envelope.to_dict()["typed_error"]["code"], "OPEND_UNAVAILABLE")

    def test_sensitive_identifier_keys_are_rejected_from_all_envelope_channels(self):
        with self.assertRaises(ValueError):
            GatewayError(
                code=GatewayErrorCode.UPSTREAM_ERROR,
                message="failed",
                retryable=False,
                details={"acc_id": 123456},
            )

        with self.assertRaises(ValueError):
            DataEnvelope(
                mode=DataMode.LIVE,
                origin_source="FUTU",
                captured_at_utc="2026-08-12T02:00:01+00:00",
                source_time_utc=None,
                freshness_status=FreshnessStatus.UNKNOWN,
                request={"operation": "health"},
                status=EnvelopeStatus.ERROR,
                data=None,
                entitlements={"access_token": "secret"},
                warnings=[],
                typed_error=GatewayError(
                    code=GatewayErrorCode.UPSTREAM_ERROR,
                    message="failed",
                    retryable=False,
                ),
            )


class RequestTypeTests(unittest.TestCase):
    def test_option_chain_request_normalizes_and_limits_window(self):
        request = OptionChainRequest(
            underlying="hk.00700",
            start="2026-08-01",
            end="2026-08-30",
            option_type="call",
        )
        self.assertEqual(request.underlying, "HK.00700")
        self.assertEqual(request.option_type, "CALL")

        with self.assertRaises(ValueError):
            OptionChainRequest("HK.00700", "2026-08-01", "2026-08-31")
        with self.assertRaises(ValueError):
            OptionChainRequest("00700", "2026-08-01", "2026-08-02")

    def test_option_leg_and_account_binding_fail_closed(self):
        leg = OptionLeg("hk.tch260828c500000", "buy", 1)
        self.assertEqual(leg.code, "HK.TCH260828C500000")
        self.assertEqual(leg.action, "BUY")

        with self.assertRaises(ValueError):
            OptionLeg("HK.TCH260828C500000", "BUY", 0)
        with self.assertRaises(ValueError):
            AccountBinding("demo", 123, trd_env="REAL")
