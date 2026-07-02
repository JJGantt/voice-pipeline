import { getClient } from './client.js';
import { now, uuid } from './helpers.js';
export async function availableLists() {
    const { data } = await getClient().from('lists').select('name').order('name');
    return (data ?? []).map(r => r.name);
}
export async function listItems(listName) {
    const { data } = await getClient().from('list_items').select('*')
        .eq('list_name', listName).eq('deleted', false).order('created');
    return (data ?? []);
}
export async function addItem(listName, text) {
    const sb = getClient();
    await sb.from('lists').upsert({ name: listName }, { onConflict: 'name', ignoreDuplicates: true });
    const n = now();
    const id = uuid();
    const { data } = await sb.from('list_items')
        .insert({ id, list_name: listName, text, done: false, deleted: false, created: n, updated: n })
        .select().single();
    return data;
}
export async function checkItem(listName, idPrefix) {
    const id = await resolveListItemId(listName, idPrefix);
    if (!id)
        return null;
    const n = now();
    await getClient().from('list_items').update({ done: true, updated: n }).eq('id', id);
    const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
    return data;
}
export async function uncheckItem(listName, idPrefix) {
    const id = await resolveListItemId(listName, idPrefix);
    if (!id)
        return null;
    const n = now();
    await getClient().from('list_items').update({ done: false, updated: n }).eq('id', id);
    const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
    return data;
}
export async function removeItem(listName, idPrefix) {
    const id = await resolveListItemId(listName, idPrefix);
    if (!id)
        return null;
    const n = now();
    await getClient().from('list_items').update({ deleted: true, updated: n }).eq('id', id);
    const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
    return data;
}
export async function createList(listName) {
    const { data } = await getClient().from('lists').select('name').eq('name', listName).maybeSingle();
    if (data)
        return false;
    await getClient().from('lists').insert({ name: listName });
    return true;
}
async function resolveListItemId(listName, prefix) {
    const { data: exact } = await getClient().from('list_items').select('id')
        .eq('id', prefix).eq('list_name', listName).eq('deleted', false).maybeSingle();
    if (exact)
        return exact.id;
    const { data: matches } = await getClient().from('list_items').select('id')
        .eq('list_name', listName).eq('deleted', false).like('id', `${prefix}%`);
    if (matches?.length === 1)
        return matches[0].id;
    return null;
}
export function formatListItems(items, listName) {
    if (!items.length)
        return `'${listName}' is empty.`;
    const lines = items.map(i => `${i.done ? '[x]' : '[ ]'} ${i.text}  [id: ${i.id.slice(0, 8)}]`);
    return `**${listName}** (${items.length} items):\n${lines.join('\n')}`;
}
