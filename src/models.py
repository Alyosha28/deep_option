"""统一数据模型：数据快照、报价、期权合约。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Snapshot:
    """带来源与捕获时间的数据快照（演示/审计的最小单位）。"""

    source: str
    payload: Dict[str, Any]
    captured_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class Quote:
    """正股/标的统一报价模型。"""

    code: str
    last_price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[int] = None
    turnover: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    source: str = "futuapi"
    captured_at: str = field(default_factory=utc_now_iso)


@dataclass
class OptionContract:
    """期权合约统一模型。

    注意：Greeks 只能由自研引擎填充（bump-and-reprice），本模型不保存
    第三方 API 返回的 Greeks，避免演示口径被外部数值污染。
    """

    code: str
    name: Optional[str] = None
    option_type: Optional[str] = None  # CALL / PUT
    strike_price: Optional[float] = None
    strike_time: Optional[str] = None  # 到期日 yyyy-MM-dd
    last_price: Optional[float] = None
    implied_volatility: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    source: str = "futuapi"
    captured_at: str = field(default_factory=utc_now_iso)
