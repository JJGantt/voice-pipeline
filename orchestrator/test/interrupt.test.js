import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { composerText, stopTurn, INTERRUPTED } from '../src/interrupt.js';

const RULE = '─'.repeat(80);
const screen = (...composer) => ['⏺ BANANA', '✻ Sautéed for 1s', RULE, ...composer, RULE, '  ⏵⏵ auto mode on'].join('\n');

test('composerText reads what is between the last two rules', () => {
  assert.equal(composerText(screen('❯ ')), '');
  assert.equal(composerText(screen('❯ Write a 3000-word essay on canals.')), 'Write a 3000-word essay on canals.');
  assert.equal(composerText(screen('❯ first line', '  second line')), 'first line\n  second line');
  assert.equal(composerText(screen('❯ Press up to edit queued messages')), '');
  assert.equal(composerText(screen('❯ Try "fix typecheck errors"')), '');
  // A rule further up the scrollback is not the composer's.
  assert.equal(composerText([RULE, 'old', RULE, 'reply', screen('❯ ')].join('\n')), '');
  assert.equal(composerText('no composer on this screen'), null);
});

function harness({ busyAfter = false, left = '', interrupts = true, idles = false } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'interrupt-'));
  const transcriptPath = path.join(dir, 't.jsonl');
  fs.writeFileSync(transcriptPath, '{"type":"user"}\n');
  const calls = [];
  let pressed = false;
  return {
    calls,
    deps: {
      session: { busy: true, waiting: null, transcriptPath },
      // Busy until pressed; then idle only when the interrupt idles it, and busy again once a queued
      // message takes over.
      state: () => (!pressed ? { busy: true } : busyAfter ? { busy: true } : { busy: !idles && !interrupts }),
      press: async () => {
        pressed = true;
        calls.push('press');
        if (interrupts) fs.appendFileSync(transcriptPath, JSON.stringify({ text: `${INTERRUPTED}]` }) + '\n');
      },
      screen: async () => screen(`❯ ${left}`),
      clear: async () => { calls.push('clear'); },
    },
  };
}

test('stopTurn sends nothing to a session that is not running a turn', async () => {
  const h = harness();
  h.deps.session = { ...h.deps.session, busy: false };
  assert.match(await stopTurn('s', h.deps), /nothing to stop/);
  assert.deepEqual(h.calls, []);
});

test('stopTurn reports a queued message taking over, and leaves its composer alone', async () => {
  const h = harness({ busyAfter: true, left: 'anything' });
  assert.match(await stopTurn('s', h.deps), /went on to the message queued/);
  assert.deepEqual(h.calls, ['press']);
});

test('stopTurn clears a prompt put back in the composer', async () => {
  const h = harness({ left: 'Write a long essay' });
  assert.match(await stopTurn('s', h.deps), /cleared the interrupted prompt/);
  assert.deepEqual(h.calls, ['press', 'clear']);
});

test('stopTurn leaves an empty composer alone', async () => {
  const h = harness();
  assert.match(await stopTurn('s', h.deps), /waiting for the next message/);
  assert.deepEqual(h.calls, ['press']);
});

test('stopTurn takes an idle record as the receipt when nothing was written', async () => {
  // A turn cut off before it answered writes no interrupt record; its prompt goes back to the composer.
  const h = harness({ interrupts: false, idles: true, left: 'Write a long essay' });
  assert.match(await stopTurn('s', h.deps), /cleared the interrupted prompt/);
});

test('stopTurn fails when the turn never ends', async () => {
  const h = harness({ interrupts: false });
  await assert.rejects(stopTurn('s', h.deps), /did not stop/);
});
