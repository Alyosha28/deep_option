# 04 专家方法库（Expert Methods）

> 每条方法都注明出处（见 sources.json 对应 id）。引用进 PPT/代码前请复核原文。

## tastytrade / tastylive（研究驱动零售期权）
- 16Δ 卖方跨式（strangle）@45 DTE，50% 利润或 21 DTE 退出 —— 其研究称该组合日均 P/L 最大。
- 30Δ 跨式的实际胜率高于期望胜率（对比 16Δ 研究）。
- 启示：策略引擎应内置“证据标注的 playbook”（入场 DTE/Delta/退出规则可配置），并展示研究依据。

## Euan Sinclair《Volatility Trading》
- 核心：成功交易=一致的流程；覆盖定价、波动率测量/预测、IV 动态、对冲、资金管理、交易心理学。
- 启示：产品需同时给“数据/定价正确性”和“流程纪律”（仓位规则、日志、复盘）。

## Cem Karsan（Alerce / Kai Volatility）
- 观点：结构性产品（如尾部基金/年金）供给波动率，影响 dispersion/correlation/广度；价格行为由流驱动；市场“被管理”。
- 长波动凸性：VIX calls / 深虚值尾部最划算；始终建模尾部情景。
- 启示：市场情境层（波动率供给/流动性状态）可作为美股部分的高级模块；港股期权流量数据有限，先做 L1 驱动。

## Kris Abdelmessih（Moontower / moontower.ai）
- “波动率镜头”：帮有观点的交易者把观点翻译成期权结构；期权偏斜（skew）在冲击事件中会快速重定价；模型必须持续重校准。
- moontower.ai 是最接近我们产品形态的竞品 → 持续跟踪其功能与定位。

## SpotGamma / Brent Kochuba（@spotgamma）
- GEX（gamma exposure）：每行权价 GEX ≈ Γ × OI × 合约乘数 × S² × 1%，买权为正、卖权为负，加总得做市商净 gamma。
- 正 gamma → 钉住/均值回归；负 gamma → 趋势放大/突破。
- 启示：可用公开数据自行计算 GEX 近似值，作为差异化分析面板（moomoo Engine 也在做）。

## TradingAgents（arXiv 2412.20138）
- 多 Agent LLM 交易框架：Fundamentals/Sentiment/News/Technical 四分析师 + Researcher + Trader + Risk Manager，agent 辩论后由 Trader 决策、Risk Manager 把关。
- 启示：我们 Agent 架构直接参考——数据 Agent、策略 Agent、研究 Agent、风险审计 Agent；Risk Manager 必须有一票否决。

## ORATS / VolRadar / ApexVol（IV 与业绩事件）
- IV crush：业绩后 IV 常一夜降 20–50%，与方向无关；earnEffect = (IV 含事件 − IV 剔除事件) / IV 含事件。
- RV Ratio = 20 日历史波动 / 30 日隐含波动：>1 表示期权相对低估（卖方信号），<1 相对高估。
- IV Rank：52 周区间归一化 0–100。
- 启示：业绩跨式用例必须输出：隐含波动幅度（ATM straddle 价格）vs 历史实际波动幅度、IV Rank、crush 预警、日历价差替代建议。

## Option Alpha（Kirk Du Plessis）
- 规则化：短腿 ITM 概率窗口（如卖权 23–35%）、GTC 自动止盈、被挑战自动 roll、不用市价单。
- 启示：自动化规则库 + 概率窗口校验，可作为“策略规则引擎”的骨架。

## 学术：期权流动性
- 常用度量：比例买卖价差 PBA（proportional bid-ask spread）、成交量、价格冲击、OI 共同性（commonality）。
- 发现：业绩公告后期权价差可能走宽（Illiquidity and Higher Cumulants）→ 流动性模块要事件感知。
- 启示：流动性评分 = 加权(PBA, OI 趋势, 成交量, 报价量, 深度[若有])，并用 PBA 口径与学术文献对齐。

## 定价引擎（B 主责）
- 欧式（恒指/国企指数期权）：Black-Scholes 解析解。
- 美式（港股个股/美股个股期权）：二叉树/有限差分/LSMC；美式 Greeks 用 bump-and-reprice（无解析 vega/rho）。
- 参考：Quant StackExchange “american-options+binomial-tree” 标签。
