// Routing/agent-config tools backed by Supabase app_settings. Lets a voice or chat agent read and
// change which model, thinking level, and runner the conversational route uses, and toggle the
// wake-free voice system. The three setters write the Custom-route knobs AND flip the conversational
// routes (chatbox_route + wakefree_route) to 'custom', so the change is live immediately. The FAB
// buttons keep their own explicit fast/power tiers and are unaffected.

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;

const MODELS = ['haiku', 'sonnet', 'opus'];
const THINK = ['none', 'medium', 'max'];
const RUNNERS = ['local', 'api'];
const MODEL_LABEL = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus' };
const THINK_LABEL = { none: 'None', medium: 'Medium', max: 'Maximum' };
const RUNNER_LABEL = { local: 'Local (subscription)', api: 'API' };

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

// Lenient parsing of spoken/typed values to the canonical stored value.
function normModel(v) {
  v = String(v || '').toLowerCase();
  if (v.includes('haiku')) return 'haiku';
  if (v.includes('sonnet')) return 'sonnet';
  if (v.includes('opus')) return 'opus';
  return null;
}
function normThink(v) {
  v = String(v || '').toLowerCase().trim();
  if (['none', 'off', 'no', 'no thinking', '0', 'disable', 'disabled'].includes(v)) return 'none';
  if (['medium', 'med', 'mid', 'middle', 'low', 'some', 'moderate', 'normal'].includes(v)) return 'medium';
  if (['max', 'maximum', 'high', 'highest', 'most', 'ultra', 'ultrathink', 'full'].includes(v)) return 'max';
  return null;
}
function normRunner(v) {
  v = String(v || '').toLowerCase().trim();
  if (['api', 'cloud', 'anthropic', 'remote', 'messages'].includes(v)) return 'api';
  if (['local', 'subscription', 'sub', 'mac', 'pi', 'claude -p', 'free'].includes(v)) return 'local';
  return null;
}
function truthy(v) {
  if (typeof v === 'boolean') return v;
  return ['1', 'true', 'on', 'yes', 'enable', 'enabled'].includes(String(v).toLowerCase().trim());
}

// Activate the Custom route on both conversational entry points, plus any knob changes in one upsert.
async function activateCustom(extra) {
  await writeSettings({ chatbox_route: 'custom', wakefree_route: 'custom', ...extra });
}

export async function getRouting() {
  const s = await readSettings(['custom_model', 'custom_think', 'custom_runner', 'wakefree_enabled', 'chatbox_route', 'wakefree_route']);
  const model = MODELS.includes(s.custom_model) ? s.custom_model : 'sonnet';
  const thinking = THINK.includes(s.custom_think) ? s.custom_think : 'medium';
  const runner = s.custom_runner === 'api' ? 'api' : 'local';
  const alwaysOn = truthy(s.wakefree_enabled ?? '0');
  const active = s.chatbox_route === 'custom' || s.wakefree_route === 'custom';
  return {
    model, model_label: MODEL_LABEL[model],
    thinking, thinking_label: THINK_LABEL[thinking],
    runner, runner_label: RUNNER_LABEL[runner],
    always_on: alwaysOn,   // hands-free, no-wake-word voice (aka 'AO' / 'wake-free')
    custom_route_active: active,
    note: active
      ? 'Custom route is active, so these are what the conversational agent uses now.'
      : 'Custom route is NOT the active route — these are staged; set any of them to make Custom live.',
  };
}

export async function setModel(v) {
  const m = normModel(v);
  if (!m) throw new Error(`Unknown model "${v}". Choose haiku, sonnet, or opus.`);
  await activateCustom({ custom_model: m });
  return `Model set to ${MODEL_LABEL[m]} (Custom route now active).`;
}

export async function setThinking(v) {
  const t = normThink(v);
  if (!t) throw new Error(`Unknown thinking level "${v}". Choose none, medium, or maximum.`);
  await activateCustom({ custom_think: t });
  const extra = t === 'max' ? ' — ultrathink injected.' : '';
  return `Thinking set to ${THINK_LABEL[t]} (Custom route now active)${extra}`;
}

export async function setRunner(v) {
  const r = normRunner(v);
  if (!r) throw new Error(`Unknown runner "${v}". Choose local (subscription) or api.`);
  await activateCustom({ custom_runner: r });
  return `Runner set to ${RUNNER_LABEL[r]} (Custom route now active).`;
}

// Always-on voice (aka 'AO' / 'wake-free' / hands-free) — toggles wakefree_enabled.
export async function setAlwaysOn(on) {
  const v = truthy(on);
  await writeSettings({ wakefree_enabled: v ? '1' : '0' });
  return `Always-on (wake-free) voice ${v ? 'enabled' : 'disabled'}.`;
}
