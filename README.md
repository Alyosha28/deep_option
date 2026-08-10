# GOAI 港美股期权智能终端

GOAI 世界人工智能开源大赛 · Boundless Agents 金融服务方向项目。

GOAI 面向有基础期权认知的用户，把自然语言观点转成带数据来源、港股产品规则、确定性定价、账户风控和审计记录的决策卡。产品定位是研究与决策支持，不是投资建议，不提供实盘自动交易。

## MVP

P0 聚焦一个可验证场景：

> 腾讯 `HK.00700` 业绩前方向不确定，账户 10 万港币，评估长跨式是否值得交易。

系统目标输出四种产品结果：

- `NO_TRADE`：当前成本和证据不支持交易；
- `BLOCK`：数据、规格、账户或风险硬门未通过；
- `DRAFT_ONLY`：可研究和生成草稿，但不能提交；
- `READY_FOR_CONFIRMATION`：客观门通过，可由用户确认 Futu 模拟方案。

`NO_TRADE` 是成功结果。不会为了展示下单而调低门槛。

## 当前实现状态

已实现的原型资产：

- Futu 期权到期日、期权链、代码解析、快照和订阅适配；
- Snapshot Recorder 与 Replay Adapter 骨架；
- Black–Scholes、通用美式二叉树、IV 求解和 bump-and-reprice Greeks；
- 腾讯跨式分析与历史回测原型；
- JSONL + SHA-256 审计工具；
- 项目级 `futu-options-agent` 工作流。

仍在建设：可运行的对话 Agent、四面板 UI、港股离散股息与 executable-cost 完整实现、独立 Edge/Risk/Action gates，以及当前版本的模拟提交安全闭环。

完整产品边界和验收标准见 [精简版 PRD](docs/PRD.md)。

## 核心原则

1. LLM 只做场景解析、工具编排和解释，不生成金融数字。
2. 行情和账户事实来自 Futu 或明确标记的 Replay；计算来自确定性引擎。
3. 理论价值与 bid/ask、费用、滑点后的可成交口径分开。
4. 风险硬门一票否决；`PASS` 不代表盈利、成交或投资建议。
5. 比赛版本无实盘入口；模拟动作也必须由用户独立确认。

## 项目结构

```text
src/                                数据适配、回放、定价与 Hero 原型
research/                           市场、数据、边界和专家方法研究
docs/PRD.md                         产品需求与比赛验收
.agents/skills/futu-options-agent/  项目特化期权 Agent 工作流
```

授权行情、账户数据、订单回执和本地审计日志不进入公开仓库。

## 快速开始

环境：Windows、Python 3.13；Live 数据能力另需本地 Futu OpenD。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python research\kb_search.py 流动性
.\.venv\Scripts\python research\kb_search.py "IV crush" --tag earnings
```

当前公开快速开始仅覆盖知识库检索，因为完整 Agent/UI 尚未形成可验证入口。需要 Live 或 Replay Hero 数据时，请在本地按项目 schema 提供有授权的数据，不要把 Futu 账户行情重新分发到公开仓库。

## 自研引擎边界

项目不声称发明 Black–Scholes 或二叉树。自研工作集中在：

- 港股产品规格解析与模型路由；
- IV、Greeks 和情景损益的可复算实现；
- bid/ask、费用、滑点和 tick 的 executable-cost 口径；
- Edge、账户风险和动作门控；
- 数据、计算与决策审计。

当前引擎仍是原型，不能用于真实资金决策。

## 数据与安全

- 不提交 `.env`、密钥、交易密码、账户号或订单号。
- 不公开分发账户授权行情和原始 Futu 快照。
- 外部文本按不可信数据处理，不能修改数值、风控或权限。
- 任何模拟提交功能都必须在独立确认和提交前复核之后启用。
- 本项目仅用于研究、教育和比赛演示，不构成投资建议。

## License status

仓库目前公开用于团队协作和比赛审阅，尚未选定开源许可证；公开不等于授予复制、修改或再分发许可。详见 [NOTICE.md](NOTICE.md)。
