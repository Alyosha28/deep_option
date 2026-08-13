"""GOAI 期权智能终端 - 本地 UI 服务。

把 `ui/` 四面板终端升级为可运行产品入口：
- 静态托管 `ui/index.html` + `styles.css` + `app.js`（零依赖、无构建步骤）；
- 只读 JSON 接口把真实产物喂给前端：决策卡、投研证据、宏观研判、
  政策事件库与来源健康报告；
- `POST /api/run` 用冻结快照重跑五阶段管线并返回新决策卡
  （默认写审计与决策卡文件；`?no_audit=1` 仅用于离线测试）。

铁律：本服务不提供下单入口；所有数字来自冻结快照与自研引擎；
只绑定本机回环地址，不对外网开放。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from src.agents.llm_client import create_client, load_settings
from src.agents.runtime import run_debate
from src.decision_pipeline import (
    DEFAULT_INPUT,
    compute_engine,
    load_frozen_snapshot,
    run_pipeline,
)
from src.macro_assessment import DEFAULT_ITEMS, DEFAULT_POLICY_DIR
from src.policy_library import load_policy_library, policy_health_report
from src.scenario_parser import parse_message

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_FINDING_KIND = re.compile(r"^(PASS|WARN|BLOCK|FAIL|NOTE)\s+")
_UNSET = object()


def llm_badge() -> dict[str, Any]:
    """顶栏 LLM 状态徽章：有 key 显示 DeepSeek，无 key 显示离线回退。"""

    settings = load_settings()
    if "deepseek" in settings.base_url.lower():
        provider = "DeepSeek"
    else:
        provider = "OpenAI-compatible"
    return {
        "available": settings.configured,
        "provider": provider,
        "model": settings.chat_model,
        "status": "deepseek" if settings.configured else "offline",
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_decision_card() -> dict[str, Any] | None:
    candidates = sorted(ROOT.glob("data/decision_card_*.json"))
    if not candidates:
        return None
    try:
        return _load_json(candidates[-1])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _leg_ui(leg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": leg["code"],
        "strike": leg["strike"],
        "last": leg.get("last"),
        "mid": leg.get("mid"),
        "bid": leg.get("bid"),
        "ask": leg.get("ask"),
        "apiIvPct": leg.get("api_iv_pct"),
        "openInterest": leg.get("open_interest"),
        "netOpenInterest": leg.get("net_open_interest"),
        "volume": leg.get("volume"),
    }


def _expiries_ui(
    payload: Mapping[str, Any],
    engine: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把快照 legs 与引擎结果拼成前端需要的到期/策略结构。"""

    primary_expiry = engine["primary"]["expiry"]
    expiries: list[dict[str, Any]] = []
    for leg in payload["legs"]:
        item = (
            engine["primary"]
            if leg["expiry"] == primary_expiry
            else engine["secondary"]
        )
        proposal_leg = next(
            candidate
            for candidate in engine["proposal"]["legs"]
            if candidate["expiry"] == leg["expiry"]
        )
        expiries.append(
            {
                "expiry": leg["expiry"],
                "dte": leg["dte"],
                "primary": leg["expiry"] == primary_expiry,
                "strike": item["strike"],
                "call": _leg_ui(leg["call"]),
                "put": _leg_ui(leg["put"]),
                "strategy": {
                    "lots": item["lots"],
                    "costPerLotAsk": item["cost_lot_ask"],
                    "costPerLotExec": item["cost_lot_exec"],
                    "maxLoss": item["max_loss_exec"],
                    "breakeven": [item["breakeven_low"], item["breakeven_high"]],
                    "greeks": dict(item["straddle_greeks"]),
                    "pnlAtExpiry": proposal_leg.get("pnl_at_expiry"),
                    "ivCrush": proposal_leg.get("pnl_after_iv_crush"),
                },
            }
        )
    return expiries


def _findings_ui(findings: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for finding in findings:
        match = _FINDING_KIND.match(str(finding))
        if match:
            rows.append(
                {"kind": match.group(1), "text": str(finding)[match.end():].strip()}
            )
        else:
            rows.append({"kind": "NOTE", "text": str(finding)})
    return rows


def _card_ui(card: Mapping[str, Any]) -> dict[str, Any]:
    saved = _latest_decision_card()
    audit_trail = []
    if isinstance(saved, dict):
        audit_trail = [
            {
                "event": item.get("event", ""),
                "hash": str(item.get("hash", "")),
            }
            for item in saved.get("audit_refs", [])
        ]
    return {
        "verdict": card["verdict"],
        "summary": card["summary"],
        "keyEvidence": card["key_evidence"],
        "edgeGate": card["edge_gate"],
        "riskGate": {
            "decision": card["risk_gate"]["decision"],
            "blocked": card["risk_gate"]["blocked"],
            "findings": _findings_ui(card["risk_gate"]["findings"]),
        },
        "actionGate": card["action_gate"],
        "conditionsThatChange": card["conditions_that_change"],
        "auditTrail": audit_trail,
    }


def _policy_library_ui() -> dict[str, Any]:
    library = load_policy_library(DEFAULT_POLICY_DIR)
    return {
        "path": library["path"],
        "eventCount": library["event_count"],
        "events": [
            {
                "id": event["id"],
                "name": event["name"],
                "date": event["date"],
                "type": event["type"],
                "status": event["status"],
                "verdictReads": event.get("verdict_reads", []),
            }
            for event in library["events"]
        ],
        "health": policy_health_report(library),
    }


def compose_state(card: Mapping[str, Any]) -> dict[str, Any]:
    """把管线输出组合成前端可直接渲染的状态（所有数字来自快照/引擎）。"""

    data = load_frozen_snapshot(DEFAULT_INPUT)
    payload = data["payload"]
    engine = compute_engine(data)
    earnings = payload["earnings"]
    return {
        "meta": {
            "product": "GOAI 港美股期权智能终端",
            "mode": data["mode"],
            "freshness": data["freshness"],
            "origin": data["origin"],
            "capturedAt": data["captured_at"],
            "source": data["source"],
            "marketState": payload.get("market_state", ""),
            "snapshotSha256": data["snapshot_sha256"],
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "underlying": {
            "code": payload["underlying"],
            "name": payload.get("name", ""),
            "spot": payload["spot"],
            "prevClose": payload.get("prev_close"),
        },
        "earnings": {
            "date": earnings["date"],
            "quarter": earnings.get("quarter", ""),
            "expectedMovePct": earnings["expected_move_pct"],
            "iv": earnings["iv"],
            "ivRank": earnings["iv_rank"],
            "ivPercentile": earnings["iv_percentile"],
            "hv30d": earnings.get("hv_30d"),
            "lastReportIvCrush": earnings.get("last_report_iv_crush"),
            "historyReportIvCrush": earnings.get("history_report_iv_crush"),
            "estimateEpsYoy": earnings.get("estimate_eps_yoy"),
            "estimateRevenueYoy": earnings.get("estimate_revenue_yoy"),
        },
        "account": {
            "cashHkd": payload["account"]["cash_hkd"],
            "riskBudgetPct": payload["account"]["risk_budget_pct"],
            "contractMultiplier": payload["account"]["contract_multiplier"],
        },
        "scenario": card["scenario"],
        "expiries": _expiries_ui(payload, engine),
        "decisionCard": _card_ui(card),
        "macro": card["macro_assessment"],
        "policyLibrary": _policy_library_ui(),
        "research": card["research_evidence"],
        "debateTrace": None,
        "researchConsensus": None,
        "llm": llm_badge(),
    }


def run_chat(
    message: str,
    snapshot: Mapping[str, Any],
    *,
    audit_enabled: bool = True,
    debate_client: Any = _UNSET,
) -> dict[str, Any]:
    """对话链路：确定性场景解析 → 五阶段管线 → 十角色辩论（有 key 时）。

    辩论只附加文字结论与证据引用；``/api/chat`` 的 verdict 与全部数字仍来自
    ``run_pipeline``，无 key / 网络失败 / 超时自动回退确定性路径。
    """

    parsed_scenario = parse_message(message, snapshot["payload"])
    card = run_pipeline(
        DEFAULT_INPUT,
        scenario=parsed_scenario["scenario"],
        research_items_path=DEFAULT_ITEMS,
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=audit_enabled,
        write_card=audit_enabled,
    )
    state = compose_state(card)
    state["chat"] = {
        "scenario": parsed_scenario["scenario"],
        "assumed": parsed_scenario["assumed"],
        "notes": parsed_scenario["notes"],
    }
    client = create_client() if debate_client is _UNSET else debate_client
    trace = run_debate(
        parsed_scenario["scenario"],
        dict(snapshot),
        client=client,
        user_text=message,
        audit_enabled=audit_enabled,
    )
    state["debateTrace"] = trace
    state["researchConsensus"] = trace.get("research_consensus")
    state["llm"] = llm_badge()
    return state


def build_state() -> dict[str, Any]:
    """默认只读状态：用冻结快照在内存重跑管线，不写审计与决策卡文件。"""

    card = run_pipeline(
        DEFAULT_INPUT,
        research_items_path=DEFAULT_ITEMS,
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=False,
        write_card=False,
    )
    return compose_state(card)


class Handler(BaseHTTPRequestHandler):
    server_version = "GOAI-UI/1.0"

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, exc: Exception) -> None:
        self._send_json({"error": str(exc)}, status=500)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _send_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        relative = path.lstrip("/")
        target = (UI_DIR / relative).resolve()
        try:
            target.relative_to(UI_DIR.resolve())
        except ValueError:
            self._send_json({"error": "not found"}, status=404)
            return
        if not target.is_file() or target.suffix.lower() not in CONTENT_TYPES:
            self._send_json({"error": "not found"}, status=404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[target.suffix.lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/state":
                self._send_json(build_state())
                return
            if parsed.path == "/api/policy-library":
                self._send_json(_policy_library_ui())
                return
            self._send_static(parsed.path)
        except Exception as exc:  # 服务端错误不外泄堆栈
            self._send_error_json(exc)

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler naming)
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/run", "/api/chat"):
            self._send_json({"error": "not found"}, status=404)
            return
        params = parse_qs(parsed.query)
        no_audit = params.get("no_audit", ["0"])[0] in ("1", "true")
        try:
            if parsed.path == "/api/chat":
                body = self._read_json_body()
                message = str(body.get("message", "")).strip()
                if not message:
                    self._send_json({"error": "message 不能为空"}, status=400)
                    return
                snapshot = load_frozen_snapshot(DEFAULT_INPUT)
                state = run_chat(
                    message,
                    snapshot,
                    audit_enabled=not no_audit,
                )
                self._send_json(state)
                return
            card = run_pipeline(
                DEFAULT_INPUT,
                research_items_path=DEFAULT_ITEMS,
                macro_policy_path=DEFAULT_POLICY_DIR,
                audit_enabled=not no_audit,
                write_card=not no_audit,
            )
            self._send_json(compose_state(card))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=422)
        except Exception as exc:
            self._send_error_json(exc)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI 期权智能终端本地 UI 服务")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="端口")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 78)
    print("GOAI 期权智能终端 UI")
    print(f"打开：{url}")
    print("API：/api/state（状态）、/api/policy-library（事件库）、POST /api/run（重跑管线）、")
    print("     POST /api/chat（场景解析 + 五阶段管线 + 十角色辩论；无 DeepSeek key 自动离线回退）")
    print("免责声明：决策支持/研究用途，非投资建议；默认模拟盘，任何订单须人机确认。")
    print("=" * 78)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
