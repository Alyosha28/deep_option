// GOAI plugin family smoke test (run WITHOUT DSH: mocks ctx/harness, uses real curl + real engine)
// Usage:  node harness/smoke_plugins.mjs [toolName...]
// Default: goai_state goai_policy_library (read-only tools against a running engine on 127.0.0.1:8000)
// Examples:
//   node harness/smoke_plugins.mjs                                  # read-only default set
//   node harness/smoke_plugins.mjs goai_run                         # full pipeline, noAudit=true
//   node harness/smoke_plugins.mjs goai_backtest                    # runs the backtest CLI (writes JSON + audit)
//   node harness/smoke_plugins.mjs goai_chat                        # chat (needs engine; offline fallback ok)
// Exit code 0 when every requested tool executes without throwing and returns ok:true.
import { readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const pluginDir = path.join(root, 'harness', 'plugins')
const configPath = path.join(root, 'harness', 'config', 'goai.plugins.json')

const config = JSON.parse(readFileSync(configPath, 'utf8'))
const requested = process.argv.slice(2)
const want = (tool) => requested.length === 0
  ? ['goai_state', 'goai_policy_library'].includes(tool)          // read-only default set
  : requested.includes(tool)

const disposers = []
function makeCtx(name) {
  const handles = new Set()
  return {
    name,
    subprocess: {
      async resolveExecutable(exe) {
        if (exe === 'curl.exe') return 'curl.exe'
        const candidate = path.join(root, '.venv', 'Scripts', 'python.exe')
        if (exe === candidate) return candidate
        return exe
      },
      spawn(opts) {
        const child = spawn(opts.argv[0], opts.argv.slice(1), { cwd: opts.cwd || root, stdio: ['pipe', 'pipe', 'pipe'] })
        // real DSH subprocess writes stdin data then closes it; mimic that so
        // curl `--data-binary @-` does not wait forever for EOF
        if (opts.stdin && opts.stdin.data !== undefined && child.stdin) {
          child.stdin.write(opts.stdin.data)
        }
        if (child.stdin) child.stdin.end()
        let stdout = '', stderr = '', settled = false
        child.stdout.on('data', (d) => { stdout += d.toString() })
        child.stderr.on('data', (d) => { stderr += d.toString() })
        const handle = {
          pid: child.pid,
          collected: {
            stdout: { readFrom: () => ({ text: stdout, lossy: false }) },
            stderr: { readFrom: () => ({ text: stderr, lossy: false }) },
          },
          done: new Promise((resolve) => {
            child.on('close', (code, signal) => { settled = true; resolve({ exitCode: code, signal }) })
            child.on('error', (err) => { settled = true; resolve({ exitCode: -1, signal: String(err) }) })
          }),
          terminate() { if (!settled) child.kill() },
        }
        handles.add(handle)
        return handle
      },
    },
    timeout: (ms) => new Promise((r) => setTimeout(r, ms)),
    effect(fn) { disposers.push(fn) },
  }
}

let failures = 0
for (const [pluginName, meta] of Object.entries(config.plugins)) {
  const tools = meta.tools || []
  if (!tools.some(want)) continue
  const code = readFileSync(path.join(root, 'harness', meta.file), 'utf8')
  const plugin = new Function(code)() // plugin files are `return { name, inject, apply(ctx) }` blocks
  const ctx = makeCtx(pluginName)
  const registered = []
  globalThis.harness = {
    defineTool: (t) => t,
    registerTool: (_c, t) => registered.push(t),
  }
  await plugin.apply(ctx) // eslint-disable-line
  for (const tool of registered) {
    if (!want(tool.name)) continue
    const args = tool.name === 'goai_run' || tool.name === 'goai_chat' ? { noAudit: true } : {}
    if (tool.name === 'goai_chat') args.message = '腾讯业绩前方向不确定，账户10万港币，评估跨式'
    try {
      const result = await tool.execute(args)
      const text = tool.output ? (tool.output.render ? tool.output.render({}, result).map((b) => b.text).join('\n') : '') : ''
      const status = result && result.ok ? 'PASS' : 'FAIL'
      if (status === 'FAIL') failures++
      console.log(`[${status}] ${pluginName}::${tool.name}`)
      console.log('  ' + (text || JSON.stringify(result)).split('\n').slice(0, 12).join('\n  '))
    } catch (err) {
      failures++
      console.log(`[FAIL] ${pluginName}::${tool.name} threw: ${err && err.stack || err}`)
    }
  }
}
for (const d of disposers) { try { d() } catch (_) {} }
console.log(failures === 0 ? 'SMOKE PASSED' : `SMOKE FAILED (${failures} failures)`)
process.exit(failures === 0 ? 0 : 1)
