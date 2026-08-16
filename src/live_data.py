"""Live quote cache and live snapshot assembly.

This module is deliberately import-safe: importing :mod:`src.live_data` never
imports or initialises the Futu SDK / OpenD.  All SDK access stays behind the
injected gateway (the same typed ``MarketDataGateway`` contract used by the rest
of the system).  A host wires a gateway instance into :class:`LiveDataService`;
the service only sees ``DataEnvelope`` objects.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Optional, Sequence

from .gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    OptionLeg,
    normalize_symbol,
)

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "hero_inputs.json"

# Stale-while-revalidate defaults.  Quote reads share one TTL by default; the
# constructor accepts explicit per-category TTLs for future UI wiring.
DEFAULT_QUOTE_TTL_SECONDS = 2.0
DEFAULT_STATE_TTL_SECONDS = 5.0


def utc_now_iso() -> str:
    """Return a timezone-aware UTC ISO-8601 timestamp (seconds precision)."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stale_error_from_exception(exc: Any) -> Optional[GatewayError]:
    """Extract a public gateway error from a loader failure, if possible."""

    if exc is None:
        return None
    if isinstance(exc, _GatewayErrorEnvelope):
        return exc.envelope.typed_error
    return _exception_error(exc)


def _exception_error(exc: BaseException) -> GatewayError:
    """Map transport/loader exceptions to typed public gateway errors."""

    if isinstance(exc, TimeoutError):
        return GatewayError(
            code=GatewayErrorCode.UPSTREAM_ERROR,
            message="live quote request timed out",
            retryable=True,
        )
    if isinstance(exc, (ConnectionError, OSError)):
        return GatewayError(
            code=GatewayErrorCode.OPEND_UNAVAILABLE,
            message="live data gateway is unavailable",
            retryable=True,
        )
    return GatewayError(
        code=GatewayErrorCode.INTERNAL_ERROR,
        message="live data gateway call failed",
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached value plus the metadata needed for stale fallback."""

    value: Any
    stored_at: float
    expires_at: float
    stale: bool = False
    error: Any = None


class LiveQuoteCache:
    """A small thread-safe TTL cache with stale-while-revalidate semantics.

    ``get`` returns the raw cached/loaded value.  ``get_entry`` returns the full
    :class:`CacheEntry` so callers (like :class:`LiveDataService`) can decide how
    to surface freshness.  A failed loader never invalidates the last good value;
    callers can request the stale entry explicitly.
    """

    def __init__(
        self,
        ttl: float = DEFAULT_QUOTE_TTL_SECONDS,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(float(ttl))
            or float(ttl) <= 0
        ):
            raise ValueError("ttl must be a positive finite number of seconds")
        self.ttl = float(ttl)
        self._clock = clock or time.monotonic
        self._guard = threading.Lock()
        self._entries: dict[Any, CacheEntry] = {}
        self._key_locks: dict[Any, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def read(self, key: Any) -> Optional[CacheEntry]:
        """Return the cached entry without invoking any loader."""

        with self._guard:
            entry = self._entries.get(key)
            return entry if entry is not None else None

    def get(self, key: Any, loader: Callable[[], Any], ttl: Optional[float] = None) -> Any:
        """Return a cached/loaded value, falling back to the last good value."""

        return self.get_entry(key, loader, ttl=ttl).value

    def get_entry(
        self,
        key: Any,
        loader: Callable[[], Any],
        ttl: Optional[float] = None,
    ) -> CacheEntry:
        """Return a :class:`CacheEntry`, loading through ``loader`` when needed.

        Concurrent readers for the same key are single-flighted: only one thread
        calls ``loader``; the others wait and reuse the same fresh/stale entry.
        """

        effective_ttl = self.ttl if ttl is None else float(ttl)
        if (
            isinstance(effective_ttl, bool)
            or not math.isfinite(effective_ttl)
            or effective_ttl <= 0
        ):
            raise ValueError("ttl must be a positive finite number of seconds")

        now = self._clock()
        with self._guard:
            cached = self._entries.get(key)
        if cached is not None and now < cached.expires_at:
            return cached

        # Single-flight per key: while holding the key lock, re-check the cache
        # and load at most once.
        lock = self._key_lock(key)
        with lock:
            now = self._clock()
            with self._guard:
                cached = self._entries.get(key)
            if cached is not None and now < cached.expires_at:
                return cached

            try:
                value = loader()
            except Exception as exc:
                if cached is not None:
                    return CacheEntry(
                        value=cached.value,
                        stored_at=cached.stored_at,
                        expires_at=cached.expires_at,
                        stale=True,
                        error=exc,
                    )
                raise

            entry = CacheEntry(
                value=value,
                stored_at=now,
                expires_at=now + effective_ttl,
                stale=False,
            )
            with self._guard:
                self._entries[key] = entry
            return entry

    def get_stale(self, key: Any) -> Optional[CacheEntry]:
        """Return the last cached entry regardless of age, marked stale."""

        with self._guard:
            cached = self._entries.get(key)
        if cached is None:
            return None
        return CacheEntry(
            value=cached.value,
            stored_at=cached.stored_at,
            expires_at=cached.expires_at,
            stale=True,
        )

    def invalidate(self, key: Any) -> None:
        with self._guard:
            self._entries.pop(key, None)


class LiveDataError(RuntimeError):
    """Raised when live data is unavailable and no stale fallback exists."""

    def __init__(self, code: GatewayErrorCode, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """Public JSON shape matching the HTTP typed-error contract."""

        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": bool(self.retryable),
        }


def _read_live_template(path: Path) -> tuple[dict[str, Any], str, list[str]]:
    """Read and validate a live snapshot template.

    Returns ``(template, normalized_underlying, option_codes)`` and raises
    :class:`LiveDataError` with the same typed codes and messages that
    :meth:`LiveDataService.build_live_snapshot` surfaces.  Shared by
    ``build_live_snapshot`` and :func:`live_template_codes`.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            f"cannot read live snapshot template {path}",
        ) from exc
    if len(raw) > 4 * 1024 * 1024:
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            "live snapshot template exceeds the 4 MiB limit",
        )
    try:
        template = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            "live snapshot template is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(template, dict):
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            "live snapshot template must be a JSON object",
        )

    underlying = template.get("underlying")
    if not isinstance(underlying, str) or not underlying.strip():
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            "live snapshot template has no underlying symbol",
        )
    underlying = normalize_symbol(underlying, "underlying")

    legs_template = template.get("legs")
    if not isinstance(legs_template, list) or not legs_template:
        raise LiveDataError(
            GatewayErrorCode.INVALID_REQUEST,
            "live snapshot template must contain at least one expiry leg",
        )

    option_codes: list[str] = []
    for group in legs_template:
        if not isinstance(group, dict):
            raise LiveDataError(
                GatewayErrorCode.INVALID_REQUEST,
                "live snapshot template contains an invalid leg group",
            )
        for side in ("call", "put"):
            leg = group.get(side)
            if not isinstance(leg, dict) or not isinstance(leg.get("code"), str):
                raise LiveDataError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "live snapshot template contains an invalid option leg",
                )
            option_codes.append(normalize_symbol(leg["code"], "option leg code"))
    return dict(template), underlying, option_codes


def live_template_codes(template_path: str | Path | None = None) -> list[str]:
    """Return the underlying + option-leg codes of a live snapshot template.

    Uses the exact validation of ``build_live_snapshot``; the SSE stream layer
    uses this as the default subscription set for a workspace project.
    """

    path = Path(template_path or DEFAULT_TEMPLATE)
    _template, underlying, option_codes = _read_live_template(path)
    return list(dict.fromkeys([underlying, *option_codes]))


def quote_rows_to_payload(
    codes: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    captured_at: str,
    freshness: str,
    ttl_seconds: Optional[float] = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Convert whitelisted gateway rows into the ``/api/live-quote`` payload shape.

    ``strict=True`` (the HTTP quote endpoint) requires a row for every requested
    code and raises :class:`LiveDataError` otherwise; ``strict=False`` (the push
    feed) emits only the codes actually present in ``rows``.
    """

    normalized = [normalize_symbol(code, "security code") for code in codes]
    by_code = {
        str(row.get("code", "")).upper(): row
        for row in rows
        if isinstance(row, Mapping)
    }
    quotes: list[dict[str, Any]] = []
    for code in normalized:
        row = by_code.get(code)
        if row is None:
            if strict:
                raise LiveDataError(
                    GatewayErrorCode.NOT_FOUND,
                    f"live gateway did not return a quote for {code}",
                    False,
                )
            continue
        quotes.append(
            {
                "code": code,
                "name": row.get("name"),
                "last": row.get("last_price"),
                "prevClose": row.get("previous_close"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "bidSize": row.get("bid_size"),
                "askSize": row.get("ask_size"),
                "lotSize": row.get("lot_size"),
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
                "updatedAt": row.get("updated_at"),
            }
        )
    normalized_freshness = (
        freshness
        if freshness in (FreshnessStatus.FRESH.value, FreshnessStatus.STALE.value)
        else FreshnessStatus.STALE.value
    )
    return {
        "mode": DataMode.LIVE.value,
        "capturedAt": captured_at,
        "freshness": normalized_freshness,
        "ttlSeconds": ttl_seconds,
        "quotes": quotes,
    }


class _GatewayErrorEnvelope(Exception):
    """Internal signal: the gateway returned a typed ERROR envelope."""

    def __init__(self, envelope: DataEnvelope):
        super().__init__("gateway returned an ERROR envelope")
        self.envelope = envelope


def _to_option_legs(legs: Sequence[Any]) -> list[OptionLeg]:
    """Normalise a sequence of :class:`OptionLeg` or ``{"code","action","quantity"}`` maps."""

    output: list[OptionLeg] = []
    for leg in legs:
        if isinstance(leg, OptionLeg):
            output.append(leg)
        elif isinstance(leg, Mapping):
            output.append(
                OptionLeg(
                    code=str(leg["code"]),
                    action=str(leg.get("action", "BUY")),
                    quantity=int(leg.get("quantity", 1)),
                )
            )
        else:
            raise TypeError("legs must be OptionLeg instances or mappings")
    return output


class LiveDataService:
    """Cached read-only live quote facade over any typed market-data gateway.

    The service never imports the Futu SDK itself.  It consumes gateway
    ``DataEnvelope`` values and assembles the live snapshot consumed by the
    decision pipeline.
    """

    def __init__(
        self,
        gateway: Any,
        *,
        template_path: str | Path | None = None,
        ttl: Optional[float] = None,
        quote_ttl: float = DEFAULT_QUOTE_TTL_SECONDS,
        option_ttl: float = DEFAULT_QUOTE_TTL_SECONDS,
        state_ttl: float = DEFAULT_STATE_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        utc_clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self.gateway = gateway
        effective_ttl = quote_ttl if ttl is None else ttl
        self.template_path = Path(template_path or DEFAULT_TEMPLATE)
        self._quote_cache = LiveQuoteCache(effective_ttl, clock=clock)
        self._option_cache = LiveQuoteCache(option_ttl, clock=clock)
        self._state_cache = LiveQuoteCache(state_ttl, clock=clock)
        self._utc_clock = utc_clock or utc_now_iso
        self._clock = clock or time.monotonic

    @property
    def mode(self) -> DataMode:
        mode = getattr(self.gateway, "mode", None)
        return mode if isinstance(mode, DataMode) else DataMode.LIVE

    # ------------------------------------------------------------------
    # Cached gateway reads
    # ------------------------------------------------------------------
    def _cache_key(self, operation: str, request: Mapping[str, Any]) -> tuple:
        return (operation, json.dumps(request, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _stale_envelope(
        cached: DataEnvelope,
        *,
        captured_at_utc: str,
        error: Optional[GatewayError] = None,
        warning: str,
    ) -> DataEnvelope:
        """Rebuild a cached good envelope as an explicit STALE envelope."""

        stale_error = error or GatewayError(
            code=GatewayErrorCode.STALE_DATA,
            message=warning,
            retryable=True,
        )
        warnings = list(cached.warnings) + [warning]
        return DataEnvelope(
            mode=cached.mode,
            origin_source=cached.origin_source,
            captured_at_utc=captured_at_utc,
            source_time_utc=cached.source_time_utc,
            freshness_status=FreshnessStatus.STALE,
            request=dict(cached.request),
            status=EnvelopeStatus.STALE,
            data=cached.data,
            entitlements=dict(cached.entitlements),
            warnings=warnings,
            typed_error=stale_error,
        )

    def _read_cached(
        self,
        operation: str,
        request: Mapping[str, Any],
        loader: Callable[[], DataEnvelope],
        cache: LiveQuoteCache,
        *,
        force: bool = False,
    ) -> DataEnvelope:
        """Return an envelope, preferring a fresh cache hit and falling back to
        the last good value when the loader fails or times out.

        ``force=True`` bypasses the cache and always invokes the gateway loader.
        """

        key = self._cache_key(operation, request)

        def load() -> DataEnvelope:
            envelope = loader()
            if not isinstance(envelope, DataEnvelope):
                raise TypeError("gateway returned a non-envelope live value")
            if envelope.status is EnvelopeStatus.ERROR:
                raise _GatewayErrorEnvelope(envelope)
            return envelope

        if force:
            try:
                return load()
            except _GatewayErrorEnvelope as exc:
                return exc.envelope
            except Exception as exc:
                return DataEnvelope.now(
                    mode=self.mode,
                    origin_source="APPLICATION",
                    freshness_status=FreshnessStatus.UNKNOWN,
                    request=dict(request),
                    status=EnvelopeStatus.ERROR,
                    data=None,
                    entitlements={},
                    warnings=[],
                    typed_error=_exception_error(exc),
                    captured_at_utc=self._utc_clock(),
                )

        try:
            entry = cache.get_entry(key, load)
        except _GatewayErrorEnvelope as exc:
            return exc.envelope
        except Exception as exc:
            error = _exception_error(exc)
            return DataEnvelope.now(
                mode=self.mode,
                origin_source="APPLICATION",
                freshness_status=FreshnessStatus.UNKNOWN,
                request=dict(request),
                status=EnvelopeStatus.ERROR,
                data=None,
                entitlements={},
                warnings=[],
                typed_error=error,
                captured_at_utc=self._utc_clock(),
            )

        value = entry.value
        if entry.stale:
            error = _stale_error_from_exception(entry.error)
            return self._stale_envelope(
                value,
                captured_at_utc=self._utc_clock(),
                error=error,
                warning=f"live {operation} is stale; returning last available value",
            )

        return value

    def _invalid_gateway(self, request: Mapping[str, Any], message: str) -> DataEnvelope:
        return DataEnvelope.now(
            mode=self.mode,
            origin_source="APPLICATION",
            freshness_status=FreshnessStatus.UNKNOWN,
            request=dict(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=GatewayError(
                code=GatewayErrorCode.SCHEMA_MISMATCH,
                message=message,
                retryable=False,
            ),
            captured_at_utc=self._utc_clock(),
        )

    def _normalise_codes(self, codes: Sequence[str]) -> list[str]:
        if isinstance(codes, str):
            values = [codes]
        else:
            values = list(codes)
        if not values:
            raise ValueError("at least one market-qualified symbol is required")
        return [normalize_symbol(code, "security code") for code in values]

    def get_market_snapshot(self, codes: Sequence[str], *, force: bool = False) -> DataEnvelope:
        """Cached ``get_market_snapshot``; stale fallback on gateway failure."""

        try:
            normalized = self._normalise_codes(codes)
        except (TypeError, ValueError) as exc:
            return self._invalid_gateway(
                {"operation": "get_market_snapshot", "codes": []},
                f"invalid live snapshot codes: {exc}",
            )
        request = {"operation": "get_market_snapshot", "codes": normalized}
        loader = self.gateway.get_market_snapshot
        return self._read_cached(
            "get_market_snapshot",
            request,
            lambda: loader(normalized),
            self._quote_cache,
            force=force,
        )

    def get_quote(self, codes: Sequence[str], *, force: bool = False) -> DataEnvelope:
        """Lightweight cached quote alias (single or multiple codes)."""

        return self.get_market_snapshot(codes, force=force)

    def quote_payload(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Return the read-only quote payload for ``GET /api/live-quote``.

        Uses the same quote cache as snapshot assembly; a hard gateway failure
        raises :class:`LiveDataError` instead of silently returning replay data.
        """

        env = self.get_market_snapshot(codes, force=force)
        if env.status is EnvelopeStatus.ERROR:
            error = env.typed_error or GatewayError(
                code=GatewayErrorCode.INTERNAL_ERROR,
                message="live data is unavailable",
                retryable=False,
            )
            raise LiveDataError(error.code, error.message, error.retryable)

        try:
            normalized = self._normalise_codes(codes)
        except (TypeError, ValueError) as exc:
            raise LiveDataError(
                GatewayErrorCode.INVALID_REQUEST,
                f"invalid live quote codes: {exc}",
                False,
            ) from exc

        rows = env.data if isinstance(env.data, list) else []
        freshness = env.freshness_status
        return quote_rows_to_payload(
            normalized,
            rows,
            captured_at=env.captured_at_utc or self._utc_clock(),
            freshness=freshness.value,
            ttl_seconds=self._quote_cache.ttl,
            strict=True,
        )

    def get_market_state(self, codes: Sequence[str], *, force: bool = False) -> DataEnvelope:
        try:
            normalized = self._normalise_codes(codes)
        except (TypeError, ValueError) as exc:
            return self._invalid_gateway(
                {"operation": "get_market_state", "codes": []},
                f"invalid live market-state codes: {exc}",
            )
        request = {"operation": "get_market_state", "codes": normalized}
        loader = getattr(self.gateway, "get_market_state", None)
        if loader is None:
            return self._invalid_gateway(
                request, "live gateway does not provide market state"
            )
        return self._read_cached(
            "get_market_state",
            request,
            lambda: loader(normalized),
            self._state_cache,
            force=force,
        )

    def get_option_quotes(self, legs: Sequence[Any], *, force: bool = False) -> DataEnvelope:
        try:
            normalized = _to_option_legs(legs)
        except (TypeError, ValueError) as exc:
            return self._invalid_gateway(
                {"operation": "get_option_quotes", "legs": []},
                f"invalid live option legs: {exc}",
            )
        request = {
            "operation": "get_option_quotes",
            "legs": [leg.to_dict() for leg in normalized],
        }
        loader = getattr(self.gateway, "get_option_quotes", None)
        if loader is None:
            return self._invalid_gateway(
                request, "live gateway does not provide option quotes"
            )
        return self._read_cached(
            "get_option_quotes",
            request,
            lambda: loader(normalized),
            self._option_cache,
            force=force,
        )

    # ------------------------------------------------------------------
    # Live snapshot assembly
    # ------------------------------------------------------------------
    def build_live_snapshot(
        self,
        template_path: str | Path | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Build the live counterpart of :func:`src.decision_pipeline.load_frozen_snapshot`.

        The returned dict has the same top-level schema as
        ``load_frozen_snapshot`` (``mode``, ``origin``, ``freshness``,
        ``captured_at``, ``source``, ``underlying``, ``spot``,
        ``snapshot_sha256``, ``payload``) plus two live-only fields
        (``live_spot`` and ``warnings``) so callers can pass it directly to the
        pipeline steps that consume the frozen-snapshot dictionary.
        """

        path = Path(template_path or self.template_path)
        template, underlying, option_codes = _read_live_template(path)

        template_spot = self._number(template.get("spot"))
        if template_spot is None or template_spot <= 0:
            raise LiveDataError(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "live snapshot template has no usable spot price",
            )

        stale = False
        captured_at = self._utc_clock()

        # Optional gateway health gate: when the gateway exposes a health check
        # the live snapshot must never be built on an unavailable OpenD.  This
        # mirrors the production behaviour that used to live in ui_server's
        # embedded LiveDataService.
        health_loader = getattr(self.gateway, "health", None)
        if health_loader is not None:
            health_env = health_loader()
            if not isinstance(health_env, DataEnvelope):
                raise LiveDataError(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "live gateway health returned a non-envelope value",
                    False,
                )
            if health_env.status is EnvelopeStatus.ERROR:
                self._raise_or_stale(health_env)
            health_data = health_env.data if isinstance(health_env.data, Mapping) else {}
            if health_data.get("ready") is not True:
                raise LiveDataError(
                    GatewayErrorCode.OPEND_UNAVAILABLE,
                    "gateway health check is not ready",
                    True,
                )

        # Optional capabilities validation: preserve the embedded service's
        # protection without duplicating the snapshot schema (the LIVE mode
        # capability declaration is rendered from ``meta.mode`` by the UI).
        capabilities_loader = getattr(self.gateway, "capabilities", None)
        if capabilities_loader is not None:
            capabilities_env = capabilities_loader()
            if not isinstance(capabilities_env, DataEnvelope):
                raise LiveDataError(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "live gateway capabilities returned a non-envelope value",
                    False,
                )
            if capabilities_env.status is EnvelopeStatus.ERROR:
                self._raise_or_stale(capabilities_env)

        # Spot and option snapshots come from get_market_snapshot.  The option
        # legs additionally use get_option_quotes for mid / IV / last price when
        # the gateway provides them; snapshot rows remain the authority for
        # bid/ask/open interest/volume when quote rows omit those fields.
        all_codes = list(dict.fromkeys([underlying] + option_codes))
        snapshot_env = self.get_market_snapshot(all_codes, force=force)
        if snapshot_env.status is EnvelopeStatus.ERROR:
            return self._raise_or_stale(snapshot_env)
        if snapshot_env.freshness_status is FreshnessStatus.STALE or snapshot_env.status is EnvelopeStatus.STALE:
            stale = True

        market_state_env = self.get_market_state([underlying], force=force)
        if market_state_env.status is EnvelopeStatus.ERROR:
            return self._raise_or_stale(market_state_env)
        if market_state_env.freshness_status is FreshnessStatus.STALE or market_state_env.status is EnvelopeStatus.STALE:
            stale = True

        legs_env: DataEnvelope | None = None
        if hasattr(self.gateway, "get_option_quotes"):
            legs_env = self.get_option_quotes(
                [OptionLeg(code, "BUY", 1) for code in option_codes], force=force
            )
            if legs_env.status is EnvelopeStatus.ERROR:
                return self._raise_or_stale(legs_env)
            if legs_env.freshness_status is FreshnessStatus.STALE or legs_env.status is EnvelopeStatus.STALE:
                stale = True

        payload = json.loads(json.dumps(template, ensure_ascii=False))
        payload["underlying"] = underlying
        payload["captured_at"] = captured_at
        payload["source"] = "futuapi/OpenD live"
        payload["market_state"] = self._market_state_text(market_state_env, template)
        live_fields: dict[str, Any] = {
            "spot": None,
            "prev_close": None,
            "legs": [],
        }

        snapshot_rows = snapshot_env.data if isinstance(snapshot_env.data, list) else []
        snapshot_by_code: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            if isinstance(row, Mapping) and isinstance(row.get("code"), str):
                snapshot_by_code[str(row["code"]).upper()] = dict(row)

        for code in option_codes:
            if code not in snapshot_by_code:
                raise LiveDataError(
                    GatewayErrorCode.NOT_FOUND,
                    f"live gateway did not return a quote for {code}",
                    False,
                )

        spot_row = snapshot_by_code.get(underlying) or {}
        spot = spot_row.get("last_price")
        if isinstance(spot, bool) or not isinstance(spot, (int, float)) or not math.isfinite(float(spot)) or float(spot) <= 0:
            raise LiveDataError(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "live market snapshot has no usable spot price",
            )
        live_spot = float(spot)
        payload["spot"] = live_spot
        live_fields["spot"] = live_spot

        prev_close = spot_row.get("previous_close")
        if isinstance(prev_close, (int, float)) and not isinstance(prev_close, bool):
            payload["prev_close"] = float(prev_close)
            live_fields["prev_close"] = float(prev_close)
        else:
            live_fields["prev_close"] = None

        quote_rows = (
            legs_env.data
            if legs_env is not None and isinstance(legs_env.data, list)
            else []
        )
        quote_by_code: dict[str, dict[str, Any]] = {}
        for row in quote_rows:
            if isinstance(row, Mapping) and isinstance(row.get("code"), str):
                quote_by_code[row["code"]] = dict(row)

        option_codes_iter = iter(option_codes)
        for group in payload["legs"]:
            for side in ("call", "put"):
                code = next(option_codes_iter)
                target = group[side]
                snapshot_row = snapshot_by_code.get(code, {})
                quote_row = quote_by_code.get(code, {})
                self._overlay_leg(target, snapshot_row, quote_row)
                live_fields["legs"].append(
                    {
                        "code": code,
                        "last": target.get("last"),
                        "bid": target.get("bid"),
                        "ask": target.get("ask"),
                        "mid": target.get("mid"),
                        "api_iv_pct": target.get("api_iv_pct"),
                        "oi": target.get("open_interest"),
                        "volume": target.get("volume"),
                    }
                )

        # The frozen option-leg template is the operator-validated pricing base.
        # A live spot far away from the template may make the live mid prices
        # unpriceable (negative intrinsic/IV).  In that case the engine keeps the
        # template spot while the snapshot still carries the live quote; the
        # warning makes the decision visible instead of either crashing or
        # silently pretending the live spot was priced.
        engine_spot = template_spot
        warnings: list[str] = []
        if self._legs_priceable(payload, live_spot):
            engine_spot = live_spot
        else:
            warnings.append(
                "live quote spot is outside the option-leg priceable "
                "range; engine prices with the template spot"
            )

        payload["live_fields"] = live_fields
        snapshot_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        freshness = FreshnessStatus.STALE.value if stale else FreshnessStatus.FRESH.value

        return {
            "mode": "LIVE",
            "origin": self._origin(snapshot_env),
            "freshness": freshness,
            "captured_at": captured_at,
            "source": payload["source"],
            "underlying": underlying,
            "spot": engine_spot,
            "live_spot": live_spot,
            "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "payload": payload,
            "warnings": warnings,
        }

    def _raise_or_stale(self, env: DataEnvelope) -> NoReturn:
        """Convert a hard live-data failure into :class:`LiveDataError`."""

        error = env.typed_error or GatewayError(
            code=GatewayErrorCode.INTERNAL_ERROR,
            message="live data is unavailable",
            retryable=False,
        )
        raise LiveDataError(error.code, error.message, error.retryable)

    @staticmethod
    def _market_state_text(env: DataEnvelope, template: Mapping[str, Any]) -> str:
        if env.status is not EnvelopeStatus.ERROR and isinstance(env.data, list):
            for row in env.data:
                if isinstance(row, Mapping) and row.get("market_state"):
                    return str(row["market_state"])
        fallback = template.get("market_state")
        return fallback if isinstance(fallback, str) else "LIVE"

    @staticmethod
    def _origin(env: DataEnvelope) -> str:
        origin = getattr(env, "origin_source", None)
        return origin if isinstance(origin, str) and origin else "FUTU"

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def _overlay_leg(
        self,
        target: dict[str, Any],
        snapshot_row: Mapping[str, Any],
        quote_row: Mapping[str, Any],
    ) -> None:
        """Overlay live fields onto one option leg, never fabricating values.

        Field precedence is explicit: snapshot rows supply last/bid/ask/oi/volume
        and quote rows supply mid/IV (and price when snapshot lacks a price).
        """

        fields: dict[str, tuple[str, ...]] = {
            "last": ("last_price", "price"),
            "bid": ("bid",),
            "ask": ("ask",),
            "mid": ("mid_price",),
            "api_iv_pct": ("implied_volatility", "api_iv_pct"),
            "open_interest": ("open_interest", "oi"),
            "volume": ("volume",),
        }
        mid_overlaid = False
        bid_overlaid = False
        ask_overlaid = False
        for target_key, source_keys in fields.items():
            for source_key in source_keys:
                value = snapshot_row.get(source_key)
                if value is None:
                    value = quote_row.get(source_key)
                number = self._number(value)
                if number is not None:
                    target[target_key] = number
                    # ``open_interest`` is the frozen-template field name; keep
                    # ``oi`` as a live alias for callers that use the shorter
                    # live-field vocabulary.
                    if target_key == "open_interest":
                        target["oi"] = number
                    mid_overlaid = mid_overlaid or target_key == "mid"
                    bid_overlaid = bid_overlaid or target_key == "bid"
                    ask_overlaid = ask_overlaid or target_key == "ask"
                    break

        # Embedded LiveDataService parity: when the gateway supplies live bid/ask
        # but no mid, synthesise mid from bid/ask instead of leaving the frozen
        # template mid in place.
        if not mid_overlaid and bid_overlaid and ask_overlaid:
            bid = self._number(target.get("bid"))
            ask = self._number(target.get("ask"))
            if bid is not None and ask is not None:
                target["mid"] = (bid + ask) / 2.0

    @staticmethod
    def _legs_priceable(payload: Mapping[str, Any], spot: float) -> bool:
        """Return True when every live option mid is above its intrinsic value."""

        try:
            for group in payload.get("legs", []):
                if not isinstance(group, Mapping):
                    return False
                call = group.get("call")
                put = group.get("put")
                if not isinstance(call, Mapping) or not isinstance(put, Mapping):
                    return False
                call_intrinsic = max(float(spot) - float(call["strike"]), 0.0)
                put_intrinsic = max(float(put["strike"]) - float(spot), 0.0)
                if float(call["mid"]) < call_intrinsic:
                    return False
                if float(put["mid"]) < put_intrinsic:
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def refresh(self, *, force: bool = False) -> DataEnvelope:
        """Rebuild the live snapshot and wrap it in a typed envelope.

        ``status``, ``typed_error`` and ``freshness_status`` mirror the gateway
        outcome: OK/FRESH when all live reads are fresh, STALE/STALE when the
        snapshot was rebuilt from at least one stale cached value, ERROR with the
        first gateway error code when live data is unavailable.
        """

        request = {"operation": "refresh_live_snapshot"}
        try:
            snapshot = self.build_live_snapshot(force=force)
        except LiveDataError as exc:
            return DataEnvelope.now(
                mode=self.mode,
                origin_source="APPLICATION",
                freshness_status=FreshnessStatus.UNKNOWN,
                request=request,
                status=EnvelopeStatus.ERROR,
                data=None,
                entitlements={},
                warnings=[],
                typed_error=GatewayError(
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                ),
                captured_at_utc=self._utc_clock(),
            )

        freshness = (
            FreshnessStatus.STALE
            if snapshot.get("freshness") == FreshnessStatus.STALE.value
            else FreshnessStatus.FRESH
        )
        status = EnvelopeStatus.STALE if freshness is FreshnessStatus.STALE else EnvelopeStatus.OK
        warnings: list[str] = []
        typed_error: Optional[GatewayError] = None
        if status is EnvelopeStatus.STALE:
            warnings.append(
                "live snapshot was rebuilt from at least one stale cached quote"
            )
            typed_error = GatewayError(
                code=GatewayErrorCode.STALE_DATA,
                message="live snapshot contains stale quotes",
                retryable=True,
            )

        return DataEnvelope(
            mode=self.mode,
            origin_source=self._origin_from_snapshot(snapshot),
            captured_at_utc=snapshot.get("captured_at") or self._utc_clock(),
            source_time_utc=None,
            freshness_status=freshness,
            request=request,
            status=status,
            data=snapshot,
            entitlements={"live_snapshot": True},
            warnings=warnings,
            typed_error=typed_error,
        )

    @staticmethod
    def _origin_from_snapshot(snapshot: Mapping[str, Any]) -> str:
        origin = snapshot.get("origin")
        return origin if isinstance(origin, str) and origin else "FUTU"


__all__ = [
    "CacheEntry",
    "DEFAULT_QUOTE_TTL_SECONDS",
    "DEFAULT_STATE_TTL_SECONDS",
    "LiveDataError",
    "LiveDataService",
    "LiveQuoteCache",
    "live_template_codes",
    "quote_rows_to_payload",
    "utc_now_iso",
]
