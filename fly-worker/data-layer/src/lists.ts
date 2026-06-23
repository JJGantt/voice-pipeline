import { getClient } from './client.js';
import { now, uuid } from './helpers.js';

export interface ListItem {
  id: string; list_name: string; text: string;
  done: boolean; deleted: boolean; created: string; updated: string;
}

export async function availableLists(): Promise<string[]> {
  const { data } = await getClient().from('lists').select('name').order('name');
  return (data ?? []).map(r => r.name);
}

export async function listItems(listName: string): Promise<ListItem[]> {
  const { data } = await getClient().from('list_items').select('*')
    .eq('list_name', listName).eq('deleted', false).order('created');
  return (data ?? []) as ListItem[];
}

export async function addItem(listName: string, text: string): Promise<ListItem> {
  const sb = getClient();
  await sb.from('lists').upsert({ name: listName }, { onConflict: 'name', ignoreDuplicates: true });
  const n = now(); const id = uuid();
  const { data } = await sb.from('list_items')
    .insert({ id, list_name: listName, text, done: false, deleted: false, created: n, updated: n })
    .select().single();
  return data as ListItem;
}

export async function checkItem(listName: string, idPrefix: string): Promise<ListItem | null> {
  const id = await resolveListItemId(listName, idPrefix);
  if (!id) return null;
  const n = now();
  await getClient().from('list_items').update({ done: true, updated: n }).eq('id', id);
  const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
  return data as ListItem;
}

export async function uncheckItem(listName: string, idPrefix: string): Promise<ListItem | null> {
  const id = await resolveListItemId(listName, idPrefix);
  if (!id) return null;
  const n = now();
  await getClient().from('list_items').update({ done: false, updated: n }).eq('id', id);
  const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
  return data as ListItem;
}

export async function removeItem(listName: string, idPrefix: string): Promise<ListItem | null> {
  const id = await resolveListItemId(listName, idPrefix);
  if (!id) return null;
  const n = now();
  await getClient().from('list_items').update({ deleted: true, updated: n }).eq('id', id);
  const { data } = await getClient().from('list_items').select('*').eq('id', id).single();
  return data as ListItem;
}

export async function createList(listName: string): Promise<boolean> {
  const { data } = await getClient().from('lists').select('name').eq('name', listName).maybeSingle();
  if (data) return false;
  await getClient().from('lists').insert({ name: listName });
  return true;
}

async function resolveListItemId(listName: string, prefix: string): Promise<string | null> {
  const { data: exact } = await getClient().from('list_items').select('id')
    .eq('id', prefix).eq('list_name', listName).eq('deleted', false).maybeSingle();
  if (exact) return exact.id;
  const { data: matches } = await getClient().from('list_items').select('id')
    .eq('list_name', listName).eq('deleted', false).like('id', `${prefix}%`);
  if (matches?.length === 1) return matches[0].id;
  return null;
}

export function formatListItems(items: ListItem[], listName: string): string {
  if (!items.length) return `'${listName}' is empty.`;
  const lines = items.map(i => `${i.done ? '[x]' : '[ ]'} ${i.text}  [id: ${i.id.slice(0, 8)}]`);
  return `**${listName}** (${items.length} items):\n${lines.join('\n')}`;
}
