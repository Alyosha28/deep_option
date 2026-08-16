// GOAI-DSH 宏观政策插件（插件族 · 可选）
// ---------------------------------------------------------------------------
// 工具：
//   goai_policy_library —— 政策事件库只读视图（GET /api/policy-library）：
//                          事件状态分布、核验健康报告、最近提升 ACTIVE 的事件。
//   goai_macro_watch    —— 手动执行一轮宏观来源监控（美联储/ECB RSS、FRED、BLS、
//                          SEC EDGAR、中国官方 HTML）：候选事件以 DRAFT 状态入库，
//                          不冒充已验证；dryRun=true 时只报告候选不写库。
//                          （独立 CLI 进程，不需要引擎在跑。）
// 依赖：引擎进程（127.0.0.1:8000，仅 goai_policy_library 需要；推荐与 goai-core
// 一起加载）。本插件自带懒启动，可独立注册。
// 使用：以本文件内容作为 cordis_define 的 code.host 注册（idPrefix: "goai"），
// 然后 cordis_run。与旧 goai-bridge 二选一，不要同时注册。
// ---------------------------------------------------------------------------
return {
  name: 'goai-macro',
  inject: ['subprocess', 'timer'],
  apply(ctx) {
    // ===================== shared engine client (self-contained) =====================
    // 与 goai-core.host.js 保持同步：每个 goai-* 插件独立可注册、不共享闭包状态。
    const PORT = 8000
    const base = 'http://127.0.0.1:' + PORT
    const envRoot = (typeof process !== 'undefined' && process.env && process.env.GOAI_PROJECT_ROOT) || null
    const root = envRoot || 'F:\\GOAi_competition'
    if (!envRoot) console.log('[goai-macro] GOAI_PROJECT_ROOT 未设置，使用默认路径 ' + root + '；换机部署请先设置该环境变量再注册插件')
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

    async function apiGet(path, timeoutMs) {
      const c = await getCurl()
      if (c.error) return { ok: false, detail: c.error }
      const t = (timeoutMs || 20000) + 10000
      const res = await exec(
        [c.exe, '-s', '--max-time', String(Math.ceil((timeoutMs || 20000) / 1000)), base + path],
        { timeoutMs: t },
      )
      if (res.error) return { ok: false, detail: res.error }
      if (res.exitCode !== 0) return { ok: false, detail: 'curl 退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-2000) }
      try {
        const body = JSON.parse(res.stdout.text)
        if (body && body.error) return { ok: false, detail: '引擎返回错误: ' + String(body.error) }
        return { ok: true, json: body, truncated: res.stdout.truncated }
      } catch (_) {
        return { ok: false, detail: '响应不是 JSON: ' + res.stdout.text.slice(0, 500) }
      }
    }

    function engineStderrTail() {
      if (!engineHandle || !engineHandle.collected || !engineHandle.collected.stderr) return ''
      const r = engineHandle.collected.stderr.readFrom(0)
      return r.text.slice(-2000)
    }

    async function resolvePython() {
      let py = null
      try { py = await ctx.subprocess.resolveExecutable(venvPython) } catch (_) { py = null }
      if (!py) {
        try { py = await ctx.subprocess.resolveExecutable('python') } catch (_) { py = null }
      }
      return py
    }

    function ensureEngine() {
      if (engineHandle && engineExited && !engineExternal) {
        console.log('[goai-macro] 引擎进程已退出，重新拉起')
        engineHandle = null
        engineExited = false
        booting = null
      }
      if (booting) return booting
      booting = (async () => {
        try {
          const h = await apiGet('/api/state', 15000)
          if (h.ok) {
            if (!engineHandle) engineExternal = true
            return { ok: true }
          }
          const py = await resolvePython()
          if (!py) return { ok: false, detail: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
          console.log('[goai-macro] 启动引擎: ' + py + ' -m src.ui_server --port ' + PORT)
          engineHandle = ctx.subprocess.spawn({
            argv: [py, '-m', 'src.ui_server', '--port', String(PORT)],
            cwd: root,
            stdio: {
              stdin: 'ignore',
              stdout: { maxBytes: 65536, spill: { maxBytes: 5242880 } },
              stderr: { maxBytes: 65536, spill: { maxBytes: 5242880 } },
            },
            graceMs: 3000,
          })
          engineExited = false
          engineExternal = false
          engineHandle.done.then(() => { engineExited = true }, () => { engineExited = true })
          for (let i = 0; i < 40; i++) {
            await ctx.timeout(1000)
            if (engineExited) {
              const h2 = await apiGet('/api/state', 12000)
              if (h2.ok) {
                engineHandle = null
                engineExternal = true
                return { ok: true }
              }
              return { ok: false, detail: '引擎进程提前退出，stderr: ' + engineStderrTail() }
            }
            const h2 = await apiGet('/api/state', 12000)
            if (h2.ok) return { ok: true }
          }
          return { ok: false, detail: '引擎 40 次探测内未就绪（每次最多约 23 秒，最坏约 15 分钟），stderr: ' + engineStderrTail() }
        } catch (err) {
          return { ok: false, detail: '引擎启动异常: ' + String(err && err.message ? err.message : err) }
        }
      })()
      booting.then((res) => { if (!res.ok) booting = null }, () => { booting = null })
      return booting
    }

    ctx.effect(() => () => {
      if (engineHandle && !engineExternal) {
        try { engineHandle.terminate() } catch (_) {}
      }
    })
    // ===================== end shared engine client =====================

    const register = (tool) => harness.registerTool(ctx, harness.defineTool(tool))

    function summarizeLibrary(v) {
      if (!v) return 'goai 无结果'
      if (v.error) return 'goai 失败: ' + String(v.error)
      const lines = []
      lines.push('policyLibrary=' + String(v.eventCount ?? '-') + ' events | libraryStatus=' + String(v.libraryStatus ?? '-') + ' | verifiedShare=' + String(v.verifiedSharePct ?? '-') + '%')
      const st = v.eventStatus || {}
      const statusLine = Object.keys(st).map((k) => k + '=' + String(st[k])).join(' ')
      if (statusLine) lines.push('eventStatus: ' + statusLine)
      if (Array.isArray(v.recentlyPromoted) && v.recentlyPromoted.length) {
        lines.push('recentlyPromoted:')
        for (const ev of v.recentlyPromoted) lines.push('  ' + String(ev.id) + ' | ' + String(ev.name) + ' | ' + String(ev.promoted_at))
      }
      const problems = v.problems || {}
      const problemLine = ['missingSource', 'missingUrl', 'missingRetrievedAt', 'stale'].filter((k) => problems[k] > 0).map((k) => k + '=' + String(problems[k])).join(' ')
      if (problemLine) lines.push('healthProblems: ' + problemLine)
      if (!problemLine) lines.push('healthProblems: none')
      return lines.join('\n')
    }

    register({
      name: 'goai_policy_library',
      description: '读取 GOAI 政策事件库：事件总数、状态分布（ACTIVE/DRAFT/FAILED）、核验健康报告（VERIFIED 占比、缺来源/URL/抓取时间、过期事件）与最近提升 ACTIVE 的事件。只读，不写审计。',
      parameters: { type: 'object', properties: {}, additionalProperties: true },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: summarizeLibrary(v) }] },
      },
      async execute() {
        const boot = await ensureEngine()
        if (!boot.ok) return { ok: false, error: boot.detail }
        const r = await apiGet('/api/policy-library', 30000)
        if (!r.ok) return { ok: false, error: r.detail }
        const j = r.json || {}
        const health = (j.health && typeof j.health === 'object') ? j.health : {}
        const problems = {
          missingSource: Array.isArray(health.facts_without_source) ? health.facts_without_source.length : 0,
          missingUrl: Array.isArray(health.facts_without_url) ? health.facts_without_url.length : 0,
          missingRetrievedAt: Array.isArray(health.facts_without_retrieved_at) ? health.facts_without_retrieved_at.length : 0,
          stale: Array.isArray(health.stale_events) ? health.stale_events.length : 0,
        }
        return {
          ok: true,
          truncated: !!r.truncated,
          path: j.path ?? null,
          eventCount: j.eventCount ?? null,
          eventStatus: (health.event_status && typeof health.event_status === 'object') ? health.event_status : null,
          libraryStatus: health.library_status ?? null,
          verifiedSharePct: health.verified_share_pct ?? null,
          recentlyPromoted: Array.isArray(health.recently_promoted) ? health.recently_promoted.slice(0, 5) : [],
          problems,
        }
      },
    })

    register({
      name: 'goai_macro_watch',
      description: '手动执行一轮 GOAI 宏观来源监控（美联储/ECB 官方 RSS、FRED、BLS、SEC EDGAR、中国央行/统计局/海关 HTML）：候选事件以 DRAFT 状态入库、核验 PENDING，不冒充已验证；单个来源失败只记 FAIL 不中断整轮。dryRun=true 时只报告候选不写库。需要外网访问，可能耗时 1-3 分钟。',
      parameters: {
        type: 'object',
        properties: {
          dryRun: { type: 'boolean', description: 'true 时只报告候选，不写事件库' },
          maxItems: { type: 'number', description: '每来源最多入库条数（默认 3）' },
        },
        additionalProperties: true,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: v.error ? ('goai 失败: ' + String(v.error)) : String(v.stdoutTail || '') }] },
      },
      async execute(args) {
        const py = await resolvePython()
        if (!py) return { ok: false, error: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
        const argv = [py, '-m', 'src.macro_source_watcher']
        if (args && args.dryRun) argv.push('--dry-run')
        else argv.push('--run-once')
        argv.push('--max-items', String(args && args.maxItems ? args.maxItems : 3))
        const res = await exec(argv, { timeoutMs: 180000 })
        if (res.error) return { ok: false, error: res.error }
        if (res.exitCode !== 0) {
          return { ok: false, error: '监控进程退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-1500) }
        }
        return { ok: true, exitCode: res.exitCode, stdoutTail: res.stdout.text.slice(-3000) }
      },
    })
  },
}
