# GOAI 实时行情升级设计（快照 → 实时）

日期：2026-08-16
状态：第二阶段已完成（SSE 推送/订阅 + UI 自动刷新 + 真实 OpenD 端到端冒烟；
第一阶段 LIVE 只读 GET 链路保持不回退）

## 0. 第一阶段完成情况（与代码实际行为一致）

| 项 | 实际行为 | 代码位置 |
|---|---|---|
| 环境开关 | `GOAI_DATA_MODE=live|replay`（缺省 replay 保持演示可复现）；项目记录也可写 `data_mode` | `src/ui_server.py::_data_mode`、`src/workspace_registry.py` |
| LIVE 状态 | `GET /api/state` 在 LIVE 模式经 `LiveDataService.build_live_snapshot(project)` 生成 live snapshot，再走同一五阶段管线；`meta.mode="LIVE"`，`meta.capturedAt` 为本次 live 刷新时间 | `src/ui_server.py::build_state`、`compose_state` |
| 轻量报价 | `GET /api/live-quote?codes=HK.00700` 返回 `{mode:"LIVE",capturedAt,freshness,ttlSeconds,quotes:[...]}`；仅在 `GOAI_DATA_MODE=live` 时可用 | `src/ui_server.py::Handler.do_GET`、`LiveDataService.quote_payload` |
| OpenD down | 实时链路抛 `_LiveDataError`（typed error），HTTP 503 且 `typedError.code` 为 `OPEND_UNAVAILABLE`/`ENTITLEMENT_DENIED` 等；绝不静默回退 replay | `src/ui_server.py::_send_live_error_json`、`src/live_data.py::LiveDataError` |
| 缓存 | REPLAY state 缓存 30s；LIVE state 缓存 5s；quote 缓存 3s | `src/ui_server.py::_state_cache_ttl_seconds`、`LiveDataService` |
| 通用实时库 | `LiveQuoteCache`（TTL + 失败保留旧值/stale 标记）、`LiveDataService`（`build_live_snapshot`/`refresh`，输出与 `load_frozen_snapshot` 同构的字典） | `src/live_data.py` |
| 管线复用 | `run_pipeline(..., snapshot_data=...)` 可接受已加载的 LIVE/REPLAY 快照字典，未传时保持原冻结快照路径 | `src/decision_pipeline.py::run_pipeline` |
| 工作区 | 项目记录新增可选 `data_mode`；`register_project` 接受并校验 `data_mode` | `src/workspace_registry.py` |
| UI 能力条 | 能力条读取 `meta.mode` 动态渲染 LIVE/REPLAY；`cap-run` 文案随模式切换；`meta.mode` 缺失时按回放/冻结显示，静态 `data.js` 回退不报错 | `ui/app.js::renderCapability`、`ui/index.html` |
| 审计铁律 | LIVE 模式的 `/api/run`、`/api/chat`、`/api/command` 只读不写审计、不写决策卡；REPLAY 缺省路径保持原审计行为 | `src/ui_server.py::Handler.do_POST` |
| 测试 | 新增 `tests/test_live_data.py`（通用实时库）、`tests/test_live_ui.py`（LIVE state / live-quote / OpenD down / replay 不回归） | `tests/` |

## 1. 现状盘点（冻结快照边界）

核心结论：**Gateway/Agent 数据合同已经具备 LIVE 能力**（`DataMode.LIVE`、
`FreshnessStatus.FRESH/STALE/FROZEN`、`FutuLiveGateway`、`DecisionInputService`
全部支持 live/replay 双模）。过时的“冻结快照假设”主要集中在产品入口与 UI 层；
第一阶段已把产品入口（UI 服务 + 能力条）切到 LIVE 可切换：

| 位置 | 第一阶段结果 |
|---|---|
| `src/ui_server.py` `build_state()` | LIVE 模式改用 live snapshot，REPLAY 原路径不变 |
| `src/ui_server.py` state cache | 区分 LIVE 5s / REPLAY 30s |
| `src/workspace_registry.py` | 项目支持 `data_mode` |
| `src/decision_pipeline.py` | `run_pipeline` 支持 `snapshot_data` |
| `ui/app.js` / `ui/index.html` | 能力条按 `meta.mode` 动态渲染 |
| `src/live_data.py` | 通用 LiveQuoteCache + LiveDataService（build_live_snapshot / refresh） |

不需要改动的已正确部分：`DataEnvelope` 合同、`FutuLiveGateway` 只读边界、
`DecisionInputService` 混合模式拒绝与速率/截止控制、审计链。

## 2. 目标数据流（实时）

```text
UI / Agent 请求
  → LiveDataService（TTL 缓存 + 可选订阅/轮询刷新）
  → FutuLiveGateway（长期只读 Context，127.0.0.1:11111）
  → DataEnvelope(LIVE) → build_live_snapshot（引擎输入）
  → decision_pipeline（五阶段 + 门控，来源标记 LIVE）
  → UI state / 决策卡 + /api/live-quote 轻量报价端点
```

- 缓存分层：quote 3s（`ui_server.LiveDataService`）/ `src.live_data.LiveQuoteCache` 通用 TTL + stale 回退；
  engine state LIVE 5s / REPLAY 30s。
- 可靠性：OpenD 不可用/超时 → typed error 显式返回（HTTP 503 + code），不回退静默 replay；
  用户显式切回 replay 才使用冻结快照。
- 性能：报价端点只读缓存、无管线重算；引擎重算在 LIVE 模式复用同一五阶段管线。

## 3. 接口与模型变更（第一阶段已实现）

- `GET /api/live-quote?codes=HK.00700`：轻量实时报价（价格/盘口/时间/新鲜度/TTL）。
- `GET /api/state`：LIVE 模式下 `meta.mode="LIVE"`，`meta.capturedAt` 为最近 live 刷新时间，freshness 为 FRESH/STALE。
- `POST /api/run` / `/api/chat`：LIVE 模式下先刷新 live snapshot 再跑管线，且只读不写审计/决策卡。
- 环境开关：`GOAI_DATA_MODE=live|replay`（缺省 replay 保持演示可复现）。
- `workspace_registry` 项目记录新增可选 `data_mode`。

## 4. Agent 完整性

- Agent 仍只通过 `refresh_decision_inputs` 获取数据（live/replay 双模）。
- UI 对话链路在 LIVE 模式走同一 `run_chat`，新增 live 证据与 freshness 声明。
- `POST /api/agent` 的 refresh / select_expiry / debate / ask 分支在 LIVE 模式
  均已使用 live snapshot，不再硬编码冻结快照。
- 后续增强（可选）：独立 `refresh_live` 动作与 SSE 推送；当前 LIVE 刷新入口
  为 `/api/state`、`/api/live-quote`、`POST /api/run|/api/chat|/api/command`。

## 5. 测试与验收

- 单元：`tests/test_live_data.py` 覆盖 `LiveQuoteCache` TTL/失败保留旧值、`LiveDataService.build_live_snapshot` 字段映射与 live/replay 隔离。
- 集成：`tests/test_live_ui.py` 在 fake gateway + `GOAI_DATA_MODE=live` 下覆盖 `/api/state`、`/api/live-quote`、OpenD down 显式 `OPEND_UNAVAILABLE`。
- 回归：全量 `python -m pytest -q` 保持 0 失败；REPLAY 缺省路径不回归；审计链只增不减。
- 验收：OpenD ready 时，state meta=LIVE 且 `captured_at` 为当前时间；
  OpenD down 时显式 `OPEND_UNAVAILABLE`，不伪装实时。

## 6. 2026-08-15 收口修复（对抗式审查遗留）

- `/api/command` 在 LIVE + OpenD down 时不再对 live error state 写
  `terminal.lastCommand`，返回 503 typedError。
- `/api/projects/select` 在 LIVE + OpenD down 时返回“切换成功 + stateError”，
  不再顶层 error。
- `register_project` 幂等重试纳入 `name` 相等；同 id 不同名显式报错。
- `_write_registry` 使用唯一临时文件 + `os.replace`；`register_project` 的
  load-check-write 以 per-registry 进程锁串行，并发相同注册幂等。
- `build_live_snapshot` 对 gateway 返回 code 做大小写归一化。
- 新增回归：`tests/test_live_ui_server.py` +2、`tests/test_workspace_registry.py` +3、
  `tests/test_live_data.py` +1。

## 7. 第二阶段（2026-08-16 已完成）：推送/订阅 + UI 自动刷新

把「请求时刷新 + TTL 缓存」升级为服务端推送（SSE）。核心模块
`src/live_stream.py`（模块顶部不导入 Futu SDK，测试可注入 fake）。

### 7.1 组件与数据流

```text
浏览器 EventSource("/api/stream?codes=...")
  → ui_server Handler._send_sse_stream（hello + 事件写回 + 15s 心跳）
  → LiveStreamService（hub 订阅/退订 + 按 codes 分组的 feed 生命周期）
     ├─ PollingQuoteFeed（默认）：2s diff 轮询 LiveDataService.quote_payload，
     │    仅在报价或新鲜度变化时发布 quote；typed 失败发布 error（去重，
     │    恢复后强制补一发 quote）
     └─ PushQuoteFeed（GOAI_LIVE_FEED=push）：FutuLiveGateway.start_quote_push
           （SDK subscribe + StockQuoteHandlerBase，专用 push context，不与
           同步查询 context 共享；is_first_push 立即送达当前报价）；
           推送失败/静默超时（默认 60s 无 tick）自动回退轮询并发布一次 warning
  → 前端：quote 事件就地更新报价条（spot/涨跌幅），防抖 700ms 重拉 /api/state；
    refresh 事件（POST 重跑后经 invalidate_state_cache 广播）立即重拉；
    断线由 EventSource 自动重连（retry: 3000），顶栏新增「实时推送」徽章
```

### 7.2 接口与事件契约

- `GET /api/stream?codes=HK.00700,...`：LIVE 模式专属（replay → 422）；
  `codes` 缺省 = 当前项目 live 模板的 underlying + 全部期权腿
  （`live_data.live_template_codes`）；`text/event-stream`。
- 事件：`hello`（mode/codes/pollSeconds/subscribers，OpenD down 时带 error）、
  `quote`（与 /api/live-quote 同形 payload）、`error`（typed live 失败）、
  `warning`（如 `FEED_FALLBACK_POLL` 回退）、`refresh`（POST 重算后提示重拉）。
- 保护：订阅上限（默认 8，超限 503 `STREAM_CAPACITY`）、每客户端有界队列
  （慢客户端丢新不阻塞）、断连即退订、feed 引用计数归零即停、
  codes ≤ 32 且逐码 normalize。

### 7.3 环境变量

| 变量 | 缺省 | 说明 |
|---|---|---|
| `GOAI_LIVE_FEED` | `poll` | `push` 启用真实 OpenD 订阅推送（失败/静默自动回退轮询） |
| `GOAI_LIVE_STREAM_POLL_SECONDS` | `2.0` | 轮询/diff 间隔 |
| `GOAI_LIVE_STREAM_PUSH_SILENCE_SECONDS` | `60.0` | 推送静默超时后回退轮询 |
| `GOAI_LIVE_STREAM_MAX_SUBSCRIBERS` | `8` | SSE 订阅上限 |

### 7.4 真实 OpenD 端到端冒烟（2026-08-16 实测记录）

OpenD GUI 10.9.6918（127.0.0.1:11111，行情/交易均登录，周六休市）：

1. `health`：OK / ready / qot_logined=True / trd_logined=True。
2. `build_live_snapshot`（hero 模板）：LIVE/STALE（休市诚实标注）；HK.00700
   spot 440.0（上周五收盘）；4 条期权腿真实 bid/ask/mid/IV。
3. `GET /api/live-quote?codes=HK.00700`：200，真实盘口（440.0/440.2，昨收 441.0）。
4. `GET /api/state`：200，meta=LIVE/STALE，verdict NO_TRADE，engine 对偏离模板
   spot 的 live 价格显式降级（warning 可见）。
5. OpenD down：冷缓存 `live-quote` 503 `OPEND_UNAVAILABLE`；`/api/state` 显式
   error state（无 decisionCard、不回退 replay）；热缓存 200 + freshness=STALE
   （失败保留最后好值）。OpenD 重启后同进程自愈（连接级失败丢 context 重建）。
6. `/api/stream`（push 模式）：hello → 初始 quote（STALE）→ SDK push 实时帧
   （FRESH，code 裸码经后缀映射回规范码）→ 8s 无 tick 后 `warning`
   `FEED_FALLBACK_POLL` + 轮询接管。
7. 浏览器 e2e（Playwright）：LIVE 模式顶栏「实时 / 过期」+「实时推送已连接」
   徽章 + 真实报价 440.00（-0.23%），console 0 错误；REPLAY 缺省模式徽章
   隐藏、冻结快照不回归、/api/stream 422。

### 7.5 测试与铁律

- 新增 `tests/test_live_stream.py`（16：hub 订阅/上限/慢客户端丢弃/关闭、
  轮询 diff 去重/错误去重与恢复强制推送、push 失败回退/静默 watchdog/行包装、
  服务生命周期）+ `tests/test_live_ui_server.py` SSE 端点 7 用例（replay 422、
  hello+quote 变化、模板缺省 codes、非法 codes、订阅上限 503、OpenD down
  error 事件、断连退订）。
- 铁律保持：LIVE 只读不写审计；JS 不重算数字（quote 事件只做格式化渲染，
  数字全部来自后端 payload）；数字/审计链路只增不减。

