#!/usr/bin/env node

// "voice" MCP server (stdio): lets any Claude session list focusable Mac Claude
// sessions and switch what the Pi/TV wall is focused on (via Supabase voice_focus).

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { getRouting, setAlwaysOn, setInjectMode } from './routing.js';

const server = new Server({ name: 'voice', version: '1.0.0' }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'get_routing',
      description:
        "Report how the conversational voice agent is routed and its live voice modes. The route is now DYNAMIC: a fast model (Sonnet) handles simple turns and automatically escalates to the most capable model (Opus, max thinking) for anything needing careful data writes — there is no manual model/thinking/runner choice anymore. Also reports whether ALWAYS-ON voice is on (the hands-free, no-wake-word mode — also called 'always on', 'AO', or 'wake-free'; all the same thing) and whether voice-inject mode is on. Use this when Jared asks what model/routing is in effect or whether always-on is enabled.",
      inputSchema: { type: 'object', properties: {} },
    },
    {
      name: 'set_inject_mode',
      description:
        "Turn voice-inject mode on or off. When on, your transcribed voice is injected straight into the active conversation (hands-free). Pass `on: true` to enable, `on: false` to disable.",
      inputSchema: {
        type: 'object',
        properties: { on: { type: 'boolean', description: 'true = enable inject mode, false = disable' } },
        required: ['on'],
      },
    },
    {
      name: 'set_always_on',
      description:
        "Turn the ALWAYS-ON voice system on or off. This is the hands-free, no-wake-word listening mode — Jared calls it 'always on', 'AO', or 'wake-free', and they all mean THIS one toggle. So 'turn on always-on', 'turn always on on', 'enable AO', 'turn off always on', 'disable wake-free', etc. all map here. Pass `on: true` to enable, `on: false` to disable.",
      inputSchema: {
        type: 'object',
        properties: { on: { type: 'boolean', description: 'true = enable always-on (wake-free) voice, false = disable' } },
        required: ['on'],
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
    if (name === 'get_routing') {
      return text(JSON.stringify(await getRouting(), null, 2));
    }
    if (name === 'set_always_on') {
      return text(await setAlwaysOn(a.on));
    }
    if (name === 'set_inject_mode') {
      return text(await setInjectMode(a.on));
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
