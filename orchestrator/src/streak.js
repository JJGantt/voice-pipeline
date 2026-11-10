// A loop that re-runs its read or write on the next tick heals a single failure by itself. console.error
// opens an incident, so a failure here is a warning, and the error is raised once per streak. Any success
// ends the streak.
//
// By default the streak is TIME: RAISE_MS of failing straight. That is for the Mac, where every wake fires
// the overdue timers before Wi-Fi is back and the first requests of the next minute or so fail as a matter
// of course. A machine that never sleeps passes `{ failures: n }` instead and raises on the n-th failure
// in a row (pi-desk's reconcile: 2, because single blips arrive about daily and clear on the next tick).
export const RAISE_MS = 5 * 60 * 1000;

export function streak(label, { failures = null, now = Date.now } = {}) {
  let since = null;
  let count = 0;
  let raised = false;
  return {
    ok() { since = null; count = 0; raised = false; },
    fail(e) {
      since ??= now();
      count += 1;
      const msg = e && e.message ? e.message : String(e);
      const due = failures != null ? count >= failures : now() - since >= RAISE_MS;
      if (!raised && due) {
        raised = true;
        const run = failures != null ? `${count} times in a row` : `since ${new Date(since).toISOString()}`;
        console.error(`${label} failing ${run}: ${msg}`);
      } else {
        console.warn(`${label}: ${msg} (will retry)`);
      }
    },
  };
}
