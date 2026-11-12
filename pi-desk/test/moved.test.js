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

test('a follow-up for a session moved to Fly or the Pi goes to the session named after its resume', () => {
  const fly = { id: 'abcdef12-0000-0000-0000-000000000000', payload: { target: 'fly', claude_session_id: CONV, repo: 'status-hub' } };
  assert.deepEqual(forwardMovedFollowUp(payload, row, fly, 'new'), {
    id: 'new', kind: 'follow_up',
    payload: { prompt: 'show them on a map', target: 'fly', session: 'hub-status-hub-abcdef12', msg: 'm1' },
  });
  const pi = { id: 'abcdef12-1', payload: { target: 'pi', claude_session_id: CONV } };
  assert.equal(forwardMovedFollowUp(payload, row, pi, 'new').payload.session, 'hub-root-abcdef12');
});

test('a resume for another conversation, or to nowhere known, is not followed', () => {
  assert.equal(forwardMovedFollowUp(payload, row, null, 'n'), null);
  assert.equal(forwardMovedFollowUp(payload, row, { ...resume, payload: { ...resume.payload, claude_session_id: 'other' } }, 'n'), null);
  assert.equal(forwardMovedFollowUp(payload, row, { ...resume, payload: { ...resume.payload, target: 'moon' } }, 'n'), null);
});
