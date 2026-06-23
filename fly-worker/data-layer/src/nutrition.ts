import { getClient } from './client.js';
import { normalizeMacroNutrition, type MacroNutrition, type MacroNutritionInput, ValidationError, uuid } from './helpers.js';
import { getRecipeNutrition } from './recipes.js';

export interface NutritionIngredient {
  name: string;
  food?: string; amount?: number | null; unit?: string;
  calories: number | null; protein_g: number | null; carbs_g: number | null; fat_g: number | null;
}

export interface NutritionEntry {
  id: string; date: string; occurred_at: string; name: string; quantity: number;
  calories: number | null; protein_g: number | null; carbs_g: number | null; fat_g: number | null;
  ingredients: NutritionIngredient[] | null; logged: string; staged?: boolean;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function fmtAmt(n: number): string { return String(Math.round(n * 1000) / 1000); }

function normalizeIngredients(raw: unknown, context: string): NutritionIngredient[] | null {
  if (raw == null) return null;
  if (!Array.isArray(raw)) throw new ValidationError(`${context}: ingredients must be an array.`);
  if (raw.length === 0) return null;
  return raw.map((ing: any, i: number) => {
    const food = (ing?.food || '').trim();
    const hasFood = !!food;
    const unit = (ing?.unit || '').trim();
    let amount: number | null = null;
    if (ing?.amount != null) {
      if (!isFiniteNumber(ing.amount)) throw new ValidationError(`${context}: ingredient #${i + 1} has a non-numeric amount.`);
      amount = Math.round(ing.amount * 1000) / 1000;
    }
    let name = (ing?.name || '').trim();
    if (!name) {
      if (hasFood) name = `${fmtAmt(amount ?? 1)}${unit ? ` ${unit}` : ''} ${food}`;
      else throw new ValidationError(`${context}: ingredient #${i + 1} requires a name.`);
    }
    const num = (v: unknown) => (v == null ? null : isFiniteNumber(v) ? Math.round(v * 10) / 10 : (() => { throw new ValidationError(`${context}: ingredient '${name}' has a non-numeric macro value.`); })());
    const macros = { calories: num(ing.calories), protein_g: num(ing.protein_g), carbs_g: num(ing.carbs_g), fat_g: num(ing.fat_g) };
    if (hasFood) return { name, food, amount, unit, ...macros };
    return { name, ...macros };
  });
}

function pad(v: number): string { return String(v).padStart(2, '0'); }
function localTimestamp(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function nowLocal(): string { return localTimestamp(new Date()); }
function normalizeOccurredAt(value?: string): string {
  const raw = (value || '').trim();
  if (!raw) return nowLocal();
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return `${raw}T12:00:00`;
  return raw;
}
// The "eating day" boundary: anything before 4 a.m. local counts toward the
// previous day, so late-night snacks roll into the day you were actually awake
// for rather than starting a fresh day at midnight.
const EATING_DAY_BOUNDARY_HOUR = 4;
function dateFromOccurredAt(value: string): string {
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2}))?/);
  if (!m) return nowLocal().slice(0, 10);
  const [y, mo, d, h, mi, s] = m.slice(1).map((v) => (v === undefined ? 0 : Number(v)));
  const dt = new Date(y, mo - 1, d, h, mi, s);
  dt.setHours(dt.getHours() - EATING_DAY_BOUNDARY_HOUR);
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
}
function scaleNutrition(n: MacroNutrition, qty: number): MacroNutrition {
  return {
    calories: Math.round(n.calories * qty * 10) / 10,
    protein_g: Math.round(n.protein_g * qty * 10) / 10,
    carbs_g: Math.round(n.carbs_g * qty * 10) / 10,
    fat_g: Math.round(n.fat_g * qty * 10) / 10,
  };
}

export async function logNutrition(items: {
  name?: string; recipe?: string; servings?: number;
  nutrition?: MacroNutritionInput; occurred_at?: string;
  ingredients?: NutritionIngredient[] | null;
}[], opts?: { staged?: boolean }): Promise<NutritionEntry[]> {
  const staged = opts?.staged ?? false;
  const loggedAt = nowLocal();
  const added: NutritionEntry[] = [];
  const sb = getClient();

  for (const item of items) {
    const occurredAt = normalizeOccurredAt(item.occurred_at);
    const day = dateFromOccurredAt(occurredAt);
    const servings = item.servings ?? 1.0;
    if (!Number.isFinite(servings) || servings <= 0) throw new ValidationError('Nutrition entries require servings to be greater than 0.');
    let nutr: MacroNutrition;
    let quantity = 1.0;
    let name = (item.name || '').trim();

    if (item.recipe) {
      const rn = await getRecipeNutrition(item.recipe);
      if (!rn) throw new ValidationError(`Recipe not found: ${item.recipe.trim()}`);
      name = item.recipe.trim();
      quantity = servings;
      nutr = scaleNutrition(rn, servings);
    } else {
      if (!name) throw new ValidationError('Manual nutrition entries require a name.');
      nutr = normalizeMacroNutrition(item.nutrition, `Manual nutrition entry '${name}'`);
    }

    const ingredients = normalizeIngredients(item.ingredients, `Nutrition entry '${name}'`);

    const id = uuid();
    const entry: NutritionEntry = { id, date: day, occurred_at: occurredAt, name, quantity, calories: nutr.calories, protein_g: nutr.protein_g, carbs_g: nutr.carbs_g, fat_g: nutr.fat_g, ingredients, logged: loggedAt, staged };
    const { data } = await sb.from('nutrition_entries').insert(entry).select().single();
    added.push(data as NutritionEntry);
  }
  return added;
}

// getNutrition returns only COMMITTED (eaten) entries — staged meals are never counted as eaten.
export async function getNutrition(date?: string): Promise<NutritionEntry[]> {
  const day = date ? dateFromOccurredAt(normalizeOccurredAt(date)) : dateFromOccurredAt(nowLocal());
  const { data } = await getClient().from('nutrition_entries').select('*').eq('date', day).eq('staged', false).order('logged');
  return (data ?? []) as NutritionEntry[];
}

// ---- staged meals ---------------------------------------------------------
// A staged meal you might eat / will eat later. Lives in nutrition_entries with staged=true, never
// counted in totals, and surfaced in the CURRENT day regardless of when it was staged. It only
// leaves the table when COMMITTED (→ a real entry stamped at commit time) or manually removed.

export async function stageNutrition(items: Parameters<typeof logNutrition>[0]): Promise<NutritionEntry[]> {
  return logNutrition(items, { staged: true });
}

// All staged meals (any day) — they all float into the current day's view.
export async function getStaged(): Promise<NutritionEntry[]> {
  const { data } = await getClient().from('nutrition_entries').select('*').eq('staged', true).order('logged');
  return (data ?? []) as NutritionEntry[];
}

async function findStaged(idPrefix: string): Promise<NutritionEntry | null> {
  const sb = getClient();
  let { data: row } = await sb.from('nutrition_entries').select('*').eq('id', idPrefix).eq('staged', true).maybeSingle();
  if (!row) {
    const { data: matches } = await sb.from('nutrition_entries').select('*').eq('staged', true).like('id', `${idPrefix}%`);
    if (matches?.length === 1) row = matches[0];
  }
  return (row as NutritionEntry) ?? null;
}

// Commit a staged meal: flip staged→false and re-stamp the time to NOW (the moment you eat it), so
// it lands as a normal entry on the current day and starts counting.
export async function commitStaged(idPrefix: string): Promise<NutritionEntry | null> {
  const row = await findStaged(idPrefix);
  if (!row) return null;
  const occurredAt = nowLocal();
  const { data } = await getClient().from('nutrition_entries')
    .update({ staged: false, occurred_at: occurredAt, date: dateFromOccurredAt(occurredAt), logged: occurredAt })
    .eq('id', row.id).select().single();
  return data as NutritionEntry;
}

// Edit a staged meal's name / macros / ingredients (replaces the supplied fields).
export async function editStaged(idPrefix: string, fields: {
  name?: string; nutrition?: MacroNutritionInput; ingredients?: NutritionIngredient[] | null;
}): Promise<NutritionEntry | null> {
  const row = await findStaged(idPrefix);
  if (!row) return null;
  const patch: Record<string, unknown> = {};
  if (fields.name != null && fields.name.trim()) patch.name = fields.name.trim();
  if (fields.nutrition != null) {
    const n = normalizeMacroNutrition(fields.nutrition, `Staged meal '${fields.name || row.name}'`);
    patch.calories = n.calories; patch.protein_g = n.protein_g; patch.carbs_g = n.carbs_g; patch.fat_g = n.fat_g;
  }
  if (fields.ingredients !== undefined) patch.ingredients = normalizeIngredients(fields.ingredients, `Staged meal '${fields.name || row.name}'`);
  if (Object.keys(patch).length === 0) return row;
  const { data } = await getClient().from('nutrition_entries').update(patch).eq('id', row.id).select().single();
  return data as NutritionEntry;
}

export async function removeStaged(idPrefix: string): Promise<NutritionEntry | null> {
  const row = await findStaged(idPrefix);
  if (!row) return null;
  await getClient().from('nutrition_entries').delete().eq('id', row.id);
  return row;
}

export function formatStagedList(entries: NutritionEntry[]): string {
  if (!entries.length) return 'No staged meals.';
  const lines = ['**Staged meals** (not eaten yet — commit to log)', '',
    '| ID | Meal | Cal | Protein | Carbs | Fat |', '|---|---|---|---|---|---|'];
  for (const e of entries)
    lines.push(`| \`${e.id.slice(0, 8)}\` | ${e.name} | ${e.calories ?? '?'} | ${e.protein_g ?? '?'}g | ${e.carbs_g ?? '?'}g | ${e.fat_g ?? '?'}g |`);
  return lines.join('\n');
}

export async function deleteNutritionEntry(idPrefix: string): Promise<NutritionEntry | null> {
  const sb = getClient();
  let { data: row } = await sb.from('nutrition_entries').select('*').eq('id', idPrefix).maybeSingle();
  if (!row) {
    const { data: matches } = await sb.from('nutrition_entries').select('*').like('id', `${idPrefix}%`);
    if (matches?.length === 1) row = matches[0];
  }
  if (!row) return null;
  await sb.from('nutrition_entries').delete().eq('id', row.id);
  return row as NutritionEntry;
}

export function formatNutritionDay(date: string, entries: NutritionEntry[]): string {
  if (!entries.length) return `No nutrition logged for ${date}.`;
  const totals = { calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0 };
  const lines = [
    `**Nutrition log — ${date}**`, '',
    '| ID | Food | Qty | Cal | Protein | Carbs | Fat |',
    '|---|---|---|---|---|---|---|',
  ];
  for (const e of entries) {
    const qtyStr = e.quantity !== 1.0 ? `${e.quantity}x` : '';
    lines.push(`| \`${e.id.slice(0, 8)}\` | ${e.name} | ${qtyStr} | ${e.calories ?? '?'} | ${e.protein_g ?? '?'}g | ${e.carbs_g ?? '?'}g | ${e.fat_g ?? '?'}g |`);
    totals.calories += e.calories || 0; totals.protein_g += e.protein_g || 0;
    totals.carbs_g += e.carbs_g || 0; totals.fat_g += e.fat_g || 0;
  }
  lines.push('|---|---|---|---|---|---|---|');
  lines.push(`| | **Total** | | **${Math.round(totals.calories)}** | **${Math.round(totals.protein_g)}g** | **${Math.round(totals.carbs_g)}g** | **${Math.round(totals.fat_g)}g** |`);
  return lines.join('\n');
}
