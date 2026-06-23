import { getClient } from './client.js';
import { normalizeMacroNutrition, now, type MacroNutrition, type MacroNutritionInput, uuid } from './helpers.js';

export interface Ingredient { item: string; quantity?: number; unit?: string; notes?: string; }
export interface Recipe {
  id: string; name: string; notes: string; tags: string[]; ingredients: Ingredient[];
  steps: string[]; nutrition: { calories?: number; protein_g?: number; carbs_g?: number; fat_g?: number } | null;
  created: string; updated: string;
}

async function loadRecipe(id: string): Promise<Recipe | null> {
  const sb = getClient();
  const { data: row } = await sb.from('recipes').select('*').eq('id', id).maybeSingle();
  if (!row) return null;
  const { data: tagRows } = await sb.from('recipe_tags').select('tag').eq('recipe_id', id).order('tag');
  const { data: ingRows } = await sb.from('recipe_ingredients').select('item, quantity, unit, notes')
    .eq('recipe_id', id).order('sort_order');
  const { data: stepRows } = await sb.from('recipe_steps').select('instruction')
    .eq('recipe_id', id).order('step_num');
  const tags = (tagRows ?? []).map(r => r.tag);
  const ingredients = (ingRows ?? []) as Ingredient[];
  const steps = (stepRows ?? []).map(r => r.instruction);
  const nutrition = (row.calories != null || row.protein_g != null || row.carbs_g != null || row.fat_g != null)
    ? { calories: row.calories, protein_g: row.protein_g, carbs_g: row.carbs_g, fat_g: row.fat_g } : null;
  return { id: row.id, name: row.name, notes: row.notes, tags, ingredients, steps, nutrition, created: row.created, updated: row.updated };
}

export async function listRecipes(tag?: string): Promise<Recipe[]> {
  const sb = getClient();
  let ids: string[];
  if (tag) {
    const { data } = await sb.from('recipe_tags').select('recipe_id').ilike('tag', tag);
    ids = (data ?? []).map(r => r.recipe_id);
  } else {
    const { data } = await sb.from('recipes').select('id').order('name');
    ids = (data ?? []).map(r => r.id);
  }
  const recipes: Recipe[] = [];
  for (const id of ids) {
    const r = await loadRecipe(id);
    if (r) recipes.push(r);
  }
  return recipes;
}

export async function getRecipe(name: string): Promise<Recipe | null> {
  const { data: row } = await getClient().from('recipes').select('id').ilike('name', name.trim()).maybeSingle();
  if (!row) return null;
  return loadRecipe(row.id);
}

export async function saveRecipe(args: {
  name: string; ingredients: Ingredient[]; steps: string[];
  notes?: string; tags?: string[]; nutrition: MacroNutritionInput;
}): Promise<{ recipe: Recipe; action: string }> {
  const sb = getClient();
  const recipeName = args.name.trim();
  const nutrition = normalizeMacroNutrition(args.nutrition, `Recipe '${recipeName}'`);
  const { data: existing } = await sb.from('recipes').select('id, created').ilike('name', recipeName).maybeSingle();
  const n = now();
  const id = existing?.id || uuid();

  if (existing) {
    await sb.from('recipes').update({ name: recipeName, notes: args.notes || '', calories: nutrition.calories, protein_g: nutrition.protein_g, carbs_g: nutrition.carbs_g, fat_g: nutrition.fat_g, updated: n }).eq('id', id);
  } else {
    await sb.from('recipes').insert({ id, name: recipeName, notes: args.notes || '', calories: nutrition.calories, protein_g: nutrition.protein_g, carbs_g: nutrition.carbs_g, fat_g: nutrition.fat_g, created: n, updated: n });
  }

  await sb.from('recipe_tags').delete().eq('recipe_id', id);
  for (const tag of args.tags || []) {
    await sb.from('recipe_tags').insert({ recipe_id: id, tag });
  }
  await sb.from('recipe_ingredients').delete().eq('recipe_id', id);
  for (let i = 0; i < args.ingredients.length; i++) {
    const ing = args.ingredients[i];
    await sb.from('recipe_ingredients').insert({ recipe_id: id, item: ing.item, quantity: ing.quantity ?? null, unit: ing.unit ?? null, notes: ing.notes ?? null, sort_order: i });
  }
  await sb.from('recipe_steps').delete().eq('recipe_id', id);
  for (let i = 0; i < args.steps.length; i++) {
    await sb.from('recipe_steps').insert({ recipe_id: id, step_num: i + 1, instruction: args.steps[i] });
  }

  return { recipe: (await loadRecipe(id))!, action: existing ? 'Updated' : 'Saved' };
}

export async function deleteRecipe(name: string): Promise<Recipe | null> {
  const recipe = await getRecipe(name);
  if (!recipe) return null;
  await getClient().from('recipes').delete().eq('id', recipe.id);
  return recipe;
}

export async function getRecipeNutrition(name: string): Promise<MacroNutrition | null> {
  const { data: row } = await getClient().from('recipes').select('calories, protein_g, carbs_g, fat_g').ilike('name', name.trim()).maybeSingle();
  if (!row) return null;
  return normalizeMacroNutrition(row, `Recipe '${name.trim()}'`);
}

export function formatRecipe(r: Recipe): string {
  const lines = [`## ${r.name}`];
  if (r.tags.length) lines.push(`*Tags: ${r.tags.join(', ')}*`);
  lines.push('');
  if (r.ingredients.length) {
    lines.push('**Ingredients:**');
    for (const ing of r.ingredients) {
      const qty = ing.quantity ? `${ing.quantity} ${ing.unit || ''} `.trim() + ' ' : '';
      const note = ing.notes ? ` (${ing.notes})` : '';
      lines.push(`- ${qty}${ing.item}${note}`);
    }
    lines.push('');
  }
  if (r.steps.length) {
    lines.push('**Instructions:**');
    for (let i = 0; i < r.steps.length; i++) lines.push(`${i + 1}. ${r.steps[i]}`);
    lines.push('');
  }
  if (r.notes) { lines.push(`**Notes:** ${r.notes}`); lines.push(''); }
  if (r.nutrition) {
    const parts: string[] = [];
    if (r.nutrition.calories != null) parts.push(`${r.nutrition.calories} cal`);
    if (r.nutrition.protein_g != null) parts.push(`${r.nutrition.protein_g}g protein`);
    if (r.nutrition.carbs_g != null) parts.push(`${r.nutrition.carbs_g}g carbs`);
    if (r.nutrition.fat_g != null) parts.push(`${r.nutrition.fat_g}g fat`);
    if (parts.length) lines.push(`**Nutrition:** ${parts.join(', ')}`);
  }
  return lines.join('\n');
}
