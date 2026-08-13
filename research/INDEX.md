# 知识库索引（INDEX）

最后更新：2026-08-13 | 用途：GOAI Boundless 期权 Agent 项目的随时参考库（领域知识层；项目架构见 README 与 docs/DSH_ARCHITECTURE.md）

## 关键结论速览

1. 赛道：Boundless Agents（无界应用）· 金融服务。产品定位：港美股期权智能投研与风险 Agent（“Bloomberg 式终端 + Agent 决策层”），不做执行、不做荐股；编排层运行在 DeepSeek Harness（「大号金融插件」），产品可独立运行。
2. 差异化：对话式场景→账户约束求解；流动性评分与主动预警；业绩事件模块（IV crush、隐含波动 vs 实现波动）；审计溯源；港美股规则差异显性化。
3. 数据层：富途 OpenAPI + 官方 futuapi Skill（实时行情/推送/模拟盘，56 API）为主力；美股期权数据同样使用富途（2026-08-12 拍板）；快照回放兜底；Yahoo 限流实测（429）不可依赖；Alpaca 不作为正式数据方案。
4. 边界：SFC 牌照（Type 1/4/9）——只做研究/教育/决策辅助；HKEX 数据延迟≥15 分钟且仅 L1，实时需牌照；港股期权美式实物交割、持仓限额；LLM 幻觉靠“计算引擎+审计”消除。
5. 方法库：tastytrade（16Δ/45DTE/50%止盈）、Sinclair（一致性流程）、Karsan（波动率供给/流动性）、Moontower（观点→期权表达）、TradingAgents（7 角色多 Agent）、SpotGamma（GEX 公式）、ORATS（IV crush 数据）。
6. 威胁：moomoo Engine（gamma/异动预警）、moontower.ai（最接近的产品形态）、Option Alpha（自动化规则）。护城河=对话场景+账户约束+流动性预警+可复核审计。

## 文档地图

| 文件 | 内容 |
|---|---|
| 01-market-landscape.md | 竞品矩阵与缺口 |
| 02-data-sources.md | 数据源分层、Futu OpenAPI/futuapi Skill、授权边界 |
| 03-boundaries-risks.md | SFC/HKEX 合规、期权市场风险、LLM 与技术风险 |
| 04-expert-methods.md | 专家与机构方法论（含可落地公式/参数） |
| 05-optimization-opportunities.md | 功能优先级、优化点、Demo 取舍 |
| 06-communities-forums.md | Reddit/EliteTrader/QuantSE/OIC/X 账号/播客 |
| sources.json | 结构化来源数据库（id/title/url/type/key_points/tags） |

## 项目文档（非研究层）

| 文件 | 内容 |
|---|---|
| README.md | 产品总览、两种运行模式（独立 / DSH）、Quick Start |
| PROJECT_STATE.md | 项目进度快照（每会话恢复必读） |
| docs/PRD.md | 产品需求与比赛验收 |
| docs/DSH_ARCHITECTURE.md | DSH 编排层权威架构（大号金融插件：插件合同/映射表/阶段路线） |
| docs/HISTORY.md | 项目历史归档（PROJECT_STATE 移出的实现/验证细节；新会话无需读） |
| ui/README.md | 四面板数据终端（独立模式 + 静态回退） |

## 待办与开放问题

- [ ] B：验证富途账户 OpenAPI 权限 + futuapi skill 安装 + 港股期权实时权限
- [ ] B：定义腾讯跨式用例的规则集（策略、Greeks、风险检查单）
- [x] A：搭建数据层骨架（FutuAdapter + SnapshotRecorder + ReplayMode + FallbackAdapter）
- [ ] C：起草合规/数据授权页 + 安全设计（密钥、防注入、审计）
- [ ] 开放问题：富途 OpenAPI 下港股/美股期权实时权限与费用需在比赛机器实测（美股数据方案已定：富途）
