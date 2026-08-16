"""Futu SIMULATE 模拟盘订单提交窄边界（P0c）。

设计不变量：

- 比赛版本唯一允许的交易环境 = SIMULATE：由 :class:`AccountBinding` 构造期
  硬约束 + worker 内 trd_env 硬编码双保险，实盘无法经此边界触发；
- 不提供、不调用 unlock_trade（交易解锁必须在 OpenD GUI 手动完成）；
- 订单经受监督子进程（:mod:`src.futu_trade_worker`）短生命周期提交：
  硬截止 20s、响应大小上限、固定 schema；只允许 BUY 腿、至多 2 条；
- 本模块导入安全：不导入 Futu SDK；SDK 只存在于 worker 子进程。
"""

from __future__ import annotations

import ipaddress
import json
import math
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from .gateway import (
    AccountBinding,
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
    normalize_symbol,
)

_MAX_ORDER_LEGS = 2
_MAX_ORDER_QTY = 10_000
_WORKER_TIMEOUT_SECONDS = 20.0
_MAX_WORKER_RESPONSE_BYTES = 256 * 1024
_ALLOWED_SIDES = {"BUY"}


@dataclass(frozen=True, slots=True)
class SimulatedOrderRequest:
    """One long-only simulated option order leg."""

    code: str
    qty: int
    price: float
    side: str = "BUY"

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalize_symbol(self.code, "order code"))
        side = str(self.side).strip().upper()
        if side not in _ALLOWED_SIDES:
            raise ValueError(
                "competition build only submits BUY legs (long straddle); "
                f"got {side!r}"
            )
        object.__setattr__(self, "side", side)
        if (
            isinstance(self.qty, bool)
            or not isinstance(self.qty, int)
            or not 1 <= self.qty <= _MAX_ORDER_QTY
        ):
            raise ValueError(
                f"order qty must be an integer between 1 and {_MAX_ORDER_QTY}"
            )
        if (
            isinstance(self.price, bool)
            or not isinstance(self.price, (int, float))
            or not math.isfinite(float(self.price))
            or float(self.price) <= 0
        ):
            raise ValueError("order price must be a positive finite number")
        object.__setattr__(self, "price", float(self.price))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "qty": self.qty, "price": self.price}


def _default_worker_runner(args: Sequence[str], stdin_text: str, timeout: float) -> Any:
    return subprocess.run(
        list(args),
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class SimulatedTradeGateway:
    """Typed SIMULATE order-submission boundary over a supervised worker."""

    mode = DataMode.LIVE

    def __init__(
        self,
        binding: AccountBinding,
        host: str = "127.0.0.1",
        port: int = 11111,
        *,
        worker_runner: Optional[Callable[[Sequence[str], str, float], Any]] = None,
        utc_clock: Optional[Callable[[], str]] = None,
    ) -> None:
        if not isinstance(binding, AccountBinding):
            raise ValueError("binding must be an AccountBinding")
        if binding.trd_env != "SIMULATE":
            raise ValueError("competition trade gateway only accepts SIMULATE bindings")
        try:
            host_address = ipaddress.ip_address(str(host).strip())
        except ValueError as exc:
            raise ValueError("trade host must be an IP literal") from exc
        if not host_address.is_loopback:
            raise ValueError("competition trade gateway only connects to loopback OpenD")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("trade port must be an integer between 1 and 65535")
        self.host = host_address.compressed
        self.port = port
        self.binding = binding
        self._worker_runner = worker_runner or _default_worker_runner
        self._utc_clock = utc_clock or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

    def place_order(self, orders: Sequence[SimulatedOrderRequest]) -> DataEnvelope:
        """Submit the validated BUY legs and return typed receipts.

        Never raises for gateway failures: returns a typed ERROR envelope.
        """

        request = {
            "operation": "place_order",
            "orders": [order.to_dict() for order in orders],
        }
        try:
            normalized = list(orders)
        except (TypeError, ValueError) as exc:
            return self._invalid(request, exc)
        if not normalized:
            return self._invalid(request, ValueError("at least one order leg is required"))
        if len(normalized) > _MAX_ORDER_LEGS:
            return self._invalid(
                request,
                ValueError(f"competition build submits at most {_MAX_ORDER_LEGS} legs"),
            )

        worker = pathlib.Path(__file__).with_name("futu_trade_worker.py")
        payload = {
            "host": self.host,
            "port": self.port,
            "acc_id": self.binding.sdk_acc_id,
            "security_firm": self.binding.security_firm,
            "orders": [order.to_dict() for order in normalized],
        }
        try:
            completed = self._worker_runner(
                [sys.executable, str(worker)],
                json.dumps(payload, separators=(",", ":")),
                _WORKER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.UPSTREAM_ERROR,
                    "simulated order worker exceeded its hard deadline",
                    True,
                ),
            )
        except OSError:
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.OPEND_UNAVAILABLE,
                    "simulated order worker could not be started",
                    True,
                ),
            )

        encoded = (completed.stdout or "").encode("utf-8", errors="replace")
        if (
            completed.returncode != 0
            or not encoded
            or len(encoded) > _MAX_WORKER_RESPONSE_BYTES
        ):
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.UPSTREAM_ERROR,
                    "simulated order worker returned an invalid response",
                    True,
                ),
            )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "simulated order worker returned non-JSON output",
                    False,
                ),
            )
        if not isinstance(response, dict):
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "simulated order worker payload failed schema validation",
                    False,
                ),
            )
        if response.get("ok") is not True:
            error_name = str(response.get("error_code") or "UPSTREAM_ERROR")
            allowed = {
                "ACCOUNT_UNAVAILABLE": GatewayErrorCode.ACCOUNT_UNAVAILABLE,
                "ENTITLEMENT_DENIED": GatewayErrorCode.ENTITLEMENT_DENIED,
                "INVALID_REQUEST": GatewayErrorCode.INVALID_REQUEST,
                "SCHEMA_MISMATCH": GatewayErrorCode.SCHEMA_MISMATCH,
                "SDK_INCOMPATIBLE": GatewayErrorCode.SDK_INCOMPATIBLE,
                "TRADE_UNLOCK_REQUIRED": GatewayErrorCode.TRADE_UNLOCK_REQUIRED,
                "UPSTREAM_ERROR": GatewayErrorCode.UPSTREAM_ERROR,
            }
            code = allowed.get(error_name, GatewayErrorCode.UPSTREAM_ERROR)
            message = {
                "TRADE_UNLOCK_REQUIRED": (
                    "模拟盘交易未解锁：请在 OpenD GUI 手动完成交易解锁后重试"
                ),
                "ENTITLEMENT_DENIED": "模拟盘下单权限被拒",
                "ACCOUNT_UNAVAILABLE": "模拟盘账户不可用（未登录或账户无效）",
            }.get(error_name, "simulated order submission failed")
            return self._error(
                request,
                GatewayError(code, message, code in (GatewayErrorCode.UPSTREAM_ERROR,)),
            )

        receipts = response.get("orders")
        if (
            not isinstance(receipts, list)
            or len(receipts) != len(normalized)
            or not all(isinstance(row, dict) for row in receipts)
        ):
            return self._error(
                request,
                GatewayError(
                    GatewayErrorCode.SCHEMA_MISMATCH,
                    "simulated order receipt count does not match the request",
                    False,
                ),
            )
        submitted_at = self._utc_clock()
        rows: list[dict[str, Any]] = []
        expected_codes = {order.code for order in normalized}
        for row in receipts:
            order_id = row.get("order_id")
            code = str(row.get("code") or "").upper()
            if not order_id or code not in expected_codes:
                return self._error(
                    request,
                    GatewayError(
                        GatewayErrorCode.SCHEMA_MISMATCH,
                        "simulated order receipt is missing order identity",
                        False,
                    ),
                )
            rows.append(
                {
                    "order_id": str(order_id),
                    "code": code,
                    "qty": row.get("qty"),
                    "price": row.get("price"),
                    "status": str(row.get("status") or "SUBMITTED"),
                    "submitted_at_utc": submitted_at,
                }
            )
        return DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="FUTU_SIMULATE",
            captured_at_utc=submitted_at,
            source_time_utc=None,
            freshness_status=FreshnessStatus.FRESH,
            request=request,
            status=EnvelopeStatus.OK,
            data=rows,
            entitlements={"execution": "simulate"},
            warnings=[],
            typed_error=None,
        )

    # ------------------------------------------------------------------
    def _invalid(self, request: Mapping[str, Any], exc: Exception) -> DataEnvelope:
        return self._error(
            request,
            GatewayError(GatewayErrorCode.INVALID_REQUEST, str(exc), False),
        )

    def _error(self, request: Mapping[str, Any], error: GatewayError) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="APPLICATION",
            captured_at_utc=self._utc_clock(),
            source_time_utc=None,
            freshness_status=FreshnessStatus.UNKNOWN,
            request=dict(request),
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=[],
            typed_error=error,
        )

    def close(self) -> None:
        # Worker 是每次调用一个短生命周期子进程，无长生命周期资源。
        return None


__all__ = [
    "SimulatedOrderRequest",
    "SimulatedTradeGateway",
]
