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

## DSH 原生模式（优先）

GOAI 的编排层运行在 DeepSeek Harness（「大号金融插件」，架构见 `docs/DSH_ARCHITECTURE.md`）。
当会话中存在 `goai_state` / `goai_run` / `goai_chat` 工具时，决策链路优先走 DSH 工具：

- `goai_state`：读当前决策卡（verdict / Edge/Risk/Action 三门控 / 快照哈希与新鲜度 / LLM 徽章）；
- `goai_run`：重跑五阶段管线（默认写审计与决策卡；`noAudit=true` 只算不写）；
- `goai_chat`：自然语言 → 场景解析 → 管线 → 十角色辩论（message 为中文场景描述，
  无 DeepSeek key 自动离线回退；返回摘要含辩论共识）。

工具返回的是紧凑摘要（render 文本），需要细节数字时再读
`data/decision_card_*.json` 与 `research/audit/audit_log.jsonl`，不要在对话里复述未经
审计链确认的数字。DSH 工具不可用时回退到下方 CLI 命令（独立模式，评审机器无 DSH 也可用）。
本 skill 同时是 `goai-options` agent preset（Phase 1b）persona 的来源，铁律在两种模式下完全一致。

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

## 研究证据整理（公告/财报/新闻/研报/行业数据）

- `futu-news-search` / `futu-stock-digest` 返回后，先用 `python -m src.research_sources`
  转成 canonical 条目（API JSON 或 Markdown 均可，`--synthetic` 只用于演示数据）；
- 再用 `python -m src.research_evidence --items <canonical.json>` 生成投研证据包
  （情绪/相关性整理 + 历史财报日实际波动 vs 隐含事件波动 + IV 水位与 crush 研判）；
- `python -m src.decision_pipeline --research-items <canonical.json>` 把投研证据并入决策卡；
- 规则化情绪分类是演示级启发式；正式版本需保留原文链接并做人工/LLM 复核，且不能改数字。

## 宏观研判（情绪量化 / IV 晴雨表 / 政策事件库 / 政治经济学）

- 消息面情绪用 `quantify_sentiment`（极性 x 强度 x 时效加权 -> 情绪指数 -100..100）；
- IV 情绪晴雨表用 `assess_iv_emotion`：IV 水位、IV-HV 溢价、主到期 put-call IV 差（Skew）、
  事件临近、crush 参考；IV 只度量震幅，不度量方向；
- 政策/消息判断用 `analyze_policy_event`：事实清单 -> 矛盾清单 -> 主要矛盾 -> 博弈回合 ->
  政治经济学审视（谁获利/谁吃亏/吃亏方是否允许）-> 可落地性分级 + 证伪条件 + 监控点；
- 政策事件库：`data/policy_events/`（事件带 `status`/`updated_at`、事实来源 URL 与核验状态
  VERIFIED/PENDING/FAILED）；`python -m src.policy_library --library data/policy_events`
  查看来源健康报告；
- 运行：`python -m src.macro_assessment --snapshot ... --items ... --policy data/policy_events
  [--policy-id <id>]`；`decision_pipeline --macro-policy data/policy_events [--policy-id <id>]`
  并入决策卡（默认取 ACTIVE 中日期最新事件为主要分析对象，其余进附加事件清单）；
- 自动接入：`python -m src.macro_source_watcher --run-once` 或 `--daemon --interval-minutes 60`，
  定时抓取官方 RSS/FRED/BLS/SEC（配置在 `data/sources_config.json`），主题过滤去重后以 DRAFT
  入库；DRAFT 不参与主要矛盾分析，补全博弈分析后提升 ACTIVE；FRED/BLS 直接抓取解析的数值
  标记 VERIFIED，RSS 标题只标记 PENDING，不伪造核验状态；
- 定时运行：`scripts/install_watcher_task.ps1 -Minutes 60` 注册 Windows 计划任务
  （Interactive 登录时运行、不保存凭据），`-Remove` 移除；日志写入
  `data/logs/watch_scheduled.log`（UTF-8）；
- 中国官方数值富化：`cn_html_list` 来源开启 `enrich_numeric` 后，正文页规则提取
  CPI/PPI 同比、LPR、进出口总值、城镇调查失业率等原文数值声明（`src/cn_data_extract.py`），
  标记 VERIFIED 并保留原文片段；提取不到则保持 PENDING 标题，不补造；
- DRAFT→ACTIVE 工作流：`python -m src.policy_draft_workflow --library data/policy_events`
  列出 DRAFT 摘要与就绪度；`--promote <event_id>` 仅当事实有来源、恰好一个主要矛盾、
  verdict_reads（可落地性分级）、证伪条件与监控点齐全且无 FAILED 核验时才提升为 ACTIVE，
  否则显式返回缺失项，绝不静默通过；提升成功写 `policy_event_promoted` 审计事件
  （测试用 `--no-audit` 跳过），决策卡宏观段展示最近激活事件；
- 求是纪律：先立事实再下判断、未知显式标注、判断必须可证伪、监控矛盾转化；
  宏观研判只给定性可能性级别，不产生概率、不构成投资建议。

## 资源

- `scripts/audit_log.py`：审计留痕工具（JSONL + 哈希链）
- 前端：`python -m src.ui_server --port 8000` 打开四面板终端（/api/state 只读状态，
  POST /api/run 重跑管线；前端零计算，数字全部来自后端）；DSH 会话内优先用 goai_* 工具
  （见上文「DSH 原生模式」）
- 对话解析（离线切片）：`src/scenario_parser.py` 把自然语言转成 scenario（标的/观点/期限/
  现金/风险预算），`POST /api/chat` 直接驱动管线；解析只做文本结构化，不产数字，
  缺失字段按快照假定并显式标注；正式版本接 LLM 解析但同样禁止 LLM 估算数字
- `references/hero-tencent-straddle.md`：Hero 用例逐步数据清单
- `references/account-constraints.md`：账户与风险约束检查单
