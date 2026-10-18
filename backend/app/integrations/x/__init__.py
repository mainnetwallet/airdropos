"""X.com (Twitter) integration for AirdropOS.

Two methods, one capability:

* **Browser mode** - reuses the project's existing persistent Chrome profiles and
  browser controller, so the operator's own X login is reused. This is what runs
  for anything phrased as "go to x.com and ...".
* **API mode** - the official X API v2 through
  :mod:`backend.app.integrations.x.client`, using one of several operator-managed
  configurations, each with its own encrypted app credentials and authorized
  account.

:mod:`backend.app.integrations.x.routing` decides which one runs, based on the
task, the operation, what is authorized, and what is actually available right now.

Nothing in this package exposes a secret: credentials are sealed with
AES-256-GCM at rest, never returned by an API endpoint, and redacted out of logs,
errors and the activity history.
"""

from backend.app.integrations.x import routing  # noqa: F401

__all__ = ["routing"]