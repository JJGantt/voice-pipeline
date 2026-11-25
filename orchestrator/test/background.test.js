// What a session still has running in the background: discover.js backgroundAgents / backgroundOf.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { backgroundAgents, backgroundOf } from '../src/discover.js';

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

test('backgroundOf: a shell is the process record saying so; no record (or Codex) is nothing', () => {
  const t = conversation([{ meta: { description: 'A', requestShape: 'background' } }]);
  const since = Date.now() - 60000;
  assert.deepEqual(backgroundOf({ shell: true, recordStartedAt: since }, t), { bg_agents: ['A'], bg_shell: true });
  assert.deepEqual(backgroundOf({ shell: false, recordStartedAt: since }, conversation([])), { bg_agents: null, bg_shell: false });
  assert.deepEqual(backgroundOf(null, t), { bg_agents: null, bg_shell: false });
  assert.deepEqual(backgroundOf({ busy: false }, t), { bg_agents: null, bg_shell: false });
});
