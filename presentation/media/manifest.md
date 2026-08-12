# presentation/media — 媒体资产清单

> 生成时间：2026-08-12（会话时钟）；生成脚本：`presentation/scripts/gen_media.py`
> 深色主题（与 deck_v2 design_lock 一致的暗底风格，任务指定色板）：
> 背景 `#0A0E14`、面板 `#131A24`、网格 `#1F2937`、主文字/数值标签 `#E6EDF3`、轴标签 `#8B949E`。
> 所有数字均取自 `data/` 下 JSON，无编造、无账户信息。

---

## 1. backtest_roi_comparison.png

| 项 | 值 |
|---|---|
| 文件 | `presentation/media/backtest_roi_comparison.png` |
| 像素尺寸 | 2560 × 1440（figsize 12.8×7.2 @ dpi 200，16:9） |
| 类型 | 分组柱状图（11 组 × 2 柱，共 22 根柱 + 逐柱数值标签） |
| 数据来源 | `data/backtest_tencent_straddle.json` |
| 字段路径 | 柱值：`engine_backtest.periods[].horizons["2"].roi`（口径 A）与 `proxy_backtest.periods[].horizons["2"].roi`（口径 B，按相同 `period` 标签对齐，共同期间 2023/Q3–2026/Q1）；附注统计：`engine_backtest.stats.d2`（mean/median/win_rate_pct，n=11）与 `proxy_backtest.stats.d2`（n=19） |
| 生成参数 | 柱色 口径 A `#22D3EE` / 口径 B `#34D399`；ROI 小数 → 百分比（×100，标签 1 位小数）；Y 轴 −132% ~ +195%，0 轴参考线 |
| 关键数值 | 口径 A d2：均值 −7.8%、中位 −35.2%、胜率 36.4%；口径 B d2：均值 −47.5%、中位 −62.9%、胜率 15.8%；极值：口径 A +108.0%（2025/Q4）/ −74.6%（2024/Q3）；口径 B（共同期间）+168.7%（2025/Q4）/ −98.7%（2023/Q4） |
| 合成/理论值 | 无合成——全部为 JSON 原始回测值 |
| 嵌入建议 | `deck_v2/pages/08_evidence_backtest.page`（历史回测负期望证据页）；可作为全宽图 |

## 2. straddle_pnl.png

| 项 | 值 |
|---|---|
| 文件 | `presentation/media/straddle_pnl.png` |
| 像素尺寸 | 2560 × 1440（figsize 12.8×7.2 @ dpi 200，16:9） |
| 类型 | 到期盈亏折线（双口径成本曲线 + 0 轴 + 盈亏平衡竖虚线 + 最大亏损水平线 + strike 标记） |
| 数据来源 | `data/decision_card_2026-08-12.json` + `data/hero_inputs.json` |
| 字段路径 | `decision_card.numbers`：`strike`=480、`lots`=2、`max_loss`=4414、`breakeven`=[458.905, 501.095]；`hero_inputs.legs[0]`（主到期 2026-08-14）：call/put `mid`（10.27/10.825）与 `ask`（10.75/11.32）；`hero_inputs.account.contract_multiplier`=100 |
| 生成参数 | 曲线：到期内在价值合成 P&L = \|S_T − 480\| × 100 × 2 − 成本；理论口径成本 4,219.0 HKD（mid 21.095×100×2，`#22D3EE`）、可成交口径成本 4,414.0 HKD（ask 22.07×100×2，`#34D399`）；X 轴 440–520（盈亏平衡点 ± 合理区间）；Y 轴 −5,900 ~ +4,600；BE 虚线 `#F5B942`、最大亏损线 `#F6465D` |
| 关键数值 | BE 458.905 / 501.095（与决策卡一致）；最大亏损 −4,414 HKD（= ask 口径成本，与 `max_loss` 一致）；理论口径谷底 −4,219；注意：BE 与理论（mid）口径曲线交于 0 轴，与 ask 口径曲线交点约 −195 HKD，即两种成本口径之差 |
| 合成/理论值 | **理论合成**：曲线为到期内在价值合成（非市场价）；全部参数（strike、张数、乘数、mid/ask 价、BE、max_loss）取自上述 JSON，未使用 BS/IV 定价 |
| 嵌入建议 | `deck_v2/pages/07_evidence_pnl.page`（盈亏结构证据页）；可半宽或全宽 |

## 3. 公式 PNG（formulas/）

| 项 | 值 |
|---|---|
| 文件 | `presentation/media/formulas/formula_black_scholes.png`（1364×121）、`formula_straddle_breakeven.png`（1367×50）；另有脚本原始输出 `media/images/`（内容相同） |
| 渲染方式 | `open-ppt-master/scripts/latex_render.py` + `media/formula_manifest.json`；provider = codecogs（成功），dpi 300，前景 `#E6EDF3`，透明背景 |
| 公式内容 | (1) Black-Scholes：C = S·N(d₁) − K·e^(−rT)·N(d₂)，d₁,₂ = [ln(S/K) + (r ± σ²/2)T] / (σ√T)；(2) 盈亏平衡式：S_BE = K ± (c_mid + p_mid) = 480 ± 21.095 ⇒ 458.905 / 501.095（mid 口径 21.095 = 10.27 + 10.825，来自 hero_inputs） |
| 降级方案 | 本次渲染成功；若环境无网络/渲染服务不可用，PPT 内改用等宽字体（Consolas）文本排印公式，内容见上 |
| 嵌入建议 | 公式 1 → `06_hero.page` 或 `07_evidence_pnl.page` 的理论支撑角标；公式 2 → `07_evidence_pnl.page` 盈亏平衡标注旁 |

## 生成与校验

- 生成命令：`F:/GOAi_competition/.venv/Scripts/python presentation/scripts/gen_media.py`（matplotlib 3.11.1，中文字体 Microsoft YaHei，`axes.unicode_minus=False`）
- 公式命令：`python latex_render.py F:/GOAi_competition/presentation/media --manifest formula_manifest.json`
- 校验：两张图表 PNG 经像素级检查确认深色背景/面板/柱色/曲线色均存在；公式 PNG 确认含前景浅色像素且尺寸已测量
- 数据一致性自检：BE = 480 ± 21.095 = 458.905/501.095 ✓（与 decision_card 一致）；ask 口径成本 22.07×100×2 = 4,414 = `max_loss` ✓
