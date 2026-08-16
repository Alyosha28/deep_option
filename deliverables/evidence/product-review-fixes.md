# GOAI 产品评审修复清单（2026-08-14/15）

> 依据产品经理视角评审结论（使用流程断点 + 结构性缺陷），逐项修复并附验证证据。
> 状态同步见 docs/PRD.md §10.5 与 PROJECT_STATE.md §14。

## 一、结构性缺陷修复

| 评审问题 | 修复 | 验证证据 |
|---|---|---|
| ① 行动闭环断在决策卡：用户拿到结论后没有下一步 | 决策卡视图新增「下一步」动作区：导出决策卡（引擎侧 `GET /api/decision-card`，含 SHA-256 身份哈希）+ 修改条件重算（打开研究条件面板）+ 按 verdict 的行动文案；模拟提交边界诚实标注（P0c 未毕业） | 浏览器实测：导出提示「已导出决策卡（sha256 c6b4ae502322…）」；重算按钮正确展开条件面板；边界声明「P0a Replay 只读 · 模拟提交未启用」；测试 `test_api_decision_card_export_shape` |
| ② 冻结快照像实时盘：数据陈旧但界面是终端外观，信任错位 | 总览顶部能力声明条：当前切片（P0a · Replay 只读）/ 数据模式 + 快照时间戳（「快照时间 2026-08-08 11:56 · 非实时行情」）/ 支持范围 / 一键 Replay 按钮 | 浏览器实测渲染；截图 `e2e-overview.png` |
| ③ 产品无法度量自己：PRD §9.1 指标无采集手段 | 本地会话度量：`data/logs/session_metrics.jsonl`（ts/event/input/verdict/duration_ms/mode，线程安全追加，失败静默不打断主流程）+ `GET /api/metrics`（尾部条目 + byEvent/byVerdict/avgDurationMs 统计）+ 审计视图「会话度量」面板 + 总览「最近分析耗时」 | 端到端实测：POST /api/agent(refresh) 13.8s → metrics 记录 NO_TRADE/13729ms/REPLAY；测试 `test_api_metrics_records_and_returns`、`test_api_agent_action_records_session_metric` |

## 二、使用流程断点修复

| 环节 | 评审问题 | 修复 | 验证 |
|---|---|---|---|
| 复盘/可信度 | 审计链只在 JSONL 文件里，用户界面看不到 | 新增「审计」视图：全链哈希校验（`GET /api/audit` 引擎侧校验 prev_hash 衔接，`chainOk`）+ 事件明细（时间/事件/摘要/dropped_refs 被拒引用徽章/哈希前缀）+ 决策卡页审计链状态徽章 | 实测：95 条链完整；auditor 角色 8 条 dropped_refs 正确显示；截图 `e2e-audit.png`、`audit-metrics-view.png` |
| 换标的 | 新标的=手动录快照，无指引；失败提示不明确 | 错误消息含范围声明与录制指引；UI 表单常驻范围声明；新增 `docs/SNAPSHOT_RECORDING.md`（契约/两种录制路径/失败对照/范围边界） | 实测：添加 SSE.600519 → 422「没有找到…请先将该标的的冻结快照 JSON 放入 data/projects/（录制与格式见 docs/SNAPSHOT_RECORDING.md）；当前 P0 演示范围以 data/ 内已有快照为准」；测试 `test_register_project_reports_missing_snapshot_with_guidance` |
| 首进 | 新用户不知道能做什么/数据从哪来 | 能力声明条（见结构性②） | 截图 `e2e-overview.png` |

## 三、定位与文档

| 评审问题 | 修复 |
|---|---|
| PRD 漂移（四面板 vs 实际 7 视图、LLM 场景解析 vs 确定性解析器） | PRD 升级 v0.6：UI 壳结构、场景解析口径、DSH 插件族、实现真相表、§10.5 增量计划 |
| 对外主入口含糊 | README：独立模式（`python -m src.ui_server`）为主入口，DSH 插件族为编排亮点；「四面板」全部替换为 7 视图表述 |

## 四、验证汇总

- Python 全量测试：**418 passed**（2026-08-15，含新增 24 个：decision-card/audit/metrics 端点 + workspace 指引 + agent 度量集成）
- 插件族：`verify_plugins.ps1` PASSED、`smoke_plugins.mjs` PASSED
- 浏览器 e2e：8 视图 + 4 抽屉全部实测，console 0 错误，截图 14 张（`deliverables/evidence/screenshots/`）
- 铁律：JS/LLM 未重算任何数字（所有展示字段来自引擎 API）；审计链只增不减（audit 端点只读）；能力宣传不超过 P0a 切片（Live/模拟提交明确标注未毕业）
