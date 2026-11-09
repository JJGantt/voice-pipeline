// Self-healing Supabase realtime subscription.
//
// supabase-js does NOT reliably recover a channel once it lands in CHANNEL_ERROR / TIMED_OUT /
// CLOSED (seen here after the Mac sleeps or a Cloudflare blip): it just sits there, so callers
// fall back to their tight poll cadence forever (this was ~20k wasted reads/day on the focus row).
// So on any non-SUBSCRIBED state we tear the channel down and resubscribe with a capped backoff,
// mirroring the worker's RealtimeWaker. `epoch` invalidates a dead channel's late callbacks so a
// teardown can't schedule a duplicate rebuild.
//
// Callers poll as a missed-event backstop and use isHealthy() to pick a slow or tight cadence.
import { createClient } from '@supabase/supabase-js';
// An explicit WebSocket, not the runtime's. Node only gained a native WebSocket in 22, and this
// module also runs on the Pi, which is on 20 — there supabase-js finds no transport, reports
// "realtime unavailable" and silently leaves every caller polling forever. data-layer's client has
// always passed this for the same reason. Harmless on Node 22+, which simply uses it instead.
import ws from 'ws';
import { config } from './config.js';

const BACKOFF_START_MS = 3000;
const BACKOFF_MAX_MS = 30000;

export function watchTable({ channel: channelName, table, event = '*', onChange, tag }) {
  let supa = null;
  let channel = null;
  let resubTimer = null;
  let epoch = 0;
  let backoffMs = BACKOFF_START_MS;
  let healthy = false;
  // When the current channel last reached SUBSCRIBED. A row inserted after this that the channel never
  // delivered means it is deaf while SUBSCRIBED, which no status callback reports.
  let subscribedAt = 0;
  const log = tag || table;

  const scheduleRebuild = () => {
    if (resubTimer) return; // a rebuild is already pending
    resubTimer = setTimeout(() => {
      resubTimer = null;
      epoch += 1; // invalidate the dead channel's callbacks before tearing it down
      try { if (channel) supa.removeChannel(channel); } catch { /* ignore */ }
      channel = null;
      subscribe();
    }, backoffMs);
    backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
  };

  const subscribe = () => {
    const myEpoch = epoch;
    try {
      if (!supa) supa = createClient(config.SUPABASE_URL, config.SUPABASE_KEY, { realtime: { transport: ws } });
      channel = supa
        .channel(channelName)
        .on('postgres_changes', { event, schema: 'public', table }, (payload) => onChange(payload))
        .subscribe((status) => {
          if (myEpoch !== epoch) return; // stale channel, already rebuilt
          healthy = status === 'SUBSCRIBED';
          console.log(`[${log}] realtime:`, status);
          if (healthy) { backoffMs = BACKOFF_START_MS; subscribedAt = Date.now(); } // recovered — reset the backoff
          else scheduleRebuild();
        });
    } catch (e) {
      console.error(`[${log}] realtime unavailable (polling only):`, e.message);
      healthy = false;
      scheduleRebuild();
    }
  };
  subscribe();

  return {
    isHealthy: () => healthy,
    subscribedAt: () => (healthy ? subscribedAt : 0),
    // Tear the channel down and subscribe afresh, for a caller that has seen it miss a row.
    rejoin() {
      healthy = false;
      if (resubTimer) clearTimeout(resubTimer);
      resubTimer = null;
      epoch += 1;
      try { if (channel) supa.removeChannel(channel); } catch { /* ignore */ }
      channel = null;
      backoffMs = BACKOFF_START_MS;
      subscribe();
    },
    stop() {
      if (resubTimer) clearTimeout(resubTimer);
      try { if (channel) supa.removeChannel(channel); } catch { /* ignore */ }
    },
  };
}
