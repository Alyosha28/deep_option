"""ReplayAdapter：演示模式数据回放，接口形状与 FutuAdapter 保持一致。"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional

from .models import Snapshot
from .snapshot_recorder import DEFAULT_DIR, iter_records


class ReplayAdapter:
    """从快照 JSONL 回放数据；找不到数据时抛出 FileNotFoundError。"""

    def __init__(self, snapshot_dir=DEFAULT_DIR):
        self.snapshot_dir = snapshot_dir

    def _latest(self, api: str, code: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None) -> Snapshot:
        latest: Optional[Snapshot] = None
        for path in sorted(self.snapshot_dir.glob("*.jsonl")):
            for snap in iter_records(path):
                if snap.payload.get("api") != api:
                    continue
                if code and snap.payload.get("code") != code:
                    continue
                latest = snap
        if latest is None:
            raise FileNotFoundError(f"快照中未找到 {api}(code={code})，请先用 FutuAdapter + SnapshotRecorder 采集。")
        return latest

    def get_expiration_dates(self, code: str) -> Snapshot:
        return self._latest("get_option_expiration_date", code=code)

    def get_option_chain(self, code: str, start=None, end=None, option_type=None, option_cond_type=None) -> Snapshot:
        return self._latest("get_option_chain", code=code)

    def resolve_option_code(self, underlying: str, expiry: str, strike: float, option_type: str) -> Snapshot:
        chain = self.get_option_chain(underlying)
        rows = chain.payload.get("data", [])
        matched = [
            row
            for row in rows
            if str(row.get("option_type", "")).upper() == option_type.upper()
            and abs(float(row.get("strike_price", 0)) - strike) < 0.001
        ]
        return Snapshot(
            source="replay",
            payload={
                "api": "resolve_option_code",
                "underlying": underlying,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
                "matched": matched,
            },
        )

    def get_market_snapshot(self, codes: Iterable[str]) -> Snapshot:
        return self._latest("get_market_snapshot")
