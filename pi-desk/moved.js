// A follow-up addressed to a session this machine has just handed to another machine.
//
// The sender picks the session's row when it sends, so a follow-up sent seconds after a transfer still
// names the row here: the transfer runs first, closes the pane, and the follow-up finds nothing to type
// into. The conversation is not gone — it is being resumed elsewhere under a name made from the
// resume_mirrored command's id — so the words go there:
//
//   the Mac:       `mac-` and the first eight characters of the id (orchestrator commands.js)
//   the Pi or Fly: `hub-<repo>-` and the same eight characters (pi-desk resume_mirrored)
//
// Each consumer runs commands in the order they were created, so the resume lands before the words.
//
// Pure, so it is testable off the Pi; index.js reads the rows into it.

const HUB_NAME = /^[A-Za-z0-9._-]+$/;

/**
 * The command to send instead, or null when the follow-up is not for a moved session (and fails as
 * before). `row` is the session's desk_sessions row; `resume` is the newest resume_mirrored command for
 * its conversation.
 */
export function forwardMovedFollowUp(payload, row, resume, id) {
  if (row?.status !== 'transferred' || !row.claude_session_id) return null;
  const p = resume?.payload || {};
  if (p.claude_session_id !== row.claude_session_id || !resume.id) return null;
  const short = resume.id.slice(0, 8);
  let session;
  if (p.target === 'mac') session = `mac-${short}`;
  else if (p.target === 'pi' || p.target === 'fly') {
    const repo = String(p.repo || '').trim() || 'root';
    if (!HUB_NAME.test(repo)) return null;
    session = `hub-${repo}-${short}`;
  } else return null;
  return { id, kind: 'follow_up', payload: { ...payload, target: p.target, session } };
}
