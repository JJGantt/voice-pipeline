// Pre-accept Claude Code's workspace-trust prompt for a directory.
//
// A fresh `claude` in a directory it hasn't seen opens with "Do you trust the files in this
// folder?" and waits. That's fine at the desk and useless from the phone: the hub's whole point is
// that the session is READY when you pick it up in the Claude app, and instead it sits on a modal
// nobody is there to answer. There's no CLI flag for it (only `-p`, which skips the dialog by
// being non-interactive — not what we want), so we write the same state the dialog writes:
// projects[<dir>].hasTrustDialogAccepted = true in ~/.claude.json.
//
// This is not a bypass of anything the user didn't decide: they tapped that specific directory in
// their own hub, and the launcher only ever runs against a path this daemon itself published.
//
// The care here is all about NOT CLOBBERING that file. Every running Claude session writes it, so a
// naive read-modify-write can drop a concurrent update. We therefore: skip the write entirely when
// the directory is already trusted (the common case), keep the read→rename window to milliseconds,
// re-check the file's mtime just before committing and retry if it moved, and write via a temp file
// + atomic rename so a crash can't leave a truncated config.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const CONFIG = path.join(os.homedir(), '.claude.json');

// The shape Claude Code creates for a project it has never seen. Anything omitted it fills in
// itself; these are just the keys that must not be missing for the entry to read as complete.
const NEW_ENTRY = {
  allowedTools: [],
  mcpContextUris: [],
  mcpServers: {},
  enabledMcpjsonServers: [],
  disabledMcpjsonServers: [],
  hasTrustDialogAccepted: true,
  projectOnboardingSeenCount: 1,
  hasClaudeMdExternalIncludesApproved: false,
  hasClaudeMdExternalIncludesWarningShown: false,
};

// Returns 'already' | 'seeded' | 'failed:<reason>' — the caller logs it, never throws. A failure
// here must not block the launch: the session still opens, it just stops on the trust prompt.
export function ensureTrusted(dir) {
  for (let attempt = 0; attempt < 3; attempt++) {
    let mtimeBefore;
    let cfg;
    try {
      mtimeBefore = fs.statSync(CONFIG).mtimeMs;
      cfg = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
    } catch (e) {
      return `failed:read ${e.message}`;
    }
    if (!cfg.projects) cfg.projects = {};
    const entry = cfg.projects[dir];
    if (entry && entry.hasTrustDialogAccepted === true) return 'already';
    cfg.projects[dir] = entry ? { ...entry, hasTrustDialogAccepted: true } : { ...NEW_ENTRY };

    // Commit only if nobody wrote the file while we were working; otherwise re-read and redo.
    try {
      if (fs.statSync(CONFIG).mtimeMs !== mtimeBefore) continue;
      const tmp = `${CONFIG}.orch-${process.pid}-${attempt}`;
      fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2));
      fs.renameSync(tmp, CONFIG);   // atomic within the same filesystem
      return 'seeded';
    } catch (e) {
      return `failed:write ${e.message}`;
    }
  }
  return 'failed:busy';   // three straight collisions — the prompt will just appear
}
