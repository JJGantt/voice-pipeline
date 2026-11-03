// A loop that re-runs its read or write on the next tick heals a single failure by itself. The Mac makes
// those failures routinely: every wake fires the overdue timers before Wi-Fi is back, and the first
// request goes out on a socket closed while it slept. console.error opens an incident, so a failure
// here is a warning, and the error is raised once per streak, when the same loop has failed for
// RAISE_MS straight. Any success ends the streak.
export const RAISE_MS = 5 * 60 * 1000;

export function streak(label) {
  let since = null;
  let raised = false;
  return {
    ok() { since = null; raised = false; },
    fail(e) {
      since ??= Date.now();
      const msg = e && e.message ? e.message : String(e);
      if (!raised && Date.now() - since >= RAISE_MS) {
        raised = true;
        console.error(`${label} failing since ${new Date(since).toISOString()}: ${msg}`);
      } else {
        console.warn(`${label}: ${msg} (will retry)`);
      }
    },
  };
}
