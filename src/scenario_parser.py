"""对话场景解析：把自然语言请求结构化成交给决策管线的 scenario 字典。

这是 futu-options-agent 工作流「阶段 1 场景解析」的离线确定性切片：
- 只做文本结构化，不产生任何金融数字；现金/风险预算等数字只从用户输入
  中提取，缺失字段显式标注为「按冻结快照假定」；
- 当前 P0 只有 HK.00700 冻结快照，其他标的由决策管线明确拒绝；
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

_SYMBOL_PATTERN = re.compile(r"(?:^|\s)([A-Z]{2}\.\d{5})(?:\s|$)")
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_CASH_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[万kK千]")
_CASH_RAW_PATTERN = re.compile(r"(?<![%.\d])(\d{4,})(?!\d)")
_HORIZON_DAYS_PATTERN = re.compile(r"(\d+)\s*(?:天|日|dte|DTE)")


def _looks_like_earnings(text: str) -> bool:
    return any(keyword in text.lower() for keyword in ("业绩", "财报", "earnings"))


def _extract_underlying(text: str) -> str | None:
    lowered = text.lower()
    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in lowered:
            return symbol
    match = _SYMBOL_PATTERN.search(text)
    if match:
        return match.group(1)
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

    underlying = _extract_underlying(text)
    if underlying is None:
        raise ValueError(
            "无法识别标的；请写明市场代码或名称，例如 HK.00700 / 腾讯。"
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
