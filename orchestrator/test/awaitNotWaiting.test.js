// A follow-up to a session on a permission prompt is held until the dialog goes (commands.js deliver).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sessions-'));
process.env.CLAUDE_SESSIONS_DIR = dir;
const { awaitNotWaiting } = await import('../src/discover.js');

const write = (pid, status) => fs.writeFileSync(path.join(dir, `${pid}.json`), JSON.stringify(
  { pid, sessionId: `s-${pid}`, cwd: os.homedir(), status, waitingFor: status === 'waiting' ? 'permission prompt' : undefined }));

test('resolves at once when the session is not on a dialog', async () => {
  write(1, 'busy');
  assert.equal((await awaitNotWaiting(1, 1000)).claudeId, 's-1');
});

test('resolves when the dialog clears, not before', async () => {
  write(2, 'waiting');
  let done = false;
  const p = awaitNotWaiting(2, 5000).then((s) => { done = true; return s; });
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(done, false);
  write(2, 'busy');
  assert.equal((await p).waiting, null);
});

test('rejects when the dialog is still up at the bound', async () => {
  write(3, 'waiting');
  await assert.rejects(awaitNotWaiting(3, 200), /still on a permission prompt/);
});

test('resolves null when the session goes away', async () => {
  write(4, 'waiting');
  const p = awaitNotWaiting(4, 5000);
  fs.unlinkSync(path.join(dir, '4.json'));
  assert.equal(await p, null);
});
