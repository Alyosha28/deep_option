# Hero 用例：腾讯 0700.HK 业绩跨式（straddle）

目标：业绩发布前方向不确定，10 万港币账户构建跨式期权，输出决策支持方案（默认模拟盘，不实盘）。

## 数据清单（每项都带快照时间戳 + 来源）

1. 事件日：`get_earnings_calendar(HK.00700)` → 业绩日期
2. 到期日：`get_option_expiration_date(HK.00700)`
3. 期权链：`get_option_chain(HK.00700, expiry)` → 行权价 / 报价 / IV / OI
4. 代码解析：`resolve_option_code`（港股期权代码勿手拼）
5. 波动率环境：`get_option_underlying_his_volatility` / `get_option_volatility` → RV、IV Rank、IV crush 参考
6. 正股与账户：`get_snapshot(HK.00700)`；`get_accounts` / `get_portfolio`（模拟盘）
7. 异动与事件上下文：`futu-derivatives-anomaly` + `futu-news-search` / `futu-stock-digest`

## 流程

1. 场景解析 JSON：`{"underlying":"HK.00700","view":"uncertain","horizon":"event date","account":100000,"currency":"HKD","risk_budget_pct":5}`
2. 取链数据并落快照（演示：快照优先，实时加分）
3. 自研引擎计算 ATM Call/Put 的 IV、Greeks（bump-and-reprice）、到期情景损益（含 ±IV crush）
4. 张数约束：权利金占用 ≤ 可用现金；完整跨式最大亏损 ≤ 5% 净值；合约乘数必须按当前 HKEX 合约规格页复核
5. 风险审计：pin risk / assignment（美式 + 实物交割）、IV crush 20–50%、流动性（买卖价差 / OI / 报价新鲜度）
6. 输出方案表 + `scripts/audit_log.py` 留痕 + 人机确认（默认模拟盘）

## 输出模板

| 腿 | 合约 | 方向 | 张数 | 权利金 | Delta | Gamma | Vega | Theta | 最大亏损 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0700 近月 ATM Call | 买入 | ? | ? | ? | ? | ? | ? | 全部权利金 |
| 2 | 0700 近月 ATM Put | 买入 | ? | ? | ? | ? | ? | ? | 全部权利金 |

## 港美股差异提醒

- HK 0700 个股期权 = 美式 + 实物交割；指数期权 = 欧式 + 现金交割
- 美股期权 = 美式 + 实物交割（多数），1 合约 = 100 股
- 现金账户按权利金占用检查；保证金账户还需检查组合保证金占用
