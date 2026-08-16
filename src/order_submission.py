"""P0c 模拟提交闭环编排：READY_FOR_CONFIRMATION → 人机确认 → SIMULATE 下单 → 回执入审计。

铁律：
- 只有 Action 门 = READY_FOR_CONFIRMATION、LIVE/FRESH 数据、用户独立确认
  （确认语原文匹配）三条件同时满足才会触达下单边界；
- 交易环境只可能是 SIMULATE（AccountBinding 构造期硬约束 + worker 内
  trd_env 硬编码双保险），实盘请求在本层与交易网关层双重硬阻断；
- 下单前强制重算引擎（用提交时点的快照与成本口径），不使用陈旧决策卡数字；
- 提交与回执写入审计哈希链（LIVE 模式下 run/chat/command 只读不写审计的
  例外：下单事件按设计写审计）。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from .gateway import (
    AccountBinding,
    DataEnvelope,
    EnvelopeStatus,
    GatewayError,
    GatewayErrorCode,
)
from .trade_gateway import SimulatedOrderRequest, SimulatedTradeGateway

CONFIRMATION_PHRASE = "提交模拟盘"


class SubmissionError(RuntimeError):
    """Typed submission failure；HTTP 层按 code 映射状态码。"""

    def __init__(
        self,
        code: GatewayErrorCode,
        message: str,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": bool(self.retryable),
        }


def _primary_payload_legs(
    payload: Mapping[str, Any],
    primary_expiry: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    for group in payload.get("legs", []):
        if group.get("expiry") == primary_expiry:
            call = group.get("call")
            put = group.get("put")
            if isinstance(call, Mapping) and isinstance(put, Mapping):
                return call, put
    raise SubmissionError(
        GatewayErrorCode.SCHEMA_MISMATCH,
        "主到期期权腿在快照中缺失",
        False,
    )


def build_straddle_orders(
    card: Mapping[str, Any],
    engine: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[SimulatedOrderRequest]:
    """从 READY_FOR_CONFIRMATION 决策卡构建跨式两腿模拟订单。

    价格取快照 ask（executable 成本口径的买入价），张数取引擎
    ``primary.lots``；任何前置条件不满足都抛 :class:`SubmissionError`。
    """

    action = (card.get("action_gate") or {}).get("action")
    if action != "READY_FOR_CONFIRMATION":
        raise SubmissionError(
            GatewayErrorCode.INVALID_REQUEST,
            f"决策卡 Action 门为 {action or '未知'}，未进入可确认状态",
            False,
        )
    evidence = card.get("data_evidence") or {}
    if evidence.get("mode") != "LIVE" or evidence.get("freshness") != "FRESH":
        raise SubmissionError(
            GatewayErrorCode.STALE_DATA,
            "提交仅允许 LIVE/FRESH 数据；当前决策卡数据非实时",
            False,
        )
    lots = engine.get("primary", {}).get("lots")
    if isinstance(lots, bool) or not isinstance(lots, (int, float)) or int(lots) < 1:
        raise SubmissionError(
            GatewayErrorCode.INVALID_REQUEST,
            "方案张数不足 1 张，无法提交",
            False,
        )
    primary_expiry = str(engine.get("primary", {}).get("expiry") or "")
    call, put = _primary_payload_legs(payload, primary_expiry)
    orders: list[SimulatedOrderRequest] = []
    for label, leg in (("call", call), ("put", put)):
        ask = leg.get("ask")
        if (
            isinstance(ask, bool)
            or not isinstance(ask, (int, float))
            or float(ask) <= 0
        ):
            raise SubmissionError(
                GatewayErrorCode.SCHEMA_MISMATCH,
                f"主到期 {label} 腿没有可用的 ask 买入价",
                False,
            )
        try:
            orders.append(
                SimulatedOrderRequest(
                    code=str(leg.get("code") or ""),
                    qty=int(lots),
                    price=float(ask),
                    side="BUY",
                )
            )
        except (TypeError, ValueError) as exc:
            raise SubmissionError(
                GatewayErrorCode.INVALID_REQUEST,
                f"主到期 {label} 腿订单无效：{exc}",
                False,
            ) from exc
    return orders


def submit_simulated_straddle(
    card: Mapping[str, Any],
    engine: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    gateway: SimulatedTradeGateway,
    human_confirmed: bool = False,
    confirmation_text: str = "",
    audit_enabled: bool = True,
    audit_fn: Optional[Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """执行 P0c 模拟提交闭环并返回回执 + 审计引用。

    ``audit_fn`` 缺省走决策管线的 JSONL + SHA-256 审计链；测试注入 fake sink。
    """

    if not human_confirmed or confirmation_text.strip() != CONFIRMATION_PHRASE:
        raise SubmissionError(
            GatewayErrorCode.INVALID_REQUEST,
            f"需要用户独立确认：请原文键入「{CONFIRMATION_PHRASE}」后重试",
            False,
        )
    orders = build_straddle_orders(card, engine, payload)
    envelope = gateway.place_order(orders)
    if envelope.status is EnvelopeStatus.ERROR:
        error = envelope.typed_error or GatewayError(
            GatewayErrorCode.INTERNAL_ERROR,
            "simulated order submission failed",
            False,
        )
        raise SubmissionError(error.code, error.message, error.retryable)

    receipts = envelope.data if isinstance(envelope.data, list) else []
    result: dict[str, Any] = {
        "submitted": True,
        "environment": "SIMULATE",
        "underlying": payload.get("underlying"),
        "expiry": engine.get("primary", {}).get("expiry"),
        "lots": engine.get("primary", {}).get("lots"),
        "submitted_at_utc": envelope.captured_at_utc,
        "receipts": [
            {
                "order_id": row.get("order_id"),
                "code": row.get("code"),
                "qty": row.get("qty"),
                "price": row.get("price"),
                "status": row.get("status"),
            }
            for row in receipts
        ],
        "audit_refs": [],
    }
    if audit_enabled:
        emit = audit_fn
        if emit is None:
            from .decision_pipeline import audit as default_audit

            emit = default_audit
        submitted_payload = {
            "environment": "SIMULATE",
            "underlying": result["underlying"],
            "expiry": result["expiry"],
            "lots": result["lots"],
            "orders": [
                {"code": order.code, "qty": order.qty, "price": order.price}
                for order in orders
            ],
        }
        receipt_payload = {
            "receipts": result["receipts"],
        }
        result["audit_refs"] = [
            {"event": "order_submitted", **emit("order_submitted", submitted_payload)},
            {"event": "order_receipt", **emit("order_receipt", receipt_payload)},
        ]
    return result


__all__ = [
    "CONFIRMATION_PHRASE",
    "SubmissionError",
    "build_straddle_orders",
    "submit_simulated_straddle",
]
