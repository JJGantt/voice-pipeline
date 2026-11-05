// A follow-up addressed to a session this machine has just handed to the Mac.
//
// The sender picks the session's row when it sends, so a follow-up sent seconds after a transfer still
// names the row here: the transfer runs first, closes the pane, and the follow-up finds nothing to type
// into. The conversation is not gone — it is being resumed on the Mac under `mac-` and the first eight
// characters of the resume_mirrored command's id (orchestrator commands.js) — so the words go there.
// The Mac runs commands in the order they were created, so the resume lands before the forwarded words.
//
// Pure, so it is testable off the Pi; index.js reads the rows into it.

/**
 * The command to queue instead, or null when the follow-up is not for a moved session (and fails as
 * before). `row` is the session's desk_sessions row; `resume` is the newest resume_mirrored command for
 * its conversation.
 */
export function forwardMovedFollowUp(payload, row, resume, id) {
  if (row?.status !== 'transferred' || !row.claude_session_id) return null;
  const p = resume?.payload || {};
  if (p.claude_session_id !== row.claude_session_id || p.target !== 'mac') return null;
  return {
    id, kind: 'follow_up',
    payload: { ...payload, target: 'mac', session: `mac-${resume.id.slice(0, 8)}` },
  };
}
