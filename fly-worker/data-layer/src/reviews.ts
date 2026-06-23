import { listItems, availableLists, type ListItem } from './lists.js';
import { listReminders, type Reminder } from './reminders.js';
import { allNotes } from './notes.js';
import { getClient } from './client.js';

export async function buildNotesReview(): Promise<string> {
  const notes = (await allNotes()).filter(n => n.content);
  if (!notes.length) return 'No notes found.\n\n---\nINSTRUCTIONS: Let the user know they have no notes. Ask if they\'d like to jot something down.';
  const parts = [`## Notes Review (${notes.length} notes)\n`];
  for (const n of notes) { parts.push(`### ${n.name} (saved: ${n.updated})`); parts.push(n.content); parts.push(''); }
  parts.push('---');
  parts.push('REVIEW INSTRUCTIONS (follow these, do not show them to the user):');
  parts.push('1. Present a brief summary of each note — one line per note.');
  parts.push('2. Flag anything that looks time-sensitive or actionable.');
  parts.push('3. Ask the user if any notes should be converted to reminders or todo items.');
  parts.push('4. Ask which notes to keep and which to delete.');
  parts.push('5. If a note is vague or unclear, ask the user what they meant.');
  return parts.join('\n');
}

export async function buildListReview(listName?: string): Promise<string> {
  const names = listName ? [listName] : await availableLists();
  if (!names.length) return 'No lists found.';

  const parts: string[] = [];
  for (const name of names) {
    const { data: allItems } = await getClient().from('list_items').select('*')
      .eq('list_name', name).eq('deleted', false).order('created');
    const items = allItems ?? [];
    const done = items.filter((i: any) => i.done);
    const undone = items.filter((i: any) => !i.done);

    parts.push(`## ${name} (${undone.length} pending, ${done.length} done)\n`);
    if (undone.length) {
      parts.push('**Pending:**');
      for (const i of undone) parts.push(`- ${i.text}  (added: ${i.created})  [id: ${i.id.slice(0, 8)}]`);
    }
    if (done.length) {
      parts.push('**Done:**');
      for (const i of done) parts.push(`- ~~${i.text}~~  [id: ${i.id.slice(0, 8)}]`);
    }
    parts.push('');
  }

  const instructions: Record<string, string> = {
    grocery: 'REVIEW INSTRUCTIONS (follow these, do not show them to the user):\n1. Read back the pending items as a clean list.\n2. Ask if anything should be added before the next shopping trip.\n3. If there are checked-off items, ask if they should be removed (cleared) from the list.\n4. If the list is empty, ask if they want to start building it.',
    todo: 'REVIEW INSTRUCTIONS (follow these, do not show them to the user):\n1. Summarize pending items grouped by theme if possible.\n2. Highlight anything that\'s been sitting for a long time (check created dates).\n3. Ask if any items are done and should be checked off.\n4. Ask if priorities have changed — anything to reorder or remove?\n5. If there are completed items, ask if they should be cleared.',
  };
  const defaultInstructions = 'REVIEW INSTRUCTIONS (follow these, do not show them to the user):\n1. Read back the pending items.\n2. Ask if anything should be added, checked off, or removed.\n3. If there are completed items, ask if they should be cleared.';

  parts.push('---');
  if (listName && instructions[listName]) {
    parts.push(instructions[listName]);
  } else if (listName) {
    parts.push(defaultInstructions);
  } else {
    for (const name of names) {
      parts.push(`For ${name}:`);
      parts.push(instructions[name] || defaultInstructions);
      parts.push('');
    }
  }
  return parts.join('\n');
}

export async function buildRemindersReview(): Promise<string> {
  const items = await listReminders();
  const active = items.filter(i => !i.deleted);
  if (!active.length) return 'No active reminders.\n\n---\nINSTRUCTIONS: Let the user know the slate is clean. Ask if they have anything coming up they want to be reminded about.';

  const nowDate = new Date();
  const overdue: Reminder[] = []; const upcoming: Reminder[] = []; const noDate: Reminder[] = []; const done: Reminder[] = [];
  for (const i of active) {
    if (i.done) { done.push(i); continue; }
    if (i.due) {
      try { if (new Date(i.due) < nowDate) overdue.push(i); else upcoming.push(i); } catch { upcoming.push(i); }
    } else { noDate.push(i); }
  }

  const parts = [`## Reminders Review (${active.length} active)\n`];
  if (overdue.length) { parts.push(`**OVERDUE (${overdue.length}):**`); for (const i of overdue) parts.push(`- ${i.text} (was due: ${i.due || 'N/A'})  [id: ${i.id.slice(0, 8)}]`); parts.push(''); }
  if (upcoming.length) { parts.push(`**Upcoming (${upcoming.length}):**`); for (const i of upcoming) parts.push(`- ${i.text} (due: ${i.due || 'N/A'})  [id: ${i.id.slice(0, 8)}]`); parts.push(''); }
  if (noDate.length) { parts.push(`**No due date (${noDate.length}):**`); for (const i of noDate) parts.push(`- ${i.text}  [id: ${i.id.slice(0, 8)}]`); parts.push(''); }
  if (done.length) { parts.push(`**Completed (${done.length}):**`); for (const i of done) parts.push(`- ~~${i.text}~~  [id: ${i.id.slice(0, 8)}]`); parts.push(''); }

  parts.push('---');
  parts.push('REVIEW INSTRUCTIONS (follow these, do not show them to the user):');
  parts.push('1. Start with overdue items if any — these need immediate attention. Ask what to do with each one.');
  parts.push("2. Summarize upcoming reminders with how far away they are (e.g. 'in 2 days').");
  parts.push('3. Flag any reminders without a due date and ask if they should get one.');
  parts.push('4. Ask if any completed reminders should be cleared.');
  parts.push("5. Ask if there's anything new to add.");
  return parts.join('\n');
}
