"""Chrome profile-lock regression tests.

The defect these pin
--------------------
``chrome.profile_lock`` treated the mere PRESENCE of Chrome's lock markers as proof
that a Chrome owned the profile. Chrome removes its markers on a clean exit but not
after a crash, a kill, or a power loss - so a profile became permanently unopenable
with no way to recover short of deleting files by hand. On this platform the Windows
``lockfile`` is a ZERO-BYTE file, so it never carried an owner to check in the first
place.

The fix moves the decision onto live ownership: a lock only blocks when a process
using that directory is found, or when ownership cannot be determined at all. Nothing
is ever deleted - Chrome takes a stale lock over on launch, which was verified
against real Chrome before the fix was written.

Safety properties these tests hold
----------------------------------
* A LIVE owner always blocks. Recovery never races a real Chrome.
* Absence of evidence counts as evidence only when the search actually worked: no
  psutil, a scan that raised, a marker from another host, or an owner we cannot
  inspect all REFUSE rather than recover.
* PID reuse cannot block a profile: the recorded pid is checked against the live
  process's real command line, not merely for liveness.
* Malformed, partial and impossible-pid metadata degrades to the process scan instead
  of locking the profile for ever.
* Two concurrent starts never produce two owners.

Nothing here touches a real user profile: every test uses its own temp directory.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from backend.app.browser import chrome


# ======================================================================
# Helpers
# ======================================================================
def _tmp_profile(prefix: str = "locktest-") -> str:
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    return str(directory)


def _seed_empty_lockfile(user_data_dir: str) -> None:
    """Exactly what a killed Chrome leaves on Windows: a zero-byte lockfile."""
    Path(user_data_dir, "lockfile").write_bytes(b"")


def _seed_marker(user_data_dir: str, name: str, content: str) -> None:
    Path(user_data_dir, name).write_text(content, encoding="utf-8")


def _as_singleton_symlink(user_data_dir: str, host: str, pid: int) -> None:
    """POSIX-style SingletonLock: a symlink to <host>-<pid>."""
    path = Path(user_data_dir, "SingletonLock")
    if path.exists() or path.is_symlink():
        path.unlink()
    try:
        os.symlink(f"{host}-{pid}", path)
    except (OSError, NotImplementedError):
        # Windows without developer mode: the regular-file form carries the same
        # "<host>-<pid>" text and is read identically.
        path.write_text(f"{host}-{pid}", encoding="utf-8")


def _psutil_module():
    import psutil

    return psutil


def _without_psutil():
    """Make ``import psutil`` fail, the way a minimal deployment would see it."""
    saved = sys.modules.get("psutil", "absent")
    sys.modules["psutil"] = None
    return saved


def _restore_psutil(saved) -> None:
    if saved == "absent":
        sys.modules.pop("psutil", None)
    else:
        sys.modules["psutil"] = saved


# ======================================================================
# Classification: what counts as a live owner
# ======================================================================
class TestProfileLockClassification:
    def test_no_markers_is_free(self):
        result = chrome.profile_lock(_tmp_profile())
        assert result["locked"] is False
        assert result["stale"] is False
        assert result["verified"] is True

    def test_stale_empty_lockfile_is_recovered(self):
        """The reported defect, exactly: a leftover zero-byte Windows lockfile."""
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        result = chrome.profile_lock(directory)
        assert result["locked"] is False
        assert result["stale"] is True
        assert result["verified"] is True
        assert "lockfile" in result["markers"]
        assert result["processes"] == []

    def test_stale_lock_naming_a_dead_pid_is_recovered(self):
        directory = _tmp_profile()
        _seed_marker(directory, "lockfile", "4194303")  # far above any live pid
        result = chrome.profile_lock(directory)
        assert result["locked"] is False
        assert result["stale"] is True

    def test_lock_is_never_removed_by_a_check(self, monkeypatch):
        """Recovery must be a DECISION, not a deletion.

        Chrome arbitrates the real lock atomically and removes its own markers on
        exit; deleting them here would race a Chrome that is starting.
        """
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        marker = Path(directory, "lockfile")
        chrome.profile_lock(directory)
        assert marker.exists(), "profile_lock must not delete Chrome's lock files"

    def test_a_live_owner_blocks(self, monkeypatch):
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        monkeypatch.setattr(
            chrome,
            "scan_profile_owners",
            lambda d: (True, [{"pid": 4242, "name": "chrome.exe"}]),
        )
        result = chrome.profile_lock(directory)
        assert result["locked"] is True
        assert result["verified"] is True
        assert result["stale"] is False
        assert "4242" in result["reason"]


# ======================================================================
# PID reuse and ambiguous ownership
# ======================================================================
class TestPidReuse:
    def test_a_live_non_chrome_pid_in_the_marker_does_not_lock(self, monkeypatch):
        """A recycled pid is not an owner of this profile.

        The pid is alive, but it is not a Chrome running this directory, so treating
        liveness alone as ownership would block the profile for ever after any pid
        reuse - which is precisely the lockout being fixed.
        """
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", f"{socket.gethostname()}-{os.getpid()}")
        monkeypatch.setattr(chrome, "process_alive", lambda pid: True)
        monkeypatch.setattr(chrome, "process_uses_profile", lambda pid, d: False)
        result = chrome.profile_lock(directory)
        assert result["locked"] is False
        assert result["stale"] is True

    def test_a_live_pid_that_is_an_owner_blocks(self, monkeypatch):
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", f"{socket.gethostname()}-4242")
        monkeypatch.setattr(chrome, "process_alive", lambda pid: True)
        monkeypatch.setattr(chrome, "process_uses_profile", lambda pid, d: True)
        result = chrome.profile_lock(directory)
        assert result["locked"] is True
        assert result["verified"] is True

    def test_an_unreadable_owner_pid_refuses_rather_than_assuming_stale(self, monkeypatch):
        """``None`` means "cannot tell" and must never collapse into "stale"."""
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", f"{socket.gethostname()}-4242")
        monkeypatch.setattr(chrome, "process_alive", lambda pid: True)
        monkeypatch.setattr(chrome, "process_uses_profile", lambda pid, d: None)
        result = chrome.profile_lock(directory)
        assert result["locked"] is True
        assert result["verified"] is False
        assert result["stale"] is False

    def test_an_impossible_pid_is_malformed_not_unverifiable(self):
        """A pid no OS could allocate must not become a permanent lock.

        Treating it as a real pid makes it unverifiable, which blocks the profile -
        the same failure mode this whole change exists to remove.
        """
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", f"{socket.gethostname()}-99999999999999999999")
        owner = chrome._marker_owner(directory)
        assert owner is not None
        assert owner["pid"] is None
        result = chrome.profile_lock(directory)
        assert result["locked"] is False, "an impossible pid must not lock the profile"
        assert result["stale"] is True

    def test_a_marker_from_another_host_is_never_declared_stale(self, monkeypatch):
        """We cannot see another host's processes, so it is not ours to recover."""
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", "some-other-machine-4242")
        monkeypatch.setattr(chrome, "_host_name", lambda: socket.gethostname())
        result = chrome.profile_lock(directory)
        assert result["locked"] is True
        assert result["verified"] is False
        assert "some-other-machine" in result["reason"]

    def test_a_zombie_owner_counts_as_dead(self, monkeypatch):
        """A zombie has already exited and cannot own a profile.

        psutil reports ``is_running()`` as True for a zombie, so trusting it alone
        would let a crashed-and-unreaped worker keep a profile locked until someone
        reaps it. psutil has no zombies on Windows, so this is simulated.
        """
        psutil = _psutil_module()

        class FakeProc:
            def is_running(self):
                return True

            def status(self):
                return psutil.STATUS_ZOMBIE

        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc())
        assert chrome.process_alive(4242) is False

    def test_a_live_non_zombie_owner_is_alive(self, monkeypatch):
        psutil = _psutil_module()

        class FakeProc:
            def is_running(self):
                return True

            def status(self):
                return psutil.STATUS_RUNNING

        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc())
        assert chrome.process_alive(4242) is True

    def test_a_zombie_in_the_marker_does_not_lock_the_profile(self, monkeypatch):
        """End to end: a recorded owner that is a zombie leaves the profile usable."""
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", f"{socket.gethostname()}-4242")
        psutil = _psutil_module()

        class FakeProc:
            def is_running(self):
                return True

            def status(self):
                return psutil.STATUS_ZOMBIE

        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc())
        result = chrome.profile_lock(directory)
        assert result["locked"] is False
        assert result["stale"] is True


# ======================================================================
# Untrustworthy evidence
# ======================================================================
class TestUntrustworthyEvidence:
    def test_without_psutil_markers_do_not_justify_recovery(self):
        """The keystone rule.

        With no way to look for an owner, a marker is not evidence that one exists -
        and it is not evidence that one does not. Recovery is therefore refused.
        """
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        saved = _without_psutil()
        try:
            result = chrome.profile_lock(directory)
        finally:
            _restore_psutil(saved)
        assert result["locked"] is True
        assert result["verified"] is False
        assert "psutil" in result["reason"]

    def test_a_failing_scan_is_not_read_as_found_nobody(self):
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        psutil = _psutil_module()
        original = psutil.process_iter
        psutil.process_iter = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = chrome.profile_lock(directory)
        finally:
            psutil.process_iter = original
        assert result["locked"] is True
        assert result["verified"] is False

    def test_recovery_resumes_once_the_scan_works_again(self):
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        psutil = _psutil_module()
        original = psutil.process_iter
        psutil.process_iter = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            assert chrome.profile_lock(directory)["locked"] is True
        finally:
            psutil.process_iter = original
        assert chrome.profile_lock(directory)["locked"] is False


# ======================================================================
# Malformed metadata
# ======================================================================
class TestMalformedMetadata:
    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not-a-pid", "----", "abc-def-ghi", "-", "99999999999999999999", "12 34"],
    )
    def test_malformed_marker_never_locks_the_profile(self, raw):
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", raw)
        result = chrome.profile_lock(directory)
        assert result["locked"] is False, f"malformed metadata {raw!r} locked the profile"
        assert result["stale"] is True

    def test_marker_owner_reports_what_it_found(self):
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", "not-a-pid")
        owner = chrome._marker_owner(directory)
        assert owner is not None
        assert owner["pid"] is None
        assert owner["source"] == "SingletonLock"

    def test_no_markers_yields_no_owner(self):
        assert chrome._marker_owner(_tmp_profile()) is None

    def test_a_partially_written_marker_is_still_classified_safely(self):
        """A bare pid with no host is accepted, and then checked properly.

        A pid without a host cannot be attributed to another machine, so it is only
        evidence about THIS host - which is exactly how it is treated: liveness and
        the live command line decide, and an unreachable pid is simply stale.
        """
        directory = _tmp_profile()
        _seed_marker(directory, "SingletonLock", "4242")
        owner = chrome._marker_owner(directory)
        assert owner is not None
        assert owner["pid"] == 4242
        assert owner["host"] == ""
        # 4242 is not running here, so this must not lock the profile.
        assert chrome.profile_lock(directory)["locked"] is False

    def test_unreadable_marker_body_is_survivable(self, monkeypatch):
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)
        original = Path.read_text

        def boom(self, *args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", boom)
        result = chrome.profile_lock(directory)
        assert result["locked"] is False
        assert result["stale"] is True


# ======================================================================
# Integration, against a real Chrome
#
# These launch Chrome in a temp directory. They never touch a user profile, and they
# SKIP (never silently pass) when Chrome cannot launch in this environment.
# ======================================================================
def _chrome_launches() -> bool:
    """Whether a real browser can start here. Probed once, at import."""
    async def probe() -> bool:
        try:
            from playwright.async_api import async_playwright
        except Exception:
            return False
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=["--no-sandbox"], timeout=30000)
                await browser.close()
            return True
        except Exception:
            return False

    return asyncio.run(probe())


_CHROME_OK = _chrome_launches()

requires_chrome = pytest.mark.skipif(
    not _CHROME_OK, reason="Chrome/Chromium cannot launch in this environment"
)


async def _lifecycle(user_data_dir: str, on_open=None):
    """Launch a real Chrome, let the caller inspect it, then close - all on ONE loop.

    Playwright objects are bound to the loop that created them, so the whole lifecycle
    has to happen inside a single ``asyncio.run``. Doing the launch and the close in
    two separate runs leaves the context's transport dead, which looks like a Chrome
    failure but is not one.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            executable_path=chrome.find_chrome_executable().get("path") or None,
            headless=True,
            args=list(chrome.BASE_CHROME_ARGS),
            ignore_default_args=list(chrome.PLAYWRIGHT_IGNORED_DEFAULT_ARGS),
            timeout=45000,
        )
        if on_open is not None:
            await on_open(context)
        return True
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass


def browser_owners(user_data_dir: str) -> list:
    """Chrome BROWSER processes (not children) using this directory.

    Chrome spawns many children per profile, each carrying ``--type=renderer`` and
    friends. Only the browser process owns the profile directory, so two of those means
    two owners - the state that actually corrupts a profile.
    """
    import psutil

    target = chrome._norm_dir(user_data_dir)
    owners = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"], ad_value=None):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue
        if "chrome" not in name and "chromium" not in name:
            continue
        if not chrome._command_line_uses_profile(list(cmdline), target):
            continue
        if any(str(arg).startswith("--type=") for arg in cmdline):
            continue
        owners.append({"pid": proc.info.get("pid"), "name": proc.info.get("name")})
    return owners


@requires_chrome
class TestRealChromeOwnership:
    def test_a_live_chrome_owns_its_profile(self):
        """The protection that must never regress."""
        directory = _tmp_profile()
        seen = {}

        async def inspect(_context):
            seen.update(chrome.profile_lock(directory))

        assert asyncio.run(_lifecycle(directory, inspect)) is True
        assert seen["locked"] is True, "a running Chrome did not lock its own profile"
        assert seen["verified"] is True
        assert seen["processes"], "the owner process was not identified"
        assert seen["stale"] is False

    def test_a_live_chrome_is_refused_by_the_pre_launch_check(self):
        """A second start must be refused while the first one holds the profile."""
        directory = _tmp_profile()
        refused = {}

        async def inspect(_context):
            refused.update(chrome.profile_lock(directory))

        assert asyncio.run(_lifecycle(directory, inspect)) is True
        assert refused["locked"] is True
        assert refused["verified"] is True

    def test_stale_lockfile_recovers_and_chrome_cleans_up_afterwards(self):
        """The reported defect, end to end against real Chrome.

        A leftover zero-byte lockfile must not make the profile unopenable, and
        recovery must not require AirdropOS to delete anything.
        """
        directory = _tmp_profile()
        _seed_empty_lockfile(directory)

        verdict = chrome.profile_lock(directory)
        assert verdict["locked"] is False
        assert verdict["stale"] is True
        assert Path(directory, "lockfile").exists(), (
            "the pre-launch check must not delete Chrome's lock files"
        )

        opened = {}

        async def inspect(_context):
            opened.update(chrome.profile_lock(directory))

        assert asyncio.run(_lifecycle(directory, inspect)) is True, (
            "a stale lockfile made the profile unopenable"
        )
        # Chrome took the stale lock over; now it genuinely owns the profile.
        assert opened["locked"] is True

        # A clean exit removes the markers entirely.
        assert chrome.lock_markers(directory) == []

    def test_abnormal_termination_leaves_a_recoverable_profile(self):
        """Killed Chrome -> leftover markers -> the profile is usable again.

        Nothing is deleted by AirdropOS; Chrome performs the recovery itself, which is
        what makes this safe against a Chrome that happens to be mid-startup.
        """
        psutil = _psutil_module()
        directory = _tmp_profile()

        async def open_and_kill():
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            context = None
            try:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=directory,
                    executable_path=chrome.find_chrome_executable().get("path") or None,
                    headless=True,
                    args=list(chrome.BASE_CHROME_ARGS),
                    ignore_default_args=list(chrome.PLAYWRIGHT_IGNORED_DEFAULT_ARGS),
                    timeout=45000,
                )
            finally:
                # Abrupt termination: no clean close, so no marker cleanup.
                for entry in chrome.chrome_processes_for(directory):
                    try:
                        psutil.Process(entry["pid"]).kill()
                    except Exception:
                        pass
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await pw.stop()
                except Exception:
                    pass

        asyncio.run(open_and_kill())

        import time

        for _ in range(60):
            if not chrome.chrome_processes_for(directory):
                break
            time.sleep(0.25)

        verdict = chrome.profile_lock(directory)
        markers = chrome.lock_markers(directory)
        if markers:
            assert verdict["locked"] is False, (
                f"a killed Chrome left {markers} but the profile was still locked"
            )
            assert verdict["stale"] is True

        # And the profile really can be opened again.
        assert asyncio.run(_lifecycle(directory)) is True

    def test_concurrent_startup_never_produces_two_owners(self):
        """Three OS processes racing on one directory must settle on ONE owner.

        This is the property that makes "let Chrome arbitrate" safe. AirdropOS decides
        nothing about the lock itself - it never deletes a marker - so two racing starts
        are separated by Chrome's own atomic lock rather than by a check-then-act race
        inside AirdropOS, which a delete-the-marker approach would have introduced.

        Two subtleties this test has to get right, both measured rather than assumed:

        * The invariant is NOT "one process reported success". Chrome's single-instance
          model lets an extra launch hand off to the running browser, so several racers
          legitimately succeed.
        * The invariant is NOT "never two processes at once". Chrome's handoff briefly
          has a second browser process alive on the directory before it exits. Measured
          here as a single ~0.2s sample. That second process never uses the profile.

        What is asserted is the property that actually protects the profile: ownership
        settles to exactly one owner, STAYS there, and the profile is still usable.
        """
        import subprocess
        import sys
        import time

        directory = _tmp_profile()
        repo = str(Path(__file__).resolve().parent.parent.parent)
        script = (
            "import asyncio, json, sys\n"
            f"sys.path.insert(0, {repo!r})\n"
            "from playwright.async_api import async_playwright\n"
            "from backend.app.browser import chrome\n"
            f"DIRECTORY = {directory!r}\n"
            "async def main():\n"
            "    pw = await async_playwright().start()\n"
            "    try:\n"
            "        ctx = await pw.chromium.launch_persistent_context(\n"
            "            user_data_dir=DIRECTORY,\n"
            "            executable_path=chrome.find_chrome_executable().get('path') or None,\n"
            "            headless=True, args=list(chrome.BASE_CHROME_ARGS),\n"
            "            ignore_default_args=list(chrome.PLAYWRIGHT_IGNORED_DEFAULT_ARGS),\n"
            "            timeout=60000)\n"
            "    except Exception as exc:\n"
            "        print(json.dumps({'ok': False, 'detail': str(exc)[:200]}))\n"
            "        await pw.stop(); return\n"
            "    try:\n"
            "        page = ctx.pages[0] if ctx.pages else await ctx.new_page()\n"
            "        await page.goto('about:blank', timeout=20000)\n"
            "        await asyncio.sleep(4)\n"
            "        print(json.dumps({'ok': True, 'detail': 'held'}))\n"
            "    finally:\n"
            "        try: await ctx.close()\n"
            "        except Exception: pass\n"
            "        try: await pw.stop()\n"
            "        except Exception: pass\n"
            "asyncio.run(main())\n"
        )

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(3)
        ]

        samples = []
        deadline = time.time() + 9.0
        while time.time() < deadline and any(p.poll() is None for p in procs):
            samples.append(len(browser_owners(directory)))
            time.sleep(0.2)

        for proc in procs:
            try:
                proc.wait(timeout=180)
            except Exception:
                proc.kill()

        outputs = []
        for proc in procs:
            try:
                outputs.append(proc.stdout.read() or "")
            except Exception:
                outputs.append("")

        assert any('"ok": true' in out or '"ok":true' in out for out in outputs), (
            "no racer managed to open the profile at all"
        )
        assert samples, "the race was never observed"

        # Ownership must SETTLE and STAY settled. Measured shape:
        #   [0, 0, 0, <burst>, 1, 1, 1, ... 1]
        # The burst is every racer starting at once, plus Chrome's handoff. From the
        # first sample that shows a single owner onwards there must never be another.
        assert 1 in samples, f"no single owner was ever observed: {samples}"
        settled_from = samples.index(1)
        after = samples[settled_from:]
        assert len(after) >= 3, f"not enough settled samples to be meaningful: {samples}"
        assert all(count == 1 for count in after), (
            "ownership did not stay on exactly one owner - a second owner appeared "
            f"after startup settled, which is the concurrency this must prevent: {samples}"
        )

        # And the profile survived the race intact.
        assert asyncio.run(_lifecycle(directory)) is True
