#!/usr/bin/env node

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import * as lists from './lists.js';
import * as reminders from './reminders.js';
import * as notes from './notes.js';
import * as health from './health.js';
import * as workouts from './workouts.js';
import * as recipes from './recipes.js';
import * as nutrition from './nutrition.js';
import * as reviews from './reviews.js';

const server = new Server({ name: 'data', version: '2.0.0' }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    { name: 'list_items', description: "View all active (non-deleted) items on a list. Available lists: todo, grocery, or any custom list. Use this to check what's on a list before adding/removing.", inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: "Name of the list (e.g. 'todo', 'grocery'). Omit to see all lists." } } } },
    { name: 'add_item', description: "Add an item to a list (todo, grocery, or any custom list). Creates the list if it doesn't exist. Use for adding to-do items, grocery items, or any list-based data.", inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: "Name of the list (e.g. 'todo', 'grocery')." }, text: { type: 'string', description: 'The item text to add.' } }, required: ['list_name', 'text'] } },
    { name: 'check_item', description: 'Mark a list item as done/checked. Use the short ID prefix shown in list_items output.', inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: 'Name of the list.' }, id: { type: 'string', description: 'ID (or prefix) of the item to check.' } }, required: ['list_name', 'id'] } },
    { name: 'uncheck_item', description: 'Mark a list item as not done/unchecked.', inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: 'Name of the list.' }, id: { type: 'string', description: 'ID (or prefix) of the item to uncheck.' } }, required: ['list_name', 'id'] } },
    { name: 'remove_item', description: 'Remove (soft-delete) an item from a list.', inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: 'Name of the list.' }, id: { type: 'string', description: 'ID (or prefix) of the item to remove.' } }, required: ['list_name', 'id'] } },
    { name: 'create_list', description: 'Create a new empty list.', inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: 'Name for the new list (lowercase, no spaces — use hyphens).' } }, required: ['list_name'] } },
    { name: 'list_reminders', description: 'View all active reminders with their due dates and IDs.', inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'add_reminder', description: 'Add a new reminder with an optional due date/time.', inputSchema: { type: 'object' as const, properties: { text: { type: 'string', description: 'The reminder text.' }, due: { type: 'string', description: "Optional due date in ISO format (e.g. '2026-03-01T09:00:00')." } }, required: ['text'] } },
    { name: 'complete_reminder', description: 'Mark a reminder as done/completed.', inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'ID (or prefix) of the reminder to complete.' } }, required: ['id'] } },
    { name: 'remove_reminder', description: 'Remove (soft-delete) a reminder.', inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'ID (or prefix) of the reminder to remove.' } }, required: ['id'] } },
    { name: 'get_note', description: 'Read a note by name. Omit name to list available notes.', inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: "Note name (e.g. 'general'). Omit to list available notes." } } } },
    { name: 'set_note', description: "Write or update a note. Creates the note if it doesn't exist.", inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: "Note name (e.g. 'general')." }, content: { type: 'string', description: 'The content to write to the note.' } }, required: ['name', 'content'] } },
    { name: 'review_notes', description: "Pull all notes for review. Returns every note with built-in instructions for how to present them to the user. Use when the user says 'review my notes' or during scheduled reviews.", inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'review_list', description: "Pull a list for review with built-in handling instructions. Use when the user says 'review my grocery list', 'review my todo list', etc. Omit list_name to review ALL lists.", inputSchema: { type: 'object' as const, properties: { list_name: { type: 'string', description: "Name of the list to review (e.g. 'todo', 'grocery'). Omit to review all lists." } } } },
    { name: 'review_reminders', description: "Pull all reminders for review with built-in handling instructions. Use when the user says 'review my reminders' or during scheduled reviews.", inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'get_health_profile', description: 'Returns height, recent weight log entries, and current nutrition targets (calories, protein, carbs, fat).', inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'set_height', description: 'Set or update height in inches.', inputSchema: { type: 'object' as const, properties: { height_in: { type: 'number', description: 'Height in inches (e.g. 72 for 6\'0").' } }, required: ['height_in'] } },
    { name: 'log_weight', description: 'Log a body weight entry. Appends to the weight log.', inputSchema: { type: 'object' as const, properties: { weight_lbs: { type: 'number', description: 'Body weight in pounds.' }, date: { type: 'string', description: 'Date in YYYY-MM-DD format. Defaults to today.' } }, required: ['weight_lbs'] } },
    { name: 'get_nutrition_targets', description: 'Get the current daily nutrition targets (calories, protein, carbs, fat) and recent target history.', inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'set_nutrition_targets', description: 'Set daily nutrition targets (calories, protein, carbs, fat). Appends a new entry to the target log so history is preserved. The most recent entry becomes the current target, shown in get_health_profile.', inputSchema: { type: 'object' as const, properties: { calories: { type: 'number', description: 'Daily calorie target.' }, protein_g: { type: 'number', description: 'Daily protein target in grams.' }, carbs_g: { type: 'number', description: 'Daily carbs target in grams.' }, fat_g: { type: 'number', description: 'Daily fat target in grams.' }, date: { type: 'string', description: 'Date these targets take effect (YYYY-MM-DD). Defaults to today.' } }, required: ['calories', 'protein_g', 'carbs_g', 'fat_g'] } },
    { name: 'get_workout_catalog', description: 'Returns the valid categories (push/pull/legs/core/cardio), the valid muscle slugs, and the canonical exercise library — each exercise with its categories and fractional muscle map. You MUST call this before log_set and reuse the EXACT existing name (and casing) when the movement already exists, so its muscle map applies. Only treat an exercise as new if nothing here matches.', inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'log_set', description: "Log one or more sets to today's workout. There is no session — each set is its own timestamped row. Pass a 'sets' array. Call get_workout_catalog first and reuse the EXACT existing name for any movement already in the catalog — categories/muscles are then inherited and you should omit them. Only for a GENUINELY NEW movement supply both `categories` and a `muscles` map; it is added to the catalog. weight_lbs is TOTAL weight (a '30 lb dumbbell' pair = 60); bodyweight = 0. Log set-by-set as you train (accurate rest timing) or several at once. Returns today's workout table — pass it to the user exactly as-is.", inputSchema: { type: 'object' as const, properties: { sets: { type: 'array', description: 'Sets to log: [{exercise, weight_lbs, reps, and for new movements only: categories + muscles}].', items: { type: 'object', properties: { exercise: { type: 'string', description: 'Exact catalog name when known.' }, weight_lbs: { type: 'number', description: 'Total weight in lbs; bodyweight = 0.' }, reps: { type: 'integer' }, categories: { type: 'array', items: { type: 'string', enum: ['push', 'pull', 'legs', 'core', 'cardio'] }, description: 'New movement only; omit for known exercises.' }, muscles: { type: 'object', additionalProperties: { type: 'number' }, description: 'New movement only: fractional map, e.g. {"quads":1.0,"glutes":0.5} (1.0 primary, 0.5 secondary). Omit for known exercises.' } }, required: ['exercise', 'weight_lbs', 'reps'] } } }, required: ['sets'] } },
    { name: 'upsert_catalog_exercise', description: 'Create a new exercise in the catalog, or edit an existing one (fix its categories or muscle map). Provide name, categories, and a fractional muscles map ({slug: contribution}, 0 < c <= 1). Editing REPLACES the muscle map. Because volume joins to the catalog at read time, edits retroactively correct all history.', inputSchema: { type: 'object' as const, properties: { name: { type: 'string' }, categories: { type: 'array', items: { type: 'string', enum: ['push', 'pull', 'legs', 'core', 'cardio'] } }, muscles: { type: 'object', additionalProperties: { type: 'number' }, description: 'Fractional map, e.g. {"quads":1.0,"glutes":0.5}.' } }, required: ['name', 'categories', 'muscles'] } },
    { name: 'list_workouts', description: 'List recent workout days (grouped from sets), newest first — date, derived category label, set count, and exercises.', inputSchema: { type: 'object' as const, properties: { limit: { type: 'integer', description: 'Max days to return. Defaults to 10.' } } } },
    { name: 'get_workout', description: "Get a day's full workout — all sets grouped by exercise. Defaults to today. Returns a pre-formatted table — pass it to the user exactly as-is.", inputSchema: { type: 'object' as const, properties: { date: { type: 'string', description: 'Date YYYY-MM-DD. Defaults to today.' } } } },
    { name: 'get_exercise_history', description: 'Get progression history for a single exercise across all days (most recent first).', inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: 'Exercise name (case-insensitive).' }, limit: { type: 'integer', description: 'Max days to return. Defaults to 60.' } }, required: ['name'] } },
    { name: 'get_weekly_volume', description: 'Weekly hypertrophy volume per muscle (trailing N days, default 7), as fractional sets (primary mover = 1.0, secondary = 0.5) summed across all logged sets. Shows all muscle regions grouped by legs/push/pull/core against the ~10–20 sets/week target, so gaps (e.g. untrained hamstrings) are visible. Returns a pre-formatted report — pass it to the user exactly as-is.', inputSchema: { type: 'object' as const, properties: { days: { type: 'integer', description: 'Trailing window in days. Defaults to 7.' } } } },
    { name: 'update_set', description: "Edit a logged set's weight or reps by its set id (shown in get_workout). Returns the day's table.", inputSchema: { type: 'object' as const, properties: { id: { type: 'integer', description: 'Set id from get_workout.' }, weight_lbs: { type: 'number' }, reps: { type: 'integer' } }, required: ['id', 'weight_lbs', 'reps'] } },
    { name: 'remove_set', description: 'Delete a logged set by its set id (shown in get_workout).', inputSchema: { type: 'object' as const, properties: { id: { type: 'integer', description: 'Set id from get_workout.' } }, required: ['id'] } },
    { name: 'list_recipes', description: 'List all saved recipes. Optionally filter by tag. Returns recipe names and tags.', inputSchema: { type: 'object' as const, properties: { tag: { type: 'string', description: "Optional tag to filter by (e.g. 'breakfast', 'high-protein')." } } } },
    { name: 'get_recipe', description: 'Get the full details of a recipe by name (case-insensitive match).', inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: 'Recipe name.' } }, required: ['name'] } },
    { name: 'save_recipe', description: "Create or update a recipe. If a recipe with the same name exists, it is overwritten. ingredients is a list of objects with fields: item (required), quantity (number, optional), unit (optional), notes (optional). steps is a list of instruction strings. tags is an optional list of strings (e.g. ['breakfast', 'high-protein']). nutrition must include protein_g, carbs_g, and fat_g; calories are optional and will be derived if omitted. Each recipe represents one serving. Use accurate nutrition values; do not omit macros unless the food genuinely has none.", inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: 'Recipe name.' }, ingredients: { type: 'array', description: 'List of ingredients.', items: { type: 'object', properties: { item: { type: 'string' }, quantity: { type: 'number' }, unit: { type: 'string' }, notes: { type: 'string' } }, required: ['item'] } }, steps: { type: 'array', items: { type: 'string' }, description: 'Step-by-step instructions.' }, notes: { type: 'string', description: 'Optional recipe-level notes.' }, tags: { type: 'array', items: { type: 'string' }, description: "Optional tags for filtering (e.g. ['breakfast', 'high-protein'])." }, nutrition: { type: 'object', description: 'Required nutrition info per serving. protein_g, carbs_g, and fat_g are required; calories are optional.', properties: { calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } }, required: ['protein_g', 'carbs_g', 'fat_g'] } }, required: ['name', 'ingredients', 'steps', 'nutrition'] } },
    { name: 'delete_recipe', description: 'Permanently delete a recipe by name (case-insensitive).', inputSchema: { type: 'object' as const, properties: { name: { type: 'string', description: 'Recipe name.' } }, required: ['name'] } },
    { name: 'log_nutrition', description: "Log food eaten. For manual entries, provide a PLAIN name — the food's identity only, with NO quantity of any kind in it (no amounts, portions, counts, units, ingredient lists, or '(N×)' multiplier; e.g. 'Red wine', not 'Red wine (100 g)') — and final protein_g, carbs_g, and fat_g totals; calories are optional and will be derived if omitted. Do not use a quantity multiplier for manual entries. For composite meals (e.g. an omelet, a stir-fry, overnight oats), also include `ingredients`: an array with one entry per component. For each ingredient provide STRUCTURED fields — `food` (clean food name with NO amount or unit baked in, e.g. 'rolled oats', 'banana', 'protein powder'), `amount` (the numeric quantity these macros are for, e.g. 1, 0.5, 0.75), and `unit` (the measurement unit like 'cup', 'tbsp', 'g', 'scoop', 'slice', 'oz'; use an empty string or omit it for bare counts like 1 banana) — plus the macros (calories optional, protein_g/carbs_g/fat_g) for that amount. Keeping food/amount/unit separate lets the amounts be adjusted later. You may omit `name`; the server synthesizes a display name from food/amount/unit. The top-level calories/protein_g/carbs_g/fat_g must still equal the sum of the ingredients. Omit `ingredients` for single-component foods. For recipe entries, provide a recipe name and optional servings. `occurred_at` is optional. Only include it if the user explicitly specifies a date or time; otherwise omit it. Always confirm what was logged by passing the result to the user as-is.", inputSchema: { type: 'object' as const, properties: { items: { type: 'array', description: 'List of food items eaten.', items: { oneOf: [ { type: 'object', properties: { name: { type: 'string', description: 'Plain food/dish name ONLY — the identity of the food, never any quantity. No amounts, portions, counts, units, ingredient lists, or "(N×)" multiplier anywhere in the name. Good: "Red wine", "Overnight oats", "Omelet", "Caffè latte", "Banana", "Protein powder". Bad: "Red wine (100 g)", "Banana (1/3)", "Protein powder (1 scoop)", "Three-egg omelet". All quantity lives in the macros and structured ingredients (which the UI scales); the name must stay valid no matter how the portion is later changed.' }, nutrition: { type: 'object', description: 'Final nutrition totals for this entry. protein_g, carbs_g, and fat_g are required; calories are optional.', properties: { calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } }, required: ['protein_g', 'carbs_g', 'fat_g'] }, ingredients: { type: 'array', description: 'Optional per-ingredient breakdown for composite meals (omelet, stir-fry, overnight oats). One entry per component, each with structured food/amount/unit so amounts can be adjusted later; the macros are for that amount and the top-level nutrition totals must equal the sum of these ingredients. Omit for single-component foods.', items: { type: 'object', properties: { food: { type: 'string', description: "Clean food name with no amount or unit, e.g. 'rolled oats', 'banana', 'protein powder'." }, amount: { type: 'number', description: 'Numeric quantity these macros are for, e.g. 1, 0.5, 0.75.' }, unit: { type: 'string', description: "Measurement unit (cup, tbsp, tsp, g, ml, scoop, slice, oz, ...). Empty string or omit for bare counts like 1 banana." }, name: { type: 'string', description: 'Optional display name; omit when food/amount/unit are given and the server will synthesize it.' }, calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } } } }, occurred_at: { type: 'string', description: 'Optional. Only include if the user explicitly specifies a date or time; otherwise omit.' } }, required: ['name', 'nutrition'] }, { type: 'object', properties: { recipe: { type: 'string', description: 'Exact recipe name if this references a saved recipe.' }, servings: { type: 'number', description: 'Optional recipe serving multiplier. Defaults to 1.' }, occurred_at: { type: 'string', description: 'Optional. Only include if the user explicitly specifies a date or time; otherwise omit.' } }, required: ['recipe'] } ] } } }, required: ['items'] } },
    { name: 'get_nutrition', description: 'Get the nutrition log for a given date, including a daily totals summary.', inputSchema: { type: 'object' as const, properties: { date: { type: 'string', description: 'Date in YYYY-MM-DD format. Defaults to today.' } } } },
    { name: 'delete_nutrition_entry', description: 'Remove a specific entry from the nutrition log by its ID.', inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'Entry ID or 8-char prefix.' } }, required: ['id'] } },
    { name: 'stage_meal', description: "Stage a meal the user MIGHT eat or will eat later, WITHOUT logging it as eaten. Same shape as log_nutrition (a name + final protein_g/carbs_g/fat_g totals, calories optional, plus optional structured `ingredients` with food/amount/unit for composite meals). Staged meals are NOT counted in any totals until committed, they always surface in the current day's list, and they persist until committed or removed (no auto-expiry). Confirm by passing the result back to the user.", inputSchema: { type: 'object' as const, properties: { items: { type: 'array', description: 'Meals to stage.', items: { type: 'object', properties: { name: { type: 'string', description: 'Plain food/dish name ONLY — the food identity, never any quantity (no amounts, portions, counts, units, ingredient lists, or "(N×)" multiplier). Good: "Red wine", "Overnight oats". Bad: "Red wine (100 g)", "Three-egg omelet". Quantity lives in the macros/ingredients, which the UI scales.' }, nutrition: { type: 'object', properties: { calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } }, required: ['protein_g', 'carbs_g', 'fat_g'] }, ingredients: { type: 'array', description: 'Optional per-ingredient breakdown (same structured food/amount/unit + macros as log_nutrition).', items: { type: 'object', properties: { food: { type: 'string' }, amount: { type: 'number' }, unit: { type: 'string' }, name: { type: 'string' }, calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } } } } }, required: ['name', 'nutrition'] } } }, required: ['items'] } },
    { name: 'list_staged', description: 'List all staged (not-yet-eaten) meals with their IDs and macros.', inputSchema: { type: 'object' as const, properties: {} } },
    { name: 'commit_staged', description: 'Commit a staged meal — log it as eaten right now so it counts toward today. Identify it by its ID or 8-char prefix.', inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'Staged meal ID or 8-char prefix.' } }, required: ['id'] } },
    { name: 'edit_staged', description: "Edit a staged meal's name, macros, and/or ingredients (replaces the supplied fields). Identify it by ID or 8-char prefix.", inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'Staged meal ID or 8-char prefix.' }, name: { type: 'string' }, nutrition: { type: 'object', properties: { calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } } }, ingredients: { type: 'array', items: { type: 'object', properties: { food: { type: 'string' }, amount: { type: 'number' }, unit: { type: 'string' }, name: { type: 'string' }, calories: { type: 'number' }, protein_g: { type: 'number' }, carbs_g: { type: 'number' }, fat_g: { type: 'number' } } } } }, required: ['id'] } },
    { name: 'remove_staged', description: 'Remove/delete a staged meal by ID or 8-char prefix. Does not affect committed entries.', inputSchema: { type: 'object' as const, properties: { id: { type: 'string', description: 'Staged meal ID or 8-char prefix.' } }, required: ['id'] } },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const a = args || {};

  function text(msg: string) {
    return { content: [{ type: 'text' as const, text: msg }] };
  }

  try {
    if (name === 'list_items') {
      const listName = (a.list_name as string || '').trim();
      if (!listName) {
        const available = await lists.availableLists();
        return text(available.length ? 'Available lists: ' + available.join(', ') : 'No lists found.');
      }
      return text(lists.formatListItems(await lists.listItems(listName), listName));
    }
    if (name === 'add_item') {
      const item = await lists.addItem((a.list_name as string).trim(), (a.text as string).trim());
      return text(`Added '${item.text}' to ${a.list_name}. [id: ${item.id.slice(0, 8)}]`);
    }
    if (name === 'check_item') {
      const item = await lists.checkItem((a.list_name as string).trim(), (a.id as string).trim());
      return text(item ? `Checked: ${item.text}` : `Item not found: ${a.id}`);
    }
    if (name === 'uncheck_item') {
      const item = await lists.uncheckItem((a.list_name as string).trim(), (a.id as string).trim());
      return text(item ? `Unchecked: ${item.text}` : `Item not found: ${a.id}`);
    }
    if (name === 'remove_item') {
      const item = await lists.removeItem((a.list_name as string).trim(), (a.id as string).trim());
      return text(item ? `Removed: ${item.text}` : `Item not found: ${a.id}`);
    }
    if (name === 'create_list') {
      const n = (a.list_name as string).trim().toLowerCase().replace(/ /g, '-');
      return text(await lists.createList(n) ? `Created empty list: ${n}` : `List '${n}' already exists.`);
    }
    if (name === 'list_reminders') {
      return text(reminders.formatReminders(await reminders.listReminders()));
    }
    if (name === 'add_reminder') {
      const r = await reminders.addReminder((a.text as string).trim(), a.due as string | undefined);
      const dueStr = r.due ? ` (due: ${r.due})` : '';
      return text(`Added reminder: '${r.text}'${dueStr} [id: ${r.id.slice(0, 8)}]`);
    }
    if (name === 'complete_reminder') {
      const r = await reminders.completeReminder((a.id as string).trim());
      return text(r ? `Completed reminder: ${r.text}` : `Reminder not found: ${a.id}`);
    }
    if (name === 'remove_reminder') {
      const r = await reminders.removeReminder((a.id as string).trim());
      return text(r ? `Removed reminder: ${r.text}` : `Reminder not found: ${a.id}`);
    }
    if (name === 'get_note') {
      const noteName = (a.name as string || '').trim();
      if (!noteName) {
        const available = await notes.availableNotes();
        return text(available.length ? 'Available notes: ' + available.join(', ') : 'No notes found.');
      }
      const note = await notes.getNote(noteName);
      if (!note) return text(`Note '${noteName}' not found.`);
      if (!note.content) return text(`Note '${noteName}' is empty. (last updated: ${note.updated})`);
      return text(`**${noteName}** (updated: ${note.updated}):\n${note.content}`);
    }
    if (name === 'set_note') {
      const noteName = (a.name as string).trim().toLowerCase().replace(/ /g, '-');
      await notes.setNote(noteName, a.content as string);
      return text(`Note '${noteName}' saved.`);
    }
    if (name === 'review_notes') return text(await reviews.buildNotesReview());
    if (name === 'review_list') return text(await reviews.buildListReview((a.list_name as string || '').trim() || undefined));
    if (name === 'review_reminders') return text(await reviews.buildRemindersReview());
    if (name === 'get_health_profile') {
      const profile = await health.getProfile();
      const weights = await health.recentWeights();
      const targets = await health.recentTargets();
      return text(health.formatProfile(profile, weights, targets));
    }
    if (name === 'set_height') {
      await health.setHeight(a.height_in as number);
      const h = a.height_in as number;
      return text(`Height set to ${h} in (${Math.floor(h / 12)}'${Math.floor(h % 12)}").`);
    }
    if (name === 'log_weight') {
      const entry = await health.logWeight(a.weight_lbs as number, a.date as string | undefined);
      return text(`Logged ${entry.weight_lbs} lbs on ${entry.date}.`);
    }
    if (name === 'get_nutrition_targets') {
      const targets = await health.recentTargets();
      if (!targets.length) return text('No nutrition targets set yet.');
      const t = targets[0];
      const lines = [`Current nutrition targets (as of ${t.date}): ${t.calories} cal, ${t.protein_g}g protein, ${t.carbs_g}g carbs, ${t.fat_g}g fat`];
      if (targets.length > 1) {
        lines.push(`\nTarget history (last ${targets.length}):`);
        for (const e of targets) lines.push(`  ${e.date}: ${e.calories} cal, ${e.protein_g}g P / ${e.carbs_g}g C / ${e.fat_g}g F`);
      }
      return text(lines.join('\n'));
    }
    if (name === 'set_nutrition_targets') {
      const entry = await health.setNutritionTargets({ calories: a.calories as number, protein_g: a.protein_g as number, carbs_g: a.carbs_g as number, fat_g: a.fat_g as number }, a.date as string | undefined);
      return text(`Nutrition targets set (${entry.date}): ${entry.calories} cal, ${entry.protein_g}g protein, ${entry.carbs_g}g carbs, ${entry.fat_g}g fat`);
    }
    if (name === 'get_workout_catalog') {
      const catalog = await workouts.getCatalog();
      const fmtMuscles = (m: Record<string, number>) => Object.entries(m).map(([k, v]) => `${k} ${v}`).join(', ');
      return text(
        `Valid categories: ${catalog.categories.join(', ')}\n` +
        `Valid muscles: ${catalog.muscles.join(', ')}\n` +
        `Exercises (${catalog.exercises.length}):\n` +
        (catalog.exercises.map(e => `  ${e.name} [${e.categories.join(', ')}] → ${fmtMuscles(e.muscles) || '(no map)'}`).join('\n') || '  none yet'));
    }
    if (name === 'get_weekly_volume') {
      const result = await workouts.getWeeklyVolume((a.days as number) || 7);
      return text(workouts.formatWeeklyVolume(result));
    }
    if (name === 'log_set') {
      await workouts.logSets(a.sets as any[]);
      return text(await workouts.formatDay());
    }
    if (name === 'upsert_catalog_exercise') {
      const e = await workouts.upsertCatalogExercise(a.name as string, a.categories, a.muscles);
      const mus = Object.entries(e.muscles).map(([k, v]) => `${k} ${v}`).join(', ');
      return text(`Catalog saved: ${e.name} [${e.categories.join(', ')}] → ${mus}`);
    }
    if (name === 'list_workouts') {
      const days = await workouts.listWorkoutDays((a.limit as number) || 10);
      if (!days.length) return text('No workouts logged.');
      return text(days.map(d => `${d.date}  ${workouts.categoryLabel(d.categories)}  ${d.setCount} sets  (${d.exercises.join(', ')})`).join('\n'));
    }
    if (name === 'get_workout') {
      return text(await workouts.formatDay((a.date as string || '').trim() || undefined));
    }
    if (name === 'get_exercise_history') {
      const hist = await workouts.getExerciseHistory((a.name as string).trim(), (a.limit as number) || 60);
      if (!hist.length) return text(`No history found for '${a.name}'.`);
      const lines = [`History for ${a.name} (${hist.length} days):`];
      for (const h of hist) lines.push(`  ${h.date}: ${h.sets.map(s => `${s.weight_lbs}x${s.reps}`).join(', ')}`);
      return text(lines.join('\n'));
    }
    if (name === 'update_set') {
      const r = await workouts.updateSet(a.id as number, a.weight_lbs as number, a.reps as number);
      if (!r) return text(`Set not found: ${a.id}`);
      return text(await workouts.formatDay(r.occurred_at.slice(0, 10)));
    }
    if (name === 'remove_set') {
      const r = await workouts.removeSet(a.id as number);
      if (!r) return text(`Set not found: ${a.id}`);
      return text(`Removed set ${a.id} (${r.exercise} ${r.weight_lbs}x${r.reps}).\n\n` + await workouts.formatDay(r.occurred_at.slice(0, 10)));
    }
    if (name === 'list_recipes') {
      const recipeList = await recipes.listRecipes((a.tag as string || '').trim() || undefined);
      if (!recipeList.length) return text(a.tag ? `No recipes found with tag '${a.tag}'.` : 'No recipes saved yet.');
      const lines = recipeList.map(r => `**${r.name}**` + (r.tags.length ? ` — ${r.tags.join(', ')}` : ''));
      return text(`${recipeList.length} recipe(s):\n\n${lines.join('\n')}`);
    }
    if (name === 'get_recipe') {
      const r = await recipes.getRecipe(a.name as string);
      if (!r) return text(`Recipe not found: ${a.name}`);
      return text(recipes.formatRecipe(r));
    }
    if (name === 'save_recipe') {
      const result = await recipes.saveRecipe(a as any);
      return text(`${result.action} recipe: ${result.recipe.name}\n\n` + recipes.formatRecipe(result.recipe));
    }
    if (name === 'delete_recipe') {
      const r = await recipes.deleteRecipe(a.name as string);
      if (!r) return text(`Recipe not found: ${a.name}`);
      return text(`Deleted recipe: ${r.name}`);
    }
    if (name === 'log_nutrition') {
      const entries = await nutrition.logNutrition(a.items as any[]);
      const day = entries[0]?.date || '';
      const allEntries = await nutrition.getNutrition(day);
      return text(nutrition.formatNutritionDay(day, allEntries));
    }
    if (name === 'get_nutrition') {
      const day = (a.date as string || '').trim() || undefined;
      const entries = await nutrition.getNutrition(day);
      return text(nutrition.formatNutritionDay(day || entries[0]?.date || '', entries));
    }
    if (name === 'delete_nutrition_entry') {
      const entry = await nutrition.deleteNutritionEntry((a.id as string).trim());
      if (!entry) return text(`Entry not found: ${a.id}`);
      const dayEntries = await nutrition.getNutrition(entry.date);
      return text(`Deleted: ${entry.name}\n\n` + nutrition.formatNutritionDay(entry.date, dayEntries));
    }
    if (name === 'stage_meal') {
      const staged = await nutrition.stageNutrition(a.items as any[]);
      return text(`Staged ${staged.length} meal${staged.length === 1 ? '' : 's'}.\n\n` + nutrition.formatStagedList(await nutrition.getStaged()));
    }
    if (name === 'list_staged') {
      return text(nutrition.formatStagedList(await nutrition.getStaged()));
    }
    if (name === 'commit_staged') {
      const entry = await nutrition.commitStaged((a.id as string).trim());
      if (!entry) return text(`Staged meal not found: ${a.id}`);
      const dayEntries = await nutrition.getNutrition(entry.date);
      return text(`Committed (logged now): ${entry.name}\n\n` + nutrition.formatNutritionDay(entry.date, dayEntries));
    }
    if (name === 'edit_staged') {
      const entry = await nutrition.editStaged((a.id as string).trim(), { name: a.name as string, nutrition: a.nutrition as any, ingredients: a.ingredients as any });
      if (!entry) return text(`Staged meal not found: ${a.id}`);
      return text(`Updated staged meal: ${entry.name}\n\n` + nutrition.formatStagedList(await nutrition.getStaged()));
    }
    if (name === 'remove_staged') {
      const entry = await nutrition.removeStaged((a.id as string).trim());
      if (!entry) return text(`Staged meal not found: ${a.id}`);
      return text(`Removed staged meal: ${entry.name}\n\n` + nutrition.formatStagedList(await nutrition.getStaged()));
    }

    return text(`Unknown tool: ${name}`);
  } catch (err: any) {
    return text(`Error: ${err.message}`);
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(err => { console.error('MCP server error:', err); process.exit(1); });
