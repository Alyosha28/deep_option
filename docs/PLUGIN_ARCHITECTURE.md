# GOAI × DeepSeek Harness — 插件族架构（Base Mode + 可选插件）

> 权威性：本文档与 `harness/plugins/goai-*.host.js`、`harness/config/goai.plugins.json`
> 一起定义 GOAI 的 DSH 插件族。数值、审计与产品边界仍以 `docs/PRD.md` 和 `src/` 引擎为准。
> 最后更新：2026-08-14。

## 0. 一句话定位

DSH 底层是 Cordis 内核（[官方 Cordis 入门](https://github.com/deepseek-ai/DeepSeek-Harness/blob/master/docs/cordis-primer.zh.md)），
GOAI 的编排层因此天然是「插件族」：**Base Mode = `goai-core` + `goai-run` + `goai-chat`**
（保证基本使用，等价旧单体 `goai-bridge` 的三工具），其余模块做成可选插件，
用户在 `harness/config/goai.plugins.json` 里勾选加载哪些。Python 引擎保持单体不动。

## 1. 为什么可行（三个事实）

1. **DSH 动态插件就是 Cordis 插件**：`{ name, inject, apply(ctx) }` + `ctx.effect`
   可逆效应 + `ctx.subprocess`/`ctx.timer` 服务注入，`cordis_define`/`cordis_run`
   是官方注册通道。旧 `goai-bridge.host.js` 已经是一个标准 Cordis 插件。
2. **引擎契约是唯一依赖**：所有插件只通过 `ui_server` JSON API（127.0.0.1:8000）
   或模块 CLI 触达引擎，JS 层不重算任何数字（铁律 #1）。
3. **Python 引擎无需改动**：插件化切在 JS 编排层，`src/` 584 tests 底线不动（铁律 #4）。

## 2. 插件清单

| 插件 | 工具 | 职责 | 配置默认 | 依赖 |
|---|---|---|---|---|
| `goai-core` | `goai_state` | 引擎生命周期（懒启动/健康检查/可逆回收）+ HTTP 桥 + 决策卡状态 | **Base 必备** | — |
| `goai-run` | `goai_run` | 重跑五阶段管线（POST /api/run） | Base 默认 | 引擎 |
| `goai-chat` | `goai_chat` | 对话链路：场景解析+管线+十角色辩论（POST /api/chat） | Base 默认 | 引擎 |
| `goai-macro` | `goai_policy_library` / `goai_macro_watch` | 政策事件库只读视图 + 宏观来源监控手动一轮 | 可选（默认关） | 引擎 / 独立 CLI |
| `goai-research` | `goai_research_evidence` / `goai_research_sources` | 投研证据包 + 新闻/研报 canonical 适配 | 可选（默认关） | 引擎 / 独立 CLI |
| `goai-backtest` | `goai_backtest` | 腾讯 0700 业绩跨式历史回测 | 可选（默认关） | 独立 CLI |

旧 `goai-bridge.host.js`：Phase 0 单体版，保留为兼容回退；**与本插件族二选一，不要同时注册**（工具名重叠）。

## 3. Base Mode（保证基本使用）

- 定义：`goai-core` + `goai-run` + `goai-chat`，功能等价旧 `goai-bridge` 三工具，
  演示/评审链路零回归。
- 用户最少只需注册这三个插件即可完成：「goai_state 看状态 → goai_run 重跑管线 →
  goai_chat 对话辩论」的完整闭环。
- 即使只加载 `goai-core`，引擎生命周期与状态读取仍然可用（最瘦 base）。

## 4. 用户选择机制

```text
harness/config/goai.plugins.json   ← 用户勾选（enabled: true/false）
        ↓ 改完运行 verify_plugins.ps1（语法+配置校验）
harness/bootstrap.ps1              ← 读取配置，打印 DSH 注册指引
        ↓ 把指引交给 DSH 会话助手
cordis_define（每个启用插件一个）→ cordis_run（host-only，无需审批）
```

- 插件文件自包含：复制任意一个 `.host.js` 的内容即可单独注册，删除文件 = 移除模块。
- 动态插件是 DSH 进程级资产：DSH 重启后需重新注册（bootstrap 输出即恢复步骤）。

## 5. 引擎生命周期约定（插件间的共享规则）

每个插件自带同一份「shared engine client」（懒启动 → 健康检查 → 失败才 spawn；
已在运行的引擎复用为 external 且不误杀；`ctx.effect` 只回收本插件自己 spawn 的进程）。
要点：

- **同一引擎进程**：所有插件共用 127.0.0.1:8000，不是每插件一个进程。
- **并发首调自愈**：两个插件同时首调且引擎未起时，后 spawn 者会因端口占用快速退出；
  探测循环检测到「本实例退出但 /api/state 已可达」时自动转为 external 复用，不报错。
- **推荐拓扑**：Base Mode 下由 `goai-core` 实际托管引擎进程；feature 插件在
  core 缺席时也能独立拉起（自包含），但建议始终加载 core。

## 6. 铁律（与 docs/DSH_ARCHITECTURE.md 一致，插件族不破坏）

1. JS/LLM 都不重算数字；verdict/门控只来自 Python 引擎。
2. `python -m src.ui_server` 独立链路任何时候可跑，评审不依赖 DSH。
3. 审计链只增不减；证据白名单、脱敏逻辑只增不减。
4. Python 584 tests 全绿是底线；插件改动不触碰 `src/`。
5. 新增插件必须是自包含 Cordis 插件（`return { name: 'goai-*', apply(ctx) }`），
   工具名遵循 `goai_*` 前缀，且不能与已注册工具重名。

## 7. 验证

```powershell
powershell -ExecutionPolicy Bypass -File harness\verify_plugins.ps1   # 配置+语法+形状+base 完整性
powershell -ExecutionPolicy Bypass -File harness\bootstrap.ps1       # 环境自检 + 注册指引
node harness\smoke_plugins.mjs                                       # 免 DSH 冒烟：mock ctx + 真 curl/真引擎（只读工具）
node harness\smoke_plugins.mjs goai_run goai_backtest                # 指定工具冒烟（run 用 noAudit=true）
```

冒烟脚本用 mock 的 `ctx`/`harness` 在 Node 里真跑插件代码：注册 → execute →
（引擎 API 或 CLI）→ 投影 → render，可覆盖除 DSH 会话本身外的全部逻辑。

## 8. 路线

- ✅ 本阶段（2026-08-14）：插件族落地（core/run/chat/macro/research/backtest）、
  配置选择机制、verify 脚本、bootstrap 升级、本文档。
- 📋 后续：`goai-terminal`（DSH client 面板）与 `goai-approval`
  （READY_FOR_CONFIRMATION 审批接管）——注册为插件时即自然纳入用户选择清单；
  Phase 2 的 freshness 事件、jobs 惯性状态机同样以插件形式叠加。
- 📋 可选演进：若未来要让 Python 引擎本身可插拔（模块注册路由），
  需另起一轮重构 `ui_server`，与本文档的 JS 层插件化正交。
