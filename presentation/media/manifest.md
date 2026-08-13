# presentation/media — 媒体资产清单

> 生成时间：2026-08-12（QA 修复版）；生成脚本：`presentation/scripts/gen_media.py`
> 色板合规声明：两张图表 PNG 只使用 `design-lock.md` §2 的九个色板 token HEX
> （`#0A0E14` / `#131A24` / `#1F2937` / `#E6EDF3` / `#8B949E` / `#22D3EE` / `#34D399` / `#F59E0B` / `#F87171`）；
> 旧版色板外 HEX（`#F6465D` / `#F5B942` / `#5A6473` / `#30363D` / `#475569`）已全部移除，
> 像素级扫描确认两张 PNG 中不再出现。透明度派生（8% 盈亏区填充、grid50 网格线）
> 沿用 design-lock §2 派生值机制（如 `accentA8`/`successA12`），不引入新 HEX。
> 所有数字均取自 `data/` 下 JSON，无编造、无账户信息。

---

## 1. backtest_roi_comparison.png

| 项 | 值 |
|---|---|
| 文件 | `presentation/media/backtest_roi_comparison.png` |
| 像素尺寸 | 1044 × 516（figsize = 376/72 × 186/72 in ≈ 5.222 × 2.583，dpi 200） |
| 嵌入设计 | 按 14 页实际嵌入尺寸 **376×186pt** 出图（1pt = 1/72 in），图内文字直接以 pt 指定（标签 ≥ 9pt、刻度 ≥ 8pt），嵌入后等效字号达标、无需缩放 |
| 类型 | 横向条形图 2 根（简洁大字版，替代旧版 22 柱分组图） |
| 数据来源 | `data/backtest_tencent_straddle.json` |
| 字段路径 | `engine_backtest.stats.d2`（口径 A）与 `proxy_backtest.stats.d2`（口径 B）的 `mean_roi_pct` / `win_rate_pct` / `n` |
| 生成参数 | 口径 A 条 `$accent` #22D3EE；口径 B 条 `$textSub` #8B949E（负收益对比不用绿）；数值标签 `$text` Consolas 9.5pt bold；胜率标注 `$textSub` 9pt 右侧；0 轴 `$textSub`；网格 grid50（`#1F2937` @ 0.5）；X 轴 %，范围 −72% ~ +30%（负向区间为主）；标题「d+2 平均 ROI · 口径 A vs 口径 B」（11pt bold）；角落小字「脱敏/模拟回测 · 非实盘业绩 · 样本 11/19 期」 |
| 关键数值 | 口径 A（引擎+历史 IV，n=11）：d+2 平均 ROI **−7.78%**（JSON 原值 -7.778800260929819）、胜率 **36.4%**（36.36363636363637）；口径 B（预期波动代理，n=19，共同期间 2023Q3–2026Q1）：d+2 平均 ROI **−47.54%**（-47.539574713722715）、胜率 **15.8%**（15.789473684210526） |
| 合成/理论值 | 无合成——全部为 JSON 原始回测值（显示取 1~2 位小数） |
| 嵌入建议 | 14 页 `pages/14-backtest-evidence.page`（376×186pt，`fit: contain`，禁止拉伸） |

## 2. straddle_pnl.png

| 项 | 值 |
|---|---|
| 文件 | `presentation/media/straddle_pnl.png` |
| 像素尺寸 | 1033 × 488（figsize = 372/72 × 176/72 in ≈ 5.167 × 2.444，dpi 200） |
| 嵌入设计 | 按 07 页实际嵌入尺寸 **372×176pt** 出图（标签 ≥ 9pt、刻度 ≥ 8pt）；同一文件放大嵌入 10 页 416×234pt，等效字号 ≥ 9pt |
| 类型 | 到期盈亏折线（双成本口径曲线 + 0 轴 + 盈亏平衡竖虚线 + 最大亏损水平虚线 + strike 标记 + 盈亏区 8% 填充） |
| 数据来源 | `data/decision_card_2026-08-12.json` + `data/hero_inputs.json` |
| 字段路径 | `decision_card.numbers`：`strike`=480、`lots`=2、`max_loss`=4414、`breakeven`=[458.905, 501.095]；`hero_inputs.legs[0]`（主到期 2026-08-14）：call/put `mid`（10.27/10.825）与 `ask`（10.75/11.32）；`hero_inputs.account.contract_multiplier`=100 |
| 生成参数 | 曲线：到期内在价值合成 P&L = \|S_T − 480\| × 100 × 2 − 成本；mid 理论成本 4,219.0 HKD（青 `$accent` #22D3EE）、ask 可成交成本 4,414.0 HKD（红 `$danger` #F87171，成本/风险语义，替代旧版绿）；X 轴 400–560；Y 轴 −5,200 ~ +4,400；BE 竖虚线琥珀 `$warn` #F59E0B（标签标注 458.905 / 501.095）；最大亏损水平虚线红（标签 −4,414）；Strike 480 青虚线 + 谷底青点；0 轴 `$textSub` #8B949E；盈亏正区 `$success` 8% 填充、负区 `$danger` 8% 填充；角落小字「理论合成（模拟）· 2026-08-08 · 非实盘业绩」 |
| 关键数值 | BE 458.905 / 501.095（与决策卡一致）；最大亏损 −4,414 HKD（= ask 口径成本，与 `max_loss` 一致）；mid 口径谷底 −4,219；BE 与 mid 口径曲线交于 0 轴，与 ask 口径曲线交点约 −195 HKD（两口径成本之差） |
| 合成/理论值 | **理论合成**：曲线为到期内在价值合成（非市场价）；全部参数（strike、张数、乘数、mid/ask 价、BE、max_loss）取自上述 JSON，未使用 BS/IV 定价 |
| 嵌入建议 | 07 页 `pages/07-hero-demo-notrade.page`（372×176pt）与 10 页 `pages/10-pricing-engine.page`（416×234pt），`fit: contain`，禁止拉伸 |

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
- 本版 matplotlib 兼容性处理（脚本内注释标注）：① `savefig(transparent=True)` 会把 axes patch 临时置为透明，面板底色改用显式不透明 Rectangle 承载，保证透明底 + `$panel` 面板 + 半透明填充正确合成；② `set_title(loc="left")` 存在丢文本 bug，标题用默认位置 + 手动左对齐；③ ylabel 自动定位 bug（锚点落在画布边缘），Y 轴标签改用 `fig.text` 旋转放置。
- 公式命令：`python latex_render.py F:/GOAi_competition/presentation/media --manifest formula_manifest.json`
- 自检（QA 修复后全部 PASS）：
  - 像素尺寸：backtest 1044×516、straddle 1033×488（与 figsize×dpi 一致）；
  - 色板合规：像素级扫描确认旧版违规 HEX（#F6465D/#F5B942/#5A6473/#30363D/#475569）不再出现；
  - 关键标签在场：backtest 10 项（口径名/n/期间/ROI/胜率/标题/角落小字）、straddle 9 项（BE 数值/最大亏损/Strike/双成本/角落小字/轴标签）全部检出；
  - 布局：所有文本/刻度/标题包围盒均落在画布内，无越界裁剪。
- 数据一致性自检：BE = 480 ± 21.095 = 458.905/501.095 ✓（与 decision_card 一致）；ask 口径成本 22.07×100×2 = 4,414 = `max_loss` ✓；口径 A/B 均值与胜率显示值 = JSON 原值按 2/1 位小数 ✓
