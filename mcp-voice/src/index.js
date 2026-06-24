#!/usr/bin/env node

// "voice" MCP server (stdio): lets any Claude session list focusable Mac Claude
// sessions and switch what the Pi/TV wall is focused on (via Supabase voice_focus).

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { listRecentSessions } from './sessions.js';
import { setFocus } from './focus.js';
import { say } from './say.js';

const server = new Server({ name: 'voice', version: '1.0.0' }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'list_sessions',
      description:
        "List the 5 most recently active Claude Code sessions on this Mac that the Pi/TV wall can focus on. Returns [{id, title, cwd, project, last_active}], newest first. Use this to see what's available before set_focus.",
      inputSchema: { type: 'object', properties: {} },
    },
    {
      name: 'set_focus',
      description:
        "Switch what the Pi/TV wall is focused on. `target` is either the literal \"hub\" (the wall's home view) or a fuzzy reference to one of the recent sessions — match against its title or project, or say \"most recent\"/\"latest\". Resolves the reference and updates the shared voice_focus record. Returns a confirmation of what was focused.",
      inputSchema: {
        type: 'object',
        properties: {
          target: {
            type: 'string',
            description: 'Either "hub", or a fuzzy reference to a recent session (title/project keyword, or "most recent").',
          },
        },
        required: ['target'],
      },
    },
    {
      name: 'say',
      description:
        "Speak ONE short line aloud to Jared — this is the ONLY thing that gets played as audio. Call it once per voice turn with a single succinct sentence. For an ACTION, say what you're doing in the present tense right as you act (\"changing the floor lamp to white\", \"logging the bagel\") — call it alongside the action so it's spoken while the action runs. For a QUESTION, say the answer (\"you've eaten 750 calories\"). One natural spoken phrase — no preamble, no markdown, no lists. Your final written reply is shown on screen and is NOT spoken, so anything you want heard must go through this tool. If something fails, you may call it again (\"couldn't reach the lamp\").",
      inputSchema: {
        type: 'object',
        properties: {
          text: {
            type: 'string',
            description: 'The one short line to speak aloud (present tense for actions; the answer for questions).',
          },
        },
        required: ['text'],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const a = args || {};

  function text(msg) {
    return { content: [{ type: 'text', text: msg }] };
  }

  try {
    if (name === 'list_sessions') {
      const sessions = await listRecentSessions(5);
      if (!sessions.length) return text('No focusable Claude sessions found.');
      return text(JSON.stringify(sessions, null, 2));
    }
    if (name === 'set_focus') {
      const target = (a.target || '').toString().trim();
      if (!target) return text('Error: `target` is required (use "hub" or a session reference).');
      return text(await setFocus(target));
    }
    if (name === 'say') {
      const line = (a.text || '').toString().trim();
      if (!line) return text('Error: `text` is required (the one short line to speak).');
      await say(line);
      return text(`Spoke aloud: ${line}`);
    }
    return text(`Unknown tool: ${name}`);
  } catch (err) {
    return text(`Error: ${err.message}`);
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error('voice MCP server error:', err);
  process.exit(1);
});
