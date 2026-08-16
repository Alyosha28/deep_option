"""Typed, SDK-independent contracts for live and replay data gateways.

This module deliberately uses only the Python standard library.  In particular,
importing the contracts must never import or initialise the Futu SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence, TypeVar, runtime_checkable


SCHEMA_VERSION = "1.0"
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9_]*\.[A-Z0-9][A-Z0-9._-]*$")
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ACCOUNT_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SENSITIVE_KEYS = {
    "acc_id",
    "account_id",
    "card_num",
    "card_number",
    "uni_card_num",
    "position_id",
    "combo_id",
    "user_id",
    "password",
    "passwd",
    "secret",
    "bearer",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
}
# 注意：不再把 6 位以上纯数字当作敏感文本（金融域常见「最大亏损 123456 HKD」
# 这类合法文案）；账号类数字的防泄漏由键名白名单 + futu_adapter._safe_message
# 在源头脱敏承担。
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:\b(?:token|password|passwd|bearer|secret|acc_id|card_num)\b|"
    r"[A-Z]:\\|\\\\|/(?:home|root|users|var|tmp|etc)/)"
)
_PUBLIC_ORIGINS = {"FUTU", "FUTU_SIMULATE", "REPLAY", "APPLICATION"}
_ENTITLEMENT_VALUES = {"available", "denied", "unverified", "simulate"}
_EnumT = TypeVar("_EnumT", bound=Enum)


class DataMode(str, Enum):
    LIVE = "LIVE"
    REPLAY = "REPLAY"


class EnvelopeStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    ERROR = "ERROR"


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    FROZEN = "FROZEN"
    UNKNOWN = "UNKNOWN"


class GatewayErrorCode(str, Enum):
    OPEND_UNAVAILABLE = "OPEND_UNAVAILABLE"
    SDK_UNAVAILABLE = "SDK_UNAVAILABLE"
    SDK_INCOMPATIBLE = "SDK_INCOMPATIBLE"
    AUTH_FAILED = "AUTH_FAILED"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
    TRADE_UNLOCK_REQUIRED = "TRADE_UNLOCK_REQUIRED"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    REPLAY_FIXTURE_MISSING = "REPLAY_FIXTURE_MISSING"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    STALE_DATA = "STALE_DATA"
    PARTIAL_DATA = "PARTIAL_DATA"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _enum_value(enum_type: type[_EnumT], value: object, field_name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or {enum_type.__name__}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


def _validate_json_value(value: Any, path: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _reject_sensitive_keys(value: Any, path: str) -> None:
    stack = [(value, path)]
    visited = 0
    while stack:
        current, current_path = stack.pop()
        visited += 1
        if visited > 100_000:
            raise ValueError(f"{path} exceeds the metadata traversal limit")
        if isinstance(current, dict):
            for key, item in current.items():
                normalized = str(key).strip().lower()
                if normalized in _SENSITIVE_KEYS or normalized.endswith(
                    ("_token", "_password", "_secret", "_private_key")
                ):
                    raise ValueError(f"{current_path} contains a forbidden sensitive field")
                stack.append((item, f"{current_path}.{key}"))
        elif isinstance(current, list):
            stack.extend(
                (item, f"{current_path}[{index}]") for index, item in enumerate(current)
            )


def _validate_public_text(value: str, field_name: str) -> None:
    if _SENSITIVE_TEXT.search(value):
        raise ValueError(f"{field_name} contains sensitive-looking content")


def _validate_entitlements(value: dict[str, Any]) -> None:
    stack = list(value.values())
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, bool):
            continue
        elif isinstance(item, str) and item in _ENTITLEMENT_VALUES:
            continue
        else:
            raise ValueError("entitlements contain a value outside the public schema")


def _json_clone(value: Any, path: str) -> Any:
    _validate_json_value(value, path)
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _canonical_json(value: Mapping[str, Any]) -> str:
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_utc_timestamp(value: object, field_name: str, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field_name} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must use UTC")


@dataclass(frozen=True, slots=True)
class GatewayError:
    code: GatewayErrorCode
    message: str
    retryable: bool
    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _enum_value(GatewayErrorCode, self.code, "code"))
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        _validate_public_text(self.message, "message")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GatewayError":
        if not isinstance(payload, dict):
            raise TypeError("typed_error must be a dict")
        expected = {"code", "message", "retryable"}
        if set(payload) != expected:
            raise ValueError("typed_error fields do not match the gateway schema")
        return cls(
            code=_enum_value(GatewayErrorCode, payload["code"], "typed_error.code"),
            message=payload["message"],
            retryable=payload["retryable"],
        )


@dataclass(frozen=True, slots=True)
class DataEnvelope:
    mode: DataMode
    origin_source: str
    captured_at_utc: str
    source_time_utc: str | None
    freshness_status: FreshnessStatus
    request: dict[str, Any]
    status: EnvelopeStatus
    data: Any
    entitlements: dict[str, Any]
    warnings: list[str]
    typed_error: GatewayError | None
    schema_version: str = SCHEMA_VERSION
    snapshot_id: str = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum_value(DataMode, self.mode, "mode"))
        object.__setattr__(
            self,
            "freshness_status",
            _enum_value(FreshnessStatus, self.freshness_status, "freshness_status"),
        )
        object.__setattr__(self, "status", _enum_value(EnvelopeStatus, self.status, "status"))
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if self.origin_source not in _PUBLIC_ORIGINS:
            raise ValueError("origin_source is outside the public schema")
        _validate_utc_timestamp(self.captured_at_utc, "captured_at_utc", optional=False)
        _validate_utc_timestamp(self.source_time_utc, "source_time_utc", optional=True)
        if not isinstance(self.request, dict):
            raise TypeError("request must be a dict")
        if not isinstance(self.entitlements, dict):
            raise TypeError("entitlements must be a dict")
        if not isinstance(self.warnings, list) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise TypeError("warnings must be a list of strings")
        for warning in self.warnings:
            _validate_public_text(warning, "warning")
        if self.typed_error is not None and not isinstance(self.typed_error, GatewayError):
            raise TypeError("typed_error must be GatewayError or None")
        if self.status is EnvelopeStatus.ERROR and self.typed_error is None:
            raise ValueError("ERROR envelopes require typed_error")
        if self.status is EnvelopeStatus.ERROR and self.data is not None:
            raise ValueError("ERROR envelopes must not carry data")
        if self.status is EnvelopeStatus.OK and self.typed_error is not None:
            raise ValueError("OK envelopes cannot carry typed_error")

        _reject_sensitive_keys(self.request, "request")
        _reject_sensitive_keys(self.data, "data")
        _reject_sensitive_keys(self.entitlements, "entitlements")
        _validate_entitlements(self.entitlements)

        object.__setattr__(self, "request", _json_clone(self.request, "request"))
        object.__setattr__(self, "data", _json_clone(self.data, "data"))
        object.__setattr__(
            self, "entitlements", _json_clone(self.entitlements, "entitlements")
        )
        object.__setattr__(self, "warnings", list(self.warnings))
        digest = self._calculate_sha256()
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "snapshot_id", f"snap_{digest[:20]}")

    def _content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "origin_source": self.origin_source,
            "captured_at_utc": self.captured_at_utc,
            "source_time_utc": self.source_time_utc,
            "freshness_status": self.freshness_status.value,
            "request": self.request,
            "status": self.status.value,
            "data": self.data,
            "entitlements": self.entitlements,
            "warnings": self.warnings,
            "typed_error": self.typed_error.to_dict() if self.typed_error else None,
        }

    def _calculate_sha256(self) -> str:
        canonical = _canonical_json(self._content_dict()).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def verify_integrity(self) -> bool:
        try:
            digest = self._calculate_sha256()
        except (RecursionError, TypeError, ValueError):
            return False
        return (
            self.content_sha256 == digest
            and self.snapshot_id == f"snap_{digest[:20]}"
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self._content_dict()
        payload["snapshot_id"] = self.snapshot_id
        payload["content_sha256"] = self.content_sha256
        return _json_clone(payload, "envelope")

    def to_json_line(self) -> str:
        if not self.verify_integrity():
            raise ValueError("cannot serialize an envelope with invalid integrity")
        return _canonical_json(self.to_dict())

    @classmethod
    def now(
        cls,
        *,
        mode: DataMode,
        origin_source: str,
        freshness_status: FreshnessStatus,
        request: dict[str, Any],
        status: EnvelopeStatus,
        data: Any,
        entitlements: dict[str, Any],
        warnings: list[str],
        typed_error: GatewayError | None,
        source_time_utc: str | None = None,
        captured_at_utc: str | None = None,
    ) -> "DataEnvelope":
        """Build an envelope captured now, while allowing deterministic clocks in tests."""

        captured = captured_at_utc or datetime.now(timezone.utc).isoformat()
        return cls(
            mode=mode,
            origin_source=origin_source,
            captured_at_utc=captured,
            source_time_utc=source_time_utc,
            freshness_status=freshness_status,
            request=request,
            status=status,
            data=data,
            entitlements=entitlements,
            warnings=warnings,
            typed_error=typed_error,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DataEnvelope":
        if not isinstance(payload, dict):
            raise TypeError("envelope payload must be a dict")
        expected = {
            "schema_version",
            "mode",
            "origin_source",
            "captured_at_utc",
            "source_time_utc",
            "freshness_status",
            "request",
            "status",
            "data",
            "entitlements",
            "warnings",
            "typed_error",
            "snapshot_id",
            "content_sha256",
        }
        if set(payload) != expected:
            raise ValueError("envelope fields do not match the gateway schema")
        supplied_snapshot_id = payload["snapshot_id"]
        supplied_sha256 = payload["content_sha256"]
        if not isinstance(supplied_snapshot_id, str) or not isinstance(supplied_sha256, str):
            raise TypeError("snapshot_id and content_sha256 must be strings")
        typed_error = payload["typed_error"]
        restored = cls(
            mode=_enum_value(DataMode, payload["mode"], "mode"),
            origin_source=payload["origin_source"],
            captured_at_utc=payload["captured_at_utc"],
            source_time_utc=payload["source_time_utc"],
            freshness_status=_enum_value(
                FreshnessStatus, payload["freshness_status"], "freshness_status"
            ),
            request=payload["request"],
            status=_enum_value(EnvelopeStatus, payload["status"], "status"),
            data=payload["data"],
            entitlements=payload["entitlements"],
            warnings=payload["warnings"],
            typed_error=(GatewayError.from_dict(typed_error) if typed_error is not None else None),
            schema_version=payload["schema_version"],
        )
        if supplied_sha256 != restored.content_sha256:
            raise ValueError("content_sha256 does not match envelope content")
        if supplied_snapshot_id != restored.snapshot_id:
            raise ValueError("snapshot_id does not match envelope content")
        return restored

    @classmethod
    def from_json_line(cls, line: str) -> "DataEnvelope":
        if not isinstance(line, str) or not line.strip():
            raise ValueError("JSONL line must be a non-empty string")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON object key: {key}")
                result[key] = value
            return result

        payload = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        return cls.from_dict(payload)


def _normalise_symbol(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty market-qualified symbol")
    normalised = value.strip().upper()
    if len(normalised) > 64 or not _SYMBOL_RE.fullmatch(normalised):
        raise ValueError(f"{field_name} must include a market prefix, e.g. HK.00700")
    return normalised


def normalize_symbol(value: object, field_name: str = "symbol") -> str:
    """Return one bounded canonical market-qualified symbol."""

    return _normalise_symbol(value, field_name)


def _normalise_token(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalised = value.strip().upper()
    if len(normalised) > 64 or not _TOKEN_RE.fullmatch(normalised):
        raise ValueError(f"invalid {field_name}: {value!r}")
    return normalised


def _parse_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use canonical YYYY-MM-DD format")
    return parsed


@dataclass(frozen=True, slots=True)
class OptionChainRequest:
    underlying: str
    start: str
    end: str
    option_type: str = "ALL"
    option_cond_type: str = "ALL"

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", _normalise_symbol(self.underlying, "underlying"))
        start_date = _parse_date(self.start, "start")
        end_date = _parse_date(self.end, "end")
        if end_date < start_date:
            raise ValueError("end must be on or after start")
        if (end_date - start_date).days + 1 > 30:
            raise ValueError("option-chain window may contain at most 30 inclusive days")
        option_type = _normalise_token(self.option_type, "option_type")
        if option_type not in {"ALL", "CALL", "PUT"}:
            raise ValueError("option_type must be ALL, CALL, or PUT")
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(
            self,
            "option_cond_type",
            _normalise_token(self.option_cond_type, "option_cond_type"),
        )
        if self.option_cond_type not in {"ALL", "WITHIN", "OUTSIDE"}:
            raise ValueError("option_cond_type must be ALL, WITHIN, or OUTSIDE")

    def to_dict(self) -> dict[str, str]:
        return {
            "underlying": self.underlying,
            "start": self.start,
            "end": self.end,
            "option_type": self.option_type,
            "option_cond_type": self.option_cond_type,
        }


@dataclass(frozen=True, slots=True)
class OptionLeg:
    code: str
    action: str
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _normalise_symbol(self.code, "code"))
        action = _normalise_token(self.action, "action")
        if action not in {"BUY", "SELL"}:
            raise ValueError("action must be BUY or SELL")
        object.__setattr__(self, "action", action)
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise TypeError("quantity must be a positive integer")
        if self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if self.quantity > 1_000_000:
            raise ValueError("quantity exceeds the configured safety limit")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "action": self.action, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class AccountBinding:
    account_ref: str
    _acc_id: int = field(repr=False)
    trd_env: str = "SIMULATE"
    currency: str = "HKD"
    market: str = "HK"
    security_firm: str = "FUTUSECURITIES"

    def __post_init__(self) -> None:
        if not isinstance(self.account_ref, str) or not _ACCOUNT_REF_RE.fullmatch(
            self.account_ref
        ):
            raise ValueError("account_ref must be a non-sensitive alias")
        if isinstance(self._acc_id, bool) or not isinstance(self._acc_id, int) or self._acc_id <= 0:
            raise ValueError("acc_id must be a positive integer")
        trd_env = _normalise_token(self.trd_env, "trd_env")
        if trd_env != "SIMULATE":
            raise ValueError("competition account bindings must use SIMULATE")
        object.__setattr__(self, "trd_env", trd_env)
        currency = _normalise_token(self.currency, "currency")
        if len(currency) != 3:
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "market", _normalise_token(self.market, "market"))
        object.__setattr__(
            self,
            "security_firm",
            _normalise_token(self.security_firm, "security_firm"),
        )

    @property
    def sdk_acc_id(self) -> int:
        """Return the SDK identifier for trusted gateway implementations only."""

        return self._acc_id


@runtime_checkable
class MarketDataGateway(Protocol):
    mode: DataMode

    def health(self) -> DataEnvelope: ...

    def capabilities(self) -> DataEnvelope: ...

    def get_market_state(self, codes: Sequence[str]) -> DataEnvelope: ...

    def get_trading_days(self, market: str, start: str, end: str) -> DataEnvelope: ...

    def get_market_snapshot(self, codes: Sequence[str]) -> DataEnvelope: ...

    def get_expiration_dates(self, underlying: str) -> DataEnvelope: ...

    def get_option_chain(self, request: OptionChainRequest) -> DataEnvelope: ...

    def resolve_option_code(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> DataEnvelope: ...

    def get_option_quotes(self, legs: Sequence[OptionLeg]) -> DataEnvelope: ...

    def get_strategy_quote(self, legs: Sequence[OptionLeg]) -> DataEnvelope: ...

    def close(self) -> None: ...


@runtime_checkable
class AccountReadGateway(Protocol):
    mode: DataMode

    def get_account_risk_summary(
        self, account_ref: str, codes: Sequence[str] | None = None
    ) -> DataEnvelope: ...

    def close(self) -> None: ...


__all__ = [
    "AccountBinding",
    "AccountReadGateway",
    "DataEnvelope",
    "DataMode",
    "EnvelopeStatus",
    "FreshnessStatus",
    "GatewayError",
    "GatewayErrorCode",
    "MarketDataGateway",
    "OptionChainRequest",
    "OptionLeg",
    "SCHEMA_VERSION",
    "normalize_symbol",
]
