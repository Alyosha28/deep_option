"""Killable, fixed-schema worker for Futu account reads.

The installed Futu trade context can retry synchronously forever during
construction.  This module is launched as a short-lived child process so the
parent gateway can enforce a hard deadline without exposing an SDK context or
arbitrary method dispatch.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import ipaddress
import json
import math
import sys
from typing import Any, Mapping


_MAX_INPUT_BYTES = 4_096
_MAX_ROWS = 10_000
_MAX_TEXT_LENGTH = 4_096
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_ACCOUNT_FIELDS = {
    "total_assets": "total_assets",
    "cash": "cash",
    "available_funds": "available_funds",
    "power": "power",
    "initial_margin": "initial_margin",
    "maintenance_margin": "maintenance_margin",
    "risk_status": "risk_status",
    "currency": "currency",
}
_POSITION_FIELDS = {
    "code": "code",
    "stock_name": "name",
    "qty": "quantity",
    "can_sell_qty": "sellable_quantity",
    "average_cost": "average_cost",
    "nominal_price": "mark_price",
    "market_val": "market_value",
    "unrealized_pl": "unrealized_pnl",
    "pl_ratio_avg_cost": "unrealized_pnl_percent",
    "strategy_type": "strategy_type",
    "position_type": "position_type",
    "currency": "currency",
}


def _error(code: str = "ACCOUNT_UNAVAILABLE") -> dict[str, Any]:
    return {"ok": False, "error_code": code}


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


def _records(raw: Any) -> list[dict[str, Any]]:
    try:
        size = len(raw)
    except (AttributeError, TypeError):
        size = None
    if size is not None and size > _MAX_ROWS:
        raise ValueError("response row limit exceeded")
    if hasattr(raw, "to_dict"):
        rows = raw.to_dict(orient="records")
    elif isinstance(raw, Mapping):
        rows = [raw]
    elif isinstance(raw, (list, tuple)):
        rows = raw
    else:
        raise ValueError("response is not tabular")
    if len(rows) > _MAX_ROWS or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("response rows are invalid")
    return [dict(row) for row in rows]


def _whitelist(
    rows: list[dict[str, Any]], fields: Mapping[str, str]
) -> list[dict[str, Any]]:
    output = [
        {
            target: _normal(row[source])
            for source, target in fields.items()
            if source in row
        }
        for row in rows
    ]
    for row in output:
        if isinstance(row.get("code"), str):
            row["code"] = row["code"].upper()
        if isinstance(row.get("currency"), str):
            row["currency"] = row["currency"].upper()
    return output


def query_account(payload: Any) -> dict[str, Any]:
    """Execute the two fixed account reads and return only public allowlisted rows."""

    expected = {
        "host",
        "port",
        "acc_id",
        "trd_env",
        "currency",
        "security_firm",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error()
    try:
        host = ipaddress.ip_address(payload["host"])
        port = payload["port"]
        acc_id = payload["acc_id"]
        if (
            not host.is_loopback
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65_535
            or isinstance(acc_id, bool)
            or not isinstance(acc_id, int)
            or acc_id <= 0
        ):
            return _error()
        from futu import Currency, OpenSecTradeContext, SecurityFirm, TrdEnv, TrdMarket

        names = {
            "security_firm": (SecurityFirm, payload["security_firm"]),
            "trd_env": (TrdEnv, payload["trd_env"]),
            "currency": (Currency, payload["currency"]),
        }
        resolved: dict[str, Any] = {}
        for name, (enum_type, raw_name) in names.items():
            if not isinstance(raw_name, str) or not hasattr(enum_type, raw_name):
                return _error("SDK_INCOMPATIBLE")
            resolved[name] = getattr(enum_type, raw_name)
        if not hasattr(TrdMarket, "NONE"):
            return _error("SDK_INCOMPATIBLE")

        context = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE,
            host=str(host),
            port=port,
            security_firm=resolved["security_firm"],
        )
        try:
            setter = getattr(context, "set_sync_query_connect_timeout", None)
            if not callable(setter):
                return _error("SDK_INCOMPATIBLE")
            setter(5.0)
            kwargs = {
                "trd_env": resolved["trd_env"],
                "acc_id": acc_id,
                "refresh_cache": True,
                "currency": resolved["currency"],
            }
            info_ret, info_raw = context.accinfo_query(**kwargs)
            if info_ret != 0:
                text = str(info_raw).lower()
                denied = any(
                    token in text
                    for token in ("permission", "right", "authority", "entitlement")
                )
                return _error("ENTITLEMENT_DENIED" if denied else "ACCOUNT_UNAVAILABLE")
            position_ret, position_raw = context.position_list_query(**kwargs)
            if position_ret != 0:
                return _error("ACCOUNT_UNAVAILABLE")
            return {
                "ok": True,
                "info": _whitelist(_records(info_raw), _ACCOUNT_FIELDS),
                "positions": _whitelist(_records(position_raw), _POSITION_FIELDS),
            }
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
        # Keep SDK console output physically separate from the JSON protocol.
        with redirect_stdout(sys.stderr):
            result = query_account(payload)
    encoded = json.dumps(
        result, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        encoded = b'{"ok":false,"error_code":"ACCOUNT_UNAVAILABLE"}'
    protocol_stdout.buffer.write(encoded + b"\n")
    protocol_stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
