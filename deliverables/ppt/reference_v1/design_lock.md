# GOAI 初赛参考版 PPT — Design Lock（Style E · 券商报告风）

> 唯一视觉依据。生成/修改任何 .page 前重读本文件。数字与事实口径以 `presentation/facts.md` 为准。

## 1. 画布与版心

- 画布：960 × 540（16:9）；左右边距 64（内容通栏宽 832，x: 64 → 896）
- 页头区：kicker y=40、标题 y=64（h 44）、强调发丝线 y=120（56×3）
- 内容区：y=140 起；4 行卡（h 64 / 间距 12）或 6 行紧凑卡（h 44 / 间距 8）
- 金句区：竖条 [64, 456, 4, 36] + 正文 [80, 458, 800, 34]
- 来源线：y=500（9.5pt 等宽）；页码：右上 [836, 518, 60, 12]「NN / 16」

## 2. 色板（唯一合法色值）

| token | HEX | 用途 |
|---|---|---|
| bg | #F7F5F0 | 页面底（暖灰纸） |
| panel | #FDFCFA | 卡片/面板底 |
| border | #DDD8CD | 1px 发丝边/分隔线 |
| text | #1F2A2A | 标题/正文主色 |
| sub | #6B726C | 次级文字/来源/页码 |
| accent | #0E7C4A | 墨绿：编号、kicker、强调条、加粗数字 |
| down | #B0413E | 亏损/拒绝/FAIL（仅在语义需要时用） |
| up | #0E7C4A | 盈利/通过（与 accent 同） |

## 3. 字体

- 标题/金句：`{latin: Georgia, ea: SimSun}` bold
- kicker/编号/来源/页码：`{latin: Consolas, ea: Microsoft YaHei}`
- 正文：`{latin: Arial, ea: Microsoft YaHei}`
- 层级：页标题 24 / 封面标题 40 / 正文 12.5 / 金句 12.5 bold / 来源 9.5 / kicker 9（字距 3）

## 4. 组件

- 行卡：roundRect，fill $panel + border 1px $border；编号 Consolas 22 bold $accent（x=84）；
  正文 x=146，数字用 `<strong>` 加粗、负值语义可用 $down
- 金句：左侧 4px $accent 竖条 + bold 正文，单行截断（wrap true 备用）
- 来源线：Consolas 9.5 $sub，左对齐，含出处文件与时间
- 封面（01）：accent 条 [64,84,56,4] + kicker + 40pt 标题 + 副题 + 元信息块
- 产品页（05）：左侧 3 行卡（x=64, w=430），右侧截图 [522, 140, 374, 224] fit contain + border，图注在下方
- 收尾页（16）：3 行卡 + 金句，无来源线（改脚注）

## 5. 图表（matplotlib 生成，scripts/gen_charts.py）

- 底色透明（露出纸底）；文字 $text；网格 $border 虚线；正 #0E7C4A / 负 #B0413E / 次负 #C8734F
- 中文字体 Microsoft YaHei；数值标签加粗；生成时以 facts 原值 assert（改图必先改脚本再跑）
- 三张：chart_breakeven（02 页右栏）、chart_roi / chart_crush（09 页双图）
- 页码总数 **17**（02 页左三行卡+右盈亏平衡图；09 页回测与压力测试双图）

## 6. 红线（违反即重做）

1. 数字不改写：一律以 facts.md / decision_card JSON 原值（3.92%、4.41%、-7.8%、36.4%、-711、4,414、83 行、367 tests、67s、38,838；评审修复轮 2026-08-14 更新测试/审计计数口径）
2. 禁语不出现：帮你赚钱 / 自动下单 / 替代专业判断 / 旧模拟单作证据 / 声称覆盖全部标的
3. 每页必须有：结论式标题 + 至少一条可溯源数字或事实 + 来源线（封面/收尾除外）
4. 不添加元素动画与演讲者备注（仅默认页间淡入）
