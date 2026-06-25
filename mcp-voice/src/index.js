#!/usr/bin/env node

// "voice" MCP server (stdio): lets any Claude session list focusable Mac Claude
// sessions and switch what the Pi/TV wall is focused on (via Supabase voice_focus).

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { getRouting, setModel, setThinking, setRunner, setAlwaysOn, setInjectMode } from './routing.js';

const server = new Server({ name: 'voice', version: '1.0.0' }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'get_routing',
      description:
        "Report how the conversational agent is currently routed: the model (haiku/sonnet/opus), thinking level (none/medium/maximum), runner (local subscription vs Anthropic API), and whether ALWAYS-ON voice is on (the hands-free, no-wake-word mode — also called 'always on', 'AO', or 'wake-free'; all the same thing). Use this when Jared asks what model/thinking/routing is in effect or whether always-on is enabled.",
      inputSchema: { type: 'object', properties: {} },
    },
    {
      name: 'set_model',
      description:
        "Set which Claude model the conversational agent uses: 'haiku', 'sonnet', or 'opus'. This activates the Custom route so it takes effect immediately for voice and the chat box.",
      inputSchema: {
        type: 'object',
        properties: { model: { type: 'string', description: 'haiku | sonnet | opus' } },
        required: ['model'],
      },
    },
    {
      name: 'set_thinking',
      description:
        "Set the agent's thinking level: 'none' (no extended thinking), 'medium' (moderate), or 'maximum' (deepest — injects ultrathink). Activates the Custom route so it takes effect immediately.",
      inputSchema: {
        type: 'object',
        properties: { level: { type: 'string', description: 'none | medium | maximum' } },
        required: ['level'],
      },
    },
    {
      name: 'set_runner',
      description:
        "Set where the agent runs: 'local' (the free Claude subscription via claude -p on Mac/Pi) or 'api' (the Anthropic Messages API). Activates the Custom route so it takes effect immediately.",
      inputSchema: {
        type: 'object',
        properties: { runner: { type: 'string', description: 'local | api' } },
        required: ['runner'],
      },
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
    if (name === 'set_model') {
      return text(await setModel(a.model));
    }
    if (name === 'set_thinking') {
      return text(await setThinking(a.level));
    }
    if (name === 'set_runner') {
      return text(await setRunner(a.runner));
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
