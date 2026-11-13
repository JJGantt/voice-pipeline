import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyMiss } from '../src/live.js';

test('a missed beat is sorted by where it failed', () => {
  // The OS failed it locally: the machine's own network, whatever a probe says.
  for (const code of ['ENETUNREACH', 'EHOSTUNREACH', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT']) {
    assert.equal(classifyMiss(code, true), 'network down');
  }
  // Ambiguous (a reset, the 5s abort): decided by whether an independent host answered.
  assert.equal(classifyMiss('ECONNRESET', false), 'network down');
  assert.equal(classifyMiss('TimeoutError', false), 'network down');
  assert.equal(classifyMiss('ECONNRESET', true), 'responder unreachable');
  assert.equal(classifyMiss('ECONNREFUSED', true), 'responder unreachable');
});
