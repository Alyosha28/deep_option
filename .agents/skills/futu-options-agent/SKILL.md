---
name: futu-options-agent
description: >-
  Project-specific options-agent workflow for the GOAI 港美股期权智能终端 (decision support
  only; simulation-only in the competition build; no real execution). Use for any options-agent task in this repo:
  option chain / Greeks / IV analysis, straddle or multi-leg strategy construction, earnings-event
  volatility plays, account-constraint checks, position risk and audit, liquidity screening, or
  anomaly/alert generation for HK or US underlyings (e.g., HK.00700, US.NVDA, US.AMZN). Triggers
  include 期权、跨式、straddle、Greeks、IV、隐含波动率、期权链、权利金、put/call、业绩跨式、
  仓位风险、账户约束、期权异动. Orchestrates the installed Futu skills (futuapi,
  futu-derivatives-anomaly, futu-stock-digest, futu-news-search, futu-comment-sentiment,
  futu-capital-anomaly, futu-technical-anomaly) under project rules: LLM parses scenarios and
  orchestrates only; every number comes from the self-built engine or the Futu API; human
  confirmation before any simulated order; real trade tools are denied; audit trail on every output.
---

# 期权智能终端 Agent（GOAI 项目特化层）

本 skill 把项目级已安装的 Futu skills（futuapi、install-futu-opend、futu-derivatives-anomaly、
futu-stock-digest、futu-news-search、futu-comment-sentiment、futu-capital-anomaly、
futu-technical-anomaly）编排为「查询 → 分析 → 策略 → 风控 → 审计」的期权决策链路。
基础 skills 保持原样（可升级覆盖），本项目规则只在这一层表达。

## 定位与边界（不可违反）

- 决策支持 / 研究 / 教育工具；不是投资建议，不自动交易。
- 比赛版本只允许 Futu 模拟盘（SIMULATE）；任何模拟订单必须人机确认，实盘工具不注册并硬阻断。
- 所有数值由自研引擎或 Futu API 产出；LLM 只做场景解析、文本与编排，禁止 LLM 估算数字。
- 每个输出带数据快照时间与来源（futuapi / 知识库 / 快照回放），并写入审计日志。

## 工作流（5 阶段）

### 1. 场景解析（LLM 结构化解析）
输出 JSON：`underlying`（标准代码如 HK.00700）、`view`（看多/看空/方向不确定）、
`horizon`（事件日/持有天数）、`account`（资金/币种）、`risk_budget_pct`、`constraints`。

### 2. 数据获取（futuapi + 场景 skills）
- 期权基础：`resolve_option_code`（港股代码勿手拼）、`get_option_expiration_date`、
  `get_option_chain`、`get_option_quote`、`get_option_volatility`、`get_option_exercise_probability`
- 行情盘口：`get_snapshot`、`get_orderbook`；订阅 `subscribe` + `push_quote`/`push_orderbook`
  用于流动性监测与预警
- 事件上下文：`get_earnings_calendar`、`get_option_event`、`get_option_event_alert`；
  `futu-news-search`、`futu-stock-digest`
- 信号辅助：`futu-derivatives-anomaly`（期权异动/IV/牛熊证）；按需叠加
  `futu-comment-sentiment`、`futu-capital-anomaly`、`futu-technical-anomaly`
- 每个数据调用前先落快照（时间戳 + 原始 JSON），演示时快照优先、实时加分

### 3. 数值计算（自研引擎，禁止 LLM 估算）
- Greeks 用 bump-and-reprice；欧式 BS / 美式二叉树；IV 用迭代求解
- 情景损益：业绩日标的价格变动 × IV crush（20–50% 参考区间）
- 流动性：PBA、买卖价差、OI 趋势、报价新鲜度

### 4. 策略匹配 + 账户约束 + 风险审计（一票否决）
- 按 `references/account-constraints.md` 逐项检查：合约乘数、保证金/现金占用、
  5% 风险规则、持仓限额、港美股交割差异
- 风险审计输出 PASS / BLOCK + 违规项 + 建议调整；BLOCK 时禁止进入下单步骤

### 5. 输出 + 审计留痕
- 输出结构：方案摘要 / 数值表 / 风险与边界 / 来源链接 / 免责声明
- 调用 `scripts/audit_log.py` 追加审计记录（JSONL + SHA-256 哈希链）
- 人机确认和提交前复核都通过后才能进入模拟下单；任何真实交易请求均硬阻断

## Hero 用例

腾讯 0700.HK 业绩前构建跨式（10 万港币账户）：见 `references/hero-tencent-straddle.md`，
包含数据清单、流程与输出模板。

## 项目知识库联动

- `research/04-expert-methods.md`：tastytrade / Sinclair / Karsan / SpotGamma 方法
- `research/03-boundaries-risks.md`：SFC / HKEX / 市场风险边界
- `research/02-data-sources.md`：数据分层与授权红线（富途数据禁止打包再分发）

## 资源

- `scripts/audit_log.py`：审计留痕工具（JSONL + 哈希链）
- `references/hero-tencent-straddle.md`：Hero 用例逐步数据清单
- `references/account-constraints.md`：账户与风险约束检查单
