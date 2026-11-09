import { test } from 'node:test';
import assert from 'node:assert/strict';
import { missedByChannel, HEARD_GRACE_MS } from '../heard.js';

const SUB = Date.parse('2026-11-08T23:51:00Z');
const row = (id, at) => ({ id, created: new Date(at).toISOString() });

test('a row inserted while subscribed and never delivered is a miss', () => {
  const now = SUB + 3600e3;
  assert.equal(missedByChannel(row('a', SUB + 60e3), new Map(), SUB, now), true);
});

test('a delivered row is not a miss', () => {
  const now = SUB + 3600e3;
  assert.equal(missedByChannel(row('a', SUB + 60e3), new Map([['a', now]]), SUB, now), false);
});

test('a row inside the grace may still have its event in flight', () => {
  const created = SUB + 60e3;
  assert.equal(missedByChannel(row('a', created), new Map(), SUB, created + HEARD_GRACE_MS - 1), false);
  assert.equal(missedByChannel(row('a', created), new Map(), SUB, created + HEARD_GRACE_MS), true);
});

test('a row from before the channel subscribed, or while it is down, proves nothing', () => {
  const now = SUB + 3600e3;
  assert.equal(missedByChannel(row('a', SUB - 1), new Map(), SUB, now), false);
  assert.equal(missedByChannel(row('a', SUB + 60e3), new Map(), 0, now), false);
});
