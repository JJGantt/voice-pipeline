#!/usr/bin/env node
/**
 * HTTP API server for data operations.
 * Runs alongside the MCP server — this is what the Mac calls.
 * Default port: 27186 (configurable via DATA_HTTP_PORT env var).
 */

import http from 'node:http';
import * as lists from './lists.js';
import * as reminders from './reminders.js';
import * as notes from './notes.js';
import * as health from './health.js';
import * as workouts from './workouts.js';
import * as recipes from './recipes.js';
import * as nutrition from './nutrition.js';
import * as reviews from './reviews.js';
import { ValidationError } from './helpers.js';

const PORT = Number(process.env.DATA_HTTP_PORT || 27186);

function parseBody(req: http.IncomingMessage): Promise<any> {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', (chunk: Buffer) => { body += chunk.toString(); });
    req.on('end', () => {
      try { resolve(JSON.parse(body)); }
      catch { resolve({}); }
    });
  });
}

function json(res: http.ServerResponse, data: any, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

function notFound(res: http.ServerResponse) {
  json(res, { error: 'not found' }, 404);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://localhost:${PORT}`);
  const method = req.method || 'GET';
  const path = url.pathname;

  try {
    // ── Lists ──
    if (path === '/data/lists' && method === 'GET') {
      return json(res, { lists: await lists.availableLists() });
    }
    if (path === '/data/lists' && method === 'POST') {
      const body = await parseBody(req);
      const created = await lists.createList(body.name?.trim().toLowerCase().replace(/ /g, '-'));
      return json(res, { ok: created });
    }
    const listItemsMatch = path.match(/^\/data\/lists\/([^/]+)\/items$/);
    if (listItemsMatch && method === 'GET') {
      const items = await lists.listItems(listItemsMatch[1]);
      return json(res, { items });
    }
    if (listItemsMatch && method === 'POST') {
      const body = await parseBody(req);
      const item = await lists.addItem(listItemsMatch[1], body.text);
      return json(res, { item });
    }
    const listItemActionMatch = path.match(/^\/data\/lists\/([^/]+)\/items\/([^/]+)\/(check|uncheck)$/);
    if (listItemActionMatch && method === 'PATCH') {
      const [, listName, id, action] = listItemActionMatch;
      const item = action === 'check'
        ? await lists.checkItem(listName, id)
        : await lists.uncheckItem(listName, id);
      if (!item) return json(res, { error: 'not found' }, 404);
      return json(res, { item });
    }
    const listItemDeleteMatch = path.match(/^\/data\/lists\/([^/]+)\/items\/([^/]+)$/);
    if (listItemDeleteMatch && method === 'DELETE') {
      const item = await lists.removeItem(listItemDeleteMatch[1], listItemDeleteMatch[2]);
      if (!item) return json(res, { error: 'not found' }, 404);
      return json(res, { item });
    }

    // ── Reminders ──
    if (path === '/data/reminders' && method === 'GET') {
      return json(res, { reminders: await reminders.listReminders() });
    }
    if (path === '/data/reminders' && method === 'POST') {
      const body = await parseBody(req);
      const r = await reminders.addReminder(body.text, body.due);
      return json(res, { reminder: r });
    }
    const reminderCompleteMatch = path.match(/^\/data\/reminders\/([^/]+)\/complete$/);
    if (reminderCompleteMatch && method === 'PATCH') {
      const r = await reminders.completeReminder(reminderCompleteMatch[1]);
      if (!r) return json(res, { error: 'not found' }, 404);
      return json(res, { reminder: r });
    }
    const reminderDeleteMatch = path.match(/^\/data\/reminders\/([^/]+)$/);
    if (reminderDeleteMatch && method === 'DELETE') {
      const r = await reminders.removeReminder(reminderDeleteMatch[1]);
      if (!r) return json(res, { error: 'not found' }, 404);
      return json(res, { reminder: r });
    }

    // ── Notes ──
    if (path === '/data/notes' && method === 'GET') {
      return json(res, { notes: await notes.availableNotes() });
    }
    const noteMatch = path.match(/^\/data\/notes\/([^/]+)$/);
    if (noteMatch && method === 'GET') {
      const note = await notes.getNote(noteMatch[1]);
      if (!note) return json(res, { error: 'not found' }, 404);
      return json(res, { note });
    }
    if (noteMatch && method === 'PUT') {
      const body = await parseBody(req);
      await notes.setNote(noteMatch[1], body.content);
      return json(res, { ok: true });
    }

    // ── Health ──
    if (path === '/data/health/profile' && method === 'GET') {
      const profile = await health.getProfile();
      const weights = await health.recentWeights();
      const targets = await health.recentTargets();
      return json(res, { profile, weights, targets });
    }
    if (path === '/data/health/height' && method === 'PUT') {
      const body = await parseBody(req);
      await health.setHeight(body.height_in);
      return json(res, { ok: true });
    }
    if (path === '/data/health/weight' && method === 'POST') {
      const body = await parseBody(req);
      const entry = await health.logWeight(body.weight_lbs, body.date);
      return json(res, { entry });
    }
    if (path === '/data/health/targets' && method === 'POST') {
      const body = await parseBody(req);
      const entry = await health.setNutritionTargets({
        calories: body.calories,
        protein_g: body.protein_g,
        carbs_g: body.carbs_g,
        fat_g: body.fat_g,
      }, body.date);
      return json(res, { entry });
    }
    if (path === '/data/health/targets' && method === 'GET') {
      const targets = await health.recentTargets();
      return json(res, { targets });
    }

    // ── Workouts (flat timestamped sets) ──
    if (path === '/data/workouts/catalog' && method === 'GET') {
      return json(res, await workouts.getCatalog());
    }
    if (path === '/data/workouts/catalog' && method === 'POST') {
      const body = await parseBody(req);
      return json(res, { exercise: await workouts.upsertCatalogExercise(body.name, body.categories, body.muscles) });
    }
    if (path === '/data/workouts' && method === 'GET') {
      const limit = Number(url.searchParams.get('limit')) || 10;
      return json(res, { days: await workouts.listWorkoutDays(limit) });
    }
    if (path === '/data/workouts/day' && method === 'GET') {
      const date = url.searchParams.get('date') || undefined;
      return json(res, await workouts.getDaySets(date));
    }
    if (path === '/data/workouts/volume' && method === 'GET') {
      const days = Number(url.searchParams.get('days')) || 7;
      return json(res, await workouts.getWeeklyVolume(days));
    }
    if (path === '/data/sets' && method === 'POST') {
      const body = await parseBody(req);
      return json(res, { sets: await workouts.logSets(body.sets) });
    }
    const setMatch = path.match(/^\/data\/sets\/(\d+)$/);
    if (setMatch && method === 'PATCH') {
      const body = await parseBody(req);
      const row = await workouts.updateSet(Number(setMatch[1]), body.weight_lbs, body.reps);
      if (!row) return json(res, { error: 'not found' }, 404);
      return json(res, { set: row });
    }
    if (setMatch && method === 'DELETE') {
      const row = await workouts.removeSet(Number(setMatch[1]));
      if (!row) return json(res, { error: 'not found' }, 404);
      return json(res, { deleted: row });
    }
    const exerciseHistoryMatch = path.match(/^\/data\/exercises\/([^/]+)\/history$/);
    if (exerciseHistoryMatch && method === 'GET') {
      const limit = Number(url.searchParams.get('limit')) || 20;
      const history = await workouts.getExerciseHistory(decodeURIComponent(exerciseHistoryMatch[1]), limit);
      return json(res, { history });
    }

    // ── Recipes ──
    if (path === '/data/recipes' && method === 'GET') {
      const tag = url.searchParams.get('tag') || undefined;
      return json(res, { recipes: await recipes.listRecipes(tag) });
    }
    const recipeMatch = path.match(/^\/data\/recipes\/([^/]+)$/);
    if (recipeMatch && method === 'GET') {
      const r = await recipes.getRecipe(decodeURIComponent(recipeMatch[1]));
      if (!r) return json(res, { error: 'not found' }, 404);
      return json(res, { recipe: r });
    }
    if (recipeMatch && method === 'PUT') {
      const body = await parseBody(req);
      body.name = decodeURIComponent(recipeMatch[1]);
      const result = await recipes.saveRecipe(body);
      return json(res, { recipe: result.recipe, action: result.action });
    }
    if (recipeMatch && method === 'DELETE') {
      const r = await recipes.deleteRecipe(decodeURIComponent(recipeMatch[1]));
      if (!r) return json(res, { error: 'not found' }, 404);
      return json(res, { deleted: r });
    }

    // ── Nutrition ──
    if (path === '/data/nutrition' && method === 'GET') {
      const date = url.searchParams.get('date') || undefined;
      const entries = await nutrition.getNutrition(date);
      return json(res, { entries });
    }
    if (path === '/data/nutrition' && method === 'POST') {
      const body = await parseBody(req);
      const items = (body.items || []).map((item: any) => ({
        ...item,
        occurred_at: item.occurred_at || body.date || undefined,
      }));
      const entries = await nutrition.logNutrition(items);
      return json(res, { entries });
    }
    const nutritionDeleteMatch = path.match(/^\/data\/nutrition\/([^/]+)$/);
    if (nutritionDeleteMatch && method === 'DELETE') {
      const entry = await nutrition.deleteNutritionEntry(nutritionDeleteMatch[1]);
      if (!entry) return json(res, { error: 'not found' }, 404);
      return json(res, { deleted: entry });
    }

    // ── Reviews ──
    if (path === '/data/reviews/notes' && method === 'GET') {
      return json(res, { text: await reviews.buildNotesReview() });
    }
    const reviewListMatch = path.match(/^\/data\/reviews\/lists(?:\/([^/]+))?$/);
    if (reviewListMatch && method === 'GET') {
      return json(res, { text: await reviews.buildListReview(reviewListMatch[1]) });
    }
    if (path === '/data/reviews/reminders' && method === 'GET') {
      return json(res, { text: await reviews.buildRemindersReview() });
    }

    // Status
    if (path === '/data/status' && method === 'GET') {
      return json(res, { ok: true, version: '2.0.0' });
    }

    notFound(res);
  } catch (err: any) {
    console.error('HTTP error:', err.message);
    json(res, { error: err.message }, err instanceof ValidationError ? 400 : 500);
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[data-http] listening on :${PORT}`);
});
