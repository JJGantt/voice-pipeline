import test from 'node:test';
import assert from 'node:assert/strict';
import { unmarkMoveInterrupt } from '../src/mirror.js';

const rec = (o) => JSON.stringify(o);
const REJECT = "The user doesn't want to proceed with this tool use. The tool use was rejected. STOP what you are doing and wait for the user to tell you how to proceed.";

test('rewrites the interrupt the move left on the cut-off turn', () => {
  const text = [
    rec({ type: 'user', message: { content: [{ type: 'text', text: '[Request interrupted by user]' }] } }),   // the user's, earlier
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

// ---- mirrorUp: whole lines only, one upload at a time per conversation ----
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { mirrorUp, assemble, mirrorDown, mirrorCompact } from '../src/mirror.js';

function stubStorage() {
  const parts = new Map();   // offset -> bytes
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const m = /\/(\d{12})\.part$/.exec(String(url));
    if (init.method === 'POST' && m) {
      await new Promise((r) => setTimeout(r, 5));   // an upload is not instant
      parts.set(Number(m[1]), Buffer.from(init.body));
      return new Response('{}', { status: 200 });
    }
    if (m) return new Response(parts.get(Number(m[1])), { status: 200 });   // a download
    if (init.method === 'DELETE') {
      for (const p of JSON.parse(init.body).prefixes) parts.delete(Number(/(\d{12})\.part$/.exec(p)[1]));
      return new Response('[]', { status: 200 });
    }
    if (/\/object\/list\//.test(String(url))) {
      const names = [...parts.keys()].map((o) => ({ name: `${String(o).padStart(12, '0')}.part` }));
      return new Response(JSON.stringify(names), { status: 200 });
    }
    return new Response('[]', { status: 200 });
  };
  return { parts, restore: () => { globalThis.fetch = real; } };
}
const tmpFile = (text) => {
  const f = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'mirror-')), 't.jsonl');
  fs.writeFileSync(f, text);
  return f;
};

test('mirrorUp uploads only whole lines and returns the byte after the last newline', async () => {
  const s = stubStorage();
  try {
    const f = tmpFile('{"a":1}\n{"b":2}\n{"c":');
    assert.equal(await mirrorUp('conv-cut', f, 0), 16);
    assert.equal(s.parts.get(0).toString(), '{"a":1}\n{"b":2}\n');
    fs.appendFileSync(f, '3}\n');
    assert.equal(await mirrorUp('conv-cut', f, 16), 24);
    assert.equal(s.parts.get(16).toString(), '{"c":3}\n');
  } finally { s.restore(); }
});

test('a line still being written with no newline yet uploads nothing', async () => {
  const s = stubStorage();
  try {
    const f = tmpFile('{"half":');
    assert.equal(await mirrorUp('conv-half', f, 0), null);
    assert.equal(s.parts.size, 0);
  } finally { s.restore(); }
});

test('two uploads from the same stale offset do not overlap: the second starts where the first ended', async () => {
  const s = stubStorage();
  try {
    const f = tmpFile('{"a":1}\n');
    const first = mirrorUp('conv-race', f, 0);
    fs.appendFileSync(f, '{"b":2}\n');
    const second = mirrorUp('conv-race', f, 0);   // the tick, reading a row the watcher had not updated
    const [a, b] = await Promise.all([first, second]);
    assert.ok(a > 0 && b === 16);
    const whole = assemble([...s.parts].sort((x, y) => x[0] - y[0]).map(([at, buf]) => ({ at, buf })));
    assert.equal(whole.toString(), '{"a":1}\n{"b":2}\n');
    // Nothing new: the reach is still reported to a caller whose row is behind it.
    assert.equal(await mirrorUp('conv-race', f, 8), 16);
  } finally { s.restore(); }
});

test('assemble keeps only what an overlapping part adds', () => {
  const p = (at, t) => ({ at, buf: Buffer.from(t) });
  assert.equal(assemble([p(0, 'aaaa'), p(2, 'aabbb'), p(7, 'c')]).toString(), 'aaaabbbc');
  assert.equal(assemble([p(0, 'aaaa'), p(1, 'aa')]).toString(), 'aaaa');
});

test('past 20 parts the mirror is folded into one part holding the whole transcript', async () => {
  const s = stubStorage();
  try {
    const lines = [];
    const f = tmpFile('');
    let at = 0;
    for (let i = 0; i < 25; i++) {
      const l = `{"turn":${i}}\n`;
      lines.push(l);
      fs.appendFileSync(f, l);
      at = await mirrorUp('conv-compact', f, at);
    }
    // Compacted at the 20th part; five more appended after it.
    assert.equal(s.parts.size, 6);
    assert.equal(s.parts.get(0).toString(), lines.slice(0, 20).join(''));
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mirror-down-'));
    const out = await mirrorDown('conv-compact', dir);
    try { assert.equal(fs.readFileSync(out, 'utf8'), lines.join('')); }
    finally { fs.rmSync(path.dirname(out), { recursive: true, force: true }); }
  } finally { s.restore(); }
});

test('mirrorCompact folds a stored mirror into one part without changing what it assembles to', async () => {
  const s = stubStorage();
  try {
    s.parts.set(0, Buffer.from('{"a":1}\n'));
    s.parts.set(8, Buffer.from('{"b":2}\n'));
    s.parts.set(16, Buffer.from('{"c":3}\n'));
    assert.equal(await mirrorCompact('conv-stored'), 3);
    assert.deepEqual([...s.parts.keys()], [0]);
    assert.equal(s.parts.get(0).toString(), '{"a":1}\n{"b":2}\n{"c":3}\n');
  } finally { s.restore(); }
});
