# 初赛证据包（应对规则红线 §11）

> 红线原文：「仅提交概念说明、PPT 或营销材料，无法提供 PoC、实验、仿真、日志、视频、平台成绩或等价可验证材料，原则上淘汰或严重扣分。」
> 对策：提交材料时附上本清单列出的可验证材料，证明项目不只是 PPT。

## 可提交的证据（全部现成，无需新造）

| # | 材料 | 位置 | 证明什么 |
|---|---|---|---|
| 1 | 四面板终端截图（桌面/移动） | `ui/preview.png`、`ui/preview_mobile.png` | 产品形态真实存在 |
| 2 | 决策卡（2026-08-13，NO_TRADE） | `data/decision_card_2026-08-13.json` | 五阶段管线真实产出，数字可追溯 |
| 3 | 审计链样例（SHA-256 哈希连续） | `research/audit/audit_log.jsonl`（83 行，35 agent_output + 2 debate_consensus） | 审计与可追溯（合规维度 10%） |
| 4 | 十角色辩论真机记录 | 审计链内 `agent_output:<role>` 事件 + `PROJECT_STATE.md` §11/`docs/HISTORY.md` §11 | 多 Agent 能力真实跑通（任务闭环 25%） |
| 5 | 测试统计 | `pip install -r requirements.txt` 后 `python -m pytest tests -q` → 584 tests / 0 failures（292.3s，pytest 9.0.3，2026-08-15 全量回归） | 工程可信度（技术深度 15%） |
| 6 | 代码仓库 | https://github.com/Alyosha28/GOAi_competition（public） | 开放/复用（5%）+ 红线防御 |
| 7 | DSH 编排层运行记录 | `docs/DSH_ARCHITECTURE.md` + `docs/HISTORY.md` §12 + `harness/README.md` | Agent 编排层架构与验证 |
| 8 | 产品评审修复清单 | `deliverables/evidence/product-review-fixes.md` | 产品闭环/可信度修复闭环（问题→修复→证据） |
| 9 | 演示脚本 | `deliverables/demo-script.md` | 主 Demo 0–120s 逐段叙事（含新能力） |
| 10 | 终端 e2e 截图（8 视图 + 4 抽屉 + 审计/度量） | `deliverables/evidence/screenshots/`（13 张） | 全链路真实可跑 |
| 11 | 会话度量样例 | `data/logs/session_metrics.jsonl`（本地） | 产品指标可度量（PRD §9.1） |

## 提交时的组装建议

1. 截图（#1/#10）直接贴进 PPT 附页或作为独立图片附件；
2. 决策卡（#2）取 `summary` / `edge_gate` / `risk_gate` / `action_gate` 字段截取为
   一页 PDF（完整文件含本地路径，不整份外发）；
3. 审计链（#3）只取最后 3 条记录截图（哈希链连续性一眼可见；完整日志不入公开仓库，
   但可提供给组委会核验）；审计视图截图见 `screenshots/e2e-audit.png`；
4. 测试统计（#5）附运行命令 + 结果截图（当前 584 tests 全绿）；
5. 仓库链接（#6）填在「可执行代码包（可选）」栏；
6. 评审修复（#8）可在 PPT「架构/产品」页引用：能力声明首屏、决策卡下一步闭环、
   审计视图、会话度量。

## 边界提醒

- 证据里的数字口径与 PPT/facts.md 必须一致（以 `presentation/facts.md` 为权威）；
- 不要提交 `data/hero_inputs.json`（含富途授权行情快照）与完整审计日志——
  按 #2/#3 的方式截取即可；
- 旧模拟单（2026-08-08，legacy）不作为任何证据。
