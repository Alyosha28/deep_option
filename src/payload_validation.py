"""Pure operation-specific validation for gateway payloads.

The generic envelope contract proves serialization and integrity.  These checks
prove that an ``OK`` payload contains the minimum facts required by downstream
decision logic.  Live and Replay gateways share this module to prevent semantic
drift between data modes.
"""

from __future__ import annotations

from datetime import date
import math
import re
from typing import Any, Mapping, Optional, Sequence

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9_]*\.[A-Z0-9][A-Z0-9._-]*$")
_LIQUIDITY_FACTS = ("cash", "available_funds", "power")
_PUBLIC_OPERATIONS = {
    "health",
    "capabilities",
    "get_market_state",
    "get_trading_days",
    "get_market_snapshot",
    "get_expiration_dates",
    "get_option_chain",
    "resolve_option_code",
    "get_option_quotes",
    "get_strategy_quote",
    "get_account_risk_summary",
}
_SENSITIVE_KEYS = {
    "acc_id",
    "account_id",
    "api_key",
    "authorization",
    "card_num",
    "card_number",
    "client_secret",
    "cookie",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "token",
}
_MARKET_SNAPSHOT_FIELDS = {
    "code", "name", "updated_at", "last_price", "open", "high", "low",
    "previous_close", "volume", "turnover", "bid", "ask", "bid_size",
    "ask_size", "lot_size", "option_type", "underlying", "expiry", "strike",
    "contract_size", "open_interest", "net_open_interest", "implied_volatility",
    "exercise_type",
}
_OPTION_CHAIN_FIELDS = {
    "code", "name", "lot_size", "option_type", "underlying", "expiry", "strike",
    "standard_type", "settlement_mode",
}
_OPTION_QUOTE_FIELDS = {
    "code", "price", "mid_price", "change", "change_rate", "volume", "turnover",
    "high", "low", "option_type", "strike", "expiry", "implied_volatility",
    "open_interest", "contract_size", "exercise_type",
}
_STRATEGY_QUOTE_FIELDS = {
    "code", "name", "option_strategy", "bid1", "ask1", "max_profit", "max_loss",
    "breakeven_points", "probability_of_profit",
}
_ACCOUNT_FIELDS = {
    "account_ref", "currency", "total_assets", "cash", "available_funds", "power",
    "initial_margin", "maintenance_margin", "risk_status", "positions",
}
_POSITION_FIELDS = {
    "code", "name", "quantity", "sellable_quantity", "average_cost", "mark_price",
    "market_value", "unrealized_pnl", "unrealized_pnl_percent", "strategy_type",
    "position_type", "currency",
}


class PayloadValidationError(ValueError):
    """An operation payload is well-formed JSON but unusable for its contract."""


SENSITIVE_FIELD_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "private_key",
    "acc_id",
    "account_id",
    "order_id",
    "card_num",
)


def reject_sensitive_fields(
    raw: Mapping[str, Any],
    *,
    label: str,
    node_limit: int = 10_000,
) -> None:
    """递归拒绝键名包含敏感片段的对象。

    收敛各数据模块（decision_pipeline / macro_assessment / policy_library /
    research_evidence）此前各自复制的实现：行为与报错文案由 label 参数保持
    向后兼容。
    """
    stack = list(raw.items())
    visited = 0
    while stack:
        key, value = stack.pop()
        visited += 1
        if visited > node_limit:
            raise ValueError(f"{label} exceeds the metadata traversal limit")
        normalized = str(key).strip().lower()
        if any(fragment in normalized for fragment in SENSITIVE_FIELD_FRAGMENTS):
            raise ValueError(f"{label} contains a forbidden sensitive field: {key}")
        if isinstance(value, dict):
            stack.extend(value.items())
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    stack.append((f"{key}[{index}]", item))


def _reject_sensitive_keys(*values: Any) -> None:
    stack = list(values)
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > 50_000:
            raise PayloadValidationError("payload exceeds the validation node limit")
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).strip().lower() in _SENSITIVE_KEYS:
                    raise PayloadValidationError("payload contains a forbidden sensitive field")
                stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)


def _require_allowed_fields(
    row: Mapping[str, Any],
    allowed: set[str],
    domain: str,
) -> None:
    if not set(row).issubset(allowed):
        raise PayloadValidationError(f"{domain} contains fields outside the public schema")


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _nonnegative_number(value: Any) -> bool:
    return _finite_number(value) and float(value) >= 0


def _valid_symbol(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 64 and bool(_SYMBOL.fullmatch(value))


def _canonical_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _require_rows(
    data: Any,
    operation: str,
    *,
    expected: Optional[int] = None,
) -> Sequence[Any]:
    if not isinstance(data, list) or not data:
        raise PayloadValidationError(f"{operation} OK payload must contain rows")
    if expected is not None and len(data) != expected:
        raise PayloadValidationError(f"{operation} row count does not match the request")
    return data


def _require_code_rows(
    request: Mapping[str, Any],
    data: Any,
    operation: str,
) -> Sequence[Mapping[str, Any]]:
    codes = request.get("codes")
    if not isinstance(codes, list) or not codes or not all(_valid_symbol(code) for code in codes):
        raise PayloadValidationError(f"{operation} request has invalid codes")
    rows = _require_rows(data, operation, expected=len(codes))
    if not all(isinstance(row, dict) for row in rows):
        raise PayloadValidationError(f"{operation} rows must be objects")
    row_codes = [row.get("code") for row in rows]
    if not all(_valid_symbol(code) for code in row_codes):
        raise PayloadValidationError(f"{operation} row has an invalid code")
    if len(set(row_codes)) != len(row_codes) or set(row_codes) != set(codes):
        raise PayloadValidationError(f"{operation} row identity does not match the request")
    return rows


def _validate_health(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("ready"), bool):
        raise PayloadValidationError("health payload has no readiness fact")
    if "server_version" not in data or not (
        data["server_version"] is None or isinstance(data["server_version"], (str, int))
    ):
        raise PayloadValidationError("health payload has an invalid server version")
    _require_allowed_fields(
        data,
        {"ready", "server_version", "quote_logged_in", "account_logged_in", "fixture_count"},
        "health payload",
    )


def _validate_capabilities(data: Any) -> None:
    required = (
        "market_data",
        "account_read",
        "strategy_combination_quote",
        "execution",
        "real_trading",
    )
    if not isinstance(data, dict) or not all(isinstance(data.get(field), bool) for field in required):
        raise PayloadValidationError("capabilities payload is missing boolean capability facts")
    if data["execution"] or data["real_trading"]:
        raise PayloadValidationError("competition capabilities must remain read-only")
    _require_allowed_fields(data, {*required, "fixture_count"}, "capabilities payload")


def _validate_market_state(request: Mapping[str, Any], data: Any) -> None:
    rows = _require_code_rows(request, data, "get_market_state")
    for row in rows:
        _require_allowed_fields(row, {"code", "market_state"}, "market-state row")
    if any(not isinstance(row.get("market_state"), str) or not row["market_state"] for row in rows):
        raise PayloadValidationError("market-state row has no state")


def _validate_market_snapshot(request: Mapping[str, Any], data: Any) -> None:
    rows = _require_code_rows(request, data, "get_market_snapshot")
    for row in rows:
        _require_allowed_fields(row, _MARKET_SNAPSHOT_FIELDS, "market-snapshot row")
    if any(not _nonnegative_number(row.get("last_price")) for row in rows):
        raise PayloadValidationError("market-snapshot row has no finite last price")


def _validate_trading_days(request: Mapping[str, Any], data: Any) -> None:
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise PayloadValidationError("trading-days payload must be a list of objects")
    start = _canonical_date(request.get("start"))
    end = _canonical_date(request.get("end"))
    if start is None or end is None or end < start:
        raise PayloadValidationError("trading-days request has an invalid date window")
    seen: set[date] = set()
    for row in data:
        _require_allowed_fields(row, {"date", "trade_date_type"}, "trading-days row")
        trading_date = _canonical_date(row.get("date"))
        if trading_date is None or not start <= trading_date <= end or trading_date in seen:
            raise PayloadValidationError("trading-days row has an invalid date")
        seen.add(trading_date)


def _validate_expiration_dates(request: Mapping[str, Any], data: Any) -> None:
    if not _valid_symbol(request.get("underlying")):
        raise PayloadValidationError("expiration-date request has an invalid underlying")
    rows = _require_rows(data, "get_expiration_dates")
    expirations: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PayloadValidationError("expiration-date rows must be objects")
        _require_allowed_fields(
            row,
            {"expiry", "days_to_expiry", "expiration_cycle"},
            "expiration-date row",
        )
        expiry = _canonical_date(row.get("expiry"))
        if expiry is None or expiry in expirations:
            raise PayloadValidationError("expiration-date row has an invalid expiry")
        expirations.add(expiry)


def _validate_option_chain(request: Mapping[str, Any], data: Any) -> None:
    rows = _require_rows(data, "get_option_chain")
    wanted_underlying = request.get("underlying")
    start = _canonical_date(request.get("start"))
    end = _canonical_date(request.get("end"))
    if not _valid_symbol(wanted_underlying) or start is None or end is None:
        raise PayloadValidationError("option-chain request identity is invalid")
    seen_codes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PayloadValidationError("option-chain rows must be objects")
        _require_allowed_fields(row, _OPTION_CHAIN_FIELDS, "option-chain row")
        expiry = _canonical_date(row.get("expiry"))
        if not _valid_symbol(row.get("code")) or not _valid_symbol(row.get("underlying")):
            raise PayloadValidationError("option-chain row has an invalid symbol")
        if row["code"] in seen_codes:
            raise PayloadValidationError("option-chain payload contains duplicate contracts")
        seen_codes.add(row["code"])
        if row.get("underlying") != wanted_underlying:
            raise PayloadValidationError("option-chain underlying does not match the request")
        option_type = str(row.get("option_type")).upper()
        if option_type not in {"CALL", "PUT"}:
            raise PayloadValidationError("option-chain row has an invalid option type")
        requested_type = request.get("option_type")
        if requested_type in {"CALL", "PUT"} and row.get("option_type") != requested_type:
            raise PayloadValidationError("option-chain row does not match the requested type")
        if expiry is None or not start <= expiry <= end:
            raise PayloadValidationError("option-chain row has an invalid expiry")
        if not _finite_number(row.get("strike")) or float(row["strike"]) <= 0:
            raise PayloadValidationError("option-chain row has an invalid strike")


def _validate_option_quotes(request: Mapping[str, Any], data: Any) -> None:
    legs = request.get("legs")
    if not isinstance(legs, list) or not legs:
        raise PayloadValidationError("option-quote request has no legs")
    rows = _require_rows(data, "get_option_quotes", expected=len(legs))
    for row, leg in zip(rows, legs):
        if not isinstance(row, dict) or not isinstance(leg, dict):
            raise PayloadValidationError("option-quote rows and legs must be objects")
        _require_allowed_fields(row, _OPTION_QUOTE_FIELDS, "option-quote row")
        if not _valid_symbol(leg.get("code")) or row.get("code") != leg.get("code"):
            raise PayloadValidationError("option-quote row does not match its requested leg")
        if not any(
            _nonnegative_number(row.get(field)) for field in ("price", "mid_price")
        ):
            raise PayloadValidationError("option-quote row has no usable price")


def _validate_strategy_quote(data: Any) -> None:
    rows = _require_rows(data, "get_strategy_quote", expected=1)
    row = rows[0]
    if isinstance(row, dict):
        _require_allowed_fields(row, _STRATEGY_QUOTE_FIELDS, "strategy-quote row")
    if not isinstance(row, dict) or not all(
        _finite_number(row.get(field)) for field in ("bid1", "ask1")
    ):
        raise PayloadValidationError("strategy quote is missing a usable bid or ask")
    if float(row["bid1"]) > float(row["ask1"]):
        raise PayloadValidationError("strategy quote has a crossed bid and ask")


def _validate_resolved_option(request: Mapping[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        raise PayloadValidationError("resolved option payload must be an object")
    _require_allowed_fields(data, _OPTION_CHAIN_FIELDS, "resolved-option payload")
    if not _valid_symbol(data.get("code")) or data.get("underlying") != request.get("underlying"):
        raise PayloadValidationError("resolved option has invalid contract identity")
    if data.get("expiry") != request.get("expiry") or data.get("option_type") != request.get(
        "option_type"
    ):
        raise PayloadValidationError("resolved option does not match expiry or type")
    if not _finite_number(data.get("strike")) or not _finite_number(request.get("strike")):
        raise PayloadValidationError("resolved option has an invalid strike")
    if abs(float(data["strike"]) - float(request["strike"])) >= 0.001:
        raise PayloadValidationError("resolved option does not match strike")


def _validate_account_risk(request: Mapping[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        raise PayloadValidationError("account-risk payload must be an object")
    _require_allowed_fields(data, _ACCOUNT_FIELDS, "account-risk payload")
    if data.get("account_ref") != request.get("account_ref"):
        raise PayloadValidationError("account-risk alias does not match the request")
    currency = data.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise PayloadValidationError("account-risk payload has an invalid currency")
    requested_codes = request.get("codes")
    if requested_codes is not None and (
        not isinstance(requested_codes, list)
        or not requested_codes
        or not all(_valid_symbol(code) for code in requested_codes)
    ):
        raise PayloadValidationError("account-risk request has invalid position codes")
    allowed_codes = set(requested_codes or [])
    positions = data.get("positions")
    if not isinstance(positions, list) or not all(
        isinstance(position, dict) for position in positions
    ):
        raise PayloadValidationError("account-risk positions must be a list of objects")
    for position in positions:
        _require_allowed_fields(position, _POSITION_FIELDS, "account position")
        if not _valid_symbol(position.get("code")) or not _finite_number(
            position.get("quantity")
        ) or not _finite_number(position.get("market_value")):
            raise PayloadValidationError(
                "account position has invalid identity, quantity, or market value"
            )
        if allowed_codes and position["code"] not in allowed_codes:
            raise PayloadValidationError("account position is outside the requested code filter")
        position_currency = position.get("currency")
        if position_currency is not None and (
            not isinstance(position_currency, str)
            or not re.fullmatch(r"[A-Z]{3}", position_currency)
        ):
            raise PayloadValidationError("account position has an invalid currency")
    if not _finite_number(data.get("total_assets")) or float(data["total_assets"]) < 0:
        raise PayloadValidationError("account-risk payload has no total-assets fact")
    if not any(_finite_number(data.get(field)) for field in _LIQUIDITY_FACTS):
        raise PayloadValidationError("account-risk payload has no liquidity fact")


def validate_operation_payload(request: Mapping[str, Any], data: Any) -> None:
    """Raise ``PayloadValidationError`` when a supported OK payload is unusable.

    Operations without a specialized decision-input schema are intentionally
    left to the generic ``DataEnvelope`` validation.
    """

    _reject_sensitive_keys(request, data)
    operation = request.get("operation")
    if operation not in _PUBLIC_OPERATIONS:
        raise PayloadValidationError("operation is outside the public read-only schema")
    if operation == "health":
        _validate_health(data)
    elif operation == "capabilities":
        _validate_capabilities(data)
    elif operation == "get_market_state":
        _validate_market_state(request, data)
    elif operation == "get_trading_days":
        _validate_trading_days(request, data)
    elif operation == "get_market_snapshot":
        _validate_market_snapshot(request, data)
    elif operation == "get_expiration_dates":
        _validate_expiration_dates(request, data)
    elif operation == "get_option_chain":
        _validate_option_chain(request, data)
    elif operation == "resolve_option_code":
        _validate_resolved_option(request, data)
    elif operation == "get_option_quotes":
        _validate_option_quotes(request, data)
    elif operation == "get_strategy_quote":
        _validate_strategy_quote(data)
    elif operation == "get_account_risk_summary":
        _validate_account_risk(request, data)


__all__ = ["PayloadValidationError", "validate_operation_payload"]
