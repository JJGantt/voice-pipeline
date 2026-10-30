import test from 'node:test';
import assert from 'node:assert/strict';
import { unmarkMoveInterrupt } from '../src/mirror.js';

const rec = (o) => JSON.stringify(o);
const REJECT = "The user doesn't want to proceed with this tool use. The tool use was rejected. STOP what you are doing and wait for the user to tell you how to proceed.";

test('rewrites the interrupt the move left on the cut-off turn', () => {
  const text = [
    rec({ type: 'user', message: { content: [{ type: 'text', text: '[Request interrupted by user]' }] } }),   // Jared's, earlier
    rec({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 't1', name: 'Bash' }] } }),
    rec({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: REJECT, is_error: true }] },
          toolUseResult: 'User rejected tool use' }),
    rec({ type: 'user', message: { content: [{ type: 'text', text: '[Request interrupted by user for tool use]' }] } }),
    rec({ type: 'file-history-snapshot' }),
    '',
  ].join('\n');
  const out = unmarkMoveInterrupt(text).split('\n');
  assert.equal(out[0], text.split('\n')[0]);
  const result = JSON.parse(out[2]);
  assert.match(result.message.content[0].content, /moved from the Pi to the Mac/);
  assert.doesNotMatch(out[2], /STOP|rejected tool use/);
  assert.equal(result.message.content[0].tool_use_id, 't1');
  assert.match(JSON.parse(out[3]).message.content[0].text, /move from the Pi, not by Jared/);
  assert.equal(out.at(-1), '');
});

test('leaves a transcript with no trailing interrupt untouched', () => {
  const text = [
    rec({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: REJECT }] } }),
    rec({ type: 'assistant', message: { content: [{ type: 'text', text: 'ok' }] } }),
  ].join('\n') + '\n';
  assert.equal(unmarkMoveInterrupt(text), text);
});
