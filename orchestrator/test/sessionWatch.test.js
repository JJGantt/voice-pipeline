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

// Parking a session types `/exit` into it. That is the machine closing the session, not the user
// speaking, so no surface may show it as the user's words, and the turn before it stands.
test('the /exit that parks a session is not shown as the user\'s words', () => {
  const st = fresh('hub-x');
  run(st, user('real question'), true);
  const turn = st.turn;
  const exit = '<command-name>/exit</command-name>\n            <command-message>exit</command-message>\n            <command-args></command-args>';
  assert.deepEqual(run(st, user(exit), true), []);
  assert.equal(st.turn, turn);
  assert.deepEqual(run(st, { type: 'queue-operation', operation: 'enqueue', content: '/exit' }, true), []);
  // Another slash command is still what the user typed.
  assert.equal(run(st, user('<command-name>/model</command-name><command-args>opus</command-args>'), true)[0].text, '/model opus');
});

// A turn end the hub never hears is never sent again (the tick reads the row, which says reported), so
// the conversation would show the prompt with no reply under it.
test('a turn end is sent until the hub takes it', async () => {
  const { pushUntilHeard } = await import('../src/sessionWatch.js');
  const answers = [null, null, {}];
  let tries = 0;
  const logged = { warn: [], error: [] };
  const was = { warn: console.warn, error: console.error };
  console.warn = (m) => logged.warn.push(m);
  console.error = (m) => logged.error.push(m);
  try {
    const heard = await pushUntilHeard({ session: 's' }, 'turn end',
      { send: async () => answers[tries++], waits: [0, 0, 0] });
    assert.deepEqual(heard, {});
  } finally { Object.assign(console, was); }
  assert.equal(tries, 3);
  assert.match(logged.warn.join('\n'), /reached the hub only on try 3/);
  assert.deepEqual(logged.error, []);
});

test('a turn end gives up after its last wait and says so', async () => {
  const { pushUntilHeard } = await import('../src/sessionWatch.js');
  let tries = 0;
  const errors = [];
  const was = console.error;
  console.error = (m) => errors.push(m);
  try {
    const heard = await pushUntilHeard({ session: 's' }, 'turn end',
      { send: async () => { tries++; return null; }, waits: [0, 0] });
    assert.equal(heard, null);
  } finally { console.error = was; }
  assert.equal(tries, 3);
  assert.match(errors.join('\n'), /never reached the hub/);
});
