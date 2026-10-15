#!/usr/bin/env bash
# Every command a Pi session runs goes through here (CLAUDE_CODE_SHELL_PREFIX, set by launch.sh).
#
# Claude Code hands over the whole command as ONE string — its Bash tool's shell line, or a tool
# server's launch line — so it is run through the shell rather than exec'd as argv.
#
# The only thing this adds is an OOM score of 1000, the maximum. A session's commands then sit ahead
# of claude itself (800, launch.sh) in every out-of-memory decision — inside the session's own cgroup
# when it reaches its ceiling, and machine-wide — so what the kernel kills is the command that ran
# memory out, and the session survives to see the command fail. Raising a score needs no privilege.
echo 1000 > /proc/self/oom_score_adj 2>/dev/null || true
exec "${SHELL:-/bin/bash}" -c "$1"
