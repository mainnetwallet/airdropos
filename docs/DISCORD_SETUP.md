# Discord Integration

## Status

**Implemented — browser-only, on your own signed-in Discord accounts.**

AirdropOS drives the ordinary discord.com web app through the browser profiles you
already have. You sign in by hand; AirdropOS reuses that session.

There is **no Discord bot**. No bot token, no gateway connection, no Discord API
client, and no unofficial endpoint. The Discord Bot module that once existed in the
source tree is preserved only so its import path keeps resolving; it has never
connected and `health()` reports `NOT_IMPLEMENTED` honestly.

### Why browser-only

| | Browser (what is implemented) | Bot (what is not) |
|---|---|---|
| Servers | Everything your account can already see, including private ones | Only servers the bot was invited to |
| Identity | Your own account, no second identity on a server | A separate "App" user |
| Credentials | None to store — you log in yourself | A long-lived token to protect and rotate |
| Multiple accounts | One browser profile per account, already isolated | Separate bot applications |
| Risk of hitting limits | Normal client behaviour, same as you | Gateway/rate-limit rules apply to the bot |

The cost is honest and worth stating: a browser is slower and more fragile than an
API, and Discord changes its web interface without notice (see
[Known limitations](#known-limitations)).

---

## Architecture

Everything reuses infrastructure that already existed. Nothing here is a second
browser, a second profile manager, or a second task engine.

```
backend/app/integrations/discord/
  browser.py   adapter over the EXISTING browser controller (the only Playwright code)
  service.py   the single executor the tools and the API both call
  routing.py   the operation catalog and the read/write boundary
  state.py     the Discord part of a task checkpoint
  store.py     activity history + duplicate/uncertain-send guards
  models.py    value objects shared across the above
  errors.py    error taxonomy; only CAT_LOGIN_REQUIRED may become a login request
  bot.py       the legacy, non-functional bot router (compatibility only)

backend/app/api/v1/endpoints/discord.py   the page's endpoints
backend/app/tools/discord_tools.py         12 agent tools
backend/app/models/discord_integration.py  one activity table
frontend/src/pages/Discord.tsx             the /discord page
```

Reused as-is: the browser controller and its per-profile interaction lock, the task
engine and its checkpoints, the Inbox intervention system, the tool registry's
permission and side-effect policy, the audit log, the navigation and the UI kit.

---

## Getting started

1. **Create a browser profile** on the [Browser Profiles](../frontend/src/pages/BrowserProfiles.tsx)
   page — one per Discord account you want to use. Name them so the account is
   obvious (`discord-account-1`, `discord-account-2`), because a task is bound to a
   profile by name or id.
2. **Open [Discord](../frontend/src/pages/Discord.tsx)** in the app.
3. **Select the profile** and press **Open Discord in this profile**.
4. **Log in by hand.** AirdropOS will not type your password, pass MFA, or read
   your session. Press **Check login status** when you are done.

That is the whole setup. There is no token, key or `.env` entry to configure.

### Multiple accounts

Each account is a separate browser profile, with its own Chrome user-data directory,
its own cookies and its own local storage. AirdropOS never copies cookies between
profiles and never silently switches one for another.

Every Discord task records the profile it was created with, in its checkpoint. A
retry, a restart and a login resume all reuse that exact profile. If a task named a
profile that no longer exists, it fails and says so rather than picking another one —
acting as the wrong person is the one failure that cannot be undone.

You can also refer to an account in plain language. `"Account 2"` resolves against
the profile names. If more than one profile matches, AirdropOS asks rather than
guessing.

---

## Supported operations

### Read

| Operation | Tool | Notes |
|---|---|---|
| Open Discord | `discord_open` | Reports the real login state it observes |
| List servers | `discord_list_servers` | With ids, so a name can become a channel id |
| Open a channel | `discord_open_channel` | By ids, or by name from the sidebar |
| Read messages | `discord_read_channel` | Optionally scrolls back for older messages |
| Search messages | `discord_search` | Uses Discord's own search box |
| Find unanswered questions | `discord_find_unanswered_questions` | See the caveat below |

### Write

| Operation | Tool | Notes |
|---|---|---|
| Send a message | `discord_send_message` | Verified against the page afterwards |
| Reply to a message | `discord_reply_to_message` | Same |
| Join a server | `discord_join_server` | Only from an invite you supplied |
| Leave a server | `discord_leave_server` | Irreversible; needs `confirmed=true` |

There is no "send to many channels" tool. Unsolicited bulk messaging and fake
engagement are out of scope, and a tool that could do it in one call is a tool that
will.

---

## Read-only, draft and write intent

The composer asks you to pick an intent, and it is enforced **in code**, not in a
prompt. A Discord task created as read-only cannot send, no matter what the model
concludes after reading a channel.

| Intent | What it may do |
|---|---|
| `read` | Open, list, read, search, find questions. **Cannot send anything.** |
| `draft` | Read, then write proposed replies for you to send. **Never opens the composer.** |
| `write` | May post. Every send is verified against the page. |

The intent is decided from **your** request, read back from the task's checkpoint and
its title — never from a tool argument or a message body. That matters because a
Discord channel can contain text shaped like an instruction: if the agent's own
wording were allowed to grant write access, anything posted in a channel could
obtain it. If the intent cannot be read, writes are refused.

Leave it on **Auto-detect** and the intent is read from your own wording. Both
English and Bangla phrasings are recognised — `"একটা reply draft করো"` is a draft,
`"reply পাঠাও"` is a write, `"পড়ো"` is a read.

This boundary is enforced outside the model on purpose. Discord content is untrusted
input: a message in a channel can contain text shaped like an instruction. Because
the agent reads that text, your own words are the only trustworthy statement of what
the task is for, so the read/write line is enforced against them.

---

## Verifying a send

A Discord write cannot be undone, so a click returning without an error is not
treated as proof that anything was sent.

1. The session is re-checked immediately before the irreversible step. If you paused
   the profile while it was typing, nothing is sent.
2. The page is re-read afterwards. The composer emptying, or the message appearing in
   the channel, are the evidence.
3. **If neither is observed, the outcome is `UNKNOWN`** — not success and not failure.
   An unconfirmed send is never retried automatically, because a blind repeat is how
   somebody ends up with the same message twice.

Close the uncertainty with `discord_verify_message`, which reads the channel and
reports whether the message is there. The `/discord` **Activity** tab shows every
such row marked `UNCONFIRMED`.

An identical, *known-completed* send is suppressed for six hours by default. The
fingerprint covers the profile, the **channel the message would actually land in**
(observed from the page, not taken from an argument), the task, and a hash of the
text — so it blocks a genuine duplicate without stopping a legitimate message to a
different channel or from a different task.

Set `discord.duplicate_window_hours` to `0` to allow a deliberate repeat of a
*known-completed* send. That setting never permits repeating an unconfirmed one.

### Resolving an unconfirmed send

An unconfirmed send blocks that exact message, and that block is **never lifted by a
timer or by the model**. There are exactly two ways out:

- `discord_verify_message` finds the message in the channel → recorded as delivered,
  and it then behaves like any known-completed send.
- An operator says what they saw, via **Resolve** on the Activity tab
  (`POST /discord/activity/resolve`, `task_write`, audit-logged):
  - **sent** — recorded as delivered; still blocked, now as an ordinary duplicate.
  - **not sent** — the attempt is abandoned and the message may be sent once.

A verification that does *not* find the message resolves nothing. "Not in the last
50 messages" is not proof of absence, and treating it as permission to resend is
exactly the duplicate the guard exists to prevent.

### Sending to the right channel

The interaction lock is held per call, so two tasks can share a profile and one can
navigate while the other is between calls. Two things prevent that from putting a
message somewhere you did not name it:

- Name the channel (`server_id` + `channel_id`, or `channel_name`) and the open and
  the send happen inside **one** lock acquisition.
- If no channel is named, the current channel is observed, and the send is refused if
  the page is no longer on that channel by the time it runs.

---

## Login recovery

Discord sessions expire. When a task finds a sign-in form, it does **not** guess:

1. It stops, saves its checkpoint, and records completed steps and evidence.
2. The task is marked `NEEDS_USER_ACTION` and an Inbox request appears.
3. The browser interaction lock is released.
4. The request names the exact profile you need to open. No password, cookie or
   token is stored with it.

Press **Open Discord browser**, log in yourself, then press
**I have logged in — Resume task** in the Agent Inbox.

**Your confirmation is not proof.** Before resuming, AirdropOS re-reads the live
discord.com page. If it still shows the sign-in form, the task stays paused with an
explanation — resuming into the same wall would be worse than waiting. Pressing it
repeatedly is safe: it can never start a second execution, because it only runs the
check.

The check reports one of four states, and they are never merged:

| State | Meaning |
|---|---|
| `LOGGED_IN` | Discord is showing a signed-in session |
| `LOGGED_OUT` | Discord is showing its sign-in form |
| `NO_BROWSER` | No browser is open for this profile |
| `UNKNOWN` | **The page could not be read.** Not proof of being signed out. |

A navigation timeout, a missing element, a 5xx or a rate limit is *never* reported
as a logout. When in doubt the answer is `UNKNOWN`, because telling you to sign in to
an account that is already signed in is a worse error than saying nothing.

### No polling

Login state is read inside a real Discord operation, and when you explicitly press
**Check login status**. There is no background checker and no scheduled probe, so a
healthy session produces no traffic and no interruptions.

---

## Authorization and permissions

| Surface | Requires | Notes |
|---|---|---|
| Read endpoints (`/discord/status`, `/activity`, `/tasks`) | `read` | |
| Create a task, open a profile, change settings | `task_write` | |
| `discord.read` tool permission | `discord.read` + `browser.read` + `browser.navigate` | |
| `discord.write` tool permission | `discord.write` + `browser.interact` + `browser.navigate` | |
| Sending a message | All of the above, **plus** write intent on the task | |

Sending a Discord message is not an endpoint. It happens through the agent's
`discord_send_message` tool, which is gated on the permission policy, on the tool
registry's side-effect and emergency-stop checks, and on the intent boundary. Every
Discord action is audit-logged.

Because a Discord account can post as you, `discord.write` is deliberately separate
from `discord.read`. Revoking it stops Discord sends on the next call; it does not
stop Discord reads.

---

## Security

- **No credentials exist.** No token, key or password is stored, sent or displayed.
  The API has no session values to return because none are read.
- **Discord content is untrusted data.** Message text is never treated as
  instructions, and cannot change protected files, settings, or task authorization.
- **No private APIs.** Only the ordinary web interface is used. There is a test that
  fails if `discord.com/api`, a bearer token, or any cookie/storage API appears in
  the browser adapter.
- **Bounded retention.** Activity stores a hash and a short excerpt, never a whole
  transcript.
- **Secrets excluded from logs.** Returned URLs are stripped of query strings, which
  is where a Discord page could carry a token.

---

## Known limitations

Please read this before trusting the integration with a real account.

- **The selectors are unverified against the live site.** Discord's web app is a
  heavily class-mangled SPA that changes without notice. The selectors are
  best-effort and re-resolved on every call, but **none has been confirmed against
  the real discord.com UI**. The test suite uses a mock page; a passing run is
  evidence about the decisions, not about Discord.
- **Unanswered-question detection is a heuristic.** Discord's web page does not
  expose reliable reply threading, so results are reported as *candidates*. Confirm
  any of them before acting.
- **Reading is slower than an API** and depends on the page having rendered.
- **Very long history is paged**, not fetched in bulk.
- **Media is not handled.** Attachments, threads and reactions are out of scope.
- **Voice channels are not supported.**
- **No scheduled monitoring.** A "watch this channel" task runs while it runs; there
  is no background watcher.

### What still needs testing with your real account

**Verified live (read-only, real discord.com, no credentials):**

- A signed-out session is detected as `LOGGED_OUT` with `login_route` evidence, from
  the real page rather than a guess — and is **not** reported as `UNKNOWN`.
- `open_app()` navigates to discord.com and raises the login-required error on a login
  wall, which is the behaviour the Inbox recovery depends on.
- `channel_of()` refuses when the profile is not on a channel, so a send would be
  refused instead of going somewhere unverified.
- Non-Discord destinations are refused, including a look-alike host such as
  `discord.com.evil.example`.

**NOT verified — everything requiring a signed-in session:**

- Logging in, and what the signed-in app chrome actually looks like to the selectors.
- Opening a channel by id and by name, reading, scrolling, and searching.
- Sending, and whether a send is correctly detected after submission.
- The Inbox recovery round-trip against a genuinely expired session.

**Blocker encountered while testing:** a browser profile on the test machine could
not be opened, because its Chrome had left a stale lock file behind and no process
owned it. That was a real defect in the shared browser layer, not in Discord: it
blocked every integration equally. It is now fixed — see `BROWSER_PROFILES.md` —
so the signed-in path is reachable but still untested.

Use a test channel first.

### A stale Chrome lock no longer blocks a profile (fixed)

This blocked the first live test, so it is recorded here for the next reader.

`profile_lock` used to treat the mere **presence** of Chrome's lock files as proof
that a Chrome owned the directory:

```python
if markers:
    return {"locked": True, "reason": f"...still holds Chrome's lock file(s): {markers}"}
```

Chrome removes its markers on a clean exit but not after a crash, a kill or a power
loss, so a profile could become permanently unopenable. It affected every
integration that uses a browser profile — Discord and X alike.

Ownership is now decided by a **live process** using that directory, not by a file
being present. When the recorded owner is provably gone, AirdropOS lets Chrome try
and Chrome recovers the lock itself; AirdropOS deletes nothing, which is what keeps
recovery safe against a Chrome that happens to be starting at the same moment. Where
ownership cannot be determined at all — no `psutil`, a scan that failed, a marker from
another host — it still refuses rather than guessing.

See `BROWSER_PROFILES.md` for the full rule and its platform limits.

---

## Discord policy

AirdropOS will not implement spam, unsolicited bulk messaging, fake engagement, mass
unsolicited invitations, or evasion of Discord's rate limits. Each server's rules and
Discord's Terms of Service apply, and leaving a server requires your explicit
authorization.

If a Discord-side refusal (rate limit, missing permission, suspension) is the answer,
that refusal is reported. The integration will not route around it with the browser.

---

## Testing

```bash
# Backend: 98 Discord tests, all mocked - no Chrome, no discord.com
.venv/Scripts/python.exe -m pytest backend/tests/test_discord_integration.py -q

# Frontend
cd frontend && npm run test
```

**Mocked vs live.** Every backend Discord test is mocked at the browser-controller
boundary. The test suite never opens a real Chrome and never touches discord.com.
The only live exercise was the read-only check listed under
[Known limitations](#what-still-needs-testing-with-your-real-account); a green run
must not be read as evidence that Discord writes work.
