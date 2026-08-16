"""对话场景解析：把自然语言请求结构化成交给决策管线的 scenario 字典。

这是 futu-options-agent 工作流「阶段 1 场景解析」的离线确定性切片：
- 只做文本结构化，不产生任何金融数字；现金/风险预算等数字只从用户输入
  中提取，缺失字段显式标注为「按冻结快照假定」；
- 标的从通用市场前缀代码或当前快照名称解析，不把某个公司写死为唯一入口；
- view 只用于记录用户观点，P0 策略仍为跨式（方向中性），解析结果会如实说明。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

SYMBOL_ALIASES: dict[str, str] = {
    "腾讯": "HK.00700",
    "0700": "HK.00700",
    "hk.00700": "HK.00700",
    "00700": "HK.00700",
    "英伟达": "US.NVDA",
    "nvda": "US.NVDA",
    "us.nvda": "US.NVDA",
    "苹果": "US.AAPL",
    "aapl": "US.AAPL",
    "us.aapl": "US.AAPL",
}

VIEW_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bullish": ("看多", "看涨", "上涨", "做多", "bullish", "买入看涨"),
    "bearish": ("看空", "看跌", "下跌", "做空", "bearish", "买入看跌"),
    "uncertain": ("不确定", "方向不明", "震荡", "跨式", "uncertain", "straddle"),
}

_SYMBOL_PATTERN = re.compile(
    r"(?<![A-Z0-9_])([A-Z][A-Z0-9_-]{1,15}\.[A-Z0-9][A-Z0-9._-]{0,46})"
    r"(?![A-Z0-9._-])",
    re.IGNORECASE,
)
_NUMERIC_DISPLAY_PATTERN = re.compile(
    r"(?<![A-Z0-9._-])(?:(\d{4,8})\s+([A-Z][A-Z0-9_-]{1,15})"
    r"|([A-Z][A-Z0-9_-]{1,15})\s+(\d{4,8}))(?![A-Z0-9._-])",
    re.IGNORECASE,
)
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_CASH_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[万kK千]")
_CASH_RAW_PATTERN = re.compile(r"(?<![%.\d])(\d{4,})(?!\d)")
_HORIZON_DAYS_PATTERN = re.compile(r"(\d+)\s*(?:天|日|dte|DTE)")


def _looks_like_earnings(text: str) -> bool:
    return any(keyword in text.lower() for keyword in ("业绩", "财报", "earnings"))


def _extract_underlying(
    text: str,
    snapshot_payload: Mapping[str, Any] | None = None,
) -> str | None:
    lowered = text.lower()
    match = _SYMBOL_PATTERN.search(text)
    if match:
        return match.group(1).upper()
    display = _NUMERIC_DISPLAY_PATTERN.search(text)
    if display:
        code, market = display.group(1), display.group(2)
        if not code or not market:
            market, code = display.group(3), display.group(4)
        if market and code:
            return f"{market.upper()}.{code.upper()}"
    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in lowered:
            return symbol
    if isinstance(snapshot_payload, Mapping):
        underlying = str(snapshot_payload.get("underlying") or "").strip().upper()
        names: list[str] = []
        for key in ("name", "company", "company_name"):
            value = snapshot_payload.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
        meta = snapshot_payload.get("meta")
        if isinstance(meta, Mapping):
            for key in ("name", "company", "company_name"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
        if underlying and any(name.casefold() in lowered for name in names):
            return underlying
    return None


def _extract_view(text: str) -> str:
    lowered = text.lower()
    for view, keywords in VIEW_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return view
    return "uncertain"


def _extract_cash(text: str) -> float | None:
    match = _CASH_UNIT_PATTERN.search(text)
    if match:
        value = float(match.group(1))
        unit = match.group(0)[-1]
        if unit in ("万",):
            return value * 10_000.0
        if unit in ("千", "k", "K"):
            return value * 1_000.0
    match = _CASH_RAW_PATTERN.search(text)
    if match:
        return float(match.group(1))
    return None


def _extract_budget(text: str) -> float | None:
    match = _PERCENT_PATTERN.search(text)
    if match:
        return float(match.group(1))
    return None


def _extract_horizon(text: str, earnings_date: str) -> str:
    if _looks_like_earnings(text):
        return f"{earnings_date} 业绩"
    match = _HORIZON_DAYS_PATTERN.search(text)
    if match:
        return f"{match.group(1)} 天"
    return f"{earnings_date} 业绩"


def parse_message(
    message: str,
    snapshot_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """把自然语言消息解析为 scenario + 假定/说明清单；解析失败抛 ValueError。"""

    text = str(message).strip()
    if not text:
        raise ValueError("消息为空，请描述标的、期限与账户约束。")

    underlying = _extract_underlying(text, snapshot_payload)
    if underlying is None:
        raise ValueError(
            "无法识别标的；请写明市场代码或当前项目名称，例如 HK.00700 / 公司名称。"
        )

    account = snapshot_payload.get("account")
    earnings = snapshot_payload.get("earnings")
    if not isinstance(account, dict) or not isinstance(earnings, dict):
        raise ValueError("冻结快照缺少 account/earnings 字段。")

    cash = _extract_cash(text)
    budget = _extract_budget(text)
    assumed: list[str] = []
    if cash is None:
        cash = float(account["cash_hkd"])
        assumed.append("account_cash_hkd")
    if budget is None:
        budget = float(account["risk_budget_pct"])
        assumed.append("risk_budget_pct")

    view = _extract_view(text)
    horizon = _extract_horizon(text, str(earnings.get("date", "")))
    constraints = [f"单笔最多亏损 {budget:g}%"]

    notes = [
        "view 仅用于记录用户观点；P0 策略仍为跨式（方向中性），不做方向性腿。"
    ]
    if assumed:
        notes.append("缺失字段按冻结快照假定：" + "、".join(assumed) + "。")
    if underlying != snapshot_payload.get("underlying"):
        notes.append(
            f"标的 {underlying} 不在当前冻结快照（{snapshot_payload.get('underlying')}），"
            "决策管线将拒绝执行。"
        )

    return {
        "scenario": {
            "underlying": underlying,
            "view": view,
            "horizon": horizon,
            "account_cash_hkd": cash,
            "risk_budget_pct": budget,
            "constraints": constraints,
        },
        "assumed": assumed,
        "notes": notes,
        "raw_underlying": underlying,
    }
