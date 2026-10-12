"""Live Broadcast: real-time monitoring and (explicit, locked) manual control of automation sessions.

Layout:
  models.py      shared enums / dataclasses (status, capabilities, event)
  events.py      bounded activity-event log with cursors
  sessions.py    per-browser-session state (pause, waiting-for-user, control lock)
  hub.py         process-wide singletons used by the browser tools and the gateway
  providers/     one provider per integration (browser, telegram, x, discord ...)
  gateway.py     internal HTTP gateway served BY THE WORKER (owns the browsers)
  client.py      API-side client for that gateway
  relay.py       API-side relay of gateway events onto the authenticated WebSocket bus
"""
