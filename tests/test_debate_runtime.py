"""十角色辩论运行时测试：编排顺序、两轮定向调用、分歧选择、汇总 schema、
数字铁律、无 key / 401 / 超时 / 5xx 回退、审计脱敏。全部用 FakeLLM，不触网、不写审计文件。"""

from __future__ import annotations

import json
import unittest
from typing import Any

from src.agents.llm_client import LLMError, LLMSettings
from src.agents.runtime import load_cards, run_debate
from src.decision_pipeline import DEFAULT_INPUT, load_frozen_snapshot

SCENARIO = {
    "underlying": "HK.00700",
    "view": "uncertain",
    "horizon": "2026-08-12 业绩",
    "account_cash_hkd": 100000.0,
    "risk_budget_pct": 5.0,
    "constraints": ["单笔最多亏损 5%"],
}

API_KEY = "sk-debate-secret-key-9999"


class FakeLLM:
    """按 (role, round) 提供固定回复的假客户端，记录每次调用。"""

    def __init__(self, responses: dict[tuple[str, int], object] | None = None) -> None:
        self.settings = LLMSettings(api_key=API_KEY)
        self.responses: dict[tuple[str, int], object] = dict(responses or {})
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, **kwargs):
        labels = kwargs.get("labels") or {}
        role = labels.get("role", "unknown")
        round_no = labels.get("round", 0)
        model = kwargs.get("model")
        self.calls.append({"role": role, "round": round_no, "model": model})
        response = self.responses.get((role, round_no))
        if response is None:
            raise LLMError("http", "FakeLLM: 未提供该角色回复", retriable=True)
        if isinstance(response, Exception):
            raise response
        return {
            "content": json.dumps(response, ensure_ascii=False),
            "usage": {"total_tokens": 100},
            "model": model,
        }


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload) -> dict[str, Any]:
        self.events.append((event, dict(payload)))
        return {"hash": f"fake-{len(self.events):06d}"}


def analyst(role: str, round_no: int, stance: str = "neutral", refs=None, conclusion=None):
    payload: dict[str, Any] = {
        "conclusion": conclusion or f"{role} 第 {round_no} 轮结论：数据一致性尚可。",
        "evidence_refs": refs if refs is not None else ["frozen_snapshot"],
        "confidence": "medium",
        "stance": stance,
    }
    if round_no == 2:
        payload["counterpoint"] = f"{role} 回辩：维持原判断。"
    return payload


def orchestrator_r1(disagreements):
    return {
        "conclusion": "首轮归纳完成。",
        "evidence_refs": ["frozen_snapshot"],
        "confidence": "medium",
        "disagreements": disagreements,
    }


def orchestrator_r2(consensus=None):
    return {
        "conclusion": "共识已形成。",
        "evidence_refs": ["frozen_snapshot"],
        "confidence": "medium",
        "research_consensus": consensus
        or {
            "summary": "各角色一致认为当前冻结快照下买入跨式缺乏优势，确定性引擎结论不受影响。",
            "stance": "neutral",
            "confidence": "medium",
            "evidence_refs": ["frozen_snapshot", "self_built_engine"],
            "open_questions": ["等待 Live 行情接入"],
        },
    }


DISPUTES = [
    {
        "topic": "成本与预期波动的分歧",
        "roles": ["news_analyst", "options_strategist", "risk_manager"],
        "question": "当前冻结快照下该跨式是否值得执行，依据是什么？",
    }
]


def full_responses(cards):
    responses = {}
    for role_id in cards:
        if role_id == "orchestrator":
            responses[("orchestrator", 1)] = orchestrator_r1(DISPUTES)
            responses[("orchestrator", 2)] = orchestrator_r2()
        else:
            responses[(role_id, 1)] = analyst(role_id, 1)
            responses[(role_id, 2)] = analyst(role_id, 2)
    return responses


class DebateRuntimeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_frozen_snapshot(DEFAULT_INPUT)
        cls.cards = load_cards()
        cls.analyst_ids = [c for c in cls.cards if c != "orchestrator"]


class OfflineTests(DebateRuntimeBase):
    def test_no_client_returns_offline_trace(self):
        trace = run_debate(
            SCENARIO, self.snapshot, client=None, audit_enabled=False
        )

        self.assertEqual(trace["status"], "offline")
        self.assertEqual(trace["fallback_reason"], "no_api_key")
        self.assertIsNone(trace["research_consensus"])
        self.assertEqual(trace["rounds"], [])
        self.assertEqual(trace["verdict"], "NO_TRADE")


class OrchestrationTests(DebateRuntimeBase):
    def test_two_rounds_and_directed_rebuttals(self):
        fake = FakeLLM(full_responses(self.cards))
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        self.assertEqual(trace["status"], "complete")
        self.assertEqual(trace["provider"], "deepseek")
        self.assertEqual(trace["verdict"], "NO_TRADE")

        round_one = trace["rounds"][0]
        self.assertEqual(len(round_one["entries"]), 10)
        first_nine = [e["role"] for e in round_one["entries"][:9]]
        self.assertEqual(first_nine, self.analyst_ids)
        self.assertEqual(round_one["entries"][9]["role"], "orchestrator")

        round_two = trace["rounds"][1]
        rebuttal_roles = [e["role"] for e in round_two["entries"]]
        self.assertEqual(
            set(rebuttal_roles),
            {"news_analyst", "options_strategist", "risk_manager", "orchestrator"},
        )

        # 调用结构：首轮 9 分析角色并行 → 主席 → 次轮只调分歧角色 → 主席汇总
        # （并行调用记录顺序随线程调度变化，只断言集合与分轮结构）
        self.assertEqual(len(fake.calls), 14)
        first_nine_calls = fake.calls[:9]
        self.assertTrue(all(c["round"] == 1 for c in first_nine_calls))
        self.assertEqual(
            {c["role"] for c in first_nine_calls}, set(self.analyst_ids)
        )
        self.assertEqual(
            (fake.calls[9]["role"], fake.calls[9]["round"]), ("orchestrator", 1)
        )
        rebuttal_calls = fake.calls[10:13]
        self.assertTrue(all(c["round"] == 2 for c in rebuttal_calls))
        self.assertEqual(
            {c["role"] for c in rebuttal_calls},
            {"news_analyst", "options_strategist", "risk_manager"},
        )
        self.assertEqual(
            (fake.calls[13]["role"], fake.calls[13]["round"]),
            ("orchestrator", 2),
        )

    def test_disputes_normalized_and_attached(self):
        fake = FakeLLM(full_responses(self.cards))
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        self.assertEqual(len(trace["disputes"]), 1)
        self.assertEqual(trace["disputes"][0]["topic"], DISPUTES[0]["topic"])
        self.assertEqual(trace["disputes"][0]["roles"], DISPUTES[0]["roles"])

    def test_llm_consensus_schema(self):
        fake = FakeLLM(full_responses(self.cards))
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        consensus = trace["research_consensus"]
        self.assertIsNotNone(consensus)
        self.assertEqual(consensus["source"], "llm")
        self.assertFalse(consensus["degraded"])
        for key in ("summary", "stance", "confidence", "evidence_refs", "open_questions"):
            self.assertIn(key, consensus)
        self.assertEqual(consensus["stance"], "neutral")


class IronRuleTests(DebateRuntimeBase):
    def test_invented_references_dropped(self):
        responses = full_responses(self.cards)
        responses[("data_officer", 1)] = analyst(
            "data_officer", 1, refs=["frozen_snapshot", "invented_source_xyz"]
        )
        fake = FakeLLM(responses)
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        entry = next(
            e
            for e in trace["rounds"][0]["entries"]
            if e["role"] == "data_officer"
        )
        self.assertEqual(entry["evidence_refs"], ["frozen_snapshot"])
        self.assertEqual(entry["dropped_refs"], ["invented_source_xyz"])

    def test_llm_text_never_changes_engine_numbers(self):
        responses = full_responses(self.cards)
        for role_id in self.analyst_ids:
            responses[(role_id, 1)] = analyst(
                role_id,
                1,
                stance="favor",
                conclusion="强烈赞成：我的私算显示成本仅 100 港币，IV 为 1%。",
            )
        fake = FakeLLM(responses)
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        # verdict 与门控仍由确定性引擎产出，不受赞成结论影响
        self.assertEqual(trace["verdict"], "NO_TRADE")
        for round_block in trace["rounds"]:
            for entry in round_block["entries"]:
                self.assertNotIn("gate_override", entry)


class FallbackTests(DebateRuntimeBase):
    def test_orchestrator_failure_uses_deterministic_fallback(self):
        responses = full_responses(self.cards)
        responses[("orchestrator", 1)] = LLMError("http", "FakeLLM: 500")
        responses[("orchestrator", 2)] = LLMError("http", "FakeLLM: 500")
        for index, role_id in enumerate(self.analyst_ids):
            responses[(role_id, 1)] = analyst(
                role_id, 1, stance="favor" if index % 2 == 0 else "oppose"
            )
        fake = FakeLLM(responses)
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        self.assertEqual(trace["status"], "degraded")
        self.assertEqual(trace["fallback_reason"], "llm_error")
        consensus = trace["research_consensus"]
        self.assertEqual(consensus["source"], "deterministic_fallback")
        self.assertTrue(consensus["degraded"])
        self.assertIn("确定性引擎结论仍为 NO_TRADE", consensus["summary"])
        # 分歧点走确定性回退，仍被定向调用
        self.assertTrue(trace["disputes"])
        for dispute in trace["disputes"]:
            self.assertGreaterEqual(len(dispute["roles"]), 2)

    def test_auth_error_fails_whole_debate_gracefully(self):
        responses = {
            (role, round_no): LLMError("auth", "LLM 鉴权失败（HTTP 401）")
            for role in self.cards
            for round_no in (1, 2)
        }
        fake = FakeLLM(responses)
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        self.assertEqual(trace["status"], "failed")
        self.assertEqual(trace["fallback_reason"], "all_roles_failed")
        self.assertTrue(trace["research_consensus"]["degraded"])
        self.assertEqual(trace["verdict"], "NO_TRADE")

    def test_single_role_timeout_is_contained(self):
        responses = full_responses(self.cards)
        responses[("data_officer", 1)] = LLMError("timeout", "LLM 请求超时")
        fake = FakeLLM(responses)
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False
        )

        entry = next(
            e
            for e in trace["rounds"][0]["entries"]
            if e["role"] == "data_officer"
        )
        self.assertEqual(entry["status"], "timeout")
        self.assertEqual(trace["status"], "degraded")
        # 其余角色不受影响
        ok_roles = [
            e["role"]
            for e in trace["rounds"][0]["entries"]
            if e["status"] == "ok"
        ]
        self.assertEqual(len(ok_roles), 9)

    def test_deadline_budget_skips_roles_without_calls(self):
        fake = FakeLLM(full_responses(self.cards))
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False, deadline_s=0.0
        )

        self.assertEqual(fake.calls, [])
        for entry in trace["rounds"][0]["entries"]:
            self.assertEqual(entry["status"], "skipped")
        self.assertEqual(trace["verdict"], "NO_TRADE")


class AuditTests(DebateRuntimeBase):
    def test_audit_events_and_secret_redaction(self):
        responses = full_responses(self.cards)
        responses[("data_officer", 1)] = analyst(
            "data_officer",
            1,
            conclusion=f"我引用了密钥 {API_KEY} 来验证快照。",
        )
        fake = FakeLLM(responses)
        sink = RecordingSink()
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=True, audit_sink=sink
        )

        events = [event for event, _ in sink.events]
        self.assertIn("debate_started", events)
        self.assertIn("debate_consensus", events)
        self.assertTrue(any(e.startswith("agent_output:") for e in events))

        officer_events = [
            payload
            for event, payload in sink.events
            if event == "agent_output:data_officer"
        ]
        self.assertTrue(officer_events)
        self.assertIn("[REDACTED]", officer_events[0]["conclusion"])
        self.assertNotIn(API_KEY, officer_events[0]["conclusion"])

        # trace 里的审计引用与 sink 一致，含哈希字段
        self.assertGreaterEqual(len(trace["audit_refs"]), 2)
        self.assertIn("hash", trace["audit_refs"][0])
        self.assertEqual(trace["metrics"]["audit_errors"], 0)

    def test_audit_disabled_never_calls_sink(self):
        fake = FakeLLM(full_responses(self.cards))
        sink = RecordingSink()
        trace = run_debate(
            SCENARIO, self.snapshot, client=fake, audit_enabled=False, audit_sink=sink
        )

        self.assertEqual(sink.events, [])
        self.assertEqual(trace["audit_refs"], [])


if __name__ == "__main__":
    unittest.main()
