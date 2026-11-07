// Session-file helpers. Claude Code stores transcripts at
//   <PROJECTS_DIR>/<project-dir>/<session-id>.jsonl
// Each line is a JSON object; the latest `aiTitle` and `cwd` we care about live
// scattered through the file, so we read tail lines to extract the freshest.
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';

// ---- pure helpers (unit-testable) ----------------------------------------

// Given the raw text of a jsonl file, return the latest aiTitle and cwd seen.
// We scan from the end so the most-recent wins, but values can appear on
// different lines, so we keep scanning until both are found (or lines run out).
export function extractMeta(text) {
  const lines = text.split('\n');
  let aiTitle = null;
  let customTitle = null; // a user `/rename` — overrides the auto aiTitle
  let cwd = null;
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (customTitle == null && obj.type === 'custom-title' && typeof obj.customTitle === 'string') customTitle = obj.customTitle;
    if (aiTitle == null && typeof obj.aiTitle === 'string') aiTitle = obj.aiTitle;
    if (cwd == null && typeof obj.cwd === 'string') cwd = obj.cwd;
    if (customTitle != null && cwd != null) break;
  }
  return { title: customTitle || aiTitle, cwd };
}

// ---- filesystem-backed helpers -------------------------------------------

// Find the absolute path to <id>.jsonl anywhere under PROJECTS_DIR.
// Excludes subagents/ sidechain files. Returns null if not found.
export function findSessionFile(id) {
  const root = config.PROJECTS_DIR;
  let projects;
  try {
    projects = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const ent of projects) {
    if (!ent.isDirectory()) continue;
    const candidate = path.join(root, ent.name, `${id}.jsonl`);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

// Read the cwd recorded inside a session file (fallback when voice_focus has
// no session_cwd). Returns null if unreadable.
export async function cwdFromSessionFile(id) {
  const file = findSessionFile(id);
  if (!file) return null;
  try {
    const text = await fsp.readFile(file, 'utf8');
    return extractMeta(text).cwd;
  } catch {
    return null;
  }
}

// Scan PROJECTS_DIR for top-level session files, return the N most recent by
// mtime as registry rows: {id, title, cwd, project, last_active}.
export async function scanRegistry(limit) {
  const root = config.PROJECTS_DIR;
  let projects;
  try {
    projects = await fsp.readdir(root, { withFileTypes: true });
  } catch {
    return [];
  }

  const files = [];
  for (const ent of projects) {
    if (!ent.isDirectory()) continue;
    const projDir = path.join(root, ent.name);
    let entries;
    try {
      entries = await fsp.readdir(projDir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const f of entries) {
      if (!f.isFile() || !f.name.endsWith('.jsonl')) continue;
      const full = path.join(projDir, f.name);
      let st;
      try {
        st = await fsp.stat(full);
      } catch {
        continue;
      }
      files.push({
        id: f.name.slice(0, -'.jsonl'.length),
        full,
        project: ent.name,
        mtime: st.mtimeMs,
      });
    }
  }

  files.sort((a, b) => b.mtime - a.mtime);
  const top = files.slice(0, limit);

  const rows = [];
  for (const f of top) {
    let meta = { title: null, cwd: null };
    try {
      meta = extractMeta(await fsp.readFile(f.full, 'utf8'));
    } catch {
      // ignore unreadable file; still register with what we have.
    }
    rows.push({
      id: f.id,
      title: meta.title,
      cwd: meta.cwd,
      project: f.project,
      last_active: new Date(f.mtime).toISOString(),
    });
  }
  return rows;
}
