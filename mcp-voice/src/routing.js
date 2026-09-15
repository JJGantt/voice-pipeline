// Voice route/mode tools backed by Supabase app_settings. The conversational route is now DYNAMIC
// (Sonnet handles simple turns and escalates to Opus for careful data writes — there are no more
// model/thinking/runner knobs), so this just reports that route and toggles the two live voice modes
// (always-on / wake-free and voice-inject) plus JaredBot. The single FAB button drives the one dynamic route.

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;

function headers() {
  if (!SUPABASE_URL || !SUPABASE_KEY) throw new Error('SUPABASE_URL / SUPABASE_KEY not set in environment.');
  return { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`, 'Content-Type': 'application/json' };
}

async function readSettings(keys) {
  const inList = keys.join(',');
  const res = await fetch(`${SUPABASE_URL}/rest/v1/app_settings?key=in.(${inList})&select=key,value`, { headers: headers() });
  if (!res.ok) throw new Error(`read app_settings failed (${res.status})`);
  const rows = await res.json();
  return Object.fromEntries((rows || []).map((r) => [r.key, r.value]));
}

// Upsert one or more {key: value} settings (value coerced to string).
async function writeSettings(obj) {
  const rows = Object.entries(obj).map(([key, value]) => ({ key, value: String(value) }));
  const res = await fetch(`${SUPABASE_URL}/rest/v1/app_settings?on_conflict=key`, {
    method: 'POST',
    headers: { ...headers(), Prefer: 'resolution=merge-duplicates' },
    body: JSON.stringify(rows),
  });
  if (!res.ok) throw new Error(`write app_settings failed (${res.status}): ${(await res.text()).slice(0, 200)}`);
}

function truthy(v) {
  if (typeof v === 'boolean') return v;
  return ['1', 'true', 'on', 'yes', 'enable', 'enabled'].includes(String(v).toLowerCase().trim());
}

export async function getRouting() {
  const s = await readSettings(['wakefree_enabled', 'voice_inject_mode']);
  return {
    route: 'dynamic',
    route_label: 'Dynamic — Sonnet handles simple turns and escalates to Opus (max thinking) for anything needing careful data writes.',
    always_on: truthy(s.wakefree_enabled ?? '0'),   // hands-free, no-wake-word voice (aka 'AO' / 'wake-free')
    inject_mode: truthy(s.voice_inject_mode ?? '0'),   // voice-inject mode (transcripts injected into the conversation)
  };
}

// Always-on voice (aka 'AO' / 'wake-free' / hands-free) — toggles wakefree_enabled.
export async function setAlwaysOn(on) {
  const v = truthy(on);
  await writeSettings({ wakefree_enabled: v ? '1' : '0' });
  return `Always-on (wake-free) voice ${v ? 'enabled' : 'disabled'}.`;
}

// Voice-inject mode — toggles voice_inject_mode (the mac-listener reads it live).
export async function setInjectMode(on) {
  const v = truthy(on);
  await writeSettings({ voice_inject_mode: v ? '1' : '0' });
  return `Voice-inject mode ${v ? 'enabled' : 'disabled'}.`;
}

// JaredBot, the group-chat auto-replier (messages-mcp jaredbot.py) — toggles jaredbot_enabled,
// which it reads before every decision.
export async function setJaredBot(on) {
  const v = truthy(on);
  await writeSettings({ jaredbot_enabled: v ? '1' : '0' });
  return `JaredBot ${v ? 'enabled' : 'disabled'}.`;
}
