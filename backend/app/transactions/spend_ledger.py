"""Persistent spend ledger (rolling 24-hour window).

Why this exists: the daily spending limit used to live in a Python float on the
engine instance. That float reset on every restart, was private to each process
(API and worker each had their own), and was checked and updated in two
separate steps, so concurrent submissions could all pass the check before any
of them was recorded.

This ledger fixes all three:

* It is a SQLite file shared by every process on the host (docker-compose
  mounts ``./data`` into both the API and the worker), so it survives restarts.
* ``reserve`` checks the limit and records the reservation inside a single
  ``BEGIN IMMEDIATE`` transaction, so the check-and-reserve step is atomic
  across threads and processes.
* A reservation is released only when a submission definitely did not happen;
  an ambiguous outcome keeps counting (fail closed) until it ages out of the
  24-hour window.

Standard library only, so it behaves the same on Windows and Linux.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 60 * 60

RESERVED = "RESERVED"
COMMITTED = "COMMITTED"
RELEASED = "RELEASED"


class SpendLimitExceeded(Exception):
    """Raised when a reservation would breach a per-transaction or daily limit."""

    def __init__(self, reason: str, category: str):
        super().__init__(reason)
        self.category = category


class SpendLedger:
    """Durable reservations of attempted spend over a rolling 24-hour window.

    The ledger is accounting and idempotency infrastructure, not a cap. It enforces a
    ceiling only when one is passed in; the platform configures none by default, so a
    legitimate large transfer is never refused for being large.
    """

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self._explicit_path = path
        self._clock = clock

    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self._explicit_path or settings.SPEND_LEDGER_PATH)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        try:
            # `CREATE ... IF NOT EXISTS` on every connection rather than once per path.
            # A path is not proof that the table is there: the file can be rotated,
            # restored from a backup, or deleted while the process is running, and a
            # cached "I already created it" flag then turned every later reservation
            # into `no such table: spend_reservations`. The statement is a cheap no-op
            # when the table exists, so correctness costs nothing here.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS spend_reservations ("
                " id TEXT PRIMARY KEY,"
                " amount_usd REAL NOT NULL,"
                " status TEXT NOT NULL,"
                " created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL,"
                " ref TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_spend_created ON spend_reservations(created_at)"
            )
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _window_total(conn: sqlite3.Connection, since: float) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM spend_reservations "
            "WHERE status IN (?, ?) AND created_at >= ?",
            (RESERVED, COMMITTED, since),
        ).fetchone()
        return float(row[0] or 0.0)

    # ------------------------------------------------------------------
    def spent_last_24h(self) -> float:
        with self._connect() as conn:
            return self._window_total(conn, self._clock() - WINDOW_SECONDS)

    def reserve(
        self,
        amount_usd: float,
        *,
        per_transaction_limit: Optional[float],
        daily_limit: Optional[float],
        ref: Optional[str] = None,
    ) -> str:
        """Atomically check any configured ceilings and record a reservation.

        A ``None`` limit means the operator configured no ceiling for it, so nothing is
        refused on that basis; the reservation is still recorded, because it is the
        durable evidence of what was attempted and what an ambiguous outcome keeps
        counting against. There is no built-in default ceiling: imposing one here would
        be a fixed application-level cap that no operator asked for.
        """
        if amount_usd is None or amount_usd != amount_usd or amount_usd < 0:
            raise SpendLimitExceeded(
                f"Invalid estimated cost: {amount_usd!r}.", "INVALID_COST"
            )
        if per_transaction_limit is not None and amount_usd > per_transaction_limit:
            raise SpendLimitExceeded(
                f"Estimated transaction cost ${amount_usd:.2f} exceeds the per-transaction "
                f"limit of ${per_transaction_limit:.2f}.", "LIMIT_EXCEEDED",
            )

        now = self._clock()
        reservation_id = uuid.uuid4().hex
        with self._connect() as conn:
            # IMMEDIATE takes the write lock up front: no other process can
            # read the total and insert between our check and our insert.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if daily_limit is not None:
                    projected = self._window_total(conn, now - WINDOW_SECONDS) + amount_usd
                    if projected > daily_limit:
                        conn.execute("ROLLBACK")
                        raise SpendLimitExceeded(
                            f"Projected daily spend ${projected:.2f} exceeds the daily limit of "
                            f"${daily_limit:.2f}.", "DAILY_LIMIT_EXCEEDED",
                        )
                conn.execute(
                    "INSERT INTO spend_reservations (id, amount_usd, status, created_at, updated_at, ref) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (reservation_id, float(amount_usd), RESERVED, now, now, ref),
                )
                conn.execute("COMMIT")
            except SpendLimitExceeded:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return reservation_id

    def commit(self, reservation_id: str, actual_usd: Optional[float] = None) -> None:
        """Mark a reservation as spent, optionally correcting the amount."""
        with self._connect() as conn:
            if actual_usd is None:
                conn.execute(
                    "UPDATE spend_reservations SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (COMMITTED, self._clock(), reservation_id, RESERVED),
                )
            else:
                conn.execute(
                    "UPDATE spend_reservations SET status = ?, amount_usd = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (COMMITTED, float(actual_usd), self._clock(), reservation_id, RESERVED),
                )

    def release(self, reservation_id: str) -> bool:
        """Give back a reservation for a submission that definitely did not happen.

        Only RESERVED rows are released; a COMMITTED row is real spend and is
        never given back. Returns True if a reservation was released.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE spend_reservations SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (RELEASED, self._clock(), reservation_id, RESERVED),
            )
            return cur.rowcount > 0

    def record(self, amount_usd: float, ref: Optional[str] = None) -> str:
        """Record already-incurred spend directly as COMMITTED."""
        now = self._clock()
        reservation_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO spend_reservations (id, amount_usd, status, created_at, updated_at, ref) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reservation_id, float(amount_usd), COMMITTED, now, now, ref),
            )
        return reservation_id

    def reset(self) -> None:
        """Forget all recorded spend (operator/test use only)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM spend_reservations")
