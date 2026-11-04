// How much memory a session's COMMANDS hold, as opposed to the session itself.
//
// A session's cgroup holds claude (~340MB), its tool servers (~230MB together) and every command it
// runs. The first two are ~575MB from the first minute and grow with the conversation, so the cgroup
// total cannot tell heavy work from a long chat: judged on the total, ordinary sessions running `sed`
// and `grep` crossed an 800MB line within minutes and were moved to the Mac mid-turn. What this Pi is
// not for is a COMMAND holding a lot of memory, so that is what is measured.
//
// A command is anything Claude Code's Bash tool started. Its shell line always begins by sourcing the
// session's shell snapshot (`~/.claude/shell-snapshots/snapshot-…`), and tool servers' launch lines
// never do, so a process whose command line carries that path is a command's root, and everything
// below it is the command's. Pure, so it is testable off the Pi; cgroups.js reads /proc into it.

const COMMAND_ROOT = 'shell-snapshots/snapshot-';

/**
 * Megabytes held by commands, from a process table: [{ pid, ppid, cmd, kb }], where `kb` is what the
 * process cannot give back (anon resident plus swapped). Each process counts once, under the nearest
 * command root above it or not at all.
 */
export function commandMb(procs) {
  const byPid = new Map(procs.map((p) => [p.pid, p]));
  const isCommand = (p) => {
    for (let q = p, n = 0; q && n < 64; q = byPid.get(q.ppid), n++) {
      if (q.cmd.includes(COMMAND_ROOT)) return true;
    }
    return false;
  };
  const kb = procs.reduce((t, p) => t + (isCommand(p) ? p.kb : 0), 0);
  return Math.round(kb / 1024);
}
