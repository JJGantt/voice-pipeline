import { test } from 'node:test';
import assert from 'node:assert/strict';
import { commandMb } from '../commands.js';

const SNAP = '/bin/bash -c source /Users/jaredgantt/.claude/shell-snapshots/snapshot-bash-1-abc.sh && eval npm test';
const MB = 1024;

test('the session itself and its tool servers are not commands', () => {
  assert.equal(commandMb([
    { pid: 10, ppid: 1, cmd: 'claude --model opus', kb: 343 * MB },
    { pid: 11, ppid: 10, cmd: 'node /Users/jaredgantt/data-layer/dist/mcp.js', kb: 96 * MB },
    { pid: 12, ppid: 10, cmd: 'python3 /Users/jaredgantt/mcp-tv/mcp_server.py', kb: 62 * MB },
  ]), 0);
});

test('a command counts with everything it started', () => {
  assert.equal(commandMb([
    { pid: 10, ppid: 1, cmd: 'claude --model opus', kb: 600 * MB },
    { pid: 20, ppid: 10, cmd: SNAP, kb: 3 * MB },
    { pid: 21, ppid: 20, cmd: 'npm test', kb: 60 * MB },
    { pid: 22, ppid: 21, cmd: 'python3 analyse.py', kb: 400 * MB },
  ]), 463);
});

test('two commands at once add up, and a process under none counts nothing', () => {
  assert.equal(commandMb([
    { pid: 10, ppid: 1, cmd: 'claude', kb: 600 * MB },
    { pid: 20, ppid: 10, cmd: SNAP, kb: 100 * MB },
    { pid: 30, ppid: 10, cmd: SNAP, kb: 150 * MB },
    { pid: 31, ppid: 99, cmd: 'orphan', kb: 500 * MB },   // parent not in this cgroup
  ]), 250);
});
