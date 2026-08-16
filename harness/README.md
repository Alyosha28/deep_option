# harness/ — DSH 编排层（GOAI 插件族）

GOAI 的 DeepSeek Harness 编排层。**Python 引擎在 `src/`（不动），这里只放把引擎接进
DSH 的 Cordis 插件族与运维脚本。** DSH 底层是 Cordis 内核，每个 `goai-*` 插件都是
独立可注册的 Cordis 插件；Base Mode = `goai-core` + `goai-run` + `goai-chat`
（保证基本使用，等价旧单体 goai-bridge 的三工具），其余模块做成可选插件，
用户在 `harness/config/goai.plugins.json` 勾选加载哪些。权威文档见
[docs/PLUGIN_ARCHITECTURE.md](../docs/PLUGIN_ARCHITECTURE.md)。

## 目录

| 文件 | 用途 |
|---|---|
| `plugins/goai-core.host.js` | **Base 必备**：引擎生命周期（懒启动/健康检查/可逆回收）+ HTTP 桥 + `goai_state` |
| `plugins/goai-run.host.js` | **Base 默认**：重跑五阶段管线（`goai_run`） |
| `plugins/goai-chat.host.js` | **Base 默认**：对话链路 + 十角色辩论（`goai_chat`） |
| `plugins/goai-macro.host.js` | 可选：政策事件库 + 宏观来源监控（`goai_policy_library` / `goai_macro_watch`） |
| `plugins/goai-research.host.js` | 可选：投研证据包 + 新闻适配（`goai_research_evidence` / `goai_research_sources`） |
| `plugins/goai-backtest.host.js` | 可选：腾讯业绩跨式回测（`goai_backtest`） |
| `plugins/goai-bridge.host.js` | **LEGACY**：Phase 0 单体版，兼容回退；与插件族二选一 |
| `config/goai.plugins.json` | 用户选择清单：每个插件的 enabled / 文件 / 工具 / 描述 |
| `verify_plugins.ps1` | 配置 + node 语法 + Cordis 形状 + base 完整性校验 |
| `smoke_plugins.mjs` | 免 DSH 冒烟：mock ctx/harness，真 curl + 真引擎跑插件逻辑（只读默认） |
| `bootstrap.ps1` | 环境自检 + 独立启动 + 按配置输出 DSH 注册指引（纯 ASCII） |
| `preset/` | `goai-options` agent preset 模板（agent.cordis.yml + preset.yml + skills/） |
| `verify_preset.ps1` | preset 静态校验 + 与已装实例逐字节比对；`-Sync` 一键同步到 `~\.dsh\.agent-presets` |
| `verify_preset.mjs` | 跨平台同款静态校验 + SHA-256 漂移比对（评审机/非 Windows 用 `node harness\verify_preset.mjs`） |
| `PRESET_RUNBOOK.md` | 5 分钟上线手册：环境修复 → 同步校验 → 注册插件 → 用户验收 → 排查表 |
| `smoke_preset.mjs` | 真实挂载冒烟：对运行中的 DSH 调 `session.create(goai-options)`，能抓住静态校验看不见的工具注册冲突 |
| `fix_dsh_tool_visibility.ps1` | 停用 DSH web profile 的 tool-search 实验插件（否则 preset 层工具对模型不可见）；`-Undo` 回滚 |

## 插件注册（DSH 会话内）

1. 编辑 `harness/config/goai.plugins.json` 勾选要加载的插件（Base Mode 三件套默认开启）；
2. 运行 `verify_plugins.ps1` 校验；
3. 运行 `bootstrap.ps1` 获得注册指引，把指引交给 DSH 会话助手：
   对每个启用插件，助手读取 `plugins/<name>.host.js` 内容作为 `cordis_define` 的
   `code.host` 注册（`idPrefix: "goai"`），然后 `cordis_run`（host-only，无需审批）；
4. 会话中直接说「goai_state」验证：首次调用自动拉起
   `.venv\Scripts\python.exe -m src.ui_server --port 8000`；
   换机部署时先在 DSH 会话进程环境里设置 `GOAI_PROJECT_ROOT=<仓库路径>`；
5. 停用/更新插件会自动回收其托管的引擎子进程（`ctx.effect` 可逆效应）；
   已在运行的 8000 端口引擎会被复用且不被误杀。

## goai-options agent preset（产品入口）

模板即安装源：`preset/agent.cordis.yml` + `preset/preset.yml` + `preset/skills/`。
同步到本机 DSH 用户 preset 根并校验：

```powershell
powershell -ExecutionPolicy Bypass -File harness\verify_preset.ps1 -Sync
node harness\smoke_preset.mjs   # 需要 DSH web 正在运行（默认 127.0.0.1:3080）
```

产品安全默认（2026-08-15 PM 评审后固化）：

- **tool-cordis 默认 disabled**。它与 cordis/standard 等预设共享宿主 `cordisInspect`
  注册表；同一 DSH 进程内任一含 tool-cordis 的预设先挂载后，后挂载者会在
  `session.create` 时报 `Host Cordis inspect provider "Service" is already
  registered`。产品会话不需要 cordis_* 自修改工具，插件注册/调试请用 cordis
  预设会话（bootstrap 指引），注册后 goai-options 会话即可调用 goai_* 工具。
- **delegation group 默认 disabled**：不挂 subagent/workflow/ralph。终端业务链
  不依赖它们（十角色辩论在 Python 引擎内完成），换来更小上下文与更小信任面。
- 保留 standard 的 pwsh/fs/fs-search/skill/web/todo/ask-user/goal/plan/compaction，
  并加 view_image（默认智谱 glm-4.6v-flash 视觉）。

推荐使用顺序：

1. `verify_preset.ps1 -Sync` 同步 preset 模板到本机；
2. 按上文「插件注册」用 **cordis 预设**会话注册 goai 插件（product preset 不自修改）；
3. 新建会话选 **GOAI Options Terminal**，直接说
   「腾讯 0700 业绩前方向不明，账户 10 万港币，评估跨式」；
4. 若插件未注册，Agent 按 persona 自动回退独立模式
   `python -m src.*` CLI，并把当前能力边界如实告知用户。

修改 preset 的生效语义：文件按 mtime/size 分代，修改后**新会话**即挂新代际、
无需重启 DSH；已运行会话保持其开始时的代际；DSH 重启后全部从磁盘重挂。

## 独立模式（评审机器不装 DSH）

```powershell
python -m src.ui_server --port 8000     # 四面板终端 + JSON API
python -m src.decision_pipeline         # 单次管线（写审计与决策卡）
```

两条入口共享同一引擎契约，DSH 层只透传数字、不重算。

## 已知限制：tool-search 与 agent preset 分层不兼容

本机 DSH web profile 装了实验插件 `@deepseek-ai/dsh-tool-search`。它只索引
**全局**工具；而 agent preset 的 pwsh/read/write/edit/glob/grep/skill/
view_image 等注册在 **preset scope 层**，于是模型只看到 `tool_search` +
全局 MCP 工具，搜索「pwsh」会得到 `No matching tools found`——GOAI 预设的
CLI 回退因此无法按文档执行。实测对照：同一 goai-options 预设，停用
tool-search 后首轮请求头里有 97 个工具（含 pwsh/read/write/view_image），
启用时只有 tool_search + MCP。

修复（已做进运维脚本）：

```powershell
powershell -ExecutionPolicy Bypass -File harness\fix_dsh_tool_visibility.ps1   # 应用（自动备份 profile patch）
powershell -ExecutionPolicy Bypass -File harness\fix_dsh_tool_visibility.ps1 -Undo  # 回滚
```

`verify_preset.ps1` 会检测「装了 tool-search 但没打修复」并输出 WARN。
**生效方式（2026-08-15 实测）**：写入 profile patch 后新建会话即恢复完整
工具目录，无需重启 DSH；已运行会话保持旧限制。重启 DSH 仍是确定性兜底。
用 `node harness\smoke_preset.mjs` 验证挂载，再开 goai-options 会话即可。

2026-08-15 主实例 3080 已全链路验证：patch 生效 → cordis 预设注册
goai-core/goai-run/goai-chat（goai-1/2/3 全 running）→ goai-options 会话
直接调 `goai_state` 并照抄引擎数字（不编造完整 sha256）。DSH 重启后需重新
注册插件族（进程级资产）。

## 已知限制

- 动态插件是进程级资产：DSH 重启后必须按 bootstrap 指引重新注册；
- 客户端插件包激活需要审批，本会话审批策略为 never 时会被自动拒绝；
- 插件内 `root` 已参数化：优先读环境变量 `GOAI_PROJECT_ROOT`（换机部署前在启动
  DSH 的会话进程里设置），未设置时回退 `F:\GOAi_competition` 并打印提示；
- 所有插件共用同一引擎进程（127.0.0.1:8000）；并发首调且引擎未起时，
  后 spawn 者自动转为复用（见 docs/PLUGIN_ARCHITECTURE.md §5）。
