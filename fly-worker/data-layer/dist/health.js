import { getClient } from './client.js';
import { now, today, ensureEasternDate } from './helpers.js';
export async function getProfile() {
    const { data } = await getClient().from('health_profile').select('height_in, updated').eq('id', 1).maybeSingle();
    return data || { height_in: null, updated: null };
}
export async function setHeight(heightIn) {
    const n = now();
    await getClient().from('health_profile').upsert({ id: 1, height_in: heightIn, updated: n }, { onConflict: 'id' });
}
export async function logWeight(weightLbs, date) {
    const d = date ? ensureEasternDate(date) : today();
    const n = now();
    const { data } = await getClient().from('weight_log')
        .insert({ date: d, weight_lbs: weightLbs, logged_at: n })
        .select().single();
    return data;
}
export async function recentWeights(limit = 5) {
    const { data } = await getClient().from('weight_log')
        .select('*').order('date', { ascending: false }).order('id', { ascending: false }).limit(limit);
    return (data ?? []);
}
export async function setNutritionTargets(targets, date) {
    const d = date ? ensureEasternDate(date) : today();
    const n = now();
    const { data } = await getClient().from('nutrition_targets')
        .insert({ date: d, calories: targets.calories, protein_g: targets.protein_g, carbs_g: targets.carbs_g, fat_g: targets.fat_g, logged_at: n })
        .select().single();
    return data;
}
export async function recentTargets(limit = 3) {
    const { data } = await getClient().from('nutrition_targets')
        .select('*').order('date', { ascending: false }).order('id', { ascending: false }).limit(limit);
    return (data ?? []);
}
export function formatProfile(profile, weights, targets = []) {
    const lines = [];
    if (profile.height_in) {
        const feet = Math.floor(profile.height_in / 12);
        const inches = Math.floor(profile.height_in % 12);
        lines.push(`Height: ${profile.height_in} in (${feet}'${inches}")`);
    }
    else {
        lines.push('Height: not set');
    }
    if (weights.length) {
        lines.push(`Latest weight: ${weights[0].weight_lbs} lbs on ${weights[0].date}`);
        lines.push(`\nWeight log (last ${weights.length}):`);
        for (const e of weights)
            lines.push(`  ${e.date}: ${e.weight_lbs} lbs`);
    }
    else {
        lines.push('Weight log: empty');
    }
    if (targets.length) {
        const t = targets[0];
        lines.push(`\nNutrition targets (as of ${t.date}): ${t.calories} cal, ${t.protein_g}g protein, ${t.carbs_g}g carbs, ${t.fat_g}g fat`);
        if (targets.length > 1) {
            lines.push(`\nTarget history (last ${targets.length}):`);
            for (const e of targets)
                lines.push(`  ${e.date}: ${e.calories} cal, ${e.protein_g}g P / ${e.carbs_g}g C / ${e.fat_g}g F`);
        }
    }
    else {
        lines.push('\nNutrition targets: not set');
    }
    return lines.join('\n');
}
