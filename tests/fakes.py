from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class FakeFrame:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = records

    def __len__(self) -> int:
        return len(self._records)

    def to_dict(self, orient: str | None = None):
        if orient == "records":
            return [dict(row) for row in self._records]
        if orient is None and len(self._records) == 1:
            return dict(self._records[0])
        raise ValueError(f"unsupported orient: {orient}")


class FakeQuoteContext:
    def __init__(self, *, snapshot_ret: int = 0, snapshot_data: Any = None):
        self.snapshot_ret = snapshot_ret
        self._dynamic_snapshot = snapshot_data is None
        self.snapshot_data = snapshot_data or FakeFrame(
            [
                {
                    "code": "HK.00700",
                    "name": "Tencent",
                    "update_time": "2026-08-12T02:00:00+00:00",
                    "last_price": 500.0,
                    "open_price": 498.0,
                    "high_price": 505.0,
                    "low_price": 495.0,
                    "prev_close_price": 497.0,
                    "volume": 1000,
                    "turnover": 500000.0,
                    "bid_price": 499.8,
                    "ask_price": 500.2,
                    "bid_vol": 10,
                    "ask_vol": 12,
                    "lot_size": 100,
                    "delta": 0.51,
                    "acc_id": 999,
                }
            ]
        )
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def set_sync_query_connect_timeout(self, timeout):
        self.calls.append(("set_sync_query_connect_timeout", timeout))

    def get_global_state(self):
        self.calls.append(("get_global_state", None))
        return 0, {
            "server_ver": "10.9.6918",
            "timestamp": "2026-08-12T02:00:00+00:00",
            "qot_logined": 1,
            "trd_logined": 1,
        }

    def get_market_state(self, codes):
        self.calls.append(("get_market_state", list(codes)))
        return 0, FakeFrame([{"code": code, "market_state": "MORNING"} for code in codes])

    def request_trading_days(self, **kwargs):
        self.calls.append(("request_trading_days", dict(kwargs)))
        return 0, [{"time": "2026-08-12", "trade_date_type": "WHOLE"}]

    def get_market_snapshot(self, codes):
        self.calls.append(("get_market_snapshot", list(codes)))
        if self._dynamic_snapshot:
            rows = []
            for code in codes:
                row = {
                    "code": code,
                    "name": code,
                    "update_time": "2026-08-12T02:00:00+00:00",
                    "last_price": 500.0,
                    "open_price": 498.0,
                    "high_price": 505.0,
                    "low_price": 495.0,
                    "prev_close_price": 497.0,
                    "volume": 1000,
                    "turnover": 500000.0,
                    "bid_price": 499.8,
                    "ask_price": 500.2,
                    "bid_vol": 10,
                    "ask_vol": 12,
                    "lot_size": 100,
                }
                upper_code = code.upper()
                if "CALL" in upper_code or "PUT" in upper_code:
                    row.update(
                        {
                            "last_price": 12.0,
                            "option_type": "PUT" if "PUT" in upper_code else "CALL",
                            "strike_time": "2026-08-28",
                            "option_strike_price": 500.0,
                            "option_contract_size": 100.0,
                        }
                    )
                rows.append(row)
            return self.snapshot_ret, FakeFrame(rows)
        return self.snapshot_ret, self.snapshot_data

    def get_option_expiration_date(self, code):
        self.calls.append(("get_option_expiration_date", code))
        return 0, FakeFrame(
            [{"strike_time": "2026-08-28", "option_expiry_date_distance": 16, "expiration_cycle": "MONTH"}]
        )

    def get_option_chain(self, code, **kwargs):
        self.calls.append(("get_option_chain", (code, dict(kwargs))))
        return 0, FakeFrame(
            [
                {
                    "code": "HK.TCH260828C500000",
                    "name": "Tencent Call",
                    "lot_size": 100,
                    "option_type": "CALL",
                    "stock_owner": code,
                    "strike_time": "2026-08-28",
                    "strike_price": 500.0,
                    "option_standard_type": "STANDARD",
                    "option_settlement_mode": "PHYSICAL",
                }
            ]
        )

    def get_option_quote(self, legs):
        self.calls.append(("get_option_quote", list(legs)))
        return 0, FakeFrame(
            [
                {
                    "price": 12.0,
                    "option_type": "PUT" if "PUT" in leg.code.upper() else "CALL",
                    "expire_time": "2026-08-28",
                    "strike_price": 500.0,
                    "contract_size": 100.0,
                    "option_delta": 0.5,
                }
                for leg in legs
            ]
        )

    def get_option_strategy_analysis(self, legs):
        self.calls.append(("get_option_strategy_analysis", list(legs)))
        return 0, FakeFrame(
            [{"code": "strategy", "name": "straddle", "bid1": 19.5, "ask1": 20.5, "delta": 0.0}]
        )

    def close(self):
        self.closed = True


class FakeAccountContext:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def set_sync_query_connect_timeout(self, timeout):
        self.calls.append(("set_sync_query_connect_timeout", {"timeout": timeout}))

    def accinfo_query(self, **kwargs):
        self.calls.append(("accinfo_query", dict(kwargs)))
        return 0, FakeFrame(
            [
                {
                    "acc_id": 987654321,
                    "card_num": "secret-card",
                    "total_assets": 100000.0,
                    "cash": 80000.0,
                    "available_funds": 75000.0,
                    "power": 90000.0,
                    "initial_margin": "N/A",
                    "maintenance_margin": 1000.0,
                    "risk_status": "SAFE",
                    "currency": "HKD",
                }
            ]
        )

    def position_list_query(self, **kwargs):
        self.calls.append(("position_list_query", dict(kwargs)))
        return 0, FakeFrame(
            [
                {
                    "acc_id": 987654321,
                    "position_id": 123,
                    "code": "HK.00700",
                    "stock_name": "Tencent",
                    "qty": 100.0,
                    "can_sell_qty": 100.0,
                    "average_cost": 450.0,
                    "nominal_price": 500.0,
                    "market_val": 50000.0,
                    "unrealized_pl": 5000.0,
                    "pl_ratio_avg_cost": 11.11,
                    "strategy_type": "N/A",
                    "position_type": "LONG",
                    "currency": "HKD",
                }
            ]
        )

    def close(self):
        self.closed = True


@dataclass
class RecordingSink:
    envelopes: list[Any]

    def record(self, envelope, tag: str = "default"):
        self.envelopes.append(envelope)
        return None
