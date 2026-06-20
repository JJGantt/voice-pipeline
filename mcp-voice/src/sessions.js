// Scans ~/.claude/projects/<project>/<session-id>.jsonl for focusable Claude sessions.
//
// Each .jsonl file is a stream of JSON-per-line records. The session's display
// name is the LATEST record with type "ai-title" (field `aiTitle`); records also
// carry a `cwd` field (we want the last one seen). Recency = file mtime.
//
// We don't need to parse whole files: we read a bounded tail (which holds the
// most recent records) and fall back to a bounded head only when the title can't
// be found there (e.g. a tiny/odd file).

import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PROJECTS_DIR = path.join(os.homedir(), '.claude', 'projects');
const TAIL_BYTES = 256 * 1024; // 256 KB tail — plenty for recent ai-title + cwd
const HEAD_BYTES = 128 * 1024; // bounded fallback read from the start

// Parse a chunk of newline-delimited JSON, returning the latest aiTitle, the
// latest cwd, and the first user-message text seen (for title fallback).
function scanChunk(chunk) {
  let aiTitle = null;
  let cwd = null;
  let firstUserText = null;
  const lines = chunk.split('\n');
  for (const line of lines) {
    const t = line.trim();
    if (!t || t[0] !== '{') continue;
    let rec;
    try {
      rec = JSON.parse(t);
    } catch {
      continue; // partial line at a chunk boundary, or non-JSON
    }
    if (rec.type === 'ai-title' && typeof rec.aiTitle === 'string') {
      aiTitle = rec.aiTitle; // keep overwriting → ends as the latest
    }
    if (typeof rec.cwd === 'string' && rec.cwd) {
      cwd = rec.cwd; // keep overwriting → ends as the latest seen in this chunk
    }
    if (firstUserText == null && rec.type === 'user' && rec.message) {
      const c = rec.message.content;
      let text = null;
      if (typeof c === 'string') text = c;
      else if (Array.isArray(c)) {
        const part = c.find((p) => p && p.type === 'text' && typeof p.text === 'string');
        if (part) text = part.text;
      }
      if (text) {
        const oneLine = text.replace(/\s+/g, ' ').trim();
        if (oneLine) firstUserText = oneLine.slice(0, 60);
      }
    }
  }
  return { aiTitle, cwd, firstUserText };
}

async function readTail(filePath, fh, size) {
  const start = Math.max(0, size - TAIL_BYTES);
  const len = size - start;
  const buf = Buffer.alloc(len);
  await fh.read(buf, 0, len, start);
  return buf.toString('utf8');
}

async function readHead(fh, size) {
  const len = Math.min(size, HEAD_BYTES);
  const buf = Buffer.alloc(len);
  await fh.read(buf, 0, len, 0);
  return buf.toString('utf8');
}

async function readSession(filePath, project, mtimeMs) {
  const id = path.basename(filePath, '.jsonl');
  let fh;
  try {
    fh = await fs.open(filePath, 'r');
    const { size } = await fh.stat();

    const tail = await readTail(filePath, fh, size);
    let { aiTitle, cwd, firstUserText } = scanChunk(tail);

    // If the title (or anything useful) wasn't in the tail and the file is larger
    // than what we read, scan a bounded head as a fallback.
    if ((!aiTitle || !cwd || !firstUserText) && size > TAIL_BYTES) {
      const head = await readHead(fh, size);
      const h = scanChunk(head);
      if (!aiTitle) aiTitle = h.aiTitle;
      if (!cwd) cwd = h.cwd; // head cwd only used if tail had none
      if (!firstUserText) firstUserText = h.firstUserText;
    }

    const title = aiTitle || firstUserText || id;
    return {
      id,
      title,
      cwd: cwd || null,
      project,
      last_active: new Date(mtimeMs).toISOString(),
    };
  } finally {
    if (fh) await fh.close();
  }
}

// Returns the N most-recent sessions by file mtime, newest first.
export async function listRecentSessions(limit = 5) {
  let projectDirs;
  try {
    projectDirs = await fs.readdir(PROJECTS_DIR, { withFileTypes: true });
  } catch {
    return [];
  }

  const candidates = [];
  for (const d of projectDirs) {
    if (!d.isDirectory()) continue;
    const dirPath = path.join(PROJECTS_DIR, d.name);
    let files;
    try {
      files = await fs.readdir(dirPath);
    } catch {
      continue;
    }
    for (const f of files) {
      if (!f.endsWith('.jsonl')) continue;
      const filePath = path.join(dirPath, f);
      let st;
      try {
        st = await fs.stat(filePath);
      } catch {
        continue;
      }
      if (!st.isFile()) continue;
      candidates.push({ filePath, project: d.name, mtimeMs: st.mtimeMs });
    }
  }

  candidates.sort((a, b) => b.mtimeMs - a.mtimeMs);
  const top = candidates.slice(0, limit);
  return Promise.all(top.map((c) => readSession(c.filePath, c.project, c.mtimeMs)));
}
