// Inserts the assistant's single spoken line into voice_speech — the ONLY audio channel. Every
// speaker-equipped surface (the Mac listener today) subscribes to voice_speech via realtime and
// plays the line the instant it lands, regardless of which input captured the command. This is
// separate from voice_messages (the on-screen display thread) and from the activity bar.

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;

export async function say(line) {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error('SUPABASE_URL / SUPABASE_KEY not set in environment.');
  }
  const text = (line || '').toString().trim();
  if (!text) throw new Error('`text` is required (the one short line to speak).');
  const res = await fetch(`${SUPABASE_URL}/rest/v1/voice_speech`, {
    method: 'POST',
    headers: {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=minimal',
    },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(`Supabase insert failed (${res.status}): ${t}`);
  }
  return text;
}
