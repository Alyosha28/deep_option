"""LIVE 模式 UI 服务集成测试：fake gateway 下 /api/state 与 /api/live-quote。"""

from __future__ import annotations

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from typing import Any, ClassVar
from unittest.mock import patch

from src import ui_server as mod
from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
)


class _ReadyGateway:
    mode = DataMode.LIVE

    def health(self) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "health"},
            status=EnvelopeStatus.OK,
            data={"ready": True},
            entitlements={},
            warnings=[],
            typed_error=None,
        )

    def capabilities(self) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.FRESH,
            request={"operation": "capabilities"},
            status=EnvelopeStatus.OK,
            data={"market_data": True},
            entitlements={},
            warnings=[],
            typed_error=None,
        )

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        rows = [
            {
                "code": code,
                "last_price": 501.0,
                "previous_close": 497.0,
                "bid": 500.0,
                "ask": 502.0,
                "volume": 1000,
                "turnover": 500000.0,
            }
            for code in codes
        ]
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


class _DownGateway:
    mode = DataMode.LIVE

    def health(self) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "health"},
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=GatewayError(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            ),
        )

    def capabilities(self) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "capabilities"},
            status=EnvelopeStatus.OK,
            data={"market_data": True},
            entitlements={},
            warnings=[],
            typed_error=None,
        )

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "get_market_snapshot", "codes": list(codes)},
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=GatewayError(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            ),
        )

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        return DataEnvelope.now(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": "get_market_state", "codes": list(codes)},
            status=EnvelopeStatus.OK,
            data=[{"code": code, "market_state": "UNKNOWN"} for code in codes],
            entitlements={},
            warnings=[],
            typed_error=None,
        )


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
) -> tuple[int, Any]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=120
    )
    connection.request(method, path)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    connection.close()
    return response.status, json.loads(raw)


class LiveUiTests(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self) -> None:
        mod._reset_live_data_service()
        mod.invalidate_state_cache()
        mod._LIVE_GATEWAY_FACTORY = mod._create_live_gateway

    def test_live_state_and_quote_use_live_mode(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: _ReadyGateway()
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            status, state = _request(self.server, "GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["meta"]["mode"], "LIVE")
            self.assertEqual(state["underlying"]["spot"], 501.0)
            self.assertEqual(state["meta"]["freshness"], "FRESH")

            status, quote = _request(
                self.server, "GET", "/api/live-quote?codes=HK.00700"
            )
            self.assertEqual(status, 200)
            self.assertEqual(quote["mode"], "LIVE")
            self.assertEqual(quote["quotes"][0]["code"], "HK.00700")
            self.assertEqual(quote["quotes"][0]["last"], 501.0)

    def test_live_quote_opend_down_returns_typed_error(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: _DownGateway()
            mod._reset_live_data_service()

            status, payload = _request(
                self.server, "GET", "/api/live-quote?codes=HK.00700"
            )

            self.assertEqual(status, 503)
            self.assertEqual(payload["typedError"]["code"], "OPEND_UNAVAILABLE")
            self.assertTrue(payload["typedError"]["retryable"])


if __name__ == "__main__":
    unittest.main()
