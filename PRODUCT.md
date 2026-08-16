# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop (PySide6 + QtWebEngine shell with a loopback Python service)

## Users

主要用户是具备基础期权知识、会使用富途但缺少专业终端和完整量化流程的港美股期权投资者。典型场景是用户描述标的、事件、方向不确定性、账户规模和最大风险预算，并需要在研究与确认前快速判断是否值得交易。

## Product Purpose

GOAI 将自然语言期权场景整理成可核验的研究任务，汇总行情、期权链、事件、账户约束和风险检查，产出决策卡。成功不是强行给出交易，而是让用户在有限时间内知道系统知道什么、结论为什么成立、哪些条件会改变结论，以及下一步是否应停止、调整或独立确认模拟方案。

## Positioning

GOAI 的差异化机制是把对话场景转成账户约束下的确定性期权研究流程，由自研定价与风控引擎守护数字和审计链，Agent 负责场景解析、研究编排和解释。它是研究与决策支持工具，不是投资建议，也不做实盘自动交易。

## Operating Context

产品运行在 DeepSeek Harness 的金融插件形态中，同时提供不依赖 Harness 的本地桌面独立模式。桌面壳由 PySide6 + QtWebEngine 提供，数据面仍只通过本机回环 Python 服务和 `/api/state`、`/api/command` 驱动；默认展示 Replay/冻结快照。用户也可以通过终端命令或自然语言触发场景解析、五阶段管线和十角色辩论。典型演示是腾讯（0700.HK）业绩前方向不确定、账户约 10 万港币、评估跨式策略。

## Agent Preset Entry

在 DeepSeek Harness 中，用户通过 **GOAI Options Terminal**（`goai-options`）agent preset 进入产品。该预设绑定 GOAI 产品人格与铁律，并采用产品安全默认：tool-cordis 默认禁用（插件注册/调试使用 cordis 预设），subagent/workflow/ralph 不挂载，view_image 视觉可用。goai_* 插件未注册时，Agent 必须如实披露并回退独立模式 CLI，不得假装工具可用。preset 的安装、校验与兼容性运维见 `harness/README.md`。

## Capabilities and Constraints

- 展示决策卡、Edge/Risk/Action 三道门、Greeks、情景损益、期权链、IV/OI、账户风险、宏观研判、投研证据、政策事件库和十角色辩论。
- `GET /api/state` 提供当前状态，`POST /api/run` 重跑冻结快照管线，`POST /api/chat` 将自然语言接入确定性场景解析与辩论。
- 所有金融数字、verdict、gate 和数量必须由 Python 引擎产出；LLM 只能提供文本、枚举和证据引用，不能重算数字。
- 每个关键结果都应能追溯到数据快照、来源时间和本地 SHA-256 审计身份。
- 当前版本为 P0a Replay + P0b Live 只读第一阶段：`GOAI_DATA_MODE=live` 时
  UI 使用只读实时报价链路（`/api/live-quote`、state LIVE/FRESH），
  OpenD 不可用显式报错；模拟提交和确认闭环仍未毕业，不能暗示已具备实盘
  或稳定模拟下单能力。
- OpenBB/yfinance 是可选历史行情 provider，只能补充历史价格 OHLCV 与由日线收益率计算的 30 日实现波动率，并保留来源/时间/可用性；它不替换 Futu/OpenD 快照、期权链，也不参与策略结论。
- 桌面端无前端构建步骤，核心界面文件为 `ui/index.html`、`ui/styles.css`、`ui/app.js`、`ui/data.js`，原生入口为 `src/desktop_app.py`；重构必须保持这些入口、接口、字段 ID 和静态回退能力可用。
- 用户可以得到 `NO_TRADE`，系统不能为了展示交易而放宽规则；任何模拟动作仍需用户独立确认。

## Brand Commitments

产品名为 GOAI 港美股期权智能终端。中文优先，金融术语可辅以英文。产品语言应直接、可核验、少承诺，不使用投资收益保证或虚构市场数据。

## Evidence on Hand

- `PROJECT_STATE.md`、`README.md`、`docs/PRD.md`：产品定位、边界、架构与已验证能力。
- `ui/README.md`、`ui/index.html`、`ui/styles.css`、`ui/app.js`、`ui/data.js`：现有前端 IA、交互和数据字段。
- `data/hero_inputs.json`：2026-08-08 的 Futu OpenD 冻结快照。
- `research/audit/audit_log.jsonl`：本地 JSONL + SHA-256 审计链。
- `data/research_items_hero.json` 为 synthetic 示例，只能标注为演示数据，不能冒充真实市场证据。
- 当前没有可用于宣传客户、收益、基准或实时交易能力的公开证据。

## Product Principles

1. 先说明证据和边界，再展示结论。
2. 数字来自确定性引擎，来源与快照身份始终可见。
3. `NO_TRADE` 是有效结果，不为更积极的动作调整规则。
4. 用户始终掌握停止、调整和独立确认的权力。
5. 复杂研究流程应在一屏内可扫描，并允许逐层展开细节。

## Accessibility & Inclusion

前端面向中文优先的桌面研究场景，同时保留移动端可用性。交互控件需要键盘可达、焦点清晰、文本与状态具备足够对比度，颜色不能成为传达风险状态的唯一方式，并应尊重 `prefers-reduced-motion`。
