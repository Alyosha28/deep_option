// GOAI-DSH 历史回测插件（插件族 · 可选）
// ---------------------------------------------------------------------------
// 工具：goai_backtest —— 重跑腾讯 0700 业绩跨式历史回测（决策支持，模拟盘基准）：
//   口径 A（引擎）：11 个历史财报期，业绩前 1 日 S_pre + 当日期权 IV，自研引擎定价
//                   ATM 跨式，加 5% ask 滑点，业绩后第 1/2/5 交易日按内在价值平仓。
//   口径 B（市场预期代理）：19 个财报期，成本 = 市场预期波动 × 1.05，
//                   平仓 = |S_post - S_pre|。
//   无未来函数；结果写 data/backtest_tencent_straddle.json + 审计链。
// 依赖：独立 CLI 进程，不需要引擎在跑（引擎为可选项，本插件不拉起引擎）。
// 使用：以本文件内容作为 cordis_define 的 code.host 注册（idPrefix: "goai"），
// 然后 cordis_run。与旧 goai-bridge 二选一，不要同时注册。
// ---------------------------------------------------------------------------
return {
  name: 'goai-backtest',
  inject: ['subprocess', 'timer'],
  apply(ctx) {
    // ===================== shared engine client (self-contained) =====================
    // 与 goai-core.host.js 保持同步：每个 goai-* 插件独立可注册、不共享闭包状态。
    const PORT = 8000
    const base = 'http://127.0.0.1:' + PORT
    const envRoot = (typeof process !== 'undefined' && process.env && process.env.GOAI_PROJECT_ROOT) || null
    const root = envRoot || 'F:\\GOAi_competition'
    if (!envRoot) console.log('[goai-backtest] GOAI_PROJECT_ROOT 未设置，使用默认路径 ' + root + '；换机部署请先设置该环境变量再注册插件')
    const venvPython = root + '\\.venv\\Scripts\\python.exe'

    let engineHandle = null
    let engineExited = false
    let engineExternal = false
    let booting = null
    let curlExe = null
    let curlError = null

    async function getCurl() {
      if (curlExe) return { exe: curlExe }
      try {
        curlExe = await ctx.subprocess.resolveExecutable('curl.exe')
        curlError = null
        return { exe: curlExe }
      } catch (err) {
        curlError = '找不到 curl.exe: ' + String(err && err.message ? err.message : err)
        return { error: curlError }
      }
    }

    async function exec(argv, opts = {}) {
      const timeoutMs = opts.timeoutMs || 60000
      let handle
      try {
        handle = ctx.subprocess.spawn({
          argv,
          cwd: root,
          stdio: {
            stdin: opts.stdin || 'ignore',
            stdout: { maxBytes: 4194304, spill: { maxBytes: 8388608 } },
            stderr: { maxBytes: 65536, spill: { maxBytes: 1048576 } },
          },
          graceMs: 3000,
          ...(opts.env ? { env: opts.env } : {}),
        })
      } catch (err) {
        return { error: 'spawn 失败: ' + String(err && err.message ? err.message : err) }
      }
      let outcome
      try {
        outcome = await Promise.race([
          handle.done,
          ctx.timeout(timeoutMs).then(() => ({ timedOut: true })),
        ])
      } catch (err) {
        return { error: '进程异常: ' + String(err && err.message ? err.message : err) }
      }
      if (outcome && outcome.timedOut) {
        try { handle.terminate() } catch (_) {}
        return { error: '超时 ' + timeoutMs + 'ms，已终止进程 (pid ' + handle.pid + ')' }
      }
      const read = (reader) => {
        if (!reader) return { text: '', truncated: false, spillPath: undefined }
        const r = reader.readFrom(0)
        return { text: r.text, truncated: r.lossy, spillPath: r.spillPath }
      }
      return {
        exitCode: outcome.exitCode,
        signal: outcome.signal,
        stdout: read(handle.collected.stdout),
        stderr: read(handle.collected.stderr),
      }
    }

    async function resolvePython() {
      let py = null
      try { py = await ctx.subprocess.resolveExecutable(venvPython) } catch (_) { py = null }
      if (!py) {
        try { py = await ctx.subprocess.resolveExecutable('python') } catch (_) { py = null }
      }
      return py
    }

    ctx.effect(() => () => {
      if (engineHandle && !engineExternal) {
        try { engineHandle.terminate() } catch (_) {}
      }
    })
    // ===================== end shared engine client =====================

    const register = (tool) => harness.registerTool(ctx, harness.defineTool(tool))

    register({
      name: 'goai_backtest',
      description: '重跑腾讯 0700 业绩跨式历史回测（自研引擎定价，无未来函数，模拟盘基准）：输出买入/卖出跨式在业绩后 d+1/d+2/d+5 平仓的平均 ROI 与胜率，含按年度拆分。结果写 data/backtest_tencent_straddle.json 与审计链。历史回测不代表未来，非投资建议。',
      parameters: { type: 'object', properties: {}, additionalProperties: true },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) {
          if (!v) return [{ type: 'text', text: 'goai 无结果' }]
          if (v.error) return [{ type: 'text', text: 'goai 失败: ' + String(v.error) }]
          return [{ type: 'text', text: String(v.stdoutTail || '(无输出)') }]
        },
      },
      async execute() {
        const py = await resolvePython()
        if (!py) return { ok: false, error: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
        const res = await exec([py, '-m', 'src.backtest_tencent_straddle'], { timeoutMs: 120000 })
        if (res.error) return { ok: false, error: res.error }
        if (res.exitCode !== 0) {
          return { ok: false, error: '回测进程退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-1500) }
        }
        return { ok: true, exitCode: res.exitCode, stdoutTail: res.stdout.text.slice(-3500) }
      },
    })
  },
}
