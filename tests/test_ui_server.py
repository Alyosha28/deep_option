"""UI 本地服务测试：离线、只读、临时端口，不写审计与决策卡文件。"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from typing import ClassVar

from src.decision_pipeline import DEFAULT_INPUT, compute_engine, load_frozen_snapshot
from src.ui_server import Handler, _expiries_ui, build_state


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    body: str | None = None,
) -> tuple[int, str]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=120
    )
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(
        method,
        path,
        body=body.encode("utf-8") if body is not None else None,
        headers=headers,
    )
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    return response.status, body


class BuildStateTests(unittest.TestCase):
    def test_state_shape_is_complete(self):
        state = build_state()

        for key in (
            "meta",
            "underlying",
            "earnings",
            "account",
            "scenario",
            "expiries",
            "decisionCard",
            "macro",
            "policyLibrary",
            "research",
        ):
            self.assertIn(key, state)
        self.assertEqual(state["decisionCard"]["verdict"], "NO_TRADE")
        self.assertEqual(len(state["expiries"]), 2)
        primary = state["expiries"][0]
        self.assertTrue(primary["primary"])
        self.assertIn("delta", primary["strategy"]["greeks"])
        self.assertTrue(state["macro"]["available"])
        self.assertGreaterEqual(state["policyLibrary"]["eventCount"], 4)
        self.assertGreaterEqual(
            state["policyLibrary"]["health"]["event_status"].get("ACTIVE", 0),
            4,
        )
        self.assertIn("recently_promoted", state["policyLibrary"]["health"])

    def test_state_is_json_safe(self):
        json.dumps(build_state(), ensure_ascii=False)

    def test_extra_legs_do_not_reuse_secondary_strategy(self):
        data = load_frozen_snapshot(DEFAULT_INPUT)
        payload = dict(data["payload"])
        extra = json.loads(json.dumps(payload["legs"][1]))
        extra["expiry"] = "2026-09-11"
        extra["dte"] = extra["dte"] + 10
        extra["call"]["strike"] = 999.0
        extra["put"]["strike"] = 999.0
        payload["legs"] = list(payload["legs"]) + [extra]
        engine = compute_engine(data)

        expiries = _expiries_ui(payload, engine)

        self.assertEqual(len(expiries), 3)
        self.assertIsNotNone(expiries[0]["strategy"])
        self.assertIsNotNone(expiries[1]["strategy"])
        self.assertIsNone(expiries[2]["strategy"])
        self.assertEqual(expiries[2]["strike"], 999.0)
        self.assertNotEqual(expiries[2]["strike"], expiries[1]["strike"])

    def test_state_cache_serves_ttl_and_invalidates(self):
        from src import ui_server as mod

        calls = []
        original = mod.build_state

        def fake_build():
            calls.append(1)
            return {"cached": True}

        mod.build_state = fake_build
        try:
            first = mod.build_state_cached()
            second = mod.build_state_cached()
            self.assertIs(first, second)
            self.assertEqual(len(calls), 1)
            mod.invalidate_state_cache()
            third = mod.build_state_cached()
            self.assertIsNot(third, first)
            self.assertEqual(len(calls), 2)
        finally:
            mod.build_state = original
            mod.invalidate_state_cache()


class HttpEndpointTests(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_index_served(self):
        status, body = _request(self.server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("GOAI", body)

    def test_api_state(self):
        status, body = _request(self.server, "GET", "/api/state")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["decisionCard"]["verdict"], "NO_TRADE")
        self.assertIn("verification", payload["policyLibrary"]["health"])

    def test_api_policy_library(self):
        status, body = _request(self.server, "GET", "/api/policy-library")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["eventCount"], 4)
        self.assertGreaterEqual(payload["health"]["verification"]["VERIFIED"], 1)

    def test_path_traversal_blocked(self):
        status, body = _request(self.server, "GET", "/../hero_inputs.json")

        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_foreign_host_header_is_rejected(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=120
        )
        connection.request(
            "GET", "/api/state", headers={"Host": "evil.example.com"}
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()

        self.assertEqual(response.status, 403)
        self.assertIn("host", json.loads(raw)["error"].lower())

    def test_run_endpoint_offline(self):
        status, body = _request(self.server, "POST", "/api/run?no_audit=1")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["decisionCard"]["verdict"], "NO_TRADE")
        self.assertTrue(payload["macro"]["available"])

    def test_chat_endpoint_parses_and_reruns(self):
        body = json.dumps(
            {
                "message": (
                    "腾讯业绩快到了，方向不确定。账户 10 万港币，单笔最多亏 5%，"
                    "帮我看看跨式。"
                )
            },
            ensure_ascii=False,
        )
        status, raw = _request(self.server, "POST", "/api/chat?no_audit=1", body)
        payload = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(payload["chat"]["scenario"]["underlying"], "HK.00700")
        self.assertEqual(payload["chat"]["scenario"]["view"], "uncertain")
        self.assertEqual(payload["chat"]["scenario"]["account_cash_hkd"], 100000.0)
        self.assertEqual(payload["decisionCard"]["verdict"], "NO_TRADE")

    def test_chat_missing_message_is_400(self):
        status, raw = _request(
            self.server, "POST", "/api/chat?no_audit=1", json.dumps({})
        )
        self.assertEqual(status, 400)
        self.assertIn("message", json.loads(raw)["error"])

    def test_chat_unparseable_message_is_422(self):
        status, raw = _request(
            self.server,
            "POST",
            "/api/chat?no_audit=1",
            json.dumps({"message": "帮我看看期权"}, ensure_ascii=False),
        )
        self.assertEqual(status, 422)
        self.assertIn("无法识别标的", json.loads(raw)["error"])

    def test_chat_unsupported_symbol_is_422(self):
        status, raw = _request(
            self.server,
            "POST",
            "/api/chat?no_audit=1",
            json.dumps({"message": "英伟达财报前怎么看"}, ensure_ascii=False),
        )
        self.assertEqual(status, 422)
        self.assertIn("underlying", json.loads(raw)["error"])

    def test_unknown_api_is_404(self):
        status, _ = _request(self.server, "GET", "/api/does-not-exist")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
