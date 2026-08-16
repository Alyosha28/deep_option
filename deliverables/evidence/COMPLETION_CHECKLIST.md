# GOAI Option Terminal preset — 目标完成检查清单（2026-08-15）

按 objective 逐项核验；全部通过后允许调用 `goal.complete`。

## 1. 时间纪律

- [x] `data/logs/goal_2h_start.txt` 记录起算时刻 2026-08-15T09:47:26.920Z。
- [x] 每轮开头检查墙钟并写入 `data/logs/goal_2h_progress.jsonl`（截至完成共 29 条）。
- [x] 满 2 小时（120 min）——2026-08-15T11:47:43Z 达到 120.28 min。

## 2. preset 调试研发目标

- [x] 复现并修复 tool-cordis 与 cordis 预设共享 cordisInspect 注册表冲突
      （product preset 默认 disabled）。
- [x] 修复模板与已装实例漂移（skills/ 补齐；verify_preset.ps1/mjs 逐字节比对）。
- [x] 精简产品工具面（delegation group 默认 disabled）。
- [x] persona 增加开场披露、Hero 示例、中文工作语言、无数据拒绝、
      CLI 用 pwsh 执行纪律、tool-search 回退纪律。
- [x] 修复实验性 tool-search 与 agent preset 分层不兼容
      （fix_dsh_tool_visibility.ps1；新建会话即生效，无需重启）。
- [x] 主实例 3080 全链路 e2e：97 工具可见 → cordis 注册 goai-1/2/3 running →
      goai-options 会话 goai_state / goai_chat 照抄引擎数字。
- [x] 真实对话四连测：开场披露 / 实盘硬阻断 / 范围外标的拒绝 / CLI 回退。

## 3. 铁律

- [x] 数字铁律：所有对话数字照抄引擎/决策卡；模型未编造完整 sha256。
- [x] 模拟盘与人机确认：实盘请求被硬阻断，只说明 Futu SIMULATE 边界。
- [x] 审计链只增不减：audit_log.jsonl 133 事件、prev_hash 断链 0。
- [x] Python 测试底线：全量 584 tests / 0 failures / 0 errors / 0 skipped
      （292.3s，junit 落盘 data/logs/pytest_full2_goal_2h.xml）。
- [x] DSH 编排层约束：JS 不重算数字、src/ 未改动、插件族 verify/smoke 全绿。

## 4. 交付物

- [x] `harness/preset/`（agent.cordis.yml + preset.yml + skills/）
- [x] `harness/verify_preset.ps1` / `harness/verify_preset.mjs` / `harness/smoke_preset.mjs`
- [x] `harness/fix_dsh_tool_visibility.ps1`
- [x] `harness/PRESET_RUNBOOK.md`
- [x] `tests/test_preset_files.py`
- [x] `deliverables/evidence/preset-pm-review.md`
- [x] 文档同步：README / PRODUCT / PRD v0.7 / DSH_ARCHITECTURE /
      PLUGIN_ARCHITECTURE / HISTORY / demo-script / futu-options-agent SKILL。

## 5. 完成动作

- [x] 墙钟 ≥ 120 min 后，`goal.complete`（ref revision 4 → 5，phase=complete）。
- [x] 最终把完成事件追加到 `goal_2h_progress.jsonl`。
