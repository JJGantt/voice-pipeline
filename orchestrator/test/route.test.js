import test from 'node:test';
import assert from 'node:assert/strict';
import { nextMachine, presenceReady, handedVerdict, flyCommandFor, evacuateVerdict, staysOnMachine } from '../src/route.js';

const NOW = Date.parse('2026-11-12T12:00:00Z');
const beat = (agoS, info = {}) => ({ last_seen: new Date(NOW - agoS * 1000).toISOString(), info });

test('the Pi hands to the Mac when it can take it, else to Fly; the Mac to the Pi, else Fly; Fly nowhere', () => {
  assert.equal(nextMachine('pi', { macReady: true }), 'mac');
  assert.equal(nextMachine('pi', { macReady: false, piReady: true }), 'fly');
  assert.equal(nextMachine('mac', { piReady: true }), 'pi');
  assert.equal(nextMachine('mac', { piReady: false, macReady: true }), 'fly');
  assert.equal(nextMachine('fly', { piReady: true, macReady: true }), null);
});

test('a machine can take work only while it beats and is signed in', () => {
  const later = new Date(NOW + 86400e3).toISOString();
  assert.equal(presenceReady(beat(10, { login_expires_at: later }), { now: NOW }), true);
  assert.equal(presenceReady(beat(10), { now: NOW }), true, 'an older beat with no login date counts as signed in');
  assert.equal(presenceReady(beat(91), { now: NOW }), false, 'stale');
  assert.equal(presenceReady(beat(10, { login_expires_at: new Date(NOW - 1).toISOString() }), { now: NOW }), false, 'expired login');
  assert.equal(presenceReady(beat(10, { login_expires_at: null }), { now: NOW }), false, 'no login at all');
  assert.equal(presenceReady(null, { now: NOW }), false);
  assert.equal(presenceReady(beat(10, { codex: true }), { agent: 'codex', now: NOW }), true);
  assert.equal(presenceReady(beat(10, {}), { agent: 'codex', now: NOW }), false);
});

test('handoff verdicts are the forms data-layer followHandoff parses', () => {
  assert.equal(handedVerdict('mac', 'abc', 'no room'), 'handed to the Mac as abc — no room');
  assert.equal(handedVerdict('fly', 'abc', 'no room'), 'handed to Fly as abc — no room');
  assert.equal(handedVerdict('pi', 'abc'), 'handed to the Pi as abc');
});

test('an expired open or mirrored resume goes to Fly as it is', () => {
  const cmd = { id: 'c1', kind: 'open_session', payload: { name: 'x', prompt: 'hi', target: 'mac' } };
  assert.deepEqual(flyCommandFor(cmd, null, 'mac'), { kind: 'open_session', payload: { name: 'x', prompt: 'hi', target: 'fly' } });
  assert.equal(flyCommandFor({ ...cmd, kind: 'resume_mirrored' }).payload.target, 'fly');
});

test('an expired resume or follow-up moves its conversation to Fly from the mirror, words and message id intact', () => {
  const row = { id: 'mac-1', repo: 'r', label: 'L', origin: 'adopted', claude_session_id: 'conv', agent: 'claude' };
  const fu = { id: 'c2', kind: 'follow_up', payload: { session: 'mac-1', prompt: 'go on', msg: 'm9', target: 'mac' } };
  assert.deepEqual(flyCommandFor(fu, row, 'mac'), { kind: 'resume_mirrored', payload: {
    target: 'fly', claude_session_id: 'conv', repo: 'r', label: 'L', origin: 'adopted', from: 'mac', prompt: 'go on', msg: 'm9' } });
  const rs = flyCommandFor({ id: 'c3', kind: 'resume_session', payload: { session: 'mac-1' } }, row, 'mac');
  assert.equal(rs.kind, 'resume_mirrored');
  assert.equal(rs.payload.prompt, undefined);
});

test('a session that cannot move, and a kind that does not, is not sent to Fly', () => {
  const fu = { id: 'c2', kind: 'follow_up', payload: { session: 's', prompt: 'x' } };
  assert.equal(flyCommandFor(fu, { claude_session_id: 'c', agent: 'codex' }, 'pi'), null);
  assert.equal(flyCommandFor(fu, { claude_session_id: null }, 'pi'), null);
  assert.equal(flyCommandFor({ id: 'c', kind: 'end_session', payload: {} }, null, 'pi'), null);
});

test('the evacuate verdict names what moved and what could not', () => {
  assert.equal(evacuateVerdict('pi', ['hub-a', 'hub-b'], ['hub-c (codex)']),
               'evacuated 2 session(s) from pi: hub-a, hub-b; could not move: hub-c (codex)');
  assert.equal(evacuateVerdict('mac', [], []), 'evacuated 0 session(s) from mac: none; could not move: none');
});

import { movedTo } from '../src/route.js';
test('a live session is fenced when its row was transferred or another worker holds its conversation', () => {
  const mine = { id: 'hub-a', status: 'resident', claude_session_id: 'conv', worker: 'pi-desk' };
  assert.equal(movedTo(mine), null);
  assert.equal(movedTo({ ...mine, status: 'transferred' }), 'another machine');
  assert.equal(movedTo(mine, new Map([['conv', { id: 'hub-x', worker: 'fly-desk' }]])), 'fly');
  assert.equal(movedTo({ ...mine, status: 'transferred' }, new Map([['conv', { worker: 'desk' }]])), 'mac');
  assert.equal(movedTo(mine, new Map([['other', { worker: 'desk' }]])), null);
  assert.equal(movedTo(null), null);
});

test('a process started after the move is a deliberate resume and is not fenced', () => {
  const ended = '2026-11-12T12:00:00Z';
  const row = { id: 'mac-a', status: 'transferred', ended, claude_session_id: 'conv' };
  assert.equal(movedTo(row, new Map(), Date.parse(ended) - 60e3), 'another machine');
  assert.equal(movedTo(row, new Map(), Date.parse(ended) + 60e3), null);
  const other = new Map([['conv', { worker: 'pi-desk', started: ended }]]);
  assert.equal(movedTo({ ...row, status: 'resident' }, other, Date.parse(ended) - 1), 'pi');
  assert.equal(movedTo({ ...row, status: 'resident' }, other, Date.parse(ended) + 1), null);
});

test('an evacuation of the Mac leaves the sessions Jared used there', () => {
  const none = new Set();
  assert.equal(staysOnMachine('mac', { origin: 'adopted', claude_session_id: 'a' }, none), true);
  assert.equal(staysOnMachine('mac', { origin: 'dispatched', touched_at: '2026-11-13T15:00:00Z', claude_session_id: 'b' }, none), true);
  // Moved back to the Mac under a new row: the mark is on an earlier row of the same conversation.
  assert.equal(staysOnMachine('mac', { origin: 'dispatched', touched_at: null, claude_session_id: 'c' }, new Set(['c'])), true);
  assert.equal(staysOnMachine('mac', { origin: 'dispatched', touched_at: null, claude_session_id: 'd' }, none), false);
  assert.equal(staysOnMachine('pi', { origin: 'adopted', touched_at: 'x', claude_session_id: 'e' }, new Set(['e'])), false);
  assert.equal(evacuateVerdict('mac', ['m-1'], [], ['m-2']),
               'evacuated 1 session(s) from mac: m-1; could not move: none; kept, used at the Mac: m-2');
});
