"""主题分类：宏观来源条目（RSS/HTML/CSV）按主题关键词归类。

从 `macro_source_watcher` 抽出，供 watcher 与中国官方 HTML 解析共用；
关键词同时覆盖英文与中文，分类只用于「是否值得入库」的粗筛，
不替代对事件重要性的博弈分析。
"""

from __future__ import annotations

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "inflation": (
        "cpi",
        "pce",
        "inflation",
        "price index",
        "通胀",
        "物价",
        "居民消费价格",
        "工业生产者出厂价格",
        "出厂价格",
        "生产资料市场价格",
        "居民消费价格指数",
    ),
    "rates": (
        "fomc",
        "interest rate",
        "federal funds",
        "policy rate",
        "利率",
        "降息",
        "加息",
        "基准利率",
        "lpr",
        "贷款市场报价利率",
        "存款准备金",
        "降准",
        "公开市场操作",
        "货币政策",
    ),
    "trade": (
        "tariff",
        "trade",
        "export",
        "import",
        "关税",
        "贸易",
        "出口",
        "进口",
        "反倾销",
        "进出口总值",
        "进出口",
        "贸易顺差",
        "贸易逆差",
    ),
    "jobs": (
        "nonfarm",
        "payroll",
        "unemployment",
        "就业",
        "非农",
        "失业率",
        "采购经理指数",
        "采购经理",
        "就业人员",
    ),
    "policy": (
        "policy",
        "regulation",
        "rule",
        "法案",
        "政策",
        "管制",
        "监管",
        "出口管制",
        "公告",
        "办法",
        "规划",
        "指导意见",
        "实施方案",
        "通知",
        "意见",
    ),
    "corporate": ("earnings", "8-k", "财报", "业绩", "公告"),
}

TOPIC_EVENT_TYPES = {
    "inflation": "macro-data",
    "rates": "monetary-policy",
    "trade": "trade-data",
    "jobs": "macro-data",
    "policy": "policy",
    "corporate": "corporate-events",
}


def classify_topic(text: str, allowed_topics: list[str]) -> str | None:
    """按允许的主题集合返回命中的第一个主题；没有命中返回 None。"""

    lowered = text.lower()
    for topic in allowed_topics:
        if any(keyword in lowered for keyword in TOPIC_KEYWORDS.get(topic, ())):
            return topic
    return None
