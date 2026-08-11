"""Deterministic, read-only replay implementation of the data gateway."""

from __future__ import annotations

import copy
import json
import math
import pathlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence

from .gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    OptionChainRequest,
    normalize_symbol,
)
from .payload_validation import validate_operation_payload
from .snapshot_recorder import DEFAULT_DIR, iter_envelopes

_LOG_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3,6})")
_FILENAME_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_CHINA_TZ = timezone(timedelta(hours=8))
_FALLBACK_CAPTURED_AT = "1970-01-01T00:00:00+00:00"
_ACCOUNT_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_FIXTURE_FILES = 100
_MAX_JSONL_BYTES = 64 * 1024 * 1024
_MAX_LEGACY_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_FIXTURE_BYTES = 128 * 1024 * 1024
_MAX_INVENTORY_RECORDS = 20_000
_MAX_EMBEDDED_OBJECTS = 256
_MAX_LEGACY_DECODE_ATTEMPTS = 256
_MAX_CODES_PER_REQUEST = 800
_MAX_OPTION_LEGS = 8
_DEFAULT_CAPTURE_SKEW_SECONDS = 60.0
_MARKET_DATA_OPERATIONS = {
    "get_market_state",
    "get_trading_days",
    "get_market_snapshot",
    "get_expiration_dates",
    "get_option_chain",
    "resolve_option_code",
    "get_option_quotes",
    "get_strategy_quote",
}
_TYPE_ALIASES = {
    "CALL": {"CALL", "C", "涨", "认购"},
    "PUT": {"PUT", "P", "跌", "认沽"},
}


@dataclass(frozen=True, slots=True)
class _LegacyFixture:
    path: pathlib.Path
    captured_at_utc: str
    payloads: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Inventory:
    paths: tuple[pathlib.Path, ...]
    envelopes: tuple[DataEnvelope, ...]
    by_request: Mapping[str, tuple[DataEnvelope, ...]]
    legacy: tuple[_LegacyFixture, ...]
    capture_times: tuple[str, ...]
    failure: Optional[tuple[pathlib.Path, Exception]]


def _is_link_or_reparse(path: pathlib.Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON object key: {key}")
        output[key] = value
    return output


def _upper_codes(codes: Iterable[str]) -> list[str]:
    if isinstance(codes, str):
        values = [codes]
    else:
        values = list(islice(iter(codes), _MAX_CODES_PER_REQUEST + 1))
        if len(values) > _MAX_CODES_PER_REQUEST:
            raise ValueError(f"at most {_MAX_CODES_PER_REQUEST} security codes are allowed")
    normalized = [normalize_symbol(code, "security code") for code in values]
    if not normalized:
        raise ValueError("at least one market-qualified security code is required")
    unique = list(dict.fromkeys(normalized))
    if len(unique) > _MAX_CODES_PER_REQUEST:
        raise ValueError(f"at most {_MAX_CODES_PER_REQUEST} security codes are allowed")
    return unique


def _value(value: Any) -> Any:
    """Convert enum-like request values to their stable JSON representation."""
    return getattr(value, "value", value)


def _serialise_legs(legs: Sequence[Any]) -> list[dict[str, Any]]:
    values = list(islice(iter(legs), _MAX_OPTION_LEGS + 1))
    if not values:
        raise ValueError("at least one option leg is required")
    if len(values) > _MAX_OPTION_LEGS:
        raise ValueError(f"at most {_MAX_OPTION_LEGS} option legs are allowed")
    output: list[dict[str, Any]] = []
    for leg in values:
        code = normalize_symbol(leg.code, "option leg code")
        action = str(_value(leg.action)).strip().upper()
        quantity = leg.quantity
        if action not in {"BUY", "SELL"}:
            raise ValueError("invalid option leg identity")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity > 1_000_000
        ):
            raise ValueError("option leg quantity must be a positive integer")
        output.append({"code": code, "action": action, "quantity": quantity})
    return output


def _canonical_utc_timestamp(value: str) -> Optional[str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _capture_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _chain_request_dict(request: OptionChainRequest) -> dict[str, Any]:
    return {
        "operation": "get_option_chain",
        "underlying": request.underlying,
        "start": request.start,
        "end": request.end,
        "option_type": _value(request.option_type),
        "option_cond_type": _value(request.option_cond_type),
    }


def _request_key(request: Mapping[str, Any]) -> str:
    return json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _freeze_inventory(
    paths: Sequence[pathlib.Path],
    envelopes: Sequence[DataEnvelope],
    legacy: Sequence[_LegacyFixture],
    capture_times: Sequence[str],
    failure: Optional[tuple[pathlib.Path, Exception]],
) -> _Inventory:
    grouped: dict[str, list[DataEnvelope]] = {}
    for envelope in envelopes:
        grouped.setdefault(_request_key(envelope.request), []).append(envelope)
    index = MappingProxyType(
        {
            key: tuple(values)
            for key, values in grouped.items()
        }
    )
    return _Inventory(
        paths=tuple(paths),
        envelopes=tuple(envelopes),
        by_request=index,
        legacy=tuple(legacy),
        capture_times=tuple(capture_times),
        failure=failure,
    )


def _replay_copy(envelope: DataEnvelope) -> DataEnvelope:
    """Rebuild an envelope so integrity fields cover the replay metadata."""
    return DataEnvelope(
        mode=DataMode.REPLAY,
        origin_source=envelope.origin_source,
        captured_at_utc=envelope.captured_at_utc,
        source_time_utc=envelope.source_time_utc,
        freshness_status=FreshnessStatus.FROZEN,
        request=copy.deepcopy(envelope.request),
        status=envelope.status,
        data=copy.deepcopy(envelope.data),
        entitlements=copy.deepcopy(envelope.entitlements),
        warnings=copy.deepcopy(envelope.warnings),
        typed_error=copy.deepcopy(envelope.typed_error),
    )


def _legacy_timestamp(text: str, path: pathlib.Path) -> str:
    match = _LOG_TIMESTAMP.search(text)
    if match:
        local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
        return local.replace(tzinfo=_CHINA_TZ).astimezone(timezone.utc).isoformat()
    match = _FILENAME_DATE.search(path.name)
    if match:
        local = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=_CHINA_TZ)
        return local.astimezone(timezone.utc).isoformat()
    return _FALLBACK_CAPTURED_AT


def _embedded_json_objects(text: str) -> Iterable[dict[str, Any]]:
    """Extract JSON objects while ignoring OpenD log lines around them."""
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
    cursor = 0
    object_count = 0
    decode_attempts = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            return
        decode_attempts += 1
        if decode_attempts > _MAX_LEGACY_DECODE_ATTEMPTS:
            raise ValueError("legacy fixture exceeds the JSON decode-attempt limit")
        try:
            value, consumed = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = consumed
        if isinstance(value, dict):
            object_count += 1
            if object_count > _MAX_EMBEDDED_OBJECTS:
                raise ValueError("legacy fixture contains too many embedded objects")
            yield value


def _legacy_row_expiry(row: dict[str, Any]) -> str:
    return str(row.get("expiry") or row.get("strike_time") or "")


def _normalise_legacy_chain_row(row: dict[str, Any], underlying: str) -> dict[str, Any]:
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "underlying": row.get("underlying") or row.get("stock_owner") or underlying,
        "option_type": str(row.get("option_type") or "").upper(),
        "strike": row.get("strike", row.get("strike_price")),
        "expiry": row.get("expiry") or row.get("strike_time"),
        "lot_size": row.get("lot_size"),
        "standard_type": row.get("standard_type") or row.get("option_standard_type"),
    }


def _validate_legacy_payload(payload: dict[str, Any]) -> None:
    underlying = payload.get("code") or payload.get("underlying")
    rows = payload.get("data")
    if not isinstance(underlying, str) or "." not in underlying or not isinstance(rows, list):
        raise ValueError("legacy fixture does not match the option-chain migration schema")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("legacy option-chain rows must be objects")
        code = row.get("code")
        option_type = str(row.get("option_type") or "").upper()
        expiry = _legacy_row_expiry(row)
        strike = row.get("strike", row.get("strike_price"))
        if not isinstance(code, str) or "." not in code or option_type not in {"CALL", "PUT"}:
            raise ValueError("legacy option-chain row has invalid contract identity")
        if strike is None:
            raise ValueError("legacy option-chain row has invalid expiry or strike")
        try:
            datetime.strptime(expiry, "%Y-%m-%d")
            numeric_strike = float(strike)
        except (TypeError, ValueError) as exc:
            raise ValueError("legacy option-chain row has invalid expiry or strike") from exc
        if numeric_strike <= 0:
            raise ValueError("legacy option-chain strike must be positive")


class ReplayGateway:
    """Serve recorded gateway envelopes without importing or contacting Futu."""

    mode = DataMode.REPLAY

    def __init__(
        self,
        snapshot_dir: pathlib.Path | str = DEFAULT_DIR,
        *,
        allow_legacy: bool = False,
        max_capture_skew_seconds: float = _DEFAULT_CAPTURE_SKEW_SECONDS,
        as_of_utc: Optional[str] = None,
    ):
        requested = pathlib.Path(snapshot_dir)
        if requested.exists() and _is_link_or_reparse(requested):
            raise ValueError("replay directory must not be a link or reparse point")
        self.snapshot_dir = requested.resolve()
        if not isinstance(allow_legacy, bool):
            raise TypeError("allow_legacy must be a boolean")
        self.allow_legacy = allow_legacy
        if (
            isinstance(max_capture_skew_seconds, bool)
            or not isinstance(max_capture_skew_seconds, (int, float))
            or not math.isfinite(float(max_capture_skew_seconds))
            or float(max_capture_skew_seconds) < 0
        ):
            raise ValueError("max_capture_skew_seconds must be a non-negative finite number")
        self._max_capture_skew_seconds = float(max_capture_skew_seconds)
        canonical_as_of = (
            _canonical_utc_timestamp(as_of_utc) if isinstance(as_of_utc, str) else None
        )
        if as_of_utc is not None and canonical_as_of is None:
            raise ValueError("as_of_utc must be a valid UTC timestamp")
        self._inventory = self._build_inventory()
        self._as_of_utc = canonical_as_of or self._fixed_capture_time()

    @property
    def as_of_utc(self) -> str:
        return self._as_of_utc

    @property
    def max_capture_skew_seconds(self) -> float:
        return self._max_capture_skew_seconds

    def _fixture_paths(self) -> list[pathlib.Path]:
        paths = list(self.snapshot_dir.glob("*.jsonl"))
        if self.allow_legacy:
            paths.extend(self.snapshot_dir.glob("*.json"))
        paths = sorted(paths)
        if len(paths) > _MAX_FIXTURE_FILES:
            raise ValueError("replay fixture count exceeds the configured limit")
        total_bytes = 0
        for path in paths:
            if _is_link_or_reparse(path) or not path.is_file():
                raise ValueError("replay fixtures must be regular non-link files")
            maximum = _MAX_JSONL_BYTES if path.suffix == ".jsonl" else _MAX_LEGACY_BYTES
            size = path.stat().st_size
            if size > maximum:
                raise ValueError("replay fixture exceeds the configured file-size limit")
            total_bytes += size
            if total_bytes > _MAX_TOTAL_FIXTURE_BYTES:
                raise ValueError("replay fixture inventory exceeds the total-size limit")
        return paths

    def _build_inventory(self) -> _Inventory:
        envelopes: list[DataEnvelope] = []
        legacy: list[_LegacyFixture] = []
        capture_times: list[str] = []
        try:
            paths = self._fixture_paths()
        except (OSError, ValueError, RecursionError) as exc:
            return _freeze_inventory(
                (),
                envelopes,
                legacy,
                capture_times,
                (self.snapshot_dir, exc),
            )
        record_count = 0
        for path in (item for item in paths if item.suffix == ".jsonl"):
            try:
                for envelope in iter_envelopes(path):
                    record_count += 1
                    if record_count > _MAX_INVENTORY_RECORDS:
                        raise ValueError("replay fixture inventory exceeds the record limit")
                    if envelope.status is not EnvelopeStatus.ERROR:
                        validate_operation_payload(envelope.request, envelope.data)
                    elif envelope.data is not None:
                        raise ValueError("ERROR replay envelopes must not contain data")
                    envelopes.append(envelope)
                    capture_times.append(envelope.captured_at_utc)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                return _freeze_inventory(
                    paths,
                    envelopes,
                    legacy,
                    capture_times,
                    (path, exc),
                )
        for path in (item for item in paths if item.suffix == ".json"):
            try:
                text = path.read_text(encoding="utf-8")
                captured_at = _legacy_timestamp(text, path)
                payloads = tuple(_embedded_json_objects(text))
                if not payloads:
                    raise ValueError("legacy fixture contains no JSON object")
                record_count += len(payloads)
                if record_count > _MAX_INVENTORY_RECORDS:
                    raise ValueError("replay fixture inventory exceeds the record limit")
                for payload in payloads:
                    _validate_legacy_payload(payload)
                legacy.append(
                    _LegacyFixture(
                        path=path,
                        captured_at_utc=captured_at,
                        payloads=payloads,
                    )
                )
                capture_times.append(captured_at)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                return _freeze_inventory(
                    paths,
                    envelopes,
                    legacy,
                    capture_times,
                    (path, exc),
                )
        return _freeze_inventory(paths, envelopes, legacy, capture_times, None)

    def _load_envelopes(
        self,
    ) -> tuple[list[DataEnvelope], Optional[tuple[pathlib.Path, Exception]]]:
        return list(self._inventory.envelopes), self._inventory.failure

    def _selectable_envelopes(self) -> tuple[DataEnvelope, ...]:
        as_of = _capture_datetime(self.as_of_utc)
        return tuple(
            item
            for item in self._inventory.envelopes
            if item.status is not EnvelopeStatus.ERROR
            and (captured := _capture_datetime(item.captured_at_utc)) <= as_of
            and (as_of - captured).total_seconds() <= self.max_capture_skew_seconds
        )

    def _selectable_legacy(self) -> tuple[_LegacyFixture, ...]:
        as_of = _capture_datetime(self.as_of_utc)
        return tuple(
            item
            for item in self._inventory.legacy
            if (captured := _capture_datetime(item.captured_at_utc)) <= as_of
            and (as_of - captured).total_seconds() <= self.max_capture_skew_seconds
        )

    def _fixed_capture_time(self) -> str:
        return max(
            self._inventory.capture_times,
            key=_capture_datetime,
            default=_FALLBACK_CAPTURED_AT,
        )

    def _schema_error(
        self, request: dict[str, Any], failure: tuple[pathlib.Path, Exception]
    ) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source="REPLAY",
            captured_at_utc=self.as_of_utc,
            source_time_utc=None,
            freshness_status=FreshnessStatus.FROZEN,
            request=copy.deepcopy(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={"recorded": True},
            warnings=[],
            typed_error=GatewayError(
                code=GatewayErrorCode.SCHEMA_MISMATCH,
                message="Replay fixture failed schema or integrity validation",
                retryable=False,
            ),
        )

    def _selected_schema_error(
        self,
        request: dict[str, Any],
        selected: DataEnvelope,
        message: str,
    ) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source=selected.origin_source,
            captured_at_utc=selected.captured_at_utc,
            source_time_utc=selected.source_time_utc,
            freshness_status=FreshnessStatus.FROZEN,
            request=copy.deepcopy(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={"recorded": True},
            warnings=[],
            typed_error=GatewayError(
                code=GatewayErrorCode.SCHEMA_MISMATCH,
                message=message,
                retryable=False,
            ),
        )

    def _synthetic(
        self,
        request: dict[str, Any],
        data: Any,
        *,
        status: EnvelopeStatus = EnvelopeStatus.OK,
        warnings: Optional[list[str]] = None,
    ) -> DataEnvelope:
        try:
            validate_operation_payload(request, data)
        except (TypeError, ValueError, RecursionError):
            return self._typed_error(
                request,
                GatewayErrorCode.SCHEMA_MISMATCH,
                "Replay synthetic payload failed operation schema validation",
            )
        return DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source="REPLAY",
            captured_at_utc=self.as_of_utc,
            source_time_utc=None,
            freshness_status=FreshnessStatus.FROZEN,
            request=copy.deepcopy(request),
            status=status,
            data=copy.deepcopy(data),
            entitlements={"recorded": True},
            warnings=list(warnings or []),
            typed_error=None,
        )

    def _missing(self, request: dict[str, Any]) -> DataEnvelope:
        return self._typed_error(
            request,
            GatewayErrorCode.REPLAY_FIXTURE_MISSING,
            "No replay fixture exactly matches the requested operation and parameters",
            entitlements={"recorded": False},
        )

    def _typed_error(
        self,
        request: dict[str, Any],
        code: GatewayErrorCode,
        message: str,
        *,
        entitlements: Optional[dict[str, Any]] = None,
    ) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source="REPLAY",
            captured_at_utc=self.as_of_utc,
            source_time_utc=None,
            freshness_status=FreshnessStatus.FROZEN,
            request=copy.deepcopy(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements=copy.deepcopy(entitlements or {"recorded": True}),
            warnings=[],
            typed_error=GatewayError(
                code=code,
                message=message,
                retryable=False,
            ),
        )

    def _invalid(self, request: dict[str, Any], message: str) -> DataEnvelope:
        return self._typed_error(
            request,
            GatewayErrorCode.INVALID_REQUEST,
            message,
            entitlements={"recorded": False},
        )

    def _select_recorded(
        self,
        request: dict[str, Any],
        matches: Sequence[DataEnvelope],
    ) -> DataEnvelope:
        """Select one semantically valid record in a coherent capture-time window."""

        as_of = _capture_datetime(self.as_of_utc)
        past = [
            item
            for item in matches
            if _capture_datetime(item.captured_at_utc) <= as_of
        ]
        if not past:
            return self._typed_error(
                request,
                GatewayErrorCode.REPLAY_FIXTURE_MISSING,
                "No replay fixture is available at or before the configured as-of time",
                entitlements={"recorded": False},
            )
        candidates = [
            item
            for item in past
            if (as_of - _capture_datetime(item.captured_at_utc)).total_seconds()
            <= self.max_capture_skew_seconds
        ]
        if not candidates:
            return self._typed_error(
                request,
                GatewayErrorCode.STALE_DATA,
                "Replay fixture is older than the configured as-of capture window",
            )

        selected = max(
            candidates,
            key=lambda item: (_capture_datetime(item.captured_at_utc), item.snapshot_id),
        )
        try:
            if selected.status is not EnvelopeStatus.ERROR:
                validate_operation_payload(selected.request, selected.data)
            return _replay_copy(selected)
        except (TypeError, ValueError, RecursionError):
            return self._selected_schema_error(
                request,
                selected,
                "Replay payload failed operation schema validation",
            )

    def _lookup(self, request: dict[str, Any]) -> DataEnvelope:
        failure = self._inventory.failure
        if failure is not None:
            return self._schema_error(request, failure)
        matches = self._inventory.by_request.get(_request_key(request), ())
        if not matches:
            return self._missing(request)
        return self._select_recorded(request, matches)

    def health(self) -> DataEnvelope:
        request = {"operation": "health"}
        paths = self._inventory.paths
        if not paths:
            if self._inventory.failure is not None:
                return self._schema_error(request, self._inventory.failure)
            return self._missing(request)
        envelopes, failure = self._load_envelopes()
        if failure is not None:
            return self._schema_error(request, failure)
        has_legacy_inventory = bool(self._inventory.legacy)
        if not envelopes and not has_legacy_inventory:
            return self._schema_error(
                request,
                (self.snapshot_dir, ValueError("no canonical replay records found")),
            )
        selectable = self._selectable_envelopes()
        selectable_legacy = self._selectable_legacy()
        ready = bool(selectable)
        has_unverified = bool(selectable_legacy)
        return self._synthetic(
            request,
            {
                "ready": ready,
                "server_version": None,
                "fixture_count": len(paths),
            },
            status=EnvelopeStatus.OK if ready else EnvelopeStatus.PARTIAL,
            warnings=(
                ["Selectable legacy fixtures are ignored by canonical health"]
                if ready and has_unverified
                else ["Legacy fixtures are available only for explicit migration"]
                if has_unverified
                else ([] if ready else ["No usable fixture is selectable at the as-of time"])
            ),
        )

    def capabilities(self) -> DataEnvelope:
        request = {"operation": "capabilities"}
        paths = self._inventory.paths
        if not paths:
            if self._inventory.failure is not None:
                return self._schema_error(request, self._inventory.failure)
            return self._missing(request)
        envelopes, failure = self._load_envelopes()
        if failure is not None:
            return self._schema_error(request, failure)
        has_legacy_inventory = bool(self._inventory.legacy)
        if not envelopes and not has_legacy_inventory:
            return self._schema_error(
                request,
                (self.snapshot_dir, ValueError("no canonical replay records found")),
            )
        operations = {
            str(item.request.get("operation", ""))
            for item in self._selectable_envelopes()
        }
        has_unverified = bool(self._selectable_legacy())
        has_canonical_market = bool(operations & _MARKET_DATA_OPERATIONS)
        legacy_needed = has_unverified and not has_canonical_market
        has_capability = has_unverified or has_canonical_market or bool(
            operations & {"get_account_risk_summary"}
        )
        return self._synthetic(
            request,
            {
                "market_data": has_unverified or has_canonical_market,
                "account_read": "get_account_risk_summary" in operations,
                "strategy_combination_quote": "get_strategy_quote" in operations,
                "execution": False,
                "real_trading": False,
                "fixture_count": len(paths),
            },
            status=(
                EnvelopeStatus.OK
                if has_capability and not legacy_needed
                else EnvelopeStatus.PARTIAL
            ),
            warnings=(
                ["Legacy fixtures are available only for explicit migration"]
                if legacy_needed
                else ["Selectable legacy fixtures are ignored by canonical capabilities"]
                if has_unverified
                else (
                    []
                    if has_capability
                    else ["No usable fixture is selectable at the as-of time"]
                )
            ),
        )

    def get_market_state(self, codes: Iterable[str]) -> DataEnvelope:
        request = {"operation": "get_market_state", "codes": []}
        try:
            request["codes"] = _upper_codes(codes)
        except (TypeError, ValueError):
            return self._invalid(request, "codes must contain market-qualified symbols")
        return self._lookup(request)

    def get_trading_days(self, market: str, start: str, end: str) -> DataEnvelope:
        request = {
            "operation": "get_trading_days",
            "market": "<invalid>",
            "start": start,
            "end": end,
        }
        try:
            if not isinstance(market, str):
                raise TypeError
            market_name = market.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", market_name):
                raise ValueError
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
            if (
                start_date.isoformat() != start
                or end_date.isoformat() != end
                or end_date < start_date
                or (end_date - start_date).days + 1 > 366
            ):
                raise ValueError
            request["market"] = market_name
        except (TypeError, ValueError):
            return self._invalid(request, "invalid market or trading-day date window")
        return self._lookup(request)

    def get_market_snapshot(self, codes: Iterable[str]) -> DataEnvelope:
        request = {"operation": "get_market_snapshot", "codes": []}
        try:
            request["codes"] = _upper_codes(codes)
        except (TypeError, ValueError):
            return self._invalid(request, "codes must contain market-qualified symbols")
        return self._lookup(request)

    def get_expiration_dates(self, underlying: str) -> DataEnvelope:
        request = {"operation": "get_expiration_dates", "underlying": "<invalid>"}
        try:
            request["underlying"] = normalize_symbol(underlying, "underlying")
        except (TypeError, ValueError):
            return self._invalid(request, "underlying must be a market-qualified symbol")
        return self._lookup(request)

    def get_option_chain(self, request: OptionChainRequest) -> DataEnvelope:
        if not isinstance(request, OptionChainRequest):
            return self._invalid(
                {"operation": "get_option_chain"},
                "request must be a validated OptionChainRequest",
            )
        wanted = _chain_request_dict(request)
        failure = self._inventory.failure
        if failure is not None:
            return self._schema_error(wanted, failure)
        recorded = self._inventory.by_request.get(_request_key(wanted), ())
        if recorded:
            return self._select_recorded(wanted, recorded)

        legacy = self._legacy_option_chain(request, wanted)
        return legacy if legacy is not None else self._missing(wanted)

    def _legacy_option_chain(
        self, request: OptionChainRequest, wanted: dict[str, Any]
    ) -> Optional[DataEnvelope]:
        if not self.allow_legacy:
            return None
        candidates: list[DataEnvelope] = []
        saw_relevant_rows = False
        requested_type = str(_value(request.option_type) or "ALL").upper()
        requested_condition = str(_value(request.option_cond_type) or "ALL").upper()
        # Legacy rows never recorded Futu's WITHIN/OUTSIDE moneyness condition.
        # Refuse to infer it from unrelated contract-standard fields.
        if requested_condition not in {"", "ALL"}:
            return None

        for fixture in self._inventory.legacy:
            for payload in fixture.payloads:
                rows = payload.get("data")
                underlying = str(payload.get("code") or payload.get("underlying") or "").upper()
                if underlying != request.underlying or not isinstance(rows, list):
                    continue

                dated_rows = [
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and request.start <= _legacy_row_expiry(row) <= request.end
                ]
                if not dated_rows:
                    continue
                saw_relevant_rows = True

                filtered = dated_rows
                if requested_type not in {"", "ALL"}:
                    filtered = [
                        row
                        for row in filtered
                        if str(row.get("option_type") or "").upper() == requested_type
                    ]
                if not filtered:
                    continue
                captured_at = fixture.captured_at_utc
                candidates.append(
                    DataEnvelope(
                        mode=DataMode.REPLAY,
                        origin_source="FUTU",
                        captured_at_utc=captured_at,
                        source_time_utc=captured_at,
                        freshness_status=FreshnessStatus.FROZEN,
                        request=copy.deepcopy(wanted),
                        status=EnvelopeStatus.PARTIAL,
                        data=[
                            _normalise_legacy_chain_row(row, request.underlying)
                            for row in filtered
                        ],
                        entitlements={
                            "recorded": True,
                            "legacy_fixture": True,
                            "integrity": "unverified",
                        },
                        warnings=[
                            "Legacy fixture is unverified and available only for migration"
                        ],
                        typed_error=None,
                    )
                )
        if not candidates:
            if saw_relevant_rows:
                return self._typed_error(
                    wanted,
                    GatewayErrorCode.NOT_FOUND,
                    "No option contracts match the requested legacy fixture filters",
                )
            return None
        return self._select_recorded(wanted, candidates)

    def resolve_option_code(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> DataEnvelope:
        canonical_type = str(option_type).strip().upper()
        for candidate, aliases in _TYPE_ALIASES.items():
            if canonical_type in aliases:
                canonical_type = candidate
                break
        request: dict[str, Any] = {
            "operation": "resolve_option_code",
            "underlying": str(underlying).strip().upper(),
            "expiry": str(expiry),
            "strike": "<invalid>",
            "option_type": canonical_type,
        }
        try:
            request["underlying"] = normalize_symbol(underlying, "underlying")
            target_strike = float(strike)
            if not math.isfinite(target_strike) or target_strike <= 0:
                raise ValueError("strike must be a positive finite number")
            request["strike"] = target_strike
            OptionChainRequest(
                request["underlying"],
                request["expiry"],
                request["expiry"],
                option_type=request["option_type"],
            )
        except (TypeError, ValueError):
            return self._invalid(request, "invalid option contract selector")
        direct = self._lookup(request)
        if (
            direct.status is not EnvelopeStatus.ERROR
            or direct.typed_error is None
            or direct.typed_error.code is not GatewayErrorCode.REPLAY_FIXTURE_MISSING
        ):
            return direct

        chain = self.get_option_chain(
            OptionChainRequest(
                request["underlying"], expiry, expiry, option_type=request["option_type"]
            )
        )
        if (
            chain.status is EnvelopeStatus.ERROR
            and chain.typed_error is not None
            and chain.typed_error.code is GatewayErrorCode.REPLAY_FIXTURE_MISSING
            and request["option_type"] != "ALL"
        ):
            chain = self.get_option_chain(
                OptionChainRequest(request["underlying"], expiry, expiry)
            )
        if chain.status is EnvelopeStatus.ERROR:
            return direct if chain.typed_error is None else DataEnvelope(
                mode=DataMode.REPLAY,
                origin_source=chain.origin_source,
                captured_at_utc=chain.captured_at_utc,
                source_time_utc=chain.source_time_utc,
                freshness_status=FreshnessStatus.FROZEN,
                request=request,
                status=chain.status,
                data=None,
                entitlements=copy.deepcopy(chain.entitlements),
                warnings=copy.deepcopy(chain.warnings),
                typed_error=copy.deepcopy(chain.typed_error),
            )
        if not isinstance(chain.data, list):
            return self._typed_error(
                request,
                GatewayErrorCode.SCHEMA_MISMATCH,
                "Replay option chain has no resolvable contract rows",
            )

        matches = []
        for row in chain.data or []:
            if not isinstance(row, dict):
                continue
            row_expiry = row.get("expiry") or row.get("strike_time")
            row_type = str(row.get("option_type") or "").upper()
            row_strike = row.get("strike", row.get("strike_price"))
            try:
                strike_matches = (
                    row_strike is not None
                    and abs(float(row_strike) - float(request["strike"])) < 0.001
                )
            except (TypeError, ValueError):
                strike_matches = False
            if row_expiry == expiry and row_type == request["option_type"] and strike_matches:
                matches.append(row)
        if not matches:
            return self._typed_error(
                request,
                GatewayErrorCode.NOT_FOUND,
                "No option contract matched the requested expiry, strike, and type",
            )
        if len(matches) > 1:
            return self._typed_error(
                request,
                GatewayErrorCode.AMBIGUOUS_MATCH,
                "More than one option contract matched the exact request",
            )
        return DataEnvelope(
            mode=DataMode.REPLAY,
            origin_source=chain.origin_source,
            captured_at_utc=chain.captured_at_utc,
            source_time_utc=chain.source_time_utc,
            freshness_status=FreshnessStatus.FROZEN,
            request=request,
            status=chain.status,
            data=copy.deepcopy(matches[0]),
            entitlements=copy.deepcopy(chain.entitlements),
            warnings=copy.deepcopy(chain.warnings),
            typed_error=None,
        )

    def get_strategy_quote(self, legs: Sequence[Any]) -> DataEnvelope:
        try:
            serialised = _serialise_legs(legs)
        except (AttributeError, TypeError, ValueError):
            return self._invalid(
                {"operation": "get_strategy_quote", "legs": []},
                "at least one valid option leg is required",
            )
        return self._lookup({"operation": "get_strategy_quote", "legs": serialised})

    def get_option_quotes(self, legs: Sequence[Any]) -> DataEnvelope:
        try:
            serialised = _serialise_legs(legs)
        except (AttributeError, TypeError, ValueError):
            return self._invalid(
                {"operation": "get_option_quotes", "legs": []},
                "at least one valid option leg is required",
            )
        return self._lookup({"operation": "get_option_quotes", "legs": serialised})

    def get_option_quote(self, legs: Sequence[Any]) -> DataEnvelope:
        """Compatibility spelling; new callers use ``get_option_quotes``."""
        return self.get_option_quotes(legs)

    def get_account_risk_summary(
        self, account_ref: str, codes: Optional[Iterable[str]] = None
    ) -> DataEnvelope:
        alias = str(account_ref).strip()
        if not _ACCOUNT_ALIAS.fullmatch(alias):
            return self._invalid(
                {"operation": "get_account_risk_summary", "account_ref": "<invalid>"},
                "account_ref must be a configured alias",
            )
        request: dict[str, Any] = {
            "operation": "get_account_risk_summary",
            "account_ref": alias,
        }
        if codes is not None:
            try:
                request["codes"] = _upper_codes(codes)
            except (TypeError, ValueError):
                return self._invalid(request, "codes must contain market-qualified symbols")
        return self._lookup(request)

    def close(self) -> None:
        """Replay owns no external resources."""


class ReplayAdapter(ReplayGateway):
    """Backward-compatible import name for the replay gateway."""
