"""Browser sessions: real Playwright contexts living in this worker process."""

from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from backend.app.browser import profiles as profile_store
from backend.app.browser.controller import controller, url_block_reason
from backend.app.browser.profiles import normalize_session_key
from backend.app.core.config import settings
from backend.app.live import hub
from backend.app.live.events import redact_text, safe_url
from backend.app.live.models import Capabilities, Conflict, Frame, NotSupported, SessionNotFound
from backend.app.live.providers.base import SessionProvider
from backend.app.live.sessions import SessionRecord

# Keys an operator may press. Anything else is refused rather than guessed at.
ALLOWED_KEYS = {
    "Enter", "Tab", "Backspace", "Delete", "Escape",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown",
}
MAX_TEXT = 200
MAX_INPUT_PER_SECOND = 30
FRAME_MIN_INTERVAL = {"full": 0.2, "thumb": 1.2}


class BrowserProvider(SessionProvider):
    integration = "browser"
    label = "Browsers"
    capabilities = Capabilities(
        visual=True, events=True, pause_resume=True, stop=True, manual_control=True, keyboard=True
    )

    def __init__(self) -> None:
        self._frames: Dict[Tuple[str, str], Tuple[float, Frame]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._input_locks: Dict[str, asyncio.Lock] = {}   # one operator input at a time per session
        self._create_locks: Dict[str, asyncio.Lock] = {}  # one start per profile at a time

    def configured(self) -> bool:
        return True

    # ------------------------------------------------------------------ listing
    async def reconcile(self) -> None:
        """Clean up what a crash or a closed tab leaves behind: sessions whose page vanished
        (their context is closed too, cookies saved first) and contexts nothing tracks any more."""
        reg = hub.browser_sessions
        now = time.monotonic()
        for rec in list(reg.records.values()):
            if rec.stopped or controller.is_busy(rec.key):
                continue
            if controller.peek_page(rec.key) is None and now - rec.created_mono > 5:
                reg.mark_stopped(rec.key, by_operator=False, reason="Browser page closed")
                self._drop_frames(rec.key)
                await controller.close_session(rec.key)
        for key in controller.context_keys():
            if controller.is_busy(key) or controller.peek_page(key) is not None:
                continue
            rec = reg.get(key)
            if rec is None or rec.stopped:
                await controller.close_session(key)
        reg.prune_stopped()

    def _drop_frames(self, key: str) -> None:
        for quality in FRAME_MIN_INTERVAL:
            self._frames.pop((key, quality), None)
        self._locks.pop(key, None)

    def _public(self, rec: SessionRecord) -> Dict[str, Any]:
        reg = hub.browser_sessions
        reg.gate(rec.key)  # expires a stale control lock as a side effect
        profile = profile_store.get_profile(rec.key)
        end = rec.stopped_at if rec.stopped and rec.stopped_at else time.time()
        return {
            "id": f"browser:{rec.key}",
            "integration": "browser",
            "key": rec.key,
            "ordinal": rec.ordinal,
            "name": f"Browser #{rec.ordinal}",
            "subtitle": rec.task_title,
            "profile": {"id": profile.id, "name": profile.name} if profile else {"id": rec.key, "name": rec.key},
            "profile_saved": profile is not None,
            "account": None,
            "status": rec.status.value,
            "status_detail": redact_text(
                rec.waiting_reason if rec.waiting else rec.last_error if rec.failed else None
            ) or None,
            "created_at": rec.created_at,
            "elapsed_seconds": max(0, int(end - rec.created_at)),
            "last_activity_at": rec.last_activity_at,
            "task": {"id": rec.task_id, "title": rec.task_title} if rec.task_id else None,
            "url": safe_url(rec.last_url),
            "title": redact_text(rec.last_title),
            "viewport": controller.viewport(rec.key) if not rec.stopped else None,
            "capabilities": self.capabilities.as_dict(),
            "control": rec.control.public() if rec.control else None,
            "waiting": (
                {
                    "reason": rec.waiting_reason,
                    "since": rec.waiting_since,
                    "timed_out": rec.waiting_timed_out,
                }
                if rec.waiting
                else None
            ),
            "meta": {"saved_session_state": profile_store.has_state(rec.key), "paused_by": rec.paused_by},
        }

    async def list_sessions(self) -> List[Dict[str, Any]]:
        await self.reconcile()
        recs = sorted(hub.browser_sessions.records.values(), key=lambda r: r.ordinal)
        return [self._public(r) for r in recs]

    async def get_session(self, key: str) -> Dict[str, Any]:
        await self.reconcile()
        rec = hub.browser_sessions.get(normalize_session_key(key))
        if rec is None:
            raise SessionNotFound(key)
        return self._public(rec)

    # ------------------------------------------------------------------ frames
    async def frame(self, key: str, quality: str) -> Frame:
        key = normalize_session_key(key)
        rec = hub.browser_sessions.get(key)
        if rec is None or rec.stopped:
            raise SessionNotFound(key)
        quality = "full" if quality == "full" else "thumb"
        jpeg_q = settings.LIVE_FRAME_JPEG_QUALITY_FULL if quality == "full" else settings.LIVE_FRAME_JPEG_QUALITY_THUMB
        cache_key = (key, quality)
        interval = FRAME_MIN_INTERVAL[quality]

        cached = self._frames.get(cache_key)
        if cached and time.monotonic() - cached[0] < interval:
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # many viewers share one capture
            cached = self._frames.get(cache_key)
            if cached and time.monotonic() - cached[0] < interval:
                return cached[1]
            shot = await controller.capture_jpeg(key, jpeg_q)
            if shot is None:
                raise SessionNotFound(key)
            frame = Frame(
                data=shot["data"],
                mime="image/jpeg",
                width=shot["width"],
                height=shot["height"],
                frame_id=uuid.uuid4().hex[:10],
                captured_at=time.time(),
            )
            self._frames[cache_key] = (time.monotonic(), frame)
            return frame

    # ------------------------------------------------------------------ lifecycle
    async def _teardown_failed_start(self, key: str) -> None:
        """A start that did not finish leaves nothing behind: no record stuck in 'failed', no
        orphaned context or page, no cached frames. The profile can simply be started again."""
        try:
            hub.browser_sessions.mark_stopped(key, by_operator=False, reason="Session could not be started")
            await controller.close_session(key)
        finally:
            self._drop_frames(key)

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile_id = str(payload.get("profile_id") or "")
        profile = profile_store.get_profile(profile_id) if profile_store.valid_profile_id(profile_id) else None
        if profile is None:
            raise Conflict("Choose an existing browser profile.", "no_profile")
        key = normalize_session_key(profile.id)

        url = str(payload.get("url") or profile.start_url or "").strip()
        if url and not re.match(r"^https?://[^\s]+$", url, re.I):
            raise Conflict("URL must begin with http:// or https://", "bad_url")
        blocked = url_block_reason(url) if url else None
        if blocked:
            raise Conflict(f"That address cannot be opened: {blocked}.", "bad_url")

        reg = hub.browser_sessions
        # Two quick clicks on "Start" must not build two contexts for one profile (the second used to
        # overwrite the first and leak it).
        async with self._create_locks.setdefault(key, asyncio.Lock()):
            existing = reg.get(key)
            if existing is not None and not existing.stopped and controller.peek_page(key) is not None:
                raise Conflict("A session for this profile is already running.", "already_running")
            reg.restart(key)
            try:
                page = await controller.get_page(key)
                if page is None:
                    reg.mark_error(key, "Browser automation is unavailable")
                    raise Conflict(
                        "Browser automation is unavailable in the worker (is Playwright/Chromium installed?).",
                        "browser_unavailable",
                    )
                if profile_store.get_profile(profile.id) is None:
                    raise Conflict("This profile was deleted while the session was starting.", "no_profile")
                reg.ensure(key, profile_id=profile.id, profile_name=profile.name)
                if url:
                    result = await controller.navigate(key, url)
                    if not result.get("ok"):
                        raise Conflict(
                            f"Could not open the URL ({result.get('reason', 'error')}).", "navigation_failed"
                        )
                    obs = result["observation"]
                    if obs.requires_human:
                        reg.mark_waiting(key, obs.human_reason or "Human interaction required.")
            except Exception:
                await self._teardown_failed_start(key)
                raise
        return await self.get_session(key)

    async def action(self, key: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = normalize_session_key(key)
        reg = hub.browser_sessions
        rec = reg.get(key)
        if rec is None:
            raise SessionNotFound(key)
        who = str(payload.get("operator") or "operator")
        owner_id = str(payload.get("owner_id") or who)

        if action == "pause":
            if rec.stopped:
                raise Conflict("Session is stopped.", "stopped")
            reg.pause(key, who)
            return {"status": rec.status.value}

        if action == "resume":
            return await self._resume(rec, who)

        if action == "stop":
            reg.mark_stopped(key, by_operator=True, reason=f"Stopped by {who}")
            await controller.close_session(key)
            self._drop_frames(key)
            return {"status": "stopped", "task_id": rec.task_id}

        if action == "control.acquire":
            if controller.peek_page(key) is None:
                raise Conflict("The browser page is gone. Start a new session.", "no_page")
            lock = reg.acquire(key, owner_id=owner_id, owner_label=who, mode=str(payload.get("mode") or "click"))
            return {
                "token": lock.token,
                "mode": lock.mode,
                "ttl": settings.LIVE_CONTROL_LOCK_TTL_SECONDS,
                "last_seq": lock.last_seq,
            }

        if action == "control.renew":
            lock = reg.renew(key, owner_id=owner_id, token=str(payload.get("token") or ""))
            return {"mode": lock.mode, "ttl": settings.LIVE_CONTROL_LOCK_TTL_SECONDS}

        if action == "control.release":
            # Wait for an input that is mid-flight, so once the release is acknowledged nothing the
            # operator queued can still reach the page while the agent is allowed to continue.
            async with self._input_locks.setdefault(key, asyncio.Lock()):
                counts = reg.release(key, owner_id=owner_id, token=payload.get("token"))
            await controller.save_state(key)  # keep cookies from a manual verification
            return {"released": True, "counts": counts}

        raise NotSupported(f"Unknown action '{action}'.")

    async def _resume(self, rec: SessionRecord, who: str) -> Dict[str, Any]:
        reg = hub.browser_sessions
        key = rec.key
        if rec.stopped:
            raise Conflict("Session is stopped. Start a new session instead.", "stopped")
        if rec.control is not None:
            raise Conflict("Release manual control before resuming the agent.", "control_active")
        resumed_from = []
        if rec.waiting:
            page = controller.peek_page(key)
            if page is None:
                raise Conflict("The browser page is gone. Start a new session.", "no_page")
            obs = await controller.inspect(page)
            reg.touch(key, url=page.url, title=obs.title)
            if obs.requires_human:
                hub.events.emit(
                    integration="browser",
                    session_id=f"browser:{key}",
                    type="verification.recheck_failed",
                    status="waiting",
                    message=f"Checkpoint not verified: {obs.human_reason or 'verification still required'}",
                )
                raise Conflict(
                    f"The page still needs verification: {obs.human_reason or 'human action required'}",
                    "checkpoint_not_verified",
                )
            reg.clear_waiting(key, f"Checkpoint verified by {who}; agent may continue")
            await controller.save_state(key)
            resumed_from.append("waiting_for_user")
        if rec.paused:
            reg.clear_pause(key, who)
            resumed_from.append("paused")
        return {"status": rec.status.value, "task_id": rec.task_id, "resumed_from": resumed_from}

    # ------------------------------------------------------------------ manual input
    async def input(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = normalize_session_key(key)
        reg = hub.browser_sessions
        owner_id = str(payload.get("owner_id") or "")
        token = str(payload.get("token") or "")

        # Inputs run strictly one at a time per session. Without this, a click and a keypress
        # arriving together interleaved their awaits, and a release/stop landing in between could
        # not stop an input that had already been accepted.
        async with self._input_locks.setdefault(key, asyncio.Lock()):
            lock = reg.require_lock(key, owner_id=owner_id, token=token)
            try:
                seq = int(payload.get("seq"))
            except (TypeError, ValueError):
                raise Conflict("Missing input sequence number.", "bad_input")
            if seq <= lock.last_seq:
                raise Conflict("Stale or duplicate input dropped.", "stale_seq")
            lock.last_seq = seq

            now = time.monotonic()
            if now - lock.window_start >= 1.0:
                lock.window_start, lock.window_count = now, 0
            lock.window_count += 1
            if lock.window_count > MAX_INPUT_PER_SECOND:
                raise Conflict("Too much input too quickly.", "rate_limited")

            page = controller.peek_page(key)
            vp = controller.viewport(key)
            if page is None or vp is None:
                raise SessionNotFound(key)

            def still_held() -> None:
                # Raises when the operator released, the lock expired or the session was stopped.
                reg.require_lock(key, owner_id=owner_id, token=token)

            kind = str(payload.get("type") or "")
            rec = reg.get(key)
            if kind in ("click", "dblclick", "wheel"):
                try:
                    fx, fy = float(payload["fx"]), float(payload["fy"])
                except (KeyError, TypeError, ValueError):
                    raise Conflict("Missing coordinates.", "bad_input")
                if not (math.isfinite(fx) and math.isfinite(fy) and 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
                    raise Conflict("Coordinates are outside the screen.", "bad_input")
                # The operator must say which picture size they clicked on. Without it the fractions
                # cannot be trusted to mean the same spot in the real page.
                try:
                    fw, fh = int(payload["frame_w"]), int(payload["frame_h"])
                except (KeyError, TypeError, ValueError):
                    raise Conflict("The view has no picture yet. Wait for it to load and try again.", "stale_frame")
                if (fw, fh) != (vp["width"], vp["height"]):
                    # The picture the operator clicked on no longer matches the real viewport.
                    raise Conflict("The browser viewport changed. Refresh the view and try again.", "stale_frame")
                x = min(max(fx * vp["width"], 0.0), vp["width"] - 1.0)
                y = min(max(fy * vp["height"], 0.0), vp["height"] - 1.0)
                if kind == "wheel":
                    try:
                        dx, dy = float(payload.get("dx") or 0), float(payload.get("dy") or 0)
                    except (TypeError, ValueError):
                        raise Conflict("Bad scroll distance.", "bad_input")
                    if not (math.isfinite(dx) and math.isfinite(dy)):
                        raise Conflict("Bad scroll distance.", "bad_input")
                    dx, dy = max(-3000.0, min(3000.0, dx)), max(-3000.0, min(3000.0, dy))
                still_held()
                await page.mouse.move(x, y)
                still_held()  # the lock may have been released while the pointer moved
                if kind == "click":
                    await page.mouse.click(x, y)
                    hub.events.emit(
                        integration="browser",
                        session_id=f"browser:{key}",
                        type="control.click",
                        status="ok",
                        message=f"{lock.owner_label} clicked at ({int(x)}, {int(y)})",
                    )
                elif kind == "dblclick":
                    await page.mouse.dblclick(x, y)
                else:
                    await page.mouse.wheel(dx, dy)
                lock.counts[kind] = lock.counts.get(kind, 0) + 1
                detail: Dict[str, Any] = {"x": int(x), "y": int(y)}
            elif kind in ("key", "text"):
                if lock.mode != "full":
                    raise Conflict("Keyboard input needs 'Take Control' mode.", "mode")
                if kind == "key":
                    name = str(payload.get("key") or "")
                    if name not in ALLOWED_KEYS:
                        raise Conflict("That key is not allowed.", "bad_input")
                    still_held()
                    await page.keyboard.press(name)
                else:
                    text = str(payload.get("text") or "")
                    if not text or len(text) > MAX_TEXT:
                        raise Conflict(f"Text must be 1-{MAX_TEXT} characters.", "bad_input")
                    still_held()
                    await page.keyboard.type(text, delay=15)  # content is never logged
                lock.counts[kind] = lock.counts.get(kind, 0) + 1
                detail = {}
            else:
                raise Conflict("Unknown input type.", "bad_input")

            lock.expires_at = time.monotonic() + settings.LIVE_CONTROL_LOCK_TTL_SECONDS
            await asyncio.sleep(0.12)
            if rec is not None and not rec.stopped:
                rec.last_url = page.url
                rec.last_activity_at = time.time()
            detail["url"] = safe_url(page.url)
            return detail
