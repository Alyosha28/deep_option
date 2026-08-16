# GOAI 桌面研究终端

这是 GOAI 的桌面终端界面：用机构研究工作站的密度呈现报价、到期损益、平值报价、策略敏感度、期权链、风险预算、事件和证据。界面不生成金融数字，也不直接提交订单。

## 运行

直接打开 `index.html` 会使用 `data.js` 的冻结快照。推荐启动原生桌面壳：

```powershell
pip install -r requirements-desktop.txt
python -m src.desktop_app
```

桌面壳会启动本机回环服务并打开最大化终端窗口。若只需要调试服务，也可以运行：

```powershell
python -m src.ui_server --port 8000
```

然后访问 `http://127.0.0.1:8000/`。

服务接口保持不变：

- `GET /api/state`：决策卡、期权链、投研、宏观和政策库状态
- `GET /api/projects`：当前工作区的项目列表与可用性
- `GET /api/projects/discover`：在受控 `data/` 目录中发现可用的快照/投研资料
- `POST /api/projects/select`：切换当前研究项目并返回新状态
- `POST /api/projects`：自动发现并注册一个新的公司研究快照并立即打开
- `POST /api/command`：执行当前项目的 `<GO>`、`REFRESH` 或自然语言研究场景
- `GET /api/policy-library`：政策事件库与来源健康
- `POST /api/run`：用冻结快照重跑研究管线
- `POST /api/chat`：解析自然语言场景并刷新研究卡
- `POST /api/agent`：研究助理动作总线；支持 `ask`、`discover_project`、`refresh`、`select_expiry`、`run_scenario`、`debate`。

终端内的“研究助理”不是装饰性聊天框：它会携带当前工作集，用户可以只说“风险上限改为 2%”而不重复标的；也可以让 Agent 打开期权链、风险检查或分歧记录。点击期权链行会改变当前到期，工作条件、到期选择和对话会在会话/页面刷新后恢复。“研究条件 / 直接重算”里可修改观点、现金、风险上限；场景条件会进入本次风险计算，若预算连最小一张方案都覆盖不了，Risk 会明确拦截，而不是显示一个虚假的可执行方案。所有请求都有真实阶段 trace、加载、错误和可执行下一步，快捷键为 `Ctrl+K` 打开助理、`F5` 刷新、`Esc` 关闭。

### 添加其他公司的期权研究项目

左侧“工作区 → 添加”打开项目抽屉。每个项目由一份独立的冻结快照绑定，不能只输入股票代码就假装拥有该公司的期权链；快照应包含 `underlying`、`spot`、`legs`、`earnings`、`account` 和 `model` 等现有管线字段。

1. 把目标公司的快照 JSON 放入 `data/projects/`，例如 `data/projects/company_inputs.json`。
2. 可选地准备该公司的投研资料 JSON，放在 `data/` 下，格式与 `data/research_items_hero.json` 相同，至少包含顶层 `items` 数组。
3. 在项目抽屉填写名称和通用市场前缀代码（如 `SSE.600519`、`NASDAQ.AAPL` 或 `HK.09988`），点击“自动发现并打开”；快照路径和资料路径默认由 Agent 扫描、校验和绑定。

服务只扫描受控 `data/` 范围，并先验证文件契约、快照 `underlying` 与标的代码，再允许注册；同一标的发现多个版本时会列出候选，不会静默猜错。找不到投研资料时使用空资料集，不会把其他项目的示例新闻带到另一家公司。注册信息保存于 `data/workspaces.json`，后续启动仍可从左栏切换。

也可以直接对研究助理说：`研究 600519 的期权`、`分析任意公司名称的期权机会`。Agent 会在开始研究前自动寻找并打开项目；没有快照、快照重复或资料重复时，会说明阻塞原因和候选文件。

也可以直接调用接口：

```powershell
$body = @{ name = "目标公司"; symbol = "SSE.600519" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/projects?no_audit=1" -Method Post -ContentType "application/json" -Body $body
```

只有同一标的存在多个快照/资料版本时，才在抽屉的“高级：手动覆盖路径”中提供 `inputPath` 或 `researchItemsPath`。

切换项目后，期权链、历史价格、历史波动率、决策卡、研究资料和 Agent 上下文都会以该项目为输入；`data/hero_inputs.json` 只保留为腾讯默认示例，不再是服务层的固定入口。

### 历史价格与实现波动率

历史数据只用于终端的“价格”和“历史波动”图表，不替换 Futu/OpenD 冻结快照、期权链或本地决策引擎。适配器优先使用 OpenBB；未安装 OpenBB 时可直接使用 yfinance provider。安装并显式打开：

```powershell
pip install -r requirements-openbb.txt
$env:GOAI_OPENBB_ENABLED = "1"
$env:GOAI_OPENBB_PROVIDER = "yfinance"
$env:GOAI_OPENBB_PERIOD = "2y"
python -m src.desktop_app
```

这些开关也可以写入项目根目录 `.env`；进程环境变量优先于 `.env`。服务启动时会读取它们。

也可以通过 `GOAI_OPENBB_START_DATE`、`GOAI_OPENBB_END_DATE`、`GOAI_OPENBB_INTERVAL` 限制请求。终端会把日线收盘价直接绘制为历史价格，并用 close-to-close 对数收益率计算 30 日年化实现波动率：`stdev(log return) × sqrt(252)`。未安装 provider、没有数据或样本不足时，图表显示数据缺口，不补造 K 线或波动率。不同 provider 的港股代码格式可能不同，适配器只对 yfinance 将 `HK.00700` 转为 `0700.HK`。

### 添加其他研究资料

研究页读取一个 canonical JSON 文件。默认是演示文件；把真实公告、业绩、新闻、研报或行业资料整理为条目后，通过环境变量切换：

```powershell
python -m src.research_sources --keyword Tencent --markdown .\notes\tencent-news.md --out .\data\research_items_tencent.json
$env:GOAI_RESEARCH_ITEMS_PATH = "data/research_items_tencent.json"
python -m src.desktop_app
```

条目至少包含 `id`、`kind`、`title`、`published_at`、`source`、`body`；可选 `url`、`tags`、`synthetic`。`kind` 使用 `announcement`、`earnings`、`news`、`research` 或 `industry`。真实资料把 `synthetic` 设为 `false`，并保留原始来源和链接；研究页会显示当前文件路径与“演示资料 / 来源已标注”状态。切换资料源只影响投研证据，不会把未经核验的文字混入期权定价或风险数字。

## 数据与交互边界

- 策略、Greeks、风险和决策数字来自冻结 Futu 快照或本地确定性引擎；历史图表数字来自已标注的历史行情 provider。
- 历史行情只进入价格图表与实现波动率图表，并在状态中保留 provider / 来源信息；它不参与当前期权策略结论。
- 页面只负责格式化和渲染，保留原有 DOM ID 与 API 契约。
- `NO_TRADE`、`LOW_EDGE` 等引擎枚举只作为内部状态保留，用户界面显示对应的自然语言判断。
- 当前版本是 Replay/冻结快照；实时行情、模拟确认和订单提交仍需用户独立确认。

## 视觉资产

- `assets/goai-signal-field.png`：事件前波动场的抽象纸张纹理，用作研究上下文，不作为数据图表。
- `assets/goai-signal-field.prompt.md`：生成该资产的 plot 提示词与使用约束。

视觉方向来自用户确认的 Image 2 高级终端母稿：Monokai Dimmed 深灰石墨工作站、顶部主模块导航、左侧研究工作区树、中部“主图 + 平值报价 + Greeks + 期权链”连续工作台、右侧结论账本。亮绿只表示当前选择，绿/红只表示市场和风险语义；界面使用矩形网格与等宽数字，不使用 AI 控制台式卡片墙。
