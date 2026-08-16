"""通用实时数据库测试：LiveQuoteCache TTL/stale 回退、LiveDataService 快照组装。"""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.decision_pipeline import DEFAULT_INPUT, load_frozen_snapshot, run_pipeline
from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    OptionLeg,
)
from src.live_data import LiveDataError, LiveDataService, LiveQuoteCache


class FakeGateway:
    mode = DataMode.LIVE

    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.option_calls = 0
        self.state_calls = 0
        self.fail_snapshot = False

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        self.snapshot_calls += 1
        if self.fail_snapshot:
            raise ConnectionError("Connection refused")
        rows: list[dict[str, Any]] = []
        for code in codes:
            upper = code.upper()
            if "TCH" in upper:
                rows.append(
                    {
                        "code": code,
                        "last_price": 12.0,
                        "bid": 11.5,
                        "ask": 12.5,
                        "open_interest": 999,
                        "volume": 333,
                        "turnover": 5000.0,
                    }
                )
            else:
                rows.append(
                    {
                        "code": code,
                        "last_price": 500.0,
                        "previous_close": 497.0,
                        "bid": 499.8,
                        "ask": 500.2,
                        "open_interest": 0,
                        "volume": 1000,
                    }
                )
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "get_market_snapshot", "codes": list(codes)},
            status=EnvelopeStatus.OK,
            data=rows,
            entitlements={},
            warnings=[],
            typed_error=None,
        )

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        self.state_calls += 1
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "get_market_state", "codes": list(codes)},
            status=EnvelopeStatus.OK,
            data=[{"code": code, "market_state": "MORNING"} for code in codes],
            entitlements={},
            warnings=[],
            typed_error=None,
        )

    def get_option_quotes(self, legs: list[OptionLeg]) -> DataEnvelope:
        self.option_calls += 1
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "get_option_quotes", "legs": [leg.to_dict() for leg in legs]},
            status=EnvelopeStatus.OK,
            data=[
                {
                    "code": leg.code,
                    "price": 12.0,
                    "mid_price": 12.0,
                    "implied_volatility": 38.0,
                }
                for leg in legs
            ],
            entitlements={},
            warnings=[],
            typed_error=None,
        )


class LiveQuoteCacheTests(unittest.TestCase):
    def test_fresh_entry_reuses_loader_value(self) -> None:
        calls: list[int] = []

        def loader() -> int:
            calls.append(1)
            return 7

        cache = LiveQuoteCache(ttl=10.0)
        self.assertEqual(cache.get("HK.00700", loader), 7)
        self.assertEqual(cache.get("HK.00700", loader), 7)
        self.assertEqual(len(calls), 1)

    def test_failed_loader_returns_stale_entry_without_losing_value(self) -> None:
        cache = LiveQuoteCache(ttl=0.05)
        self.assertEqual(cache.get("HK.00700", lambda: {"last_price": 500.0})["last_price"], 500.0)
        time.sleep(0.06)

        entry = cache.get_entry(
            "HK.00700",
            lambda: (_ for _ in ()).throw(TimeoutError("down")),
        )

        self.assertTrue(entry.stale)
        self.assertEqual(entry.value["last_price"], 500.0)
        self.assertIsInstance(entry.error, TimeoutError)

    def test_expired_entry_reloads_after_ttl(self) -> None:
        cache = LiveQuoteCache(ttl=0.05)
        calls: list[int] = []

        def loader() -> int:
            calls.append(1)
            return len(calls)

        self.assertEqual(cache.get("HK.00700", loader), 1)
        time.sleep(0.06)
        self.assertEqual(cache.get("HK.00700", loader), 2)
        self.assertEqual(len(calls), 2)

    def test_single_flight_under_concurrency(self) -> None:
        cache = LiveQuoteCache(ttl=10.0)
        calls: list[int] = []
        lock = threading.Lock()

        def loader() -> dict[str, int]:
            with lock:
                calls.append(1)
                time.sleep(0.05)
                return {"value": len(calls)}

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: cache.get("HK.00700", loader), range(8)))

        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result == {"value": 1} for result in results))



class LiveDataServiceTests(unittest.TestCase):
    def test_build_live_snapshot_has_frozen_schema_and_live_fields(self) -> None:
        gateway = FakeGateway()
        service = LiveDataService(gateway)

        snapshot = service.build_live_snapshot()

        frozen = load_frozen_snapshot(DEFAULT_INPUT)
        self.assertTrue(set(frozen.keys()).issubset(set(snapshot.keys())))
        self.assertEqual(set(snapshot.keys()), set(frozen.keys()) | {"live_spot", "warnings"})
        self.assertEqual(snapshot["mode"], "LIVE")
        self.assertEqual(snapshot["freshness"], "FRESH")
        # The fake gateway returns spot=500 while call mid=12 with strike=480 is
        # below intrinsic (20.0), so the engine must fall back to the template
        # spot while the live quote stays visible in live_spot / payload.spot.
        self.assertEqual(snapshot["spot"], 478.8)
        self.assertEqual(snapshot["live_spot"], 500.0)
        self.assertEqual(snapshot["payload"]["spot"], 500.0)
        self.assertEqual(snapshot["payload"]["prev_close"], 497.0)
        self.assertEqual(snapshot["payload"]["market_state"], "MORNING")
        self.assertEqual(snapshot["payload"]["legs"][0]["call"]["last"], 12.0)
        self.assertEqual(snapshot["payload"]["legs"][0]["call"]["mid"], 12.0)
        self.assertEqual(snapshot["payload"]["legs"][0]["call"]["api_iv_pct"], 38.0)
        self.assertEqual(snapshot["payload"]["legs"][0]["call"]["open_interest"], 999)
        self.assertEqual(snapshot["underlying"], "HK.00700")
        self.assertEqual(len(snapshot["warnings"]), 1)
        self.assertIn("template spot", snapshot["warnings"][0])

    def test_inconsistent_live_spot_falls_back_and_pipeline_succeeds(self) -> None:
        """FakeGateway spot=500 with call mid=12 at strike=480 must not crash
        run_pipeline / compose_state with a negative IV."""
        service = LiveDataService(FakeGateway())

        snapshot = service.build_live_snapshot()
        card = run_pipeline(
            DEFAULT_INPUT,
            audit_enabled=False,
            write_card=False,
            snapshot_data=snapshot,
        )

        self.assertIsInstance(card, dict)
        self.assertEqual(card.get("underlying"), "HK.00700")

        from src.ui_server import compose_state

        state = compose_state(card, input_path=DEFAULT_INPUT, snapshot_data=snapshot)
        self.assertEqual(state["meta"]["mode"], "LIVE")
        self.assertEqual(state["underlying"]["spot"], 500.0)
        self.assertEqual(state["meta"]["warnings"], snapshot["warnings"])

    def test_live_snapshot_error_is_typed(self) -> None:
        gateway = FakeGateway()
        gateway.fail_snapshot = True
        service = LiveDataService(gateway)

        with self.assertRaises(Exception) as ctx:
            service.build_live_snapshot()

        error = ctx.exception
        self.assertEqual(error.code.value, "OPEND_UNAVAILABLE")
        self.assertTrue(error.retryable)

    def test_swr_returns_stale_envelope_with_last_good_quote(self) -> None:
        gateway = FakeGateway()
        service = LiveDataService(gateway, quote_ttl=0.05)
        codes = ["HK.00700"]

        first = service.get_market_snapshot(codes)
        self.assertEqual(first.status, EnvelopeStatus.OK)

        gateway.fail_snapshot = True
        time.sleep(0.06)
        second = service.get_market_snapshot(codes)

        self.assertEqual(second.status, EnvelopeStatus.STALE)
        self.assertEqual(second.freshness_status, FreshnessStatus.STALE)
        self.assertEqual(second.data, [first.data[0]])
        self.assertIsNotNone(second.typed_error)
        self.assertEqual(second.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)

    def test_refresh_returns_data_envelope_evidence(self) -> None:
        service = LiveDataService(FakeGateway())

        envelope = service.refresh()

        self.assertIsInstance(envelope, DataEnvelope)
        self.assertEqual(envelope.status, EnvelopeStatus.OK)
        self.assertEqual(envelope.mode, DataMode.LIVE)
        self.assertIsInstance(envelope.data, dict)
        self.assertEqual(envelope.data.get("mode"), DataMode.LIVE.value)
        self.assertEqual(envelope.entitlements, {"live_snapshot": True})

    def test_refresh_returns_error_envelope_when_live_is_unavailable(self) -> None:
        gateway = FakeGateway()
        gateway.fail_snapshot = True
        service = LiveDataService(gateway)

        envelope = service.refresh()

        self.assertIsInstance(envelope, DataEnvelope)
        self.assertEqual(envelope.status, EnvelopeStatus.ERROR)
        self.assertIsNotNone(envelope.typed_error)
        self.assertEqual(envelope.typed_error.code, GatewayErrorCode.OPEND_UNAVAILABLE)

    def test_build_live_snapshot_maps_all_quote_fields(self) -> None:
        service = LiveDataService(FakeGateway())

        snapshot = service.build_live_snapshot()

        call = snapshot["payload"]["legs"][0]["call"]
        self.assertEqual(call["last"], 12.0)
        self.assertEqual(call["bid"], 11.5)
        self.assertEqual(call["ask"], 12.5)
        self.assertEqual(call["mid"], 12.0)
        self.assertEqual(call["api_iv_pct"], 38.0)
        self.assertEqual(call["open_interest"], 999)
        self.assertEqual(call["volume"], 333)

        put = snapshot["payload"]["legs"][0]["put"]
        self.assertEqual(put["last"], 12.0)
        self.assertEqual(put["bid"], 11.5)
        self.assertEqual(put["ask"], 12.5)
        self.assertEqual(put["mid"], 12.0)
        self.assertEqual(put["api_iv_pct"], 38.0)
        self.assertEqual(put["open_interest"], 999)
        self.assertEqual(put["volume"], 333)



    def test_build_live_snapshot_normalises_gateway_code_case(self) -> None:
        """gateway 返回小写 code 时也必须按大写匹配，不能 NOT_FOUND。"""

        class LowerCodeGateway(FakeGateway):
            def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
                env = super().get_market_snapshot(codes)
                lowered = [
                    {**row, "code": str(row["code"]).lower()}
                    for row in env.data
                ]
                return DataEnvelope.now(
                    mode=DataMode.LIVE,
                    origin_source="FUTU",
                    freshness_status=FreshnessStatus.FRESH,
                    request={"operation": "get_market_snapshot", "codes": list(codes)},
                    status=EnvelopeStatus.OK,
                    data=lowered,
                    entitlements={},
                    warnings=[],
                    typed_error=None,
                )

        service = LiveDataService(LowerCodeGateway())
        snapshot = service.build_live_snapshot()

        self.assertEqual(snapshot["mode"], "LIVE")
        self.assertEqual(snapshot["underlying"], "HK.00700")
        self.assertEqual(snapshot["payload"]["spot"], 500.0)
        self.assertEqual(snapshot["payload"]["prev_close"], 497.0)
        self.assertEqual(snapshot["payload"]["legs"][0]["call"]["last"], 12.0)


if __name__ == "__main__":
    unittest.main()
