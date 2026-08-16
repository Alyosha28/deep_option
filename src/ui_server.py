"""GOAI 期权智能终端 - 本地 UI 服务。

把 `ui/` 桌面终端界面作为可运行产品入口：
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
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from src.agents.llm_client import create_client, load_settings, parse_dotenv
from src.agents.runtime import run_debate
from src.decision_pipeline import (
    compute_engine,
    load_frozen_snapshot,
    parse_scenario,
    run_pipeline,
)
from src.futu_adapter import FutuLiveGateway
from src.live_data import (
    LiveDataError,
    LiveDataService,
    live_template_codes,
    quote_rows_to_payload,
    utc_now_iso,
)
from src.live_stream import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_MAX_SUBSCRIBERS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PUSH_SILENCE_SECONDS,
    LiveStreamService,
    StreamCapacityError,
)
from src.gateway import (
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    normalize_symbol as normalize_security_code,
)
from src.macro_assessment import DEFAULT_ITEMS, DEFAULT_POLICY_DIR
from src.openbb_adapter import configured_provider, fetch_historical
from src.policy_library import load_policy_library, policy_health_report
from src.scenario_parser import parse_message
from src.workspace_registry import (
    discover_project_assets,
    get_project,
    list_projects,
    load_registry,
    normalize_symbol,
    register_project,
)

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
_ACTIVE_PROJECT_ID: str | None = None
_ACTIVE_PROJECT_LOCK = threading.Lock()


def _runtime_env(name: str, default: str = "") -> str:
    """Read a runtime switch from the process first, then the local .env."""

    process_value = os.getenv(name)
    if process_value is not None:
        return process_value
    return parse_dotenv(ROOT / ".env").get(name, default)


def _parse_data_mode(value: str) -> DataMode:
    raw = value.strip().lower()
    if raw in ("live", "1", "true", "yes", "on"):
        return DataMode.LIVE
    if raw in ("", "replay", "0", "false", "no", "off"):
        return DataMode.REPLAY
    raise ValueError("GOAI_DATA_MODE 必须是 live 或 replay")


def _data_mode(project: Mapping[str, Any] | None = None) -> DataMode:
    """Resolve the effective data mode: env switch first, project record second.

    ``GOAI_DATA_MODE`` is the operator-level switch (default replay so the demo
    stays reproducible).  When it is absent, a project record may still declare
    ``data_mode``; otherwise the behaviour stays on the frozen-snapshot path.
    """

    configured = _runtime_env("GOAI_DATA_MODE")
    if configured.strip():
        return _parse_data_mode(configured)
    if project is not None:
        return _parse_data_mode(str(project.get("data_mode", "replay")))
    return DataMode.REPLAY


def _state_cache_ttl_seconds(mode: DataMode | None = None) -> float:
    """State cache TTL by mode: live data changes quickly, replay is frozen."""

    if (mode or _data_mode()) is DataMode.LIVE:
        return _STATE_CACHE_TTL_LIVE_SECONDS
    return _STATE_CACHE_TTL_SECONDS


def _workspace_registry_path() -> Path:
    configured = _runtime_env("GOAI_WORKSPACE_PATH").strip()
    if not configured:
        return ROOT / "data" / "workspaces.json"
    path = Path(configured)
    return path if path.is_absolute() else ROOT / path


def _workspace_registry() -> dict[str, Any]:
    return load_registry(_workspace_registry_path(), root=ROOT)


def _workspace_project(project_id: str | None = None) -> dict[str, Any]:
    registry = _workspace_registry()
    with _ACTIVE_PROJECT_LOCK:
        requested = project_id or _ACTIVE_PROJECT_ID or registry["active_project_id"]
    return get_project(registry, requested)


def _set_active_project(project_id: str) -> dict[str, Any]:
    project = _workspace_project(project_id)
    global _ACTIVE_PROJECT_ID
    with _ACTIVE_PROJECT_LOCK:
        _ACTIVE_PROJECT_ID = project["id"]
    invalidate_state_cache()
    return project


def _workspace_ui(project: Mapping[str, Any]) -> dict[str, Any]:
    registry = _workspace_registry()
    return {
        "activeProjectId": project["id"],
        "projects": list_projects(registry, active_project_id=project["id"]),
        "registryPath": "data/workspaces.json",
    }


def _research_items_path(project: Mapping[str, Any] | None = None) -> Path:
    """Resolve the operator-selected canonical research feed.

    The UI never accepts a file path from a browser request.  This is a local
    process setting so a researcher can switch from the demo fixture to a
    verified news/filing/research export without changing Python code.
    """

    configured = _runtime_env("GOAI_RESEARCH_ITEMS_PATH").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else ROOT / path
    if project is not None:
        return Path(project["research_items_path"])
    return DEFAULT_ITEMS


def _project_for_command(command: str) -> dict[str, Any] | None:
    """把 ``CODE MARKET <GO>`` / ``MARKET.CODE <GO>`` 指令映射到项目。"""

    normalized = re.sub(r"\s+", " ", str(command).strip().upper())
    normalized = re.sub(r"\s*<GO>\s*$", "", normalized)
    compact = re.sub(r"[^A-Z0-9]", "", normalized)
    try:
        registry = _workspace_registry()
    except ValueError:
        return None
    for project in registry["projects"]:
        market, code = project["symbol"].split(".", 1)
        short_code = code[-4:] if len(code) > 4 else code
        candidates = {
            re.sub(r"[^A-Z0-9]", "", project["symbol"]),
            f"{code}{market}",
            f"{market}{code}",
            code,
            f"{short_code}{market}",
            f"{market}{short_code}",
            short_code,
        }
        if compact in candidates:
            return dict(project)
    return None


# The production live-data service lives in src.live_data.  ui_server only
# keeps the HTTP-facing error alias and the quote TTL so route handlers can
# map typed live failures to the same JSON contract without duplicating the
# service implementation.
_LiveDataError = LiveDataError

_LIVE_QUOTE_TTL_SECONDS = 3.0

# ---- Live stream (SSE) configuration -------------------------------------
# 第二阶段：/api/stream 服务端推送。轮询间隔与推送静默阈值可用环境变量
# 覆盖（GOAI_LIVE_STREAM_POLL_SECONDS / GOAI_LIVE_STREAM_PUSH_SILENCE_SECONDS），
# GOAI_LIVE_FEED=push 启用真实 OpenD 订阅推送（失败/静默自动回退轮询）。
_STREAM_HEARTBEAT_SECONDS = DEFAULT_HEARTBEAT_SECONDS
_STREAM_RETRY_MS = 3000
_STREAM_MAX_CODES = 32
_STREAM_MAX_SUBSCRIBERS = DEFAULT_MAX_SUBSCRIBERS


def _parse_env_float(name: str, default: float) -> float:
    try:
        value = float(_runtime_env(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _parse_env_int(name: str, default: int) -> int:
    try:
        value = int(_runtime_env(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _stream_push_connector() -> Any | None:
    """真实 OpenD 订阅推送入口（gateway 可选能力，缺省 None → 轮询源）。"""
    gateway = _get_live_data_service().gateway
    connector = getattr(gateway, "start_quote_push", None)
    return connector if callable(connector) else None


def _stream_push_row_wrapper(
    codes: list[str], rows: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """把 SDK 推送的 snake_case 行包装成 /api/live-quote 同形 payload。"""
    return quote_rows_to_payload(
        codes,
        rows,
        captured_at=utc_now_iso(),
        freshness="FRESH",
        strict=False,
    )


_LIVE_STREAM_SERVICE: LiveStreamService | None = None
_LIVE_STREAM_LOCK = threading.Lock()


def _get_live_stream_service() -> LiveStreamService:
    global _LIVE_STREAM_SERVICE
    with _LIVE_STREAM_LOCK:
        if _LIVE_STREAM_SERVICE is None:
            use_push = _runtime_env("GOAI_LIVE_FEED", "poll").strip().lower() in {
                "push",
                "1",
                "true",
                "yes",
                "on",
            }
            service = _get_live_data_service()
            _LIVE_STREAM_SERVICE = LiveStreamService(
                service.quote_payload,
                poll_interval=_parse_env_float(
                    "GOAI_LIVE_STREAM_POLL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
                ),
                max_subscribers=_parse_env_int(
                    "GOAI_LIVE_STREAM_MAX_SUBSCRIBERS", _STREAM_MAX_SUBSCRIBERS
                ),
                push_connector=_stream_push_connector() if use_push else None,
                push_silence_seconds=_parse_env_float(
                    "GOAI_LIVE_STREAM_PUSH_SILENCE_SECONDS", DEFAULT_PUSH_SILENCE_SECONDS
                ),
                row_wrapper=_stream_push_row_wrapper,
            )
        return _LIVE_STREAM_SERVICE


def _close_live_stream() -> None:
    """停止 SSE 服务（测试与服务器关停用；可重复调用）。"""
    global _LIVE_STREAM_SERVICE
    with _LIVE_STREAM_LOCK:
        service = _LIVE_STREAM_SERVICE
        _LIVE_STREAM_SERVICE = None
    if service is not None:
        service.close()


def _notify_stream_refresh() -> None:
    """POST 重跑/对话后广播 ``refresh``，让 LIVE 前端立即重拉 state。"""
    service = _LIVE_STREAM_SERVICE
    if service is None:
        return
    try:
        service.hub.publish("refresh", {"reason": "state_rebuilt"})
    except Exception:
        pass


def _stream_default_codes(project: Mapping[str, Any]) -> list[str]:
    """默认订阅集 = 当前项目 live 模板的 underlying + 全部期权腿。"""
    try:
        return live_template_codes(Path(project["input_path"]))
    except _LiveDataError:
        return [normalize_security_code(str(project.get("symbol") or ""), "underlying")]


def _create_live_gateway() -> Any:
    return FutuLiveGateway()


_LIVE_GATEWAY_FACTORY: Callable[[], Any] = _create_live_gateway
_LIVE_DATA_SERVICE: LiveDataService | None = None
_LIVE_DATA_SERVICE_LOCK = threading.Lock()


def _get_live_data_service() -> LiveDataService:
    global _LIVE_DATA_SERVICE
    with _LIVE_DATA_SERVICE_LOCK:
        if _LIVE_DATA_SERVICE is None:
            _LIVE_DATA_SERVICE = LiveDataService(
                _LIVE_GATEWAY_FACTORY(),
                quote_ttl=_LIVE_QUOTE_TTL_SECONDS,
                option_ttl=_LIVE_QUOTE_TTL_SECONDS,
            )
        return _LIVE_DATA_SERVICE


def _build_live_snapshot_for_project(
    project: Mapping[str, Any],
    *,
    force: bool,
) -> dict[str, Any]:
    """Build a LIVE snapshot for a workspace project through src.live_data."""

    return _get_live_data_service().build_live_snapshot(
        Path(project["input_path"]),
        force=force,
    )


def _reset_live_data_service() -> None:
    """Drop the cached live service (tests inject a fresh fake gateway)."""

    global _LIVE_DATA_SERVICE
    with _LIVE_DATA_SERVICE_LOCK:
        _LIVE_DATA_SERVICE = None


def _project_symbol_from_text(value: str | None) -> str | None:
    """从自然语言中提取通用 ``MARKET.CODE`` / 显示格式标的代码。"""

    raw_text = str(value or "").strip()
    text = raw_text.upper()
    canonical = re.search(
        r"(?<![A-Z0-9_])([A-Z][A-Z0-9_-]{1,15}\.[A-Z0-9][A-Z0-9._-]{0,46})"
        r"(?![A-Z0-9._-])",
        text,
    )
    if canonical:
        try:
            return normalize_symbol(canonical.group(1))
        except ValueError:
            pass
    display = re.search(r"\b([A-Z0-9][A-Z0-9._-]*)\s+(HK|US)\b", text)
    if not display:
        display = re.search(r"\b(HK|US)\s+([A-Z0-9][A-Z0-9._-]*)\b", text)
        if display:
            market, code = display.group(1), display.group(2)
        else:
            market = code = ""
    else:
        code, market = display.group(1), display.group(2)
    if not market or not code:
        numeric_display = re.search(
            r"(?<![A-Z0-9._-])(\d{4,8})\s+([A-Z][A-Z0-9_-]{1,15})"
            r"(?![A-Z0-9._-])",
            text,
        )
        if numeric_display:
            code, market = numeric_display.group(1), numeric_display.group(2)
        else:
            numeric_display = re.search(
                r"(?<![A-Z0-9._-])([A-Z][A-Z0-9_-]{1,15})\s+(\d{4,8})"
                r"(?![A-Z0-9._-])",
                text,
            )
            if numeric_display:
                market, code = numeric_display.group(1), numeric_display.group(2)
    if not market or not code:
        # A two-token command such as ``NASDAQ AAPL`` is unambiguous enough
        # when entered as a standalone command.  In a sentence, avoid turning
        # arbitrary company names such as ``Berkshire Hathaway`` into symbols;
        # those are resolved by the asset-name discovery path instead.
        display_tokens = re.findall(r"[A-Z0-9][A-Z0-9._-]*", text)
        raw_display_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw_text)
        if (
            len(display_tokens) == 2
            and len(raw_display_tokens) == 2
            and all(token == token.upper() for token in raw_display_tokens)
            and all(len(token) <= 8 for token in display_tokens)
            and not any(token in {"THE", "AND", "FOR", "WITH"} for token in display_tokens)
        ):
            market, code = display_tokens
    if not market or not code:
        return None
    if market == "HK" and code.isdigit() and len(code) < 5:
        code = code.zfill(5)
    try:
        return normalize_symbol(f"{market}.{code}")
    except ValueError:
        return None


def _agent_discovery_signal(message: str) -> bool:
    """识别需要先找标的资产的请求，避免直接把它当成当前项目场景。"""

    text = str(message or "").lower()
    verbs = (
        "找",
        "搜索",
        "发现",
        "导入",
        "添加",
        "加入",
        "扫描",
        "定位",
        "find",
        "search",
        "discover",
        "import",
        "add",
    )
    assets = (
        "快照",
        "snapshot",
        "投研资料",
        "研究资料",
        "研究文件",
        "research",
        "项目",
        "工作区",
    )
    has_verb = any(verb in text for verb in verbs)
    has_asset = any(
        asset in text for asset in assets
    )
    if has_verb and has_asset:
        generic_research_asset = any(
            asset in text
            for asset in ("投研资料", "研究资料", "研究文件", "research")
        ) and not any(asset in text for asset in ("快照", "snapshot", "项目", "工作区"))
        has_explicit_symbol = _project_symbol_from_text(message) is not None
        has_company_marker = any(
            marker in text for marker in ("公司", "股票", "个股", "标的")
        )
        if not generic_research_asset or has_explicit_symbol or has_company_marker:
            return True
    if has_verb and any(
        marker in text
        for marker in ("另一家公司", "其他公司", "别的公司", "新标的", "另一个标的")
    ):
        return True

    research_intent = any(
        token in text
        for token in (
            "研究",
            "分析",
            "研判",
            "评估",
            "看看",
            "查看",
            "research",
            "analy",
            "underwrite",
            "study",
            "review",
        )
    )
    finance_context = any(
        token in text
        for token in (
            "股票",
            "个股",
            "标的",
            "证券",
            "公司",
            "期权",
            "期权链",
            "财报",
            "行情",
            "投研",
            "资料",
            "研究资料",
            "研究文件",
            "报告",
            "新闻",
        )
    )
    explicit_symbol = _project_symbol_from_text(message) is not None
    ticker_stopwords = {
        "US",
        "HK",
        "THE",
        "AND",
        "FOR",
        "WITH",
        "THIS",
        "THAT",
        "RESEARCH",
        "ANALYZE",
        "ANALYSIS",
        "STOCK",
        "OPTION",
        "OPTIONS",
        "PLEASE",
        "HELP",
        "LOOK",
        "AT",
        "FIND",
        "SEARCH",
        "DISCOVER",
        "IMPORT",
        "ADD",
        "STUDY",
        "REVIEW",
        "UNDERWRITE",
        "OPPORTUNITY",
        "OPPORTUNITIES",
        "COMPANY",
        "EQUITY",
        "SHARE",
        "SHARES",
        "WHY",
        "NO",
        "TRADE",
        "CURRENT",
        "CONCLUSION",
        "RISK",
        "BUDGET",
        "OPEN",
        "CLOSE",
        "CHAIN",
        "REASON",
        "WHAT",
        "HOW",
        "CAN",
        "YOU",
        "ME",
    }
    has_plain_ticker = any(
        token not in ticker_stopwords
        for token in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", str(message).upper())
    )
    numeric_codes = re.findall(r"\b\d{4,8}\b", str(message))
    has_numeric_code = any(
        re.search(
            rf"(?:股票|个股|标的|公司|证券|期权|研究|分析).{{0,12}}{re.escape(code)}"
            rf"|{re.escape(code)}.{{0,12}}(?:股票|个股|标的|公司|证券|期权)",
            text,
        )
        or (
            research_intent
            and not any(
                marker in text
                for marker in ("现金", "账户", "资金", "预算", "亏损", "港币", "美元", "hkd", "usd")
            )
        )
        for code in numeric_codes
    )
    has_english_company = any(
        token not in ticker_stopwords
        for token in re.findall(r"\b[A-Z][A-Z0-9&-]{2,31}\b", str(message).upper())
    )
    company_text = text
    for token in (
        "研究",
        "投研",
        "资料",
        "研究文件",
        "研究资料",
        "报告",
        "新闻",
        "分析",
        "研判",
        "评估",
        "看看",
        "查看",
        "打开",
        "关闭",
        "展开",
        "收起",
        "切换",
        "跳转",
        "显示",
        "进入",
        "页面",
        "面板",
        "一下",
        "请",
        "帮我",
        "公司",
        "股票",
        "个股",
        "标的",
        "期权链",
        "期权",
        "财报",
        "行情",
        "机会",
        "当前",
        "这个",
        "的",
        "和",
        "与",
    ):
        company_text = company_text.replace(token, " ")
    has_chinese_company = any(
        len(token) >= 2 for token in re.findall(r"[\u4e00-\u9fff]+", company_text)
    )
    target_hint = (
        explicit_symbol
        or has_plain_ticker
        or has_numeric_code
        or has_english_company
        or has_chinese_company
    )
    return (research_intent and target_hint) or (target_hint and finance_context)


def _agent_project_request(body: Mapping[str, Any]) -> tuple[str | None, str]:
    message = str(body.get("message") or body.get("query") or "").strip()
    query = str(body.get("query") or message).strip()
    symbol = _project_symbol_from_text(str(body.get("symbol") or ""))
    return symbol or _project_symbol_from_text(query), query


def _research_items_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


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


def _decision_card_export() -> dict[str, Any]:
    """GET /api/decision-card：导出最新落盘决策卡（只读，不写审计）。

    返回文件相对路径与 SHA-256 身份哈希，供 UI「导出决策卡」按钮使用；
    哈希由引擎侧计算，前端不重算任何数字。
    """
    candidates = sorted(ROOT.glob("data/decision_card_*.json"))
    if not candidates:
        return {"found": False, "error": "尚未生成决策卡（先运行 POST /api/run 或 /api/chat）"}
    path = candidates[-1]
    try:
        card = _load_json(path)
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"found": False, "error": "最新决策卡文件不可读"}
    return {
        "found": True,
        "path": str(path.relative_to(ROOT)),
        "sha256": sha256,
        "card": card,
    }


AUDIT_LOG = ROOT / "research" / "audit" / "audit_log.jsonl"


def _redact(text: str) -> str:
    """审计视图脱敏：仓库绝对路径不落到前端。"""
    return text.replace(str(ROOT), "<repo>")


def _audit_entry_summary(event: str, payload: Mapping[str, Any]) -> str:
    """每类审计事件给一行紧凑摘要；只提取安全的小字段，不展开巨 payload。"""
    p = payload if isinstance(payload, Mapping) else {}
    if event.startswith("agent_output:"):
        parts = [
            str(p.get("role") or event.split(":", 1)[-1]),
            str(p.get("status") or ""),
        ]
        if p.get("stance") is not None:
            parts.append("stance=" + str(p["stance"]))
        if p.get("confidence") is not None:
            parts.append("conf=" + str(p["confidence"]))
        conclusion = str(p.get("conclusion") or "").strip()
        if conclusion:
            parts.append(conclusion[:120])
        return _redact(" | ".join(part for part in parts if part))
    if event == "debate_consensus":
        consensus = p.get("consensus") if isinstance(p.get("consensus"), Mapping) else {}
        parts = [
            "verdict=" + str(p.get("verdict") or ""),
            "stance=" + str(consensus.get("stance") or ""),
            "conf=" + str(consensus.get("confidence") or ""),
        ]
        summary = str(consensus.get("summary") or "").strip()
        if summary:
            parts.append(summary[:160])
        return _redact(" | ".join(part for part in parts if part))
    if event == "scenario_parsed":
        return _redact(
            "view={} horizon={} cash={} risk={}%".format(
                p.get("view") or "-",
                p.get("horizon") or "-",
                p.get("account_hkd") or p.get("account_cash_hkd") or "-",
                p.get("risk_budget_pct") or "-",
            )
        )
    if event in ("edge_gate", "risk_gate", "action_gate", "decision_card"):
        verdict = p.get("verdict") or p.get("decision") or p.get("action") or "-"
        return _redact("{} verdict={}".format(event, verdict))
    keys = ", ".join(str(k) for k in list(p.keys())[:6])
    return _redact("{}: {}".format(event, keys or "(无 payload)"))


def _audit_view(limit: int = 60) -> dict[str, Any]:
    """GET /api/audit：只读审计视图（不写审计）。

    全链校验 prev_hash 衔接（首条 prev_hash 为空视为合法起点），
    返回尾部 limit 条投影：seq / ts / event / summary / dropped_refs / hash 前缀。
    """
    limit = max(1, min(int(limit), 200))
    if not AUDIT_LOG.is_file():
        return {"found": False, "error": "审计日志不存在", "entries": [], "chainOk": False, "total": 0}
    entries: list[dict[str, Any]] = []
    chain_ok = True
    prev_hash: str | None = None
    total = 0
    try:
        with AUDIT_LOG.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    chain_ok = False
                    continue
                rec_hash = str(rec.get("hash") or "")
                rec_prev = str(rec.get("prev_hash") or "")
                if prev_hash is not None and rec_prev != prev_hash:
                    chain_ok = False
                prev_hash = rec_hash
                payload = rec.get("payload") if isinstance(rec.get("payload"), Mapping) else {}
                dropped = payload.get("dropped_refs")
                entries.append(
                    {
                        "seq": total,
                        "ts": str(rec.get("ts") or ""),
                        "event": str(rec.get("event") or ""),
                        "summary": _audit_entry_summary(str(rec.get("event") or ""), payload),
                        "droppedRefs": list(dropped) if isinstance(dropped, list) and dropped else [],
                        "prevHash": rec_prev[:12],
                        "hash": rec_hash[:12],
                    }
                )
    except OSError:
        return {"found": False, "error": "审计日志不可读", "entries": [], "chainOk": False, "total": total}
    return {
        "found": True,
        "path": str(AUDIT_LOG.relative_to(ROOT)),
        "total": total,
        "chainOk": chain_ok,
        "entries": entries[-limit:],
    }


METRICS_LOCK = threading.Lock()
METRICS_LOG = ROOT / "data" / "logs" / "session_metrics.jsonl"


def _record_session_metric(event: str, text: str, verdict: Any, duration_ms: float, mode: Any) -> None:
    """本地会话度量（独立于审计链，不写审计）：JSONL 追加，失败静默不打断主流程。"""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "input": str(text or "")[:200],
        "verdict": str(verdict) if verdict is not None else "",
        "duration_ms": int(duration_ms or 0),
        "mode": str(mode or ""),
    }
    try:
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_LOCK, METRICS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _metrics_view(limit: int = 100) -> dict[str, Any]:
    """GET /api/metrics：只读会话度量（最近 limit 条 + 事件/verdict 统计）。"""
    limit = max(1, min(int(limit), 500))
    if not METRICS_LOG.is_file():
        return {"found": False, "error": "尚无会话度量记录", "entries": [], "stats": {}, "total": 0}
    entries: list[dict[str, Any]] = []
    total = 0
    try:
        with METRICS_LOG.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(
                    {
                        "ts": str(rec.get("ts") or ""),
                        "event": str(rec.get("event") or ""),
                        "input": str(rec.get("input") or "")[:200],
                        "verdict": str(rec.get("verdict") or ""),
                        "durationMs": int(rec.get("duration_ms") or 0),
                        "mode": str(rec.get("mode") or ""),
                    }
                )
    except OSError:
        return {"found": False, "error": "度量日志不可读", "entries": [], "stats": {}, "total": total}
    try:
        rel_path = str(METRICS_LOG.relative_to(ROOT))
    except ValueError:
        rel_path = str(METRICS_LOG)
    stats: dict[str, Any] = {"byEvent": {}, "byVerdict": {}, "avgDurationMs": 0}
    if entries:
        for item in entries:
            stats["byEvent"][item["event"]] = stats["byEvent"].get(item["event"], 0) + 1
            if item["verdict"]:
                stats["byVerdict"][item["verdict"]] = stats["byVerdict"].get(item["verdict"], 0) + 1
        stats["avgDurationMs"] = round(sum(item["durationMs"] for item in entries) / len(entries))
    return {
        "found": True,
        "path": rel_path,
        "total": total,
        "stats": stats,
        "entries": entries[-limit:],
    }


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
    """把快照 legs 与引擎结果拼成前端需要的到期/策略结构。

    引擎只为主/次两个到期计算策略；快照中的其余到期只呈现行情与行权价，
    strategy 置空——绝不把主/次到期的数字张冠李戴到其他到期。
    """

    primary_expiry = engine["primary"]["expiry"]
    engine_by_expiry = {
        engine["primary"]["expiry"]: engine["primary"],
        engine["secondary"]["expiry"]: engine["secondary"],
    }
    proposal_by_expiry = {
        candidate["expiry"]: candidate for candidate in engine["proposal"]["legs"]
    }
    expiries: list[dict[str, Any]] = []
    for leg in payload["legs"]:
        item = engine_by_expiry.get(leg["expiry"])
        proposal_leg = proposal_by_expiry.get(leg["expiry"])
        strategy = None
        if item is not None:
            strategy = {
                "lots": item["lots"],
                "costPerLotAsk": item["cost_lot_ask"],
                "costPerLotExec": item["cost_lot_exec"],
                "maxLoss": item["max_loss_exec"],
                "breakeven": [item["breakeven_low"], item["breakeven_high"]],
                "greeks": dict(item["straddle_greeks"]),
                "pnlAtExpiry": (
                    proposal_leg.get("pnl_at_expiry") if proposal_leg else None
                ),
                "ivCrush": (
                    proposal_leg.get("pnl_after_iv_crush") if proposal_leg else None
                ),
            }
        expiries.append(
            {
                "expiry": leg["expiry"],
                "dte": leg["dte"],
                "primary": leg["expiry"] == primary_expiry,
                "strike": leg["call"]["strike"],
                "call": _leg_ui(leg["call"]),
                "put": _leg_ui(leg["put"]),
                "strategy": strategy,
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


def _openbb_history_ui(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Load historical bars when the operator enables the configured provider.

    OpenBB/yfinance is a supplemental history source for price and realized
    volatility views; it never replaces the frozen Futu/OpenD snapshot used by
    the deterministic options engine.
    """

    provider = _runtime_env("GOAI_OPENBB_PROVIDER").strip().lower() or configured_provider()
    disabled = _runtime_env("GOAI_OPENBB_ENABLED", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disabled:
        return {
            "available": False,
            "reason": "disabled",
            "provider": provider or "OpenBB",
            "source": "OpenBB",
            "symbol": payload.get("underlying", ""),
            "points": [],
            "metrics": {},
            "capturedAt": None,
        }

    return fetch_historical(
        str(payload.get("underlying", "")),
        provider=provider,
        start_date=_runtime_env("GOAI_OPENBB_START_DATE") or None,
        end_date=_runtime_env("GOAI_OPENBB_END_DATE") or None,
        interval=_runtime_env("GOAI_OPENBB_INTERVAL", "1d"),
        period=_runtime_env("GOAI_OPENBB_PERIOD", "2y"),
    )


def _terminal_ui(
    payload: Mapping[str, Any],
    card: Mapping[str, Any],
    expiries: list[dict[str, Any]],
    snapshot_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the dense desktop-terminal surface without inventing market data.

    The frontend used to assemble the same facts in several unrelated panels.  The
    terminal view now receives one explicit, read-only model: quote strip, chart
    coverage, ATM chain rows and the inspection rail.  The only chart series we
    expose is the deterministic engine's expiry P/L path; no historical candles
    are manufactured when the frozen snapshot does not contain them.
    """

    earnings = payload["earnings"]
    account = dict(payload["account"])
    scenario = card.get("scenario") or {}
    if scenario.get("account_cash_hkd") is not None:
        account["cash_hkd"] = scenario["account_cash_hkd"]
    if scenario.get("risk_budget_pct") is not None:
        account["risk_budget_pct"] = scenario["risk_budget_pct"]
    spot = float(payload["spot"])
    prev_close = payload.get("prev_close")
    market_prefix = str(payload.get("underlying", "")).split(".", 1)[0].upper()
    currency = {"HK": "HKD", "US": "USD"}.get(market_prefix, market_prefix or "--")
    change_pct = None
    if prev_close not in (None, 0):
        change_pct = (spot - float(prev_close)) / float(prev_close) * 100

    primary = next((item for item in expiries if item.get("primary")), expiries[0])
    strategy = primary.get("strategy") or {}
    chart_points: list[dict[str, Any]] = []
    for group in strategy.get("pnlAtExpiry") or []:
        for row in group.get("rows", []):
            chart_points.append(
                {
                    "spot": row.get("spot"),
                    "pnl": row.get("pnl"),
                    "label": group.get("label", "到期路径"),
                    "direction": row.get("direction"),
                }
            )
    chart_points.sort(key=lambda item: float(item.get("spot", 0)))

    history = _openbb_history_ui(payload)
    price_points = [
        {
            "date": row.get("date"),
            "close": row.get("close"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume"),
        }
        for row in history.get("points", [])
        if row.get("date") is not None and row.get("close") is not None
    ]
    volatility_points = [
        {
            "date": row.get("date"),
            "hv30d": row.get("hv30d"),
        }
        for row in history.get("points", [])
        if row.get("date") is not None and row.get("hv30d") is not None
    ]
    history_metrics = history.get("metrics") or {}
    history_source = history.get("source") or "OpenBB"

    chain_rows: list[dict[str, Any]] = []
    for expiry in expiries:
        call = expiry["call"]
        put = expiry["put"]

        def spread_pct(leg: Mapping[str, Any]) -> float | None:
            bid = leg.get("bid")
            ask = leg.get("ask")
            mid = leg.get("mid")
            if bid is None or ask is None or mid in (None, 0):
                return None
            return (float(ask) - float(bid)) / float(mid) * 100

        chain_rows.append(
            {
                "expiry": expiry["expiry"],
                "dte": expiry["dte"],
                "primary": expiry["primary"],
                "strike": expiry["strike"],
                "call": {
                    "bid": call.get("bid"),
                    "ask": call.get("ask"),
                    "iv": call.get("apiIvPct"),
                    "oi": call.get("openInterest"),
                    "volume": call.get("volume"),
                },
                "put": {
                    "bid": put.get("bid"),
                    "ask": put.get("ask"),
                    "iv": put.get("apiIvPct"),
                    "oi": put.get("openInterest"),
                    "volume": put.get("volume"),
                },
                "callSpreadPct": spread_pct(call),
                "putSpreadPct": spread_pct(put),
            }
        )

    decision = _card_ui(card)
    return {
        "quote": {
            "symbol": payload["underlying"],
            "name": payload.get("name", ""),
            "currency": currency,
            "spot": spot,
            "prevClose": prev_close,
            "changePct": change_pct,
            "marketState": payload.get("market_state", ""),
        },
        "session": {
            "product": "GOAI / 研究终端",
            "mode": (snapshot_data or {}).get("mode", "REPLAY"),
            "capturedAt": (snapshot_data or {}).get(
                "captured_at", payload.get("captured_at")
            ),
            "source": (snapshot_data or {}).get("source", payload.get("source")),
            "freshness": (snapshot_data or {}).get("freshness", "FROZEN"),
            "historySource": (
                f"{history_source} / {history.get('provider')}"
                if history.get("available")
                else "未启用可选历史行情"
            ),
        },
        "event": {
            "date": earnings["date"],
            "label": f"{earnings.get('quarter', '')} 业绩",
            "days": primary.get("dte"),
            "expectedMovePct": earnings.get("expected_move_pct"),
        },
        "quoteMetrics": {
            "iv": earnings.get("iv"),
            "ivRank": earnings.get("iv_rank"),
            "hv30d": earnings.get("hv_30d"),
            "historicalHv30d": history_metrics.get("latestHv30d"),
            "ivPercentile": earnings.get("iv_percentile"),
        },
        "chart": {
            "mode": "pnl",
            "title": "主到期损益路径",
            "points": chart_points,
            "breakeven": strategy.get("breakeven", []),
            "source": "本地计算引擎 / 到期损益",
            "coverage": "当前快照未录制历史行情序列，未绘制价格 K 线。",
            "price": {
                "mode": "price",
                "title": "正股历史价格",
                "points": price_points,
                "source": (
                    f"{history_source} / {history.get('provider')}"
                    if history.get("available")
                    else "可选历史行情 provider"
                ),
                "coverage": (
                    f"历史行情 provider 返回 {len(price_points)} 根日线。"
                    if history.get("available")
                    else "历史行情 provider 未启用或不可用，未虚构价格 K 线。"
                ),
            },
            "volatility": {
                "mode": "volatility",
                "title": "30 日实现波动率",
                "points": volatility_points,
                "source": (
                    f"{history_source} / {history.get('provider')} · 对数收益率年化"
                    if history.get("available")
                    else "可选历史价格 / 对数收益率年化"
                ),
                "coverage": (
                    f"{len(volatility_points)} 个交易日有完整 30 日波动率。"
                    if volatility_points
                    else "需要启用历史价格 provider，且至少有 5 个有效收益率观察值。"
                ),
            },
        },
        "history": history,
        "chain": {
            "rows": chain_rows,
            "coverage": "仅录制 ATM 合约",
            "note": "完整期权链未在本次冻结快照中保存，其他行权价不做推断。",
        },
        "decision": {
            "verdict": decision["verdict"],
            "summary": decision["summary"],
            "edge": decision["edgeGate"],
            "risk": decision["riskGate"],
            "action": decision["actionGate"],
            "conditions": decision["conditionsThatChange"],
            "evidence": decision["keyEvidence"],
        },
        "risk": {
            "cashHkd": account["cash_hkd"],
            "budgetPct": account["risk_budget_pct"],
            "budgetHkd": account["cash_hkd"] * account["risk_budget_pct"] / 100,
            "maxLoss": strategy.get("maxLoss"),
            "contractMultiplier": account.get("contract_multiplier"),
        },
        "selection": {
            "expiry": primary["expiry"],
            "label": "主到期",
        },
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


def compose_state(
    card: Mapping[str, Any],
    *,
    project: Mapping[str, Any] | None = None,
    input_path: str | Path | None = None,
    snapshot_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把管线输出组合成前端可直接渲染的状态（所有数字来自快照/引擎）。

    ``snapshot_data`` 是 LIVE 模式下由 ``LiveDataService`` 生成的快照字典；
    未提供时保持原行为，从 ``input_path`` 读取冻结快照。
    """

    active_project = dict(project or _workspace_project())
    if snapshot_data is None:
        snapshot_path = Path(input_path or active_project["input_path"])
        data = load_frozen_snapshot(snapshot_path)
    else:
        data = dict(snapshot_data)
    payload = data["payload"]
    earnings = payload["earnings"]
    scenario = card.get("scenario") or {}
    account = payload["account"]
    scenario_payload = dict(payload)
    scenario_account = dict(account)
    if scenario.get("account_cash_hkd") is not None:
        scenario_account["cash_hkd"] = scenario["account_cash_hkd"]
    if scenario.get("risk_budget_pct") is not None:
        scenario_account["risk_budget_pct"] = scenario["risk_budget_pct"]
    scenario_payload["account"] = scenario_account
    scenario_data = {**data, "payload": scenario_payload}
    engine = compute_engine(scenario_data, scenario=scenario)
    expiries = _expiries_ui(payload, engine)
    research = dict(card["research_evidence"])
    research["sourcePath"] = _research_items_label(_research_items_path(active_project))
    research["sourceMode"] = "配置文件"
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
            "capabilities": data.get("capabilities") or {},
            "warnings": data.get("warnings", []),
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
            "cashHkd": scenario.get("account_cash_hkd", account["cash_hkd"]),
            "riskBudgetPct": scenario.get("risk_budget_pct", account["risk_budget_pct"]),
            "contractMultiplier": account["contract_multiplier"],
        },
        "scenario": card["scenario"],
        "expiries": expiries,
        "terminal": _terminal_ui(payload, card, expiries, data),
        "decisionCard": _card_ui(card),
        "macro": card["macro_assessment"],
        "policyLibrary": _policy_library_ui(),
        "research": research,
        "workspace": _workspace_ui(active_project),
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
    parsed_result: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    input_path: str | Path | None = None,
    snapshot_data: Mapping[str, Any] | None = None,
    write_card: bool | None = None,
) -> dict[str, Any]:
    """对话链路：确定性场景解析 → 五阶段管线 → 十角色辩论（有 key 时）。

    辩论只附加文字结论与证据引用；``/api/chat`` 的 verdict 与全部数字仍来自
    ``run_pipeline``，无 key / 网络失败 / 超时自动回退确定性路径。
    """

    active_project = dict(project or _workspace_project())
    snapshot_path = Path(input_path or active_project["input_path"])
    parsed_scenario = (
        dict(parsed_result)
        if parsed_result is not None
        else parse_message(message, snapshot["payload"])
    )
    card = run_pipeline(
        snapshot_path,
        scenario=parsed_scenario["scenario"],
        research_items_path=_research_items_path(active_project),
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=audit_enabled,
        write_card=audit_enabled if write_card is None else write_card,
        snapshot_data=snapshot_data,
    )
    state = compose_state(
        card,
        project=active_project,
        input_path=snapshot_path,
        snapshot_data=snapshot_data,
    )
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


def _agent_suggestions(state: Mapping[str, Any]) -> list[dict[str, str]]:
    """给前端返回少量有上下文的下一步，不把内部枚举暴露成操作语言。"""

    selected = ((state.get("terminal") or {}).get("selection") or {}).get("expiry")
    expiries = state.get("expiries") or []
    next_expiry = next(
        (item.get("expiry") for item in expiries if item.get("expiry") != selected),
        None,
    )
    suggestions: list[dict[str, str]] = [
        {"label": "解释当前结论", "message": "解释一下当前结论和主要原因"},
        {"label": "检查风险预算", "message": "检查当前方案的风险预算和最大亏损"},
        {"label": "打开期权链", "action": "open_view", "view": "chain"},
        {"label": "生成分歧记录", "action": "debate"},
        {"label": "刷新冻结快照", "action": "refresh"},
        {"label": "研究其他公司", "message": "研究另一家公司的期权"},
    ]
    if next_expiry:
        suggestions.insert(
            2,
            {"label": "查看另一到期", "action": "select_expiry", "expiry": next_expiry},
        )
    return suggestions


def _agent_trace(kind: str) -> list[dict[str, str]]:
    """返回真实动作完成后的阶段记录，前端据此呈现工作流而非装饰动画。"""

    if kind in {"scenario", "chat"}:
        labels = ["理解工作条件", "读取冻结数据", "重算损益", "检查风险", "更新工作台"]
    elif kind == "refresh":
        labels = ["读取当前任务", "刷新冻结数据", "重算损益", "检查风险", "更新工作台"]
    elif kind == "selection":
        labels = ["读取当前任务", "定位到期", "同步报价与 Greeks", "更新工作台"]
    elif kind == "discovery":
        labels = ["理解找数请求", "扫描受控资料目录", "校验文件归属", "绑定研究项目", "更新工作台"]
    else:
        labels = ["读取当前工作集", "理解请求", "更新工作台"]
    return [
        {"id": str(index + 1), "label": label, "status": "complete"}
        for index, label in enumerate(labels)
    ]


def _agent_view_action(message: str) -> tuple[str, str] | None:
    text = str(message).lower()
    if "期权链" in text or "报价" in text or "流动性" in text:
        return "chain", "已为你打开期权链，当前行可以继续点击切换到期。"
    if "事件环境" in text or "宏观" in text or "业绩日" in text:
        return "macro", "已为你打开事件环境，查看业绩与宏观情景。"
    if "投研" in text or "资料" in text or "新闻" in text or "报告" in text:
        return "research", "已为你打开投研资料。"
    if "分歧" in text or "共识" in text:
        return "debate", "已为你打开分歧记录。"
    if any(token in text for token in ("风险检查", "结论与风险", "打开风险", "查看风险")):
        return "card", "已为你打开结论与风险检查。"
    return None


def _agent_expiry_request(message: str, expiries: list[Mapping[str, Any]]) -> str | None:
    text = str(message).lower().replace("/", "-")
    for item in expiries:
        expiry = str(item.get("expiry", ""))
        short = expiry[5:].replace("-", "-")
        if expiry.lower() in text or short.lower() in text:
            return expiry
    if "次到期" in text or "第二个到期" in text:
        return next((str(item.get("expiry")) for item in expiries if not item.get("primary")), None)
    if "主到期" in text or "第一个到期" in text:
        return next((str(item.get("expiry")) for item in expiries if item.get("primary")), None)
    return None


def _agent_has_scenario_signal(message: str) -> bool:
    text = str(message).lower()
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|万|千|k|天|日|dte)", text):
        return True
    if _project_symbol_from_text(message) is not None:
        return True
    if re.search(r"\b\d{4,8}\b", text) and any(
        token in text for token in ("股票", "个股", "标的", "公司", "期权", "证券")
    ):
        return True
    return any(
        token in text
        for token in (
            "重算",
            "分歧",
            "辩论",
            "共识",
            "看多",
            "看涨",
            "看空",
            "看跌",
            "方向不确定",
            "跨式",
            "业绩前",
            "财报前",
        )
    )


def _agent_context_scenario(
    body: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any] | None:
    context = body.get("context")
    if not isinstance(context, Mapping):
        return None
    raw = context.get("scenario")
    if not isinstance(raw, Mapping):
        return None
    defaults = snapshot["payload"]
    scenario = dict(raw)
    scenario.setdefault("underlying", defaults["underlying"])
    scenario.setdefault("view", "uncertain")
    scenario.setdefault("horizon", f"{defaults['earnings']['date']} 业绩")
    scenario.setdefault("account_cash_hkd", defaults["account"]["cash_hkd"])
    scenario.setdefault("risk_budget_pct", defaults["account"]["risk_budget_pct"])
    scenario.setdefault("constraints", [])
    try:
        return parse_scenario(scenario)
    except ValueError:
        # 客户端上下文只是辅助信息，失效时回到冻结快照，而不是阻塞新的提问。
        return None


def _parse_agent_scenario(
    message: str,
    snapshot: Mapping[str, Any],
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """允许用户只说“风险改成 2%”，不必每轮重复标的和期限。"""

    seed = f"{current['underlying']} {message}" if current else message
    parsed = parse_message(seed, snapshot["payload"])
    if current is None:
        return parsed

    scenario = dict(parsed["scenario"])
    text = str(message).lower()
    if "account_cash_hkd" in parsed.get("assumed", []):
        scenario["account_cash_hkd"] = current["account_cash_hkd"]
    if "risk_budget_pct" in parsed.get("assumed", []):
        scenario["risk_budget_pct"] = current["risk_budget_pct"]
    if not any(token in text for token in ("看多", "看涨", "上涨", "做多", "bullish", "看空", "看跌", "下跌", "做空", "bearish", "不确定", "方向不明", "震荡", "跨式", "uncertain", "straddle")):
        scenario["view"] = current["view"]
    if not any(token in text for token in ("业绩", "财报", "earnings")) and not re.search(r"\d+\s*(?:天|日|dte)", text):
        scenario["horizon"] = current["horizon"]
    if "account_cash_hkd" in parsed.get("assumed", []) and "risk_budget_pct" in parsed.get("assumed", []):
        scenario["constraints"] = list(current.get("constraints", []))
    return {
        "scenario": scenario,
        "assumed": [],
        "notes": ["本轮只补充了你修改的条件，其余条件沿用当前工作集。"],
    }


def _state_for_agent_scenario(
    scenario: Mapping[str, Any],
    *,
    audit_enabled: bool,
    selected_expiry: str | None = None,
    project: Mapping[str, Any] | None = None,
    input_path: str | Path | None = None,
    snapshot_data: Mapping[str, Any] | None = None,
    write_card: bool | None = None,
) -> dict[str, Any]:
    active_project = dict(project or _workspace_project())
    snapshot_path = Path(input_path or active_project["input_path"])
    parsed = parse_scenario(dict(scenario))
    card = run_pipeline(
        snapshot_path,
        scenario=parsed,
        research_items_path=_research_items_path(active_project),
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=audit_enabled,
        write_card=audit_enabled if write_card is None else write_card,
        snapshot_data=snapshot_data,
    )
    invalidate_state_cache()
    state = compose_state(
        card,
        project=active_project,
        input_path=snapshot_path,
        snapshot_data=snapshot_data,
    )
    state["chat"] = {
        "scenario": card["scenario"],
        "assumed": [],
        "notes": ["本轮只补充了你修改的条件，其余条件沿用当前工作集。"],
    }
    state["_agent_selected_expiry"] = selected_expiry
    return state


def _agent_context_message(
    message: str,
    state: Mapping[str, Any],
    selected_expiry: str | None = None,
) -> str:
    """离线上下文回答：解释类问题不强迫用户重复输入标的。"""

    decision = state.get("decisionCard") or {}
    terminal = state.get("terminal") or {}
    risk = terminal.get("risk") or {}
    selection = terminal.get("selection") or {}
    expiry = selected_expiry or selection.get("expiry") or "当前到期"
    text = str(message).lower()
    verdict = decision.get("verdict")
    if any(token in text for token in ("风险", "预算", "亏损", "risk")):
        max_loss = risk.get("maxLoss")
        if max_loss in (None, 0, 0.0):
            return (
                f"当前选中 {expiry}。风险预算为 {float(risk.get('budgetHkd', 0)):,.0f} HKD，"
                f"不足以覆盖最小一张方案成本；当前可执行数量为 0 张。"
                "这是冻结快照下的研究判断，放宽预算或更换结构后再重新计算。"
            )
        return (
            f"当前选中 {expiry}。风险预算为 {float(risk.get('budgetHkd', 0)):,.0f} HKD，"
            f"方案最大亏损约 {float(max_loss):,.0f} HKD；"
            "这只是冻结快照下的研究估算，提交任何模拟动作前仍需重新核对账户。"
        )
    if verdict in {"NO_TRADE", "BLOCK"}:
        reason = "机会条件没有覆盖成本与波动要求" if verdict == "NO_TRADE" else "风险预算或数据核验未通过"
        return f"当前结论是先不交易。主要原因：{reason}；你可以改动研究条件后重新计算，或切换到另一到期查看。"
    return "当前快照已经形成研究结论。你可以继续问我解释原因、检查风险预算，或直接修改下方条件重新计算。"


def _attach_agent(
    state: dict[str, Any],
    *,
    action: str,
    message: str,
    intent: str = "context",
    expiry: str | None = None,
    actions: list[dict[str, Any]] | None = None,
    trace: list[dict[str, str]] | None = None,
    discovery: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expiry = expiry or state.pop("_agent_selected_expiry", None)
    terminal = state.setdefault("terminal", {})
    if expiry is not None:
        available = {item.get("expiry"): item for item in state.get("expiries") or []}
        if expiry not in available:
            raise ValueError("expiry 不在当前冻结快照中")
        terminal["selection"] = {
            "expiry": expiry,
            "label": "主到期" if available[expiry].get("primary") else "次到期",
        }
    state["agent"] = {
        "action": action,
        "status": "complete",
        "message": message,
        "intent": intent,
        "selectedExpiry": (terminal.get("selection") or {}).get("expiry"),
        "suggestions": _agent_suggestions(state),
        "actions": actions or [],
        "trace": trace or _agent_trace(intent),
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if discovery is not None:
        state["agent"]["discovery"] = discovery
    return state


def _project_discovery_state(
    *,
    action: str,
    message: str,
    discovery: list[dict[str, Any]],
) -> dict[str, Any]:
    state = copy.deepcopy(build_state_cached())
    return _attach_agent(
        state,
        action=action,
        intent="discovery",
        message=message,
        discovery=discovery,
        trace=_agent_trace("discovery"),
    )


def _run_agent_project_discovery(
    body: Mapping[str, Any],
    *,
    audit_enabled: bool,
) -> dict[str, Any]:
    """让助理在受控目录内寻找并注册一个公司研究项目。"""

    symbol, query = _agent_project_request(body)
    registry = _workspace_registry()

    # 已注册项目优先切换，避免 Agent 重复创建同一标的。
    existing = None
    query_terms = [
        token.casefold()
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", query)
        if len(token) >= 2
    ]
    for project in registry["projects"]:
        if symbol and project["symbol"] == symbol:
            existing = project
            break
        project_text = " ".join(
            [
                str(project.get("name") or "").casefold(),
                str(project.get("symbol") or "").casefold(),
            ]
        )
        if query and any(term in project_text for term in query_terms):
            existing = project
            break
    if existing is not None:
        _set_active_project(existing["id"])
        return _project_discovery_state(
            action="discover_project",
            message=f"工作区里已经有 {existing['name']}（{existing['symbol']}），已为你切换。",
            discovery=[],
        )

    if symbol:
        candidates = discover_project_assets(symbol=symbol, root=ROOT)
    else:
        candidates = discover_project_assets(query=query or None, root=ROOT)
        # 泛化请求（如“找一个新项目”）不应因为自然语言没有命中文件名而
        # 直接说没有数据；退回列出所有已通过校验的候选供 Agent 继续判断。
        if not candidates:
            candidates = discover_project_assets(root=ROOT)

    snapshots = [item for item in candidates if item["kind"] == "snapshot"]
    if not snapshots:
        if candidates:
            research_paths = "、".join(
                str(item["path"])
                for item in candidates
                if item["kind"] == "research"
            )
            detail = (
                f"我找到了投研资料（{research_paths}），但没有找到对应的有效期权快照。"
                if research_paths
                else "当前目录里没有找到可用的研究资产。"
            )
        else:
            detail = "当前目录里没有找到可用的研究资产。"
        target = symbol or query or "目标公司"
        return _project_discovery_state(
            action="discover_project",
            message=(
                f"我在 {target} 的受控资料范围内没有找到有效期权快照，未创建项目。"
                f"{detail}请把符合输入契约的 JSON 放入 data/projects/，我会自动继续绑定。"
            ),
            discovery=candidates,
        )

    if len(snapshots) > 1:
        paths = "、".join(str(item["path"]) for item in snapshots[:5])
        target = symbol or "该查询"
        return _project_discovery_state(
            action="discover_project",
            message=(
                f"我为 {target} 找到多个有效快照，为避免接错公司，暂未创建项目：{paths}。"
                "请告诉我使用哪一个文件，或移走旧版本后让我重新扫描。"
            ),
            discovery=candidates,
        )

    selected_snapshot = snapshots[0]
    selected_symbol = str(selected_snapshot["symbol"])
    # 选定快照后按其真实 underlying 再做一次资料筛选，避免自然语言查询
    # 只命中快照而漏掉同标的的投研文件。
    resolved = discover_project_assets(symbol=selected_symbol, root=ROOT)
    research_candidates = [item for item in resolved if item["kind"] == "research"]
    if len(research_candidates) > 1:
        paths = "、".join(str(item["path"]) for item in research_candidates[:5])
        return _project_discovery_state(
            action="discover_project",
            message=(
                f"快照已找到（{selected_snapshot['path']}），但同标的有多份投研资料：{paths}。"
                "为避免混用，暂未创建项目；请保留主资料后让我重新扫描。"
            ),
            discovery=resolved,
        )

    requested_name = str(body.get("name") or "").strip()
    project_name = requested_name or str(selected_snapshot.get("name") or "").strip()
    try:
        project = register_project(
            name=project_name,
            symbol=selected_symbol,
            input_path=None,
            research_items_path=None,
            project_id=str(body.get("projectId") or "").strip() or None,
            registry_path=_workspace_registry_path(),
            root=ROOT,
        )
        _set_active_project(project["id"])
    except ValueError as exc:
        return _project_discovery_state(
            action="discover_project",
            message=f"我找到了候选文件，但注册项目没有完成：{exc}",
            discovery=resolved,
        )

    state = copy.deepcopy(build_state_cached())
    workspace_project = _workspace_project()
    research_path = _research_items_label(Path(workspace_project["research_items_path"]))
    research_note = (
        f"投研资料已自动绑定：{research_path}。"
        if research_candidates
        else "没有找到同标的投研资料，当前使用空资料集。"
    )
    return _attach_agent(
        state,
        action="discover_project",
        intent="discovery",
        message=(
            f"已自动找到并打开 {workspace_project['name']}（{selected_symbol}）。"
            f"快照：{_research_items_label(Path(workspace_project['input_path']))}；"
            f"{research_note}"
        ),
        discovery=resolved,
        trace=_agent_trace("discovery"),
    )


def _load_agent_snapshot(
    project: Mapping[str, Any],
    input_path: Path,
    *,
    force: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """Load the snapshot for agent actions without a silent replay fallback.

    REPLAY mode keeps the frozen-file path.  LIVE mode builds/refreshes the live
    snapshot through ``LiveDataService`` and returns the same dict as both the
    pipeline snapshot and the ``snapshot_data`` that ``run_pipeline`` /
    ``compose_state`` consume.  A live-data failure raises ``_LiveDataError``
    instead of falling back to ``load_frozen_snapshot``.
    """

    if _data_mode(project) is DataMode.LIVE:
        snapshot_data = _build_live_snapshot_for_project(project, force=force)
        return snapshot_data, snapshot_data, True
    return load_frozen_snapshot(input_path), None, False


def run_agent_action(
    body: Mapping[str, Any],
    *,
    audit_enabled: bool = True,
) -> dict[str, Any]:
    """桌面助理动作总线：发现、切换和重算都落到受控数据或确定性管线。"""

    requested_project = str(body.get("projectId") or "").strip()
    if requested_project:
        _set_active_project(requested_project)
    active_project = _workspace_project()
    input_path = Path(active_project["input_path"])
    action = str(body.get("action", "ask")).strip().lower()
    if action in {"discover_project", "find_project", "add_project", "discover"}:
        return _run_agent_project_discovery(body, audit_enabled=audit_enabled)
    if action == "refresh":
        snapshot, snapshot_data, live_mode = _load_agent_snapshot(
            active_project, input_path, force=True
        )
        effective_audit = audit_enabled and not live_mode
        context = body.get("context") if isinstance(body.get("context"), Mapping) else {}
        current = _agent_context_scenario(body, snapshot)
        if current is not None:
            parsed = parse_scenario(current)
            state = _state_for_agent_scenario(
                parsed,
                audit_enabled=effective_audit,
                selected_expiry=str(context.get("selectedExpiry", "")).strip() or None,
                project=active_project,
                input_path=input_path,
                snapshot_data=snapshot_data,
                write_card=effective_audit,
            )
        else:
            card = run_pipeline(
                input_path,
                research_items_path=_research_items_path(active_project),
                macro_policy_path=DEFAULT_POLICY_DIR,
                audit_enabled=effective_audit,
                write_card=effective_audit,
                snapshot_data=snapshot_data,
            )
            state = compose_state(
                card,
                project=active_project,
                input_path=input_path,
                snapshot_data=snapshot_data,
            )
        invalidate_state_cache()
        return _attach_agent(
            state,
            action="refresh",
            intent="refresh",
            message=(
                "实时行情已刷新，损益、报价和风险结论已同步。"
                if live_mode
                else "冻结快照已重新读取，损益、报价和风险结论已同步。"
            ),
        )

    if action == "select_expiry":
        expiry = str(body.get("expiry", "")).strip()
        if not expiry:
            raise ValueError("select_expiry 需要 expiry")
        snapshot, snapshot_data, live_mode = _load_agent_snapshot(
            active_project, input_path, force=False
        )
        current = _agent_context_scenario(body, snapshot)
        if current is not None:
            state = _state_for_agent_scenario(
                current,
                audit_enabled=False,
                selected_expiry=expiry,
                project=active_project,
                input_path=input_path,
                snapshot_data=snapshot_data,
                write_card=False,
            )
        else:
            state = copy.deepcopy(build_state_cached())
        return _attach_agent(
            state,
            action="select_expiry",
            intent="selection",
            expiry=expiry,
            message=f"已切换到 {expiry}。右侧报价、Greeks、损益和研究条件会围绕当前到期显示。",
        )

    if action in {"run_scenario", "scenario"}:
        scenario = body.get("scenario")
        if not isinstance(scenario, dict):
            raise ValueError("run_scenario 需要 scenario 对象")
        # 先显式校验，给桌面端稳定的 422 文案；管线会再次校验并记录标准化结果。
        parsed = parse_scenario(scenario)
        snapshot, snapshot_data, live_mode = _load_agent_snapshot(
            active_project, input_path, force=True
        )
        effective_audit = audit_enabled and not live_mode
        state = _state_for_agent_scenario(
            parsed,
            audit_enabled=effective_audit,
            selected_expiry=str(body.get("expiry", "")).strip() or None,
            project=active_project,
            input_path=input_path,
            snapshot_data=snapshot_data,
            write_card=effective_audit,
        )
        selected = str(body.get("expiry", "")).strip() or None
        return _attach_agent(
            state,
            action="run_scenario",
            intent="scenario",
            expiry=selected,
            message=(
                f"研究条件已重算：现金 {parsed['account_cash_hkd']:,.0f} HKD，"
                f"风险上限 {parsed['risk_budget_pct']:g}%。"
            ),
            trace=_agent_trace("scenario"),
        )

    if action == "debate":
        message = str(body.get("message", "围绕当前工作集生成分歧记录")).strip()
        snapshot, snapshot_data, live_mode = _load_agent_snapshot(
            active_project, input_path, force=True
        )
        effective_audit = audit_enabled and not live_mode
        current = _agent_context_scenario(body, snapshot)
        parsed = (
            {"scenario": current, "assumed": [], "notes": ["沿用当前工作集生成分歧记录。"]}
            if current is not None
            else parse_message(
                f"{active_project['symbol']} 方向不确定 业绩", snapshot["payload"]
            )
        )
        state = run_chat(
            message,
            snapshot,
            audit_enabled=effective_audit,
            parsed_result=parsed,
            project=active_project,
            input_path=input_path,
            snapshot_data=snapshot_data,
            write_card=effective_audit,
        )
        invalidate_state_cache()
        return _attach_agent(
            state,
            action="debate",
            intent="debate",
            message="分歧记录已生成，可以在分歧工作区查看角色观点与共识。",
            expiry=(body.get("context") or {}).get("selectedExpiry") if isinstance(body.get("context"), Mapping) else None,
            actions=[{"type": "open_view", "view": "debate", "label": "打开分歧记录"}],
            trace=_agent_trace("chat"),
        )

    if action == "ask":
        message = str(body.get("message", "")).strip()
        if not message:
            raise ValueError("ask 需要 message")
        if _agent_discovery_signal(message):
            return _run_agent_project_discovery(
                {**dict(body), "query": message}, audit_enabled=audit_enabled
            )
        snapshot, snapshot_data, live_mode = _load_agent_snapshot(
            active_project, input_path, force=True
        )
        effective_audit = audit_enabled and not live_mode
        current = _agent_context_scenario(body, snapshot)
        context = body.get("context") if isinstance(body.get("context"), Mapping) else {}
        selected_expiry = str(context.get("selectedExpiry", "")).strip() or None
        view_action = _agent_view_action(message)
        scenario_signal = _agent_has_scenario_signal(message)

        # 导航/解释类请求直接作用于当前工作集，不再把它误判成一次新的场景解析。
        if not scenario_signal:
            if current is not None:
                state = _state_for_agent_scenario(
                    current,
                    audit_enabled=False,
                    selected_expiry=selected_expiry,
                    project=active_project,
                    input_path=input_path,
                    snapshot_data=snapshot_data,
                    write_card=False,
                )
            else:
                state = copy.deepcopy(build_state_cached())
            actions: list[dict[str, Any]] = []
            if view_action:
                view, reply = view_action
                actions.append({"type": "open_view", "view": view, "label": "打开" + reply.split("已为你打开", 1)[-1].rstrip("。")})
                intent = "navigation"
                message_text = reply
            else:
                if any(token in message.lower() for token in ("风险", "预算", "亏损", "risk")):
                    actions.append({"type": "open_view", "view": "card", "label": "打开结论与风险"})
                actions.append({"type": "open_controls", "label": "展开研究条件"})
                intent = "context"
                message_text = _agent_context_message(message, state, selected_expiry)
            return _attach_agent(
                state,
                action="ask",
                intent=intent,
                expiry=selected_expiry,
                actions=actions,
                message=message_text,
            )

        try:
            parsed = _parse_agent_scenario(message, snapshot, current)
            deep_request = any(token in message.lower() for token in ("分歧", "辩论", "共识", "角色观点"))
            if deep_request:
                state = run_chat(
                    message,
                    snapshot,
                    audit_enabled=effective_audit,
                    parsed_result=parsed,
                    project=active_project,
                    input_path=input_path,
                    snapshot_data=snapshot_data,
                    write_card=effective_audit,
                )
                invalidate_state_cache()
            else:
                state = _state_for_agent_scenario(
                    parsed["scenario"],
                    audit_enabled=effective_audit,
                    selected_expiry=selected_expiry,
                    project=active_project,
                    input_path=input_path,
                    snapshot_data=snapshot_data,
                    write_card=effective_audit,
                )
            scenario = (state.get("chat") or {}).get("scenario") or parsed["scenario"]
            actions = [{"type": "open_view", "view": "overview", "label": "回到行情总览"}]
            if any(token in message.lower() for token in ("风险", "预算", "亏损")):
                actions.append({"type": "open_view", "view": "card", "label": "查看风险结论"})
            if not deep_request:
                actions.append({"type": "debate", "label": "生成分歧记录"})
            return _attach_agent(
                state,
                action="ask",
                intent="scenario",
                message=(
                    "已沿用当前工作集并完成重算。"
                    f"现金 {float(scenario['account_cash_hkd']):,.0f} HKD，"
                    f"风险上限 {float(scenario['risk_budget_pct']):g}%。"
                ),
                expiry=selected_expiry,
                actions=actions,
                trace=_agent_trace("chat"),
            )
        except ValueError as exc:
            # “解释当前结论”这类追问天然有上下文，不要求用户重复输入 0700.HK。
            if "无法识别标的" not in str(exc):
                raise
            state = copy.deepcopy(build_state_cached())
            return _attach_agent(
                state,
                action="ask",
                intent="context",
                message=_agent_context_message(message, state),
            )

    raise ValueError("不支持的助理动作")


def _live_error_state(project: Mapping[str, Any], exc: _LiveDataError) -> dict[str, Any]:
    """Explicit LIVE error state for GET /api/state; never a replay fallback."""

    return {
        "meta": {
            "product": "GOAI 港美股期权智能终端",
            "mode": DataMode.LIVE.value,
            "freshness": FreshnessStatus.UNKNOWN.value,
            "origin": "FUTU",
            "capturedAt": None,
            "source": "Futu OpenD 实时行情（Live snapshot）",
            "marketState": "",
            "snapshotSha256": None,
            "capabilities": {},
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "error": exc.to_dict(),
        "typedError": exc.to_dict(),
        "underlying": {
            "code": str(project.get("symbol") or ""),
            "name": str(project.get("name") or ""),
            "spot": None,
            "prevClose": None,
        },
        "workspace": _workspace_ui(project),
    }


def _project_registration_state(
    project: Mapping[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """POST /api/projects 的响应：注册成功与实时状态不可用分开表达。

    ``build_state_cached`` 在 LIVE 模式 OpenD 不可用时会返回带顶层
    ``error``/``typedError`` 的 ``_live_error_state``。若把它直接作为
    POST /api/projects 的响应，客户端会误以为注册失败并重试；而项目已经
    持久化，重试会撞上工作区重复检查返回 422。这里把响应改为注册成功
    信封，并把实时行情错误降级为 ``stateError``。
    """

    if state.get("error") is not None and state.get("typedError") is not None:
        return {
            "registered": True,
            "project": {
                "id": project["id"],
                "name": project["name"],
                "symbol": project["symbol"],
                "data_mode": project.get("data_mode", "replay"),
            },
            "meta": state.get("meta", {}),
            "underlying": state.get("underlying", {}),
            "workspace": _workspace_ui(project),
            "stateError": state.get("typedError"),
            "warning": (
                "项目已注册并写入工作区，但 OpenD 实时行情不可用，"
                "当前状态未生成。请启动 OpenD 后刷新。"
            ),
        }
    return state


def _project_selection_state(
    project: Mapping[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """POST /api/projects/select 的响应：切换成功与实时状态不可用分开表达。

    与注册路径同一规则：LIVE 模式 OpenD 不可用时项目切换已经持久化，
    不能因为 build_state 返回 live error 让客户端误以为切换失败而重试。
    """

    if state.get("error") is not None and state.get("typedError") is not None:
        return {
            "selected": True,
            "project": {
                "id": project["id"],
                "name": project["name"],
                "symbol": project["symbol"],
                "data_mode": project.get("data_mode", "replay"),
            },
            "meta": state.get("meta", {}),
            "underlying": state.get("underlying", {}),
            "workspace": _workspace_ui(project),
            "stateError": state.get("typedError"),
            "warning": (
                "项目已切换，但 OpenD 实时行情不可用，当前状态未生成。"
                "请启动 OpenD 后刷新。"
            ),
        }
    return state


def build_state(project_id: str | None = None) -> dict[str, Any]:
    """当前项目的只读状态：REPLAY 用冻结快照重跑，LIVE 用 live snapshot。

    两种模式都调用五阶段管线，但本函数是只读路径：不写审计、不写决策卡。
    LIVE 模式下 OpenD 不可用时返回显式 OPEND_UNAVAILABLE 状态，不静默回退
    到冻结快照。
    """

    project = _workspace_project(project_id)
    input_path = Path(project["input_path"])
    if _data_mode(project) is DataMode.LIVE:
        try:
            snapshot_data = _build_live_snapshot_for_project(project, force=False)
        except _LiveDataError as exc:
            return _live_error_state(project, exc)
        card = run_pipeline(
            input_path,
            research_items_path=_research_items_path(project),
            macro_policy_path=DEFAULT_POLICY_DIR,
            audit_enabled=False,
            write_card=False,
            snapshot_data=snapshot_data,
        )
        return compose_state(
            card,
            project=project,
            input_path=input_path,
            snapshot_data=snapshot_data,
        )
    card = run_pipeline(
        input_path,
        research_items_path=_research_items_path(project),
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=False,
        write_card=False,
    )
    return compose_state(card, project=project, input_path=input_path)


_STATE_CACHE_TTL_SECONDS = 30.0
_STATE_CACHE_TTL_LIVE_SECONDS = 5.0
_STATE_CACHE: dict[str, Any] = {
    "value": None,
    "built_at": 0.0,
    "project_id": None,
    "mode": None,
}
_STATE_CACHE_LOCK = threading.Lock()


def invalidate_state_cache() -> None:
    """POST 重跑/对话后失效缓存，避免下次 GET /api/state 读到旧审计轨迹。"""
    with _STATE_CACHE_LOCK:
        _STATE_CACHE["value"] = None
        _STATE_CACHE["built_at"] = 0.0
        _STATE_CACHE["project_id"] = None
        _STATE_CACHE["mode"] = None
    # 所有写路径（POST run/chat/command/agent/projects）都会经过这里；
    # 广播 refresh 让 LIVE 前端立即重拉 state，而不是等下一轮轮询。
    _notify_stream_refresh()


def build_state_cached() -> dict[str, Any]:
    """GET /api/state 专用：按模式缓存管线结果。

    REPLAY 快照冻结不变，缓存 30s；LIVE 行情会变化，缓存 5s。
    POST /api/run、/api/chat 与写审计路径总是实时重算并失效缓存。
    """
    project = _workspace_project()
    mode = _data_mode(project)
    ttl = _state_cache_ttl_seconds(mode)
    now = time.monotonic()
    with _STATE_CACHE_LOCK:
        value = _STATE_CACHE["value"]
        if (
            value is not None
            and _STATE_CACHE["project_id"] == project["id"]
            and _STATE_CACHE["mode"] == mode
            and now - _STATE_CACHE["built_at"] < ttl
        ):
            return value
    state = build_state()
    with _STATE_CACHE_LOCK:
        _STATE_CACHE["value"] = state
        _STATE_CACHE["built_at"] = time.monotonic()
        _STATE_CACHE["project_id"] = project["id"]
        _STATE_CACHE["mode"] = mode
    return state


def _pipeline_state(
    project: Mapping[str, Any],
    *,
    audit_enabled: bool,
    write_card: bool,
    snapshot_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the five-stage pipeline for a project and compose the UI state.

    REPLAY callers pass ``snapshot_data=None`` (pipeline loads the frozen file);
    LIVE callers pass the snapshot built by ``LiveDataService``.
    """

    input_path = Path(project["input_path"])
    card = run_pipeline(
        input_path,
        research_items_path=_research_items_path(project),
        macro_policy_path=DEFAULT_POLICY_DIR,
        audit_enabled=audit_enabled,
        write_card=write_card,
        snapshot_data=snapshot_data,
    )
    return compose_state(
        card,
        project=project,
        input_path=input_path,
        snapshot_data=snapshot_data,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "GOAI-UI/1.0"

    def _host_allowed(self) -> bool:
        """只接受本机 Host（防 DNS rebinding 把浏览器访问导向本地服务）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        hostname = host.rsplit(":", 1)[0].strip("[]")
        return hostname in ("localhost", "127.0.0.1", "::1")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中途断开：静默处理，避免线程堆栈刷屏
            pass

    def _finish_state(self, state: dict[str, Any], event: str, text: str, start: float) -> None:
        """写会话度量后返回状态（只读端点不经过这里）。"""
        verdict = ((state or {}).get("decisionCard") or {}).get("verdict")
        mode = ((state or {}).get("meta") or {}).get("mode")
        _record_session_metric(
            event,
            text,
            verdict,
            (time.monotonic() - start) * 1000.0,
            mode,
        )
        self._send_json(state)

    def _send_error_json(self, exc: Exception) -> None:
        # 服务端错误不泄露堆栈与绝对路径（与 do_GET 注释一致）
        message = str(exc)
        message = message.replace(str(ROOT), "<repo>")
        message = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", message)
        self._send_json({"error": message[:400]}, status=500)

    def _send_live_error_json(self, exc: _LiveDataError) -> None:
        typed_error = exc.to_dict()
        code = str(typed_error.get("code", "OPEND_UNAVAILABLE"))
        message = str(typed_error.get("message", str(exc)))
        self._send_json(
            {
                "error": f"{code}: {message}",
                "typedError": typed_error,
            },
            status=503,
        )

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0 or length > 1024 * 1024:
            raise ValueError("请求体超过 1 MiB 上限")
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
        # 逐组件校验 symlink（校验发生在读取前，防止 TOCTOU 逃逸出 UI_DIR）
        current = UI_DIR
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                self._send_json({"error": "not found"}, status=404)
                return
        target = current
        try:
            target.resolve().relative_to(UI_DIR.resolve())
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_sse_stream(
        self,
        service: LiveStreamService,
        sub_id: int,
        codes: list[str],
        hello_payload: Mapping[str, Any] | None,
        hello_error: Mapping[str, Any] | None,
        poll_seconds: float,
    ) -> None:
        """Serve one ``text/event-stream`` connection until the client leaves.

        事件：``hello``（订阅元数据 + 订阅数）、``quote``（报价/新鲜度变化）、
        ``error``（typed live 失败）、``warning``（推送回退等）、``refresh``
        （POST 重跑后提示前端重拉 state）；15s 注释心跳保活，断连即退订。
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            wfile = self.wfile

            def write_block(block: str) -> None:
                wfile.write(block.encode("utf-8"))
                wfile.flush()

            write_block(f"retry: {_STREAM_RETRY_MS}\n\n")
            hello = {
                "mode": DataMode.LIVE.value,
                "codes": codes,
                "pollSeconds": poll_seconds,
                "subscribers": service.hub.subscriber_count,
            }
            if hello_error is not None:
                hello["error"] = dict(hello_error)
            write_block(
                "event: hello\ndata: "
                + json.dumps(hello, ensure_ascii=False)
                + "\n\n"
            )
            if hello_payload is not None:
                write_block(
                    "event: quote\ndata: "
                    + json.dumps(dict(hello_payload), ensure_ascii=False)
                    + "\n\n"
                )
            last_heartbeat = time.monotonic()
            while True:
                kind, name, payload = service.next_event(sub_id, timeout=1.0)
                if kind == "closed":
                    break
                if kind == "timeout":
                    if time.monotonic() - last_heartbeat >= _STREAM_HEARTBEAT_SECONDS:
                        write_block(": heartbeat\n\n")
                        last_heartbeat = time.monotonic()
                    continue
                write_block(
                    f"event: {name}\ndata: "
                    + json.dumps(payload, ensure_ascii=False)
                    + "\n\n"
                )
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端中途断开：静默处理
            pass
        finally:
            service.unsubscribe(sub_id, codes)
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        parsed = urlparse(self.path)
        try:
            if not self._host_allowed():
                self._send_json({"error": "forbidden host"}, status=403)
                return
            if parsed.path == "/api/state":
                self._send_json(build_state_cached())
                return
            if parsed.path == "/api/live-quote":
                if _data_mode(_workspace_project()) is not DataMode.LIVE:
                    self._send_json(
                        {"error": "live-quote 仅在 GOAI_DATA_MODE=live 或项目 data_mode=live 时可用"},
                        status=422,
                    )
                    return
                raw_codes = parse_qs(parsed.query).get("codes", [""])[0]
                codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
                if not codes:
                    self._send_json({"error": "codes 不能为空"}, status=400)
                    return
                service = _get_live_data_service()
                try:
                    payload = service.quote_payload(codes)
                except _LiveDataError as exc:
                    self._send_live_error_json(exc)
                    return
                self._send_json(payload)
                return
            if parsed.path == "/api/stream":
                if _data_mode(_workspace_project()) is not DataMode.LIVE:
                    self._send_json(
                        {"error": "stream 仅在 GOAI_DATA_MODE=live 或项目 data_mode=live 时可用"},
                        status=422,
                    )
                    return
                raw_codes = parse_qs(parsed.query).get("codes", [""])[0]
                if raw_codes.strip():
                    codes = [
                        code.strip()
                        for code in raw_codes.split(",")
                        if code.strip()
                    ]
                else:
                    codes = _stream_default_codes(_workspace_project())
                try:
                    codes = [
                        normalize_security_code(code, "security code") for code in codes
                    ]
                except (TypeError, ValueError) as exc:
                    self._send_json({"error": f"codes 无效：{exc}"}, status=422)
                    return
                codes = list(dict.fromkeys(codes))
                if not codes:
                    self._send_json({"error": "codes 不能为空"}, status=400)
                    return
                if len(codes) > _STREAM_MAX_CODES:
                    self._send_json(
                        {"error": f"codes 数量超过上限 {_STREAM_MAX_CODES}"},
                        status=422,
                    )
                    return
                stream_service = _get_live_stream_service()
                try:
                    sub_id, hello_payload, hello_error = stream_service.subscribe(codes)
                except StreamCapacityError as exc:
                    self._send_json(
                        {
                            "error": str(exc),
                            "typedError": {
                                "code": "STREAM_CAPACITY",
                                "message": str(exc),
                                "retryable": True,
                            },
                        },
                        status=503,
                    )
                    return
                self._send_sse_stream(
                    stream_service,
                    sub_id,
                    codes,
                    hello_payload,
                    hello_error,
                    poll_seconds=stream_service.poll_interval,
                )
                return
            if parsed.path == "/api/projects":
                project = _workspace_project()
                self._send_json(_workspace_ui(project))
                return
            if parsed.path == "/api/decision-card":
                self._send_json(_decision_card_export())
                return
            if parsed.path == "/api/audit":
                limit = parse_qs(parsed.query).get("limit", ["60"])[0]
                try:
                    limit_int = int(limit)
                except ValueError:
                    limit_int = 60
                self._send_json(_audit_view(limit_int))
                return
            if parsed.path == "/api/metrics":
                limit = parse_qs(parsed.query).get("limit", ["100"])[0]
                try:
                    limit_int = int(limit)
                except ValueError:
                    limit_int = 100
                self._send_json(_metrics_view(limit_int))
                return
            if parsed.path == "/api/projects/discover":
                query = parse_qs(parsed.query)
                requested_symbol = str(query.get("symbol", [""])[0]).strip() or None
                requested_query = str(
                    query.get("q", query.get("query", [""]))[0]
                ).strip() or None
                try:
                    candidates = discover_project_assets(
                        query=requested_query,
                        symbol=requested_symbol,
                        root=ROOT,
                    )
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=422)
                    return
                self._send_json(
                    {
                        "query": requested_query,
                        "symbol": requested_symbol,
                        "candidates": candidates,
                    }
                )
                return
            if parsed.path == "/api/policy-library":
                self._send_json(_policy_library_ui())
                return
            self._send_static(parsed.path)
        except _LiveDataError as exc:
            self._send_live_error_json(exc)
        except Exception as exc:  # 服务端错误不外泄堆栈
            self._send_error_json(exc)

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler naming)
        parsed = urlparse(self.path)
        if not self._host_allowed():
            self._send_json({"error": "forbidden host"}, status=403)
            return
        if parsed.path not in (
            "/api/run",
            "/api/chat",
            "/api/command",
            "/api/agent",
            "/api/projects",
            "/api/projects/select",
        ):
            self._send_json({"error": "not found"}, status=404)
            return
        params = parse_qs(parsed.query)
        no_audit = params.get("no_audit", ["0"])[0] in ("1", "true")
        start = time.monotonic()
        try:
            if parsed.path == "/api/projects/select":
                body = self._read_json_body()
                project_id = str(body.get("projectId") or body.get("project_id") or "").strip()
                if not project_id:
                    self._send_json({"error": "projectId 不能为空"}, status=400)
                    return
                _set_active_project(project_id)
                project = _workspace_project()
                state = build_state_cached()
                self._finish_state(
                    _project_selection_state(project, state),
                    "projects/select",
                    project_id,
                    start,
                )
                return
            if parsed.path == "/api/projects":
                body = self._read_json_body()
                project = register_project(
                    name=str(body.get("name") or "").strip(),
                    symbol=str(body.get("symbol") or "").strip(),
                    input_path=(
                        str(body.get("inputPath") or body.get("input_path") or "").strip()
                        or None
                    ),
                    research_items_path=(
                        str(body.get("researchItemsPath") or body.get("research_items_path") or "").strip()
                        or None
                    ),
                    project_id=str(body.get("projectId") or body.get("project_id") or "").strip() or None,
                    data_mode=str(body.get("dataMode") or body.get("data_mode") or "").strip() or None,
                    registry_path=_workspace_registry_path(),
                    root=ROOT,
                )
                _set_active_project(project["id"])
                state = build_state_cached()
                self._finish_state(
                    _project_registration_state(project, state),
                    "projects",
                    str(body.get("symbol") or ""),
                    start,
                )
                return
            if parsed.path == "/api/chat":
                body = self._read_json_body()
                message = str(body.get("message", "")).strip()
                if not message:
                    self._send_json({"error": "message 不能为空"}, status=400)
                    return
                active_project = _workspace_project()
                input_path = Path(active_project["input_path"])
                if _data_mode(active_project) is DataMode.LIVE:
                    snapshot_data = _build_live_snapshot_for_project(
                        active_project, force=True
                    )
                    state = run_chat(
                        message,
                        snapshot_data,
                        audit_enabled=False,
                        project=active_project,
                        input_path=input_path,
                        snapshot_data=snapshot_data,
                        write_card=False,
                    )
                else:
                    snapshot = load_frozen_snapshot(input_path)
                    state = run_chat(
                        message,
                        snapshot,
                        audit_enabled=not no_audit,
                        project=active_project,
                        input_path=input_path,
                    )
                invalidate_state_cache()
                self._finish_state(state, "chat", message, start)
                return
            if parsed.path == "/api/agent":
                body = self._read_json_body()
                state = run_agent_action(body, audit_enabled=not no_audit)
                self._finish_state(state, "agent", str(body.get("message") or body.get("action") or ""), start)
                return
            if parsed.path == "/api/command":
                body = self._read_json_body()
                command = str(body.get("command", "")).strip()
                if not command:
                    self._send_json({"error": "command 不能为空"}, status=400)
                    return

                normalized = re.sub(r"\s+", " ", command).upper()
                command_project = _project_for_command(normalized)
                if command_project is not None:
                    _set_active_project(command_project["id"])
                    state = build_state_cached()
                elif normalized in {"REFRESH", "F5", "RUN", "RUN <GO>"}:
                    active_project = _workspace_project()
                    snapshot_data = None
                    write_card = not no_audit
                    live_mode = _data_mode(active_project) is DataMode.LIVE
                    if live_mode:
                        snapshot_data = _build_live_snapshot_for_project(
                            active_project, force=True
                        )
                        write_card = False
                    state = _pipeline_state(
                        active_project,
                        audit_enabled=(not no_audit) and not live_mode,
                        write_card=write_card,
                        snapshot_data=snapshot_data,
                    )
                    invalidate_state_cache()
                else:
                    active_project = _workspace_project()
                    input_path = Path(active_project["input_path"])
                    if _data_mode(active_project) is DataMode.LIVE:
                        snapshot_data = _build_live_snapshot_for_project(
                            active_project, force=True
                        )
                        state = run_chat(
                            command,
                            snapshot_data,
                            audit_enabled=False,
                            project=active_project,
                            input_path=input_path,
                            snapshot_data=snapshot_data,
                            write_card=False,
                        )
                    else:
                        snapshot = load_frozen_snapshot(input_path)
                        state = run_chat(
                            command,
                            snapshot,
                            audit_enabled=not no_audit,
                            project=active_project,
                            input_path=input_path,
                        )
                    invalidate_state_cache()
                if state.get("terminal") is not None and state.get("error") is None and state.get("typedError") is None:
                    state["terminal"]["lastCommand"] = command
                self._finish_state(state, "command", command, start)
                return
            active_project = _workspace_project()
            snapshot_data = None
            write_card = not no_audit
            live_mode = _data_mode(active_project) is DataMode.LIVE
            if live_mode:
                snapshot_data = _build_live_snapshot_for_project(
                    active_project, force=True
                )
                write_card = False
            state = _pipeline_state(
                active_project,
                audit_enabled=(not no_audit) and not live_mode,
                write_card=write_card,
                snapshot_data=snapshot_data,
            )
            invalidate_state_cache()
            self._finish_state(state, "run", "run", start)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=422)
        except _LiveDataError as exc:
            self._send_live_error_json(exc)
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
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print("=" * 78)
    print("GOAI 期权研究终端服务")
    print(f"打开：{url}")
    print("API：/api/state（状态）、/api/policy-library（事件库）、POST /api/run（重跑管线）、")
    print("     POST /api/chat（场景解析 + 五阶段管线 + 十角色辩论；无 DeepSeek key 自动离线回退）")
    print("     POST /api/agent（对话、场景重算、到期选择与刷新动作）")
    print("     /api/stream（LIVE 模式 SSE 实时推送：quote/error/refresh 事件 + 心跳）")
    print("免责声明：决策支持/研究用途，非投资建议；默认模拟盘，任何订单须人机确认。")
    print("=" * 78)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        _close_live_stream()
        server.server_close()


if __name__ == "__main__":
    main()
