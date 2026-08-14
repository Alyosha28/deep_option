"""Futu OpenAPI 只读数据适配层。

依赖：固定版本的 futu-api；实际 OpenD 兼容性由 typed health 验证；
OpenD 已启动并登录（默认 127.0.0.1:11111）。

``FutuLiveGateway`` 返回统一 ``DataEnvelope``，长期复用只读行情 Context；
默认账户读取在有硬截止的受监督子进程中创建短生命周期 Context。
``FutuAdapter`` 是同一窄只读边界的兼容类名。导入本模块不会导入 Futu SDK。
"""

from __future__ import annotations

import ipaddress
import json
import math
import pathlib
import re
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime, timezone
from itertools import islice
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .gateway import (
    AccountBinding,
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    OptionChainRequest,
    OptionLeg,
    normalize_symbol,
)
from .payload_validation import PayloadValidationError, validate_operation_payload

_SNAPSHOT_BATCH_SIZE = 400
_MAX_CODES_PER_REQUEST = 800
_MAX_OPTION_LEGS = 8
_MAX_RESULT_ROWS = 10_000
_MAX_PROVIDER_TEXT_LENGTH = 4_096
_MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
_SYNC_QUERY_CONNECT_TIMEOUT_SECONDS = 5.0
_ACCOUNT_WORKER_TIMEOUT_SECONDS = 20.0
_MAX_ACCOUNT_WORKER_RESPONSE_BYTES = 4 * 1024 * 1024
_TYPE_ALIASES = {
    "CALL": {"CALL", "C", "涨", "认购"},
    "PUT": {"PUT", "P", "跌", "认沽"},
}
_ACCOUNT_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DEFAULT_RATE_LIMITS: Mapping[str, tuple[int, float]] = {
    # Conservative local guards for endpoints without a documented upstream
    # quota, followed by limits verified from the installed futuapi Skill.
    "health": (10, 30.0),
    "get_market_state": (10, 30.0),
    "get_trading_days": (10, 30.0),
    "get_market_snapshot": (10, 30.0),
    "get_option_chain": (60, 30.0),
    "get_expiration_dates": (60, 30.0),
    "get_option_quotes": (30, 30.0),
    "get_strategy_quote": (30, 30.0),
    "get_account_risk_summary": (10, 30.0),
}

_MARKET_SNAPSHOT_FIELDS = {
    "code": "code",
    "name": "name",
    "update_time": "updated_at",
    "last_price": "last_price",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "prev_close_price": "previous_close",
    "volume": "volume",
    "turnover": "turnover",
    "bid_price": "bid",
    "ask_price": "ask",
    "bid_vol": "bid_size",
    "ask_vol": "ask_size",
    "lot_size": "lot_size",
    # Non-Greek option contract facts required by the pricing engine.
    "option_type": "option_type",
    "stock_owner": "underlying",
    "strike_time": "expiry",
    "option_strike_price": "strike",
    "option_contract_size": "contract_size",
    "option_open_interest": "open_interest",
    "option_net_open_interest": "net_open_interest",
    "option_implied_volatility": "implied_volatility",
    "option_area_type": "exercise_type",
}
_MARKET_STATE_FIELDS = {"code": "code", "market_state": "market_state"}
_TRADING_DAY_FIELDS = {"time": "date", "trade_date_type": "trade_date_type"}
_EXPIRATION_FIELDS = {
    "strike_time": "expiry",
    "option_expiry_date_distance": "days_to_expiry",
    "expiration_cycle": "expiration_cycle",
}
_OPTION_CHAIN_FIELDS = {
    "code": "code",
    "name": "name",
    "lot_size": "lot_size",
    "option_type": "option_type",
    "stock_owner": "underlying",
    "strike_time": "expiry",
    "strike_price": "strike",
    "option_standard_type": "standard_type",
    "option_settlement_mode": "settlement_mode",
}
_OPTION_QUOTE_FIELDS = {
    "code": "code",
    "price": "price",
    "mid_price": "mid_price",
    "change_val": "change",
    "change_rate": "change_rate",
    "volume": "volume",
    "turnover": "turnover",
    "high_price": "high",
    "low_price": "low",
    "option_type": "option_type",
    "strike_price": "strike",
    "expire_time": "expiry",
    "implied_volatility": "implied_volatility",
    "open_interest": "open_interest",
    "contract_size": "contract_size",
    "exercise_type": "exercise_type",
}
_STRATEGY_QUOTE_FIELDS = {
    "code": "code",
    "name": "name",
    "option_strategy": "option_strategy",
    "bid1": "bid1",
    "ask1": "ask1",
    "max_profit": "max_profit",
    "max_loss": "max_loss",
    "breakeven_points": "breakeven_points",
    "prob_of_profit": "probability_of_profit",
}
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


class _GatewayFailure(Exception):
    """Internal exception converted to a typed live envelope at the boundary."""

    def __init__(self, code: GatewayErrorCode, message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _default_opend_probe(host: str, port: int) -> bool:
    """Probe only the configured local TCP endpoint; never starts OpenD."""

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class FutuLiveGateway:
    """Typed and strictly read-only Futu gateway with bounded SDK lifecycles.

    Context factories and the TCP probe are injectable so the entire surface can
    be contract-tested without importing the SDK or connecting to OpenD.
    """

    mode = DataMode.LIVE

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        *,
        quote_context_factory: Optional[Callable[[], Any]] = None,
        account_context_factory: Optional[Callable[[AccountBinding], Any]] = None,
        account_bindings: Optional[Mapping[str, AccountBinding]] = None,
        recorder: Any = None,
        opend_probe: Optional[Callable[[str, int], bool]] = None,
        clock: Optional[Callable[[], Any]] = None,
        rate_limits: Optional[Mapping[str, tuple[int, float]]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        freshness_max_age_seconds: float = 60.0,
    ) -> None:
        try:
            host_address = ipaddress.ip_address(str(host).strip())
        except ValueError as exc:
            raise ValueError("OpenD host must be a loopback IP literal") from exc
        if not host_address.is_loopback:
            raise ValueError("Competition build only connects to a loopback Futu OpenD host")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("OpenD port must be an integer between 1 and 65535")
        if (
            isinstance(freshness_max_age_seconds, bool)
            or not isinstance(freshness_max_age_seconds, (int, float))
            or not math.isfinite(float(freshness_max_age_seconds))
            or float(freshness_max_age_seconds) <= 0
        ):
            raise ValueError("freshness_max_age_seconds must be a positive finite number")

        self.host = host_address.compressed
        self.port = port
        self._uses_default_quote_factory = quote_context_factory is None
        self._uses_default_account_factory = account_context_factory is None
        self._quote_context_factory = quote_context_factory or self._make_quote_context
        self._account_context_factory = account_context_factory
        self._account_bindings = dict(account_bindings or {})
        for alias, binding in self._account_bindings.items():
            if not isinstance(binding, AccountBinding) or alias != binding.account_ref:
                raise ValueError("account binding keys must exactly match their validated alias")
        self._recorder = recorder
        self._opend_probe = opend_probe or _default_opend_probe
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._freshness_max_age_seconds = float(freshness_max_age_seconds)
        effective_rate_limits = dict(_DEFAULT_RATE_LIMITS)
        if rate_limits is not None:
            overrides = self._validate_rate_limits(rate_limits)
            for operation, policy in overrides.items():
                default = _DEFAULT_RATE_LIMITS.get(operation)
                if default is not None and (policy[0] > default[0] or policy[1] < default[1]):
                    raise ValueError("custom rate limits may only tighten safety defaults")
            effective_rate_limits.update(overrides)
        self._rate_limits = self._validate_rate_limits(effective_rate_limits)
        self._rate_events: Dict[str, deque[float]] = {
            operation: deque() for operation in self._rate_limits
        }
        self._quote_context: Any = None
        self._account_contexts: Dict[str, Any] = {}
        self._lock = RLock()
        self._closed = False

    # ---------- lifecycle and context construction ----------

    def __enter__(self) -> "FutuLiveGateway":
        return self

    def __exit__(self, exc_type: Any, _exc: Any, _traceback: Any) -> None:
        try:
            self.close()
        except RuntimeError:
            # Never replace an exception raised inside the context body with a
            # cleanup failure.  Explicit close() still reports and can retry it.
            if exc_type is None:
                raise

    def close(self) -> None:
        """Close every long-lived context exactly once; safe to call repeatedly."""

        with self._lock:
            contexts: List[Any] = []
            if self._quote_context is not None:
                contexts.append(self._quote_context)
            contexts.extend(self._account_contexts.values())
            self._closed = True

            seen: set[int] = set()
            failed: set[int] = set()
            for context in contexts:
                if id(context) in seen:
                    continue
                seen.add(id(context))
                try:
                    context.close()
                except Exception:
                    failed.add(id(context))

            if self._quote_context is not None and id(self._quote_context) not in failed:
                self._quote_context = None
            self._account_contexts = {
                alias: context
                for alias, context in self._account_contexts.items()
                if id(context) in failed
            }
            if failed:
                raise RuntimeError("one or more Futu contexts failed to close")

    def _probe(self) -> None:
        try:
            ready = bool(self._opend_probe(self.host, self.port))
        except Exception:
            raise _GatewayFailure(
                self._code("OPEND_UNAVAILABLE"),
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            ) from None
        if not ready:
            raise _GatewayFailure(
                self._code("OPEND_UNAVAILABLE"),
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            )

    def _quote(self) -> Any:
        with self._lock:
            if self._closed:
                raise _GatewayFailure(
                    self._code("OPEND_UNAVAILABLE"),
                    "Futu gateway is closed",
                    False,
                )
            if self._quote_context is None:
                self._probe()
                try:
                    self._quote_context = self._quote_context_factory()
                except _GatewayFailure:
                    raise
                except ImportError:
                    raise _GatewayFailure(
                        self._code("SDK_UNAVAILABLE", "UPSTREAM_ERROR"),
                        "Futu SDK is unavailable",
                        False,
                    ) from None
                except Exception as exc:
                    raise self._exception_failure(exc) from None
            return self._quote_context

    def _account(self, binding: AccountBinding) -> Any:
        account_ref = binding.account_ref
        with self._lock:
            if self._closed:
                raise _GatewayFailure(
                    self._code("OPEND_UNAVAILABLE"),
                    "Futu gateway is closed",
                    False,
                )
            if account_ref not in self._account_contexts:
                factory = self._account_context_factory
                if factory is None:
                    raise _GatewayFailure(
                        self._code("ACCOUNT_UNAVAILABLE"),
                        "Default account reads require the supervised worker",
                        False,
                    )
                self._probe()
                try:
                    self._account_contexts[account_ref] = factory(binding)
                except _GatewayFailure:
                    raise
                except ImportError:
                    raise _GatewayFailure(
                        self._code("SDK_UNAVAILABLE", "UPSTREAM_ERROR"),
                        "Futu SDK is unavailable",
                        False,
                    ) from None
                except Exception as exc:
                    raise self._exception_failure(exc, account=True) from None
            return self._account_contexts[account_ref]

    def _drop_quote_context(self) -> None:
        """连接级失败后丢弃缓存的 quote context；下一次调用按需重建。"""
        with self._lock:
            context = self._quote_context
            if context is None:
                return
            self._quote_context = None
            try:
                context.close()
            except Exception:
                pass

    def _drop_account_context(self, account_ref: str) -> None:
        """丢弃指定账户的缓存 context；下一次账户调用按需重建。"""
        with self._lock:
            context = self._account_contexts.pop(account_ref, None)
            if context is None:
                return
            try:
                context.close()
            except Exception:
                pass

    def _note_connection_failure(
        self, failure: _GatewayFailure, *, account_ref: str | None = None
    ) -> None:
        """连接级失败后丢弃缓存连接，避免 OpenD 重启后旧 context 被永久复用。

        只重置真正的连接类失败（OpenD 不可达 / SDK 错误）；限频、权限、
        业务校验类失败不动缓存。
        """
        if failure.code in (
            self._code("OPEND_UNAVAILABLE"),
            self._code("UPSTREAM_ERROR", "SDK_ERROR"),
            self._code("SDK_ERROR"),
        ):
            self._drop_quote_context()
            return
        if failure.code == self._code("ACCOUNT_UNAVAILABLE") and account_ref is not None:
            self._drop_account_context(account_ref)

    def _make_quote_context(self) -> Any:
        # Deliberately imported only after the loopback probe succeeds.
        from futu import OpenQuoteContext

        context = OpenQuoteContext(
            host=self.host,
            port=self.port,
            is_async_connect=True,
        )
        self._configure_sync_connect_timeout(context)
        return context

    def _configure_sync_connect_timeout(self, context: Any) -> None:
        setter = getattr(context, "set_sync_query_connect_timeout", None)
        if not callable(setter):
            try:
                context.close()
            except Exception:
                pass
            raise _GatewayFailure(
                self._code("SDK_INCOMPATIBLE"),
                "Futu SDK is missing required connection-timeout support",
                False,
            )
        try:
            setter(_SYNC_QUERY_CONNECT_TIMEOUT_SECONDS)
        except Exception:
            try:
                context.close()
            except Exception:
                pass
            raise _GatewayFailure(
                self._code("SDK_INCOMPATIBLE"),
                "Futu SDK rejected required connection-timeout configuration",
                False,
            ) from None

    # ---------- public read-only surface ----------

    def health(self) -> DataEnvelope:
        request = {"operation": "health"}
        try:
            with self._lock:
                self._check_rate_limit("health")
                ret, raw = self._quote().get_global_state()
            self._ensure_ok(ret, raw)
            state = self._object_mapping(raw)
            if not self._is_explicit_true(state.get("qot_logined")):
                raise _GatewayFailure(
                    self._code("AUTH_FAILED"),
                    "Futu quote service is not logged in",
                    False,
                )
            source_time = state.get("timestamp")
            data = {
                "ready": True,
                "server_version": self._normal_value(state.get("server_ver")),
                "quote_logged_in": self._normal_value(state.get("qot_logined")),
                "account_logged_in": self._normal_value(state.get("trd_logined")),
            }
            return self._success(request, data, source_time=source_time)
        except _GatewayFailure as failure:
            self._note_connection_failure(failure)
            return self._error(request, failure)
        except Exception as exc:
            failure = self._exception_failure(exc)
            self._note_connection_failure(failure)
            return self._error(request, failure)

    def capabilities(self) -> DataEnvelope:
        request = {"operation": "capabilities"}
        return self._success(
            request,
            {
                "market_data": True,
                "account_read": bool(self._account_bindings),
                "strategy_combination_quote": True,
                "execution": False,
                "real_trading": False,
            },
        )

    def get_market_state(self, codes: Iterable[str]) -> DataEnvelope:
        try:
            normalized = self._codes(codes)
        except (TypeError, ValueError) as exc:
            request = {"operation": "get_market_state", "codes": []}
            return self._error(request, self._invalid_failure(exc))
        request = {"operation": "get_market_state", "codes": normalized}
        return self._quote_records(
            request,
            "get_market_state",
            (normalized,),
            _MARKET_STATE_FIELDS,
            required_fields=("code", "market_state"),
        )

    def get_trading_days(
        self,
        market: str,
        start: str,
        end: str,
    ) -> DataEnvelope:
        market_name = (
            market.strip().upper()
            if isinstance(market, str) and len(market) <= 16
            else ""
        )
        request = {
            "operation": "get_trading_days",
            "market": market_name,
            "start": start,
            "end": end,
        }
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", market_name):
            return self._error(request, self._invalid_failure(ValueError("market is required")))
        try:
            if not isinstance(start, str) or not isinstance(end, str):
                raise TypeError("start and end are required")
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            return self._error(request, self._invalid_failure(exc))
        if (
            start_date.isoformat() != start
            or end_date.isoformat() != end
            or end_date < start_date
            or (end_date - start_date).days + 1 > 366
        ):
            return self._error(
                request,
                self._invalid_failure(
                    ValueError("trading-day range must be canonical, ordered and at most 366 days")
                ),
            )
        kwargs: Dict[str, Any] = {"market": market_name}
        if start:
            kwargs["start"] = start
        if end:
            kwargs["end"] = end
        return self._quote_records(
            request,
            "request_trading_days",
            (),
            _TRADING_DAY_FIELDS,
            kwargs=kwargs,
            sdk_enums={"market": "TradeDateMarket"},
            required_fields=("date",),
        )

    def get_market_snapshot(self, codes: Iterable[str]) -> DataEnvelope:
        try:
            normalized = self._codes(codes)
        except (TypeError, ValueError) as exc:
            request = {"operation": "get_market_snapshot", "codes": []}
            return self._error(request, self._invalid_failure(exc))
        request = {"operation": "get_market_snapshot", "codes": normalized}
        try:
            output: List[Dict[str, Any]] = []
            with self._lock:
                context = self._quote()
                for offset in range(0, len(normalized), _SNAPSHOT_BATCH_SIZE):
                    batch = normalized[offset : offset + _SNAPSHOT_BATCH_SIZE]
                    self._check_rate_limit("get_market_snapshot")
                    ret, raw = context.get_market_snapshot(batch)
                    self._ensure_ok(ret, raw)
                    output.extend(self._whitelist(self._records(raw), _MARKET_SNAPSHOT_FIELDS))
                    if len(output) > _MAX_RESULT_ROWS:
                        raise _GatewayFailure(
                            self._code("SCHEMA_MISMATCH"),
                            "Futu response exceeds the configured row limit",
                            False,
                        )
            if not output:
                raise _GatewayFailure(
                    self._code("NOT_FOUND"),
                    "Futu returned no market snapshots",
                    False,
                )
            if any(row.get("code") is None or row.get("last_price") is None for row in output):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu market snapshot is missing required fields",
                    False,
                )
            returned_codes = [row.get("code") for row in output]
            if len(returned_codes) != len(normalized) or set(returned_codes) != set(normalized):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu market snapshot does not cover the exact requested symbols",
                    False,
                )
            if any(not self._is_finite_number(row.get("last_price")) for row in output):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu market snapshot contains an invalid price",
                    False,
                )
            parsed_source_times = [
                self._snapshot_source_time(row.get("updated_at"), row.get("code"))
                for row in output
            ]
            if any(value is None for value in parsed_source_times):
                return self._success(
                    request,
                    output,
                    status=EnvelopeStatus.PARTIAL,
                    freshness_status=FreshnessStatus.UNKNOWN,
                    warnings=[
                        "Vendor update_time has no explicit timezone; source freshness is unknown"
                    ],
                )
            captured_for_batch = self._now()
            if any(
                self._source_age_seconds(captured_for_batch, value)
                < -_MAX_FUTURE_CLOCK_SKEW_SECONDS
                for value in parsed_source_times
                if value is not None
            ):
                return self._success(
                    request,
                    output,
                    status=EnvelopeStatus.PARTIAL,
                    freshness_status=FreshnessStatus.UNKNOWN,
                    warnings=[
                        "At least one source timestamp is ahead of the local clock beyond the allowed skew"
                    ],
                )
            source_time = min(value for value in parsed_source_times if value is not None)
            return self._success(request, output, source_time=source_time)
        except _GatewayFailure as failure:
            self._note_connection_failure(failure)
            return self._error(request, failure)
        except Exception as exc:
            failure = self._exception_failure(exc)
            self._note_connection_failure(failure)
            return self._error(request, failure)

    def get_expiration_dates(self, underlying: str) -> DataEnvelope:
        try:
            code = self._code_value(underlying)
        except (TypeError, ValueError) as exc:
            request = {"operation": "get_expiration_dates", "underlying": str(underlying).upper()}
            return self._error(request, self._invalid_failure(exc))
        request = {"operation": "get_expiration_dates", "underlying": code}
        return self._quote_records(
            request,
            "get_option_expiration_date",
            (code,),
            _EXPIRATION_FIELDS,
            require_data=True,
            required_fields=("expiry",),
        )

    def get_option_chain(self, request_data: OptionChainRequest) -> DataEnvelope:
        try:
            underlying = self._code_value(request_data.underlying)
            start = request_data.start
            end = request_data.end
            option_type = request_data.option_type
            option_cond_type = request_data.option_cond_type
        except (AttributeError, ValueError) as exc:
            request = {"operation": "get_option_chain"}
            return self._error(request, self._invalid_failure(exc))
        request = {
            "operation": "get_option_chain",
            "underlying": underlying,
            "start": start,
            "end": end,
            "option_type": option_type,
            "option_cond_type": option_cond_type,
        }
        kwargs: Dict[str, Any] = {"start": start, "end": end}
        if option_type and option_type != "ALL":
            kwargs["option_type"] = option_type
        if option_cond_type and option_cond_type != "ALL":
            kwargs["option_cond_type"] = option_cond_type
        return self._quote_records(
            request,
            "get_option_chain",
            (underlying,),
            _OPTION_CHAIN_FIELDS,
            kwargs=kwargs,
            sdk_enums={
                "option_type": "OptionType",
                "option_cond_type": "OptionCondType",
            },
            require_data=True,
            required_fields=("code", "underlying", "option_type", "expiry", "strike"),
        )

    def resolve_option_code(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
    ) -> DataEnvelope:
        """Resolve one exact contract from an expiry-scoped option chain."""

        canonical_type = str(option_type).strip().upper()
        for candidate, aliases in _TYPE_ALIASES.items():
            if canonical_type in aliases:
                canonical_type = candidate
                break
        request = {
            "operation": "resolve_option_code",
            "underlying": str(underlying).strip().upper(),
            "expiry": str(expiry),
            "strike": self._normal_value(strike),
            "option_type": canonical_type,
        }
        try:
            chain_request = OptionChainRequest(
                underlying=underlying,
                start=expiry,
                end=expiry,
                option_type=canonical_type,
            )
            target_strike = float(strike)
            if not math.isfinite(target_strike) or target_strike <= 0:
                raise ValueError("strike must be a positive finite number")
            request["strike"] = target_strike
            with self._lock:
                self._check_rate_limit("get_option_chain")
                context = self._quote()
                kwargs: Dict[str, Any] = {"start": expiry, "end": expiry}
                if self._uses_default_quote_factory:
                    kwargs["option_type"] = self._sdk_enum("OptionType", canonical_type)
                else:
                    kwargs["option_type"] = canonical_type
                ret, raw = context.get_option_chain(chain_request.underlying, **kwargs)
            self._ensure_ok(ret, raw)
            raw_rows = self._records(raw)
            if len(raw_rows) > _MAX_RESULT_ROWS:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu option-chain response exceeds the configured row limit",
                    False,
                )
            rows = self._whitelist(raw_rows, _OPTION_CHAIN_FIELDS)
            matches = []
            for row in rows:
                row_strike = row.get("strike")
                try:
                    strike_matches = (
                        row_strike is not None
                        and abs(float(row_strike) - target_strike) < 0.001
                    )
                except (TypeError, ValueError):
                    strike_matches = False
                if (
                    row.get("expiry") == expiry
                    and row.get("option_type") == canonical_type
                    and strike_matches
                ):
                    matches.append(row)
            if not matches:
                raise _GatewayFailure(
                    self._code("NOT_FOUND"),
                    "No option contract matched the requested expiry, strike, and type",
                    False,
                )
            if len(matches) > 1:
                raise _GatewayFailure(
                    self._code("AMBIGUOUS_MATCH"),
                    "More than one option contract matched the exact request",
                    False,
                )
            return self._success(request, matches[0])
        except _GatewayFailure as failure:
            self._note_connection_failure(failure)
            return self._error(request, failure)
        except (TypeError, ValueError) as exc:
            return self._error(request, self._invalid_failure(exc))
        except Exception as exc:
            failure = self._exception_failure(exc)
            self._note_connection_failure(failure)
            return self._error(request, failure)

    def get_option_quotes(self, legs: Sequence[OptionLeg]) -> DataEnvelope:
        request: Dict[str, Any]
        try:
            normalized = self._legs(legs)
        except (TypeError, ValueError) as exc:
            request = {"operation": "get_option_quotes", "legs": []}
            return self._error(request, self._invalid_failure(exc))
        request = {
            "operation": "get_option_quotes",
            "legs": self._leg_request(normalized),
        }
        try:
            codes = list(dict.fromkeys(leg.code for leg in normalized))
            with self._lock:
                context = self._quote()
                self._check_rate_limit("get_market_snapshot")
                snapshot_ret, snapshot_raw = context.get_market_snapshot(codes)
                self._ensure_ok(snapshot_ret, snapshot_raw)
                quote_raw_rows: List[Any] = []
                # Futu's option-quote response has no contract-code column.  A
                # one-leg request is therefore the only stable way to bind a
                # response row to a requested identity without fabricating it.
                for leg in normalized:
                    self._check_rate_limit("get_option_quotes")
                    sdk_legs = self._sdk_option_legs([leg])
                    quote_ret, quote_raw = context.get_option_quote(sdk_legs)
                    self._ensure_ok(quote_ret, quote_raw)
                    records = self._records(quote_raw)
                    if not records:
                        raise _GatewayFailure(
                            self._code("NOT_FOUND"),
                            "Futu returned no option quote for a requested leg",
                            False,
                        )
                    if len(records) != 1:
                        raise _GatewayFailure(
                            self._code("SCHEMA_MISMATCH"),
                            "Futu option quote row count does not match one requested leg",
                            False,
                        )
                    quote_raw_rows.append(records[0])

            snapshot_rows = self._whitelist(
                self._records(snapshot_raw), _MARKET_SNAPSHOT_FIELDS
            )
            quote_rows = self._whitelist(quote_raw_rows, _OPTION_QUOTE_FIELDS)
            if len(snapshot_rows) != len(codes) or len(quote_rows) != len(normalized):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu option quote row count does not match the requested legs",
                    False,
                )
            if not all(isinstance(row.get("code"), str) for row in snapshot_rows):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu option snapshots contain an invalid contract identity",
                    False,
                )
            snapshot_by_code = {row["code"]: row for row in snapshot_rows}
            if len(snapshot_by_code) != len(codes) or set(snapshot_by_code) != set(codes):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu option snapshots do not cover the exact requested legs",
                    False,
                )
            for leg, quote_row in zip(normalized, quote_rows):
                snapshot_row = snapshot_by_code[leg.code]
                if not self._same_option_contract(snapshot_row, quote_row):
                    raise _GatewayFailure(
                        self._code("SCHEMA_MISMATCH"),
                        "Futu option quote order or contract facts do not match the requested legs",
                        False,
                    )
                quote_row["code"] = leg.code
            return self._success(request, quote_rows)
        except _GatewayFailure as failure:
            self._note_connection_failure(failure)
            return self._error(request, failure)
        except Exception as exc:
            failure = self._exception_failure(exc)
            self._note_connection_failure(failure)
            return self._error(request, failure)

    def get_option_quote(self, legs: Sequence[OptionLeg]) -> DataEnvelope:
        """Compatibility spelling; new callers use ``get_option_quotes``."""

        return self.get_option_quotes(legs)

    def get_strategy_quote(self, legs: Sequence[OptionLeg]) -> DataEnvelope:
        """Return vendor-computed combination bid/ask; never synthesize leg prices."""

        request: Dict[str, Any]
        try:
            normalized = self._legs(legs)
        except (TypeError, ValueError) as exc:
            request = {"operation": "get_strategy_quote", "legs": []}
            return self._error(request, self._invalid_failure(exc))
        request = {
            "operation": "get_strategy_quote",
            "legs": self._leg_request(normalized),
        }
        return self._quote_records(
            request,
            "get_option_strategy_analysis",
            (normalized,),
            _STRATEGY_QUOTE_FIELDS,
            option_legs=True,
            require_data=True,
            required_fields=("bid1", "ask1"),
            expected_rows=1,
        )

    def get_account_risk_summary(
        self,
        account_ref: str,
        codes: Optional[Iterable[str]] = None,
    ) -> DataEnvelope:
        alias = str(account_ref).strip()
        if not _ACCOUNT_ALIAS.fullmatch(alias):
            invalid_request = {
                "operation": "get_account_risk_summary",
                "account_ref": "<invalid>",
            }
            return self._error(
                invalid_request,
                self._invalid_failure(ValueError("account_ref must be a configured alias")),
            )
        request: Dict[str, Any] = {"operation": "get_account_risk_summary", "account_ref": alias}
        normalized_codes: Optional[List[str]] = None
        if codes is not None:
            try:
                normalized_codes = self._codes(codes)
            except (TypeError, ValueError) as exc:
                request["codes"] = []
                return self._error(request, self._invalid_failure(exc))
            request["codes"] = normalized_codes

        binding = self._account_bindings.get(alias)
        if binding is None:
            return self._error(
                request,
                _GatewayFailure(
                    self._code("ACCOUNT_UNAVAILABLE"),
                    "Account alias is not configured",
                    False,
                ),
            )

        try:
            sdk_acc_id = self._binding_acc_id(binding)
            if self._uses_default_account_factory:
                with self._lock:
                    if self._closed:
                        raise _GatewayFailure(
                            self._code("OPEND_UNAVAILABLE"),
                            "Futu gateway is closed",
                            False,
                        )
                    self._check_rate_limit("get_account_risk_summary")
                    # Coordinate the short-lived worker with close(): the lock
                    # prevents close from returning while a result can still
                    # be accepted.  The worker itself has a hard 20s deadline.
                    raw_info_records, raw_positions = self._query_account_worker(binding)
            else:
                query_kwargs = {
                    "trd_env": binding.trd_env,
                    "acc_id": sdk_acc_id,
                    "refresh_cache": True,
                    "currency": binding.currency,
                }
                with self._lock:
                    context = self._account(binding)
                    self._check_rate_limit("get_account_risk_summary")
                    info_ret, info_raw = context.accinfo_query(**query_kwargs)
                    self._ensure_ok(info_ret, info_raw, account=True)
                    position_ret, position_raw = context.position_list_query(**query_kwargs)
                    self._ensure_ok(position_ret, position_raw, account=True)
                raw_info_records = self._records(info_raw)
                raw_positions = self._records(position_raw)
            if len(raw_info_records) > 1 or len(raw_positions) > _MAX_RESULT_ROWS:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu account response exceeds the configured row limit",
                    False,
                )
            if self._uses_default_account_factory:
                info_records = [dict(row) for row in raw_info_records]
                positions = [dict(row) for row in raw_positions]
            else:
                info_records = self._whitelist(raw_info_records, _ACCOUNT_FIELDS)
                positions = self._whitelist(raw_positions, _POSITION_FIELDS)
            if not info_records:
                raise _GatewayFailure(
                    self._code("NOT_FOUND"),
                    "Futu returned no account risk data",
                    False,
                )
            if len(info_records) != 1:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu account response must contain exactly one risk summary",
                    False,
                )
            risk_row = info_records[0]
            liquidity_fields = ("cash", "available_funds", "power")
            if not self._is_finite_number(risk_row.get("total_assets")) or not any(
                self._is_finite_number(risk_row.get(field)) for field in liquidity_fields
            ):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu account response is missing finite equity or liquidity facts",
                    False,
                )
            if float(risk_row["total_assets"]) < 0:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu account equity must not be negative",
                    False,
                )
            try:
                positions_valid = all(
                    normalize_symbol(row.get("code"), "position code") == row.get("code")
                    and self._is_finite_number(row.get("quantity"))
                    and self._is_finite_number(row.get("market_value"))
                    for row in positions
                )
            except (TypeError, ValueError):
                positions_valid = False
            if not positions_valid:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu position response is missing canonical identity or quantity",
                    False,
                )
            if normalized_codes is not None:
                allowed = set(normalized_codes)
                positions = [row for row in positions if row.get("code") in allowed]
            summary: Dict[str, Any] = {
                "account_ref": alias,
                "currency": self._normal_value(binding.currency),
            }
            if info_records:
                summary.update(info_records[0])
            # The configured currency is authoritative and never an account identifier.
            summary["currency"] = self._normal_value(binding.currency) or summary.get("currency")
            summary["positions"] = positions
            return self._success(request, summary, entitlements={"account_read": "available"})
        except _GatewayFailure as failure:
            self._note_connection_failure(failure, account_ref=alias)
            return self._error(
                request,
                self._redact_account_failure(failure),
                entitlement_domain="account_read",
            )
        except Exception as exc:
            failure = self._exception_failure(exc, account=True)
            self._note_connection_failure(failure, account_ref=alias)
            return self._error(request, failure)

    def _query_account_worker(
        self, binding: AccountBinding
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        worker = pathlib.Path(__file__).with_name("futu_account_worker.py")
        payload = {
            "host": self.host,
            "port": self.port,
            "acc_id": self._binding_acc_id(binding),
            "trd_env": binding.trd_env,
            "currency": binding.currency,
            "security_firm": binding.security_firm,
        }
        try:
            completed = subprocess.run(
                [sys.executable, str(worker)],
                input=json.dumps(payload, separators=(",", ":")),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                timeout=_ACCOUNT_WORKER_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise _GatewayFailure(
                self._code("ACCOUNT_UNAVAILABLE"),
                "Futu account worker exceeded its hard deadline",
                True,
            ) from None
        except OSError:
            raise _GatewayFailure(
                self._code("ACCOUNT_UNAVAILABLE"),
                "Futu account worker could not be started",
                True,
            ) from None
        encoded = completed.stdout.encode("utf-8", errors="replace")
        if (
            completed.returncode != 0
            or not encoded
            or len(encoded) > _MAX_ACCOUNT_WORKER_RESPONSE_BYTES
        ):
            raise _GatewayFailure(
                self._code("ACCOUNT_UNAVAILABLE"),
                "Futu account worker returned an invalid response",
                True,
            )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise _GatewayFailure(
                self._code("ACCOUNT_UNAVAILABLE"),
                "Futu account worker returned an invalid response",
                True,
            ) from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            error_name = response.get("error_code") if isinstance(response, dict) else None
            allowed = {
                "ACCOUNT_UNAVAILABLE": self._code("ACCOUNT_UNAVAILABLE"),
                "ENTITLEMENT_DENIED": self._code("ENTITLEMENT_DENIED"),
                "SDK_INCOMPATIBLE": self._code("SDK_INCOMPATIBLE"),
            }
            raise _GatewayFailure(
                allowed.get(str(error_name), self._code("ACCOUNT_UNAVAILABLE")),
                "Futu account worker could not provide account data",
                error_name == "ACCOUNT_UNAVAILABLE",
            )
        info = response.get("info")
        positions = response.get("positions")
        if (
            not isinstance(info, list)
            or not isinstance(positions, list)
            or not all(isinstance(row, dict) for row in [*info, *positions])
        ):
            raise _GatewayFailure(
                self._code("SCHEMA_MISMATCH"),
                "Futu account worker payload failed schema validation",
                False,
            )
        return [dict(row) for row in info], [dict(row) for row in positions]

    # ---------- SDK call, envelope and normalization helpers ----------

    def _quote_records(
        self,
        request: Dict[str, Any],
        method_name: str,
        args: tuple[Any, ...],
        fields: Mapping[str, str],
        *,
        kwargs: Optional[Dict[str, Any]] = None,
        sdk_enums: Optional[Mapping[str, str]] = None,
        option_legs: bool = False,
        require_data: bool = False,
        required_fields: Sequence[str] = (),
        required_any_fields: Sequence[str] = (),
        expected_rows: Optional[int] = None,
    ) -> DataEnvelope:
        try:
            with self._lock:
                self._check_rate_limit(str(request.get("operation", method_name)))
                context = self._quote()
                method = getattr(context, method_name)
                call_args = args
                if option_legs:
                    call_args = (self._sdk_option_legs(args[0]),)
                call_kwargs = dict(kwargs or {})
                if self._uses_default_quote_factory:
                    for key, enum_name in (sdk_enums or {}).items():
                        if key in call_kwargs:
                            call_kwargs[key] = self._sdk_enum(enum_name, call_kwargs[key])
                ret, raw = method(*call_args, **call_kwargs)
            self._ensure_ok(ret, raw)
            records = self._records(raw)
            if len(records) > _MAX_RESULT_ROWS:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu response exceeds the configured row limit",
                    False,
                )
            data = self._whitelist(records, fields)
            if require_data and not data:
                raise _GatewayFailure(
                    self._code("NOT_FOUND"),
                    "Futu returned no matching option contracts",
                    False,
                )
            if expected_rows is not None and len(data) != expected_rows:
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu response row count does not match the request",
                    False,
                )
            if any(
                any(field not in row or row[field] is None for field in required_fields)
                for row in data
            ):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu response is missing required fields",
                    False,
                )
            if required_any_fields and any(
                not any(row.get(field) is not None for field in required_any_fields)
                for row in data
            ):
                raise _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu response is missing required quote values",
                    False,
                )
            return self._success(request, data)
        except _GatewayFailure as failure:
            self._note_connection_failure(failure)
            return self._error(request, failure)
        except Exception as exc:
            failure = self._exception_failure(exc)
            self._note_connection_failure(failure)
            return self._error(request, failure)

    def _ensure_ok(self, ret: Any, raw: Any, *, account: bool = False) -> None:
        if ret == 0:
            return
        message = self._safe_message(raw)
        lowered = message.lower()
        if any(
            token in lowered
            for token in (
                "permission",
                "authority",
                "entitlement",
                "no quota",
                "quota",
                "bmp",
                "lv1",
                "lv2",
                "权限",
                "未开通",
                "未购买",
            )
        ):
            message = (
                "Futu account read entitlement denied"
                if account
                else "Futu market-data entitlement denied"
            )
            raise _GatewayFailure(self._code("ENTITLEMENT_DENIED"), message, False)
        if any(token in lowered for token in ("rate limit", "too frequent", "frequency", "限频", "频率")):
            raise _GatewayFailure(
                self._code("RATE_LIMITED", "UPSTREAM_ERROR"),
                "Futu upstream rate limit reached",
                True,
            )
        if account:
            raise _GatewayFailure(self._code("ACCOUNT_UNAVAILABLE"), "Futu account query failed", True)
        raise _GatewayFailure(
            self._code("UPSTREAM_ERROR", "SDK_ERROR"),
            "Futu market-data request failed",
            True,
        )

    def _success(
        self,
        request: Dict[str, Any],
        data: Any,
        *,
        source_time: Optional[Any] = None,
        entitlements: Optional[Dict[str, Any]] = None,
        status: EnvelopeStatus = EnvelopeStatus.OK,
        freshness_status: Optional[FreshnessStatus] = None,
        warnings: Optional[List[str]] = None,
    ) -> DataEnvelope:
        try:
            validate_operation_payload(request, data)
        except PayloadValidationError:
            return self._error(
                request,
                _GatewayFailure(
                    self._code("SCHEMA_MISMATCH"),
                    "Futu payload failed semantic validation",
                    False,
                ),
            )
        normalized_source_time = self._source_time(source_time)
        captured_at = self._now()
        output_status = status
        output_warnings = list(warnings or [])
        if freshness_status is not None:
            freshness = freshness_status
        elif normalized_source_time is None:
            freshness = FreshnessStatus.UNKNOWN
        else:
            source_age_seconds = self._source_age_seconds(captured_at, normalized_source_time)
            if source_age_seconds < -_MAX_FUTURE_CLOCK_SKEW_SECONDS:
                freshness = FreshnessStatus.UNKNOWN
                if output_status is EnvelopeStatus.OK:
                    output_status = EnvelopeStatus.PARTIAL
                output_warnings.append(
                    "Source timestamp is ahead of the local clock beyond the allowed skew"
                )
            elif source_age_seconds > self._freshness_max_age_seconds:
                freshness = FreshnessStatus.STALE
                if output_status is EnvelopeStatus.OK:
                    output_status = EnvelopeStatus.STALE
                output_warnings.append(
                    "Source timestamp exceeds the configured live-data freshness window"
                )
            else:
                freshness = FreshnessStatus.FRESH
        envelope = DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            captured_at_utc=captured_at,
            source_time_utc=normalized_source_time,
            freshness_status=freshness,
            request=request,
            status=output_status,
            data=data,
            entitlements=entitlements or {"market_data": "available"},
            warnings=output_warnings,
            typed_error=None,
        )
        return self._record(envelope)

    def _error(
        self,
        request: Dict[str, Any],
        failure: _GatewayFailure,
        *,
        entitlement_domain: str = "market_data",
    ) -> DataEnvelope:
        entitlements: Dict[str, Any] = {}
        if failure.code == self._code("ENTITLEMENT_DENIED"):
            entitlements[entitlement_domain] = "denied"
        envelope = DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU",
            captured_at_utc=self._now(),
            source_time_utc=None,
            freshness_status=FreshnessStatus.UNKNOWN,
            request=request,
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements=entitlements,
            warnings=[],
            typed_error=GatewayError(
                code=failure.code,
                message=failure.message,
                retryable=failure.retryable,
            ),
        )
        return self._record(envelope)

    def _record(self, envelope: DataEnvelope) -> DataEnvelope:
        if self._recorder is None:
            return envelope
        try:
            self._recorder.record(envelope, tag=str(envelope.request.get("operation", "futu_live")))
        except Exception:
            warning = "Snapshot recording failed"
            failure = envelope.typed_error or GatewayError(
                code=self._code("INTERNAL_ERROR"),
                message=warning,
                retryable=True,
            )
            return DataEnvelope(
                mode=envelope.mode,
                origin_source=envelope.origin_source,
                captured_at_utc=envelope.captured_at_utc,
                source_time_utc=envelope.source_time_utc,
                freshness_status=envelope.freshness_status,
                request=envelope.request,
                status=EnvelopeStatus.ERROR,
                data=None,
                entitlements=envelope.entitlements,
                warnings=[*envelope.warnings, warning],
                typed_error=failure,
            )
        return envelope

    def _now(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @classmethod
    def _source_time(cls, value: Any) -> Optional[str]:
        normalized = cls._normal_value(value)
        if isinstance(normalized, bool) or normalized is None:
            return None
        if isinstance(normalized, (int, float)) or (
            isinstance(normalized, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", normalized)
        ):
            try:
                seconds = float(normalized)
                if not math.isfinite(seconds) or seconds <= 0:
                    return None
                return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        if not isinstance(normalized, str):
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()

    @classmethod
    def _snapshot_source_time(cls, value: Any, code: Any) -> Optional[str]:
        parsed_aware = cls._source_time(value)
        if parsed_aware is not None:
            return parsed_aware
        normalized = cls._normal_value(value)
        if not isinstance(normalized, str) or not isinstance(code, str):
            return None
        market = code.split(".", 1)[0].upper()
        zone_name = {
            "HK": "Asia/Shanghai",
            "SH": "Asia/Shanghai",
            "SZ": "Asia/Shanghai",
            "US": "America/New_York",
        }.get(market)
        if zone_name is None:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
        return parsed.replace(tzinfo=ZoneInfo(zone_name)).astimezone(timezone.utc).isoformat()

    @staticmethod
    def _source_age_seconds(captured_at: str, source_time: str) -> float:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        source = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        return (captured - source).total_seconds()

    @classmethod
    def _is_explicit_true(cls, value: Any) -> bool:
        normalized = cls._normal_value(value)
        if normalized is True or normalized == 1:
            return True
        return isinstance(normalized, str) and normalized.strip().lower() in {
            "1",
            "true",
            "yes",
            "logged_in",
        }

    @classmethod
    def _is_explicit_false(cls, value: Any) -> bool:
        normalized = cls._normal_value(value)
        if normalized is False or normalized == 0:
            return True
        return isinstance(normalized, str) and normalized.strip().lower() in {
            "0",
            "false",
            "no",
            "not_logged_in",
        }

    @staticmethod
    def _records(raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        try:
            raw_size = len(raw)
        except (TypeError, AttributeError):
            raw_size = None
        if raw_size is not None and raw_size > _MAX_RESULT_ROWS:
            raise _GatewayFailure(
                GatewayErrorCode.SCHEMA_MISMATCH,
                "Futu response exceeds the configured row limit",
                False,
            )
        if hasattr(raw, "to_dict"):
            try:
                result = raw.to_dict(orient="records")
                if len(result) > _MAX_RESULT_ROWS:
                    raise _GatewayFailure(
                        GatewayErrorCode.SCHEMA_MISMATCH,
                        "Futu response exceeds the configured row limit",
                        False,
                    )
                return [dict(row) for row in result]
            except (TypeError, ValueError):
                result = raw.to_dict()
                if isinstance(result, dict):
                    return [dict(result)]
        if isinstance(raw, Mapping):
            return [dict(raw)]
        if isinstance(raw, (list, tuple)):
            return [dict(row) if isinstance(row, Mapping) else {"value": row} for row in raw]
        return [{"value": raw}]

    @classmethod
    def _whitelist(
        cls,
        records: Iterable[Mapping[str, Any]],
        fields: Mapping[str, str],
    ) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for record in records:
            normalized: Dict[str, Any] = {}
            for source, target in fields.items():
                if source in record:
                    normalized[target] = cls._normal_value(record[source])
            if "code" in normalized and isinstance(normalized["code"], str):
                normalized["code"] = normalized["code"].upper()
            if "underlying" in normalized and isinstance(normalized["underlying"], str):
                normalized["underlying"] = normalized["underlying"].upper()
            output.append(normalized)
        return output

    @classmethod
    def _normal_value(cls, value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "item"):
            try:
                value = value.item()
            except (TypeError, ValueError):
                pass
        if isinstance(value, str):
            stripped = value.strip()
            if len(stripped) > _MAX_PROVIDER_TEXT_LENGTH:
                raise _GatewayFailure(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "Futu response contains an oversized text field",
                    False,
                )
            if stripped.upper() in {"", "N/A", "NA", "NONE", "NULL", "--"}:
                return None
            return stripped
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, Mapping):
            return {str(key): cls._normal_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._normal_value(item) for item in value]
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (datetime,)):
            return value.isoformat()
        if hasattr(value, "name"):
            return str(value.name)
        return str(value)

    @staticmethod
    def _object_mapping(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, Mapping):
            return dict(raw)
        if hasattr(raw, "to_dict"):
            result = raw.to_dict()
            if isinstance(result, Mapping):
                return dict(result)
        return {}

    @staticmethod
    def _first_time(records: Iterable[Mapping[str, Any]], field: str) -> Optional[Any]:
        for record in records:
            if record.get(field):
                return record[field]
        return None

    @staticmethod
    def _code_value(code: Any) -> str:
        return normalize_symbol(code, "security code")

    @classmethod
    def _codes(cls, codes: Iterable[str]) -> List[str]:
        if isinstance(codes, str):
            values = [codes]
        else:
            values = list(islice(iter(codes), _MAX_CODES_PER_REQUEST + 1))
            if len(values) > _MAX_CODES_PER_REQUEST:
                raise ValueError(f"at most {_MAX_CODES_PER_REQUEST} security codes are allowed")
        normalized = [cls._code_value(code) for code in values]
        if not normalized:
            raise ValueError("at least one security code is required")
        # Preserve order while keeping deterministic requests and avoiding duplicate SDK calls.
        unique = list(dict.fromkeys(normalized))
        if len(unique) > _MAX_CODES_PER_REQUEST:
            raise ValueError(f"at most {_MAX_CODES_PER_REQUEST} security codes are allowed")
        return unique

    @staticmethod
    def _legs(legs: Sequence[OptionLeg]) -> List[OptionLeg]:
        values = list(islice(iter(legs), _MAX_OPTION_LEGS + 1))
        if not values:
            raise ValueError("at least one option leg is required")
        if len(values) > _MAX_OPTION_LEGS:
            raise ValueError(f"at most {_MAX_OPTION_LEGS} option legs are allowed")
        for leg in values:
            if not all(hasattr(leg, name) for name in ("code", "action", "quantity")):
                raise ValueError("invalid option leg")
        return values

    @staticmethod
    def _is_finite_number(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )

    @classmethod
    def _same_option_contract(
        cls,
        snapshot: Mapping[str, Any],
        quote: Mapping[str, Any],
    ) -> bool:
        if str(snapshot.get("option_type", "")).upper() != str(
            quote.get("option_type", "")
        ).upper():
            return False
        if snapshot.get("expiry") != quote.get("expiry"):
            return False
        numeric_fields = ("strike", "contract_size")
        for field in numeric_fields:
            if not cls._is_finite_number(snapshot.get(field)) or not cls._is_finite_number(
                quote.get(field)
            ):
                return False
            if abs(float(snapshot[field]) - float(quote[field])) > 0.001:
                return False
        return any(cls._is_finite_number(quote.get(field)) for field in ("price", "mid_price"))

    @staticmethod
    def _leg_request(legs: Sequence[OptionLeg]) -> List[Dict[str, Any]]:
        return [
            {"code": leg.code, "action": leg.action, "quantity": leg.quantity}
            for leg in legs
        ]

    @staticmethod
    def _validate_rate_limits(
        rate_limits: Mapping[str, tuple[int, float]],
    ) -> Dict[str, tuple[int, float]]:
        validated: Dict[str, tuple[int, float]] = {}
        for operation, policy in rate_limits.items():
            if not isinstance(operation, str) or not operation.strip():
                raise ValueError("rate-limit operation names must be non-empty strings")
            if not isinstance(policy, (tuple, list)) or len(policy) != 2:
                raise ValueError("each rate limit must be a (max_calls, window_seconds) pair")
            max_calls, window_seconds = policy
            if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
                raise ValueError("rate-limit max_calls must be a positive integer")
            if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)):
                raise ValueError("rate-limit window_seconds must be positive")
            window = float(window_seconds)
            if not math.isfinite(window) or window <= 0:
                raise ValueError("rate-limit window_seconds must be positive")
            validated[operation.strip()] = (max_calls, window)
        return validated

    def _check_rate_limit(self, operation: str) -> None:
        policy = self._rate_limits.get(operation)
        if policy is None:
            return
        max_calls, window_seconds = policy
        now = float(self._monotonic())
        events = self._rate_events[operation]
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= max_calls:
            raise _GatewayFailure(
                self._code("RATE_LIMITED"),
                f"Local Futu rate limit reached for {operation}",
                True,
            )
        events.append(now)

    def _sdk_option_legs(self, legs: Sequence[OptionLeg]) -> List[Any]:
        if not self._uses_default_quote_factory:
            return list(legs)
        # This path is reached only after callers have requested live data. The
        # actual SDK import remains lazy and no CLI helper is imported.
        import futu
        from futu import OptionStrategyLeg

        action_type = getattr(futu, "OptionStrategyAction", None) or getattr(
            futu, "StrategyLegAction", None
        )
        if action_type is None:
            raise _GatewayFailure(
                self._code("SDK_INCOMPATIBLE", "UPSTREAM_ERROR"),
                "Futu SDK does not expose an option-strategy action enum",
                False,
            )

        result = []
        for value in legs:
            leg = OptionStrategyLeg()
            leg.code = value.code
            leg.action = getattr(action_type, value.action)
            leg.quantity = float(value.quantity)
            result.append(leg)
        return result

    @staticmethod
    def _sdk_enum(enum_name: str, value: Any) -> Any:
        # Import only inside a live call; injected test contexts receive raw strings.
        try:
            import futu
        except ImportError:
            raise _GatewayFailure(
                FutuLiveGateway._code("SDK_UNAVAILABLE"),
                "Futu SDK is unavailable",
                False,
            ) from None
        enum_type = getattr(futu, enum_name, None)
        key = str(value).upper()
        if enum_type is None or not hasattr(enum_type, key):
            raise _GatewayFailure(
                FutuLiveGateway._code("SDK_INCOMPATIBLE"),
                "Futu SDK is missing a required enum member",
                False,
            )
        return getattr(enum_type, key)

    @staticmethod
    def _binding_acc_id(binding: AccountBinding) -> int:
        return int(binding.sdk_acc_id)

    @staticmethod
    def _safe_message(value: Any) -> str:
        text = str(value).strip()
        if not text:
            return "Futu upstream request failed"
        text = re.sub(r"(?i)\b(?:token|password|passwd|secret|bearer|acc_id|card)\s*[:=]\s*\S+", "<redacted>", text)
        text = re.sub(r"(?i)[A-Z]:\\[^\s]+", "<path>", text)
        text = re.sub(r"\b\d{6,}\b", "<redacted-id>", text)
        return text[:240]

    @classmethod
    def _exception_failure(cls, exc: Exception, *, account: bool = False) -> _GatewayFailure:
        message = cls._safe_message(exc)
        lowered = message.lower()
        if account:
            return _GatewayFailure(
                cls._code("ACCOUNT_UNAVAILABLE"),
                "Futu account query failed",
                True,
            )
        if any(token in lowered for token in ("refused", "timed out", "timeout", "10061", "broken pipe")):
            return _GatewayFailure(
                cls._code("OPEND_UNAVAILABLE"),
                "OpenD connection failed",
                True,
            )
        return _GatewayFailure(
            cls._code("UPSTREAM_ERROR", "SDK_ERROR"),
            "Futu SDK request failed",
            True,
        )

    @classmethod
    def _invalid_failure(cls, _exc: Exception) -> _GatewayFailure:
        return _GatewayFailure(
            cls._code("INVALID_REQUEST", "UPSTREAM_ERROR"),
            "Invalid Futu gateway request",
            False,
        )

    @staticmethod
    def _redact_account_failure(failure: _GatewayFailure) -> _GatewayFailure:
        if failure.code == FutuLiveGateway._code("ENTITLEMENT_DENIED"):
            return _GatewayFailure(failure.code, "Futu account read entitlement denied", False)
        return _GatewayFailure(failure.code, "Futu account query failed", failure.retryable)

    @staticmethod
    def _code(name: str, fallback: Optional[str] = None) -> GatewayErrorCode:
        if hasattr(GatewayErrorCode, name):
            return getattr(GatewayErrorCode, name)
        if fallback and hasattr(GatewayErrorCode, fallback):
            return getattr(GatewayErrorCode, fallback)
        # Every contract includes OPEND_UNAVAILABLE; this is only a defensive
        # compatibility fallback for older gateway modules during migration.
        return GatewayErrorCode.OPEND_UNAVAILABLE


class FutuAdapter(FutuLiveGateway):
    """Deprecated name for the same typed, read-only gateway.

    The former adapter exposed a raw SDK context and subscription mutations;
    those escape hatches are intentionally not preserved.
    """
