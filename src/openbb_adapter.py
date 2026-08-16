"""Optional OpenBB historical-market-data adapter.

OpenBB is deliberately not imported at module import time.  The terminal must
remain usable with the project's frozen replay snapshot when the optional
package, a provider extension, or a provider API key is absent.
"""

from __future__ import annotations

import math
import os
import statistics
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_HISTORY_COLUMNS = ("date", "open", "high", "low", "close", "volume")
_DEFAULT_HV_WINDOW = 30
_DEFAULT_ANNUALIZATION = 252
_MIN_HV_OBSERVATIONS = 5


def normalize_symbol(symbol: str, provider: str | None = None) -> str:
    """Translate the internal symbol into a provider-specific symbol.

    OpenBB documents that symbol conventions vary by provider.  The only
    translation we make here is the one needed by the optional yfinance path;
    all other providers receive the project's original symbol unchanged.
    """

    raw = str(symbol or "").strip()
    if provider and provider.lower() == "yfinance" and raw.upper().startswith("HK."):
        code = raw[3:]
        if code.isdigit():
            code = code.lstrip("0").zfill(4)
        return code + ".HK"
    return raw


def _load_openbb() -> Any:
    from openbb import obb  # type: ignore[import-not-found]

    return obb


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if value is None or isinstance(value, (str, bool, int)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return number if math.isfinite(number) else None


def _records_from_result(result: Any) -> list[Mapping[str, Any]]:
    """Read either current ``to_dataframe`` or older ``to_df`` outputs."""

    frame_method = getattr(result, "to_dataframe", None) or getattr(result, "to_df", None)
    if callable(frame_method):
        frame = frame_method()
        to_dict = getattr(frame, "to_dict", None)
        if callable(to_dict):
            records = to_dict("records")
            if isinstance(records, list):
                normalized = [dict(item) for item in records if isinstance(item, Mapping)]
                index = getattr(frame, "index", None)
                if index is not None:
                    try:
                        index_values = list(index)
                    except TypeError:
                        index_values = []
                    if len(index_values) == len(normalized):
                        for item, index_value in zip(normalized, index_values):
                            item.setdefault("date", index_value)
                return normalized

    raw = getattr(result, "results", result)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def _normalize_rows(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for record in records:
        close = _json_scalar(record.get("close"))
        if close is None:
            continue
        point = {key: _json_scalar(record.get(key)) for key in _HISTORY_COLUMNS}
        if point["date"] is None:
            continue
        points.append(point)
    return points


def add_realized_volatility(
    points: list[Mapping[str, Any]],
    *,
    window: int = _DEFAULT_HV_WINDOW,
    annualization: int = _DEFAULT_ANNUALIZATION,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add rolling annualized historical volatility to daily close rows.

    The value is the sample standard deviation of log returns over the latest
    ``window`` observations, annualized by ``sqrt(252)`` and expressed as a
    percentage.  Rows with too little history keep ``hv30d=None`` instead of
    presenting a misleading short-sample number.
    """

    if window < 2:
        raise ValueError("historical volatility window must be at least 2")
    if annualization < 1:
        raise ValueError("historical volatility annualization must be positive")

    ordered = [dict(point) for point in points]
    ordered.sort(key=lambda point: str(point.get("date", "")))
    returns: list[float] = []
    previous_close: float | None = None
    for point in ordered:
        close_raw = point.get("close")
        try:
            close = float(close_raw)
        except (TypeError, ValueError):
            close = float("nan")
        if previous_close is not None and math.isfinite(close) and close > 0:
            returns.append(math.log(close / previous_close))
        elif not returns:
            returns = []
        previous_close = close if math.isfinite(close) and close > 0 else None
        sample = returns[-window:]
        point[f"hv{window}d"] = (
            statistics.stdev(sample) * math.sqrt(annualization) * 100
            if len(sample) >= _MIN_HV_OBSERVATIONS
            else None
        )

    valid = [point for point in ordered if point.get(f"hv{window}d") is not None]
    latest = ordered[-1] if ordered else {}
    metrics = {
        "rows": len(ordered),
        "firstDate": ordered[0].get("date") if ordered else None,
        "lastDate": latest.get("date") if ordered else None,
        "latestClose": latest.get("close") if ordered else None,
        "latestHv30d": latest.get(f"hv{window}d") if ordered else None,
        "window": window,
        "annualization": annualization,
        "volatilityRows": len(valid),
    }
    return ordered, metrics


def _status(
    *,
    available: bool,
    reason: str | None,
    symbol: str,
    provider: str | None,
    points: list[dict[str, Any]],
    error: str | None = None,
    source: str = "OpenBB",
    metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "available": available,
        "reason": reason,
        "provider": provider or "OpenBB",
        "source": source,
        "symbol": symbol,
        "points": points,
        "metrics": dict(metrics or {}),
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": error[:240] if error else None,
    }


def fetch_historical(
    symbol: str,
    *,
    provider: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    interval: str = "1d",
    period: str = "2y",
    obb_module: Any | None = None,
) -> dict[str, Any]:
    """Fetch daily OHLCV data through OpenBB when explicitly available.

    ``obb_module`` is a test seam.  Passing ``False`` explicitly disables the
    import and is also useful to callers that have already checked capability.
    """

    requested = str(symbol or "").strip()
    resolved = normalize_symbol(requested, provider)
    if not requested:
        return _status(
            available=False,
            reason="missing_symbol",
            symbol=resolved,
            provider=provider,
            points=[],
        )

    if obb_module is False:
        return _status(
            available=False,
            reason="not_installed",
            symbol=resolved,
            provider=provider,
            points=[],
        )
    fallback_to_yfinance = False
    if obb_module is None:
        try:
            obb_module = _load_openbb()
        except (ImportError, ModuleNotFoundError):
            if str(provider or "").lower() != "yfinance":
                return _status(
                    available=False,
                    reason="not_installed",
                    symbol=resolved,
                    provider=provider,
                    points=[],
                )
            try:
                import yfinance as yfinance  # type: ignore[import-not-found]
            except (ImportError, ModuleNotFoundError):
                return _status(
                    available=False,
                    reason="not_installed",
                    symbol=resolved,
                    provider=provider,
                    points=[],
                )
            obb_module = yfinance
            fallback_to_yfinance = True

    try:
        if fallback_to_yfinance:
            cache_dir = Path(
                os.getenv(
                    "GOAI_YFINANCE_CACHE_DIR",
                    str(Path(tempfile.gettempdir()) / "goai-yfinance-cache"),
                )
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            set_cache_location = getattr(obb_module, "set_tz_cache_location", None)
            if callable(set_cache_location):
                set_cache_location(str(cache_dir))
            ticker = obb_module.Ticker(resolved)
            kwargs = {"interval": interval, "auto_adjust": False}
            if start_date or end_date:
                kwargs.update({"start": start_date, "end": end_date})
            else:
                kwargs["period"] = period
            result = ticker.history(**kwargs)
            frame = result.reset_index()
            records = [
                {str(key).lower().replace(" ", "_"): value for key, value in row.items()}
                for row in frame.to_dict("records")
            ]
        else:
            historical = obb_module.equity.price.historical
            kwargs = {
                "symbol": resolved,
                "interval": interval,
            }
            if provider:
                kwargs["provider"] = provider
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            result = historical(**kwargs)
            records = _records_from_result(result)
        points = _normalize_rows(records)
        points, metrics = add_realized_volatility(points)
    except Exception as exc:  # provider extensions expose heterogeneous errors
        return _status(
            available=False,
            reason="provider_error",
            symbol=resolved,
            provider=provider,
            points=[],
            error=str(exc),
        )

    actual_provider = str(
        getattr(result, "provider", "")
        or provider
        or ("yfinance" if fallback_to_yfinance else "OpenBB")
    )
    warnings = getattr(result, "warnings", None)
    status = _status(
        available=bool(points),
        reason=None if points else "no_rows",
        symbol=resolved,
        provider=actual_provider,
        points=points,
        source="yfinance direct" if fallback_to_yfinance else "OpenBB",
        metrics=metrics,
    )
    if warnings:
        status["warning"] = str(warnings)[:240]
    return status


def configured_provider() -> str | None:
    """Read the historical provider without making one provider mandatory."""

    value = os.getenv("GOAI_OPENBB_PROVIDER", "").strip().lower()
    return value or None
