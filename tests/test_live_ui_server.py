"""LIVE 模式 UI 服务测试：模式隔离、只读报价缓存、只读 state、OpenD 不可用。

使用注入的 fake gateway 驱动 ``src.ui_server`` 的真实 HTTP 端点；不启动真实
OpenD，不写审计与决策卡文件。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, ClassVar
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
from src.workspace_registry import load_registry


def _envelope(
    operation: str,
    *,
    data: Any = None,
    status: EnvelopeStatus = EnvelopeStatus.OK,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    typed_error: GatewayError | None = None,
) -> DataEnvelope:
    return DataEnvelope.now(
        mode=DataMode.LIVE,
        origin_source="FUTU",
        freshness_status=freshness,
        request={"operation": operation},
        status=status,
        data=data,
        entitlements={},
        warnings=[],
        typed_error=typed_error,
    )


class _ReadyGateway:
    """Ready live gateway with quotes consistent with the hero snapshot."""

    mode = DataMode.LIVE

    def __init__(self) -> None:
        self.snapshot_calls = 0

    def health(self) -> DataEnvelope:
        return _envelope("health", data={"ready": True})

    def capabilities(self) -> DataEnvelope:
        return _envelope("capabilities", data={"market_data": True})

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        self.snapshot_calls += 1
        rows: list[dict[str, Any]] = []
        for code in codes:
            if "TCH" in code.upper():
                rows.append(
                    {
                        "code": code,
                        "last_price": 10.0,
                        "bid": 9.8,
                        "ask": 10.2,
                        "mid_price": 10.0,
                        "implied_volatility": 30.0,
                        "open_interest": 100,
                        "volume": 50,
                    }
                )
            else:
                rows.append(
                    {
                        "code": code,
                        "last_price": 478.8,
                        "previous_close": 500.0,
                        "bid": 478.6,
                        "ask": 479.0,
                        "volume": 1000,
                    }
                )
        return _envelope("get_market_snapshot", data=rows)

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        return _envelope(
            "get_market_state",
            data=[{"code": code, "market_state": "MORNING"} for code in codes],
        )


class _DownGateway:
    """Live gateway whose OpenD health check is explicitly unavailable."""

    mode = DataMode.LIVE

    def health(self) -> DataEnvelope:
        return _envelope(
            "health",
            status=EnvelopeStatus.ERROR,
            freshness=FreshnessStatus.UNKNOWN,
            typed_error=GatewayError(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            ),
        )

    def capabilities(self) -> DataEnvelope:
        return _envelope("capabilities", data={"market_data": True})

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        return _envelope(
            "get_market_snapshot",
            status=EnvelopeStatus.ERROR,
            freshness=FreshnessStatus.UNKNOWN,
            typed_error=GatewayError(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            ),
        )

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        return _envelope(
            "get_market_state",
            data=[{"code": code, "market_state": "UNKNOWN"} for code in codes],
        )


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _read_sse(
    server: ThreadingHTTPServer,
    path: str,
    until: Callable[[dict[str, list[Any]]], bool],
    timeout: float = 15.0,
) -> tuple[int, Any, Any, dict[str, list[Any]]]:
    """打开一条 SSE 流，收集命名事件直到 ``until(events)`` 为真。

    返回 ``(status, body, connection, events)``：非 200 时 ``body`` 是 JSON
    解码后的错误负载且 ``connection`` 为 None；200 时调用方负责关闭
    ``connection``。
    """
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=30
    )
    connection.request("GET", path, headers={"Accept": "text/event-stream"})
    if connection.sock is not None:
        # 阻塞读：socket 读超时会把 SocketIO 毒化（后续读永久 OSError），
        # 因此读循环放独立线程、主线程按 until 谓词轮询，最后关连接唤醒。
        connection.sock.settimeout(None)
    response = connection.getresponse()
    if response.status != 200:
        raw = response.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        connection.close()
        return response.status, body, None, {}
    events: dict[str, list[Any]] = {}
    buffer = b""
    stop = threading.Event()

    def reader() -> None:
        nonlocal buffer
        while not stop.is_set():
            try:
                chunk = response.read1(4096)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                name: str | None = None
                data: Any = None
                for line in block.decode("utf-8").split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        raw_data = line[5:].strip()
                        try:
                            data = json.loads(raw_data)
                        except json.JSONDecodeError:
                            data = raw_data
                if name:
                    events.setdefault(name, []).append(data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    matched = False
    while time.monotonic() < deadline:
        if until(events):
            matched = True
            break
        time.sleep(0.05)
    stop.set()
    try:
        connection.close()
    except OSError:
        pass
    thread.join(1.0)
    if not matched:
        raise AssertionError("SSE 等待事件超时；已收到：{}".format(
            {name: len(items) for name, items in events.items()}
        ))
    return 200, None, connection, events


def _request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    body: str | None = None,
) -> tuple[int, Any]:
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
    raw = response.read().decode("utf-8")
    connection.close()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    return response.status, payload


class LiveUiServerTests(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        mod._reset_live_data_service()
        mod.invalidate_state_cache()

    def tearDown(self) -> None:
        mod._reset_live_data_service()
        mod.invalidate_state_cache()
        mod._LIVE_GATEWAY_FACTORY = mod._create_live_gateway

    def test_live_quote_reads_cached_quotes_without_pipeline(self) -> None:
        gateway = _ReadyGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()

            with patch("src.ui_server.run_pipeline") as run_pipeline:
                status, first = _request(
                    self.server, "GET", "/api/live-quote?codes=HK.00700"
                )
                status2, second = _request(
                    self.server, "GET", "/api/live-quote?codes=HK.00700"
                )

        self.assertEqual(status, 200)
        self.assertEqual(status2, 200)
        self.assertEqual(first["mode"], "LIVE")
        self.assertEqual(first["quotes"][0]["code"], "HK.00700")
        self.assertEqual(first["quotes"][0]["last"], 478.8)
        self.assertEqual(first["quotes"][0]["prevClose"], 500.0)
        self.assertEqual(second["quotes"][0]["last"], 478.8)
        self.assertEqual(gateway.snapshot_calls, 1, "TTL 内的第二次报价请求必须命中缓存")
        run_pipeline.assert_not_called()

    def test_live_quote_is_rejected_in_replay_mode(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "replay"}, clear=False):
            status, payload = _request(
                self.server, "GET", "/api/live-quote?codes=HK.00700"
            )

        self.assertEqual(status, 422)
        self.assertIn("GOAI_DATA_MODE=live", payload["error"])

    def test_live_quote_uses_project_data_mode_when_env_unset(self) -> None:
        gateway = _ReadyGateway()
        project = dict(mod._workspace_project(), data_mode="live")
        with patch.dict(os.environ, {"GOAI_DATA_MODE": ""}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()

            with patch.object(mod, "_workspace_project", return_value=project):
                status, payload = _request(
                    self.server, "GET", "/api/live-quote?codes=HK.00700"
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "LIVE")
        self.assertEqual(payload["quotes"][0]["code"], "HK.00700")
        self.assertEqual(payload["quotes"][0]["last"], 478.8)

    def test_state_in_live_mode_uses_live_snapshot_and_stays_read_only(self) -> None:
        gateway = _ReadyGateway()
        real_run_pipeline = mod.run_pipeline
        captured: dict[str, Any] = {}

        def recording_run_pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return real_run_pipeline(*args, **kwargs)

        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            with patch.object(
                mod, "run_pipeline", side_effect=recording_run_pipeline
            ):
                status, state = _request(self.server, "GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(state["meta"]["mode"], "LIVE")
        self.assertEqual(state["meta"]["freshness"], "FRESH")
        self.assertEqual(state["underlying"]["prevClose"], 500.0)
        self.assertEqual(captured.get("audit_enabled"), False)
        self.assertEqual(captured.get("write_card"), False)
        self.assertEqual((captured.get("snapshot_data") or {}).get("mode"), "LIVE")

    def test_state_in_replay_mode_stays_replay(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "replay"}, clear=False):
            mod.invalidate_state_cache()
            status, state = _request(self.server, "GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(state["meta"]["mode"], "REPLAY")

    def test_state_when_opend_down_returns_explicit_error_without_replay_fallback(
        self,
    ) -> None:
        gateway = _DownGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            status, state = _request(self.server, "GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(state["meta"]["mode"], "LIVE")
        self.assertEqual(state["error"]["code"], "OPEND_UNAVAILABLE")
        self.assertNotIn("decisionCard", state)
        self.assertNotEqual(state["meta"]["mode"], "REPLAY")

    def test_live_quote_when_opend_down_returns_typed_error(self) -> None:
        gateway = _DownGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()

            status, payload = _request(
                self.server, "GET", "/api/live-quote?codes=HK.00700"
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload["typedError"]["code"], "OPEND_UNAVAILABLE")
        self.assertTrue(payload["typedError"]["retryable"])

    def test_agent_refresh_in_live_mode_uses_live_snapshot(self) -> None:
        gateway = _ReadyGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            state = mod.run_agent_action({"action": "refresh"}, audit_enabled=False)

        self.assertEqual(state["meta"]["mode"], "LIVE")
        self.assertEqual(state["meta"]["freshness"], "FRESH")
        self.assertNotEqual(state["meta"]["mode"], "REPLAY")
        self.assertEqual(state["underlying"]["prevClose"], 500.0)

    def test_agent_debate_in_live_mode_uses_live_snapshot(self) -> None:
        gateway = _ReadyGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            # run_chat 的五阶段管线是本测试关注点；十角色辩论是慢速
            # LLM 路径，这里只验证 live snapshot 被传入管线并反映到 meta。
            with patch.object(
                mod, "create_client", return_value=object()
            ), patch.object(
                mod, "run_debate", return_value={"research_consensus": None}
            ):
                state = mod.run_agent_action({"action": "debate"}, audit_enabled=False)

        self.assertEqual(state["meta"]["mode"], "LIVE")
        self.assertEqual(state["meta"]["freshness"], "FRESH")
        self.assertNotEqual(state["meta"]["mode"], "REPLAY")

    def test_agent_refresh_when_opend_down_raises_typed_error(self) -> None:
        gateway = _DownGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            with self.assertRaises(mod._LiveDataError) as raised:
                mod.run_agent_action({"action": "refresh"}, audit_enabled=False)

        self.assertEqual(
            raised.exception.to_dict()["code"], GatewayErrorCode.OPEND_UNAVAILABLE.value
        )

    def test_post_projects_live_opend_down_registers_once_and_is_idempotent(
        self,
    ) -> None:
        """回归：POST /api/projects 注册 live 项目时 OpenD 不可用。

        真实路径（不 patch build_state_cached）：注册已持久化，第一次响应
        必须仍表示注册成功（顶层无 error），重试同一请求也必须 200 而不是
        422「项目 id 已存在/标的已存在」。
        """

        gateway = _DownGateway()
        repo = Path(__file__).resolve().parent.parent
        aapl_projects: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            projects_dir = root / "data" / "projects"
            projects_dir.mkdir(parents=True)
            snapshot = json.loads(
                (repo / "data" / "hero_inputs.json").read_text(encoding="utf-8")
            )
            snapshot["underlying"] = "US.AAPL"
            snapshot["name"] = "Apple Inc."
            (projects_dir / "aapl_inputs.json").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            (root / "data" / "research_items_empty.json").write_text(
                json.dumps({"meta": {}, "items": []}), encoding="utf-8"
            )
            body = json.dumps(
                {
                    "name": "Apple Inc.",
                    "symbol": "US.AAPL",
                    "inputPath": "data/projects/aapl_inputs.json",
                    "researchItemsPath": "data/research_items_empty.json",
                    "dataMode": "live",
                },
                ensure_ascii=False,
            )
            with patch.dict(os.environ, {"GOAI_DATA_MODE": ""}, clear=False), patch.object(
                mod, "ROOT", root
            ), patch.object(mod, "_ACTIVE_PROJECT_ID", None):
                mod._LIVE_GATEWAY_FACTORY = lambda: gateway
                mod._reset_live_data_service()
                mod.invalidate_state_cache()
                try:
                    status1, payload1 = _request(
                        self.server, "POST", "/api/projects?no_audit=1", body
                    )
                    status2, payload2 = _request(
                        self.server, "POST", "/api/projects?no_audit=1", body
                    )
                finally:
                    mod._reset_live_data_service()
                    mod.invalidate_state_cache()

            registry = load_registry(root / "data" / "workspaces.json", root=root)
            aapl_projects = [
                project
                for project in registry["projects"]
                if project["symbol"] == "US.AAPL"
            ]

        self.assertEqual(status1, 200, payload1)
        self.assertEqual(status2, 200, payload2)
        self.assertNotIn("error", payload1)
        self.assertEqual(payload1.get("registered"), True)
        self.assertEqual(payload1["workspace"]["activeProjectId"], "us-aapl")
        self.assertEqual(payload1["stateError"]["code"], "OPEND_UNAVAILABLE")
        self.assertNotIn("error", payload2)
        self.assertEqual(payload2.get("registered"), True)
        self.assertEqual(len(aapl_projects), 1)
        self.assertEqual(aapl_projects[0]["data_mode"], "live")


    def test_post_command_live_opend_down_returns_typed_error_not_500(
        self,
    ) -> None:
        """回归：LIVE + OpenD down 时 /api/command 不能 KeyError('terminal')。"""

        gateway = _DownGateway()
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod.invalidate_state_cache()

            status, payload = _request(
                self.server,
                "POST",
                "/api/command?no_audit=1",
                json.dumps({"command": "RUN"}, ensure_ascii=False),
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload["typedError"]["code"], "OPEND_UNAVAILABLE")
        self.assertNotIn("terminal", payload)

    def test_post_projects_select_live_opend_down_selects_and_reports_state_error(
        self,
    ) -> None:
        """回归：LIVE + OpenD down 时项目切换已成功，不能返回顶层 error。"""

        gateway = _DownGateway()
        repo = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            projects_dir = root / "data" / "projects"
            projects_dir.mkdir(parents=True)
            snapshot = json.loads(
                (repo / "data" / "hero_inputs.json").read_text(encoding="utf-8")
            )
            snapshot["underlying"] = "US.AAPL"
            snapshot["name"] = "Apple Inc."
            (projects_dir / "aapl_inputs.json").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            (root / "data" / "research_items_empty.json").write_text(
                json.dumps({"meta": {}, "items": []}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"GOAI_DATA_MODE": ""}, clear=False), patch.object(
                mod, "ROOT", root
            ), patch.object(mod, "_ACTIVE_PROJECT_ID", None):
                mod._LIVE_GATEWAY_FACTORY = lambda: gateway
                mod._reset_live_data_service()
                mod.invalidate_state_cache()
                try:
                    registered = mod.register_project(
                        name="Apple Inc.",
                        symbol="US.AAPL",
                        input_path=projects_dir / "aapl_inputs.json",
                        research_items_path=root / "data" / "research_items_empty.json",
                        data_mode="live",
                        registry_path=root / "data" / "workspaces.json",
                        root=root,
                    )
                    status, payload = _request(
                        self.server,
                        "POST",
                        "/api/projects/select",
                        json.dumps(
                            {"projectId": registered["id"]}, ensure_ascii=False
                        ),
                    )
                finally:
                    mod._reset_live_data_service()
                    mod.invalidate_state_cache()

        self.assertEqual(status, 200)
        self.assertNotIn("error", payload)
        self.assertEqual(payload.get("selected"), True)
        self.assertEqual(payload["project"]["id"], registered["id"])
        self.assertEqual(payload["stateError"]["code"], "OPEND_UNAVAILABLE")
        self.assertEqual(payload["workspace"]["activeProjectId"], registered["id"])


class _MutableGateway:
    """Ready live gateway whose underlying price can change between polls."""

    mode = DataMode.LIVE

    def __init__(self) -> None:
        self.price = 478.8

    def health(self) -> DataEnvelope:
        return _envelope("health", data={"ready": True})

    def capabilities(self) -> DataEnvelope:
        return _envelope("capabilities", data={"market_data": True})

    def get_market_snapshot(self, codes: list[str]) -> DataEnvelope:
        rows: list[dict[str, Any]] = []
        for code in codes:
            if "TCH" in code.upper():
                rows.append(
                    {
                        "code": code,
                        "last_price": 10.0,
                        "bid": 9.8,
                        "ask": 10.2,
                        "mid_price": 10.0,
                        "implied_volatility": 30.0,
                        "open_interest": 100,
                        "volume": 50,
                    }
                )
            else:
                rows.append(
                    {
                        "code": code,
                        "last_price": self.price,
                        "previous_close": 500.0,
                        "bid": self.price - 0.2,
                        "ask": self.price + 0.2,
                        "volume": 1000,
                    }
                )
        return _envelope("get_market_snapshot", data=rows)

    def get_market_state(self, codes: list[str]) -> DataEnvelope:
        return _envelope(
            "get_market_state",
            data=[{"code": code, "market_state": "MORNING"} for code in codes],
        )


class LiveStreamServerTests(unittest.TestCase):
    """SSE /api/stream 端点测试：模式隔离、hello/quote/error 事件、上限与断连。"""

    server: ClassVar[ThreadingHTTPServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        mod._close_live_stream()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        mod._close_live_stream()
        mod._reset_live_data_service()
        mod.invalidate_state_cache()

    def tearDown(self) -> None:
        mod._close_live_stream()
        mod._reset_live_data_service()
        mod.invalidate_state_cache()
        mod._LIVE_GATEWAY_FACTORY = mod._create_live_gateway

    def test_stream_rejected_in_replay_mode(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "replay"}, clear=False):
            status, payload = _request(
                self.server, "GET", "/api/stream?codes=HK.00700"
            )

        self.assertEqual(status, 422)
        self.assertIn("GOAI_DATA_MODE=live", payload["error"])

    def test_stream_hello_and_quote_change_in_live_mode(self) -> None:
        gateway = _MutableGateway()
        with patch.dict(
            os.environ,
            {"GOAI_DATA_MODE": "live", "GOAI_LIVE_STREAM_POLL_SECONDS": "0.05"},
            clear=False,
        ):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod._close_live_stream()

            state: dict[str, Any] = {"mutated": False}

            def until(events: dict[str, list[Any]]) -> bool:
                quotes = events.get("quote") or []
                if events.get("hello") and not state["mutated"]:
                    state["mutated"] = True
                    gateway.price = 479.9
                return any(
                    quote and quote.get("quotes")
                    and quote["quotes"][0].get("last") == 479.9
                    for quote in quotes
                )

            status, body, connection, events = _read_sse(
                self.server, "/api/stream?codes=HK.00700", until, timeout=15.0
            )
            try:
                self.assertEqual(status, 200, body)
                hello = events["hello"][0]
                self.assertEqual(hello["mode"], "LIVE")
                self.assertEqual(hello["codes"], ["HK.00700"])
                self.assertGreater(hello["pollSeconds"], 0)
                quotes = events["quote"]
                self.assertGreaterEqual(len(quotes), 2)
                self.assertEqual(quotes[0]["quotes"][0]["last"], 478.8)
                self.assertTrue(
                    any(q["quotes"][0]["last"] == 479.9 for q in quotes),
                    "价格变化必须通过 quote 事件推送",
                )
                self.assertEqual(mod._get_live_stream_service().hub.subscriber_count, 1)
            finally:
                if connection is not None:
                    connection.close()

    def test_stream_defaults_to_project_template_codes(self) -> None:
        gateway = _ReadyGateway()
        with patch.dict(
            os.environ,
            {"GOAI_DATA_MODE": "live", "GOAI_LIVE_STREAM_POLL_SECONDS": "0.05"},
            clear=False,
        ):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod._close_live_stream()

            status, body, connection, events = _read_sse(
                self.server,
                "/api/stream",
                lambda events: bool(events.get("hello")),
                timeout=10.0,
            )
            try:
                self.assertEqual(status, 200, body)
                codes = events["hello"][0]["codes"]
                self.assertEqual(codes[0], "HK.00700")
                self.assertGreaterEqual(len(codes), 5, "underlying + 4 条期权腿")
                self.assertIn("HK.TCH260814C480000", codes)
            finally:
                if connection is not None:
                    connection.close()

    def test_stream_rejects_invalid_codes(self) -> None:
        with patch.dict(os.environ, {"GOAI_DATA_MODE": "live"}, clear=False):
            status, payload = _request(
                self.server, "GET", "/api/stream?codes=%25%25%25"
            )

        self.assertEqual(status, 422)
        self.assertIn("codes", payload["error"])

    def test_stream_subscriber_cap_returns_503(self) -> None:
        gateway = _ReadyGateway()
        with patch.dict(
            os.environ,
            {
                "GOAI_DATA_MODE": "live",
                "GOAI_LIVE_STREAM_POLL_SECONDS": "0.05",
                "GOAI_LIVE_STREAM_MAX_SUBSCRIBERS": "1",
            },
            clear=False,
        ):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod._close_live_stream()

            status1, body1, connection, _events = _read_sse(
                self.server,
                "/api/stream?codes=HK.00700",
                lambda events: bool(events.get("hello")),
                timeout=10.0,
            )
            try:
                self.assertEqual(status1, 200, body1)
                status2, payload2 = _request(
                    self.server, "GET", "/api/stream?codes=HK.00700"
                )
                self.assertEqual(status2, 503)
                self.assertEqual(payload2["typedError"]["code"], "STREAM_CAPACITY")
            finally:
                if connection is not None:
                    connection.close()

    def test_stream_when_opend_down_emits_typed_error_event(self) -> None:
        gateway = _DownGateway()
        with patch.dict(
            os.environ,
            {"GOAI_DATA_MODE": "live", "GOAI_LIVE_STREAM_POLL_SECONDS": "0.05"},
            clear=False,
        ):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod._close_live_stream()

            status, body, connection, events = _read_sse(
                self.server,
                "/api/stream?codes=HK.00700",
                lambda events: bool(events.get("hello") and events.get("error")),
                timeout=10.0,
            )
            try:
                self.assertEqual(status, 200, body)
                hello = events["hello"][0]
                self.assertEqual(hello["error"]["code"], "OPEND_UNAVAILABLE")
                errors = events["error"]
                self.assertTrue(
                    any(item.get("code") == "OPEND_UNAVAILABLE" for item in errors)
                )
            finally:
                if connection is not None:
                    connection.close()

    def test_client_disconnect_unsubscribes_and_stops_feed(self) -> None:
        gateway = _MutableGateway()
        with patch.dict(
            os.environ,
            {"GOAI_DATA_MODE": "live", "GOAI_LIVE_STREAM_POLL_SECONDS": "0.05"},
            clear=False,
        ):
            mod._LIVE_GATEWAY_FACTORY = lambda: gateway
            mod._reset_live_data_service()
            mod._close_live_stream()

            status, body, _connection, _events = _read_sse(
                self.server,
                "/api/stream?codes=HK.00700",
                lambda events: bool(events.get("hello")),
                timeout=10.0,
            )
            self.assertEqual(status, 200, body)
            service = mod._get_live_stream_service()
            self.assertEqual(service.hub.subscriber_count, 1)
            # 客户端已断开。服务端只在写入时发现断连（心跳 15s 一次）；
            # 本测试把心跳间隔打到 0.2s：下一次心跳写失败即退订，快速确定。
            # 同时变更价格让轮询 feed 也产出一个 quote 事件（双保险）。
            gateway.price = 481.2
            with patch.object(mod, "_STREAM_HEARTBEAT_SECONDS", 0.2):
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and service.hub.subscriber_count > 0:
                    time.sleep(0.1)
            self.assertEqual(service.hub.subscriber_count, 0, "断连后必须退订")
            self.assertEqual(len(service._feeds), 0, "最后一个订阅离开后 feed 必须停止")


if __name__ == "__main__":
    unittest.main()
