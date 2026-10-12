# Live Broadcast

Watch every running automation session in real time, open any of them in a large live view,
and (explicitly, with a lock) take over a real browser to complete a human-verification step.

Pages: `/live` (overview) · `/live/<integration>/<key>` (expanded view) · `/browser-profiles` (profiles).

## Architecture

```
Dashboard (React) ──REST + WS──▶ API (FastAPI) ──shared-secret HTTP──▶ Worker gateway ──▶ Playwright / Telethon
        ▲                          │ auth, scopes, audit                  │ owns the real sessions
        └──── live.event over the existing authenticated WebSocket ◀──────┘ (API relays the worker's event log)
```

The browsers and the Telegram client live in the **worker** process, so only the worker can show or control them.
The worker serves a small internal gateway (`backend/app/live/gateway.py`, default `127.0.0.1:8765`). Only the API calls
it, with a shared secret (`LIVE_GATEWAY_SECRET`, or derived from `SECRET_KEY`). Browsers never talk to it. The API does the real
authentication, scope checks and audit logging. In Docker the gateway binds to the private network only (no published port); the worker refuses
to start a non-loopback gateway while `SECRET_KEY` is still the default and no `LIVE_GATEWAY_SECRET` is set.

### Integrations are providers

`backend/app/live/providers/` has one class per integration declaring its **real** capabilities
(`visual`, `events`, `pause_resume`, `stop`, `manual_control`, `keyboard`). The UI only shows controls whose capability is true.
To add X.com or Discord later: write a provider, `register()` it in `providers/__init__.py`. The page does not change.

| Integration | Sessions | Live screen | Pause/Resume | Stop | Manual control |
|---|---|---|---|---|---|
| Browser (Playwright) | one per running context / profile | yes (JPEG frames) | yes | yes | yes (click, scroll, text, keys) |
| Telegram (Telethon, MTProto) | one, after the agent first uses the account | **no** (API session, no GUI) | yes | disconnect | no |
| X.com / Discord | none yet (declared, no provider) | – | – | – | – |

Nothing is faked: no sessions, counts, activity or LIVE badges appear unless they come from the worker.
The LIVE badge only shows while frames are actually arriving.

## Frames

`GET /api/v1/live/sessions/{integration}/{key}/frame?quality=thumb|full` (needs `read`). The browser fetches frames with the
`Authorization` header (no token in URLs) in a sequential loop, so slow links drop frame rate instead of queueing. Thumbnails ≈ every 1.5 s,
expanded view ≈ up to 5 fps. Captures are shared between viewers and cached for a few hundred ms. Frames are CSS-pixel
JPEGs of the real viewport (`X-Frame-Width/Height` headers).

## Screen Click Mode (manual control)

* **Off by default.** While off, the overlay on the picture is inert: taps never reach the session.
* **Enable Click** → `POST …/control` (needs `task_write`). The worker issues an exclusive lock token for that session (TTL 45 s, renewed by a
  heartbeat while the view is open). Another operator gets `409`. While locked, **the agent's browser tools refuse to act** on that session.
* **Take Control** upgrades the same lock to `full`, enabling text and a small allow-list of keys.
* Every input event carries the lock token and a strictly increasing sequence number; stale/duplicate/queued events are dropped,
  and the token dies the instant control is released (queued events are also discarded client-side first).
* **Coordinate mapping:** the picture sits in a box with exactly the viewport's aspect ratio. The client sends the click as a fraction of that box
  (independent of preview size, fullscreen, CSS scaling and device pixel ratio) together with the frame's viewport size; the worker multiplies by the real
  viewport, rejects the click if the viewport changed since that frame (`stale_frame`), and calls Playwright's `mouse.click` on the **actual page**.
* Touch: a still tap is a click; a dragging finger scrolls the remote page.
* Typed text is never written to logs or the activity feed; only "N characters" counts are audited.
* Releasing control saves the profile's cookies (encrypted) and audits the input counts.

## Human verification workflow

1. A browser tool detects CAPTCHA / MFA / login wall → returns `needs_user_action` (existing behavior) and the session is marked **Waiting for you**
   with the page, cookies and profile left exactly as they are. The task parks in `NEEDS_USER_ACTION`.
2. The card is highlighted. Open it → Enable Click → complete the step yourself (only where the site permits manual completion; nothing here solves or bypasses challenges).
3. Disable Click, then **Resume Agent** (`POST …/resume`). The worker **re-inspects the real page first**; if verification is still showing it answers `409 checkpoint_not_verified`
   and nothing resumes. Otherwise the session is cleared and the linked task is moved `NEEDS_USER_ACTION → READY`. Blocked while the emergency stop is active.
4. Every step is in the activity timeline. If nobody responds for `LIVE_HUMAN_WAIT_TIMEOUT_SECONDS` (default 30 min) the control lock is dropped and a timeout event is recorded; the session stays parked.

Pause/Stop work the same way through a gate in the browser tools: a paused/stopped/controlled/waiting session makes the next tool call return `needs_user_action` instead of acting.

## Browser profiles

`/browser-profiles` manages many profiles on one page (name, description, viewport, locale, timezone, start URL). A profile's id is the browser `session_key`, so manual logins carry over to agent tasks.
Metadata: `<BROWSER_USER_DATA_DIR>/profiles.json`. Cookies/local storage: `profiles/<id>/state.enc`, **encrypted at rest** (Fernet, key derived from `SECRET_KEY`), file mode 0600.
Changing `SECRET_KEY` makes saved sessions unreadable (they are ignored, not crashed on). Session keys are normalised to `[A-Za-z0-9_.-]` before touching the filesystem.

## Security summary

* All routes authenticated; reads need `read`, anything that changes a session or sends input needs `task_write` (same as cancelling a task). Read-only logins can watch only.
* Input is bound to (operator, session, lock token); there is no way to address another session through an input event.
* Audit entries: `LIVE_SESSION_START/PAUSE/RESUME/STOP`, `LIVE_CONTROL_ACQUIRE/RELEASE` (with input counts), `BROWSER_PROFILE_*`.
* Activity messages are redacted with the worker's credential redactor; URLs lose query strings/fragments/credentials.
* No new LLM, model routing, shell, SQL, filesystem or network access for the agent; wallet vault, approvals and the emergency stop are untouched.

## Limits (honest list)

* Streaming is repeated JPEG capture, not video (no audio, no drag-and-drop, no right-click/hover, no multi-touch).
* Telegram has no screen. A graphical Telegram session would need a browser-based Telegram Web session (use a browser profile) or a desktop client stream; neither exists in the codebase.
* The agent that is mid-tool-call when you pause finishes that call; it stops at the next tool boundary.
* One worker process: sessions are listed from the worker the API's `LIVE_GATEWAY_URL` points at.
* Resource figures come from the worker host (`psutil` if installed, otherwise load average for CPU only).
