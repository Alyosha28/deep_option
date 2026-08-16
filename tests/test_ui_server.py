"""UI 本地服务测试：离线、只读、临时端口，不写审计与决策卡文件。"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from src.decision_pipeline import DEFAULT_INPUT, compute_engine, load_frozen_snapshot
from src.ui_server import (
    Handler,
    _expiries_ui,
    _project_symbol_from_text,
    build_state,
    run_agent_action,
)


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
            "workspace",
        ):
            self.assertIn(key, state)
        self.assertEqual(state["workspace"]["activeProjectId"], "tencent-0700")
        self.assertEqual(state["workspace"]["projects"][0]["symbol"], "HK.00700")
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

    @patch.dict(os.environ, {"GOAI_RESEARCH_ITEMS_PATH": "data/research_items_hero.json"})
    def test_research_source_path_is_exposed(self):
        state = build_state()

        self.assertEqual(state["research"]["sourcePath"], "data/research_items_hero.json")
        self.assertEqual(state["research"]["sourceMode"], "配置文件")

    def test_terminal_surface_shape_is_explicit(self):
        state = build_state()
        terminal = state["terminal"]

        self.assertEqual(terminal["quote"]["symbol"], "HK.00700")
        self.assertEqual(terminal["chain"]["coverage"], "仅录制 ATM 合约")
        self.assertIn("points", terminal["chart"])
        self.assertGreater(len(terminal["chart"]["points"]), 0)
        self.assertEqual(terminal["decision"]["verdict"], "NO_TRADE")

    @patch.dict(os.environ, {"GOAI_OPENBB_ENABLED": "1", "GOAI_OPENBB_PROVIDER": "yfinance"})
    @patch(
        "src.ui_server.fetch_historical",
        return_value={
            "available": True,
            "reason": None,
            "provider": "yfinance",
            "source": "OpenBB",
            "symbol": "0700.HK",
            "capturedAt": "2026-08-14T00:00:00+00:00",
            "metrics": {"latestHv30d": 28.4},
            "points": [
                {"date": "2026-08-13", "open": 470.0, "high": 481.0, "low": 469.0, "close": 479.0, "volume": 1000.0, "hv30d": 28.4}
            ],
        },
    )
    def test_openbb_history_is_opt_in_and_available_to_price_chart(self, fetch):
        state = build_state()
        history = state["terminal"]["history"]

        self.assertTrue(history["available"])
        self.assertEqual(history["provider"], "yfinance")
        self.assertEqual(state["terminal"]["chart"]["price"]["points"][0]["close"], 479.0)
        self.assertEqual(state["terminal"]["chart"]["volatility"]["points"][0]["hv30d"], 28.4)
        self.assertEqual(state["terminal"]["quoteMetrics"]["historicalHv30d"], 28.4)
        fetch.assert_called_once()


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

    def test_api_decision_card_export_shape(self):
        status, body = _request(self.server, "GET", "/api/decision-card")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(payload["found"], "hero 管线应已落盘决策卡")
        self.assertRegex(payload["path"], r"^data[\\/]decision_card_.+\.json$")
        self.assertRegex(payload["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["card"]["verdict"], "NO_TRADE")

    def test_api_audit_returns_chain_projection(self):
        status, body = _request(self.server, "GET", "/api/audit?limit=20")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(payload["found"], "审计日志应存在")
        self.assertTrue(payload["chainOk"], "哈希链应完整衔接")
        self.assertGreater(payload["total"], 0)
        self.assertLessEqual(len(payload["entries"]), 20)
        last = payload["entries"][-1]
        self.assertIn("event", last)
        self.assertIn("summary", last)
        self.assertIn("droppedRefs", last)
        self.assertRegex(last["hash"], r"^[0-9a-f]{12}$")

    def test_api_audit_degrades_when_log_missing(self):
        from src import ui_server as mod

        with patch.object(mod, "AUDIT_LOG", Path("nonexistent") / "audit.jsonl"):
            status, body = _request(self.server, "GET", "/api/audit")
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertFalse(payload["found"])
            self.assertEqual(payload["entries"], [])
            self.assertFalse(payload["chainOk"])

    def test_api_metrics_records_and_returns(self):
        from src import ui_server as mod

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "metrics.jsonl"
            with patch.object(mod, "METRICS_LOG", log):
                mod._record_session_metric("test/metrics", "测试输入", "NO_TRADE", 1234, "REPLAY")
                status, body = _request(self.server, "GET", "/api/metrics?limit=10")
                payload = json.loads(body)

                self.assertEqual(status, 200)
                self.assertTrue(payload["found"])
                self.assertEqual(payload["total"], 1)
                last = payload["entries"][-1]
                self.assertEqual(last["event"], "test/metrics")
                self.assertEqual(last["verdict"], "NO_TRADE")
                self.assertEqual(last["durationMs"], 1234)
                self.assertEqual(payload["stats"]["byVerdict"]["NO_TRADE"], 1)

    def test_api_agent_action_records_session_metric(self):
        from src import ui_server as mod

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "metrics.jsonl"
            with patch.object(mod, "METRICS_LOG", log):
                status, body = _request(
                    self.server,
                    "POST",
                    "/api/agent?no_audit=1",
                    json.dumps({"action": "select_expiry", "expiry": "2026-08-28"}),
                )
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertIn("decisionCard", payload)
                lines = log.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                rec = json.loads(lines[0])
                self.assertEqual(rec["event"], "agent")
                self.assertEqual(rec["verdict"], "NO_TRADE")
                self.assertGreater(rec["duration_ms"], 0)

    def test_api_projects_lists_registered_workspace(self):
        status, body = _request(self.server, "GET", "/api/projects")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["activeProjectId"], "tencent-0700")
        self.assertEqual(payload["projects"][0]["symbol"], "HK.00700")

    def test_api_project_discovery_returns_safe_candidates(self):
        status, body = _request(
            self.server, "GET", "/api/projects/discover?symbol=US.AAPL"
        )
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["symbol"], "US.AAPL")
        self.assertIsInstance(payload["candidates"], list)

    def test_agent_discovery_request_reports_missing_snapshot(self):
        payload = run_agent_action(
            {
                "action": "discover_project",
                "symbol": "US.AAPL",
                "query": "US.AAPL",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")
        self.assertIn("没有找到", payload["agent"]["message"])
        self.assertIn("data/projects", payload["agent"]["message"])

    def test_agent_natural_language_discovery_does_not_parse_as_scenario(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "帮我找一下 US.AAPL 的快照和投研资料并加入工作区",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")

    def test_agent_research_request_with_ticker_starts_discovery_automatically(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "研究 US.AAPL 的期权机会",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")
        self.assertIn("没有找到", payload["agent"]["message"])

    def test_agent_research_request_with_company_name_starts_discovery_automatically(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "研究苹果公司的期权机会",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")

    def test_agent_research_request_with_numeric_code_starts_discovery_automatically(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "研究 600519 的股票和期权机会",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")

    def test_agent_research_request_with_long_english_company_name_starts_discovery(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "研究 Berkshire Hathaway 的期权机会",
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["action"], "discover_project")
        self.assertEqual(payload["agent"]["intent"], "discovery")

    def test_generic_display_symbol_does_not_confuse_company_name(self):
        self.assertEqual(_project_symbol_from_text("NASDAQ AAPL"), "NASDAQ.AAPL")
        self.assertEqual(_project_symbol_from_text("600519 SSE"), "SSE.600519")
        self.assertIsNone(_project_symbol_from_text("Berkshire Hathaway"))
        self.assertIsNone(_project_symbol_from_text("Tesla Motors"))

    def test_agent_discovery_registers_matching_assets_and_switches_workspace(self):
        from src import ui_server as mod

        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "data" / "projects").mkdir(parents=True)
            source = json.loads(
                (project_root / "data" / "hero_inputs.json").read_text(encoding="utf-8")
            )
            (root / "data" / "hero_inputs.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8"
            )
            source["underlying"] = "US.AAPL"
            source["name"] = "Apple Inc."
            (root / "data" / "projects" / "aapl_inputs.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8"
            )
            research = (
                project_root / "data" / "research_items_hero.json"
            ).read_text(encoding="utf-8")
            (root / "data" / "projects" / "aapl_research.json").write_text(
                research, encoding="utf-8"
            )
            (root / "data" / "research_items_empty.json").write_text(
                json.dumps({"meta": {}, "items": []}), encoding="utf-8"
            )
            with patch.object(mod, "ROOT", root), patch.object(
                mod, "_ACTIVE_PROJECT_ID", None
            ), patch.dict(
                os.environ,
                {"GOAI_WORKSPACE_PATH": "data/workspaces.json"},
                clear=False,
            ):
                mod.invalidate_state_cache()
                payload = mod.run_agent_action(
                    {
                        "action": "ask",
                        "message": "帮我找一下 US.AAPL 的快照和投研资料并加入工作区",
                    },
                    audit_enabled=False,
                )
                mod.invalidate_state_cache()

        self.assertEqual(payload["underlying"]["code"], "US.AAPL")
        self.assertEqual(payload["workspace"]["activeProjectId"], "us-aapl")
        self.assertEqual(
            payload["research"]["sourcePath"], "data/projects/aapl_research.json"
        )
        self.assertIn("已自动找到并打开", payload["agent"]["message"])

    def test_agent_discovery_registers_generic_numeric_market_project(self):
        from src import ui_server as mod

        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "data" / "projects").mkdir(parents=True)
            source = json.loads(
                (project_root / "data" / "hero_inputs.json").read_text(encoding="utf-8")
            )
            (root / "data" / "hero_inputs.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8"
            )
            source["underlying"] = "SSE.600519"
            source["name"] = "贵州茅台"
            (root / "data" / "projects" / "kweichow_inputs.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8"
            )
            research_payload = json.loads(
                (project_root / "data" / "research_items_hero.json").read_text(
                    encoding="utf-8"
                )
            )
            research_payload["meta"] = {
                "underlying": "SSE.600519",
                "name": "贵州茅台",
            }
            (root / "data" / "projects" / "kweichow_research.json").write_text(
                json.dumps(research_payload, ensure_ascii=False), encoding="utf-8"
            )
            (root / "data" / "research_items_empty.json").write_text(
                json.dumps({"meta": {}, "items": []}), encoding="utf-8"
            )
            with patch.object(mod, "ROOT", root), patch.object(
                mod, "_ACTIVE_PROJECT_ID", None
            ), patch.dict(
                os.environ,
                {"GOAI_WORKSPACE_PATH": "data/workspaces.json"},
                clear=False,
            ):
                mod.invalidate_state_cache()
                payload = mod.run_agent_action(
                    {
                        "action": "ask",
                        "message": "研究 600519 的股票和期权机会",
                    },
                    audit_enabled=False,
                )
                mod.invalidate_state_cache()

        self.assertEqual(payload["underlying"]["code"], "SSE.600519")
        self.assertEqual(payload["workspace"]["activeProjectId"], "sse-600519")
        self.assertEqual(
            payload["research"]["sourcePath"],
            "data/projects/kweichow_research.json",
        )
        self.assertIn("已自动找到并打开", payload["agent"]["message"])

    def test_api_project_selection_returns_current_state(self):
        status, body = _request(
            self.server,
            "POST",
            "/api/projects/select?no_audit=1",
            json.dumps({"projectId": "tencent-0700"}),
        )
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"]["activeProjectId"], "tencent-0700")
        self.assertEqual(payload["underlying"]["code"], "HK.00700")

    def test_api_project_registration_rejects_file_path_escape(self):
        status, body = _request(
            self.server,
            "POST",
            "/api/projects?no_audit=1",
            json.dumps(
                {
                    "name": "越界项目",
                    "symbol": "US.AAPL",
                    "inputPath": "C:/Users/Public/aapl_inputs.json",
                },
                ensure_ascii=False,
            ),
        )

        self.assertEqual(status, 422)
        self.assertIn("data", json.loads(body)["error"])

    def test_api_project_registration_reads_data_mode_from_body(self):
        from src import ui_server as mod
        from src.workspace_registry import load_registry

        original_active = mod._ACTIVE_PROJECT_ID
        original_build = mod.build_state_cached
        mod.build_state_cached = lambda: {"ok": True}
        repo = Path(__file__).resolve().parent.parent
        try:
            for field, symbol in (("dataMode", "HK.09999"), ("data_mode", "HK.09998")):
                with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    projects_dir = root / "data" / "projects"
                    projects_dir.mkdir(parents=True)
                    snapshot = json.loads(
                        (repo / "data" / "hero_inputs.json").read_text(encoding="utf-8")
                    )
                    snapshot["underlying"] = symbol
                    snapshot["name"] = symbol
                    (projects_dir / f"{symbol}.json").write_text(
                        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
                    )
                    (root / "data" / "research_items_empty.json").write_text(
                        json.dumps({"meta": {}, "items": []}), encoding="utf-8"
                    )
                    with patch.object(mod, "ROOT", root):
                        mod._ACTIVE_PROJECT_ID = None
                        server = _start_server()
                        try:
                            status, body = _request(
                                server,
                                "POST",
                                "/api/projects?no_audit=1",
                                json.dumps(
                                    {
                                        "name": f"live-{field}",
                                        "symbol": symbol,
                                        field: "live",
                                        "inputPath": f"data/projects/{symbol}.json",
                                        "researchItemsPath": "data/research_items_empty.json",
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            self.assertEqual(status, 200, body)
                            registry = load_registry(
                                root / "data" / "workspaces.json", root=root
                            )
                            project = next(
                                p for p in registry["projects"] if p["symbol"] == symbol
                            )
                            self.assertEqual(project["data_mode"], "live")
                        finally:
                            server.shutdown()
                            server.server_close()
                    mod._ACTIVE_PROJECT_ID = None
        finally:
            mod.build_state_cached = original_build
            mod._ACTIVE_PROJECT_ID = original_active
            mod.invalidate_state_cache()


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

    def test_terminal_command_go_returns_state(self):
        status, raw = _request(
            self.server,
            "POST",
            "/api/command?no_audit=1",
            json.dumps({"command": "0700 HK <GO>"}),
        )
        payload = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(payload["terminal"]["quote"]["symbol"], "HK.00700")

    def test_terminal_command_requires_command(self):
        status, raw = _request(
            self.server,
            "POST",
            "/api/command?no_audit=1",
            json.dumps({}),
        )
        self.assertEqual(status, 400)
        self.assertIn("command", json.loads(raw)["error"])

    def test_agent_select_expiry_returns_contextual_selection(self):
        status, raw = _request(
            self.server,
            "POST",
            "/api/agent?no_audit=1",
            json.dumps({"action": "select_expiry", "expiry": "2026-08-28"}),
        )
        payload = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["intent"], "selection")
        self.assertEqual(payload["terminal"]["selection"]["expiry"], "2026-08-28")
        self.assertTrue(payload["agent"]["suggestions"])

    def test_agent_select_expiry_preserves_working_set_context(self):
        payload = run_agent_action(
            {
                "action": "select_expiry",
                "expiry": "2026-08-28",
                "context": {
                    "scenario": {
                        "underlying": "HK.00700",
                        "view": "bullish",
                        "horizon": "2026-08-28 到期前",
                        "account_cash_hkd": 250_000,
                        "risk_budget_pct": 1,
                        "constraints": ["只用模拟盘"],
                    }
                },
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["scenario"]["view"], "bullish")
        self.assertEqual(payload["scenario"]["account_cash_hkd"], 250_000.0)
        self.assertEqual(payload["scenario"]["risk_budget_pct"], 1.0)
        self.assertEqual(payload["terminal"]["selection"]["expiry"], "2026-08-28")

    def test_agent_refresh_preserves_working_set_context(self):
        payload = run_agent_action(
            {
                "action": "refresh",
                "context": {
                    "scenario": {
                        "underlying": "HK.00700",
                        "view": "uncertain",
                        "horizon": "2026-08-14 业绩",
                        "account_cash_hkd": 200_000,
                        "risk_budget_pct": 2,
                        "constraints": ["只用模拟盘"],
                    },
                    "selectedExpiry": "2026-08-28",
                },
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["scenario"]["account_cash_hkd"], 200_000.0)
        self.assertEqual(payload["scenario"]["risk_budget_pct"], 2.0)
        self.assertEqual(payload["terminal"]["selection"]["expiry"], "2026-08-28")

    def test_agent_scenario_recalculation_uses_form_risk_budget(self):
        payload = run_agent_action(
            {
                "action": "run_scenario",
                "scenario": {
                    "underlying": "HK.00700",
                    "view": "uncertain",
                    "horizon": "2026-08-14 业绩",
                    "account_cash_hkd": 100_000,
                    "risk_budget_pct": 1,
                    "constraints": ["单笔最多亏损 1%"],
                },
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["intent"], "scenario")
        self.assertEqual(payload["scenario"]["risk_budget_pct"], 1.0)
        self.assertEqual(payload["terminal"]["risk"]["budgetHkd"], 1000.0)
        self.assertEqual(payload["decisionCard"]["riskGate"]["decision"], "BLOCK")

    def test_agent_context_question_does_not_require_repeating_symbol(self):
        with patch("src.ui_server.create_client", return_value=None):
            payload = run_agent_action(
                {"action": "ask", "message": "为什么先不交易？"},
                audit_enabled=False,
            )

        self.assertEqual(payload["agent"]["intent"], "context")
        self.assertIn("结论", payload["agent"]["message"])

    def test_agent_partial_message_merges_into_current_working_set(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "把风险上限改为 1%",
                "context": {
                    "scenario": {
                        "underlying": "HK.00700",
                        "view": "bullish",
                        "horizon": "2026-08-28 到期前",
                        "account_cash_hkd": 250_000,
                        "risk_budget_pct": 5,
                        "constraints": ["只用模拟盘"],
                    },
                    "selectedExpiry": "2026-08-28",
                },
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["intent"], "scenario")
        self.assertEqual(payload["chat"]["scenario"]["view"], "bullish")
        self.assertEqual(payload["chat"]["scenario"]["account_cash_hkd"], 250_000.0)
        self.assertEqual(payload["chat"]["scenario"]["risk_budget_pct"], 1.0)
        self.assertEqual(payload["terminal"]["selection"]["expiry"], "2026-08-28")
        self.assertEqual(payload["terminal"]["risk"]["budgetHkd"], 2500.0)

    def test_agent_navigation_request_returns_executable_view_action(self):
        payload = run_agent_action(
            {
                "action": "ask",
                "message": "打开期权链",
                "context": {
                    "scenario": {
                        "underlying": "HK.00700",
                        "view": "uncertain",
                        "horizon": "2026-08-14 业绩",
                        "account_cash_hkd": 100_000,
                        "risk_budget_pct": 5,
                        "constraints": [],
                    }
                },
            },
            audit_enabled=False,
        )

        self.assertEqual(payload["agent"]["intent"], "navigation")
        self.assertEqual(payload["agent"]["actions"][0]["type"], "open_view")
        self.assertEqual(payload["agent"]["actions"][0]["view"], "chain")
        self.assertTrue(all(item["status"] == "complete" for item in payload["agent"]["trace"]))


if __name__ == "__main__":
    unittest.main()
