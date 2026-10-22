// node --test orchestrator/test/*.test.js  (or `npm test` in orchestrator/)
import test from 'node:test';
import assert from 'node:assert/strict';
import { expectPrompt, step } from '../src/sessionWatch.js';

const fresh = (id) => ({ id, said: null, queue: [], echo: null, turn: null, onActivity() {} });
const user = (text) => ({ type: 'user', uuid: `u-${text.length}`, timestamp: '2026-10-22T19:57:25.595Z',
                          message: { role: 'user', content: text } });
const run = (st, d, live) => { const out = []; step(st, d, (b) => out.push(b), live); return out; };

// A resumed session writes its first prompt before the watcher attaches, so it is read as PREFIX
// (live = false). The words were sent from a device holding a placeholder for them; the frame has to
// carry that device's id back or the placeholder stays under the whole turn.
test('a prompt read as prefix claims the id it was typed with', () => {
  expectPrompt('hub-a', 'Okay, a couple things.\nGo ahead.', 'msg-1');
  const [said] = run(fresh('hub-a'), user('Okay, a couple things.\nGo ahead.'), false);
  assert.equal(said.state, 'said');
  assert.equal(said.cmd, 'msg-1');
});

test('a prompt read live claims the id it was typed with', () => {
  expectPrompt('hub-b', 'next thing', 'msg-2');
  const [said] = run(fresh('hub-b'), user('next thing'), true);
  assert.equal(said.cmd, 'msg-2');
});

test('an id is claimed once', () => {
  expectPrompt('hub-c', 'same words', 'msg-3');
  assert.equal(run(fresh('hub-c'), user('same words'), true)[0].cmd, 'msg-3');
  assert.equal(run(fresh('hub-c'), user('same words'), true)[0].cmd, undefined);
});

test('words nobody registered carry no id', () => {
  expectPrompt('hub-d', 'what was typed', 'msg-4');
  assert.equal(run(fresh('hub-d'), user('something else'), false)[0].cmd, undefined);
  assert.equal(run(fresh('hub-e'), user('what was typed'), false)[0].cmd, undefined);
});
