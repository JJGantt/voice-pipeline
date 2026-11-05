import { test } from 'node:test';
import assert from 'node:assert/strict';
import { forwardMovedFollowUp } from '../moved.js';

const CONV = '29e34c60-a7ff-41f1-8d17-25d88779d1b3';
const row = { id: 'hub-root-mw3vuwi1', status: 'transferred', claude_session_id: CONV };
const resume = { id: '6123f1f8-00d2-480d-b336-3f8e43779396', payload: { target: 'mac', claude_session_id: CONV } };
const payload = { prompt: 'show them on a map', target: 'pi', session: 'hub-root-mw3vuwi1', msg: 'm1' };

test('a follow-up for a session moved to the Mac goes to its Mac session, words and message id intact', () => {
  assert.deepEqual(forwardMovedFollowUp(payload, row, resume, 'new'), {
    id: 'new', kind: 'follow_up',
    payload: { prompt: 'show them on a map', target: 'mac', session: 'mac-6123f1f8', msg: 'm1' },
  });
});

test('a session that ended or is still here is not forwarded', () => {
  assert.equal(forwardMovedFollowUp(payload, { ...row, status: 'parked' }, resume, 'new'), null);
  assert.equal(forwardMovedFollowUp(payload, null, resume, 'new'), null);
});

test('only a resume of the same conversation, on the Mac, is followed', () => {
  assert.equal(forwardMovedFollowUp(payload, row, null, 'new'), null);
  assert.equal(forwardMovedFollowUp(payload, row,
    { ...resume, payload: { target: 'mac', claude_session_id: 'other' } }, 'new'), null);
  assert.equal(forwardMovedFollowUp(payload, row,
    { ...resume, payload: { target: 'fly', claude_session_id: CONV } }, 'new'), null);
});
