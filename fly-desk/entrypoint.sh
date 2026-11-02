#!/usr/bin/env bash
# Boot the Fly session machine: home on the volume, every CLI signed in from secrets, the same config
# the Mac and the Pi read, then the session host (pi-desk with DESK_MACHINE=fly) in the foreground.
set -uo pipefail
mkdir -p "$HOME/.claude" "$REPO_ROOT"
# Trimmed BEFORE tee opens it: replacing the file afterwards leaves tee writing to the unlinked one.
tail -n 20000 /data/desk.log > /data/desk.log.tmp 2>/dev/null && mv /data/desk.log.tmp /data/desk.log
exec > >(tee -a /data/desk.log) 2>&1

# git over HTTPS through gh's credential helper (GH_TOKEN), committing as the owner, as on the other machines.
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

# Codex's config: the same MCP servers, each with its tools approved (a Codex session runs with approvals
# off, which refuses any MCP tool without it). Rewritten every boot like ~/.claude.json; the login
# (auth.json) beside it is left alone.
mkdir -p "$CODEX_HOME"
node - <<'JS' > "$CODEX_HOME/config.toml"
const env = (...ks) => '{ ' + ks.filter((k) => process.env[k]).map((k) => `${k} = ${JSON.stringify(process.env[k])}`).join(', ') + ' }';
const srv = (name, command, args, e) => `[mcp_servers.${name}]\ncommand = ${JSON.stringify(command)}\nargs = ${JSON.stringify(args)}\n`
  + `env = ${e}\ndefault_tools_approval_mode = "approve"\n`;
process.stdout.write([
  'approval_policy = "never"', 'sandbox_mode = "danger-full-access"', 'check_for_update_on_startup = false', '',
  srv('data', 'node', ['/app/data-layer/dist/mcp.js'], env('SUPABASE_URL', 'SUPABASE_KEY')),
  srv('govee-lights', 'python3', ['/app/govee-mcp/govee_mcp.py'], env('GOVEE_API_KEY', 'SUPABASE_URL', 'SUPABASE_KEY')),
  srv('gmail', '/opt/gmail-venv/bin/python', ['/app/gmail-mcp/gmail_mcp.py'], env('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN')),
].join('\n'));
JS
chmod 600 "$CODEX_HOME/config.toml"

cd /app/host/pi-desk && exec node index.js
