import { test } from 'node:test';
import assert from 'node:assert/strict';
import { streak, RAISE_MS } from '../src/streak.js';

function capture(fn) {
  const out = { error: [], warn: [] };
  const orig = { error: console.error, warn: console.warn };
  console.error = (m) => out.error.push(m);
  console.warn = (m) => out.warn.push(m);
  try { fn(); } finally { Object.assign(console, orig); }
  return out;
}

test('count streak: warns on the first failure, raises once on the n-th in a row', () => {
  const s = streak('[t]', { failures: 2 });
  const out = capture(() => { s.fail(new Error('a')); s.fail(new Error('b')); s.fail(new Error('c')); });
  assert.equal(out.error.length, 1);
  assert.match(out.error[0], /failing 2 times in a row: b/);
  assert.equal(out.warn.length, 2);
});

test('count streak: a success in between resets the count and re-arms the raise', () => {
  const s = streak('[t]', { failures: 2 });
  const out = capture(() => {
    s.fail(new Error('a')); s.ok(); s.fail(new Error('b'));   // two failures, never consecutive
    s.ok(); s.fail(new Error('c')); s.fail(new Error('d'));   // then a real streak
  });
  assert.equal(out.error.length, 1);
  assert.match(out.error[0], /: d$/);
});

test('time streak: raises only once RAISE_MS has passed failing', () => {
  let t = 0;
  const s = streak('[t]', { now: () => t });
  const out = capture(() => {
    s.fail(new Error('a'));
    t = RAISE_MS - 1; s.fail(new Error('b'));
    t = RAISE_MS; s.fail(new Error('c'));
    t = RAISE_MS + 30000; s.fail(new Error('d'));
  });
  assert.equal(out.error.length, 1);
  assert.match(out.error[0], /: c$/);
});
