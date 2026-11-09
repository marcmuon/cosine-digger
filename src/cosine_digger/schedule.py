"""Local calendar schedules, persisted runs, and an embeddable scheduler."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
import threading

from . import storage
from .config import AppConfig

JOBS = ("daily", "weekly")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
logger = logging.getLogger(__name__)


def latest_occurrence(job: str, cfg: AppConfig, now: datetime) -> datetime:
    """Return the latest scheduled wall time, including an occurrence at now.

    Naive datetimes represent system local time. An aware ``now`` supplies its
    local timezone, useful to callers with an explicit ZoneInfo timezone.
    """
    if job not in JOBS:
        raise ValueError(f"Unknown scheduled job: {job!r}")
    clock = time.fromisoformat(cfg.daily_time if job == "daily" else cfg.weekly_time)
    day = now.date()
    period = timedelta(days=1 if job == "daily" else 7)
    if job == "weekly":
        day -= timedelta(days=(day.weekday() - _DAYS.index(cfg.weekly_day)) % 7)
    occurrence = datetime.combine(day, clock, tzinfo=now.tzinfo)
    if occurrence > now:
        occurrence -= period
    return occurrence


def next_run(job: str, cfg: AppConfig, now: datetime) -> datetime:
    """Return the next calendar occurrence strictly after now."""
    return latest_occurrence(job, cfg, now) + timedelta(days=1 if job == "daily" else 7)


def _comparable(value: datetime, now: datetime) -> datetime:
    if now.tzinfo is None:
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    return value.astimezone(now.tzinfo)


def due_jobs(
    cfg: AppConfig,
    now: datetime,
    last_success: Callable[[str], datetime | None],
) -> list[tuple[str, datetime]]:
    """Catch up once per job, regardless of how many occurrences were missed."""
    due = []
    for job in JOBS:
        occurrence = latest_occurrence(job, cfg, now)
        succeeded = last_success(job)
        if succeeded is None or occurrence > _comparable(succeeded, now):
            due.append((job, occurrence))
    return due


def last_success(conn: sqlite3.Connection, job: str) -> datetime | None:
    """Read both scheduler 'ok' runs and storage's legacy 'success' runs."""
    legacy = storage.last_success(conn, job)
    row = conn.execute(
        "SELECT finished_at FROM job_runs WHERE job = ? AND status = 'ok' "
        "AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
        (job,),
    ).fetchone()
    values = [datetime.fromisoformat(value) for value in (legacy, row[0] if row else None) if value]
    return max(values, key=lambda value: value.timestamp()) if values else None


def last_failure(conn: sqlite3.Connection, job: str) -> datetime | None:
    row = conn.execute(
        "SELECT finished_at FROM job_runs WHERE job = ? AND status = 'error' "
        "AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
        (job,),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


# A 'running' job_runs row older than this was abandoned (a crash or a kill).
STALE_AFTER_S = 3600


def claim(
    conn: sqlite3.Connection, job: str, scheduled_for: datetime, scheduled_iso: str,
    now: datetime, stale_after_s: float = STALE_AFTER_S, retry_after_s: float = 0,
) -> int | None:
    """Atomically mark a due job as running, unless another process (the app's
    scheduler and an OS-installed schedule can both be live) holds a fresh
    claim, already finished this occurrence, or failed it less than
    ``retry_after_s`` ago. Claims older than ``stale_after_s`` are treated as
    abandoned."""
    if conn.in_transaction:  # the scheduler owns this connection; settle its pending writes
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        running = conn.execute(
            "SELECT started_at FROM job_runs WHERE job = ? AND status = 'running' "
            "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        if running and now.timestamp() - datetime.fromisoformat(running[0]).timestamp() < stale_after_s:
            conn.rollback()
            return None
        succeeded = last_success(conn, job)
        if succeeded is not None and scheduled_for <= _comparable(succeeded, scheduled_for):
            conn.rollback()
            return None
        failed = last_failure(conn, job)
        if failed is not None and now.timestamp() - failed.timestamp() < retry_after_s:
            conn.rollback()
            return None
        cursor = conn.execute(
            "INSERT INTO job_runs (job, scheduled_for, started_at, status) VALUES (?, ?, ?, 'running')",
            (job, scheduled_iso, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return cursor.lastrowid
    except BaseException:
        conn.rollback()
        raise


def _safe_detail(exc: Exception) -> str:
    """What a failed run may store and log: our own messages, else the type only."""
    from .api import scrub
    from .locks import LockTimeout
    from .playlists import PlaylistError
    from .youtube import YouTubeAuthMissing, YouTubeExportError

    if isinstance(exc, (PlaylistError, YouTubeExportError, LockTimeout)):
        return scrub(str(exc))[:500]
    if isinstance(exc, YouTubeAuthMissing):
        return "YouTube Music login missing: run `cosine-digger youtube login`"
    return f"{type(exc).__name__} (details hidden: they can contain credentials)"


def run_due(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    runner: Callable[[str, datetime], str],
    now: datetime | None = None,
    retry_after_s: float = 3600,
) -> list[dict]:
    """Run due jobs independently; a failed job stays due and is retried once
    ``retry_after_s`` has passed since its last failure (not on every tick)."""
    now = now if now is not None else datetime.now()
    results = []
    for job, scheduled_for in due_jobs(cfg, now, lambda job: last_success(conn, job)):
        failed = last_failure(conn, job)
        # timestamp() reads naive values as local time, aware ones exactly.
        if failed is not None and now.timestamp() - failed.timestamp() < retry_after_s:
            continue
        # Store offsets even for naive local schedules, so DB timestamps remain
        # unambiguous when compared with storage's UTC completion timestamps.
        scheduled_iso = scheduled_for.astimezone().isoformat()
        run_id = claim(conn, job, scheduled_for, scheduled_iso, now, retry_after_s=retry_after_s)
        if run_id is None:  # another process is running it, or just finished it
            continue
        try:
            detail = runner(job, scheduled_for)
        except Exception as exc:
            status, detail = "error", _safe_detail(exc)
            # No traceback: provider exceptions (and their causes) can quote credentials.
            logger.error("Scheduled job %s failed (run %s): %s", job, run_id, detail)
        else:
            status = "ok"
        storage.job_finish(conn, run_id, status, detail)
        results.append({
            "job": job, "run_id": run_id, "scheduled_for": scheduled_iso,
            "status": status, "detail": detail,
        })
    return results


class SchedulerLoop:
    """Run immediately, then poll until stopped; each tick owns its connection."""

    def __init__(
        self, cfg: AppConfig, runner: Callable[[str, datetime], str],
        db_path: str | Path, interval_s: float = 300,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.cfg = cfg
        self.runner = runner
        self.db_path = db_path
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cosine-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with closing(storage.connect(self.db_path)) as conn:
                    storage.migrate(conn)
                    run_due(conn, self.cfg, self.runner)
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop.wait(self.interval_s)
