"""Process-wide singletons for the worker process."""

from backend.app.live.events import EventLog
from backend.app.live.sessions import BrowserSessionRegistry

events = EventLog()
browser_sessions = BrowserSessionRegistry(events)
