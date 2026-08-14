"""FallbackAdapter：兜底数据源占位（Alpaca 美股实时 / Yahoo 历史）。

当前刻意不接入任何管线：比赛版本只允许 Futu 模拟盘数据面。本类保持
「显式不可用」而非抛异常：任何调用都返回 honest 的 ERROR envelope，
调用方校验失败即关闭，绝不静默伪造行情。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.gateway import (
    DataEnvelope,
    DataMode,
    EnvelopeStatus,
    FreshnessStatus,
    GatewayError,
    GatewayErrorCode,
)


class FallbackAdapter:
    """显式不可用的兜底适配器（占位，未接任何管线）。"""

    mode = DataMode.LIVE

    def __init__(self) -> None:
        self.available = False

    def _unavailable(self, operation: str, request: dict[str, Any]) -> DataEnvelope:
        return DataEnvelope(
            mode=DataMode.LIVE,
            origin_source="APPLICATION",
            captured_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source_time_utc=None,
            freshness_status=FreshnessStatus.UNKNOWN,
            request={"operation": operation, **request},
            status=EnvelopeStatus.ERROR,
            data=None,
            entitlements={},
            warnings=["FallbackAdapter is a placeholder and is never wired in"],
            typed_error=GatewayError(
                code=GatewayErrorCode.PROVIDER_ERROR,
                message="fallback data provider is not implemented in this build",
                retryable=False,
            ),
        )

    def get_quote(self, code: str) -> DataEnvelope:
        return self._unavailable("get_quote", {"code": str(code).upper()})

    def health(self) -> DataEnvelope:
        return self._unavailable("health", {})
