# GOAI · 期权决策智能体 — 初赛评审 Design Lock

> 本文件是 16 个页代理（pages/*.page）的唯一视觉依据。**逐页生成前必须重读本文件。**
> 颜色 / 字体 / 组件 / 动画 / 图表 / 红线只以本文件为准，禁止凭记忆或临时发明新值。
> 格式规范以 `open-ppt-master/references/pptd.md` 为准；本文件与 deck.pptd（theme 部分）互为镜像，冲突时以本文件为准。

---

## 1. 画布与版心

- **画布**: 16:9 标准演示尺寸 `960 × 540`（pptd.md 定义的演示 16:9 标准值；`1280×720` 为海报尺寸，不用）。
- **单位**: 1px = 1pt；原点 (0,0) 为左上角；元素按 `elements` 数组顺序叠放（后者在上）。
- **边距**: 左右 `64`，上 `24`，下 `24`。内容通栏宽 `832`（x: 64 → 896）。
- **页面分区**（内容页通用骨架，坐标固定，禁止漂移）:

| 分区 | 坐标 | 说明 |
|---|---|---|
| 页头区 | y 24–92 | eyebrow + 结论式标题 + 副标题 + 页头 hairline |
| 正文区 | y 104–372（高 268） | 卡片 / 表 / 图 / 流程图主体 |
| 金句条 | [64, 384, 832, 40] | 每页一条金句 |
| 总结条 | [64, 436, 832, 48] | 每页一条结论（cap「结论」+ 正文） |
| 页脚 | hairline y=512；文字 y=518 | 左：来源/口径；右：页码「NN / 16」 |

- **例外**: 封面（01）与结尾（16）不套页头/页脚框架，仅右下角保留角标「NN / 16」；第 07 页（终端演示）可把正文区扩为整版终端窗，仍保留金句条/总结条。
- **布局节奏**: 不对称优先（3:7 / 2:8 / 左证右论），禁止页页对称卡片墙；分隔用 hairline，不用重边框；单张基础图表宽度 ≤ 版面 1/2。
- 每页 `background: {type: solid, color: "$bg"}`（PPTD 页面背景默认白色，**必须显式设置**）。

## 2. 色板 Token（唯一合法色值）

> 页面与图表（含 matplotlib PNG）只能使用以下 token 与派生值；出现其他 HEX 即为违规。

| Token | HEX | 用途（锁定） |
|---|---|---|
| `$bg` | `#0A0E14` | 页面背景；matplotlib 图底（透明 PNG 外层） |
| `$panel` | `#131A24` | 卡片/面板/终端窗/总结条底 |
| `$border` | `#1F2937` | 边框、hairline、网格线、表格行线 |
| `$text` | `#E6EDF3` | 主文字、标题、表头、图表主文本 |
| `$textSub` | `#8B949E` | 次级文字、标注、图例、轴刻度 |
| `$accent` | `#22D3EE` | 强调青：唯一主色。编号/eyebrow/竖标 tick/KPI 数字/激活态/焦点框/主图表系列 |
| `$success` | `#34D399` | 通过/盈利/生效的闸门与信号 |
| `$warn` | `#F59E0B` | 警告/待验证/行权价虚线/风险提示（非致命） |
| `$danger` | `#F87171` | 拒绝/亏损/阻断/风险闸门 |

派生值（仅在指定场景使用，不得外扩）:

| 派生值 | HEX | 用途 |
|---|---|---|
| `accentA8` | `#22D3EE14` | 金句条底、选中卡底（accent 8%） |
| `accentA20` | `#22D3EE33` | 焦点框底、高亮描边底（accent 20%） |
| `panel2` | `#10161F` | 表头底、嵌套内层面板、深色条 |
| `successA12` | `#34D3991F` | 通过态色带/角标底 |
| `warnA12` | `#F59E0B1F` | 警告态色带/角标底 |
| `dangerA12` | `#F871711F` | 风险态色带/角标底 |
| `grid50` | `$border` + opacity 0.5 | 终端网格装饰线（用 Line 元素 `opacity: 0.5` 实现，不新造 HEX） |

配色纪律: 每页强调色只有 `$accent` 一个主色，红/绿/琥珀只作状态语义；同一页上红紫黄绿四色同框为禁区；暗底不使用黑色阴影（用浅描边/发光表达层级）。

## 3. 字体与字号

- **中文**: `Microsoft YaHei`；**正文拉丁**: `Arial`；**数字/代码/编号/终端文本**: `Consolas`。
- **PPTD 写法**: `fontFamily: {latin: Arial, ea: Microsoft YaHei}`；数字/代码/eyebrow 用 `fontFamily: {latin: Consolas, ea: Microsoft YaHei}`。
  （三者均为 Windows 预装字体，不声明 `customFonts`——customFonts 仅支持 Google Fonts。）
- **字号层级（px，锁定值）**:

| 层级 | px | 粗细 | 字族 | 用途 |
|---|---|---|---|---|
| coverTitle | 44 | bold | YaHei/Arial | 仅封面主标题 |
| title | 26 | bold | YaHei/Arial | 每页结论式页标题 |
| subtitle | 12 | normal | YaHei/Arial | 页标题下方导语 |
| cardTitle | 13 | bold | YaHei/Arial | 卡片/面板标题 |
| body | 11 | normal | YaHei/Arial，行高 1.5 | 正文、表体 |
| annotation | 10 | normal | YaHei/Arial | 标注、图注、说明 |
| footnote | 9 | normal | YaHei/Arial | 来源/口径/免责脚注（最小字号） |
| kpi | 30 | bold | Consolas | 大数字 KPI（hero 场景可用 34） |
| quote | 14 | bold | YaHei/Arial | 金句正文 |
| eyebrow | 9 | bold | Consolas，letterSpacing 3 | 眉题/编号标签/「结论」cap |
| code | 11 | normal | Consolas | 终端/代码/参数值 |
| statusbar | 8 | normal | Consolas，letterSpacing 2 | 右上角状态行专用 |

- **红线**: 全页最小 9px（仅 footnote/eyebrow/statusbar）；正文 ≥ 11px；图表 PNG 内文字视觉等效 ≥ 8px（刻度）/≥ 9px（标签）。
- 文本强调用 `$accent`/`$text` 色差与粗细，不滥用下划线；数字一律写成字符串（YAML 引号包裹）。

## 4. 页面骨架（每页通用件）

### 4.1 标题栏（action-title 式 + 编号）

- 左上 accent 竖标 tick: `rect` [64, 28, 3, 22]，fill `$accent`。
- eyebrow: x 78, y 26，Consolas 9 bold letterSpacing 3，`$accent`，全大写英文（如 `01 · POSITIONING`）。
- 页标题: x 78, y 44–68，`$title`（26 bold `$text`），**必须是结论句（action title）**，不是主题名。
- 副标题: x 78, y 72，`$subtitle`（12 `$textSub`），一行说明页目标。
- 页头 hairline: Line 元素 [64, 92] → [896, 92]，width 1，`$border`。
- 右上角状态行（每页固定，x 786 右对齐，y 22）: `statusbar` 8 Consolas `$textSub`，内容 `GOAI · TERMINAL — 初赛评审   NN/16`（NN 为页码）。
- 标题编号也出现在 eyebrow 与状态行；正文编号（列表/卡片/阶段）用 Consolas 加 `$accent`。

### 4.2 金句条

- 底: roundRect [64, 384, 832, 40]，adjustments 5000，fill `accentA8`，无边框。
- 左轨: rect [64, 384, 3, 40]，fill `$accent`。
- 引导符: Consolas 12 bold `$accent`，文本 `>`（x 76，垂直居中）。
- 引语: x 96，`$quote`（14 bold `$text`），单行（超过 40 字必须精简），行高 1.2。

### 4.3 总结条

- 底: roundRect [64, 436, 832, 48]，adjustments 5000，fill `$panel`，border 1px `$border`。
- cap 标签: 文字 `结论`，`$eyebrow` 风格（Consolas 9 bold letterSpacing 3 `$accent`），x 78 垂直居中。
- 正文: x 130，`$body`（11），**单句结论**，≤ 40 字，可含 1 个 `$accent` 数字。

### 4.4 页脚与终端网格装饰

- hairline: Line [64, 512] → [896, 512]，width 1，`$border`。
- 左: 来源/口径，`$footnote`（9 `$textSub`），x 64, y 518，格式 `来源: <文件> · <日期/口径>`。
- 右: `NN / 16`，Consolas 9 `$textSub`，右对齐 x 896, y 518。
- **终端网格装饰**（可选，用于正文区留白处，每页 ≤ 2 处，克制）:
  - 细网格: 4–8 条横/竖 Line，width 1，`$border` + opacity 0.5（即 grid50），间隔 40–48px，不压内容。
  - 角括号: 两条 10px 短线组成 L 形 bracket，`$accent`，用于封面/结尾/焦点卡四角（最多两角）。

## 5. 组件库

> 以下组件几何为**推荐值**，页代理可按内容微调，但样式（fill/border/字号/圆角）必须照抄。圆角统一 roundRect `adjustments: 5000`（约 6px @120px 高）；胶囊标签 `adjustments: 50000`。

### 5.1 数据卡片

- 底: roundRect adjustments 5000，fill `$panel`，border 1px `$border`；内边距 12。
- 结构: 卡片标题 `cardTitle`（13 bold `$text`）→ KPI 大数字 `kpi`（30 Consolas bold `$accent`）→ 单位/说明 `annotation`（10 `$textSub`）。
- 状态角标: 卡片右上 12×12 小形状（rect/ellipse/triangle）+ 8px 状态字，仅用 success/warn/danger 三个语义色；状态底带用对应 `*A12`。
- 不叠阴影；同页卡片同级一律同样式（不搞主次卡）。

### 5.2 对比表

- 用 deck.pptd 的 `tableStyles.default`（`style: "$default"`）：表头 fill `panel2`、底边 1px `$accent`、bold `$text`；表体行底 hairline 1px `$border`；无竖线、无斑马纹、无重边框。
- 对齐: 首列（名称）left + bold；数字列 right；表头与首列 left。
- 强调单元格: 文字 `$accent` bold 或单元格底 `accentA8`；对错列用 `✓`/`—` 文字符号（`$success`/`$textSub`）。

### 5.3 管线流程图（分阶段揭示，第 06 页）

- 结构: 5 个阶段卡（约 150×96，panel + 1px `$border`）横向排列，卡间用 `rightArrow`/`chevron` 形状（16px，fill `$border`）连接；轨道线 Line 1px `$border` 贯穿卡中心。
- 卡内: 阶段编号 Consolas 20 bold `$accent`（如 `01`）+ 阶段名 `cardTitle` + 一行说明 `annotation`。
- 状态色（边/编号）: 当前激活 `$accent`；已通过 `$success`；被阻断 `$danger`；未启用 `$textSub`。
- 动画: 分阶段揭示（见 §7），初始只显示轨道与第 1 阶段。

### 5.4 决策卡（首屏六件事，第 08 页）

- 2×3 网格: 单卡 266×126，gap 16，起点 (64, 104)。
- 卡内: 编号 `01–06`（Consolas 9 bold `$accent`）+ `cardTitle`（13）+ ≤ 2 行 `body`（11）。
- 六件事方向: 标的快照 / 方向观点 / 期权腿结构 / 执行规则 / 风控闸门 / 状态与结论（具体文案由页代理从 `data/decision_card_2026-08-12.json` 提炼，不得改数字）。
- 焦点卡（裁决所在卡）: 边框换 `$accent`，底 `accentA8`。

### 5.5 时间线（第 15 页）

- 主轴: Line 1px `$border` 横贯正文区中线上方；节点: ellipse 8×8（fill `$accent`，过去节点 `$success`，未来节点 `$textSub`）；里程碑用 donut。
- 节点上方: 日期 Consolas 11 `$text`；节点下方: 事件 `cardTitle` 13 + 说明 `annotation` 10。
- 节点间连线 Line 1px `$border`；相邻节点等距。

### 5.6 终端窗 / 概念 UI（第 07 页及任何界面示意）

- 窗: roundRect adjustments 5000，fill `$panel`，border 1px `$border`；标题栏（高 22）: panel2 底 + 左三个 6×6 rect（`$border`）+ 中 `code` 11 `$textSub` 窗口名。
- 内容: `code` 11 Consolas，`$text` 为命令/输出、`$accent` 为提示符与关键值、`$success`/`$danger` 为信号。
- **概念示意标注（强制）**: 所有手绘 UI 线框右下角加胶囊标签——roundRect adjustments 50000，fill `$panel`，border 1px `$border`，文字 `概念示意`（9px `$textSub`）。

## 6. 动画约定（每页 1–3 组）

- **效果白名单**: 仅 `fade-in` / `wipe-in` / `zoom-in`（对应 fade/wipe/zoom）。禁用其余全部效果。
- **触发**: 自动组 = 首个动画 `trigger: withPrevious`（页进入即播），组内后续 `afterPrevious`（顺序）或 `withPrevious`（同时）；点击组 = 首个动画 `trigger: onClick`，组内同上。
- **参数**: 不写 `durationMs` / `delayMs` / `easing`（用 500ms 默认）；`wipe-in` 方向按表（up/right/left）；`zoom-in` 无方向。
- 每页 ≤ 3 组；组内 ≤ 4 个动画；动画对象是「逻辑块」元素（卡、表、窗、条），不对小块文字逐个做。

| 页 | 节奏 | G1（auto） | G2（click） | G3（click） |
|---|---|---|---|---|
| 01 cover | anchor | 背景装饰 fade-in → 主标题 fade-in → 副标题/信息 fade-in | — | — |
| 02 positioning | dense | 标题栏 fade-in | 定位卡 1 wipe-in(right) | 定位卡 2→3 wipe-in(right) 依次 |
| 03 painpoints | dense | 标题栏 fade-in | 痛点卡 1 wipe-in(up) | 痛点卡 2–4 wipe-in(up) 同时 |
| 04 hero-scenario | breathing | 标题栏+场景面板 fade-in | 参数块 zoom-in | 推论条 wipe-in(up) |
| 05 why-agent | dense | 标题栏 fade-in | 对比表 wipe-in(right) | 结论条 fade-in |
| 06 pipeline | dense | 标题栏+轨道 fade-in | 阶段 1 wipe-in(right) | 阶段 2→5 wipe-in(right) 依次 |
| 07 hero-demo-notrade | breathing | 标题栏+终端窗 fade-in | 交互步骤 1→n wipe-in(left) 依次 | NO_TRADE 裁决框 zoom-in |
| 08 decision-card | dense | 标题栏+六卡框架 fade-in | 卡 1–3 wipe-in(up) 同时 | 卡 4–6 wipe-in(up) 同时 |
| 09 data-layer | dense | 标题栏+层架 fade-in | 层 1–2 wipe-in(right) 依次 | 层 3–4 wipe-in(right) 依次 |
| 10 pricing-engine | dense | 标题栏+引擎面板 fade-in | 希腊值表 wipe-in(up) | 图表 zoom-in |
| 11 risk-gates | dense | 标题栏+闸门轨道 fade-in | 闸门 1–2 wipe-in(left) 依次 | 闸门 3–4 wipe-in(left) 依次 |
| 12 competitors | dense | 标题栏+矩阵 fade-in | 对比行 1→n wipe-in(left) 依次 | 焦点格 zoom-in + 标注 fade-in |
| 13 release-slice | breathing | 标题栏+范围环 fade-in | MVP 扇区 zoom-in | 后续切片 wipe-in(up) |
| 14 backtest-evidence | dense | 标题栏+图卡 fade-in | 图 1 zoom-in | 图 2 zoom-in + 结论 fade-in |
| 15 team-timeline | dense | 标题栏+时间轴 fade-in | 节点 1–2 wipe-in(up) 同时 | 节点 3–4 wipe-in(up) 同时 |
| 16 closing | anchor | 金句 fade-in | 免责声明条 wipe-in(up) | — |

页级过渡由导出器默认 fade，页面文件不写。

## 7. 图表风格（深色 matplotlib PNG 嵌入）

对应资产: `media/backtest_roi_comparison.png`（第 14 页）、`media/straddle_pnl.png`（第 10/14 页候选）。
图表 PNG 由图表代理按本节生成；页面以 `elementType: image` 嵌入，`fit: {mode: contain}`，**禁止 fill 拉伸**。

### 7.1 全局 matplotlib 规范

- 透明底输出: `savefig(..., transparent=True)`；坐标系面板 `axes.facecolor="#131A24"`（$panel），页面底由 `$bg` 透出。
- 出图尺寸: figsize = 嵌入尺寸(px)/96 英寸，dpi ≥ 200（等效 2× 清晰度）；禁止图中带阴影、3D、渐变、图片水印。
- 轴与网格: 轴脊 `#1F2937`（$border）1px；去掉 top/right 脊；网格仅横线，`#1F2937` alpha 0.5，`--` 0.8pt。
- 文字: 刻度/数值 Consolas `#8B949E`（$textSub，视觉等效 ≥ 8pt）；轴标签/图例中文 Microsoft YaHei `#8B949E`（≥ 9pt）；图内不放大标题、不放来源与结论句（这些由 PPTD 文本承载，保证可编辑）。
- 系列色（只用 token）: 主系列 `#22D3EE`（$accent）；基准/对比 `#8B949E`（$textSub）虚线；正收益 `#34D399`（$success）；负收益 `#F87171`（$danger）；警示线/行权价 `#F59E0B`（$warn）虚线；零轴 `#1F2937`。
- 只标关键点: 峰值、断点、终点、最新值；数据标签 Consolas。

### 7.2 单图规格

- `backtest_roi_comparison.png`: 横向条形对比（Hero 策略 vs 基准，≥2 bar），条色 `$accent` / `$textSub`，数值标签 Consolas 9pt `$text`；x 轴单位 %。
- `straddle_pnl.png`: 到期盈亏曲线，x=标的价格、y=P&L；曲线 `$accent` 2pt；盈亏正区 `#34D399` 半透明填充、负区 `#F87171` 半透明填充；行权价 `$warn` 虚线、零轴 `$border`；断点标注数值。
- 嵌入后每图下方放 PPTD 来源注脚: `来源: data/backtest_tencent_straddle.json · 口径: src/backtest_tencent_straddle.py`。
- 图表未就绪时的占位: panel 卡 + 1px `$border` 虚线 + 文字 `图表占位: media/<文件名>`（annotation），**不得放错比例假图**。

## 8. 内容红线（逐页硬约束）

1. **不出现任何真实截图、账户号、密钥、订单回执**；概念 UI 一律用 PPTD 形状画线框，并带「概念示意」标签（§5.6）。
2. **第 16 页必须逐字包含**: 「研究/教育/比赛演示用途，非投资建议，不构成收益承诺」（全文呈现，不得改写/省略）。
3. 数字只来自 `data/` 真实文件并标来源，禁止编造:
   `data/backtest_tencent_straddle.json`、`data/decision_card_2026-08-12.json`、`data/filters_tencent.json`、`data/hero_inputs.json`、`data/hero_proposal_2026-08-08.json`；回测口径见 `src/backtest_tencent_straddle.py`。引用时注明文件与日期。
4. 每页标题是结论句（action title）；主点下 2–4 个子点；正文每块 ≤ 6 行；每页一条金句 + 一条总结。
5. 数字一律写成带引号字符串（YAML 数字歧义防护）；文本中 XML 保留字符 `& < >` 转义为 `&amp; &lt; &gt;`；特殊符号文本用块标量 `|` 承载。
6. 不写空话/口号式 AI 腔（「不是 X 而是 Y」「打造闭环」等）；交易相关表述与「比赛演示用途」口径一致，不暗示真实下单。

## 9. 图标与符号约定

- **禁止** `elementType: icon`（Font Awesome 图标）与任何外站/外链图标资源。
- 图标一律用 PPTD 内置几何形状（`rect` / `roundRect` / `ellipse` / `triangle` / `diamond` / `donut` / `chevron` / `rightArrow` / `hexagon` 等，见 shapes.md）+ Unicode 文字符号，颜色只取 §2 token。
- 符号对照: 上升 `triangle`（`$success`）、下降 `triangle` 旋转 180°（`$danger`）、风险/阻断 `diamond`（`$warn`/`$danger`）、信号节点 `donut`+`ellipse`（`$accent`）、流程箭头 `rightArrow`/`chevron`（`$border`/`$accent`）、对错 `✓`/`—`（文字，`$success`/`$textSub`）、提示 `⚠`（`$warn`）、编号 `01/02…`（Consolas `$accent`）。

## 10. 页代理自查清单（交稿前）

- [ ] 画布 960×540；`background` 为 `$bg`；所有坐标落在画布内、互不遮挡。
- [ ] 颜色只用 §2 token/派生值；字号符合 §3 且 ≥ 9px。
- [ ] 标题为结论句；金句条/总结条/页脚齐全（封面结尾除外）；状态行与页码正确。
- [ ] 组件样式（fill/border/圆角 5000/字号）与 §5 一致；概念 UI 带「概念示意」标签。
- [ ] 动画 ≤ 3 组、只用 fade/wipe/zoom、按 §6 表执行；elementId 唯一且被动画引用有效。
- [ ] 数字来自 data/ 文件、带引号、有来源；无截图/账号/密钥/回执；第 16 页免责声明逐字在。
- [ ] 表格 `style: "$default"`；图片 `fit: contain` 不拉伸；富文本无未转义特殊字符。
