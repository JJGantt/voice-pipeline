import { getClient } from './client.js';
import { now, uuid } from './helpers.js';
export async function listReminders() {
    const { data } = await getClient().from('reminders').select('*').eq('deleted', false).order('created');
    return (data ?? []);
}
export async function addReminder(text, due) {
    const n = now();
    const id = uuid();
    const { data } = await getClient().from('reminders')
        .insert({ id, text, due: due || null, done: false, deleted: false, created: n, updated: n })
        .select().single();
    return data;
}
export async function completeReminder(idPrefix) {
    const id = await resolveReminderId(idPrefix);
    if (!id)
        return null;
    const n = now();
    await getClient().from('reminders').update({ done: true, updated: n }).eq('id', id);
    const { data } = await getClient().from('reminders').select('*').eq('id', id).single();
    return data;
}
export async function removeReminder(idPrefix) {
    const id = await resolveReminderId(idPrefix);
    if (!id)
        return null;
    const n = now();
    await getClient().from('reminders').update({ deleted: true, updated: n }).eq('id', id);
    const { data } = await getClient().from('reminders').select('*').eq('id', id).single();
    return data;
}
async function resolveReminderId(prefix) {
    const { data: exact } = await getClient().from('reminders').select('id')
        .eq('id', prefix).eq('deleted', false).maybeSingle();
    if (exact)
        return exact.id;
    const { data: matches } = await getClient().from('reminders').select('id')
        .eq('deleted', false).like('id', `${prefix}%`);
    if (matches?.length === 1)
        return matches[0].id;
    return null;
}
export function formatReminders(items) {
    const active = items.filter(i => !i.deleted);
    if (!active.length)
        return 'No active reminders.';
    const lines = active.map(i => {
        const check = i.done ? '[x]' : '[ ]';
        const due = i.due ? ` (due: ${i.due})` : '';
        return `${check} ${i.text}${due}  [id: ${i.id.slice(0, 8)}]`;
    });
    return `**Reminders** (${active.length} items):\n${lines.join('\n')}`;
}
