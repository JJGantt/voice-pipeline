import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { projectDir, readTail, turnBoundary } from '../src/transcripts.js';

test('projectDir names the directory the way Claude Code does', () => {
  const name = (cwd) => path.basename(projectDir(cwd));
  assert.equal(name('/Users/jaredgantt/repos/voice-pipeline'), '-Users-jaredgantt-repos-voice-pipeline');
  assert.equal(name('/Users/jaredgantt/.hammerspoon'), '-Users-jaredgantt--hammerspoon');
  assert.equal(name('/Users/jaredgantt/repos/litter_robot_analyzer'), '-Users-jaredgantt-repos-litter-robot-analyzer');
  assert.equal(name('/Users/jaredgantt/My Repo'), '-Users-jaredgantt-My-Repo');
});

// A transcript of records, written to a temp file for readTail.
function transcript(records) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tail-'));
  const f = path.join(dir, 't.jsonl');
  fs.writeFileSync(f, records.map((r) => JSON.stringify(r)).join('\n') + '\n');
  return f;
}
const prompt = (ts, text) => ({ type: 'user', timestamp: ts, message: { role: 'user', content: text } });
const reply = (ts, text) => ({ type: 'assistant', timestamp: ts,
  message: { role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text }] } });
const meta = (ts, extra) => ({ type: 'user', timestamp: ts, ...extra, message: { role: 'user', content: 'injected' } });

test('readTail: the turn boundary is the last prompt or reply, not injected context nobody answered', () => {
  const base = [prompt('2026-11-12T22:50:00Z', 'what is the weather'), reply('2026-11-12T22:52:00Z', 'Sunny.')];
  // An idle compaction, about an hour after the last turn.
  let t = readTail(transcript([...base, meta('2026-11-12T23:47:00Z', { isCompactSummary: true })]));
  assert.equal(t.turnAt, '2026-11-12T22:52:00Z');
  assert.equal(t.working, false);
  // The caveat Claude Code writes ahead of a typed /exit, when a session is parked.
  t = readTail(transcript([...base, meta('2026-11-12T23:40:00Z', { isMeta: true })]));
  assert.equal(t.turnAt, '2026-11-12T22:52:00Z');
  assert.equal(t.working, false);
  // Injected context the model does answer is a turn, started when it arrived.
  t = readTail(transcript([...base, meta('2026-11-12T23:00:00Z', { isMeta: true }),
    { type: 'assistant', timestamp: '2026-11-12T23:00:05Z', message: { role: 'assistant', content: [{ type: 'text', text: 'On it' }] } }]));
  assert.equal(t.turnAt, '2026-11-12T23:00:00Z');
  assert.equal(t.working, true);
  // Compaction mid-turn keeps the turn and its start.
  t = readTail(transcript([...base, prompt('2026-11-12T23:10:00Z', 'go'), meta('2026-11-12T23:20:00Z', { isCompactSummary: true })]));
  assert.equal(t.turnAt, '2026-11-12T23:10:00Z');
  assert.equal(t.working, true);
});

test('turnBoundary trusts the process record only while a turn is in flight', () => {
  const tail = { turnAt: '2026-11-12T22:52:00Z' };
  assert.equal(turnBoundary({ busy: true, statusAt: '2026-11-12T23:10:00Z' }, tail, null), '2026-11-12T23:10:00Z');
  // Idle since an idle compaction finished: the transcript's boundary, even moving the row back.
  assert.equal(turnBoundary({ busy: false, statusAt: '2026-11-12T23:47:00Z' }, tail, '2026-11-12T23:47:00Z'), '2026-11-12T22:52:00Z');
  assert.equal(turnBoundary(null, { turnAt: null }, '2026-11-12T20:00:00Z'), '2026-11-12T20:00:00Z');
});
