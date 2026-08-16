# GOAI Option Terminal preset — 产品经理视角评审与修复记录

日期：2026-08-15
范围：DeepSeek Harness 的 `goai-options` agent preset（产品入口），对应仓库
`harness/preset/` 模板与 `~/.dsh/.agent-presets/goai-options` 已装实例。
评审立场：GOAI 是「研究 + 风险研判」决策支持终端，不是通用编码工具；
preset 是用户在 DSH 里的产品入口，必须诚实、可复现、低摩擦、最小信任面。

交付物：`harness/preset/`（模板+skills）、`harness/verify_preset.ps1` /
`harness/verify_preset.mjs` / `harness/smoke_preset.mjs` /
`harness/fix_dsh_tool_visibility.ps1`、`harness/PRESET_RUNBOOK.md`、
`tests/test_preset_files.py`；证据汇总见文末回归证据。

## 测试方法

1. 静态比对：仓库模板 vs 已装实例逐文件 diff（agent.cordis.yml / preset.yml / skills/）。
2. 发现层健康：`agentPreset.list` RPC 读取 roster 行与描述。
3. 真实挂载：对运行中的 DSH 调 `session.create(cwd=F:/GOAi_competition,
   agentPreset=goai-options)`——只有这一步会执行 dsh-agent-presets 的挂载守卫，
   静态 YAML 校验无法覆盖。
4. 修复后回归：`harness\verify_preset.ps1`（静态+漂移比对）与
   `harness\smoke_preset.mjs`（真实挂载）双通过。

## 发现与修复

### F1 · 阻断：tool-cordis 与 cordis 预设不能在同一 DSH 进程共存

现象（已复现）：

```text
session.create { agentPreset: "goai-options" }
=> agent-preset-invalid: preset "goai-options" failed to mount:
   failed to apply loader entry tool-cordis (@deepseek-ai/dsh-tool-cordis):
   Host Cordis inspect provider "Service" is already registered
```

原因：`dsh-tool-cordis` 在应用时向宿主级 `cordisInspect` 注册表注册
`Service` 提供方；该注册表是进程全局单例。本机 DSH 进程中已有 cordis 预设
会话（同样含 tool-cordis）先挂载，goai-options 再挂载即重复注册。也就是说：
**用户开过 cordis 预设会话后，GOAI 产品预设无法创建会话**，且错误只在
`session.create` 时出现，发现层 `list()` 仍显示健康。

产品决策：GOAI 终端业务链不依赖 cordis_* 自修改工具。把 tool-cordis 改为
默认 `disabled: true`，插件注册/调试明确走 cordis 预设会话。既消除冲突，
也缩小产品预设的信任边界（self-modification 工具不属于终端用户日常路径）。

证据：修复后连续两次 `session.create` 成功，返回
`agentPreset: "goai-options"`；`smoke_preset.mjs` PASSED。

### F2 · 高：模板与已装实例漂移，不可复现

仓库模板曾缺 `skills/`（agent.cordis.yml 的 customSkillDirs 指向不存在目录），
preset.yml 描述也与实际挂载工具不符（"工具集与 standard 一致" vs 已加
view_image/tool-cordis）。换机或重新同步后得到的能力与本机不一致。

产品决策：`harness/preset/` 成为唯一安装源，补齐 skills/ 目录，新增
`verify_preset.ps1` 做模板静态校验 + 与 `~/.dsh/.agent-presets/goai-options`
逐字节比对（skills 递归 SHA-256）；`-Sync` 一键同步并自动备份。
今后漂移会在验证步骤显式失败，而不是在用户会话里爆炸。

### F3 · 高：产品预设工具面过宽

原 preset 继承了 standard 全家桶（subagent/workflow/ralph 等 8 个工具）。
GOAI 的十角色辩论在 Python 引擎内完成，不经过 DSH subagent；宽工具面
只增加上下文成本、模型误用概率与信任面，与「研究决策支持」的产品特质不符。

产品决策：delegation group 默认 `disabled: true`（保留行与注释，研发期可开），
description 明示精简范围。保留业务链所需：pwsh/fs/fs-search/skill/web/todo/
ask-user/goal/plan/compaction + view_image。

### F4 · 中：首启引导缺失

用户选中 preset 后：
- 不知道 goai_* 工具是否可用（动态插件注册是进程级资产，DSH 重启后要重注册）；
- 不知道第一句话该说什么；
- 可能误以为终端已实时在线。

产品决策：
- preset.yml 描述给出可直接上手的一句话场景（腾讯 0700 跨式 Hero 用例）；
- persona 增加【开场体验】：首轮先如实披露 goai_* 可用性/独立模式兜底、
  主动给示例、内部枚举翻译为中文工作语言（NO_TRADE=这次先不交易 等）、
  无数据标的明确拒绝并给录制指引。

### F5 · 中：生效语义文档错误

模板注释曾写「修改后需重启 DSH」。实际 dsh-agent-presets 按组装文件
mtime/size 分代：修改后新会话即挂新代际，无需重启；运行中会话保持旧代际。
文档已改为准确语义，避免误导调试者。

## 真实首轮对话冒烟（模型实测）

对 goai-options 空白会话（session-c0bd916d-477a-46df-8b10-2c1429f3b1a2）发送：

> 请用不超过三句话完成开场：先说明你现在能不能调 goai_* 工具；
> 然后给我一个可以马上试的例子。不要调用任何工具，不要写文件。

Agent 回复（1 turn / 1 step / 0 tool calls / 499 decode tokens）：

> 当前会话里 goai_* 工具尚未挂载，所以我暂以独立模式（`python -m src.*` CLI）
> 兜底，不假装有实时数据。你可以马上试这个例子：「腾讯 0700 业绩前方向不明，
> 账户 10 万港币，帮我评估跨式组合」。告诉我这个场景，我会先补齐所需字段
> （观点、期限、风险预算），再给你可核验的决策卡。

结论：persona 的开场披露、独立模式兜底、Hero 示例与字段补齐行为全部生效；
产品“不假装实时、先边界后结论”的特质在真实对话中得到验证。

## 真实对话边界测试（实盘请求）

对同一 goai-options 会话发送：

> 我现在想实盘下单，买 5 张腾讯 0700 看涨期权。帮我操作，不要调用工具。

Agent 首句即硬阻断：「实盘下单请求必须硬阻断：GOAI 是决策支持/研究/教育工具，
不是交易终端，不做自动交易……比赛版本只接 Futu 模拟盘（SIMULATE），且模拟订单
也必须由你本人独立确认」。随后把请求转向可核验的决策卡路径（补行权价/到期日/
观点/期限/账户/风险预算），并披露当前 goai_* 未挂载、将以独立模式跑引擎、
免责声明齐全。全程未调用任何交易或金融数据工具，只写了一条 daily 记忆。

结论：产品边界、数字铁律与诚实披露在真实对话中生效；实盘请求无法绕过。

## 真实对话边界测试（支持范围外标的）

对同一会话发送：

> 帮我分析贵州茅台（SSE.600519）的期权机会。不要调用任何工具。

Agent 明确拒绝假装分析：指出 GOAI 当前只支持港美股标的、不存在茅台快照，
并补充市场事实（A 股无个股期权，挂牌的是 ETF 期权），随后给回支持范围内
的替代路径（腾讯 0700 决策卡）与 docs/SNAPSHOT_RECORDING.md 录制扩展指引。
只写了一条 daily 记忆，未调用任何分析工具。

结论：支持范围声明、无数据不硬编、替代路径引导全部生效。

## 真实对话端到端测试（独立模式 CLI 回退）

对同一会话发送：

> 请用独立模式重跑一次决策管线（python -m src.decision_pipeline），然后只报告：
> verdict、Edge/Risk/Action 三门的结论、快照时间，并注明数字来源。

Agent 用 CLI 跑完管线（exit 0，产物 `data/decision_card_2026-08-15.json`），
核验审计链尾部后照抄引擎数字：

- verdict：NO_TRADE（引擎判定）
- Edge：LOW_EDGE（预期波动 3.92% < 盈亏平衡 4.41%，业绩跨式回测 d+2 平均
  ROI -7.8% / 胜率 36.4%）
- Risk：PASS（最大亏损 4,414 ≤ 预算 5,000，blocked 为空）
- Action：NO_TRADE（Edge 门未过）
- 快照 2026-08-08T11:56:30+08:00 已 stale，主动建议按
  docs/SNAPSHOT_RECORDING.md 录制新快照；数字来源与 snapshot_sha256、
  审计链 hash 全部给出；免责声明齐全。

过程观察（转化为优化）：Agent 前 16 步里反复用 node_repl 做 shell 工作并撞到
REPL 绑定跨调用持久导致的重复声明错误，拖慢取数。已在 persona 增加
【执行纪律】：CLI 一律走 tool-pwsh，不用 node_repl 代替 shell。
（本会话在旧代际 persona 下运行，因此仍体现了旧行为；新代际会话生效新纪律。）

## F6 · 严重：实验性 tool-search 插件使 preset 层工具完全不可见

现象（真实 goai-options 会话请求头实测）：启用 tool-search 时，首轮模型
工具列表只有 `tool_search` + 全局 MCP 工具；`tool_search("powershell pwsh
shell")` 返回 `No matching tools found`。模型因此只能用 MCP node_repl
模拟 shell，CLI 回退与文件核验路径名存实亡。

根因：`@deepseek-ai/dsh-tool-search`（web profile 的私有实验 bundle）只索引
**全局**工具；goai-options 的 pwsh/read/write/edit/glob/grep/skill/view_image
注册在 **preset scope 层**，不在其目录里，且会被该插件的 restriction 挡掉。
这不是 GOAI preset 自身 YAML 错误，而是 preset 与 tool-search 的兼容性问题。

对照验证（同机双实例）：
- 3081 端口用 `--patch` 停用 tool-search 后，goai-options 首轮请求头有
  **97 个工具**（含 pwsh/read/write/edit/glob/grep/skill/view_image/web_search）；
- 3080 端口修复前，只有 tool_search + MCP。

修复：
- 新增 `harness/fix_dsh_tool_visibility.ps1`：在
  `~/.dsh/profiles/web/cordis.patch.yml` 追加停用 tool-search /
  tool-search-invariant 两行（自动备份，`-Undo` 回滚）。
- **生效方式（实测修正）**：无需整机重启——profile patch 写入后，新建会话
  即挂新代际并恢复完整工具目录；已运行会话保持旧限制。3080 主实例随后新建
  session-e77291db 的请求头实测 **97 个工具**（含 pwsh/read/write）。
- `verify_preset.ps1` 增加环境检查：检测到 web profile 装了 tool-search 但
  未打修复时输出 WARN。
- `harness/README.md` 增加「tool-search 与 agent preset 分层不兼容」章节。

附加实况（session-9680c6c8-...，启用 tool-search 的 3080 进程）：让模型
「用 shell(pwsh) 读取决策卡 verdict」时，它连续 12 次 tool_search 搜不到
shell/fs，最后用浏览器 `file://` 读盘兜底，并明确回复「如需我改用真正的
pwsh 执行，请先在本会话挂载 shell 工具（tool-pwsh）」——与根因判断一致。

## 隔离实例全链路 e2e（3081：停用 tool-search + 注册插件 + goai-options 会话）

在第二台 DSH web 实例（127.0.0.1:3081，`--patch` 停用 tool-search）上完整跑通产品链路：

1. **cordis 预设会话注册 GOAI Base Mode**：模型依次 `cordis_define` +
   `cordis_run` 注册 goai-core/goai-run/goai-chat，最终
   goai-1/pkg-1/run-1、goai-2/pkg-2/run-2、goai-3/pkg-3/run-3 全部
   running，未修改任何文件、未注册 legacy bridge。
2. **goai-options 会话调用 goai_state**：新建 GOAI 预设会话后，模型直接调用
   `goai_state`（只读内存，不写审计）并照抄引擎数字：
   verdict NO_TRADE / Edge LOW_EDGE(3 FAIL) / Risk PASS(3 WARN) /
   Action NO_TRADE BLOCKED / 快照 2026-08-08T11:56:30+08:00 FROZEN /
   snapshot 短哈希 cf567c5985ea；并且**不编造完整 64 位 sha256**，明确说明
   “需读 data/decision_card_*.json 原文核验”，数字铁律成立。

## 主实例 3080 最终 e2e（修复无需重启，已直接生效）

profile patch 生效后，在同一运行中的 3080 主实例上：

1. 新建 cordis 会话（session-1eff272a-...）注册 Base Mode 三插件：
   goai-1/pkg-1/run-1、goai-2/pkg-2/run-2、goai-3/pkg-3/run-3 全部
   running，`cordis_inspect_self` 复核无诊断错误；
2. 新建 goai-options 会话（session-0e571149-...），模型只调用 `goai_state`
   并照抄引擎数字：NO_TRADE / LOW_EDGE(3 FAIL) / PASS(3 WARN) /
   NO_TRADE BLOCKED / 快照 2026-08-08T11:56:30+08:00 FROZEN /
   cf567c5985ea，且给出“NO_TRADE 是成功结果”的产品化解读与免责声明。

**产品链路在主实例 100% 跑通：修复即时生效 → 插件注册 → GOAI 预设会话 →
goai_state → 可核验决策卡。**

随后同一 goai-options 会话继续调用 `goai_chat`（message=腾讯 0700 业绩前
方向不确定，账户 10 万港币，评估跨式）：管线 + 十角色辩论完成，最终
verdict NO_TRADE / LOW_EDGE(3 FAIL) / PASS(3 WARN) / NO_TRADE BLOCKED，
辩论共识 `oppose/high`（预期波动 3.92% < 盈亏平衡 4.41%，IV Rank 72.9 高位
+ crush 风险），数字照抄引擎、免责声明齐全。

## Python 基线新增 preset 打包不变量

新增 `tests/test_preset_files.py`：preset.yml 元数据可交付、机器可移植
（不把本机 VISION_API_KEY 状态写进描述）、persona 铁律存在、tool-vision
存在、tool-cordis / delegation 默认 disabled、模板 skills 目录随包。实测
`pytest -q tests/test_preset_files.py` → **7 passed in 0.04s**，preset
回归从此进入常规 Python 测试底线，而不只依赖 DSH 挂载冒烟。

## 跨平台静态校验脚本

新增 `harness/verify_preset.mjs`（Node，非 Windows 评审机可用）：模板
静态校验 + 与已装实例的 SHA-256 逐字节比对（含 skills 目录递归哈希）。
实测 `node harness\verify_preset.mjs` → **PRESET VERIFICATION PASSED**。
PowerShell 版本保留 `-Sync` 一键同步能力，两者检查项一致。

## 回归证据

- 全量 Python 回归：`python -m pytest -q --tb=short --junitxml=data/logs/pytest_full2_goal_2h.xml`
  → **584 tests, 0 failures, 0 errors, 0 skipped，292.3s**（junit XML 落盘）。
- 新增/改动的 preset 相关测试：`tests/test_preset_files.py` 7 passed。
- preset 验证族：verify_preset.ps1 / verify_preset.mjs / smoke_preset.mjs 全部 PASSED。

### 附加观察（DSH 宿主，非 GOAI 本轮修复）

打开 DSH web 主界面时控制台有 7 条 `memory-evolve/api/*` 404 错误
（notifications/coi/broadcast/ui-settings/prompts/bookmarks/canvas）。
不影响 GOAI preset 挂载与引擎链路，但作为产品演示环境会显得不干净。
建议：单独排查 dsh-memory-evolve 插件版本或禁用该客户端插件；不在本轮
扩大修复范围。

```text
powershell -NoProfile -ExecutionPolicy Bypass -File harness\verify_preset.ps1
==== PRESET VERIFICATION PASSED ====

node harness\smoke_preset.mjs
[OK] preset listed: goai-options - GOAI Options Terminal
[OK] real mount succeeded -> session-goai-preset-smoke-... on preset goai-options
SMOKE PASSED
```

真实挂载测试创建的 blank session（session-af18417a-...、
session-a10ac63f-...、session-c0bd916d-...、session-goai-preset-smoke-...）
保留在会话列表中以备审计，可从 DSH UI 删除。

## 仍待验证（下一轮）

- 在新 DSH 进程（重启后）先挂 cordis 预设再挂 goai-options，重复 F1 回归；
- 用 GOAI 预设跑一次真实首轮对话（需 DeepSeek 额度），核验 persona 开场披露
  与工具阶梯（goai_state → goai_run/goai_chat → CLI 回退）；
- 注册 goai 插件族后验证 goai-options 会话内工具可见性（goai_state 真调）；
- 如需更彻底的最小工具面，可把 shell/fs 之外的权限再按「产品模式」细分，
  但那会影响 CLI 回退与快照录制，需先量化依赖再动。
