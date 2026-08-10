"""FutuAdapter：富途 OpenAPI 只读数据适配层。

依赖：项目虚拟环境 .venv 内的 futu-api（与 OpenD 版本匹配）；
OpenD 已启动并登录（默认 127.0.0.1:11111）。

设计：每个方法返回 Snapshot（source="futuapi"、captured_at、payload），
便于快照录制与审计；futu 包采用惰性导入，未安装 SDK 时也能离线导入本模块。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .models import Snapshot

_SNAPSHOT_BATCH_SIZE = 400
_TYPE_ALIASES = {"CALL": {"CALL", "涨", "认购"}, "PUT": {"PUT", "跌", "认沽"}}
_ALLOWED_OPEND_HOSTS = {"127.0.0.1", "::1", "localhost"}


class FutuAdapter:
    """Futu OpenAPI 行情适配器（仅行情；不创建交易上下文）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        if host.lower() not in _ALLOWED_OPEND_HOSTS:
            raise ValueError("Competition build only connects to a loopback Futu OpenD host")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("OpenD port must be an integer between 1 and 65535")
        self.host = host
        self.port = port
        self._ctx = None

    # ---------- 生命周期 ----------

    def connect(self):
        if self._ctx is None:
            from futu import OpenQuoteContext

            self._ctx = OpenQuoteContext(host=self.host, port=self.port)
        return self._ctx

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None

    def __enter__(self) -> "FutuAdapter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 内部工具 ----------

    @staticmethod
    def _check(ret: str, data) -> None:
        from futu import RET_OK

        if ret != RET_OK:
            raise RuntimeError(f"Futu API error: {data}")

    @staticmethod
    def _records(data) -> List[Dict[str, Any]]:
        if data is None or len(data) == 0:
            return []
        return data.to_dict(orient="records")

    def _snapshot(self, api: str, payload: Dict[str, Any]) -> Snapshot:
        return Snapshot(source="futuapi", payload={"api": api, **payload})

    # ---------- 期权数据 ----------

    def get_expiration_dates(self, code: str) -> Snapshot:
        """获取正股期权到期日列表。"""
        ctx = self.connect()
        ret, data = ctx.get_option_expiration_date(code)
        self._check(ret, data)
        return self._snapshot("get_option_expiration_date", {"code": code, "data": self._records(data)})

    def get_option_chain(
        self,
        code: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        option_type: Optional[str] = None,
        option_cond_type: Optional[str] = None,
    ) -> Snapshot:
        """获取期权链；start~end 跨度不得超过 30 天（接口限制）。"""
        ctx = self.connect()
        kwargs: Dict[str, Any] = {}
        if start:
            kwargs["start"] = start
        if end:
            kwargs["end"] = end
        if option_type:
            kwargs["option_type"] = option_type
        if option_cond_type:
            kwargs["option_cond_type"] = option_cond_type
        ret, data = ctx.get_option_chain(code, **kwargs)
        self._check(ret, data)
        return self._snapshot("get_option_chain", {"code": code, **kwargs, "data": self._records(data)})

    def resolve_option_code(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> Snapshot:
        """在期权链中匹配标准期权代码（港股代码勿手拼）。"""
        if "." not in underlying:
            raise ValueError(f"underlying 缺少市场前缀，应为 US.JPM / HK.00700 格式: {underlying}")
        chain = self.get_option_chain(underlying, start=expiry, end=expiry)
        rows = chain.payload.get("data", [])
        aliases = _TYPE_ALIASES.get(option_type.upper(), set())
        matched = []
        for row in rows:
            row_type = str(row.get("option_type", "")).upper()
            try:
                strike_ok = abs(float(row.get("strike_price", 0)) - strike) < 0.001
            except (TypeError, ValueError):
                strike_ok = False
            if row_type in aliases and strike_ok:
                matched.append(
                    {k: row.get(k) for k in ("code", "name", "strike_price", "strike_time", "option_type", "last_price")}
                )
        return Snapshot(
            source="futuapi",
            payload={
                "api": "resolve_option_code",
                "underlying": underlying,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
                "matched": matched,
            },
        )

    # ---------- 行情快照 ----------

    def get_market_snapshot(self, codes: Iterable[str]) -> Snapshot:
        """获取市场快照；自动按 400 个/批拆分（港股 BMP 权限单批 20 个）。"""
        code_list = list(codes)
        ctx = self.connect()
        records: List[Dict[str, Any]] = []
        for i in range(0, len(code_list), _SNAPSHOT_BATCH_SIZE):
            batch = code_list[i : i + _SNAPSHOT_BATCH_SIZE]
            ret, data = ctx.get_market_snapshot(batch)
            self._check(ret, data)
            records.extend(self._records(data))
        return self._snapshot("get_market_snapshot", {"codes": code_list, "data": records})

    def get_market_state(self) -> Snapshot:
        """查询各市场开闭市状态。"""
        ctx = self.connect()
        ret, data = ctx.get_global_state()
        self._check(ret, data)
        return self._snapshot("get_global_state", {"data": data.to_dict() if hasattr(data, "to_dict") else data})

    # ---------- 实时订阅 ----------

    def subscribe(
        self,
        codes: List[str],
        subtype_names: List[str],
        is_first_push: bool = True,
        subscribe_push: bool = True,
        extended_time: bool = False,
    ) -> Snapshot:
        """订阅 QUOTE / ORDER_BOOK / TICKER 等实时推送。"""
        from futu import Session

        ctx = self.connect()
        subtypes = self._parse_subtypes(subtype_names)
        ret, msg = ctx.subscribe(
            codes,
            subtypes,
            is_first_push=is_first_push,
            subscribe_push=subscribe_push,
            extended_time=extended_time,
            session=Session.NONE,
        )
        self._check(ret, msg)
        return self._snapshot(
            "subscribe",
            {"codes": codes, "subtypes": subtype_names, "status": "subscribed"},
        )

    def unsubscribe(self, codes: List[str], subtype_names: List[str]) -> Snapshot:
        ctx = self.connect()
        subtypes = self._parse_subtypes(subtype_names)
        ret, msg = ctx.unsubscribe(codes, subtypes)
        self._check(ret, msg)
        return self._snapshot("unsubscribe", {"codes": codes, "subtypes": subtype_names, "status": "unsubscribed"})

    @staticmethod
    def _parse_subtypes(names: List[str]):
        from futu import SubType

        mapping = {name: getattr(SubType, name) for name in names if hasattr(SubType, name)}
        unknown = [n for n in names if n not in mapping]
        if unknown:
            raise ValueError(f"不支持的订阅类型: {unknown}")
        return list(mapping.values())
