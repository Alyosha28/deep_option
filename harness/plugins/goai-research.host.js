// GOAI-DSH 投研证据插件（插件族 · 可选）
// ---------------------------------------------------------------------------
// 工具：
//   goai_research_evidence —— 投研证据包：新闻/公告/研报/行业数据 → 股价与期权
//                             影响研判（LLM 只出文字，检查项由引擎产出）。
//   goai_research_sources  —— 把 futu-news-search / futu-stock-digest 的真实输出
//                             （api-json 或 markdown 文件）转成 canonical 条目，
//                             只做格式转换与来源留痕，不改写标题/时间/链接。
// 依赖：引擎进程（127.0.0.1:8000，仅 goai_research_evidence 需要；推荐与
// goai-core 一起加载）。本插件自带懒启动，可独立注册。
// 使用：以本文件内容作为 cordis_define 的 code.host 注册（idPrefix: "goai"），
// 然后 cordis_run。与旧 goai-bridge 二选一，不要同时注册。
// ---------------------------------------------------------------------------
return {
  name: 'goai-research',
  inject: ['subprocess', 'timer'],
  apply(ctx) {
    // ===================== shared engine client (self-contained) =====================
    // 与 goai-core.host.js 保持同步：每个 goai-* 插件独立可注册、不共享闭包状态。
    const PORT = 8000
    const base = 'http://127.0.0.1:' + PORT
    const envRoot = (typeof process !== 'undefined' && process.env && process.env.GOAI_PROJECT_ROOT) || null
    const root = envRoot || 'F:\\GOAi_competition'
    if (!envRoot) console.log('[goai-research] GOAI_PROJECT_ROOT 未设置，使用默认路径 ' + root + '；换机部署请先设置该环境变量再注册插件')
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
        console.log('[goai-research] 引擎进程已退出，重新拉起')
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
          console.log('[goai-research] 启动引擎: ' + py + ' -m src.ui_server --port ' + PORT)
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

    function renderCli(v) {
      if (!v) return 'goai 无结果'
      if (v.error) return 'goai 失败: ' + String(v.error)
      return String(v.stdoutTail || '(无输出)')
    }

    register({
      name: 'goai_research_evidence',
      description: '生成 GOAI 投研证据包：把研究条目（新闻/公告/研报/行业数据）与回测摘要整理为可审计证据，输出股价影响与期权影响研判及检查项。items/backtest 为可选 JSON 路径，缺省用仓库默认演示数据。',
      parameters: {
        type: 'object',
        properties: {
          items: { type: 'string', description: '研究条目 canonical JSON 路径（缺省 data/research_items_hero.json）' },
          backtest: { type: 'string', description: '回测摘要 JSON 路径（缺省 data/backtest_tencent_straddle.json）' },
          noAudit: { type: 'boolean', description: 'true 时不写审计链' },
        },
        additionalProperties: true,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: renderCli(v) }] },
      },
      async execute(args) {
        const py = await resolvePython()
        if (!py) return { ok: false, error: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
        const argv = [py, '-m', 'src.research_evidence']
        if (args && args.items) argv.push('--items', String(args.items))
        if (args && args.backtest) argv.push('--backtest', String(args.backtest))
        if (args && args.noAudit) argv.push('--no-audit')
        const res = await exec(argv, { timeoutMs: 120000 })
        if (res.error) return { ok: false, error: res.error }
        if (res.exitCode !== 0) {
          return { ok: false, error: '进程退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-1500) }
        }
        return { ok: true, exitCode: res.exitCode, stdoutTail: res.stdout.text.slice(-3500) }
      },
    })

    register({
      name: 'goai_research_sources',
      description: '把 futu-news-search / futu-stock-digest 的真实输出转成 GOAI canonical 研究条目：只做格式转换与来源留痕，不改写标题、时间或链接；缺发布时间但有原文 URL 的条目标记 publish_time_unknown。sourceType=api-json 时 file 为 /news_search 响应 JSON 路径，sourceType=markdown 时 file 为 skill 输出文本路径。',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string', description: '检索关键词/标的，如 Tencent' },
          sourceType: { type: 'string', enum: ['api-json', 'markdown'], description: '输入文件类型' },
          file: { type: 'string', description: '输入文件路径（api-json 或 markdown）' },
          out: { type: 'string', description: '输出 canonical JSON 路径（缺省 data/research_items_futu.json）' },
          synthetic: { type: 'boolean', description: 'true 时标记为演示数据（synthetic=True），不冒充真实市场证据' },
        },
        required: ['keyword', 'sourceType', 'file'],
        additionalProperties: true,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_a, v) { return [{ type: 'text', text: renderCli(v) }] },
      },
      async execute(args) {
        if (!args || !args.keyword || !args.sourceType || !args.file) {
          return { ok: false, error: '缺少必填参数 keyword/sourceType/file' }
        }
        const py = await resolvePython()
        if (!py) return { ok: false, error: '找不到 Python 解释器（.venv 缺失且 PATH 无 python）' }
        const argv = [py, '-m', 'src.research_sources', '--keyword', String(args.keyword)]
        if (args.sourceType === 'api-json') argv.push('--api-json', String(args.file))
        else argv.push('--markdown', String(args.file))
        if (args.out) argv.push('--out', String(args.out))
        if (args.synthetic) argv.push('--synthetic')
        const res = await exec(argv, { timeoutMs: 90000 })
        if (res.error) return { ok: false, error: res.error }
        if (res.exitCode !== 0) {
          return { ok: false, error: '进程退出码 ' + res.exitCode + '; stderr: ' + res.stderr.text.slice(-1500) }
        }
        return { ok: true, exitCode: res.exitCode, stdoutTail: res.stdout.text.slice(-3000) }
      },
    })
  },
}
