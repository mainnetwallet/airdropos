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
  than working around it.
- Extensions need a real window, so a managed profile is headed by default.
  `BROWSER_HEADLESS_EXTENSIONS=true` allows `--headless=new`, which does support them.

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

## 6. Security notes

See `SECURITY.md` §2.5.0 for the full list. In short:

- Passwords and tokens typed by the agent are filled but never returned, logged or
  shown. Form activity records field **names** only.
- Uploads are restricted to the AirdropOS data directory and `BROWSER_MAX_UPLOAD_BYTES`.
- Cookies are never returned by any tool; Chrome keeps them in its own encrypted
  profile and the agent cannot read them out of it.
- Operator stop, manual control locks, pause and the human-verification park all gate
  every agent action, unchanged.

---

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "This Chrome profile is already open in another process" | Another Chrome has that directory. Close it, or use Reconnect. AirdropOS will not take the profile over. |
| "Google Chrome was not found" | Set `BROWSER_CHROME_EXECUTABLE`, or the executable path on the profile. |
| "Status unknown" on the page | The browser worker is unreachable. Status is never reported as "stopped" when it cannot be confirmed. |
| An extension is not listed | Only extensions actually **installed into the profile** are listed. A developer extension passed with `--load-extension` is deliberately session-scoped. |
| A Web Store install is not automated | Chrome requires a person to accept it. The tool opens the listing and pauses the session for that click. |
