"""AirdropOS terminal control surface.

One Rich-based dashboard, launched by ``run.py``, that renders the live state of
the whole deployment: backend API, frontend, worker, live gateway, scheduler
jobs, tool registry, task counters, recent events and current warnings.

It is a *reader*, never a second source of truth:

* lifecycle events arrive from :mod:`backend.app.terminal.events`, a
  cross-process JSON-lines stream every process appends to;
* runtime state is read from the same places the rest of the platform reads it
  from (the ``integration_health`` heartbeat rows, the ``automation_schedules``
  and ``tasks`` tables, the shared emergency-stop state file, the HTTP health
  endpoint of the API it is looking at).

Nothing here changes how the application runs, what it logs to its rotating
diagnostic log, or what any API returns.

Submodules are imported directly (``python -m backend.app.terminal``) so that
this package can stay import-light for callers that only need the event stream.
"""