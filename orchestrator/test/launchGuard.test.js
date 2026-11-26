import { test } from 'node:test';
import assert from 'node:assert/strict';
import { beginLaunch, endLaunch, launchSnapshot, mayAdopt } from '../src/deskSessions.js';

test('adoption: allowed when no launch is in flight or happened since the rows were read', () => {
  assert.equal(mayAdopt(launchSnapshot()), true);
});

test('adoption: held while a launch is in flight', () => {
  const snap = launchSnapshot();
  beginLaunch();
  try { assert.equal(mayAdopt(snap), false); assert.equal(mayAdopt(launchSnapshot()), false); }
  finally { endLaunch(); }
});

test('adoption: held when a launch began and ended after the rows were read', () => {
  // The resume that wrote its row mid-tick: the tick's rows lack it, so its process looks unowned.
  const snap = launchSnapshot();
  beginLaunch(); endLaunch();
  assert.equal(mayAdopt(snap), false);
  assert.equal(mayAdopt(launchSnapshot()), true);   // the next tick reads the new row and may adopt
});
