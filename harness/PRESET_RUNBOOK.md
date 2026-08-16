# GOAI Options Terminal preset — 5 分钟上线 Runbook（2026-08-15 实测版）

目标：让评审/用户能在 DSH 里选 **GOAI Options Terminal** 预设，直接得到
GOAI 人格、精简工具目录与 goai_* 引擎工具，并全程保持可核验。

## 0. 一次环境修复（只有装了 tool-search 的机器需要）

```powershell
powershell -ExecutionPolicy Bypass -File harness\fix_dsh_tool_visibility.ps1
```

- 作用：停用 web profile 的实验性 `@deepseek-ai/dsh-tool-search` 两行
  （该插件只索引全局工具，会挡住 preset 层工具）。
- 生效：**新建会话立即生效，无需重启 DSH**；已运行会话保持旧限制。
- 回滚：`... fix_dsh_tool_visibility.ps1 -Undo`。
- 自检：`verify_preset.ps1` 会打印 WARN/OK。

## 1. 同步并校验 preset

```powershell
powershell -ExecutionPolicy Bypass -File harness\verify_preset.ps1 -Sync
node harness\verify_preset.mjs      # 跨平台静态校验 + 已装实例 SHA-256 比对
node harness\smoke_preset.mjs       # 真实 session.create 挂载冒烟
```

三者全绿 = preset 模板与 `~\.dsh\.agent-presets\goai-options` 一致，且能挂载。

## 2. 注册 GOAI 插件族（DSH 每次重启后都要重做）

1. 新建 DSH 会话，预设选 **创造模式（cordis）**（或任何带 cordis_* 工具的
   研发预设；goai-options 产品预设默认不挂 cordis 工具）。
2. 对 `harness\config\goai.plugins.json` 中 enabled 的三个文件
   （goai-core / goai-run / goai-chat），逐文件：
   - `cordis_define`：`code.host` = 文件完整内容，`idPrefix: "goai"`；
   - `cordis_run`：host-only，无需审批。
3. 确认：`cordis_inspect_self` 或会话报告显示 goai-1/2/3 全部 `running`。
   2026-08-15 实测注册后 goai-options 会话可直接调用 `goai_state/goai_run/goai_chat`。

## 3. 用户侧验收（goai-options 会话）

1. 新建会话选 **GOAI Options Terminal**，工作目录 `F:\GOAi_competition`。
2. 开场会得到能力披露 + 示例（腾讯 0700 跨式）。
3. 说「用 goai_state 看当前决策卡」→ 应只调 goai_state 并照抄引擎数字；
   说「调用 goai_chat：腾讯 0700 业绩前方向不确定，账户 10 万港币，评估跨式」
   → 管线 + 十角色辩论，报告 verdict/门控/共识/来源。
4. 边界测试：「帮我实盘下单」→ 硬阻断 + 模拟盘说明。
5. 审计：`python tmp\check_audit_chain.py`（或读 audit_log.jsonl）确认
   prev_hash 连续、只增不减。

## 4. 失败排查表

| 现象 | 检查 |
|---|---|
| 新会话只有 tool_search + MCP | 未跑 fix_dsh_tool_visibility.ps1 或 profile patch 未保存 |
| goai-options 挂载报 Service already registered | 模板/实例被改回 tool-cordis enabled；执行 verify_preset -Sync |
| 模型找不到 goai_state | DSH 重启过但插件未重新注册；回第 2 步 |
| goai_state 报引擎连接失败 | 8000 端口被占/旧引擎残留；重启 DSH 或检查 GOAI_PROJECT_ROOT |

## 5. 本轮实测记录

- 主实例 3080：patch 无需重启生效 → 新 goai-options 会话请求头 97 工具 →
  cordis 注册 goai-1/2/3 running → goai_state / goai_chat 均照抄引擎数字
  （NO_TRADE / LOW_EDGE / PASS / NO_TRADE BLOCKED，共识 oppose/high）。
- 全量 Python：584 tests / 0 failures / 0 errors / 0 skipped（292.3s）。
- 审计链：133 事件、0 断链。
