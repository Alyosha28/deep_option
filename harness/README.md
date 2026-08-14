# harness/ — DSH 编排层（大号金融插件的插槽）

GOAI 的 DeepSeek Harness 编排层。**Python 引擎在 `src/`（不动），这里只放把引擎接进 DSH 的 JS 插件与运维脚本。**

## 目录

| 文件 | 用途 |
|---|---|
| `plugins/goai-bridge.host.js` | host 插件规范源码：注册 `goai_state` / `goai_run` / `goai_chat`，生命周期托管引擎进程（可逆效应） |
| `bootstrap.ps1` | 环境自检 + 独立模式启动 + DSH 注册指引（一条命令恢复演示环境；纯 ASCII，防 PowerShell 5.1 编码坑） |
| `preset/` | `goai-options` agent preset 组合模板（GOAI persona + 项目铁律；已在 DSH 用户 preset 根安装并 mount-validate 通过） |

## 插件注册（DSH 会话内，Phase 1c 前的手工步骤）

DSH 重启后动态插件失效，按以下步骤重新注册（约 1 分钟）：

1. 在 DSH 会话中让助手执行：把 `plugins/goai-bridge.host.js` 的内容作为
   `cordis_define` 的 `code.host` 注册（新插件 `idPrefix: "goai"`），然后
   `cordis_run`（host-only，无需审批）；
2. 会话中直接说「goai_state」即可验证：首次调用自动拉起
   `.venv\Scripts\python.exe -m src.ui_server --port 8000`；
   换机部署时先在 DSH 会话进程环境里设置 `GOAI_PROJECT_ROOT=<仓库路径>`（否则
   插件回退默认路径并打印提示）；
3. 停用/更新插件会自动回收引擎子进程（`ctx.effect` 可逆效应）；已在运行的
   8000 端口引擎会被复用且不被误杀。

## 独立模式（评审机器不装 DSH）

```powershell
python -m src.ui_server --port 8000     # 四面板终端 + JSON API
python -m src.decision_pipeline         # 单次管线（写审计与决策卡）
```

两条入口共享同一引擎契约，DSH 层只透传数字、不重算。

## 阶段路线

- Phase 0（已完成）：goai-bridge 三工具真机验证（审计链 82 行、340 tests）
- Phase 1a（待审批可用）：client 插件——DSH 内决策卡面板 + 预警 dock + approval 接管
  `READY_FOR_CONFIRMATION`（激活需用户授权：单勾=当前版本，双勾=后续版本）
- Phase 1b（已完成）：`goai-options` agent preset（GOAI persona + 铁律，已 mount-validate；
  新会话在预设选择器里选「GOAI Options Terminal」即可用；工具集与 standard 一致，
  goai_* 工具仍需本会话注册 bridge 插件）
- Phase 1c（已完成）：本目录 + bootstrap 脚本
- Phase 2：freshness 失效传播事件、jobs 惯性状态机、十角色辩论 subagent 化（可选）

## 已知限制

- 动态插件是进程级资产：DSH 重启后必须重新注册（上面的手工步骤）；
- 客户端插件包激活需要审批，本会话审批策略为 never 时会被自动拒绝——跑 Phase 1a
  前先把会话审批切回 ask；
- 插件内 `root` 已参数化：优先读环境变量 `GOAI_PROJECT_ROOT`（换机部署前在启动
  DSH 的会话进程里设置），未设置时回退 `F:\GOAi_competition` 并打印提示。
