import { getClient } from './client.js';
import { deriveCaloriesFromMacros, type MacroNutrition, type MacroNutritionInput, ValidationError, uuid } from './helpers.js';
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
  warnings?: string[];   // non-persisted: macro/calorie sanity warnings surfaced back to the caller
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
function round10(v: number): number { return Math.round(v * 10) / 10; }

function fmtAmt(n: number): string { return String(Math.round(n * 1000) / 1000); }

// Human-readable label for one ingredient: structured rows → "1 cup rolled oats" / "0.5 banana";
// legacy rows (name only) → their stored name.
function ingLabel(g: NutritionIngredient): string {
  if (g.food && g.amount != null && isFiniteNumber(g.amount)) {
    const un = (g.unit || '').trim();
    return `${fmtAmt(g.amount)}${un ? ` ${un}` : ''} ${g.food}`;
  }
  return g.name || '—';
}

// A meal's calories legitimately diverge from 4/4/9·macros (fiber ≈ 2 cal/g not 4, sugar alcohols ≈ 0,
// and alcohol is 7 cal/g but not protein/carb/fat at all). So we TRUST stated ingredient calories and
// only SOFT-WARN when they're wildly out of line with the macros — the signature of a bad estimate
// (e.g. milk logged as 190 cal but 6 P / 8 C, which only Atwaters to ~56). Non-blocking; surfaced back.
const CAL_WARN_RATIO = 0.30;   // |stated − atwater| / atwater above this …
const CAL_WARN_ABS = 40;       // … AND at least this many calories apart (don't nag on tiny foods).

// Validate/clean one ingredient. P/C/F are REQUIRED and numeric (they're the source of truth now);
// calories are optional and Atwater-derived per-ingredient when absent. Pushes a soft warning when a
// STATED calorie value is grossly inconsistent with the ingredient's own macros.
function normalizeIngredient(ing: any, context: string, i: number, warnings: string[]): NutritionIngredient {
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
  const reqMacro = (v: unknown, field: string): number => {
    if (!isFiniteNumber(v)) throw new ValidationError(`${context}: ingredient '${name}' requires a numeric ${field}.`);
    if (v < 0) throw new ValidationError(`${context}: ingredient '${name}' has a negative ${field}.`);
    return round10(v);
  };
  const protein_g = reqMacro(ing?.protein_g, 'protein_g');
  const carbs_g = reqMacro(ing?.carbs_g, 'carbs_g');
  const fat_g = reqMacro(ing?.fat_g, 'fat_g');

  const atwater = deriveCaloriesFromMacros(protein_g, carbs_g, fat_g);
  let calories: number;
  if (ing?.calories == null) {
    calories = atwater;                      // no stated calories → fall back to Atwater for THIS ingredient
  } else {
    if (!isFiniteNumber(ing.calories)) throw new ValidationError(`${context}: ingredient '${name}' has a non-numeric calorie value.`);
    if (ing.calories < 0) throw new ValidationError(`${context}: ingredient '${name}' has negative calories.`);
    calories = round10(ing.calories);
    const diff = Math.abs(calories - atwater);
    if (diff >= CAL_WARN_ABS && diff / Math.max(atwater, 1) > CAL_WARN_RATIO) {
      warnings.push(`'${name}': ${calories} cal stated but ${protein_g}P/${carbs_g}C/${fat_g}F only implies ~${atwater} cal — double-check the macros (or it's an alcohol/high-fiber food).`);
    }
  }
  const macros = { calories, protein_g, carbs_g, fat_g };
  return hasFood ? { name, food, amount, unit, ...macros } : { name, ...macros };
}

// Parse the ingredient list (≥1 required) and the warnings raised while validating it.
function normalizeIngredients(raw: unknown, context: string): { ingredients: NutritionIngredient[]; warnings: string[] } {
  if (!Array.isArray(raw) || raw.length === 0) throw new ValidationError(`${context}: at least one ingredient is required.`);
  const warnings: string[] = [];
  const ingredients = raw.map((ing: any, i: number) => normalizeIngredient(ing, context, i, warnings));
  return { ingredients, warnings };
}

// The entry's totals ARE the sum of its ingredients — every field summed independently. This is the
// single source of truth for calories/protein/carbs/fat; nothing else may set them.
function rollup(ingredients: NutritionIngredient[]): MacroNutrition {
  const t = { calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0 };
  for (const g of ingredients) {
    t.calories += g.calories ?? 0; t.protein_g += g.protein_g ?? 0;
    t.carbs_g += g.carbs_g ?? 0; t.fat_g += g.fat_g ?? 0;
  }
  return { calories: round10(t.calories), protein_g: round10(t.protein_g), carbs_g: round10(t.carbs_g), fat_g: round10(t.fat_g) };
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
    calories: round10(n.calories * qty),
    protein_g: round10(n.protein_g * qty),
    carbs_g: round10(n.carbs_g * qty),
    fat_g: round10(n.fat_g * qty),
  };
}

// Build the ingredient list for one logged item. EVERY entry is ≥1 ingredient:
//  · explicit `ingredients` (a composite meal) → used as-is
//  · a recipe → one ingredient carrying the recipe's macros × servings
//  · a simple food (top-level `nutrition`, no ingredients) → one name-only ingredient
// The caller then derives the entry totals by summing these ingredients (never the other way around).
async function resolveItemIngredients(item: {
  name?: string; recipe?: string; servings?: number; nutrition?: MacroNutritionInput; ingredients?: NutritionIngredient[] | null;
}, servings: number): Promise<{ name: string; quantity: number; raw: any[] }> {
  if (item.recipe) {
    const rn = await getRecipeNutrition(item.recipe);
    if (!rn) throw new ValidationError(`Recipe not found: ${item.recipe.trim()}`);
    const name = item.recipe.trim();
    const scaled = scaleNutrition(rn, servings);
    return { name, quantity: servings, raw: [{ name, ...scaled }] };
  }
  const name = (item.name || '').trim();
  if (!name) throw new ValidationError('Manual nutrition entries require a name.');
  if (Array.isArray(item.ingredients) && item.ingredients.length) {
    return { name, quantity: 1.0, raw: item.ingredients as any[] };
  }
  // Simple food: collapse the top-level nutrition into a single name-only ingredient.
  if (item.nutrition == null) throw new ValidationError(`Nutrition entry '${name}' requires either ingredients or nutrition.`);
  return { name, quantity: 1.0, raw: [{ name, ...item.nutrition }] };
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

    const { name, quantity, raw } = await resolveItemIngredients(item, servings);
    const { ingredients, warnings } = normalizeIngredients(raw, `Nutrition entry '${name}'`);
    const nutr = rollup(ingredients);   // totals = sum of ingredients, always

    const id = uuid();
    const entry: NutritionEntry = { id, date: day, occurred_at: occurredAt, name, quantity, calories: nutr.calories, protein_g: nutr.protein_g, carbs_g: nutr.carbs_g, fat_g: nutr.fat_g, ingredients, logged: loggedAt, staged };
    const { data } = await sb.from('nutrition_entries').insert(entry).select().single();
    const saved = data as NutritionEntry;
    if (warnings.length) saved.warnings = warnings;
    added.push(saved);
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

// Edit a staged meal's name and/or ingredients. Macros are NEVER set directly — any edit that changes
// the food rebuilds the ingredient list and re-derives the entry totals from it, so parent == sum of
// ingredients always holds. A bare `nutrition` (simple food) is folded into a single name-only ingredient.
export async function editStaged(idPrefix: string, fields: {
  name?: string; nutrition?: MacroNutritionInput; ingredients?: NutritionIngredient[] | null;
}): Promise<NutritionEntry | null> {
  const row = await findStaged(idPrefix);
  if (!row) return null;
  const newName = fields.name != null && fields.name.trim() ? fields.name.trim() : row.name;
  const patch: Record<string, unknown> = {};
  if (newName !== row.name) patch.name = newName;

  // Decide the new ingredient list (if the food is being edited at all).
  let raw: any[] | undefined;
  if (Array.isArray(fields.ingredients) && fields.ingredients.length) raw = fields.ingredients as any[];
  else if (fields.nutrition != null) raw = [{ name: newName, ...fields.nutrition }];

  let warnings: string[] = [];
  if (raw) {
    const out = normalizeIngredients(raw, `Staged meal '${newName}'`);
    const nutr = rollup(out.ingredients);
    patch.ingredients = out.ingredients;
    patch.calories = nutr.calories; patch.protein_g = nutr.protein_g; patch.carbs_g = nutr.carbs_g; patch.fat_g = nutr.fat_g;
    warnings = out.warnings;
  }
  if (Object.keys(patch).length === 0) return row;
  const { data } = await getClient().from('nutrition_entries').update(patch).eq('id', row.id).select().single();
  const saved = data as NutritionEntry;
  if (warnings.length) saved.warnings = warnings;
  return saved;
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
    // Composite meals (>1 ingredient): render each component as an indented sub-row so callers see the
    // structured breakdown. A single-ingredient entry IS its own breakdown, so it renders flat (no sub-row).
    // Sub-rows are NOT added to the day totals — the entry row already carries the summed macros.
    if (Array.isArray(e.ingredients) && e.ingredients.length > 1) {
      for (const g of e.ingredients) {
        lines.push(`| | ↳ ${ingLabel(g)} | | ${g.calories ?? '?'} | ${g.protein_g ?? '?'}g | ${g.carbs_g ?? '?'}g | ${g.fat_g ?? '?'}g |`);
      }
    }
    totals.calories += e.calories || 0; totals.protein_g += e.protein_g || 0;
    totals.carbs_g += e.carbs_g || 0; totals.fat_g += e.fat_g || 0;
  }
  lines.push('|---|---|---|---|---|---|---|');
  lines.push(`| | **Total** | | **${Math.round(totals.calories)}** | **${Math.round(totals.protein_g)}g** | **${Math.round(totals.carbs_g)}g** | **${Math.round(totals.fat_g)}g** |`);
  return lines.join('\n');
}
