"""FallbackAdapter：兜底数据源占位（Alpaca 美股实时 / Yahoo 历史）。

当前未实现：接口形状后续与 FutuAdapter/ReplayAdapter 对齐后接入。
"""

from __future__ import annotations


class FallbackAdapter:
    def __init__(self):
        self.available = False

    def get_quote(self, code: str):
        raise NotImplementedError(
            "FallbackAdapter 未实现：美股实时指示价接 Alpaca 免费档，港美股历史兜底接 Yahoo。"
        )
