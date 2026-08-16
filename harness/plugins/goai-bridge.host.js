// GOAI-DSH 桥接插件（Phase 0 单体版 · LEGACY）
// ---------------------------------------------------------------------------
// 已被插件族取代：goai-core + goai-run + goai-chat（Base Mode）功能等价本插件，
// 并新增 goai-macro / goai-research / goai-backtest 可选插件。本文件保留用于
// 兼容旧注册流程（bootstrap 未找到 config/goai.plugins.json 时的回退）。
// 注意：与本插件族二选一，不要同时注册（工具名重叠）。
// ---------------------------------------------------------------------------
// 作用：把 GOAI 的 Python 引擎（src/ui_server.py, 127.0.0.1:8000）以 DSH model tools 形式暴露：
//   goai_state  读取决策终端状态（GET /api/state，内存重算不写审计）
//   goai_run    重跑五阶段管线（POST /api/run，默认写审计与决策卡）
//   goai_chat   对话链路（POST /api/chat，场景解析 + 管线 + 十角色辩论，无 key 离线回退）
// 生命周期：引擎由本插件用 ctx.subprocess.spawn 拉起，ctx.effect 持有终止句柄
// （可逆效应：插件停用/会话结束自动回收子进程）；已运行的引擎被复用且不被误杀。
// 结果契约：execute 返回紧凑投影（叶字段），render 输出实质摘要——LLM 通过 render 文本读取
// 引擎数字，任何数字都不在 JS 层重算。
// 使用：以本文件内容作为 cordis_define 的 code.host 注册为动态插件并 cordis_run。
return {
  name: 'goai-bridge',
  inject: ['subprocess', 'timer'],
  apply(ctx) {
    const PORT = 8000
    const base = 'http://127.0.0.1:' + PORT
    // 项目根目录：优先读环境变量 GOAI_PROJECT_ROOT（跨机部署在启动 DSH 前设置），
    // 未设置时回退到本仓库默认路径并打印提示。
    const envRoot = (typeof process !== 'undefined' && process.env && process.env.GOAI_PROJECT_ROOT) || null
    const root = envRoot || 'F:\\GOAi_competition'
    if (!envRoot) console.log('[goai-bridge] GOAI_PROJECT_ROOT 未设置，使用默认路径 ' + root + '；换机部署请先设置该环境变量再注册本插件')
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

    function ensureEngine() {
      if (engineHandle && engineExited && !engineExternal) {
        // 引擎启动成功后崩溃（booting 已 resolved）：丢弃句柄，重新拉起
        console.log('[goai-bridge] 引擎进程已退出，重新拉起')
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
          let py = null
          try { py = await ctx.subprocess.resolveExecutable(venvPython) } catch (_) { py = null }
          if (!py) {
            try { py = await ctx.subprocess.resolveExecutable('python') } catch (_) { py = null }
          }
          if (!py) return { ok: false, detail: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
          console.log('[goai-bridge] 启动引擎: ' + py + ' -m src.ui_server --port ' + PORT)
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

    const finish = (res, truncated) => ({
      ok: true,
      engine: engineExternal ? 'external' : 'spawned',
      truncated: !!truncated,
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
        return finish(r, r.truncated)
      },
    })

    register({
      name: 'goai_run',
      description: '重跑 GOAI 五阶段决策管线（场景解析→冻结快照→自研引擎→Edge/Risk/Action 门控→决策卡）。默认写审计链并落盘决策卡；noAudit=true 时只计算不写。verdict 可能为 NO_TRADE/BLOCK/DRAFT_ONLY/READY_FOR_CONFIRMATION。',
      parameters: {
        type: 'object',
        properties: { noAudit: { type: 'boolean', description: 'true 时只重算不写审计与决策卡文件' } },
        additionalProperties: true,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: summarize(v) }] },
      },
      async execute(args) {
        const boot = await ensureEngine()
        if (!boot.ok) return { ok: false, error: boot.detail }
        const path = args && args.noAudit ? '/api/run?no_audit=1' : '/api/run'
        const r = await apiPost(path, {}, 300000)
        if (!r.ok) return { ok: false, error: r.detail }
        return finish(r, r.truncated)
      },
    })

    register({
      name: 'goai_chat',
      description: 'GOAI 对话链路：自然语言 → 确定性场景解析 → 五阶段管线（全部数字与 verdict 来自引擎）→ 十角色辩论（LLM 只产出文字结论与证据引用，无 DeepSeek key 自动离线回退）。message 为期权场景描述，例如“腾讯业绩前方向不确定，账户10万港币，评估跨式”。默认写审计链。',
      parameters: {
        type: 'object',
        properties: {
          message: { type: 'string', description: '自然语言期权场景描述' },
          noAudit: { type: 'boolean', description: 'true 时不写审计' },
        },
        required: ['message'],
        additionalProperties: true,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: summarize(v) }] },
      },
      async execute(args) {
        const boot = await ensureEngine()
        if (!boot.ok) return { ok: false, error: boot.detail }
        const path = args && args.noAudit ? '/api/chat?no_audit=1' : '/api/chat'
        const r = await apiPost(path, { message: String(args && args.message || '') }, 420000)
        if (!r.ok) return { ok: false, error: r.detail }
        return finish(r, r.truncated)
      },
    })
  },
}
