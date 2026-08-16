# GOAI 项目历史归档（HISTORY）

> 本文件是 `PROJECT_STATE.md` 的历史细节归档：新会话**不需要**读本文件；
> 查找实现细节、验证记录或历史数值时按节号回查。当前状态以 `PROJECT_STATE.md` 为唯一权威。
> 归档时间：2026-08-13（文档拆分）。

| 原节号 | 内容 |
|---|---|
| §4 | 知识库关键知识摘要（8.8 版细节） |
| §4.6 | 宏观来源自动接入与政策事件库（2026-08-13 实现细节） |
| §5.5 | 环境与数据层进度（含 Hero 数值、legacy 模拟单、回测结论） |
| §11 | 十角色辩论运行时（实现清单、验证检查点、真机冒烟记录） |
| §12 | DSH 编排层 Phase 0（验证与踩坑细节） |

---

## 4. 知识库关键知识摘要（8.8 版细节，速览见 research/INDEX.md）

- 竞品：富途（港股期权 LV1 实时免费，美股期权实时 L1 2.99/L2 3.99 USD/月，Moomoo Engine 进行中）；老虎（TigerOpen API）；IBKR（策略创建器/Probability Lab，延迟数据）；Bloomberg（OMON/OVME/OSA，机构级）
- 边界：HKEX 延迟数据最短 15 分钟且仅 L1；港股个股期权=美式+实物交割，指数期权=欧式+现金；持仓限额（单类单方向 5 万张、LOP 申报 1 千张）；pin risk/assignment；IV crush 20-50%；LLM 幻觉（数值引擎+审计消除）
- 方法：tastytrade 16Δ/45DTE/50% 或 21DTE 退出；Sinclair 一致性流程；Karsan 波动率供给/尾部建模；Moontower 波动率镜头；SpotGamma GEX=Γ×OI×S²×100；TradingAgents 七角色辩论；ORATS earnEffect/RV Ratio/IV Rank；Option Alpha ITM 概率窗口 23-35%；学术 PBA 流动性度量
- 社区：r/options、r/VegaGang、EliteTrader、Quant StackExchange、OIC/Cboe、HKEX Option ABC；X 账号：@spotgamma、@darjohn25、Cem Karsan、Kris Abdelmessih、tastylive（引用前核验 handle）

---

## 4.6 宏观来源自动接入与政策事件库（2026-08-13）

实现「不用等用户提醒」的自动接入链路：定时抓取官方来源 → 主题过滤（通胀/利率/贸易/就业/政策/公司事件）→ URL 去重 → `DRAFT` 入库 → 自动摘要 → 补全博弈分析后提升 `ACTIVE`。

- 来源配置：`data/sources_config.json`。实测可达并启用：美联储/ECB RSS、FRED 超时/BLS/SEC 403 默认停用、中国人民银行新闻发布与统计局数据发布可达并启用、海关总署本机 TLS 证书校验失败默认停用待复核（诚实标注，不伪装可用）
- 中国官方解析：`src/cn_source_parsers.py` + `src/topic_classifier.py`（kind `cn_html_list`，HTML 列表页解析，日期优先从链接提取，提取不到留空不虚构；标题类条目一律 `PENDING`）
- 自动入库：`src/macro_source_watcher.py --run-once / --daemon`；FRED/BLS 直接解析数值标 `VERIFIED`，RSS/HTML 标 `PENDING`；单来源失败只记 `FAIL` 不中断
- 中国官方数值富化（2026-08-13）：`src/cn_data_extract.py` 抓正文页规则提取 CPI/PPI 同比、
  LPR、进出口总值、城镇调查失业率等原文数值声明，标记 `VERIFIED` 并保留原文片段；
  实测统计局提取 CPI/PPI、央行提取 LPR；`enrich_numeric: true` 在来源配置中开启
- 事件库：`data/policy_events/`，`src/policy_library.py` 提供加载/校验/健康报告/入库更新/状态写回
- DRAFT 工作流：`src/policy_draft_workflow.py`——复核模式列出摘要卡片、来源链接、核验计数与缺失分析字段；`--promote <id>` 要求 facts 有来源、tensions 恰好一个 principal、verdict_reads/falsification/monitor 非空、无 FAILED 核验，条件不满足时逐条拒绝，通过后写 `ACTIVE + promoted_at/promoted_by(manual-review)`，并写入 `policy_event_promoted` 审计事件；库健康报告含 `recently_promoted`，决策卡宏观段展示最近激活事件
- 命令：`python -m src.macro_source_watcher --dry-run` / `--run-once` / `--daemon`；`python -m src.policy_draft_workflow --library data/policy_events [--promote <id>]`；`python -m src.policy_library --library data/policy_events`
- 四面板终端前端（2026-08-13）：`ui/`（对话与任务 / 期权链流动性 / 策略与账户 / 事件与审计）
  + `src/ui_server.py` 本地只读服务：`GET /api/state`（决策卡 + 投研证据 + 宏观研判 +
  政策事件库 + 来源健康报告）、`GET /api/policy-library`、`POST /api/run`（重跑五阶段管线，
  默认写审计与决策卡；`?no_audit=1` 仅测试）；前端零计算、动态文本全 textContent 防注入、
  服务仅绑定 127.0.0.1；静态回退 `ui/data.js`（直接双击 index.html 可用）
- 对话场景解析（2026-08-13）：`src/scenario_parser.py` 确定性解析自然语言
  （标的别名/市场代码、view 关键词、期限、现金 万/千/k、风险预算 %），缺失字段按快照假定
  并标注；`POST /api/chat` 接入 UI 聊天面板，解析失败 400/422 不补造；
  当前 P0 仅 HK.00700 冻结快照，view 只记录不改策略（仍为跨式）
- Windows 计划任务：`scripts/run_watcher_scheduled.ps1`（自动定位 venv，UTF-8 日志写入
  `data/logs/watch_scheduled.log`）+ `scripts/install_watcher_task.ps1 -Minutes 60 / -Remove`；
  已在本机注册 `GOAI-PolicyWatcher`（每 60 分钟、Interactive 登录时运行、不存凭据）；
  开机无人值守需要自行配置凭据，脚本故意不保存任何凭据
- 质量：全量 unittest 已从 275 增至 340 passed（含十角色辩论运行时 54 条，见第 11 节）；本轮新增文件 ruff/mypy 零告警（仓库既有 `backtest_tencent_straddle.py:275 F541`、`pricing_engine.py:152 E731` 与本次无关未动）

---

## 5.5 环境与数据层进度

- OpenD GUI 10.9.6918 已安装到 `%APPDATA%\Futu_OpenD\`；2026-08-12 02:36 已确认在 `127.0.0.1:11111` 登录并通过 typed health，Gateway 不会自动启动、登录或解锁
- futu-api 10.9.6908 + numpy/pandas/matplotlib/backtrader：安装于项目 `.venv`（避开全局 protobuf 5.x 冲突；futu-api 要求 protobuf>=3.20）
- 版本戳 `~/.futu_skill_version = 0.1.1` 已写入（futuapi skill 校验用）
- 数据层骨架（离线冒烟测试通过）：
  - `src/gateway.py`：SDK 无关的 typed contracts、Envelope、请求和账户别名
  - `src/futu_adapter.py`：FutuLiveGateway（只读、惰性 SDK、typed envelope）及 legacy FutuAdapter
  - `src/futu_account_worker.py`：默认真实账户只读的固定 schema 受监督子进程（20 秒硬截止，可终止）
  - `src/snapshot_recorder.py`：并发安全 JSONL 快照录制（`data/snapshots/`）
  - `src/replay_adapter.py`：与 Live 同合同的确定性回放
  - `src/decision_inputs.py`：Agent 粗粒度只读数据刷新入口
  - `src/decision_pipeline.py`：端到端五阶段决策管线（场景解析 → 数据 → 引擎 → Edge/Risk/Action 门控 → 决策卡 → 审计留痕）
  - `src/fallback_adapter.py`：Alpaca/Yahoo 兜底占位
  - `src/models.py`：统一 Quote/OptionContract/Snapshot 模型
- 自研定价引擎与 Hero 用例（2026-08-08）：
  - `src/pricing_engine.py`：欧式 BS + 美式二叉树、IV 二分求解、Greeks bump-and-reprice（Greeks 已对齐 OpenD 期权报价口径）
  - `src/hero_tencent_straddle.py` + `data/hero_inputs.json`：腾讯 0700 业绩跨式全流程（输入为 futuapi 快照）
  - `data/hero_proposal_2026-08-08.json`：方案 JSON（含情景损益）
- 审计：`research/audit/audit_log.jsonl` 已追加 scenario_parsed / proposal / risk_audit 三条哈希链记录
- 投研证据层（2026-08-13）：`src/research_evidence.py` + `data/research_items_hero.json`（synthetic 示例），
  把公告/财报/新闻/研报/行业数据整理成可溯源摘要，并与历史财报日实际波动、隐含事件波动、IV 水位、
  IV crush 参考交叉分析，输出“股价影响 + 期权影响”研判；`python -m src.research_evidence` 独立运行，
  `python -m src.decision_pipeline --research-items data/research_items_hero.json` 已接入决策卡；
  正式数据需替换为 futu-news-search / futu-stock-digest / 公告接口输出的带来源条目（示例数据不得冒充真实证据）
- 宏观研判层（2026-08-13）：`src/macro_assessment.py`：消息面情绪量化指数、IV 情绪晴雨表
  （水位/IV-HV/Skew/crush）、政策博弈矩阵与政治经济学审视（谁获利/谁吃亏/吃亏方是否允许）、
  求是检验（事实/前提/证伪/监控）；`python -m src.macro_assessment` 独立运行，
  `decision_pipeline --macro-policy` 已接入决策卡；方法参考 qiushi-skill 矛盾分析法
- 政策事件库（2026-08-13）：`data/policy_events/` 4 个事件（2025-04 关税 / 2025-05 FOMC /
  2025-04 非农 / 2025-05 芯片管制），每个事件带 `status`/`updated_at`、事实来源 URL 与核验状态；
  `src/policy_library.py` 提供库级加载/校验、来源健康报告（VERIFIED/PENDING/FAILED 计数、
  无来源/无 URL/无抓取时间、过期标记）与入库更新插件 `upsert_policy_event`；
  核验状态不伪造：FOMC 声明链接已实际打开（VERIFIED），其余公开报道链接 PENDING；
  BLS 归档 URL 实测 403，保留 PENDING 供复核
- 宏观来源自动监控（2026-08-13）：`src/macro_source_watcher.py` + `data/sources_config.json`，
  定时抓取美联储/ECB RSS、FRED 宏观序列（CPI/PCE、利率、贸易余额、就业）、BLS、SEC EDGAR，
  按主题过滤、URL 去重后以 DRAFT 入库；FRED/BLS 直接抓取解析的数值标记 VERIFIED，
  RSS 标题只标记 PENDING；单源失败只记录不中断；实测 Fed/ECB 可达、FRED 超时、BLS/SEC 403
  （可 `"enabled": false` 停用）；中国官方（央行/统计局/海关）HTML 列表解析已接入
  （kind `cn_html_list`，`src/cn_source_parsers.py` + `src/topic_classifier.py`），
  2026-08-13 实测 PBC 新闻发布（6 条/轮）与统计局数据发布（5 条/轮）可达并默认启用，
  海关总署本机 TLS 证书链校验失败（Python/curl 均失败）默认停用待复核；标题级提取一律
  PENDING，日期从链接路径 YYYYMMDD 提取，不虚构；2026-08-13 已真实跑通一轮
  `--run-once`（新入库 23 条 DRAFT：Fed 9 / ECB 3 / PBC 6 / 统计局 5），随后回滚为
  4 个精选事件，保证提交库确定性与测试可复现；需要实时数据时运行
  `python -m src.macro_source_watcher --run-once` 或 `--daemon`
- 端到端管线（2026-08-12）：腾讯示例全流程跑通，输出 NO_TRADE（Edge 未过：预期波动 < 盈亏平衡、历史回测负期望、IV crush 情景转负），决策卡 `data/decision_card_2026-08-12.json`，审计链追加 7 条记录
  - Hero 结果摘要：8/14 480 ATM 跨式 2 张（ask 成本 4,414），盈亏平衡 458.9/501.1，最大亏损 ≤ 5% 预算，风险审计 PASS（3 项 WARN：持仓限额/LOP 未验证、费用/滑点 policy 未冻结、美式提前行权/交割风险；以 decision_card_2026-08-12.json 为准）
- 模拟盘执行 legacy 记录（2026-08-08，账户标识已脱敏，现金 100 万 HKD）：
  - `get_max_trd_qtys` 复核通过（C480 最大可买 925 张、P480 879 张）
  - 旧回执标识已脱敏：买入 HK.TCH260814C480000 × 2 @ 10.75（DAY 限价）
  - 旧回执标识已脱敏：买入 HK.TCH260814P480000 × 2 @ 11.32（DAY 限价）
  - 状态：SUBMITTED（周末提交，下一交易日若价格触达限价则成交；模拟盘不支持 GTD）
  - 审计链已追加 `sim_order_submitted`
- 历史回测（2026-08-08，`src/backtest_tencent_straddle.py` → `data/backtest_tencent_straddle.json`）：
  - 口径A（引擎+历史IV，2023Q3–2026Q1 共 11 期）：d+2 平均 ROI -7.8% ± 18.6%（胜率 36%），d+5 平均 +21% ± 19.1%（胜率 64%）
  - 口径B（市场预期波动代理，2021Q3–2026Q1 共 19 期）：d+2 平均 ROI -47.5% ± 14.7%（胜率 16%）
  - 结论：买入业绩跨式历史整体负期望（IV 溢价 > 实际波动），2025–2026 转好但样本小；当前 8/12 模拟单是样本外验证，按小仓位+尾部思维执行
  - 卖跨式镜像：口径B d+2 平均 +42.0% ± 16.3%（胜率 84%，最差 -197%）；口径A（近期 11 期）d+2 平均 -1.9% ± 20.6%（胜率 64%）→ “卖方有溢价”的结论依赖样本与成本口径，近期实际 IV 定价下卖跨式不再显著占优，且尾部损失极大
- `requirements.txt`、`.gitignore` 已建；`.venv/` 与 `data/` 不入库
- 当前不建立 Futu MCP：单一 Python 宿主直接使用 typed Gateway；待出现多宿主/跨语言需求后再增加只读 facade
- 发布信任边界：快照 SHA-256 只检测损坏；当前 fixture 目录按单用户受信任目录处理，多用户部署前需 owner-only ACL + 签名/HMAC manifest 与回滚保护
- 可用性边界：行情 Context 使用异步初始化和 5 秒连接等待上限；futu-api 账户 Context 的同步构造被隔离在 20 秒可终止 worker 内，超时返回 typed `ACCOUNT_UNAVAILABLE`
- 决策（2026-08-12）：腾讯仅为示例标的，模拟跨式单不再跟单/复盘；四面板终端 UI 已落地（2026-08-13，ui/ + src/ui_server.py）；可运行对话 Agent 编排已以十角色辩论运行时落地（2026-08-13，见第 11 节），待办：Live 行情接入

---

## 11. 十角色辩论运行时 — 已完成（2026-08-13）

### 目标（一句话）

把确定性切片升级为「十角色多 Agent 辩论运行时」，通过 OpenAI 兼容接口调用 DeepSeek
（默认 `https://api.deepseek.com/v1`）。LLM 只产文字结论与证据引用，所有数字 / verdict /
门控仍由自研引擎与冻结快照产生；无 key / 网络失败 / 超时自动回退确定性管线，Demo 不崩。

### 已完成（本轮已验证）

- `src/agents/llm_client.py`：stdlib urllib 的 OpenAI 兼容 `/chat/completions` 客户端。
  chat 超时 30s / reasoner 90s（可配）、5xx/429/网络瞬时错误重试 2 次、401/403 不重试；
  `LLMSettings` frozen、`load_settings()` 真实环境变量优先于 `.env`、密钥脱敏
  `redact_secrets()`、粗 token 估算 `estimate_tokens()`、`create_client()` 无 key 返回 None。
- `src/agents/cards.json`：10 张 agent card（orchestrator / data_officer / news_analyst /
  report_analyst / macro_policy_analyst / sentiment_analyst / technical_flow_analyst /
  options_strategist / risk_manager / auditor），含中文角色 prompt、tools、model（reasoner
  用于 orchestrator/macro/risk/auditor，其余 chat）、writes 字段与 output_kind。
- `src/agents/tools.py`：`ToolRegistry` 白名单只读确定性工具（snapshot_summary /
  news_digest / report_comparison / macro_policy / sentiment_iv / technical_flow /
  option_chain / risk_gate / audit_health / injection_check）+ `build_allowed_refs()` 证据 id
  白名单 + `DebateContext` frozen dataclass。technical_flow 诚实标注 frozen_slice，futu
  实时异动接口留 P0b。
- `src/agents/runtime.py`：`run_debate(scenario, snapshot, ...)` 两轮编排——首轮 9 分析角色
  并行 + 主席选最多 3 分歧点 → 次轮只调相关角色回辩 + 主席汇总 `research_consensus`；
  输出 `debate_trace`（每角色每轮 conclusion/evidence_refs/定性置信度/耗时/token/status）；
  reasoner 失败回退 chat；整体 180s 软预算；主席输出非法时确定性分歧/共识回退；
  证据引用按白名单过滤（`dropped_refs` 记录被丢弃项）；`agent_output:<role>` +
  `debate_consensus` 审计事件（复用 audit_log.py 哈希链，密钥片段脱敏）。
- `src/ui_server.py`：新增 `llm_badge()`、`run_chat()`；`POST /api/chat` 流程改为
  确定性场景解析 → `run_pipeline` 取数字与 verdict → 有 key 时 `run_debate` 把
  `debateTrace` + `researchConsensus` 附加到 state → 无 key 保持确定性路径。
  `compose_state()` 已加 `debateTrace=None / researchConsensus=None / llm=llm_badge()`。
- 第五面板（ui/，本轮新增）：`index.html` 底部全宽可折叠 dock（`debate-dock` /
  `debate-toggle` / `debate-body` / `debate-meta` / `debate-rounds` /
  `debate-disputes` / `debate-consensus` / `debate-disclaimer`）+ topbar `llm-chip`；
  `app.js` 加 `renderDebate(D)` + `bindDebateToggle()`，动态文本一律 `textContent`
  （app.js 内所有 innerHTML 赋值均为清空 ""，有静态测试断言）；`styles.css` 加 dock
  样式（状态点/立场/引用 pill/共识卡，移动端折叠为卡片列表，尊重 prefers-reduced-motion）；
  `data.js` 静态回退补 `llm: {available:false,status:"offline"...}` +
  `debateTrace:null, researchConsensus:null`。
- `.env.example`：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / GOAI_CHAT_MODEL /
  GOAI_REASONER_MODEL + 超时与重试参数（`.env` 已 gitignore，密钥不入库）。

### 已验证的检查点状态

- 全量测试：`python -m unittest discover -s tests -q` → **340 passed**（本轮新增 54 条：
  test_llm_client / test_agent_tools / test_debate_runtime / test_debate_ui）。
- 新代码质量：`ruff check src\agents src\ui_server.py tests\test_llm_client.py
  tests\test_agent_tools.py tests\test_debate_runtime.py tests\test_debate_ui.py` 全过；
  `mypy --ignore-missing-imports src\agents src\ui_server.py` 无告警（全局 anaconda3 的
  ruff/mypy，不在 .venv；顺手修掉了 ui_server.py run_chat 传给 run_debate 的 Mapping/dict 类型）。
- 离线冒烟：无 key 时 `run_chat()` 返回 `debateTrace.status == "offline"`、
  `fallback_reason == "no_api_key"`、`researchConsensus == None`、verdict 仍 `NO_TRADE`。
- FakeLLM 冒烟：十角色两轮 trace 结构正确、分歧点只调用相关角色、伪造引用被 dropped、
  主席失败走确定性分歧/共识回退、单角色超时被隔离、401 全场失败仍优雅回退、审计脱敏。
- 前端静态断言：第五面板元素齐备、app.js 动态文本一律 textContent（innerHTML 仅清空）。

### 真机冒烟（2026-08-13 已跑通，DeepSeek 真实 key）

- 配置：`.env`（gitignore，不入库）填 DEEPSEEK_API_KEY → `python -m src.ui_server --port 8000`
  → POST `/api/chat`。实测一次通过：
  - `debateTrace.status=complete`，两轮：首轮 10/10 ok（9 分析角色 + 主席），次轮 6 条
    （3 个分歧点定向调用 5 个相关角色 + 主席汇总）；
  - `research_consensus`：source=llm、stance=oppose、confidence=high，摘要正确引用引擎数字
    （预期波动 3.92% < 盈亏平衡 4.41%、d+2 ROI -7.8%、IV crush 最差 -711 HKD）并声明维持
    引擎 NO_TRADE/BLOCK 判定；open_questions 3 条；
  - 墙钟 67s（180s 软预算内），total_tokens 38,838，audit_errors=0；
  - 审计链追加 16 条 `agent_output:<role>` + 1 条 `debate_consensus`（SHA-256 哈希链完整），
    密钥零泄漏；顶栏 llm 徽章 `available=true / DeepSeek`，第五面板 dock 正常服务。
- 离线回退复测：临时移除 `.env` 后 POST `/api/chat` → `status=offline`、
  `fallback_reason=no_api_key`、`researchConsensus=null`、verdict 仍 NO_TRADE；恢复
  `.env` 后徽章回到 DeepSeek。Demo 不崩目标达成。
- 备注：DeepSeek 端点当前把 deepseek-chat 请求实际路由为 deepseek-v4-flash 模型
  （tiny 连通性调用实测），不影响 OpenAI 兼容接口合同；正式演示前可自行确认额度。

> 设计不变量（数字铁律/引用白名单/失败降级/审计注入/编码）已上移到 PROJECT_STATE.md §11，勿破坏。

---

## 12. DSH 编排层 Phase 0 — 已完成（2026-08-13）

架构决策：GOAI 采用「Python 引擎 + DSH 编排层」混合架构。数值铁律、审计链、定价引擎、
十角色辩论全部留在 Python（Futu OpenAPI 仅 Python SDK；评审可独立跑
`python -m src.ui_server`，不依赖 DSH）；DSH 负责 Agent 编排、工具注册、审批与答辩展示。
JS 层不重算任何数字，只透传引擎结果。

- 新增工件：`harness/plugins/goai-bridge.host.js`（host-only 动态插件源码，DSH 进程级资产；
  DSH 重启后需按文件内容 cordis_define/cordis_run 重新注册，Phase 1 固化 bootstrap 脚本）
- 工具：`goai_state`（GET /api/state，内存重算不写审计）、`goai_run`（POST /api/run，
  默认写审计+决策卡，noAudit=true 只算不写）、`goai_chat`（POST /api/chat，
  场景解析→五阶段管线→十角色辩论，无 key 离线回退）。execute 返回紧凑投影（叶字段），
  render 输出实质摘要（verdict/三门控/快照哈希/辩论共识）——render 文本是模型可见内容
- 引擎生命周期：插件用 subprocess.spawn 拉起 `.venv\Scripts\python.exe -m src.ui_server`
  （127.0.0.1:8000），ctx.effect 持有终止句柄（插件停用/更新自动回收子进程=可逆效应）；
  已运行的引擎先健康检查、复用且不误杀；启动轮询 40s 超时并带回 stderr 尾部诊断
- 真机验证（2026-08-13）：goai_run → 审计链+`decision_card_2026-08-13.json` 落盘；
  goai_chat → 辩论 complete、consensus oppose/high（带 open_questions）、审计链累计
  82 行 / 35 agent_output / 2 debate_consensus、哈希链完整、usage token 脱敏 [REDACTED]；
  中文 message 经 stdin 传参无编码问题
- 踩坑记录：defineTool 要求 parameters/output 根 schema 显式 `additionalProperties: true`；
  `riskGate.blocked` 是数组（空数组=未阻断，`!![]` 恒 true 是 bug）；插件内 root 需硬编码
  仓库路径（sandboxPolicy.workspaceRoot 不指向仓库）
- 权限：本会话审批策略已切回 ask（preset workspace-write：沙箱 workspace-write + 审批 ask）；
  DSH 客户端插件激活需用户在授权卡打勾（单勾=当前版本，双勾=后续版本自动放行）


---

## 13. 代码评审修复轮 — 已完成（2026-08-14）

对评审报告（B1 + M1-M5 + m1-m12 + harness M6-M9/M11）逐条修复，4 个 commit
（4f4b520 / 98a69c6 / 5e6b077 / 93ca425），本地 main 待 push。

- B1（唯一 Blocking）：决策卡 summary/scenario 改为按 Edge/Risk/Action 门控结果与 parsed
  场景动态生成（新增 _summary_text），清掉 key_evidence 结论句、8/28 到期、480 ATM、
  默认约束 5% 等全部写死文案；hero CLI 的 proposal scenario / 到期日 / r-q 参数同步数据驱动。
- M1 定价引擎基准测试（BS 已知价 / IV 往返 / 美式下界 / Greeks 解析对照，19 条）时发现并
  修复真 bug：美式 Greeks 小 bump 撞二叉树节点扭结，hero 参数（2 DTE、steps=500、h=0.5）
  下 gamma 恒为 0.0（解析真值 0.0176/股）→ delta/gamma 改节点间距自适应 bump
  （2×/6× spacing），决策卡美式 Greeks 数字随之修正。
- M2 Futu 断连自愈（连接级失败 close+置空缓存）；M3 快照 model 段有限数值校验；
  M4 requirements 补 pytest==9.0.3；M5 bridge 根路径参数化（GOAI_PROJECT_ROOT）。
- 打磨轮：m4 敏感键拒绝收敛 payload_validation；m3 决策卡原子写；m10 审计 subprocess 超时；
  m7 3+ 到期不张冠李戴（strategy 置空 + 前端防御渲染）；m8 /api/state 30s 缓存；
  m9 UI 加固（Content-Length 上限 / 错误路径脱敏 / symlink 逐组件校验 / BrokenPipe /
  Host 校验防 DNS rebinding）；m11 GatewayError 删除死重 details 字段（wire 3 字段）、
  6 位数字误伤修复、assert 控制流、recorder 行数 O(n²)→缓存、watcher 类型化
  EmptyPolicyLibrary；m12 回测 R/Q/T 与快照 model 同步（重跑输出字节一致）；
  harness M6 引擎崩溃重拉 / M8 就绪探测文案 / M9 curl 解析失败重试。
- 踩坑记录：hero CLI 的 audit 子进程 text=True 未指定 encoding="utf-8"，中文 payload 按
  locale（GBK）编码后审计脚本 UTF-8 解码崩溃（与 §3 的 stdin 坑同源，pipeline 版用
  bytes 编码早已规避）；M6 修复第一版把「崩溃重拉」分支放在 booting 短路之后导致失效。
- 验收：.venv 按 requirements.txt 安装后 `python -m pytest tests -q` →
  367 passed + 157 subtests（208.5s）；审计链 83 行哈希完整；回测输出字节一致；
  /api/chat 自然语言场景（看跌/20 万/3%）summary 正确跟随场景，无 key 离线降级正常。

---

## 14. 产品评审修复轮 — 已完成（2026-08-14/15）

产品经理视角评审（行动闭环断点 / 冻结快照像实时盘 / 不可度量 / 审计不可见 / 多标的体验 / 文档漂移）以长期 goal 分两阶段修复。速览见 PROJECT_STATE.md §14 与 deliverables/evidence/product-review-fixes.md。

### 第一阶段：产品修复六项

1. **决策卡行动闭环**：GET /api/decision-card（最新落盘决策卡 + 相对路径 + SHA-256 身份哈希，只读不写审计）；决策卡视图「下一步」动作区（导出按钮下载 JSON + 哈希提示；修改条件重算 → 打开研究条件面板；按 verdict 的行动文案；P0c 边界声明「模拟提交未启用」）。浏览器实测导出 sha256 提示正确、重算面板展开。
2. **能力声明首屏**：总览顶部能力条（当前切片 P0a·Replay 只读 / 数据模式 + 快照时间戳「非实时行情」/ 支持范围 / 一键运行管线按钮）。实测渲染正常。
3. **审计视图**：GET /api/audit?limit=N（全链 prev_hash 衔接校验 chainOk + 每类事件紧凑摘要 + dropped_refs 投影 + 12 位哈希前缀 + 路径脱敏）；新「审计」视图（链状态徽章 / 事件明细表 / 被拒引用红字徽章）；「分歧」后的第 8 个视图。实测 95 条链完整、auditor 8 条 dropped_refs 正确显示。
4. **会话度量日志**：data/logs/session_metrics.jsonl（ts/event/input/verdict/duration_ms/mode；线程安全追加；失败静默）；GET /api/metrics（尾部条目 + byEvent/byVerdict/avgDurationMs）；审计视图「会话度量」面板；总览「最近分析耗时」。端到端实测 POST /api/agent(refresh) 13.8s → NO_TRADE 记录。
5. **多标的项目体验**：workspace_registry（未提交改动）整合验证；无快照标的失败路径实测（422 + 明确范围声明 + 录制指引）；新增 docs/SNAPSHOT_RECORDING.md（快照契约/两种录制路径/失败对照表/范围声明）；UI 表单常驻范围声明。
6. **文档同步**：PRD v0.6（场景解析口径、7 视图壳结构、DSH 插件族、实现真相表、§10.5 增量计划）；README（7 视图/插件族/新 API/录制指引/核心原则口径修正「LLM 不解析场景」）。

### 第二阶段：深化与打磨

7. 审计/度量深化：总览「最近分析耗时」（metrics 驱动）；决策卡「下一步」页脚「审计链完整 · 共 N 条」（audit 驱动，链校验可视化）。
8. 多标的落地：失败路径实测（SSE.600519 → 422 含指引）、超范围明确提示、录制指引文档。
9. 全链路浏览器 e2e：8 视图 + 4 抽屉实测（DOM 断言 + 内容检查），console 0 错误，14 张截图存档 deliverables/evidence/screenshots/。
10. 测试扩充：新增 decision-card/audit/metrics 端点测试、agent 动作写度量集成（真实 select_expiry 14.5s）、audit 日志缺失降级、workspace 无快照指引；插件族 verify/smoke 复跑 PASSED。
11. 发布材料：deliverables/evidence/product-review-fixes.md（评审问题→修复→证据）、deliverables/demo-script.md（0-120s 演示脚本）。
12. 稳定性：审计降级测试（AUDIT_LOG 缺失 → found:false 不 500）；gitignore 补 .playwright-mcp//tmp/；管线耗时由 metrics 记录（command 5.6s / refresh 13.8s）；未提交工作区文件处置清单（PROJECT_STATE §14）。

### 验证

- Python 全量：418 passed（2026-08-15；含新增 24 个）
- 插件族：verify_plugins.ps1 PASSED、smoke_plugins.mjs PASSED
- 浏览器 e2e：8 视图 + 4 抽屉、console 0 错误
- 铁律：JS/LLM 未重算数字（展示字段全部来自引擎 API）；审计链只增不减（audit 端点只读）；能力宣传不超过 P0a 切片（Live/模拟提交明确标注未毕业）


## 15. DSH agent preset 产品评审修复轮 — 进行中（2026-08-15）

目标：像业内顶尖产品经理一样测试 goai-options preset，产出优化建议并执行。

### 发现与修复

1. **tool-cordis 挂载冲突（阻断）**：同一 DSH 进程内 cordis 预设先挂载后，
   goai-options 的 tool-cordis 在 `session.create` 时报
   `Host Cordis inspect provider "Service" is already registered`。
   → tool-cordis 默认 `disabled: true`（插件注册/调试走 cordis 预设）。
2. **模板漂移**：harness/preset 曾缺 skills/、描述与能力不符。
   → 模板成为唯一安装源，新增 verify_preset.ps1/mjs 做静态+逐字节比对。
3. **工具面过宽**：subagent/workflow/ralph 与终端业务链无关。
   → delegation group 默认 disabled。
4. **首启引导**：persona 增加开场披露（goai_* 可用性/独立模式兜底）、
   Hero 示例、中文工作语言、范围外标的拒绝与录制指引、CLI 用 pwsh 执行纪律。
5. **tool-search 与 preset 分层不兼容（严重）**：实验性
   `@deepseek-ai/dsh-tool-search` 只索引全局工具，preset 层
   pwsh/read/write/view_image 全部不可见。新增 fix_dsh_tool_visibility.ps1
   停用该插件（-Undo 回滚），已应用到 `~/.dsh/profiles/web/cordis.patch.yml`，
   3081 对照实例验证 97 工具可见，3080 主实例待重启激活。

### 验证

- verify_preset.ps1 / verify_preset.mjs / smoke_preset.mjs PASSED；
- 真实 goai-options 对话四连测：开场披露、实盘硬阻断、范围外标的拒绝、
  独立模式 CLI 重跑 NO_TRADE（照抄引擎数字 + stale 提示 + 审计来源）；
- 主实例 3080 最终 e2e：profile patch 无需重启即对新建会话生效（请求头 97
  工具）→ cordis 会话注册 goai-core/run/chat 全 running → goai-options 会话
  调 goai_state 照抄引擎数字（NO_TRADE/LOW_EDGE/PASS/stale/短哈希）；
- 新增 tests/test_preset_files.py（7 项 preset 打包不变量，0.04s 全绿）；
- PRD v0.7 新增 §3.6 agent preset 产品入口。

验证完成：全量 pytest 回归 **584 tests / 0 failures / 0 errors / 0 skipped**
（292.3s，junit 落盘）；DSH 将来重启后需按 bootstrap 重新注册插件族
（进程级资产语义，已文档化）。
