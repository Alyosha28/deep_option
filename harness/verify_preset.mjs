// GOAI preset template verifier (cross-platform companion to verify_preset.ps1).
// Static checks + optional byte-for-byte comparison against the installed
// user preset (~/.dsh/.agent-presets/goai-options or $DSH_HOME).
//
// Usage:
//   node harness\verify_preset.mjs
// Exit code 0 = PASS, 1 = FAIL.

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const presetDir = join(here, 'preset');
const agentPath = join(presetDir, 'agent.cordis.yml');
const metaPath = join(presetDir, 'preset.yml');
const skillsDir = join(presetDir, 'skills');

let failed = false;
function check(ok, message) {
  console.log((ok ? '[OK] ' : '[FAIL] ') + message);
  if (!ok) failed = true;
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function hashTree(dir) {
  const out = new Map();
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const full = join(d, name);
      const st = statSync(full);
      if (st.isDirectory()) walk(full);
      else out.set(full.slice(dir.length + 1).replaceAll('\\', '/'), sha256(full));
    }
  };
  if (existsSync(dir)) walk(dir);
  return out;
}

check(existsSync(agentPath), 'template agent.cordis.yml exists');
check(existsSync(metaPath), 'template preset.yml exists');

const agent = existsSync(agentPath) ? readFileSync(agentPath, 'utf8') : '';
const meta = existsSync(metaPath) ? readFileSync(metaPath, 'utf8') : '';

check(/name:\s*GOAI Options Terminal/.test(meta), 'preset.yml name = GOAI Options Terminal');
check(meta.includes('goai_state'), 'preset description mentions goai_state');
check(meta.includes('python -m src'), 'preset description documents CLI fallback');
check(meta.includes('tool-cordis 默认禁用'), 'preset description states tool-cordis disabled');
check(meta.includes('腾讯 0700'), 'preset description gives the hero example');
check(agent.includes("name: '@deepseek-ai/dsh-persona'"), 'GOAI persona row present');
check(agent.includes('数字铁律'), 'GOAI persona iron rules present');
check(agent.includes('NO_TRADE'), 'NO_TRADE-is-success discipline present');
check(agent.includes('- id: tool-vision'), 'tool-vision row present');
check(agent.includes("name: '@dsh-external/dsh-vision'"), 'view_image backend row present');

const cordisBlock = agent.match(/- id: tool-cordis[\s\S]*?(?=\n- id: \S|\n*$)/)?.[0] ?? '';
check(cordisBlock.includes('disabled: true'), 'tool-cordis disabled by default (registry collision fix)');
const delegBlock = agent.match(/- id: delegation[\s\S]*?(?=\n- id: \S|\n*$)/)?.[0] ?? '';
check(delegBlock.includes('disabled: true'), 'delegation group disabled by default (lean product surface)');

for (const skill of ['cordis-plugin-development', 'editing-cordis-compositions']) {
  check(existsSync(join(skillsDir, skill, 'SKILL.md')), `template ships skills/${skill}/SKILL.md`);
}

const dshHome = process.env.DSH_HOME || join(process.env.USERPROFILE || '', '.dsh');
const installed = join(dshHome, '.agent-presets', 'goai-options');
console.log('[INFO] installed preset path: ' + installed);
if (!existsSync(installed)) {
  console.log('[WARN] installed preset not found; copy harness/preset there to install');
  failed = true;
} else {
  const instAgent = join(installed, 'agent.cordis.yml');
  const instMeta = join(installed, 'preset.yml');
  const instSkills = join(installed, 'skills');
  check(existsSync(instAgent) && existsSync(instMeta), 'installed agent.cordis.yml + preset.yml exist');
  if (existsSync(instAgent) && existsSync(instMeta)) {
    check(sha256(agentPath) === sha256(instAgent), 'installed agent.cordis.yml byte-identical to template');
    check(sha256(metaPath) === sha256(instMeta), 'installed preset.yml byte-identical to template');
  }
  check(existsSync(instSkills), 'installed skills dir exists');
  if (existsSync(instSkills)) {
    const a = hashTree(skillsDir);
    const b = hashTree(instSkills);
    const same = a.size === b.size && [...a].every(([k, v]) => b.get(k) === v);
    check(same, 'installed skills byte-identical to template');
  }
}

if (failed) {
  console.log('==== PRESET VERIFICATION FAILED ====');
  process.exitCode = 1;
} else {
  console.log('==== PRESET VERIFICATION PASSED ====');
}
