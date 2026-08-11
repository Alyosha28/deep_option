"""Agent-facing, read-only orchestration over the typed market-data gateways.

The decision agent receives one coarse tool.  It never receives an SDK context,
an arbitrary-method escape hatch, or an execution capability.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
import math
import re
from threading import Lock
import time
from typing import Any, Callable, Optional

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
from .payload_validation import PayloadValidationError, validate_operation_payload


_STATUS_RANK = {
    EnvelopeStatus.OK: 0,
    EnvelopeStatus.PARTIAL: 1,
    EnvelopeStatus.STALE: 2,
    EnvelopeStatus.ERROR: 3,
}
_FRESHNESS_RANK = {
    FreshnessStatus.FRESH: 0,
    FreshnessStatus.FROZEN: 1,
    FreshnessStatus.UNKNOWN: 2,
    FreshnessStatus.STALE: 3,
}
_ACCOUNT_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _utc_datetime(value: str) -> datetime:
    """Parse an already envelope-validated UTC timestamp for ordering."""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


class DecisionInputService:
    """Collect deterministic inputs without granting the LLM raw Futu access."""

    def __init__(
        self,
        gateway: Any,
        account_gateway: Optional[Any] = None,
        *,
        refresh_limit: tuple[int, float] = (10, 30.0),
        max_refresh_seconds: float = 30.0,
        monotonic: Optional[Callable[[], float]] = None,
    ):
        max_calls, window_seconds = refresh_limit
        if (
            isinstance(max_calls, bool)
            or not isinstance(max_calls, int)
            or not 0 < max_calls <= 10
            or isinstance(window_seconds, bool)
            or not isinstance(window_seconds, (int, float))
            or not math.isfinite(float(window_seconds))
            or float(window_seconds) < 30.0
        ):
            raise ValueError("refresh_limit may only tighten the default 10 calls per 30 seconds")
        if (
            isinstance(max_refresh_seconds, bool)
            or not isinstance(max_refresh_seconds, (int, float))
            or not math.isfinite(float(max_refresh_seconds))
            or not 0 < float(max_refresh_seconds) <= 30.0
        ):
            raise ValueError("max_refresh_seconds must be within (0, 30]")
        self.gateway = gateway
        self.account_gateway = account_gateway if account_gateway is not None else gateway
        self._refresh_limit = (max_calls, float(window_seconds))
        self._refresh_events: deque[float] = deque()
        self._refresh_lock = Lock()
        self._max_refresh_seconds = float(max_refresh_seconds)
        self._monotonic = monotonic or time.monotonic
        # Store one stable callable object; hosts can register it without exposing
        # the service or discovering additional methods by reflection.
        self.refresh_decision_inputs: Callable[[Mapping[str, Any]], DataEnvelope] = (
            self._refresh_decision_inputs
        )

    def registered_tools(self) -> dict[str, Callable[..., DataEnvelope]]:
        """Return the complete P0a/P0b Agent tool surface."""

        return {"refresh_decision_inputs": self.refresh_decision_inputs}

    def _refresh_decision_inputs(self, scenario: Mapping[str, Any]) -> DataEnvelope:
        if not isinstance(scenario, Mapping):
            return self._invalid_request("scenario must be an object", {})

        underlying = str(scenario.get("underlying", "")).strip().upper()
        expiry = str(scenario.get("expiry", "")).strip()
        if not underlying or "." not in underlying:
            return self._invalid_request(
                "underlying must include a market prefix, for example HK.00700",
                {"underlying": underlying},
            )
        if not expiry:
            return self._invalid_request(
                "expiry must be confirmed before loading an option chain",
                {"underlying": underlying},
            )

        try:
            chain_request = OptionChainRequest(
                underlying=underlying,
                start=expiry,
                end=expiry,
                option_type=str(scenario.get("option_type", "ALL")),
                option_cond_type=str(scenario.get("option_cond_type", "ALL")),
            )
        except (TypeError, ValueError) as exc:
            return self._invalid_request(str(exc), {"underlying": underlying, "expiry": expiry})

        account_ref = scenario.get("account_ref")
        if account_ref is not None and (
            not isinstance(account_ref, str) or not _ACCOUNT_ALIAS.fullmatch(account_ref.strip())
        ):
            return self._invalid_request(
                "account_ref must be a configured non-sensitive alias",
                {"underlying": underlying, "expiry": expiry},
            )
        if isinstance(account_ref, str):
            account_ref = account_ref.strip()

        relevant_codes: Optional[list[str]] = None
        if account_ref:
            raw_codes = scenario.get("position_codes")
            if raw_codes is None:
                raw_codes = [underlying]
            if isinstance(raw_codes, str):
                raw_values = [raw_codes]
            elif isinstance(raw_codes, (list, tuple)):
                raw_values = list(raw_codes)
            else:
                return self._invalid_request(
                    "position_codes must be a symbol or a bounded list of symbols",
                    {"underlying": underlying, "expiry": expiry},
                )
            if not raw_values or len(raw_values) > 800:
                return self._invalid_request(
                    "position_codes must contain between 1 and 800 symbols",
                    {"underlying": underlying, "expiry": expiry},
                )
            try:
                relevant_codes = list(
                    dict.fromkeys(normalize_symbol(value, "position code") for value in raw_values)
                )
            except (TypeError, ValueError) as exc:
                return self._invalid_request(str(exc), {"underlying": underlying, "expiry": expiry})

        limit_error = self._consume_refresh_limit(underlying, expiry)
        if limit_error is not None:
            return limit_error
        refresh_started = self._monotonic()

        try:
            health = self.gateway.health()
        except Exception:
            return self._component_error(
                GatewayErrorCode.INTERNAL_ERROR,
                "gateway health check failed",
                {"operation": "health"},
                mode=self._declared_mode(self.gateway) or DataMode.REPLAY,
            )
        if self._deadline_exceeded(refresh_started):
            return self._deadline_error(
                self._declared_mode(self.gateway) or DataMode.REPLAY,
                "APPLICATION",
                underlying,
                expiry,
            )
        health_validation = self._validate_envelope(
            health,
            expected_mode=self._declared_mode(self.gateway),
            expected_request={"operation": "health"},
            error_request={"operation": "health"},
        )
        if health_validation is not None:
            return health_validation
        outputs: dict[str, DataEnvelope] = {"health": health}
        health_ready = isinstance(health.data, Mapping) and health.data.get("ready") is True
        if health.status is EnvelopeStatus.OK and not health_ready:
            outputs["health"] = self._component_error(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "gateway health check is not ready",
                {"operation": "health"},
                mode=health.mode,
                origin_source=health.origin_source,
            )
            health = outputs["health"]

        account_mode = getattr(self.account_gateway, "mode", None)
        if (
            account_ref
            and isinstance(account_mode, DataMode)
            and account_mode is not health.mode
        ):
            return self._component_error(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "mixed LIVE and REPLAY inputs are not allowed",
                {"operation": "refresh_decision_inputs"},
                mode=health.mode,
                origin_source=health.origin_source,
            )

        if (
            account_ref
            and health.status is EnvelopeStatus.OK
            and health.mode is DataMode.LIVE
            and not self._login_true(
                health.data.get("account_logged_in") if isinstance(health.data, Mapping) else None
            )
        ):
            return self._component_error(
                GatewayErrorCode.ACCOUNT_UNAVAILABLE,
                "Futu account session is not logged in",
                {
                    "operation": "refresh_decision_inputs",
                    "underlying": underlying,
                    "expiry": expiry,
                },
                mode=health.mode,
                origin_source=health.origin_source,
            )

        if health.status is EnvelopeStatus.OK:
            components = (
                (
                    "capabilities",
                    self.gateway.capabilities,
                    {"operation": "capabilities"},
                ),
                (
                    "market_state",
                    lambda: self.gateway.get_market_state([underlying]),
                    {"operation": "get_market_state", "codes": [underlying]},
                ),
                (
                    "underlying_quote",
                    lambda: self.gateway.get_market_snapshot([underlying]),
                    {"operation": "get_market_snapshot", "codes": [underlying]},
                ),
                (
                    "expirations",
                    lambda: self.gateway.get_expiration_dates(underlying),
                    {"operation": "get_expiration_dates", "underlying": underlying},
                ),
                (
                    "option_chain",
                    lambda: self.gateway.get_option_chain(chain_request),
                    {"operation": "get_option_chain", **chain_request.to_dict()},
                ),
            )
            for name, load, expected_request in components:
                if self._deadline_exceeded(refresh_started):
                    return self._deadline_error(health.mode, health.origin_source, underlying, expiry)
                try:
                    item = load()
                except Exception:
                    return self._component_error(
                        GatewayErrorCode.INTERNAL_ERROR,
                        f"{name} input could not be loaded",
                        {
                            "operation": "refresh_decision_inputs",
                            "underlying": underlying,
                            "expiry": expiry,
                        },
                        mode=health.mode,
                        origin_source=health.origin_source,
                    )
                if self._deadline_exceeded(refresh_started):
                    return self._deadline_error(
                        health.mode, health.origin_source, underlying, expiry
                    )
                validation = self._validate_envelope(
                    item,
                    expected_mode=health.mode,
                    expected_request=expected_request,
                    error_request={
                        "operation": "refresh_decision_inputs",
                        "underlying": underlying,
                        "expiry": expiry,
                    },
                    origin_source=health.origin_source,
                )
                if validation is not None:
                    return validation
                outputs[name] = item
                if item.status is not EnvelopeStatus.OK:
                    break
                if name == "capabilities" and isinstance(item.data, Mapping):
                    if item.data.get("market_data") is not True:
                        return self._component_error(
                            GatewayErrorCode.ENTITLEMENT_DENIED,
                            "market-data capability is unavailable",
                            {"operation": "refresh_decision_inputs"},
                            mode=health.mode,
                            origin_source=health.origin_source,
                        )
                    if account_ref and item.data.get("account_read") is not True:
                        return self._component_error(
                            GatewayErrorCode.ACCOUNT_UNAVAILABLE,
                            "account-read capability is unavailable",
                            {"operation": "refresh_decision_inputs"},
                            mode=health.mode,
                            origin_source=health.origin_source,
                        )

        if all(item.status is EnvelopeStatus.OK for item in outputs.values()):
            expiration_data = outputs.get("expirations")
            if expiration_data is not None and not any(
                isinstance(row, Mapping) and row.get("expiry") == expiry
                for row in expiration_data.data
            ):
                return self._component_error(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "expiration evidence does not contain the requested expiry",
                    {"operation": "refresh_decision_inputs"},
                    mode=health.mode,
                    origin_source=health.origin_source,
                )

        if account_ref and all(
            item.status is EnvelopeStatus.OK for item in outputs.values()
        ):
            account_method = getattr(self.account_gateway, "get_account_risk_summary", None)
            if account_method is None:
                outputs["account_risk"] = self._component_error(
                    GatewayErrorCode.ACCOUNT_UNAVAILABLE,
                    "account-read capability is not configured",
                    {"operation": "get_account_risk_summary", "account_ref": str(account_ref)},
                    mode=health.mode,
                    origin_source=health.origin_source,
                )
            else:
                assert relevant_codes is not None
                if self._deadline_exceeded(refresh_started):
                    return self._deadline_error(health.mode, health.origin_source, underlying, expiry)
                try:
                    account_result = account_method(str(account_ref), codes=relevant_codes)
                except Exception:
                    return self._component_error(
                        GatewayErrorCode.INTERNAL_ERROR,
                        "account-risk input could not be loaded",
                        {
                            "operation": "refresh_decision_inputs",
                            "underlying": underlying,
                            "expiry": expiry,
                        },
                        mode=health.mode,
                        origin_source=health.origin_source,
                    )
                if self._deadline_exceeded(refresh_started):
                    return self._deadline_error(
                        health.mode, health.origin_source, underlying, expiry
                    )
                validation = self._validate_envelope(
                    account_result,
                    expected_mode=health.mode,
                    expected_request={
                        "operation": "get_account_risk_summary",
                        "account_ref": str(account_ref),
                        "codes": relevant_codes,
                    },
                    error_request={
                        "operation": "refresh_decision_inputs",
                        "underlying": underlying,
                        "expiry": expiry,
                    },
                    origin_source=health.origin_source,
                )
                if validation is not None:
                    return validation
                outputs["account_risk"] = account_result

        modes = {item.mode for item in outputs.values()}
        if len(modes) != 1:
            return self._component_error(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "mixed LIVE and REPLAY inputs are not allowed",
                {"operation": "refresh_decision_inputs", "underlying": underlying, "expiry": expiry},
            )

        status = max((item.status for item in outputs.values()), key=_STATUS_RANK.__getitem__)
        freshness = max(
            (item.freshness_status for item in outputs.values()),
            key=_FRESHNESS_RANK.__getitem__,
        )
        first_error = next((item.typed_error for item in outputs.values() if item.typed_error), None)
        captured_at = max(
            (item.captured_at_utc for item in outputs.values()),
            key=_utc_datetime,
        )
        source_times = [item.source_time_utc for item in outputs.values() if item.source_time_utc]
        source_time = min(source_times, key=_utc_datetime) if source_times else None
        warnings = [
            f"{name}: {warning}"
            for name, item in outputs.items()
            for warning in item.warnings
        ]

        evidence = {
            name: {
                "snapshot_id": item.snapshot_id,
                "captured_at_utc": item.captured_at_utc,
                "source_time_utc": item.source_time_utc,
                "origin_source": item.origin_source,
                "status": item.status.value,
            }
            for name, item in outputs.items()
        }
        data = None
        if status is not EnvelopeStatus.ERROR:
            data = {name: item.data for name, item in outputs.items()}
            data["evidence"] = evidence

        return DataEnvelope(
            mode=next(iter(modes)),
            origin_source=next(iter(outputs.values())).origin_source,
            captured_at_utc=captured_at,
            source_time_utc=source_time,
            freshness_status=freshness,
            request={
                "operation": "refresh_decision_inputs",
                "underlying": underlying,
                "expiry": expiry,
                "option_type": chain_request.option_type,
                "option_cond_type": chain_request.option_cond_type,
                "has_account_ref": bool(account_ref),
            },
            status=status,
            data=data,
            entitlements={
                name: item.entitlements for name, item in outputs.items() if item.entitlements
            },
            warnings=warnings,
            typed_error=first_error,
        )

    @staticmethod
    def _login_true(value: Any) -> bool:
        if value is True or value == 1:
            return True
        return isinstance(value, str) and value.strip().lower() in {
            "1",
            "true",
            "yes",
            "logged_in",
        }

    def _consume_refresh_limit(self, underlying: str, expiry: str) -> Optional[DataEnvelope]:
        max_calls, window_seconds = self._refresh_limit
        now = self._monotonic()
        with self._refresh_lock:
            while self._refresh_events and now - self._refresh_events[0] >= window_seconds:
                self._refresh_events.popleft()
            if len(self._refresh_events) >= max_calls:
                return self._component_error(
                    GatewayErrorCode.RATE_LIMITED,
                    "decision-input refresh rate limit reached",
                    {
                        "operation": "refresh_decision_inputs",
                        "underlying": underlying,
                        "expiry": expiry,
                    },
                    mode=self._declared_mode(self.gateway) or DataMode.REPLAY,
                )
            self._refresh_events.append(now)
        return None

    def _deadline_exceeded(self, started: float) -> bool:
        return self._monotonic() - started > self._max_refresh_seconds

    @staticmethod
    def _deadline_error(
        mode: DataMode,
        origin_source: str,
        underlying: str,
        expiry: str,
    ) -> DataEnvelope:
        return DecisionInputService._component_error(
            GatewayErrorCode.UPSTREAM_ERROR,
            "decision-input refresh deadline exceeded",
            {
                "operation": "refresh_decision_inputs",
                "underlying": underlying,
                "expiry": expiry,
            },
            mode=mode,
            origin_source=origin_source,
        )

    @staticmethod
    def _declared_mode(gateway: Any) -> Optional[DataMode]:
        mode = getattr(gateway, "mode", None)
        return mode if isinstance(mode, DataMode) else None

    @staticmethod
    def _validate_envelope(
        item: Any,
        *,
        expected_mode: Optional[DataMode],
        expected_request: Mapping[str, Any],
        error_request: Mapping[str, Any],
        origin_source: str = "APPLICATION",
    ) -> Optional[DataEnvelope]:
        if not isinstance(item, DataEnvelope) or not item.verify_integrity():
            return DecisionInputService._component_error(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "gateway returned an invalid or tampered envelope",
                error_request,
                mode=expected_mode or DataMode.REPLAY,
                origin_source=origin_source,
            )
        if expected_mode is not None and item.mode is not expected_mode:
            return DecisionInputService._component_error(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "mixed LIVE and REPLAY inputs are not allowed",
                error_request,
                mode=expected_mode,
                origin_source=origin_source,
            )
        if item.request != dict(expected_request):
            return DecisionInputService._component_error(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "gateway response does not match the requested operation and parameters",
                error_request,
                mode=expected_mode or item.mode,
                origin_source=origin_source,
            )
        if item.status is not EnvelopeStatus.ERROR:
            try:
                validate_operation_payload(item.request, item.data)
            except PayloadValidationError:
                return DecisionInputService._component_error(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "gateway payload failed semantic validation",
                    error_request,
                    mode=expected_mode or item.mode,
                    origin_source=origin_source,
                )
        return None

    def _invalid_request(self, _message: str, _request: Mapping[str, Any]) -> DataEnvelope:
        # Validation exceptions and raw scenario values are untrusted.  Keep
        # the public error stable and never reflect paths, identifiers, or
        # provider-specific parser text into the Agent protocol.
        return self._component_error(
            GatewayErrorCode.INVALID_REQUEST,
            "decision-input request is invalid",
            {"operation": "refresh_decision_inputs"},
            mode=self._declared_mode(self.gateway) or DataMode.REPLAY,
        )

    @staticmethod
    def _component_error(
        code: GatewayErrorCode,
        message: str,
        request: Mapping[str, Any],
        *,
        mode: DataMode = DataMode.REPLAY,
        origin_source: str = "APPLICATION",
    ) -> DataEnvelope:
        return DataEnvelope.now(
            mode=mode,
            origin_source=origin_source,
            freshness_status=FreshnessStatus.UNKNOWN,
            request=dict(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=GatewayError(code=code, message=message, retryable=False),
        )
