"""辩论 UI 集成测试：run_chat 返回 debateTrace、HTTP /api/chat 离线路径、第五面板静态断言。"""

from __future__ import annotations

import http.client
import json
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from src.agents.runtime import load_cards
from src.decision_pipeline import DEFAULT_INPUT, load_frozen_snapshot
from src.ui_server import Handler, run_chat
from tests.test_debate_runtime import FakeLLM, full_responses

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"

MESSAGE = "腾讯业绩快到了，方向不确定。账户 10 万港币，单笔最多亏 5%，帮我看看跨式。"


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(server: ThreadingHTTPServer, method: str, path: str, body: str | None = None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=120
    )
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body.encode("utf-8") if body is not None else None, headers=headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    connection.close()
    return response.status, raw


class RunChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_frozen_snapshot(DEFAULT_INPUT)
        cls.cards = load_cards()

    def test_offline_without_key_returns_offline_trace(self):
        state = run_chat(
            MESSAGE, self.snapshot, audit_enabled=False, debate_client=None
        )

        self.assertEqual(state["chat"]["scenario"]["underlying"], "HK.00700")
        self.assertEqual(state["decisionCard"]["verdict"], "NO_TRADE")
        self.assertIsNotNone(state["debateTrace"])
        self.assertEqual(state["debateTrace"]["status"], "offline")
        self.assertEqual(state["debateTrace"]["fallback_reason"], "no_api_key")
        self.assertIsNone(state["researchConsensus"])
        self.assertIn("llm", state)

    def test_fake_llm_attaches_trace_and_consensus(self):
        fake = FakeLLM(full_responses(self.cards))
        state = run_chat(
            MESSAGE, self.snapshot, audit_enabled=False, debate_client=fake
        )

        self.assertEqual(state["debateTrace"]["status"], "complete")
        self.assertIsNotNone(state["researchConsensus"])
        self.assertEqual(state["researchConsensus"]["source"], "llm")
        # verdict 仍来自确定性管线
        self.assertEqual(state["decisionCard"]["verdict"], "NO_TRADE")
        # 整个状态可 JSON 序列化
        json.dumps(state, ensure_ascii=False)


class HttpDebateTests(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_api_state_has_debate_fields(self):
        status, raw = _request(self.server, "GET", "/api/state")
        payload = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertIn("debateTrace", payload)
        self.assertIn("researchConsensus", payload)
        self.assertIn("llm", payload)
        self.assertIsNone(payload["debateTrace"])

    def test_chat_offline_path(self):
        body = json.dumps({"message": MESSAGE}, ensure_ascii=False)
        with mock.patch("src.ui_server.create_client", return_value=None):
            status, raw = _request(self.server, "POST", "/api/chat?no_audit=1", body)
        payload = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(payload["debateTrace"]["status"], "offline")
        self.assertEqual(
            payload["debateTrace"]["fallback_reason"], "no_api_key"
        )
        self.assertIsNone(payload["researchConsensus"])
        self.assertEqual(payload["decisionCard"]["verdict"], "NO_TRADE")


class DebatePanelStaticTests(unittest.TestCase):
    """第五面板静态断言：元素存在、渲染只用 textContent、回退数据带辩论字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (UI_DIR / "app.js").read_text(encoding="utf-8")
        cls.data_js = (UI_DIR / "data.js").read_text(encoding="utf-8")

    def test_panel_elements_present(self):
        for element_id in (
            "llm-chip",
            "debate-dock",
            "debate-toggle",
            "debate-body",
            "debate-meta",
            "debate-rounds",
            "debate-disputes",
            "debate-consensus",
            "debate-disclaimer",
        ):
            self.assertIn(f'id="{element_id}"', self.html, element_id)

    def test_render_functions_defined_and_bound(self):
        self.assertIn("function renderDebate(D)", self.app_js)
        self.assertIn("function bindDebateToggle()", self.app_js)
        self.assertIn("renderDebate(D);", self.app_js)
        self.assertIn("bindDebateToggle();", self.app_js)

    def test_dynamic_text_only_via_textcontent(self):
        # app.js 中所有 innerHTML 赋值都必须是清空（""），动态文本一律 textContent
        for line in self.app_js.splitlines():
            if "innerHTML" in line:
                self.assertRegex(line, r'innerHTML\s*=\s*"";?', line)
        self.assertIn("textContent", self.app_js)

    def test_static_fallback_data_has_debate_fields(self):
        self.assertIn("debateTrace: null", self.data_js)
        self.assertIn("researchConsensus: null", self.data_js)
        llm_match = re.search(r'llm:\s*\{[^}]*\}', self.data_js)
        self.assertIsNotNone(llm_match)
        block = llm_match.group(0)
        self.assertIn("available: false", block)
        self.assertIn('status: "offline"', block)


if __name__ == "__main__":
    unittest.main()
