#!/usr/bin/env bash
# Boot the Fly session machine: home on the volume, every CLI signed in from secrets, the same config
# the Mac and the Pi read, then the session host (pi-desk with DESK_MACHINE=fly) in the foreground.
set -uo pipefail
mkdir -p "$HOME/.claude" "$REPO_ROOT"
exec > >(tee -a /data/desk.log) 2>&1
tail -n 20000 /data/desk.log > /data/desk.log.tmp 2>/dev/null && mv /data/desk.log.tmp /data/desk.log

# git over HTTPS through gh's credential helper (GH_TOKEN), committing as Jared, as on the other machines.
gh auth setup-git
git config --global user.name "Jared Gantt"
git config --global user.email "jaredgantt@gmail.com"
git config --global pull.ff only

# The shared CLAUDE.md, this machine's own, its settings and the skills — linked by the repo's installer.
if [ -d "$REPO_ROOT/claude-config/.git" ]; then git -C "$REPO_ROOT/claude-config" pull -q --ff-only || true
else gh repo clone JJGantt/claude-config "$REPO_ROOT/claude-config" -- -q; fi
"$REPO_ROOT/claude-config/install.sh" >/dev/null

# ~/.claude.json: onboarding done (a fresh interactive claude otherwise opens on a theme picker nobody
# can answer) and the MCP servers, with their credentials from secrets. Rewritten every boot so a
# changed secret takes effect; everything else Claude Code keeps in the file is preserved.
node - <<'JS'
const fs = require('fs'), f = `${process.env.HOME}/.claude.json`;
let c = {}; try { c = JSON.parse(fs.readFileSync(f, 'utf8')); } catch {}
const env = (...ks) => Object.fromEntries(ks.filter((k) => process.env[k]).map((k) => [k, process.env[k]]));
c.hasCompletedOnboarding = true;
c.mcpServers = {
  data: { type: 'stdio', command: 'node', args: ['/app/data-layer/dist/mcp.js'], env: env('SUPABASE_URL', 'SUPABASE_KEY'), alwaysLoad: true },
  'govee-lights': { type: 'stdio', command: 'python3', args: ['/app/govee-mcp/govee_mcp.py'], env: env('GOVEE_API_KEY', 'SUPABASE_URL', 'SUPABASE_KEY') },
  gmail: { type: 'stdio', command: '/opt/gmail-venv/bin/python', args: ['/app/gmail-mcp/gmail_mcp.py'],
           env: env('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN') },
};
fs.writeFileSync(f, JSON.stringify(c, null, 2));
JS

cd /app/host/pi-desk && exec node index.js
