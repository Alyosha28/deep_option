// GOAI preset live-mount smoke test.
// Runs against a running DSH web server (default http://127.0.0.1:3080,
// override with DSH_WEB_URL). Reuses ONE blank session per day; a real
// session.create is the only check that exercises the DSH mount guard, which
// static YAML checks cannot (known failure mode: tool-cordis collides on the
// host cordisInspect registry when another cordis-preset session mounted
// first).
//
// Usage:
//   node harness\\smoke_preset.mjs
// Exit code 0 = mount OK, 1 = mount failed or server not reachable.

import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const base = (process.env.DSH_WEB_URL ?? 'http://127.0.0.1:3080').replace(/\/$/, '');
const presetId = 'goai-options';
const day = new Date().toISOString().slice(0, 10).replaceAll('-', '');
const sessionId = 'session-goai-preset-smoke-' + day;

async function rpc(method, payload = {}) {
  const res = await fetch(base + '/api/' + method, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload }),
  });
  return await res.json();
}

async function main() {
  const list = await rpc('agentPreset.list', {});
  const row = list?.result?.value?.presets?.find((p) => p.id === presetId);
  if (!row) throw new Error('preset not found in DSH roster: ' + presetId);
  if (row.broken) throw new Error('preset discovered as broken: ' + JSON.stringify(row.broken));
  console.log('[OK] preset listed:', row.id, '-', row.name);

  const sessions = await rpc('session.list', {});
  const previous = sessions?.result?.value?.items?.find((it) => it.sessionId === sessionId);
  if (previous?.agentPreset === presetId) {
    console.log('[OK] existing smoke session from today already mounted the preset ->', sessionId);
    console.log('SMOKE PASSED (delete the session in the UI to force a fresh mount next run)');
    return;
  }

  const created = await rpc('session.create', { cwd: repoRoot, sessionId, agentPreset: presetId });
  if (!created?.result?.ok) {
    throw new Error('session.create rejected the preset mount:\n' + JSON.stringify(created, null, 2));
  }
  if (created.result.value.agentPreset !== presetId) {
    throw new Error('created session did not record the preset: ' + created.result.value.agentPreset);
  }
  console.log('[OK] real mount succeeded ->', created.result.value.sessionId, 'on preset', created.result.value.agentPreset);
  console.log('SMOKE PASSED (blank test session left in the session list; delete it from the UI)');
}

try {
  await main();
} catch (err) {
  console.error('[FAIL]', err.message);
  process.exitCode = 1;
}
