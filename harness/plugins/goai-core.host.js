// GOAI-DSH 核心插件（插件族 · Base Mode 必备）
// ---------------------------------------------------------------------------
// 插件族背景：DSH 底层是 Cordis 内核，每个 goai-* 插件都是独立可注册的 Cordis
// 插件（{name, inject, apply(ctx)}）。用户通过 harness/config/goai.plugins.json
// 选择加载哪些模块；Base Mode = goai-core + goai-run + goai-chat（等价旧单体
// goai-bridge 的三工具）。
//
// 本插件（goai-core）职责：
//   · 引擎生命周期：懒启动 → 健康检查 → 失败才 spawn；ctx.effect 持有终止句柄
//     （可逆效应：插件停用/会话结束自动回收子进程）；已运行的引擎复用且不误杀。
//   · HTTP 桥：curl.exe 子进程 GET/POST 统一封装（POST 请求体走 stdin {data}，
//     避开 Windows 命令行中文编码问题）。
//   · goai_state：读取决策终端状态（GET /api/state，内存重算不写审计）。
//
// 使用：以本文件内容作为 cordis_define 的 code.host 注册（idPrefix: "goai"），
// 然后 cordis_run（host-only，无需审批）。与旧 goai-bridge 二选一，不要同时注册。
// ---------------------------------------------------------------------------
return {
  name: 'goai-core',
  inject: ['subprocess', 'timer'],
  apply(ctx) {
    // ===================== shared engine client (self-contained) =====================
    // 与 goai-run / goai-chat / goai-macro / goai-research / goai-backtest 保持同步：
    // 每个 goai-* 插件独立可注册、不共享闭包状态，所以此块在每个插件文件里各有一份。
    // 设计：懒启动 → 健康检查 → 失败才 spawn；已在运行的引擎被复用（external 模式）。
    // 可逆效应：仅回收本插件自己 spawn 的引擎进程；external 引擎不误杀。
    const PORT = 8000
    const base = 'http://127.0.0.1:' + PORT
    // 项目根目录：优先读环境变量 GOAI_PROJECT_ROOT（跨机部署在启动 DSH 前设置），
    // 未设置时回退到本仓库默认路径并打印提示。
    const envRoot = (typeof process !== 'undefined' && process.env && process.env.GOAI_PROJECT_ROOT) || null
    const root = envRoot || 'F:\\GOAi_competition'
    if (!envRoot) console.log('[goai-core] GOAI_PROJECT_ROOT 未设置，使用默认路径 ' + root + '；换机部署请先设置该环境变量再注册插件')
    const venvPython = root + '\\.venv\\Scripts\\python.exe'

    let engineHandle = null
    let engineExited = false
    let engineExternal = false
    let booting = null
    let curlExe = null
    let curlError = null

    async function getCurl() {
      if (curlExe) return { exe: curlExe }
      // 解析失败不永久缓存：下一次调用重新解析（curl 安装/进 PATH 后自愈）
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

    async function apiPost(path, body, timeoutMs) {
      const c = await getCurl()
      if (c.error) return { ok: false, detail: c.error }
      const bodyText = body === undefined ? '' : JSON.stringify(body)
      const argv = [c.exe, '-s', '--max-time', String(Math.ceil((timeoutMs || 20000) / 1000)), '-X', 'POST', '-H', 'Content-Type: application/json']
      if (bodyText) argv.push('--data-binary', '@-')
      argv.push(base + path)
      const res = await exec(argv, { timeoutMs: (timeoutMs || 20000) + 15000, stdin: { data: bodyText } })
      if (res.error) return { ok: false, detail: res.error }
      if (res.exitCode !== 0) return { ok: false, detail: 'curl 退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-2000) }
      try {
        const parsed = JSON.parse(res.stdout.text)
        if (parsed && parsed.error) return { ok: false, detail: '引擎返回错误: ' + String(parsed.error) }
        return { ok: true, json: parsed, truncated: res.stdout.truncated }
      } catch (_) {
        return { ok: false, detail: '响应不是 JSON: ' + res.stdout.text.slice(0, 500) }
      }
    }

    function engineStderrTail() {
      if (!engineHandle || !engineHandle.collected || !engineHandle.collected.stderr) return ''
      const r = engineHandle.collected.stderr.readFrom(0)
      return r.text.slice(-2000)
    }

    // 返回可用的 Python 解释器路径（优先 .venv）
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
        // 引擎启动成功后崩溃（booting 已 resolved）：丢弃句柄，重新拉起
        console.log('[goai-core] 引擎进程已退出，重新拉起')
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
          console.log('[goai-core] 启动引擎: ' + py + ' -m src.ui_server --port ' + PORT)
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
              // 端口被并发启动的另一实例占用时，本实例会快速退出：若引擎实际已就绪，
              // 视为 external 复用，不报错（多插件并发首调的自愈路径）。
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

    // 紧凑投影：只保留叶字段，LLM 通过摘要文本读取引擎数字。
    function projectState(json) {
      const s = json || {}
      const card = s.decisionCard || {}
      const meta = s.meta || {}
      const llm = s.llm || {}
      const eg = card.edgeGate || {}
      const rg = card.riskGate || {}
      const ag = card.actionGate || {}
      const debate = (s.debateTrace && typeof s.debateTrace === 'object' && Object.keys(s.debateTrace).length > 0) ? s.debateTrace : null
      const consensus = (s.researchConsensus && typeof s.researchConsensus === 'object' && Object.keys(s.researchConsensus).length > 0) ? s.researchConsensus : null
      return {
        verdict: typeof card.verdict === 'string' ? card.verdict : null,
        summary: typeof card.summary === 'string' ? card.summary : null,
        edgeGate: { verdict: eg.verdict ?? null, recommendation: eg.recommendation ?? null, checkCount: Array.isArray(eg.checks) ? eg.checks.length : null },
        riskGate: {
          decision: rg.decision ?? null,
          blocked: Array.isArray(rg.blocked) ? rg.blocked.length > 0 : !!rg.blocked,
          findingCount: Array.isArray(rg.findings) ? rg.findings.length : null,
          warnCount: Array.isArray(rg.findings) ? rg.findings.filter((f) => f && f.kind === 'WARN').length : null,
        },
        actionGate: {
          action: ag.action ?? null,
          blocked: Array.isArray(ag.blocked) ? ag.blocked.length > 0 : !!ag.blocked,
          blockedReasons: Array.isArray(ag.blocked) ? ag.blocked.slice(0, 3).map((x) => String(x)) : null,
          next_step: ag.next_step ?? null,
        },
        scenario: (s.scenario && typeof s.scenario === 'object') ? s.scenario : null,
        snapshot: {
          sha256: meta.snapshotSha256 ?? null,
          capturedAt: meta.capturedAt ?? null,
          freshness: meta.freshness ?? null,
          mode: meta.mode ?? null,
        },
        llm: { available: !!llm.available, provider: llm.provider ?? null, status: llm.status ?? null },
        debate: debate ? {
          status: debate.status ?? null,
          fallback_reason: debate.fallback_reason ?? null,
          total_tokens: debate.total_tokens ?? null,
          consensus: consensus ? {
            source: consensus.source ?? null,
            stance: consensus.stance ?? null,
            confidence: consensus.confidence ?? null,
            summary: typeof consensus.summary === 'string' ? consensus.summary : null,
          } : null,
        } : null,
      }
    }

    function summarize(v) {
      if (!v) return 'goai 无结果'
      if (v.error) return 'goai 失败: ' + String(v.error)
      const lines = []
      lines.push('verdict=' + String(v.verdict ?? '-'))
      lines.push('summary=' + String(v.summary || '').slice(0, 240))
      lines.push('edge=' + String(v.edgeGate.verdict ?? '-') + ' | risk=' + String(v.riskGate.decision ?? '-') + (v.riskGate.warnCount ? ' (' + v.riskGate.warnCount + ' WARN)' : '') + (v.riskGate.blocked ? ' BLOCKED' : ''))
      lines.push('action=' + String(v.actionGate.action ?? '-') + (v.actionGate.blocked ? ' BLOCKED' + (v.actionGate.blockedReasons && v.actionGate.blockedReasons.length ? ': ' + v.actionGate.blockedReasons.join('; ').slice(0, 160) : '') : ''))
      lines.push('snapshot=' + String(v.snapshot.sha256 || '').slice(0, 12) + ' captured=' + String(v.snapshot.capturedAt || '-') + ' freshness=' + String(v.snapshot.freshness || '-'))
      if (v.llm && v.llm.available) lines.push('llm=' + String(v.llm.provider) + '/' + String(v.llm.status))
      if (v.debate) {
        lines.push('debate=' + String(v.debate.status) + (v.debate.fallback_reason ? ' (fallback: ' + v.debate.fallback_reason + ')' : ''))
        if (v.debate.consensus) {
          lines.push('consensus=' + String(v.debate.consensus.stance ?? '-') + '/' + String(v.debate.consensus.confidence ?? '-') + ': ' + String(v.debate.consensus.summary || '').slice(0, 240))
        }
      }
      return lines.join('\n')
    }

    const finish = (res) => ({
      ok: true,
      engine: engineExternal ? 'external' : 'spawned',
      truncated: !!res.truncated,
      ...projectState(res.json),
    })

    const register = (tool) => harness.registerTool(ctx, harness.defineTool(tool))

    register({
      name: 'goai_state',
      description: '读取 GOAI 期权决策终端当前状态：决策卡 verdict 与 Edge/Risk/Action 三门控、快照身份与新鲜度、LLM 徽章、辩论状态。用冻结快照在内存重算管线，不写审计。首次调用自动拉起本地引擎（127.0.0.1:8000，引擎随插件生命周期自动回收）。',
      parameters: { type: 'object', properties: {}, additionalProperties: true },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: summarize(v) }] },
      },
      async execute() {
        const boot = await ensureEngine()
        if (!boot.ok) return { ok: false, error: boot.detail }
        const r = await apiGet('/api/state', 30000)
        if (!r.ok) return { ok: false, error: r.detail }
        return finish(r)
      },
    })
  },
}
