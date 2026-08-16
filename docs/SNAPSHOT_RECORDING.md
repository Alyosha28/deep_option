# GOAI 冻结快照录制指引（新标的多项目支持）

> 数据铁律：产品运行时的所有金融数字来自 **Futu Live**（P0b+）或**冻结快照**
> （P0a Replay）。当前 P0 切片为 Replay：新增研究项目 = 为标的录制一份
> 通过契约校验的冻结快照。本文档说明快照长什么样、怎么录制、放哪里、如何注册。

## 0. 一句话

新标的进入工作区 = 把该标的的冻结快照 JSON 放入 `data/projects/`，再在 UI
「添加研究项目」里填名称 + 标的代码（如 `SSE.600519`），Agent 自动校验并打开。
**P0 演示范围以 `data/` 内已有快照为准（默认 HK.00700 腾讯）**；录制新快照
不代表 Live 能力，所有数字仍标记为 Replay / 冻结。

## 1. 快照契约（必须通过 `load_frozen_snapshot` 校验）

参考现有快照：`data/hero_inputs.json`（hero 演示）与 `data/snapshots/2026-08-08_*.json`。
必需要素（与 `src/decision_pipeline.py::load_frozen_snapshot` 的校验一致）：

```jsonc
{
  "schema_version": "1.0",
  "mode": "REPLAY",            // 录制来源：REPLAY（冻结）/ LIVE
  "origin": "FUTU",            // 数据来源声明
  "freshness": "FROZEN",
  "captured_at": "2026-08-08T11:56:30+08:00",   // ISO-8601，录制时点
  "source": "futuapi/OpenD 127.0.0.1:11111",
  "snapshot_sha256": "<64 hex>",                // 快照身份哈希（防误改/损坏）
  "payload": {
    "underlying": "HK.00700",                   // 必须与注册 symbol 完全一致
    "name": "腾讯控股",
    "spot": 478.8,
    "prev_close": 479.2,
    "market_state": "HK CLOSED ...",
    "earnings": { "date": "2026-08-12", "expected_move_pct": 3.916, ... },
    "account": { "cash_hkd": 100000, "risk_budget_pct": 5.0, ... },
    "legs": [ /* 主/次到期 call/put：code、strike、bid/ask、iv、open_interest ... */ ],
    "model": { "riskfree_rate": 0.035, "div_yield": 0.0 }
  }
}
```

校验规则（注册时强制执行）：
- 能通过 `load_frozen_snapshot`（schema、必需键、sha256 一致性）；
- `payload.underlying` 与填写的标的代码**完全一致**（大小写/市场前缀规范化后比较）；
- 快照文件必须位于 `data/projects/` 目录内；
- 同一标的只允许一份快照（多份时在高级路径中指定）。

## 2. 录制路径 A：已有数据（最快）

如果你已有该标的的合法快照 JSON（例如从 `data/snapshots/` 复制）：

```powershell
Copy-Item data\snapshots\<标的快照>.json data\projects\<symbol>.json
```

然后在 UI 添加项目，或直接调用：

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/projects -H "Content-Type: application/json" `
  --data-binary "{\"name\":\"贵州茅台\",\"symbol\":\"SSE.600519\"}"
```

## 3. 录制路径 B：OpenD + futuapi（从真实行情录制）

1. 启动并登录 Futu OpenD（127.0.0.1:11111）；
2. 用项目 skill `futu-options-agent`（或 `futuapi` skill 的行情接口）拉取：
   标的快照/报价、期权链（call/put 的 bid/ask/IV/OI/成交量）、业绩日历、
   账户风险摘要（可选）；
3. 按 §1 契约组装 JSON，写入 `data/projects/<symbol>.json`；
   可用 `src/snapshot_recorder.py` 的 `SnapshotRecorder`（线程安全 JSONL/JSON 落盘）
   或直接写文件（必须填 `snapshot_sha256`，或让注册校验按文件内容计算）；
4. 投研资料（可选）：canonical 条目 JSON（`src/research_sources.py` 适配输出）
   放入 `data/projects/` 同目录；缺省使用空资料集，**不会误用腾讯示例**。

> 录制是新数据接入行为：行情/期权链来自授权 OpenD 会话，录制产物属本地数据资产，
> 与审计链一样只增不减、不进公开仓库。

## 4. 失败路径对照（UI「添加研究项目」的错误提示）

| 场景 | 提示 |
|---|---|
| 没有该标的快照 | `没有找到 <symbol> 的有效期权快照：请先将...放入 data/projects/（录制与格式见本文档）；当前 P0 演示范围以 data/ 内已有快照为准` |
| 快照 underlying 与填写的代码不一致 | `项目 symbol (...) 与快照 underlying (...) 不一致` |
| 发现多份快照 | `发现多个 <symbol> 快照，请在高级路径中指定一个：...` |
| 快照不在 data/projects/ 内 | `新项目快照必须放在 data/projects/ 目录内` |
| 标的已存在 | `标的已经在工作区中：<symbol>` |
| 投研资料多份 | `发现多个 <symbol> 投研资料，请在高级路径中指定一个：...` |

## 5. 范围声明（诚实边界）

- 新标的录制成功即进入 **Replay 研究范围**：确定性引擎照常计算，数字全部
  标记为冻结快照来源；
- **不因此宣称 Live 能力**：Live 行情接入（P0b）与模拟提交（P0c）仍依赖
  OpenD 权限与验收，未毕业前对外宣传不超过最高已验证切片；
- 港股规格/费用/滑点 policy 未冻结前，新标的的 executable 成本口径与
  hero 一致（以 ask 计，状态 UNVERIFIED）。
