# X.com Integration — browser profiles + official X API

X.com is **one capability with two methods**. The agent picks the method per task, based on
what the task is, what the operation supports, what is authorized, and what is available right
now. The user never has to choose a tool.

- **Browser mode** reuses the project's existing persistent Chrome profiles and browser
  controller, so the operator's own X login is reused. See `docs/BROWSER_PROFILES.md`.
- **API mode** uses the official X API v2 with operator-managed, encrypted credentials.

Everything is on one page: `/x` in the dashboard (**X.com Integration** in the sidebar).

---

## 1. What it does

| Operation | Browser | API | Required API scope |
|---|:--:|:--:|---|
| `read_profile` — look up an account | ✅ | ✅ | `users.read` |
| `read_post` — read one post | ✅ | ✅ | `tweet.read` |
| `create_post` — publish a post | ✅ | ✅ | `tweet.read`, `tweet.write`, `users.read` |
| `create_reply` — publish a reply | ✅ | ✅ | as above |
| `like` / `unlike` | ✅ | ✅ | `like.read`, `like.write` |
| `follow` / `unfollow` | ✅ | ✅ | `follows.read`, `follows.write` |
| `verify_action` — did it actually happen? | ✅ | ✅ | `tweet.read`, `like.read`, `follows.read`, `users.read` |

Endpoints used (verified against the official reference, OpenAPI `2.172`):

```
GET    /2/users/me                                   whoami
GET    /2/users/by/username/{handle}                 profile
GET    /2/tweets/{id}                                read a post
POST   /2/tweets                                     create post / reply
POST   /2/users/{id}/likes                           like
DELETE /2/users/{id}/likes/{tweet_id}                unlike
POST   /2/users/{id}/following                       follow
DELETE /2/users/{id}/following/{target}              unfollow
GET    /2/users/{id}/liked_tweets                    verify a like
GET    /2/users/{id}/following                       verify a follow
POST   /2/oauth2/token                               refresh / authorize
```

Base URL is fixed at `https://api.x.com`. **Not implemented, on purpose:** media upload,
quote-posting (Enterprise only), DMs, Spaces, Ads. They are not part of this surface and
guessing at them would be worse than refusing.

---

## 2. What actually gates a write

Verified against `docs.x.com/x-api/getting-started/pricing` and
`docs.x.com/x-api/getting-started/getting-access` on **2026-10-18**. This
corrects an earlier version of this document, which claimed writes needed "a
paid API tier". That was wrong: **X API v2 has no subscriptions and no free/paid
write tiers.** It is pay-per-usage credits.

| What you want | What it needs |
|---|---|
| Read posts / profiles | An app plus a credit balance; reads are billed **per resource returned** |
| Post, reply, like, follow, unfollow | An app authorized for user context, the scopes in §1, and credits — writes are billed **per request** |

Published unit costs at the time of writing (always confirm in the Developer
Console, since prices change):

| Action | Cost |
|---|---|
| Post: Create | $0.015 per request |
| Post: Create (with a URL) | $0.200 per request |
| User Interaction: Create (like, follow) | $0.015 per request |
| Interaction: Delete (unlike, unfollow) | $0.010 per request |
| Posts: Read | $0.005 per resource |
| Following/Followers: Read | $0.010 per resource |

Two practical consequences:

- **Requests are blocked when the credit balance is exhausted.** AirdropOS treats
  the resulting error as a hard failure and stops; it never routes around it.
- **Verification reads cost money too.** `x_verify_action` pages a list endpoint,
  so it is page-bounded (§7) and an incomplete check is reported as PARTIAL
  rather than as a definitive "no".

When X answers `403`, AirdropOS reports it as a permission problem and **falls
back to the signed-in browser profile** — it never pretends the write succeeded,
and it never uses the browser to work around an X restriction.

---

## 2b. Limits and response codes

Per-user rate limits for the endpoints this integration uses (15-minute windows,
from the official rate-limit table):

| Endpoint | Limit |
|---|---|
| `GET /2/users/me` | 75 |
| `POST /2/tweets` | 100 |
| `POST`/`DELETE` likes | 50 |
| `POST`/`DELETE` following | 50 |
| `GET /2/users/:id/liked_tweets` | 75 |
| `GET /2/users/:id/following` | 300 |
| `GET /2/tweets/:id` | 900 |
| `GET /2/users/by/username/:username` | 900 |

A `429` is recorded with X's `x-rate-limit-reset`, and the configuration is
refused until that moment rather than being retried into another `429`.

---

## 3. Setup

### 3.1 Browser mode (no API credentials needed)

1. Open **Browser Profiles** and create a persistent Chrome profile.
2. Make it the **default agent profile**.
3. On the **X.com** page, open the profile, click **Open X.com**, and **sign in yourself** in
   the Live Broadcast view.
4. The X page then reports `LOGGED_IN` for that profile.

AirdropOS never types your X password, never solves a CAPTCHA, and never bypasses MFA. When one
of those appears, the operation returns `needs_user_action` and the operator takes over.

### 3.2 API mode

1. Create an app at <https://developer.x.com> and copy its **consumer key** (client id) and
   **consumer secret**.
2. Register a redirect URI on the app. It must match `X_OAUTH_REDIRECT_URI` **exactly**.
   It can be any URL you control — AirdropOS does not host a callback endpoint; you paste the
   `code` back into the page yourself.
3. On the X page: **Add configuration** → enter the name, client id and client secret → **Save**.
4. Click **Authorize**. X shows you the exact scopes; approve them. Paste the `code` from the URL
   it redirects you to.
5. Click **Test connection**. It calls `GET /2/users/me` and records the account it belongs to.

Without `offline.access` no refresh token is issued, and you would have to re-authorize whenever
the access token expires. AirdropOS always requests it.

### 3.3 Environment

```env
# Master switch for the API half. Browser mode keeps working when this is false.
X_API_ENABLED=true

# auto | api_preferred | browser_preferred
X_EXECUTION_PREFERENCE=auto

# Must exactly match a callback URL on the X developer app. AirdropOS uses a
# manual code paste (there is no hosted callback endpoint), so this only has to
# match what you registered.
X_OAUTH_REDIRECT_URI=https://your-operator.example/x-callback

# Optional: pin a dedicated browser profile for X, separate from the default agent profile.
X_BROWSER_PROFILE_ID=
```

The execution preference can also be changed from the X page (**Settings** tab). It is stored in
`system_settings`, not in memory, because the API and the worker are separate processes and must
route identically.

---

## 4. Credential storage

| Property | How |
|---|---|
| At rest | AES-256-GCM envelope, `xcv1.<nonce>.<ciphertext+tag>.<aad>` |
| Key | `WALLET_VAULT_MASTER_KEY` if set, else derived from `SECRET_KEY` |
| AAD binding | Each bundle is bound to its configuration id, so a ciphertext copied into another row fails to open |
| Refusal | In production, a default `SECRET_KEY` with no vault key ⇒ **secrets are refused, not stored** |
| UI | Masks only (`••••`). The real value is never returned by any endpoint |
| Logs | Redacted by `crypto.redact`; audit rows go through `write_audit`'s existing redactor |
| History | `x_activity_records` stores a SHA-256 prefix of post text, never the text |

Never paste a secret into source control. Never commit `.env`.

---

## 5. Bulk import

JSON (a list, or `{"configurations": [...]}`) or CSV with a header row.

| Field | Required | Notes |
|---|:--:|---|
| `name` | ✅ | unique, 1–120 chars |
| `client_id` | ✅ | the app's consumer key |
| `client_secret` | unless `access_token` | needed to refresh a token |
| `access_token` | — | user access token |
| `refresh_token` | — | needed to renew silently |
| `username` | — | `@handle`; *verified*, not trusted, by Test connection |
| `description` | — | free text |
| `enabled` | — | default `true` |
| `default` | — | at most one row per import |

Aliases are accepted and normalised: `label`→`name`, `api_key`→`client_id`,
`api_secret`→`client_secret`, `token`→`access_token`, `handle`→`username`.

**Validate** runs the same parser as the commit and writes nothing. It reports, per row: invalid
fields, duplicate names (in file *and* in the database), conflicting defaults, and soft warnings
(e.g. "a key and secret look swapped"). A malformed credential is rejected, never silently
accepted — a configuration that stores an unusable secret looks fine until the agent tries to
post for you.

---

## 6. Routing

Deterministic code, not a prompt instruction. See `backend/app/integrations/x/routing.py`.

| Situation | Method |
|---|---|
| "Open X.com and inspect this profile" | browser (explicit website intent beats the preference) |
| "Like this post via the API" | api (explicit API intent beats the preference) |
| "Publish this text" with a configured, authorized account | api (it returns a verifiable post id) |
| Same, with no authorized account | browser, and the result says it fell back and why |
| `api_preferred`, API rate-limited, browser available | browser — the preference is not a licence to evade X's limits |
| Neither method available | refused, with **both** reasons so the operator can act |

An explicit `method` on a tool call is honoured when that method supports the operation and is
authorized. Otherwise the agent reports why it cannot — the refusal is an answer, not an obstacle.

---

## 7. Duplicate prevention and uncertain outcomes

This is the part that stops the agent from posting the same thing twice.

1. **Before** every write, a stable fingerprint is computed:
   `<operation>:<account>:sha256(target)[:32]>`.
   Post text is hashed, so the fingerprint cannot be reversed into your words.
2. **A previous SUCCESS** with that fingerprint means the action is already done. The write is
   **not sent**; the original post id and timestamp are returned instead
   (`status: SKIPPED_DUPLICATE`).
3. **A previous UNCERTAIN** with that fingerprint means we do not know. The write is **not sent**;
   the agent is told to run `x_verify_action` first.
4. **An UNCERTAIN result** is produced by a request timeout, a dropped connection, or a browser
   click the page never confirmed. It comes back as a **non-retryable** failure whose message says
   the outcome is unknown, so the deterministic retry policy cannot resend it.
5. `x_verify_action` closes the loop by reading the real state: `GET /2/tweets/{id}`,
   `GET /2/users/{id}/liked_tweets`, or `GET /2/users/{id}/following`.

A browser failure mid-write is treated as uncertain too: the click may have landed before the page
failed to confirm it.

### Tuning it

The "already done" window is a stored setting, `x.duplicate_window_hours` (default `6`). It exists
because the guard has to be escapable: an operator who legitimately wants to publish the same text
twice otherwise has no recourse, since `allow_duplicate` is deliberately unreachable from any tool.

| Value | Effect |
|---|---|
| `6` (default) | Refuse a known-duplicate write within 6 hours |
| `0` | Allow deliberate repeats, but **still never** repeat an UNCERTAIN write |

Set it to `0` in the Security & Settings page (`x.duplicate_window_hours`), or:

```env
# environment is only the fallback; the stored setting wins
X_DUPLICATE_WINDOW_HOURS=6
```

---

## 8. Agent tools

Registered through the normal registry and permission system, so the emergency stop, the audit
trail, argument validation and the operator's confirmation policy all apply unchanged.

```
x_status         x_read_profile   x_read_post     x_post          x_reply
x_like           x_unlike         x_follow        x_unfollow      x_verify_action
```

| Tool | Permissions | Side effect |
|---|---|---|
| `x_status` | `x.read`, `browser.read`, `browser.navigate` | external read |
| `x_read_profile`, `x_read_post`, `x_verify_action` | `x.read`, `browser.read`, `browser.navigate` | external read |
| `x_post`, `x_reply`, `x_like`, `x_unlike`, `x_follow`, `x_unfollow` | `x.write`, `browser.interact`, `browser.navigate` | web interaction |

Each tool takes an optional `method` (`auto` / `api` / `browser`), `config_id`, and
`browser_profile_id`.

Every tool declares `browser.navigate`, **reads included**: X's UI has no in-page API, so reading
a profile means navigating to it. Without it, revoking `browser.navigate` — the permission
`bootstrap.py` deliberately separates from `browser.read` — would not actually stop X work from
changing pages. The chosen method's browser permission is then re-checked live against the
running policy on every call, so revoking it takes effect immediately.

Browser-backed operations hold the controller's **per-session interaction lock** for their whole
duration, exactly as `browser_tools._serialized` does. This matters more here than anywhere else:
`create_post` types into the composer and *then* clicks Post, so an unprotected interleaving would
publish the other task's text to your public account. The session gate is re-checked immediately
before the irreversible click, so stopping the session mid-compose does not post.

**There is no shell, SQL, filesystem or raw-HTTP tool for X.** Every X action goes through one of
these registered tools. `allow_duplicate` is deliberately absent from every schema, so the model
cannot switch the duplicate guard off.

---

## 9. HTTP endpoints

All under `/api/v1/x`, all authenticated.

| Endpoint | Scope |
|---|---|
| `GET /overview`, `/configurations`, `/configurations/{id}`, `/preference`, `/browser-profile`, `/browser-status`, `/activity`, `/catalog` | `read` |
| `POST /configurations`, `PATCH /configurations/{id}`, `/enabled`, `DELETE /configurations/{id}`, `/test`, `/authorize`, `/authorize/complete`, `/import`, `/import/preview`, `/configurations/default`, `/preference`, `/browser-profile`, `/duplicate-window` | **`x_credentials`** |

`x_credentials` is a dedicated scope, exactly like `wallet_write`, because an X access token is a
bearer secret that can post as you: a session that may create tasks cannot mint or destroy your X
identity. It is **not** in `READONLY_SCOPES`. Viewing which accounts are configured is a `read`.

> **After upgrading: log in again.** Scopes are snapshotted into the session row at login
> (`UserSession.scopes`), so a session created before this release has no `x_credentials` and the
> credential endpoints answer `403` until it is re-issued. It fails closed — a new scope never
> widens an existing session.

Every X read is rate limited, including `/overview` and `/browser-status`, which drive a live
Chrome inspection.

---

## 10. Activity & task history

`GET /api/v1/x/activity` returns every X action: which account, which method, which operation,
when, the outcome, and whether the outcome is *known*. Each row carries the `task_id` that
caused it, so an activity record can be traced back to the task that produced it.

The `error` and `target` columns are passed through a token-shaped scrubber before they are
written, so a stray token in an exception message cannot outlive the key rotation in this table.

Deleting a configuration **keeps** its activity history on purpose: that history is the evidence
of what was actually sent with that credential.

### Known limitation: deleting does not revoke at X

`DELETE /configurations/{id}` erases the stored tokens from AirdropOS. It does **not** call X's
`/2/oauth2/revoke`, so the token remains technically valid at X until it expires.

This is deliberate rather than overlooked: revocation is an outbound call inside a destructive
endpoint, and it cannot be exercised without a real authorized account, so shipping it unverified
would risk turning "delete a configuration" into "delete hangs, or fails halfway". Revoke manually
in the X developer console if you need immediate invalidation. Treat this as a documented gap, not a
feature.

---

## 11. Prohibited

- Using the browser to work around an X rate limit, a permission denial, or a suspension.
- Bypassing CAPTCHA, MFA, geo-blocks or any access control.
- Automating mass following, engagement farming, or spam.
- Posting that impersonates a person or project.
- Exposing a credential to the model, the logs, or the UI.

Posts are **untrusted data**. Anything the agent reads on X is treated as data to be analysed,
never as an instruction — the existing `sanitize_external_text()` applies.

---

## 12. Testing

```bash
.venv/Scripts/python.exe -m pytest backend/tests/test_x_integration.py backend/tests/test_x_api.py -q
cd frontend && npx vitest run
```

Every external X call in the suite is mocked.

> **Unverified:** no test in this repository has contacted `api.x.com`, and no live X API
> functionality has been exercised with a real credential. Endpoint shapes, paths and scopes were
> written against the official reference documentation. Confirm against your own app and account
> with **Test connection** before relying on a write.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "No authorized X account" | configuration has app credentials but no user token | run **Authorize** |
| Test connection → `AUTH_REQUIRED` | token revoked, or `X_OAUTH_REDIRECT_URI` mismatch | re-authorize; check the redirect URI matches exactly |
| Test connection → `INVALID` (403) | the app or account is not permitted to do that | check the app's permissions in the developer console |
| `RATE_LIMITED` | X window exhausted | the configuration is refused until `rate_limit_reset_at`; it is **not** retried early |
| `403` on the credential endpoints | your session lacks the `x_credentials` scope | see §9 |
| "Credentials could not be opened" | the encryption key changed | re-authorize; stored secrets cannot be recovered |
| "The credential vault is not ready" | production with a default `SECRET_KEY` and no vault key | set `WALLET_VAULT_MASTER_KEY` |
| `X login: UNKNOWN` | the browser worker is down, or the page is not an X page | check Live Broadcast |
| Browser mode "refused" | session paused, or an operator holds manual control | resume the session in Live Broadcast |