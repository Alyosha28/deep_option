# GOAI · DeepSeek Harness 金融期权终端

GOAI 世界人工智能开源大赛 · Boundless Agents 金融服务方向项目。

GOAI 面向有基础期权认知的用户，把自然语言观点转成带数据来源、港股产品规则、确定性定价、账户风控和审计记录的决策卡。产品定位是研究与决策支持，不是投资建议，不提供实盘自动交易。

**项目形态：一个运行在 DeepSeek Harness（DSH）上的「大号金融插件」。** Python 确定性引擎守护全部金融数字与审计链，DSH 负责 Agent 编排、工具注册、人机审批与对话体验。评审也可以完全不装 DSH，用 `python -m src.ui_server` 独立运行——两条入口共享同一引擎契约。架构权威文档见 [docs/DSH_ARCHITECTURE.md](docs/DSH_ARCHITECTURE.md)。

## 架构（一句话）

```text
DSH 编排层（host 插件 + model tools + skills + preset）
        → 引擎契约（ui_server JSON API，127.0.0.1:8000）
        → Python 引擎（数值铁律：gateway / pricing / pipeline / debate / audit）
```

## MVP

P0 聚焦一个可验证场景：

> 腾讯 `HK.00700` 业绩前方向不确定，账户 10 万港币，评估长跨式是否值得交易。

系统目标输出四种产品结果：

- `NO_TRADE`：当前成本和证据不支持交易；
- `BLOCK`：数据、规格、账户或风险硬门未通过；
- `DRAFT_ONLY`：可研究和生成草稿，但不能提交；
- `READY_FOR_CONFIRMATION`：客观门通过，可由用户确认 Futu 模拟方案。

`NO_TRADE` 是成功结果。不会为了展示下单而调低门槛。

## 当前实现状态

已实现并验证的资产：

- DSH 编排层（Phase 3 插件族）：`harness/plugins/goai-*.host.js`（Base Mode：goai-core / goai-run / goai-chat，可选 macro / research / backtest）注册 `goai_state` /
  `goai_run` / `goai_chat` 等 model tools，真机跑通「对话 → 五阶段管线 → 十角色辩论 →
  审计链」闭环；插件生命周期托管引擎进程（可逆效应）；用户经 `harness/config/goai.plugins.json` 自选加载；
- SDK 无关的 typed Gateway 合同、稳定快照哈希和 typed error；
- Futu Live 只读行情/账户 Gateway，以及同合同的确定性 Replay Gateway；
- 线程安全 Snapshot Recorder、严格 JSONL 校验和 legacy 快照迁移读取；
- Agent 侧唯一粗粒度只读入口 `refresh_decision_inputs`；
- 端到端决策管线 `src/decision_pipeline.py`：场景解析 → 冻结快照 → 自研引擎 →
  Edge/Risk/Action 门控 → 可溯源决策卡 + 审计留痕；
- Black–Scholes、通用美式二叉树、IV 求解和 bump-and-reprice Greeks；
- 腾讯跨式分析与历史回测原型；
- JSONL + SHA-256 审计工具；
- 政策事件库（带来源链接与核验状态）与宏观来源自动监控程序；
- 桌面研究终端前端（`ui/` + `src/ui_server.py` 本地只读服务，支持真实重跑管线与多项目工作区）；
- 项目注册表（`data/workspaces.json`）：可为不同公司分别绑定期权快照、历史行情和投研资料，切换后整条研究管线跟随当前项目；
- 十角色多 Agent 辩论运行时（LLM 只出文字，数字/verdict 仍由引擎产出，离线回退）；
- 项目级 `futu-options-agent` 工作流（`goai_*` tools 可用时优先走 DSH 工具）。

实时行情第一阶段已接入：设置 `GOAI_DATA_MODE=live` 后 UI 走只读 live 链路
（`GET /api/live-quote` 轻量报价、`GET /api/state` 显示 LIVE/FRESH、
`POST /api/run|/api/chat|/api/command` 用实时快照重算且只读不写审计）；
OpenD 不可用时显式返回 `OPEND_UNAVAILABLE`，绝不静默回退 Replay。
实时行情第二阶段已接入：`GET /api/stream` SSE 服务端推送（`quote`/`error`/
`refresh` 事件 + 15s 心跳；LIVE 模式专属，订阅上限与断连清理），LIVE 前端
自动连接并在报价变化时就地更新报价条、防抖重拉完整状态（顶栏「实时推送」
状态徽章）；上游默认 diff 轮询（2s，变化才推送），设 `GOAI_LIVE_FEED=push`
启用真实 OpenD 订阅推送（SDK subscribe + QuoteHandler，失败/静默自动回退轮询
并推 `warning` 事件）。可选环境变量：`GOAI_LIVE_STREAM_POLL_SECONDS`、
`GOAI_LIVE_STREAM_PUSH_SILENCE_SECONDS`、`GOAI_LIVE_STREAM_MAX_SUBSCRIBERS`。
仍在建设：DSH 客户端决策卡面板与审批闭环（Phase 1）、港股离散股息的
实时富化（引擎已支持快照声明的离散股息，escrowed-spot 口径）、
executable-cost 完整实现（费用/滑点/保证金/持仓限额链）、独立
Edge/Risk/Action gates，以及当前版本的模拟提交安全闭环（P0c）。

中国官方来源 HTML 解析已接入（央行/统计局实测可达并默认启用；海关总署本机 TLS 证书校验失败，默认停用待复核），Windows 计划任务已注册。

完整产品边界和验收标准见 [精简版 PRD](docs/PRD.md)。

## 核心原则

1. LLM 只做工具编排和解释，不生成金融数字；场景解析由确定性引擎完成。
2. 行情和账户事实来自 Futu 或明确标记的 Replay；计算来自确定性引擎。
3. 理论价值与 bid/ask、费用、滑点后的可成交口径分开。
4. 风险硬门一票否决；`PASS` 不代表盈利、成交或投资建议。
5. 比赛版本无实盘入口；模拟动作也必须由用户独立确认。
6. DSH 编排层不重算任何数字，只透传引擎结果；JS 与 LLM 同等受此约束。

## 项目结构

```text
src/                                数据适配、回放、定价与 Hero 原型
src/agents/                         十角色辩论运行时（llm_client/tools/runtime）
src/ui_server.py                    引擎契约：7 视图研究终端 + JSON API（127.0.0.1:8000）
ui/                                 7 视图终端前端（独立模式/静态回退）
harness/plugins/goai-*.host.js   DSH 编排层插件族（Base：core/run/chat + 可选：macro/research/backtest）
tests/                              Gateway 合同、安全边界与离线集成测试（367 passed + 157 subtests）
research/                           市场、数据、边界和专家方法研究
docs/PRD.md                         产品需求与比赛验收
docs/DSH_ARCHITECTURE.md            DSH 编排层权威架构文档
.agents/skills/futu-options-agent/  项目特化期权 Agent 工作流
```

授权行情、账户数据、订单回执和本地审计日志不进入公开仓库。

## 快速开始

环境：Windows、Python 3.13；Live 数据能力另需本地 Futu OpenD。

### 独立模式（评审可用，不依赖 DSH）

端到端决策管线（无需 OpenD，使用冻结快照）：

```powershell
python -m src.decision_pipeline
```

输出 `data/decision_card_*.json` 与 `research/audit/audit_log.jsonl` 哈希链记录。

7 视图研究终端（总览 / 决策卡 / 期权链 / 宏观 / 投研 / 政策库 / 分歧 / 审计，含十角色辩论 dock）：

```powershell
python -m src.ui_server --port 8000
```

访问 `http://127.0.0.1:8000/`。对话面板输入自然语言（`POST /api/chat`）直接驱动
「场景解析 → 五阶段管线 → 十角色辩论」；无 DeepSeek key 自动离线回退，Demo 不崩。
只读 API 另有 `GET /api/decision-card`（导出决策卡 + SHA-256）、`GET /api/audit`
（审计链校验视图）、`GET /api/metrics`（会话度量）、`GET /api/projects`（项目工作区）。

左侧工作区的“添加”可导入其他公司的研究快照。新快照放入 `data/projects/`，在项目抽屉填写
`SSE.600519`、`NASDAQ.AAPL`、`HK.09988` 等通用市场前缀代码即可；Agent 会在受控 `data/` 范围内自动找快照和投研资料，
校验 `underlying` 与代码一致后注册，并隔离该项目的期权链、历史价格/实现波动率、投研资料和 Agent
上下文。也可以直接对研究助理说“研究 600519 的期权”或直接说公司名称，Agent 会先自动发现该标的文件。具体规则与 API 示例见
[ui/README.md](ui/README.md) 的“添加其他公司的期权研究项目”；冻结快照格式与录制方法见
[docs/SNAPSHOT_RECORDING.md](docs/SNAPSHOT_RECORDING.md)。

知识库检索：

```powershell
python research\kb_search.py 流动性
python research\kb_search.py "IV crush" --tag earnings
```

### DSH 模式（对话 Agent 编排，本机）

编排层是 **Cordis 插件族**（DSH 底层即 Cordis 内核）：Base Mode（`goai-core` +
`goai-run` + `goai-chat`）保证基本使用，宏观/投研/回测为可选插件，用户在
`harness/config/goai.plugins.json` 勾选加载哪些（详见 [docs/PLUGIN_ARCHITECTURE.md](docs/PLUGIN_ARCHITECTURE.md)）。
在 DSH 会话中直接说「用 goai_state 看当前决策卡」或「goai_chat：腾讯业绩前方向不确定，
账户10万港币，评估跨式」——插件自动拉起引擎并返回带快照哈希与门控的决策卡摘要。
DSH 重启后插件需要重新注册：运行 `harness\bootstrap.ps1` 获取注册指引，把
`harness\plugins\goai-*.host.js`（启用者）内容交给会话助手执行 `cordis_define` +
`cordis_run` 即可（旧单体 `goai-bridge.host.js` 为 LEGACY 回退，与插件族二选一）。

**agent preset 入口（推荐）**：新建 DSH 会话选 **GOAI Options Terminal**，同步/校验
preset 用 `harness\verify_preset.ps1`（`-Sync` 一键同步到 `~\.dsh\.agent-presets`），
真实挂载冒烟用 `node harness\smoke_preset.mjs`。注意：若 DSH web profile 装了
实验性 `@deepseek-ai/dsh-tool-search`，preset 层工具会对模型不可见，需先跑
`harness\fix_dsh_tool_visibility.ps1`（新建会话即生效，无需重启；已运行会话
保持旧限制，详见 harness/README.md 的“tool-search 与 agent preset 分层不兼容”）。

投研证据整理与影响研判（公告/财报/新闻/研报/行业数据 -> 股价与期权影响）：

```powershell
python -m src.research_evidence
python -m src.decision_pipeline --research-items data/research_items_hero.json
```

`data/research_items_hero.json` 是 `synthetic=True` 的示例数据，只用于演示证据链路，不冒充真实市场证据；正式运行应替换为 `futu-news-search` / `futu-stock-digest` / 公告接口输出的带来源与抓取时间的条目。

Futu 新闻/公告/研报适配（把 `futu-news-search` / `futu-stock-digest` 的真实输出转成 canonical 条目）：

```powershell
python -m src.research_sources --keyword Tencent --api-json <news_search_响应.json> --out data/research_items_futu.json
python -m src.research_sources --keyword Tencent --markdown <skill输出.txt> --out data/research_items_futu.json
python -m src.decision_pipeline --research-items data/research_items_futu.json
```

适配器只做格式转换和来源留痕，不改写标题、时间或链接；缺少发布时间但有原文 URL 的条目会标记 `publish_time_unknown=True`，不会虚构时间。

宏观研判（情绪量化 + IV 情绪晴雨表 + 政策事件库 + 政治经济学 + 求是检验）：

```powershell
python -m src.macro_assessment --snapshot data/hero_inputs.json --items data/research_items_hero.json --policy data/policy_events
python -m src.decision_pipeline --research-items data/research_items_hero.json --macro-policy data/policy_events --policy-id fed-fomc-2025-05
```

`data/policy_events/` 是政策事件库：每个事件带 `status` / `updated_at`、事实来源 URL 与核验状态
（VERIFIED / PENDING / FAILED）。`--policy` 传目录时加载全部事件，默认取 ACTIVE 中日期最新者作为
主要分析对象，其余事件与来源健康报告进入决策卡的 `policy_analysis.library`；也可以用
`--policy-id` 指定主事件。宏观研判只输出定性可能性级别（HIGH/MEDIUM/LOW）与分析依据，
不产生概率、不构成投资建议。

政策事件库健康检查（核验状态计数、无来源/无 URL/过期标记）：

```powershell
python -m src.policy_library --library data/policy_events
```

自动接入重大金融事件 / 重大金融政策 / 宏观数据（通胀、利率、贸易、就业）：

```powershell
python -m src.macro_source_watcher --dry-run                          # 试跑一轮，不写库
python -m src.macro_source_watcher --run-once                         # 跑一轮并 DRAFT 入库
python -m src.macro_source_watcher --daemon --interval-minutes 60     # 后台定时监控
python -m src.policy_draft_workflow --library data/policy_events      # 复核 DRAFT 摘要与提升就绪度
python -m src.policy_draft_workflow --library data/policy_events --promote <event_id>  # 条件满足才提升 ACTIVE
```

来源在 `data/sources_config.json` 配置：美联储 / ECB 官方 RSS、FRED 宏观序列（CPI、PCE、利率、
贸易余额、就业）、BLS、SEC EDGAR（上市公司 8-K），以及中国官方 HTML 列表页
`cn_html_list`（中国人民银行新闻发布、国家统计局数据发布、海关总署新闻发布；海关因本机
TLS 证书校验失败默认停用）。自动抓取的新事件一律以 `DRAFT` 状态入库、核验状态为 `PENDING`
（FRED/BLS 直接抓取并解析的数值标记 `VERIFIED`），不冒充已验证；
补全博弈分析后再提升为 `ACTIVE`。单个来源失败只记录 `FAIL`，不伪造数据、不中断整轮。
2026-08-13 实测：美联储与 ECB RSS 可达；FRED 在本机网络超时、BLS/SEC 返回 403，
可在配置中将对应来源 `"enabled": false` 停用。Windows 上可用 `--daemon` 常驻，
或用已提供的计划任务脚本定时执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Minutes 60  # 注册
powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Remove      # 移除
```

脚本自动定位本仓库的 `.venv`，每轮输出 UTF-8 日志到 `data\logs\watch_scheduled.log`。
任务以当前用户的 Interactive 身份运行（登录时才触发），不保存任何凭据；
开机无人值守运行需要自行配置凭据。

中国官方数值页支持数值富化（`enrich_numeric: true`）：列表页标题保持 `PENDING`，
正文页用规则提取的「同比上涨/下降 X%」「LPR 为 X%」「进出口总值 X 亿元」等原文数值
声明标记 `VERIFIED` 并保留原文片段；2026-08-13 实测从统计局正文页提取到 CPI/PPI 同比、
从央行正文页提取到 LPR。提升 `ACTIVE` 成功时写入 `policy_event_promoted` 审计事件，
决策卡宏观研判段会展示最近激活事件。

## 十角色辩论运行时（LLM 只出文字，数字仍来自自研引擎）

`POST /api/chat`（或 DSH 的 `goai_chat` 工具）在五阶段确定性管线之上附加一层「十角色多
Agent 辩论」（`src/agents/`，参考 TradingAgents 的多角色质证思路）：

1. 首轮：九个分析角色（数据官 / 新闻公告 / 研报对比 / 宏观政策 / 情绪舆情 / 技术资金流 /
   期权策略 / 风险管理 / 审计官）并行给出文字结论与证据引用，主席（orchestrator）选出
   最多 3 个真正影响决策的分歧点；
2. 次轮：只调用分歧相关的角色回辩，主席汇总 `research_consensus`（summary / stance /
   confidence / evidence_refs / open_questions）。

铁律（运行时强制）：LLM 输出只解析为文本 + 枚举（stance/confidence）+ 证据引用，引用按
白名单过滤（编造的 evidence id 记入 `dropped_refs`）；所有数字、verdict 与门控仍由冻结
快照与自研引擎产出，辩论不改变确定性结论。单角色失败绝不拖垮整场；无 key / 网络失败 /
超时自动回退确定性管线，Demo 不崩。每个角色的结论与最终共识经 SHA-256 哈希链写入
`research/audit/audit_log.jsonl`（`agent_output:<role>` / `debate_consensus` 事件，密钥片段脱敏）。

默认使用 DeepSeek 的 OpenAI 兼容接口（`https://api.deepseek.com/v1`），零新增依赖
（stdlib urllib）。配置方式：复制 `.env.example` 为 `.env` 并填写密钥（`.env` 已
gitignore，密钥不入库；真实环境变量优先于 `.env`）：

```powershell
Copy-Item .env.example .env   # 然后编辑填入 DEEPSEEK_API_KEY
python -m src.ui_server --port 8000
```

可调参数：`GOAI_CHAT_MODEL` / `GOAI_REASONER_MODEL`（默认 deepseek-chat / deepseek-reasoner，
主席与宏观/风险/审计用 reasoner）、`GOAI_CHAT_TIMEOUT_S`（默认 30）、`GOAI_REASONER_TIMEOUT_S`
（默认 90）、`GOAI_LLM_RETRIES`（默认 2，5xx/429/瞬时网络错误重试，401/403 不重试）。
未配置密钥时终端顶栏显示「LLM · offline 确定性回退」，页面底部第五面板（十角色辩论 dock）
展示每轮每个角色的结论、回辩、证据引用、分歧点与研究共识，全部动态文本用 `textContent`
渲染防注入。真实调用前请先在 DeepSeek 控制台充值/确认额度，端点缺 key 返回 401 属正常。

## Futu Gateway 边界

产品运行时不直接执行 `.agents/skills/futuapi` 下的 CLI，也不把 SDK Context、Bash 或任意方法调用权交给 Agent。现有 skill 保留为开发诊断和 API 规则参考；正式数据流为：

```text
Decision Agent / UI
        → DecisionInputService
        → MarketDataGateway / AccountReadGateway
        → ReplayGateway 或 FutuLiveGateway
        → 本地 fixture 或 127.0.0.1:11111 OpenD
```

- P0a 只装配 `ReplayGateway`，不会导入 Futu SDK 或连接 OpenD；
- P0b 才装配 `FutuLiveGateway`，仅注册读取能力并使用服务端账户别名；
- P0c 模拟执行是独立边界，当前 Gateway 不包含下单、改单、撤单或解锁方法；
- 当前没有 Futu MCP。单一 Python 宿主直接扩展 typed Boundary 已覆盖当前需求，也避免再增加一层协议和工具发现攻击面；只有出现多宿主或跨语言复用需求后，才在同一 Boundary 外增加本地只读 MCP facade。
- DSH 编排层（`goai-*` tools）只通过 `ui_server` 的 JSON API 触达引擎，不越过
  `DecisionInputService` 边界直接调用 SDK。

Replay 默认只读取带完整 schema、内容哈希和业务语义校验的 canonical JSONL。`as_of_utc` 在 Gateway 构造时固定；每次查询只选择该时点之前、默认 60 秒一致性窗口内的同请求快照，不会按调用顺序拼接不同批次。旧 OpenD 日志包裹的 `.json` 只用于显式迁移：必须设置 `allow_legacy=True`，结果标记为 `PARTIAL/unverified`，不能作为正式发布证据。

快照内的 SHA-256 用于发现误改和损坏，不是对恶意本地写入者的身份认证。当前部署假设 fixture 目录由单一受信任用户控制；多用户或不可信目录上线前必须增加 owner-only ACL、签名/HMAC manifest、bundle sequence 和回滚保护。

<!-- AUTO-GENERATED: gateway-validation:start -->
以下命令直接来自当前 `tests/` 测试入口：

```powershell
python -m unittest discover -s tests -v
ruff check src/gateway.py src/payload_validation.py src/futu_adapter.py src/futu_account_worker.py src/replay_adapter.py src/snapshot_recorder.py src/decision_inputs.py tests
mypy --ignore-missing-imports src/gateway.py src/payload_validation.py src/futu_adapter.py src/futu_account_worker.py src/replay_adapter.py src/snapshot_recorder.py src/decision_inputs.py
```
<!-- AUTO-GENERATED: gateway-validation:end -->

Live 验证前需由用户手动启动并登录 OpenD；Gateway 不会自动启动、登录或解锁。健康检查未通过时只返回 typed error，不会静默切换为 Replay。2026-08-12 已用正式 `FutuLiveGateway.health()` 验证本机 OpenD `ready=true`、行情与交易会话均已登录、server `1009`；该验证没有查询账户、订阅或交易。行情 Context 使用 SDK 官方异步初始化与连接等待上限；真实账户读取放在固定 schema、20 秒硬截止且可终止的本地 worker 中，避免 SDK 同步构造无限重试阻塞 Agent 进程。该 worker 是内部安全边界，不是 MCP，也不暴露任意调用。`DecisionInputService.max_refresh_seconds` 是在组件调用之间和返回后检查的协作式预算，不是整个刷新任务的强制 wall-clock 截止；若未来需要严格的全链路 SLA，应把完整刷新也放入可终止的监督进程，并把剩余预算传递给各数据调用。

## 自研引擎边界

项目不声称发明 Black–Scholes 或二叉树。自研工作集中在：

- 港股产品规格解析与模型路由；
- IV、Greeks 和情景损益的可复算实现；
- bid/ask、费用、滑点和 tick 的 executable-cost 口径；
- Edge、账户风险和动作门控；
- 数据、计算与决策审计。

当前引擎仍是原型，不能用于真实资金决策。

## 数据与安全

- 不提交 `.env`、密钥、交易密码、账户号或订单号。
- 不公开分发账户授权行情和原始 Futu 快照。
- 外部文本按不可信数据处理，不能修改数值、风控或权限。
- 任何模拟提交功能都必须在独立确认和提交前复核之后启用。
- 本项目仅用于研究、教育和比赛演示，不构成投资建议。

## License status

仓库目前公开用于团队协作和比赛审阅，尚未选定开源许可证；公开不等于授予复制、修改或再分发许可。详见 [NOTICE.md](NOTICE.md)。
