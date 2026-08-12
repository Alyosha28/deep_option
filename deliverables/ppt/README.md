# GOAI 初赛方案 PPT — 交付说明

## 当前版本：v3（编辑杂志风视觉重做）

`deck_v3/` 在 v2 基础上仅重做视觉层：16 页叙事、结论、全部数字与来源保持不变。
浅色纸底 + 墨黑 + 青蓝强调，衬线大标题（SimSun/Georgia）+ 无衬线正文
（Microsoft YaHei/Arial），发丝规则线分栏，无卡片阴影；页切换淡入 + 自动播放的
逐元素入场动效（标题升起、内容分组渐入、图表/卡片缩放）。

| 文件 | 用途 |
|---|---|
| `deck_v3/deck_v3.pptx` | 演示版：16 页，页切换淡入 + 463 条入场动效（自动播放） |
| `deck_v3/deck_v3.pdf` | 提交/审阅用 PDF（LibreOffice 渲染，16 页） |
| `deck_v3/` | 可编辑 PPTD 工程（manifest + pages + design lock） |
| `.qa-images-v3/` | 16 页渲染图（QA 产物） |

重新生成：

```powershell
python C:\Users\Administrator\.agents\skills\open-ppt-master\scripts\export_pptx.py deliverables\ppt\deck_v3\deck_v3.pptd --output deliverables\ppt\deck_v3\deck_v3.pptx --force
python deliverables\ppt\fix_pptx.py deliverables\ppt\deck_v3\deck_v3.pptx
Remove-Item deliverables\ppt\.roundtrip-v3\deck_v3.pptx -ErrorAction SilentlyContinue
& 'C:\Program Files\LibreOffice\program\soffice.exe' -env:UserInstallation=file:///C:/Users/Administrator/AppData/Local/Temp/lo_profile_v3 --headless --convert-to pptx --outdir deliverables\ppt\.roundtrip-v3 deliverables\ppt\deck_v3\deck_v3.pptx
Copy-Item deliverables\ppt\.roundtrip-v3\deck_v3.pptx deliverables\ppt\deck_v3\deck_v3.pptx -Force
& 'C:\Program Files\LibreOffice\program\soffice.exe' -env:UserInstallation=file:///C:/Users/Administrator/AppData/Local/Temp/lo_profile_v3 --headless --convert-to pdf --outdir deliverables\ppt\deck_v3 deliverables\ppt\deck_v3\deck_v3.pptx
python deliverables\ppt\qa_check_v3.py
```

> 说明：字体使用系统自带的 SimSun / Microsoft YaHei / Georgia / Arial，避免可变字体
> 在 LibreOffice 中被映射成 Thin/ExtraLight。重新导出后必须完成 fix_pptx.py +
> LibreOffice 往返两步，否则 PowerPoint 可能提示修复文稿。

## v2（深色终端风，上一版）

## 交付文件

| 文件 | 用途 |
|---|---|
| `deck_v2/deck_v2_live.pptx` | 演示版：16 页全部带逐级动画（328 个动画条目），页切换淡入 |
| `deck_v2/deck_v2_print.pptx` | 静态版：同内容、无元素动画 |
| `deck_v2/deck_v2_print.pdf` | 提交/审阅用 PDF（LibreOffice 渲染，16 页） |
| `deck_v2/` | 可编辑 PPTD 工程（manifest + pages + design lock） |
| `deck_v2_print/` | 无动画的 PPTD 副本（供静态导出） |
| `.qa-images-v2/overview.jpg` | 16 页缩略总览，人工视觉 QA |
| `fix_pptx.py` / `qa_check_v2.py` | PowerPoint 兼容修复与质检脚本 |

## 设计方向（v2 redesign）

深色终端 × Linear 式精修：近黑底 `#07090D`、面板 `#0D1117`、1px 发丝边框、
青蓝主色 `#38BDF8`，红/绿仅用于盈亏语义，琥珀用于警示/待验证。产品页为
四面板终端的矢量重绘（无模糊截图），数字全部来自冻结快照与决策卡 JSON。

内容结构：产品市场叙事（问题→方案→产品→证据→市场→竞品→团队→路线），
每页 action title 写结论、子点+论据支撑、金句条+总结条；任务闭环、数据来源、
隐私保护、行业边界按赛道评审口径显式覆盖。

## 已通过检查

- 页数 16；Open XML 导出零错误；LibreOffice 规范化重写后 PowerPoint 兼容
- live 版 328 个入场动画、print 版 0；关键数字与 data/*.json 程序化核对一致
- 脱敏扫描干净；PDF 字体仅 Arial / Georgia / MiSans / Microsoft YaHei

## 重新生成

```powershell
python C:\Users\Administrator\.agents\skills\open-ppt-master\scripts\export_pptx.py deliverables\ppt\deck_v2\deck_v2.pptd --output deliverables\ppt\deck_v2\deck_v2_live.pptx --force
python C:\Users\Administrator\.agents\skills\open-ppt-master\scripts\export_pptx.py deliverables\ppt\deck_v2_print\deck_v2_print.pptd --output deliverables\ppt\deck_v2\deck_v2_print.pptx --force
python deliverables\ppt\fix_pptx.py deliverables\ppt\deck_v2\deck_v2_live.pptx deliverables\ppt\deck_v2\deck_v2_print.pptx
New-Item -ItemType Directory -Path deliverables\ppt\.roundtrip -Force | Out-Null
& 'C:\Program Files\LibreOffice\program\soffice.exe' --headless --convert-to pptx --outdir deliverables\ppt\.roundtrip deliverables\ppt\deck_v2\deck_v2_live.pptx deliverables\ppt\deck_v2\deck_v2_print.pptx
Copy-Item deliverables\ppt\.roundtrip\deck_v2_live.pptx deliverables\ppt\deck_v2\deck_v2_live.pptx -Force
Copy-Item deliverables\ppt\.roundtrip\deck_v2_print.pptx deliverables\ppt\deck_v2\deck_v2_print.pptx -Force
& 'C:\Program Files\LibreOffice\program\soffice.exe' --headless --convert-to pdf --outdir deliverables\ppt\deck_v2 deliverables\ppt\deck_v2\deck_v2_print.pptx
python deliverables\ppt\qa_check_v2.py
```

> 重新导出后必须完成 fix_pptx.py + LibreOffice 往返两步，否则 PowerPoint 可能提示修复文稿。
