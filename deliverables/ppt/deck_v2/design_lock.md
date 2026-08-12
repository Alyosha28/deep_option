# GOAI v2 Deck — Design Lock

> PPTD 版设计锁：颜色/字体/组件唯一来源；逐页生成前重读本文件。

## 1. Canvas & Grid

- Canvas: 960 × 540（16:9）
- Margin: 左右 60；页头区 y=10–104；正文区 y=120–390；金句条 y=396–430；总结条 y=438–492；页脚 y=518–534
- 布局节奏：不对称优先（3:7 / 2:8），避免每页对称卡片墙；分隔用 hairline，不用重边框

## 2. Colors

| token | HEX | 用途 |
|---|---|---|
| bg | #07090D | 页面底 |
| panel | #0D1117 | 面板/卡片 |
| deep | #0A0F14 | 总结条、深色块 |
| hairline | #FFFFFF14 | 1px 发丝边 |
| ink | #F2F5F9 | 主文字 |
| sub | #8B93A3 | 次级文字 |
| faint | #5A6473 | 注脚 |
| primary | #38BDF8 | 主色/信号（青蓝） |
| primaryA | #38BDF81A | 主色 10% 底 |
| up | #16C784 | 盈利/通过 |
| down | #F6465D | 亏损/拒绝 |
| warn | #F5B942 | 警示/待验证 |
| watermark | #10151C | 页码水印 |

## 3. Typography

- 标题中文: Microsoft YaHei Bold；标题拉丁: Arial Bold（大写、字距 2）
- 正文中文: MiSans；正文拉丁: Arial
- 大数字: Georgia
- 层级: 页标题 26 / 副标题 11 / 正文 11–12 / 注脚 9 / hero 数字 30–34

## 4. Components

- 页头: 左侧 primary 竖标 [60,17,14,2] + eyebrow（Arial Bold 10，字距 2）+ 结论式标题 + 副标题
- 面板: fill $panel + border hairline 1px；角半径 0（Linear 式方正）
- hero 数字: Georgia 30–34 白色，配 cap 标签
- 金句条: fill $primaryA + 左侧 primary 4px 轨 + 「金句」cap 标签 + 12px bold 引语
- 总结条: fill $deep + hairline 边 + 11px 正文
- 页脚: hairline 分隔线 + 左来源注脚 + 右页码；右上角 watermark 大页码

## 5. Content rules

- 每页标题必须是结论（action title），不是主题名
- 主点下 2–4 个子点；论据只用 data/*.json 真实数字并标来源；正文 ≤7 行
- 每页一条金句 + 一条总结；数字一律写成带引号字符串
