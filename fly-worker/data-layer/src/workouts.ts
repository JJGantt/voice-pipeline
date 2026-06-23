import { getClient } from './client.js';
import { now, toEastern, fmtWeight } from './helpers.js';

export const VALID_CATEGORIES = ['push', 'pull', 'legs', 'core', 'cardio'] as const;
const CAT_ORDER: Record<string, number> = { push: 0, pull: 1, legs: 2, core: 3, cardio: 4 };
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// High-fidelity muscle taxonomy, grouped + ordered for display. Exercises map to these via
// fractional set counting (1.0 primary / 0.5 secondary), stored in exercise_muscles.
const MUSCLE_GROUPS: { group: string; muscles: { slug: string; label: string }[] }[] = [
  { group: 'Legs', muscles: [
    { slug: 'quads', label: 'Quads' }, { slug: 'rectus_femoris', label: 'Rectus femoris' },
    { slug: 'hamstrings', label: 'Hamstrings' }, { slug: 'glutes', label: 'Glutes' },
    { slug: 'adductors', label: 'Adductors' }, { slug: 'hip_flexors', label: 'Hip flexors' },
    { slug: 'gastrocnemius', label: 'Gastrocnemius' }, { slug: 'soleus', label: 'Soleus' } ] },
  { group: 'Push', muscles: [
    { slug: 'upper_chest', label: 'Upper chest' }, { slug: 'lower_chest', label: 'Lower chest' },
    { slug: 'front_delts', label: 'Front delts' }, { slug: 'side_delts', label: 'Side delts' },
    { slug: 'triceps_long', label: 'Triceps (long head)' }, { slug: 'triceps_lateral', label: 'Triceps (lateral)' } ] },
  { group: 'Pull', muscles: [
    { slug: 'lats', label: 'Lats' }, { slug: 'upper_traps', label: 'Upper traps' },
    { slug: 'mid_back', label: 'Mid-back' }, { slug: 'rear_delts', label: 'Rear delts' },
    { slug: 'biceps', label: 'Biceps' }, { slug: 'forearms', label: 'Forearms' } ] },
  { group: 'Core', muscles: [
    { slug: 'abs', label: 'Abs' }, { slug: 'obliques', label: 'Obliques' },
    { slug: 'erectors', label: 'Lower back' } ] },
];
const MUSCLE_LABEL: Record<string, string> = Object.fromEntries(
  MUSCLE_GROUPS.flatMap(g => g.muscles.map(m => [m.slug, m.label])));
export const VALID_MUSCLES = Object.keys(MUSCLE_LABEL);

/** Validate a fractional muscle map: { slug: contribution } with known slugs and 0 < c <= 1. */
export function validateMuscles(muscles: unknown): Record<string, number> {
  if (typeof muscles !== 'object' || muscles === null || Array.isArray(muscles))
    throw new Error(`muscles required for a new exercise: an object like {"quads":1.0,"glutes":0.5}. Valid muscles: ${VALID_MUSCLES.join(', ')}`);
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(muscles as Record<string, unknown>)) {
    const slug = String(k).trim().toLowerCase();
    if (!MUSCLE_LABEL[slug]) throw new Error(`invalid muscle '${k}'. Valid muscles: ${VALID_MUSCLES.join(', ')}`);
    const c = Number(v);
    if (!(c > 0 && c <= 1)) throw new Error(`muscle '${k}' contribution must be > 0 and <= 1 (use 1.0 primary, 0.5 secondary)`);
    out[slug] = c;
  }
  if (!Object.keys(out).length) throw new Error('muscles map cannot be empty for a new exercise');
  return out;
}

/** Validate + normalize a category set: a non-empty subset of VALID_CATEGORIES. Throws on bad input. */
export function validateCategories(cats: unknown): string[] {
  if (!Array.isArray(cats) || cats.length === 0)
    throw new Error(`categories required: a non-empty set drawn from ${VALID_CATEGORIES.join(', ')}`);
  const norm = [...new Set(cats.map(c => String(c).trim().toLowerCase()))];
  for (const c of norm)
    if (!(VALID_CATEGORIES as readonly string[]).includes(c))
      throw new Error(`invalid category '${c}'. Allowed: ${VALID_CATEGORIES.join(', ')}`);
  return norm.sort((a, b) => CAT_ORDER[a] - CAT_ORDER[b]);
}

/** Human label for a derived category set: "Full body", "Push / Legs", "Pull / Legs + Core". */
export function categoryLabel(cats: string[]): string {
  if (!cats.length) return 'Untyped';
  const ppl = ['push', 'pull', 'legs'].filter(c => cats.includes(c));
  const extra = cats.filter(c => !['push', 'pull', 'legs'].includes(c));
  const parts: string[] = ppl.length === 3 ? ['Full body'] : ppl.map(cap);
  parts.push(...extra.map(cap));
  return parts.join(' / ') || 'Untyped';
}

// ---- catalog (normalized: exercise_catalog + exercise_muscles) ----
export interface CatalogExercise { name: string; categories: string[]; muscles: Record<string, number>; }

/** Categories + muscle map keyed by exercise name, loaded once per operation. */
async function catalogMaps(): Promise<{ cats: Map<string, string[]>; muscles: Map<string, { muscle: string; contribution: number }[]> }> {
  const sb = getClient();
  const { data: catRows } = await sb.from('exercise_catalog').select('name, categories');
  const { data: muscleRows } = await sb.from('exercise_muscles').select('name, muscle, contribution');
  const cats = new Map<string, string[]>();
  for (const c of (catRows ?? [])) cats.set(c.name.toLowerCase(), (c.categories ?? []) as string[]);
  const muscles = new Map<string, { muscle: string; contribution: number }[]>();
  for (const m of (muscleRows ?? [])) {
    const k = m.name.toLowerCase();
    if (!muscles.has(k)) muscles.set(k, []);
    muscles.get(k)!.push({ muscle: m.muscle, contribution: m.contribution });
  }
  return { cats, muscles };
}

export async function getCatalog(): Promise<{ categories: string[]; muscles: string[]; exercises: CatalogExercise[] }> {
  const sb = getClient();
  const { data: catRows } = await sb.from('exercise_catalog').select('name, categories');
  const { data: muscleRows } = await sb.from('exercise_muscles').select('name, muscle, contribution');
  const byName = new Map<string, Record<string, number>>();
  for (const m of (muscleRows ?? [])) {
    if (!byName.has(m.name)) byName.set(m.name, {});
    byName.get(m.name)![m.muscle] = m.contribution;
  }
  const exercises = (catRows ?? []).map(c => ({
    name: c.name, categories: (c.categories ?? []) as string[], muscles: byName.get(c.name) ?? {},
  })).sort((a, b) => a.name.localeCompare(b.name));
  return { categories: [...VALID_CATEGORIES], muscles: [...VALID_MUSCLES], exercises };
}

async function lookupCatalog(name: string): Promise<{ name: string; categories: string[] } | null> {
  const { data } = await getClient().from('exercise_catalog').select('name, categories').ilike('name', name.trim()).maybeSingle();
  return data ? { name: data.name, categories: (data.categories ?? []) as string[] } : null;
}

/** Create or replace a catalog entry (categories + fractional muscle map). Fixes mistakes too. */
export async function upsertCatalogExercise(name: string, categories: unknown, muscles: unknown): Promise<CatalogExercise> {
  const sb = getClient();
  const nm = String(name).trim();
  if (!nm) throw new Error('exercise name required');
  const cats = validateCategories(categories);
  const mus = validateMuscles(muscles);
  const existing = await lookupCatalog(nm);
  const canonical = existing?.name ?? nm;
  if (existing) {
    await sb.from('exercise_catalog').update({ categories: cats }).eq('name', canonical);
    await sb.from('exercise_muscles').delete().eq('name', canonical);
  } else {
    await sb.from('exercise_catalog').insert({ name: canonical, categories: cats });
  }
  const rows = Object.entries(mus).map(([muscle, contribution]) => ({ name: canonical, muscle, contribution }));
  if (rows.length) await sb.from('exercise_muscles').insert(rows);
  return { name: canonical, categories: cats, muscles: mus };
}

// ---- sets (the flat log) ----
export interface SetRow { id: number; occurred_at: string; exercise: string; weight_lbs: number; reps: number; }

/**
 * Log one or more sets. Each set is its own timestamped row. Unknown exercises are rejected
 * unless they carry categories + a muscles map, in which case they're registered in the catalog.
 * Sets in a single call share the call's timestamp (logged together); log set-by-set for real rest gaps.
 */
export async function logSets(items: { exercise: string; weight_lbs: number; reps: number; categories?: string[]; muscles?: Record<string, number> }[]): Promise<SetRow[]> {
  const sb = getClient();
  const ts = now();
  const rows: { occurred_at: string; exercise: string; weight_lbs: number; reps: number }[] = [];
  for (const it of items) {
    const nm = String(it.exercise || '').trim();
    if (!nm) throw new Error('each set needs an exercise name');
    const entry = await lookupCatalog(nm);
    let canonical: string;
    if (entry) {
      canonical = entry.name;
    } else if (it.muscles && Object.keys(it.muscles).length) {
      const created = await upsertCatalogExercise(nm, it.categories, it.muscles);
      canonical = created.name;
    } else {
      throw new Error(`Unknown exercise '${nm}'. Call get_workout_catalog and reuse the EXACT existing name. If it is genuinely new, include categories and a muscles map (e.g. {"quads":1.0,"glutes":0.5}) so it is added to the catalog.`);
    }
    if (!Number.isFinite(it.weight_lbs) || it.weight_lbs < 0) throw new Error(`'${nm}': weight_lbs must be >= 0 (bodyweight = 0)`);
    if (!Number.isInteger(it.reps) || it.reps <= 0) throw new Error(`'${nm}': reps must be a positive integer`);
    rows.push({ occurred_at: ts, exercise: canonical, weight_lbs: it.weight_lbs, reps: it.reps });
  }
  const { data } = await sb.from('sets').insert(rows).select();
  return (data ?? []) as SetRow[];
}

export async function removeSet(id: number): Promise<SetRow | null> {
  const sb = getClient();
  const { data: row } = await sb.from('sets').select('*').eq('id', id).maybeSingle();
  if (!row) return null;
  await sb.from('sets').delete().eq('id', id);
  return row as SetRow;
}

export async function updateSet(id: number, weight_lbs: number, reps: number): Promise<SetRow | null> {
  const sb = getClient();
  const { data } = await sb.from('sets').update({ weight_lbs, reps }).eq('id', id).select().maybeSingle();
  return (data ?? null) as SetRow | null;
}

/** All sets for a given local day (defaults today), ordered by time. */
export async function getDaySets(date?: string): Promise<{ date: string; sets: SetRow[] }> {
  const day = (date && /^\d{4}-\d{2}-\d{2}$/.test(date.trim())) ? date.trim() : toEastern(new Date()).slice(0, 10);
  const { data } = await getClient().from('sets').select('*')
    .gte('occurred_at', `${day}T00:00:00`).lte('occurred_at', `${day}T23:59:59`).order('occurred_at');
  return { date: day, sets: (data ?? []) as SetRow[] };
}

/** Recent workout days (grouped from sets), newest first. */
export async function listWorkoutDays(limit = 10): Promise<{ date: string; setCount: number; categories: string[]; exercises: string[] }[]> {
  const { data } = await getClient().from('sets').select('occurred_at, exercise').order('occurred_at', { ascending: false }).limit(2000);
  const { cats } = await catalogMaps();
  const byDay = new Map<string, { setCount: number; cats: Set<string>; ex: Set<string> }>();
  for (const s of (data ?? [])) {
    const d = String(s.occurred_at).slice(0, 10);
    if (!byDay.has(d)) byDay.set(d, { setCount: 0, cats: new Set(), ex: new Set() });
    const e = byDay.get(d)!;
    e.setCount++; e.ex.add(s.exercise);
    for (const c of (cats.get(String(s.exercise).toLowerCase()) ?? [])) e.cats.add(c);
  }
  return [...byDay.entries()]
    .sort((a, b) => b[0].localeCompare(a[0]))
    .slice(0, limit)
    .map(([date, e]) => ({ date, setCount: e.setCount, categories: [...e.cats].sort((a, b) => CAT_ORDER[a] - CAT_ORDER[b]), exercises: [...e.ex] }));
}

export async function getExerciseHistory(name: string, limit = 60): Promise<{ date: string; sets: { weight_lbs: number; reps: number }[] }[]> {
  const { data } = await getClient().from('sets').select('occurred_at, weight_lbs, reps')
    .ilike('exercise', name.trim()).order('occurred_at', { ascending: false }).limit(limit);
  const byDay = new Map<string, { weight_lbs: number; reps: number }[]>();
  for (const s of (data ?? [])) {
    const d = String(s.occurred_at).slice(0, 10);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d)!.push({ weight_lbs: s.weight_lbs, reps: s.reps });
  }
  return [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([date, sets]) => ({ date, sets }));
}

export interface MuscleVolume { muscle: string; label: string; group: string; sets: number; }

/** Trailing-window fractional set volume per muscle, summed across all logged sets. */
export async function getWeeklyVolume(days = 7): Promise<{ days: number; volumes: MuscleVolume[] }> {
  const since = toEastern(new Date(Date.now() - days * 86400000)).slice(0, 10);
  const { data: setRows } = await getClient().from('sets').select('exercise, muscles_override').gte('occurred_at', since);
  const { muscles } = await catalogMaps();

  const totals = new Map<string, number>();
  for (const s of (setRows ?? [])) {
    const override = (s as any).muscles_override as Record<string, number> | null;
    const map = override
      ? Object.entries(override).map(([muscle, contribution]) => ({ muscle, contribution }))
      : (muscles.get(String(s.exercise).toLowerCase()) ?? []);
    for (const mm of map) totals.set(mm.muscle, (totals.get(mm.muscle) ?? 0) + mm.contribution);
  }
  const volumes = MUSCLE_GROUPS.flatMap(g => g.muscles.map(m => ({
    muscle: m.slug, label: m.label, group: g.group,
    sets: Math.round((totals.get(m.slug) ?? 0) * 10) / 10,
  })));
  return { days, volumes };
}

export function formatWeeklyVolume(result: { days: number; volumes: MuscleVolume[] }): string {
  const status = (s: number) => s === 0 ? '▫️ none' : s < 10 ? '⚠️ below' : s <= 20 ? '✅' : '🔺 high';
  const lines = [`**Weekly volume** — trailing ${result.days} days, fractional sets (target ~10–20 / muscle)`];
  let lastGroup = '';
  for (const v of result.volumes) {
    if (v.group !== lastGroup) { lines.push('', `_${v.group}_`); lastGroup = v.group; }
    lines.push(`  ${v.label.padEnd(20)} ${String(v.sets).padStart(4)}  ${status(v.sets)}`);
  }
  lines.push('', '_Smaller muscles (calves, arms, delts) tolerate the upper end; legs/back the middle._');
  return lines.join('\n');
}

/** A day's sets as a grouped table (by exercise, in order first logged). */
export async function formatDay(date?: string): Promise<string> {
  const { date: day, sets } = await getDaySets(date);
  if (!sets.length) return `No sets logged for ${day}.`;
  const { cats } = await catalogMaps();
  const order: string[] = [];
  const byEx = new Map<string, SetRow[]>();
  for (const s of sets) {
    if (!byEx.has(s.exercise)) { byEx.set(s.exercise, []); order.push(s.exercise); }
    byEx.get(s.exercise)!.push(s);
  }
  const allCats = new Set<string>();
  for (const ex of order) for (const c of (cats.get(ex.toLowerCase()) ?? [])) allCats.add(c);
  const lines = [
    `**${categoryLabel([...allCats].sort((a, b) => CAT_ORDER[a] - CAT_ORDER[b]))}** (${day})`,
    '', '| Exercise | Sets | Reps | Weight |', '|---|---|---|---|',
  ];
  for (const ex of order) {
    const list = byEx.get(ex)!;
    const reps = list.map(s => s.reps), weights = list.map(s => s.weight_lbs);
    const repsStr = new Set(reps).size === 1 ? String(reps[0]) : reps.join(', ');
    const weightStr = new Set(weights).size === 1 ? fmtWeight(weights[0]) : weights.map(fmtWeight).join(', ');
    lines.push(`| ${ex} | ${list.length} | ${repsStr} | ${weightStr} |`);
  }
  // Per-set ids so a specific set can be targeted with update_set / remove_set (e.g. "the 2nd set").
  lines.push('', 'Individual sets (use the id to edit or remove one):');
  for (const ex of order) {
    byEx.get(ex)!.forEach((s, i) => {
      lines.push(`- \`${s.id}\` ${ex} — set ${i + 1}: ${fmtWeight(s.weight_lbs)} × ${s.reps}`);
    });
  }
  return lines.join('\n');
}
