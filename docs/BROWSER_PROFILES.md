# Browser Profiles & Persistent Chrome

A **browser profile** in AirdropOS is a real Google Chrome profile: a dedicated
user-data directory that Chrome owns. Cookies, site storage, installed extensions and
extension settings live there, so the same identity — with the same logins and the same
extensions — is reused by the next task, the next day, and after an AirdropOS restart.

This document describes how it works, what the agent may do with it, and what it
deliberately cannot do.

---

## 1. Two modes

### Mode A — Managed persistent Chrome (default)

AirdropOS launches the **installed Google Chrome** with
`--user-data-dir=<profile directory>`. This is the mode a normal task uses.

- One directory per profile: `%LOCALAPPDATA%\AirdropOS\ChromeProfiles\<profile-id>`
  on Windows, `~/.airdropos/chrome-profiles/<profile-id>` elsewhere. Override the root
  with `BROWSER_PROFILE_ROOT`.
- Cookies, storage, installed extensions and extension settings **persist**.
- Chrome enforces the directory lock: a second Chrome cannot open a profile another
  Chrome owns. AirdropOS checks the lock first and reports a conflict honestly rather
  than working around it. See "Profile locking and stale-lock recovery" below.
- **Show browser on PC** is a per-profile switch. Off (the default): Chrome runs hidden
  (`--headless=new`, which supports extensions) and nothing opens on the PC; watch and
  click it from Live Broadcast. On: a real Chrome window also opens on the PC.

### Mode B — Connect to an existing Chrome

Where the operator has started Chrome themselves with a remote debugging port, a
profile can connect to it instead of launching a new one.

```bash
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\my-chrome"
```

Set `debugger_port` on the profile (host must stay `127.0.0.1`).

- **Loopback only.** Chrome's DevTools port is unauthenticated remote control; a
  non-loopback host is refused at configuration time and at attach time.
- AirdropOS **disconnects**; it never closes a browser it did not start, and it never
  launches a competing Chrome against a directory that is already in use.
- **Limitation, reported not hidden:** Playwright cannot intercept requests in the
  default context of a CDP browser, so the destination guard covers navigations only.
  `profile_status.route_guard` reports `unavailable_on_existing_chrome` and
  `limitations` explains it.

### Unsupported configurations

- A personal Chrome/Chromium profile root (`%LOCALAPPDATA%\Google\Chrome\User Data`
  and its equivalents) is **refused** as a managed profile directory, and refused
  again by the data-deletion endpoint. AirdropOS will not take over or delete a
  person's own browser data.
- Remote debugging on a non-loopback interface.
- Any `chrome://` page other than `chrome://extensions` and `chrome://version`.

### Profile locking and stale-lock recovery

Chrome leaves lock markers in the profile directory (`lockfile` on Windows,
`SingletonLock` on macOS/Linux). The presence of a marker means *a Chrome has been
here*, **not** *a Chrome is here now* — Chrome removes its markers on a clean exit
but not after a crash, a kill, or a power loss.

`profile_lock` therefore decides on a **live process using that directory**, never on
a file being present:

| Evidence found | Verdict |
| --- | --- |
| A Chrome process running on this directory | **Locked** — refuse to launch |
| Markers, but the recorded owner is provably gone | **Stale** — let Chrome try |
| Markers and the recorded PID is alive but on a *different* directory (PID reuse) | **Stale** |
| No `psutil`, a process scan that failed, a marker from another host, or an owner whose command line cannot be read | **Locked, unverified** — refuse rather than guess |

The governing rule: **absence of evidence only counts as evidence when the search
actually worked.** If AirdropOS cannot look for an owner, it will not assume there is
none.

Nothing is ever deleted. Chrome holds its own exclusive lock, removes its markers on
exit, and takes a stale lock over on launch — verified against real Chrome for both an
empty `lockfile` and one naming a dead PID. AirdropOS only declines to block, and says
so, which is what makes recovery safe against a Chrome that happens to be starting at
the same moment. Two concurrent starts are separated by Chrome's own atomic lock, not
by a check-then-act race here.

Platform limits:

- **Windows** writes a zero-byte `lockfile` that records no owner at all (confirmed by
  inspecting a real one). Recovery therefore rests entirely on the process scan. If
  `psutil` is unavailable there, recovery is refused — safe, but the profile stays
  blocked until `psutil` is installed.
- **macOS/Linux** record the owner as a `<hostname>-<pid>` symlink target. If the host
  does not match this machine, or the symlink was lost (a copied or restored profile),
  the check degrades to the process scan.
- A **zombie** owner counts as dead: it has already exited and cannot hold a profile.
- Processes belonging to **another user** are invisible to `psutil`. Such a profile
  reports unverified and refuses; it needs manual intervention.

---

## 2. Lifecycle

| Action | Endpoint | What actually happens |
|---|---|---|
| Create / edit | `POST` / `PATCH /api/v1/browser/profiles[/{id}]` | Configuration only. Paths are resolved on the server (`%LOCALAPPDATA%`, `~` expanded), validated, and refused if unsafe. |
| Open Browser | `POST /api/v1/browser/profiles/{id}/open` | Worker launches (or reuses) Chrome for that profile and returns its **observed** status. Returns 409 if it did not actually start, 503 if the worker is unreachable. |
| Stop Browser | `POST /api/v1/browser/profiles/{id}/close` | Chrome is closed. The profile data on disk is untouched. |
| Reconnect | `POST /api/v1/browser/profiles/{id}/reconnect` | Health-checks a running browser, or restarts it from the saved configuration. |
| Set as agent default | `POST /api/v1/browser/profiles/default` | Persisted; survives a restart. |
| Delete configuration | `DELETE /api/v1/browser/profiles/{id}` | Removes the **configuration only**. The Chrome directory is returned as `chrome_data_kept_at`. |
| Delete Chrome data | `DELETE /api/v1/browser/profiles/{id}/data` | Irreversible. Requires `{"confirm": "<profile-id>"}`, fails closed while the worker is unreachable, and is refused for an attached browser. |

Stopping a managed profile waits for Chrome to exit. An *installed* Chrome stays alive
in background mode after its last window closes and would keep the directory locked, so
if it lingers AirdropOS terminates only the processes whose command line names that exact
managed directory. That is what makes "stop, then open again" reliable.

---

## 3. The default agent profile

The first profile created becomes the agent's default. The operator can change it at
any time; the choice is stored alongside the profiles and survives a restart.

Every browser tool takes an **optional** `session_key`:

- omitted → the default agent profile;
- supplied → exactly that profile;
- a stale default id → nothing is substituted; the call fails honestly.

`browser_list_profiles` shows the current default and each profile's configuration
problems.

---

## 4. Extensions

`browser_extensions` reads the profile's own `Default/Preferences`, so what it reports
is Chrome's real installed-extension list (component and policy extensions are excluded).

| Action | Supported | Notes |
|---|---|---|
| `list` | Yes | Everything installed into this profile. |
| `check` | Yes | By name or id. |
| `open_extension` | Yes | Opens the extension's own page. |
| `open_settings` | Yes | Opens `chrome://extensions` so the user can install or manage. |
| `web_store` | **Partly** | Opens an explicitly supplied official Chrome Web Store listing, then **parks the session** so the user can accept Chrome's own "Add to Chrome" confirmation. AirdropOS does not script, skip or pre-approve that click. Non-`chromewebstore.google.com` URLs are refused, and AirdropOS never guesses a listing URL or downloads an extension package. |

Developer extensions can be loaded unpacked from a profile's
`unpacked_extensions` directories. Chrome loads them with
`--disable-extensions-except` / `--load-extension`, but **does not record them as
installed**, so they are session-scoped and disappear on restart. `browser_extensions`
reports this rather than implying persistence.

An extension **popup** rendered in Chrome's own window is not part of the page DOM and is
not reachable by any web automation. Only the extension's own pages are.

---

## 5. Agent tools

28 browser tools are registered, permissioned and offered to the planner and the chat
loop. Every one of them reports what actually happened.

| Area | Tools |
|---|---|
| Navigation | `browser_navigate`, `browser_history` (back/forward/reload), `browser_wait` (load state / element / text / URL glob) |
| Tabs | `browser_tabs` (list, open, switch by id / index / URL / next-previous, close) |
| Mouse | `browser_click` (ten locator strategies, right button, double click, force, `expect_text`/`expect_url`), `browser_click_at`, `browser_hover`, `browser_drag` |
| Keyboard | `browser_type`, `browser_press` |
| Scrolling | `browser_scroll` (direction, pixels, edges, a named container, before/after metrics) |
| Forms | `browser_fill_form`, `browser_fill_field`, `browser_submit`, `browser_upload_file` |
| Inspection | `inspect_page`, `browser_snapshot`, `browser_find` |
| Evidence | `capture_evidence`, `browser_diagnostics`, `browser_handle_dialog` |
| Profiles | `browser_list_profiles`, `browser_start_profile`, `browser_status_profile`, `browser_stop_profile`, `browser_close` |
| Extensions / wallets | `browser_extensions`, `browser_wallet` |

Permissions: `browser.read` (observe), `browser.navigate` (change URL),
`browser.interact` (act on a page), `browser.profile` (start/stop/inspect a profile's
browser), `browser.extension` (extension and wallet surfaces). They are granted in
`bootstrap_tools()` and can be withheld per deployment.

Honest-reporting rules the tools enforce:

- A click that merely did not error is **not** a successful submission.
- A miss reports what **is** on the page, so the retry can be useful.
- `browser_history` returns `no_history` at the end of the stack rather than pretending.
- `browser_wallet` never signs, never submits and never claims a receipt, and states
  that a page can define its own `window.ethereum`.

---

## 6. When Chrome may start

Chrome starts **only** as the result of an explicit action:

| Action | Starts Chrome? |
|---|---|
| Opening `/browser-profiles` | **No** |
| The 8-second list poll | **No** |
| Reading a profile's status / health | **No** |
| Opening the extensions modal | **No** |
| Editing, or listing, profiles | **No** |
| Opening the extensions list | **No** |
| **Clicking "Open Browser"** | Yes |
| An agent browser tool that needs a page (`browser_navigate`, `browser_click`, …) | Yes |
| Worker shutdown | No — it closes |

There is **no auto-start option**: nothing launches a browser at startup, on login, or on
a timer. If Chrome should start, an operator starts it.

Two independent guarantees back this up:

1. Nothing on the read path executes a process. The Chrome version shown on the page is
   read from the executable's file metadata (Windows PE resource / macOS `Info.plist` /
   Linux `product_version`), never by running `chrome.exe --version` — that switch does
   not exist on Windows and starts the browser against the **operator's personal profile**,
   which is what produced the first-run profile picker.
2. Immediately before a launch, the controller refuses any profile directory that is (or
   is inside) a real personal Chrome/Chromium profile root, and refuses to launch when no
   managed directory resolves. Chrome silently falls back to the user's own profile when
   `--user-data-dir` is missing, so that fallback is closed off rather than relied upon.

`backend/tests/test_browser_no_autolaunch.py` and the
`Browser Profiles never launches Chrome implicitly` suite in
`frontend/src/pages/BrowserProfiles.test.tsx` lock both in.

---

## 7. Security notes

See `SECURITY.md` §2.5.0 for the full list. In short:

- Passwords and tokens typed by the agent are filled but never returned, logged or
  shown. Form activity records field **names** only.
- Uploads are restricted to the AirdropOS data directory and `BROWSER_MAX_UPLOAD_BYTES`.
- Cookies are never returned by any tool; Chrome keeps them in its own encrypted
  profile and the agent cannot read them out of it.
- Operator stop, manual control locks, pause and the human-verification park all gate
  every agent action, unchanged.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A Chrome window appears without clicking "Open Browser" | Should be impossible. AirdropOS never executes the Chrome binary outside a launch: the version shown on the page is read from the executable's file metadata. `backend/tests/test_browser_no_autolaunch.py` proves it, including that no process of any kind is spawned while reading the list. |
| "This Chrome profile is already open in another process" | Another Chrome has that directory. Close it, or use Reconnect. AirdropOS will not take the profile over. |
| "Google Chrome was not found" | Set `BROWSER_CHROME_EXECUTABLE`, or the executable path on the profile. |
| "Status unknown" on the page | The browser worker is unreachable. Status is never reported as "stopped" when it cannot be confirmed. |
| The Chrome version field is blank | Only populated on platforms where the version can be read from file metadata (Windows via the PE resource, macOS via `Info.plist`, Linux via the `product_version` file). It is display-only and never blocks anything. |
| An extension is not listed | Only extensions actually **installed into the profile** are listed. A developer extension passed with `--load-extension` is deliberately session-scoped. |
| A Web Store install is not automated | Chrome requires a person to accept it. The tool opens the listing and pauses the session for that click. |
