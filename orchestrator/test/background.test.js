// What a session still has running in the background: discover.js backgroundAgents / backgroundOf.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { backgroundAgents, backgroundOf, shellCommand } from '../src/discover.js';

function conversation(agents) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bg-'));
  const transcript = path.join(root, 'conv.jsonl');
  fs.writeFileSync(transcript, '');
  const dir = path.join(root, 'conv', 'subagents');
  fs.mkdirSync(dir, { recursive: true });
  for (const [i, a] of agents.entries()) {
    const file = path.join(dir, `agent-${i}.meta.json`);
    fs.writeFileSync(file, JSON.stringify(a.meta));
    if (a.mtime) fs.utimesSync(file, a.mtime / 1000, a.mtime / 1000);
  }
  return transcript;
}

test('a background agent with no end recorded is running; a finished, stopped or foreground one is not', () => {
  const t = conversation([
    { meta: { description: 'Build the tool', requestShape: 'background' } },
    { meta: { description: 'Done', requestShape: 'background', finished: true } },
    { meta: { description: 'Stopped', requestShape: 'background', stoppedByUser: true } },
    { meta: { description: 'Inline', requestShape: 'foreground' } },
  ]);
  assert.deepEqual(backgroundAgents(t, Date.now() - 60000), ['Build the tool']);
});

test('an agent record older than the process was left by a process that died with it in flight', () => {
  const start = Date.now() - 60000;
  const t = conversation([{ meta: { description: 'Orphan', requestShape: 'background' }, mtime: start - 3600000 }]);
  assert.deepEqual(backgroundAgents(t, start), []);
});

test('no subagents directory, no transcript or no process start is nothing running', () => {
  assert.deepEqual(backgroundAgents(path.join(os.tmpdir(), 'nope.jsonl'), Date.now()), []);
  assert.deepEqual(backgroundAgents(null, Date.now()), []);
  assert.deepEqual(backgroundAgents(conversation([{ meta: { requestShape: 'background' } }]), null), []);
});

test('backgroundOf: a shell is the process record saying so; no record (or Codex) is nothing', async () => {
  const t = conversation([{ meta: { description: 'A', requestShape: 'background' } }]);
  const since = Date.now() - 60000;
  // A pid with no shells under it: the record still says one is running, with nothing to name it by.
  assert.deepEqual(await backgroundOf({ shell: true, recordStartedAt: since, recordPid: 999999 }, t), { bg_agents: ['A'], bg_shell: true, bg_shells: null });
  assert.deepEqual(await backgroundOf({ shell: false, recordStartedAt: since }, conversation([])), { bg_agents: null, bg_shell: false, bg_shells: null });
  assert.deepEqual(await backgroundOf(null, t), { bg_agents: null, bg_shell: false, bg_shells: null });
  assert.deepEqual(await backgroundOf({ busy: false }, t), { bg_agents: null, bg_shell: false, bg_shells: null });
});

test('shellCommand reads the command out of the wrapper Claude Code runs it in', () => {
  const wrap = (c) => `/bin/zsh -c source /x.sh 2>/dev/null || true && eval '${c}' < /dev/null && pwd -P >| /tmp/claude-ab-cwd`;
  assert.equal(shellCommand(wrap('fly deploy -a hub')), 'fly deploy -a hub');
  assert.equal(shellCommand(wrap(`grep -q '"'"'a b'"'"' f`)), "grep -q 'a b' f");
  assert.equal(shellCommand('node /x/mcp.js'), null);
});
