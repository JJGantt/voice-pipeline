import { getClient } from './client.js';
import { now } from './helpers.js';
export async function availableNotes() {
    const { data } = await getClient().from('notes').select('name').order('name');
    return (data ?? []).map(r => r.name);
}
export async function getNote(name) {
    const { data } = await getClient().from('notes').select('*').eq('name', name).maybeSingle();
    return data;
}
export async function setNote(name, content) {
    const n = now();
    await getClient().from('notes').upsert({ name, content, updated: n }, { onConflict: 'name' });
}
export async function allNotes() {
    const { data } = await getClient().from('notes').select('*').order('name');
    return (data ?? []);
}
