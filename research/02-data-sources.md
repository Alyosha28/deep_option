# 02 数据源分层与授权边界（Data Sources）

## 结论

- 比赛 Demo：富途 OpenAPI + 官方 futuapi Skill 为主力（港美股实时/推送/模拟盘），Alpaca 免费档补美股实时指示价，快照回放兜底。
- 生产：HKEX 授权实时 + OPRA/Polygon/ThetaData + 盈利日历服务。
- 免费公开 API（Yahoo）只能当兜底：2026-08-08 实测 yfinance 拉腾讯 0700.HK 期权链被限流（YFRateLimitError 429）。

## L0 快照回放（演示保命层）

- SnapshotRecorder：仅在账户与数据许可范围内把期权链快照缓存到本机（JSON/SQLite）；缓存不改变数据所有权，也不得公开再分发。
- ReplayMode：演示时在授权环境内回放本地录制数据，不依赖 OpenD/网络/限流；公开仓库不包含该数据。
- 原则：数据源可插拔；演示时“快照优先、实时加分”。

## L1 免费公开源

| 源 | 覆盖 | 限制 |
|---|---|---|
| Yahoo Finance / yfinance | 港美股期权链（0700.HK 存在）、部分 Greeks | 延迟约 15 分钟；限流严重（实测 429）；TOS 禁再分发/商用 |
| Alpaca 免费档 | 美股期权实时指示价、200 报价 WebSocket、纸面交易 | 仅美股；Basic 档历史受限 |
| IBKR 纸面账户 | 港美股期权链 + API | 默认延迟 15 分钟；30 天试用；需账户 |

## L2 富途 OpenAPI（主力）

官方入口：
- Skill Hub：https://www.futunn.com/skillhub/openapi（futuapi Skill：56 个 API、5 市场【港/美/沪深/新/日】、自然语言调用、默认模拟盘、本地 OpenD 加密网关）
- 文档：https://openapi.futunn.com/futu-api-doc/intro/ai.html

关键接口（futuapi / Futu OpenAPI）：
- get_option_expiration_date(HK.00700) —— 到期日列表
- get_option_chain(HK.00700, expiry) —— 期权链
- resolve_option_code —— 解析/构造期权代码（港股代码勿手拼）
- subscribe([代码], [QUOTE, TICKER, ORDER_BOOK]) —— 报价/逐笔/买卖盘推送
- get_market_snapshot —— 盘口快照
- 上游 SDK 具备实盘能力，但本比赛项目不注册、不支持实盘工具；仅在产品门通过后允许人工确认的模拟盘动作

权限与费用：
- 港股及期权 LV1 实时：App 端免费（2023-12 起）；OpenAPI 通道需实测确认。
- 美股期权实时：L1 ~2.99 USD/月、L2 ~3.99 USD/月（非专业用户）。
- ORDER_BOOK（深度）港股期权大概率需额外权限/订阅。

## L3 生产级数据

- HKEX 实时数据：需 vendor 牌照/终端用户协议；延迟数据最短 15 分钟且仅 L1（无深度、无经纪队列）。
- 美股期权：OPRA 实时，经 Polygon/ThetaData 等。
- 盈利日历/基本面：FMP 免费档有限，生产用付费源。
- 合约规格校验：HKEX 官网（股票期权=美式+实物交割，指数期权=欧式+现金，合约规模=一手正股）。

## 授权红线

- HKEX 数据禁止未经许可再分发；延迟数据不得含深度。
- Yahoo TOS 禁止再分发/商用。
- 富途行情绑定个人账户，禁止打包分发 → 公开仓库只放适配器代码与采集说明，不放数据。
- PPT 数据合规页：写明“演示用账户授权行情 + 快照回放；生产用持牌数据源”。
