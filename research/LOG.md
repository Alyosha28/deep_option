# 研究日志（Research Log / Agent Card）

- 日期：2026-08-08
- 目标：为 GOAI Boundless 期权 Agent 构建可检索的调研知识库（竞品、专家方法、边界、优化点）
- 方法：多轮网络检索（search）+ 页面核对（open_page）+ 已有会话结论沉淀；来源逐条记录到 sources.json
- 工具：search / open_page / PowerShell（沙箱外写文件）；模型与参数以 Codex 会话默认配置为准
- 已完成：竞品调研（富途/老虎/IBKR/Bloomberg/OptionStrat/moontower.ai）、数据源实测（yfinance 限流）、专家方法（tastytrade、Sinclair、Karsan、Abdelmessih、TradingAgents、SpotGamma、ORATS/Option Alpha）、边界（SFC、HKEX 数据牌照、行权/钉子风险、LLM 幻觉）、社区（r/options、EliteTrader、QuantSE、OIC、X 账号）
- 待办：B 验证富途 OpenAPI 权限；C 起草合规与数据授权页；A 搭建数据层骨架（FutuAdapter + 快照 + 回放）
- 诚实声明：本库中所有结论均为公开资料整理，不构成投资建议；来源真实性与失效时间需在引用前复核

- 2026-08-08 补充：富途官方 Skill Hub（futuapi）纳入数据层；sources.json 共 46 条来源；kb_search.py 支持中英别名检索；JSON 与脚本已验证。
