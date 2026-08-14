# GOAI × DeepSeek Harness — 架构文档（大号金融插件）

> 权威性：本文档与 `harness/plugins/goai-bridge.host.js` 一起定义 GOAI 的 DSH 编排层。
> 数值、审计与产品边界仍以 `docs/PRD.md` 和 `src/` 引擎为准。最后更新：2026-08-13。

## 0. 一句话定位

GOAI 是一个运行在 **DeepSeek Harness（DSH）** 上的**大号金融插件**：
Python 确定性引擎守护全部金融数字与审计链，DSH 负责 Agent 编排、工具注册、人机审批与对话体验。
评审/用户也可以完全不装 DSH，用 `python -m src.ui_server` 独立运行——两条入口共享同一引擎契约。

## 1. 为什么是混合架构（三个硬约束）

1. **Futu OpenAPI 只有 Python SDK**：`FutuLiveGateway`、`SnapshotRecorder`、自研定价引擎、审计链
   全部留在 Python 3.13 + `.venv`，不可重写为 JS/TS。
2. **比赛评审要能独立跑**：初赛/复赛的"可运行 Demo"不能依赖 DSH，`python -m src.ui_server`
   这条链路必须永远可用。
3. **项目缺的正是 Agent 编排层**：DSH 的 `tools` 注册表、`approval` 人机确认、`subprocess`
   进程管理、`timer` 定时器、`skills` 注册表，每一项都正好补上"可运行的对话 Agent 编排"。

结论：**数字留在 Python，编排交给 DSH，JS 层不重算任何数字。**

## 2. 三层架构

```text
┌────────────────────────────────────────────────────────────────┐
│ DSH 编排层（harness/plugins/goai-bridge.host.js，JS）            │
│  · host 插件：subprocess.spawn 拉起引擎，ctx.effect 持有终止句柄  │
│  · model tools：goai_state / goai_run / goai_chat               │
│  · 计划（Phase 1）：approval 接管 READY_FOR_CONFIRMATION、        │
│    client 面板、goai-options agent preset、bootstrap 脚本        │
├────────────────────────────────────────────────────────────────┤
│ 引擎契约（已有，不动）：src/ui_server.py 的 JSON API             │
│  GET /api/state · POST /api/run · POST /api/chat                │
│  （127.0.0.1:8000；四面板 UI 与 DSH 插件共用同一契约）            │
├────────────────────────────────────────────────────────────────┤
│ Python 引擎（不动）：gateway / pricing / pipeline /              │
│  debate runtime / audit 链 / policy library · 367 tests         │
└────────────────────────────────────────────────────────────────┘
```

## 3. 资产映射表（GOAI → DSH 机制）

| GOAI 资产 | DSH 落点 | 状态 |
|---|---|---|
| 五阶段管线 `decision_pipeline.py` | model tool `goai_run`（POST /api/run） | ✅ Phase 0 已通 |
| 决策终端状态 | model tool `goai_state`（GET /api/state） | ✅ Phase 0 已通 |
| 对话链路（场景解析+管线+十角色辩论） | model tool `goai_chat`（POST /api/chat） | ✅ Phase 0 已通 |
| 引擎进程生命周期 | `subprocess.spawn` + `ctx.effect` 终止句柄（可逆效应） | ✅ Phase 0 已通 |
| `READY_FOR_CONFIRMATION` 模拟下单确认 | DSH `approval` 服务（withhold-until-commit） | 📋 Phase 1a |
| 四面板终端 `ui/` | 独立数据终端；DSH client 插件面板（settings.section / shell.overlay / tool.call.toolview） | 📋 Phase 1a |
| 项目铁律（LLM 不算数/模拟盘/人机确认） | `goai-options` agent preset 的 systemPrompt 节 + `futu-options-agent` skill | 📋 Phase 1b |
| 宏观监控 Windows 计划任务 | 保留计划任务；可选 `timer.interval` 双轨 | 📋 Phase 2 |
| 十角色辩论 `agents/runtime.py` | 保留 Python 实现；可选改 DSH `subagents` 子代理 | 📋 Phase 2（可选） |
| 快照新鲜度失效传播 | DSH Event（`goai/snapshot-stale`）+ 工具 guard 对比快照哈希 | 📋 Phase 2 |

## 4. 插件合同（goai-bridge）

- **工具**：`goai_state`（内存重算，不写审计）、`goai_run`（默认写审计+决策卡，
  `noAudit=true` 只算不写）、`goai_chat`（`message` 中文场景，默认写审计；无 key 离线回退）。
- **结果契约**：`execute` 返回紧凑投影（只取叶字段：verdict、三门控、快照身份、LLM 徽章、
  辩论共识）；`render` 输出实质摘要文本——**render 文本是模型可见内容**，必须包含数字，
  不能只回状态码。
- **引擎生命周期**：懒启动（首次调用）→ 健康检查 → 失败才 spawn；
  `ctx.effect` 持有终止句柄，插件停用/更新/会话结束自动回收子进程（可逆效应）；
  已在运行的引擎复用且不误杀（external 模式）。
- **HTTP 桥**：`curl.exe` 子进程（GET/POST 统一）；POST 请求体走 stdin `{data}`，
  避开 Windows 命令行中文编码问题。
- **已知约束**：动态插件是 DSH 进程级资产，DSH 重启后需按仓库文件重新
  `cordis_define`/`cordis_run`（Phase 1c 固化为 bootstrap 脚本）；客户端插件包激活需要
  用户在授权卡打勾（单勾=当前版本，双勾=后续版本自动放行）。

## 5. 机制对照（论文概念 ↔ DSH ↔ GOAI）

| 论文/Cordis 概念 | DSH 机制 | GOAI 落地 |
|---|---|---|
| 可逆效应（effect + inverse） | `ctx.effect` disposer；不可变 package 版本 + 事务化 update/rollback | 引擎子进程随插件生命周期回收 |
| 输出提交（withhold until commit） | `approval` 服务 | READY_FOR_CONFIRMATION → 人机确认（Phase 1a） |
| 故意不可逆效应 | 会话日志 append-only | 审计链 JSONL + SHA-256，永不回滚 |
| uid/摘要比较 | 快照哈希作为工具结果身份 | `snapshot_sha256` 进投影与摘要 |
| realm 隔离 | agent preset 的 isolate realm / 会话级工具作用域 | Phase 2：多会话账户/快照隔离 |
| 惯性状态机 | `jobs` 服务 + 目标摘要比较 | Phase 2：POST /api/run 连点竞态 |

## 6. 阶段路线

- **Phase 0（已完成，2026-08-13）**：goai-bridge host 插件 + 三工具全链路真机验证
  （审计链 83 行、辩论 complete、consensus oppose/high）。
- **Phase 1a**：client 插件（DSH 内决策卡面板 + 预警 dock + 自定义工具卡）+ approval 接管。
- **Phase 1b**：`goai-options` agent preset（复制 standard → 注入 GOAI 铁律 persona）。
- **Phase 1c**：bootstrap 脚本（一条命令恢复插件注册）、`futu-options-agent` skill 更新、
  本文档维护。
- **Phase 2**：freshness 失效传播、jobs 惯性状态机、辩论 subagent 化（可选）、realm 隔离。

## 7. 铁律（编排层不许破坏）

1. JS/LLM 都不重算数字；verdict/门控只来自 Python 引擎。
2. `python -m src.ui_server` 独立链路任何时候可跑，评审不依赖 DSH。
3. 审计链只增不减；证据白名单、脱敏逻辑只增不减。
4. Python 367 tests 全绿是底线；插件改动不触碰 `src/`。
