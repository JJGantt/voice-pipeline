// The inbox label for a relayed session answer: the question it answers, cut to a few words, written
// by Haiku from the dispatched prompt and the answer. The worker labels the hub's own answers the same
// way (worker/voice_worker/labels.py — the prompt is kept word for word in step with it).
import { spawn } from 'node:child_process';

const LABEL_MAX = 40;
const PROMPT =
  'Someone asked their voice assistant the question below and got the answer below. Write the label '
  + 'for this answer in an inbox list on a watch. The label is the QUESTION, rewritten as short as it '
  + "can be while still saying what was asked: always a question ending in '?', at most 5 words, "
  + 'ideally 3, understandable with no other context (resolve \'it\', \'that one\', \'what about '
  + 'Saturday\' from the answer). Examples: "Who is Paul Rust?", "Surf good Sunday?", "Resident Evil '
  + 'streaming?", "When does Costco close?". Output only the label, no quotes.';

/** Haiku's label, or null when it could not be had (the caller reports that). */
export function makeLabel(question, answer) {
  return new Promise((resolve) => {
    let out = '', done = false, child;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    try {
      // No tools and no MCP servers: the answer can quote anything a session read.
      child = spawn('claude', ['-p', '--model', 'haiku', '--tools', '', '--strict-mcp-config'], { stdio: ['pipe', 'pipe', 'ignore'] });
    } catch (_) { return finish(null); }
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} finish(null); }, 45000);
    child.stdout.on('data', (c) => { out += c; if (out.length > 2000) out = out.slice(0, 2000); });
    child.on('error', () => { clearTimeout(timer); finish(null); });
    child.on('close', () => {
      clearTimeout(timer);
      const line = out.replace(/\s+/g, ' ').trim().replace(/^["']|["']$/g, '');
      finish(line ? line.slice(0, LABEL_MAX) : null);
    });
    const body = `${PROMPT}\n\n--- QUESTION ---\n${String(question || '').trim().slice(0, 4000)}`
      + `\n\n--- ANSWER ---\n${String(answer || '').trim().slice(0, 6000)}`;
    try { child.stdin.end(body); } catch (_) { clearTimeout(timer); finish(null); }
  });
}
