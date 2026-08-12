# GOAI 期权智能终端 - 参考 UI

一版可直接在浏览器打开的四面板参考界面（`index.html` + `styles.css` + `app.js` + `data.js`，无外部依赖、无构建步骤）。

## 打开方式

直接用浏览器打开 `ui/index.html`，或：

```powershell
python -m http.server 8000 --directory ui
```

然后访问 `http://127.0.0.1:8000/`。

## 数据来源

所有数字取自真实产物，未虚构：

- `data/hero_inputs.json`：2026-08-08 Futu OpenD 冻结快照（正股、期权链、IV、OI、账户）
- `data/decision_card_2026-08-12.json`：自研引擎产出的决策卡（成本、Greeks、情景损益、Edge/Risk/Action 门、审计哈希）

冻结快照仅录制了 ATM 480 合约，界面中已显式标注，没有补造其他行权价。

## 设计参考（开源金融项目）

配色、密度和信息层级综合参考了以下项目的公开 UI 方向：

| 项目 | 借鉴点 |
|---|---|
| OpenBB Workspace / Terminal | 终端式深色面板、物化产物（artifacts）、审计与来源可见性 |
| AlphaMatrix（React + shadcn + Tailwind） | 策略研究 UI、Greeks 指标卡、回测/运行监控的密度 |
| Ghostfolio（Angular Material） | 清晰的卡片层级、指标速览、移动端优先的响应式 |
| Maybe Finance | 简洁信息架构、净值和现金流可视化层级 |
| TradingView 风格暗色终端 | 行情数字用等宽字体、红绿 P&L 语义、图表优先 |
| shadcn-fintech-template 等 | 现代 fintech 暗色仪表盘的通用质感 |

## 视觉系统

- 方向：工业终端 × 编辑级清晰，一屏内呈现「对话与任务 / 期权链与流动性 / 策略与账户 / 事件与证据」四面板。
- 颜色：近黑底 `#07090D`，面板 `#0D1117`，唯一信号色为琥珀 `#F5B942`；P&L 语义红跌 `#F6465D` / 绿涨 `#16C784`。
- 字体：中文用系统无衬线栈，所有数字用等宽栈，保证报价对齐。
- 动效：只保留载入时的逐面板进入和「重新运行」的阶段动画，尊重 `prefers-reduced-motion`。
- 响应式：1280px 以下右栏下移并重排，980px 以下单列。

## 与当前产品能力的关系

这是参考 UI，不是可运行的产品入口。数据展示对应 P0a（Replay/冻结快照）决策卡；Live 数据、对话 Agent 编排和模拟确认流程尚未接入。
