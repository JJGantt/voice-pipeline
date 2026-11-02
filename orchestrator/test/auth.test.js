import test from 'node:test';
import assert from 'node:assert/strict';
import { authed } from '../src/auth.js';

const req = (headers) => ({ headers });

test('admits the token in an X-Token header', () => {
  assert.equal(authed(req({ 'x-token': 'right' }), 'right'), true);
});

test('refuses a wrong, missing or query-string token', () => {
  assert.equal(authed(req({ 'x-token': 'wrong' }), 'right'), false);
  assert.equal(authed(req({}), 'right'), false);
  // A token in the URL is what this gate exists to stop accepting: only headers are read.
  assert.equal(authed({ headers: {}, url: '/inject?token=right' }, 'right'), false);
});

test('an unset expected token admits nobody, even an empty header', () => {
  assert.equal(authed(req({ 'x-token': '' }), ''), false);
  assert.equal(authed(req({}), undefined), false);
});
