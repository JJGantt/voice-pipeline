// Deaf-channel detection for desk_commands. A realtime channel can sit SUBSCRIBED and deliver nothing;
// supabase-js reports no status change for that, so the only evidence is a row the channel should have
// delivered and did not. Pure so the rule can be tested.

/** Grace for a row whose event is still in flight when a drain reads it. */
export const HEARD_GRACE_MS = 10000;

/**
 * True when `cmd` proves the channel deaf: it was inserted while the channel was SUBSCRIBED
 * (`subscribedAt`, 0 when not subscribed), more than the grace ago, and its id is not in `heard`.
 */
export function missedByChannel(cmd, heard, subscribedAt, now = Date.now()) {
  if (heard.has(cmd.id) || !subscribedAt) return false;
  const created = new Date(cmd.created).getTime();
  return created >= subscribedAt && now - created >= HEARD_GRACE_MS;
}
