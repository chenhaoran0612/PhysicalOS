"""Reservation / session clean-up (review finding C-05).

Without this, a process crash between a gateway call's reserve and settle
transactions left the reservation ``held`` and the usage record ``pending``
forever, an expired session nobody closed kept its whole budget reserved, and
a crashed session call kept ``inflight_tokens`` > 0 so the session could never
be closed. Operators could not see any of it (``/admin/reservations``
defaulted to ``reconciliation_required``).

A sweep (idempotent, safe to run concurrently with live traffic: every change
is made under the org's wallet lock):

1. **Lost calls** — usage records still ``pending`` after
   ``stale_call_seconds`` (default 600 s, ten times the default upstream
   timeout): marked ``reconciliation_required``. For ``/infer`` the
   reservation goes to ``reconciliation_required`` (credits stay held; a late
   result still settles normally). For a session call, its hold is removed from
   ``inflight_tokens`` and it counts as an unknown call (the session will close
   as ``reconciliation_required``).
2. **Expired sessions** — open sessions past ``expires_at`` with nothing in
   flight are closed automatically exactly as ``/close`` would (settle the
   metered usage, or reconciliation if a call's outcome is unknown);
   ``close_request_id = "auto:expired"``.
3. **Orphan holds** — any other ``held`` reservation older than the longest
   possible session (120 min) + ``stale_call_seconds`` is flagged
   ``reconciliation_required``.

Everything the sweep changes is audited (``maintenance.*``) and visible in
``/admin/reservations`` (``?status=held`` or the default
``reconciliation_required``). It runs: in a background thread when
``maintenance_interval_seconds`` > 0 (default 60 s for the server process),
lazily (at most every 30 s) on gateway requests, on ``POST
/admin/maintenance/sweep``, and via ``python3 -m neuxmind_cloud sweep``.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from . import audit, ledger
from .config import Settings
from .db import Database, lock_wallet
from .models import GatewaySession, Reservation, UsageRecord, now

log = logging.getLogger("neuxmind_cloud.maintenance")
MAX_SESSION_SECONDS = 120 * 60
LAZY_MIN_INTERVAL = 30.0
ACTOR = "system:sweeper"


def sweep(db: Database, settings: Settings, at: float | None = None) -> dict:
    t = now() if at is None else at
    stale_before = t - settings.stale_call_seconds
    counts = {"lost_calls": 0, "sessions_closed": 0, "orphan_holds": 0}

    # 1. lost calls -----------------------------------------------------------
    with db.transaction() as s:
        candidates = [(r.id, r.org_id) for r in s.execute(select(UsageRecord).where(
            UsageRecord.status == "pending", UsageRecord.created_at < stale_before)).scalars()]
    for record_id, org_id in candidates:
        with db.transaction() as s:
            lock_wallet(s, org_id)
            record = s.get(UsageRecord, record_id)
            s.refresh(record)
            if record.status != "pending":
                continue
            record.status = "reconciliation_required"
            record.error = "调用结果丢失（进程中断或超时未返回），已转人工核对"
            if record.session_id:
                gws = s.get(GatewaySession, record.session_id)
                if gws is not None:
                    s.refresh(gws)
                    gws.inflight_tokens = max(0, gws.inflight_tokens - (record.hold_tokens or 0))
                    gws.unknown_calls += 1
            elif record.reservation_id:
                ledger.mark_reconciliation(s, record.reservation_id, "sweeper: call lost (stale pending)")
            audit.record(s, "maintenance.call_lost", ACTOR, org_id=org_id, target=record.id,
                         detail={"session_id": record.session_id, "reservation_id": record.reservation_id})
            counts["lost_calls"] += 1

    # 2. expired sessions ----------------------------------------------------
    from .gateway import close_locked  # local import: gateway imports nothing from here

    with db.transaction() as s:
        expired = [(g.id, g.org_id) for g in s.execute(select(GatewaySession).where(
            GatewaySession.status == "open", GatewaySession.expires_at <= t)).scalars()]
    for session_id, org_id in expired:
        with db.transaction() as s:
            lock_wallet(s, org_id)
            gws = s.get(GatewaySession, session_id)
            s.refresh(gws)
            if gws.status != "open" or gws.close_result is not None or gws.inflight_tokens > 0:
                continue
            close_locked(s, gws, "auto:expired", ACTOR)
            counts["sessions_closed"] += 1

    # 3. orphan holds ----------------------------------------------------------
    orphan_before = t - MAX_SESSION_SECONDS - settings.stale_call_seconds
    with db.transaction() as s:
        orphans = [(r.id, r.org_id) for r in s.execute(select(Reservation).where(
            Reservation.status == "held", Reservation.created_at < orphan_before)).scalars()]
    for reservation_id, org_id in orphans:
        with db.transaction() as s:
            reservation = ledger._locked_reservation(s, reservation_id)
            if reservation.status != "held":
                continue
            open_session = s.execute(select(GatewaySession).where(
                GatewaySession.reservation_id == reservation_id, GatewaySession.status == "open")).scalar_one_or_none()
            if open_session is not None and open_session.inflight_tokens > 0:
                continue  # a call is still running; step 1 will catch it once stale
            ledger.mark_reconciliation(s, reservation_id, "sweeper: hold outlived any possible call/session")
            audit.record(s, "maintenance.orphan_hold", ACTOR, org_id=org_id, target=reservation_id)
            counts["orphan_holds"] += 1
    if any(counts.values()):
        log.info("sweep: %s", counts)
    return counts


class Maintenance:
    """Owns the lazy-sweep throttle and the optional background thread."""

    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self._lock = threading.Lock()
        self._last = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run(self) -> dict:
        with self._lock:
            self._last = time.monotonic()
        return sweep(self.db, self.settings)

    def maybe_sweep(self) -> None:
        """Lazy sweep from request handlers; never raises into the request."""
        with self._lock:
            if time.monotonic() - self._last < LAZY_MIN_INTERVAL:
                return
            self._last = time.monotonic()
        try:
            sweep(self.db, self.settings)
        except Exception:  # pragma: no cover - logged, the request continues
            log.exception("lazy sweep failed")

    def start_background(self, interval: int) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.run()
                except Exception:  # pragma: no cover
                    log.exception("background sweep failed")

        self._thread = threading.Thread(target=loop, name="neuxmind-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
