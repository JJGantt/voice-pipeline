import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { projectDir } from '../src/transcripts.js';

test('projectDir names the directory the way Claude Code does', () => {
  const name = (cwd) => path.basename(projectDir(cwd));
  assert.equal(name('/Users/jaredgantt/repos/voice-pipeline'), '-Users-jaredgantt-repos-voice-pipeline');
  assert.equal(name('/Users/jaredgantt/.hammerspoon'), '-Users-jaredgantt--hammerspoon');
  assert.equal(name('/Users/jaredgantt/repos/litter_robot_analyzer'), '-Users-jaredgantt-repos-litter-robot-analyzer');
  assert.equal(name('/Users/jaredgantt/My Repo'), '-Users-jaredgantt-My-Repo');
});
