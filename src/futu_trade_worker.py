"""Killable, fixed-schema worker for simulated (SIMULATE) option order placement.

P0c 模拟提交闭环的交易侧边界：

- 交易环境在本模块内**硬编码 SIMULATE**，绝不接受输入指定（实盘无法经此
  边界触发）；不调用、不引入任何 unlock_trade 接口（交易解锁必须在
  OpenD GUI 手动完成，见项目 install-futu-opend 规则）。
- 短生命周期子进程 + 硬截止（父侧 20s）+ 固定 schema 输出，只允许
  BUY 腿、至多 2 条（跨式两腿），输出只含 order_id/code/qty/price/status。
"""

from __future__ import annotations

from contextlib import redirect_stdout
import ipaddress
import json
import math
import sys
from typing import Any, Mapping

_MAX_INPUT_BYTES = 4_096
_MAX_ORDERS = 2
_MAX_QTY = 10_000
_MAX_TEXT_LENGTH = 4_096
_MAX_OUTPUT_BYTES = 256 * 1024

_ORDER_FIELDS = {
    "order_id": "order_id",
    "code": "code",
    "qty": "qty",
    "price": "price",
    "order_status": "status",
}


def _error(code: str = "UPSTREAM_ERROR") -> dict[str, Any]:
    return {"ok": False, "error_code": code}


def _records(raw: Any) -> list[dict[str, Any]]:
    if hasattr(raw, "to_dict"):
        rows = raw.to_dict(orient="records")
    elif isinstance(raw, Mapping):
        rows = [raw]
    elif isinstance(raw, (list, tuple)):
        rows = raw
    else:
        raise ValueError("response is not tabular")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("response rows are invalid")
    return [dict(row) for row in rows]


def _normal(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        value = value.strip()
        if len(value) > _MAX_TEXT_LENGTH:
            raise ValueError("response text limit exceeded")
        return None if value.upper() in {"", "N/A", "NA", "NONE", "NULL", "--"} else value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    text = str(value)
    if len(text) > _MAX_TEXT_LENGTH:
        raise ValueError("response text limit exceeded")
    return text


def _failure_code(text: str) -> str:
    lowered = str(text or "").lower()
    if any(token in lowered for token in ("unlock", "解锁")):
        return "TRADE_UNLOCK_REQUIRED"
    if any(
        token in lowered
        for token in ("permission", "authority", "entitlement", "right", "权限", "未开通")
    ):
        return "ENTITLEMENT_DENIED"
    if any(token in lowered for token in ("login", "登录", "not signed")):
        return "ACCOUNT_UNAVAILABLE"
    return "UPSTREAM_ERROR"


def place_orders(payload: Any) -> dict[str, Any]:
    """Submit at most two BUY legs on the SIMULATE environment (never REAL)."""

    expected = {"host", "port", "acc_id", "security_firm", "orders"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error()
    try:
        host = ipaddress.ip_address(payload["host"])
        port = payload["port"]
        acc_id = payload["acc_id"]
        orders = payload["orders"]
        if (
            not host.is_loopback
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65_535
            or isinstance(acc_id, bool)
            or not isinstance(acc_id, int)
            or acc_id <= 0
            or not isinstance(orders, list)
            or not 1 <= len(orders) <= _MAX_ORDERS
        ):
            return _error("INVALID_REQUEST")
        normalized_orders: list[dict[str, Any]] = []
        for order in orders:
            if not isinstance(order, dict):
                return _error("INVALID_REQUEST")
            code = order.get("code")
            qty = order.get("qty")
            price = order.get("price")
            if (
                not isinstance(code, str)
                or "." not in code
                or isinstance(qty, bool)
                or not isinstance(qty, int)
                or not 1 <= qty <= _MAX_QTY
                or isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or float(price) <= 0
            ):
                return _error("INVALID_REQUEST")
            normalized_orders.append(
                {"code": code.strip().upper(), "qty": qty, "price": float(price)}
            )

        from futu import (
            OrderType,
            OpenSecTradeContext,
            SecurityFirm,
            TimeInForce,
            TrdEnv,
            TrdMarket,
            TrdSide,
        )

        firm_name = str(payload["security_firm"]).strip().upper()
        if firm_name not in ("FUTUSECURITIES",):
            return _error("SDK_INCOMPATIBLE")

        context = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE,
            host=str(host),
            port=port,
            security_firm=getattr(SecurityFirm, firm_name),
        )
        try:
            setter = getattr(context, "set_sync_query_connect_timeout", None)
            if not callable(setter):
                return _error("SDK_INCOMPATIBLE")
            setter(5.0)
            receipts: list[dict[str, Any]] = []
            for order in normalized_orders:
                ret, raw = context.place_order(
                    price=order["price"],
                    qty=order["qty"],
                    code=order["code"],
                    trd_side=TrdSide.BUY,
                    order_type=OrderType.NORMAL,
                    adjust_limit=0.1,
                    trd_env=TrdEnv.SIMULATE,
                    acc_id=acc_id,
                    remark="GOAI P0c simulated",
                    time_in_force=TimeInForce.DAY,
                )
                if ret != 0:
                    return _error(_failure_code(str(raw)))
                rows = _records(raw)
                if len(rows) != 1:
                    return _error("SCHEMA_MISMATCH")
                row = {
                    target: _normal(rows[0].get(source))
                    for source, target in _ORDER_FIELDS.items()
                    if source in rows[0]
                }
                if row.get("order_id") is None:
                    return _error("SCHEMA_MISMATCH")
                if isinstance(row.get("code"), str):
                    row["code"] = row["code"].upper()
                receipts.append(row)
            return {"ok": True, "orders": receipts}
        finally:
            try:
                context.close()
            except Exception:
                pass
    except Exception:
        return _error()


def main() -> int:
    protocol_stdout = sys.stdout
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        result = _error()
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            payload = None
        with redirect_stdout(sys.stderr):
            result = place_orders(payload)
    encoded = json.dumps(
        result, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        encoded = b'{"ok":false,"error_code":"UPSTREAM_ERROR"}'
    protocol_stdout.buffer.write(encoded + b"\n")
    protocol_stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
