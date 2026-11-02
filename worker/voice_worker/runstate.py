"""Process state more than one module reads or writes. Always accessed as `runstate.<name>`."""





# Set for the duration of a FALLBACK command (`process_request`): the hub is the thing that did not answer,
# so every push to it would only spend its timeout, once per tool call. One request runs at a time.
_hub_unreachable = False


_stopping = False   # set by the signal handler: others stop deferring to a worker on its way out


# True while this worker is standing by (deferring to a healthy higher-priority worker). Set each claim
# attempt in claim_one; read by the heartbeat thread and the main loop to pick the slow standby cadence.
# A plain bool is safe here: one writer (the main loop), and the GIL makes the read/write atomic.
_standby = False


# While standing by with something pending: the monotonic moment the preferred worker's chance to claim it
# runs out. The main loop sleeps to exactly then instead of its slow standby backstop — a clock arrival,
# not a poll — and claim_one stops deferring once it has passed.
_failover_at = 0.0


# Set while a claim is deliberately deferred because a mic is still hearing him. The main loop looks
# again in a second rather than waiting out the idle backstop, so the hold costs ~1s, not ~15.
_holding = False


_running = True
