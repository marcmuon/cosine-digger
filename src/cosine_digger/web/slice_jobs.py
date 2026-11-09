"""Durable GUI jobs: reserve, commit, run, and replay the committed journal.

Each job has one serialized event writer. Connections are opened and closed
inside a single call/thread, including stream reads between async yields.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .. import chain, discogs, storage, taste
from ..api import track_key
from ..client import CosineClientError, CosineRequestError
from ..client import failure as cosine_failure
from ..config import usable_key
from ..httpcache import DbHttpCache
from ..locks import file_lock
from ..models import Track
from ..playlists import load_exclusions
from ..profile import build_feed_profile
from ..slice_types import MoveEvent, Point, StepIdentity, build_hubs

KEEPALIVE_SECONDS = 15.0
REPLAY_POLL_SECONDS = 0.5
# A failed cosine.club call, by cause (client.failure): the code picks the step's button.
STEP_ERRORS = {
    "missing": ("cosine_key_missing", "This step needs your cosine.club key. Nothing was lost."),
    "rejected": ("cosine_key_rejected", "cosine.club didn't accept your key, so this step didn't run. Nothing was lost."),
    "unreachable": ("cosine_unreachable", "Couldn't reach cosine.club. Nothing was lost."),
    "busy": ("cosine_unavailable", "cosine.club didn't answer: rate limited. Nothing was lost."),
    "server_error": ("cosine_unavailable", "cosine.club didn't answer: server error. Nothing was lost."),
}
logger = logging.getLogger(__name__)
# While the server drains, a locked terminal write retries this long before
# the worker exits; restart recovery settles the job it leaves running.
DRAIN_FINAL_WRITE_GRACE_SECONDS = 5.0
_queue_lock = threading.Lock()
_queue_users: set[SliceRuntime] = set()


def _run_move(*args, **kwargs):
    from ..api import run_move

    return run_move(*args, **kwargs)


class _MoveClient:
    """Retry GUI transport failures and retain provider release links for facts."""
    def __init__(self, client, *, cancelled=None):
        self.client = client
        self.cancelled = cancelled if cancelled is not None else threading.Event()
        self.tracks = {}
        self.release_links = {}
        self.lock = threading.Lock()

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in {"search", "lookup", "similar"}:
            return method

        def call(*args, **kwargs):
            try:
                result = method(*args, **kwargs)
            except CosineRequestError:
                if self.cancelled.wait(1.0) or self.cancelled.is_set():
                    raise
                result = method(*args, **kwargs)
            tracks = ([result.source_track, *result.similar_tracks] if name == "similar" else result)
            with self.lock:
                self.tracks.update((track.id, track) for track in tracks)
                for track in tracks:
                    key = track_key(track)
                    if key != "|" and track.external_link and discogs._release_reference(track.external_link):
                        self.release_links.setdefault(key, track.external_link)
            return result
        return call


@dataclass(eq=False)
class _Worker:
    release: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    writer: threading.RLock = field(default_factory=threading.RLock)
    action: Callable | None = None
    thread: threading.Thread | None = None
    job_id: str | None = None


class SliceRuntime:
    def __init__(self, *, get_config, client_factory, redact, discogs_client_factory=None,
                 key_result=lambda token, state: None):
        self.get_config = get_config
        self.client_factory = client_factory  # client_factory(config): a cosine.club client with that config's key
        self.redact = redact
        self.discogs_client_factory = discogs_client_factory
        self.key_result = key_result  # tells Setup and the key screen that cosine.club refused the key in use
        self._admission = threading.RLock()
        self._wake = threading.Condition()
        self._revision = 0
        self._accepting = False
        self._started = False
        self._workers: set[_Worker] = set()
        self._jobs: dict[str, _Worker] = {}
        self._server_lock = None
        self._facts = None
        self._db_path = None

    def startup(self):
        with self._admission:
            if self._started:
                return
            self._db_path = self.get_config().db_path.resolve()
            # Key and directory both follow the DB, even if two configs use
            # different app homes for the same database. The short wait only
            # rides out `cosine-digger start/status` probing this lock.
            lock = file_lock(f"slice-server:{self._db_path}", home=self._db_path.parent, timeout=2)
            lock.__enter__()
            try:
                with closing(storage.connect(self._db_path)) as conn:
                    storage.migrate(conn)
                    interrupted = conn.execute(
                        "SELECT id FROM slice_jobs WHERE state='running' ORDER BY started_at,id"
                    ).fetchall()
                for (job_id,) in interrupted:
                    self._recover(job_id)
                with _queue_lock:
                    self._facts = discogs.FactsQueue.shared()
                    _queue_users.add(self)
            except BaseException:
                lock.__exit__(None, None, None)
                raise
            self._server_lock = lock
            self._started = self._accepting = True

    def stop_accepting(self):
        with self._admission:
            self._accepting = False

    def running(self) -> int:
        with self._admission:
            return len(self._workers)

    def drain(self):
        self.stop_accepting()
        with self._admission:
            workers = list(self._workers)
        for worker in workers:
            worker.thread.join()
        with self._admission:
            if not self._started:
                return
            with _queue_lock:
                _queue_users.discard(self)
                if not _queue_users:
                    self._facts.close()
            self._server_lock.__exit__(None, None, None)
            self._server_lock = None
            self._started = False

    def owns(self, job_id):
        """Route the sl- namespace here, including missing IDs (which return 404)."""
        return isinstance(job_id, str) and job_id.startswith("sl-")

    def job_snapshot(self, job_id):
        with closing(storage.connect(self._db_path)) as conn:
            try:
                return storage.get_slice_job(conn, job_id)
            except ValueError:
                raise HTTPException(404, "Job not found") from None

    def _replay(self, job_id, after, last_event_id=None):
        with closing(storage.connect(self._db_path)) as conn:
            conn.execute("BEGIN")
            try:
                job = storage.get_slice_job(conn, job_id)
            except ValueError:
                raise HTTPException(404, "Job not found") from None
            try:
                rows = storage.slice_events(conn, job_id, after=after, last_event_id=last_event_id)
            except storage.SliceConflict:
                raise HTTPException(409, "Cursor is ahead of the journal") from None
            except ValueError:
                raise HTTPException(400, "Invalid event cursor") from None
            cursor = max(after, int(last_event_id.rpartition(":")[2])) if last_event_id is not None else after
            return rows, job["state"], cursor

    def _wait(self, revision, timeout):
        with self._wake:
            self._wake.wait_for(lambda: self._revision != revision, timeout=timeout)

    def job_events(self, job_id, after=0, last_event_id=None):
        # Validate before StreamingResponse sends its 200 headers. Polling the
        # journal also covers a commit missed by any in-memory wake-up.
        initial = self._replay(job_id, after, last_event_id)

        async def stream():
            rows, state, cursor = initial
            heartbeat = time.monotonic()
            while True:
                for row in rows:
                    cursor = row["data"]["seq"]
                    yield (f"id: {row['id']}\nevent: {row['event']}\n"
                           f"data: {storage.canonical_json(row['data'])}\n\n")
                    heartbeat = time.monotonic()
                    if row["event"] in ("done", "error"):
                        return
                if state != "running":
                    return
                with self._wake:
                    revision = self._revision
                rows, state, _ = await asyncio.to_thread(self._replay, job_id, cursor)
                if rows or state != "running":
                    continue
                remaining = KEEPALIVE_SECONDS - (time.monotonic() - heartbeat)
                if remaining <= 0:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                else:
                    await asyncio.to_thread(self._wait, revision, min(REPLAY_POLL_SECONDS, remaining))
                rows, state, _ = await asyncio.to_thread(self._replay, job_id, cursor)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _reserve(self):
        if not self._accepting:
            raise HTTPException(503, "The app is shutting down")
        worker = _Worker()

        def run():
            worker.release.wait()
            try:
                if worker.action is not None:
                    worker.action()
            finally:
                with self._admission:
                    self._workers.discard(worker)
                    if worker.job_id:
                        self._jobs.pop(worker.job_id, None)

        worker.thread = threading.Thread(target=run, name="slice-job-worker", daemon=False)
        self._workers.add(worker)
        try:
            worker.thread.start()
        except Exception:
            self._workers.remove(worker)
            raise HTTPException(503, "Could not start the job") from None
        return worker

    def submit_step(self, create_in_transaction) -> StepIdentity:
        with self._admission:
            worker = self._reserve()
            try:
                with closing(storage.connect(self._db_path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    with conn:
                        identity, launch = create_in_transaction(conn)
                        identity = StepIdentity.model_validate(identity)
                        if type(launch) is not bool:
                            raise ValueError("launch must be boolean")
                        job = storage.get_slice_job(conn, identity.job_id)
                        if job["step_id"] != identity.step_id or (launch and job["state"] != "running"):
                            raise storage.SliceConflict("invalid step job admission")
                if launch:
                    worker.job_id = identity.job_id
                    worker.action = lambda: self._run_step(worker)
                    self._jobs[identity.job_id] = worker
                return identity
            finally:
                # On any rollback/duplicate, action remains None. A provider
                # cannot run until creation and its idempotency record commit.
                worker.release.set()

    def submit_enrichment(self, *, budget, resume_job_id=None, runner):
        if type(budget) is not int or not 1 <= budget <= 500:
            raise ValueError("budget must be an integer from 1 to 500")
        with self._admission:
            worker = self._reserve()
            try:
                with closing(storage.connect(self._db_path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    with conn:
                        active = conn.execute(
                            "SELECT id FROM slice_jobs WHERE kind='label_enrichment' AND state='running'"
                        ).fetchone()
                        if active:
                            return {"job_id": active[0]}
                        if not (self.get_config().discogs_token or "").strip():
                            return {"job_id": None, "skipped": True, "note": {"code": "S-SET-FILL-NOTOKEN"}}
                        job_id = storage.create_enrichment_job(
                            conn, progress={"work": {"budget": budget}}, resume_job_id=resume_job_id
                        )
                        work = storage.get_slice_job(conn, job_id)["progress"].get("work", {})
                        storage.update_slice_progress(conn, job_id, {"work": taste.label_checkpoint_work(conn, work)},
                                                      redact=self.redact)
                worker.job_id = job_id
                worker.action = lambda: self._run_enrichment(worker, runner)
                self._jobs[job_id] = worker
                return {"job_id": job_id}
            finally:
                worker.release.set()

    def cancel_enrichment(self, job_id):
        with self._admission:
            job = self.job_snapshot(job_id)
            if job["kind"] != "label_enrichment":
                raise HTTPException(422, "This job is not label enrichment")
            if job["state"] == "running":
                self._jobs[job_id].cancelled.set()
                return {"job_id": job_id, "status": "stopping"}
            return {"job_id": job_id, "status": job["state"]}

    def _emit(self, worker, event, data):
        with worker.writer:
            with closing(storage.connect(self._db_path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                with conn:
                    payload = storage.append_slice_event(conn, worker.job_id, event, data, redact=self.redact)
            with self._wake:
                self._revision += 1
                self._wake.notify_all()
            return payload

    def _finalize(self, worker, action):
        """Retain the worker until its final writes commit, even during contention.

        Shutdown joins every worker, so once the server drains the retries stop
        after a short grace period instead of holding shutdown on a lock.
        """
        delay, give_up_at = 0.05, None
        while True:
            try:
                with worker.writer:
                    return action()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if not self._accepting:
                    now = time.monotonic()
                    give_up_at = give_up_at or now + DRAIN_FINAL_WRITE_GRACE_SECONDS
                    if now >= give_up_at:
                        return None
                # Each failed transaction has rolled back. Retry committed
                # state, with a capped delay and without restarting providers.
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def _snapshot(self, step_id):
        with closing(storage.connect(self._db_path)) as conn:
            return storage.get_step_content(conn, step_id)

    def _hubs(self, worker, step_id):
        snapshot = self._snapshot(step_id)
        existing = {hub["hub_key"]: hub for hub in snapshot["hubs"]}
        for hub in build_hubs(snapshot["picks"], snapshot["hubs"]):
            if existing.get(hub.hub_key) != hub.to_dict():
                self._emit(worker, "hub", {"hub": hub.to_dict()})

    def _facts_result(self, worker, step_id, result):
        with worker.writer:
            if self.job_snapshot(worker.job_id)["state"] != "running":
                return  # A late callback from a terminal/previous attempt.
            note = result.get("note")
            if note:
                repeated = (note.get("phase") == "facts"
                            and note["code"] in {"discogs_budget_exhausted", "discogs_unavailable", "facts_interrupted"}
                            and any(saved.get("phase") == "facts" and saved["code"] == note["code"]
                                    for saved in self._snapshot(step_id)["step"]["note"]))
                if not repeated:
                    self._emit(worker, "note", note)
            self._emit(worker, "facts", {key: result[key] for key in ("rank", "facts_state", "facts")})
            self._hubs(worker, step_id)

    @contextmanager
    def _discogs_client(self, cfg, tracker, *, priority, rank=None, created_at=None):
        try:
            token = usable_key(cfg.discogs_token, "DISCOGS_TOKEN")
        except ValueError:
            token = None  # a malformed optional token reads as not connected; track digs still run (P9)
        if not token:
            yield None
            return
        with ExitStack() as stack:
            cache = DbHttpCache(self._db_path)
            stack.callback(cache.close)
            factory = self.discogs_client_factory or chain.make_rate_limited_client
            client, actual = factory(
                budget=40, http_cache=cache, token=cfg.discogs_token, budget_tracker=tracker,
                request_slot=lambda: self._facts.request_slot(
                    priority=priority, rank=rank, step_created_at=created_at
                ),
            )
            stack.callback(client.close)
            if actual is not tracker:
                raise ValueError("move and facts must share their request budget")
            yield client

    def _run_step(self, worker):
        job = self.job_snapshot(worker.job_id)
        step_id = job["step_id"]
        failure = None
        failure_exc = None
        token = None
        key_revision = None  # from which settings revision the server holds a refusal of the key
        try:
            step = self._snapshot(step_id)["step"]
            origins = [Point.model_validate(point) for point in step["origins"]]
            cfg = self.get_config()
            token = cfg.cosine_token
            tracker = chain.BudgetTracker(40)
            with closing(storage.connect(self._db_path)) as conn:
                exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
            with ExitStack() as stack:
                client = self.client_factory(cfg)  # the key in cfg is the one the verdict below is about
                stack.callback(client.close)
                move_client = _MoveClient(client, cancelled=worker.cancelled)
                metadata = stack.enter_context(self._discogs_client(cfg, tracker, priority="foreground"))
                events = iter(_run_move(origins, step["move"], step["further"], client=move_client,
                                        discogs_client=metadata, exclusions=exclusions,
                                        cancelled=worker.cancelled.is_set))
                if hasattr(events, "close"):
                    stack.callback(events.close)
                for event in events:
                    event = MoveEvent.model_validate(event)
                    data = {"pick": event.data} if event.event == "pick" else event.data
                    with worker.writer:
                        # S6 resolves copies and announces them before its first
                        # pick. Persist that announcement through the same writer;
                        # origins are snapshot data, never an enrichment checkpoint.
                        if event.event == "note" and "origins" in data.get("progress", {}):
                            data = dict(data)
                            progress = dict(data["progress"])
                            resolved = progress.pop("origins")
                            with closing(storage.connect(self._db_path)) as conn:
                                conn.execute("BEGIN IMMEDIATE")
                                with conn:
                                    storage.resolve_step_origins(conn, step_id, self.redact(resolved))
                            if progress:
                                data["progress"] = progress
                            else:
                                del data["progress"]
                        payload = self._emit(worker, event.event, data)
                        if event.event == "pick":
                            pick = payload["pick"]
                            if pick["facts_state"] == "loading":
                                track = Track(id=pick["cosine_id"], artist=pick["artist"],
                                              track=pick["title"], name=pick["title"], video_id=pick["video_id"])
                                source = move_client.tracks.get(track.id)
                                if source and source.artist == track.artist and (source.track or source.name) == track.track:
                                    track.external_link = source.external_link
                                if not discogs._release_reference(track.external_link or ""):
                                    track.external_link = move_client.release_links.get(pick["track_key"], track.external_link)
                                rank = pick["rank"]
                                self._facts.submit(
                                    job_id=worker.job_id, step_id=step_id, rank=rank,
                                    step_created_at=step["started_at"], track=track,
                                    client_factory=lambda rank=rank: self._discogs_client(
                                        cfg, tracker, priority="facts", rank=rank, created_at=step["started_at"]
                                    ),
                                    on_result=lambda result: self._facts_result(worker, step_id, result),
                                )
                            else:
                                self._hubs(worker, step_id)
                with worker.writer:
                    self._emit(worker, "picks_done", {"pick_count": len(self._snapshot(step_id)["picks"])})
        except Exception as exc:
            failure = self._error(exc)
            failure_exc = exc
            if failure["code"] == "cosine_key_rejected":
                key_revision = self.key_result(token, "rejected")
        self._facts.seal(worker.job_id)
        try:
            self._facts.settle(worker.job_id, unavailable_reason="generation_failed" if failure else None)
        except Exception as exc:
            if failure is None:
                failure = self._error(None)
                failure_exc = exc
        if failure_exc is not None:
            message = self.redact({"message": str(failure_exc)})["message"]
            logger.warning("Step %s failed: %s: %s", step_id, type(failure_exc).__name__,
                           " ".join(message.splitlines())[:500])

        def finish():
            self._settle_loading(worker, step_id)
            self._hubs(worker, step_id)
            snapshot = self._snapshot(step_id)
            if failure:
                refused = {"key_revision": key_revision} if failure["code"] == "cosine_key_rejected" else {}
                self._emit(worker, "error", {"error": failure, "status": "failed", "pick_count": len(snapshot["picks"]), **refused})
            else:
                self._emit(worker, "done", {"status": "done", "pick_count": len(snapshot["picks"]),
                                           "warning_codes": list(dict.fromkeys(n["code"] for n in snapshot["step"]["note"]))})
        self._finalize(worker, finish)

    def _settle_loading(self, worker, step_id):
        with worker.writer:
            snapshot = self._snapshot(step_id)
            loading = [pick for pick in snapshot["picks"] if pick["facts_state"] == "loading"]
            if loading:
                if not any(note.get("phase") == "facts" and note["code"] == "facts_interrupted"
                           for note in snapshot["step"]["note"]):
                    self._emit(worker, "note", {"code": "facts_interrupted", "params": {}, "phase": "facts",
                                               "message": "Facts unavailable: this step stopped before facts settled."})
                for pick in loading:
                    self._emit(worker, "facts", {"rank": pick["rank"], "facts_state": "unavailable", "facts": None})

    def _error(self, exc):
        retry = None
        code, message = "move_failed", "This move stopped with an error: request failed. Nothing was lost."
        if isinstance(exc, CosineClientError):
            # Key problems read as key problems (Add key / Fix key); only busy and
            # server errors carry the Retry countdown (P1).
            state, wait = cosine_failure(exc)
            code, message = STEP_ERRORS[state]
            if state in {"busy", "server_error"}:
                retry = wait
        elif isinstance(exc, discogs.DiscogsTokenMissing):
            code, message = "discogs_missing", str(exc)  # our own words, naming the move
        elif isinstance(exc, discogs.DiscogsAuthError):
            # Only label, artist and person moves need Discogs; track digs treat a refused token as absent.
            code, message = "discogs_rejected", "Discogs didn't accept the token, so this step didn't run. Nothing was lost."
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=retry)).isoformat() if retry is not None else None
        return {"code": code, "message": message, "retry_after_s": retry, "retry_at": retry_at}

    def _run_enrichment(self, worker, runner):
        try:
            with taste.label_checkpoint_redaction(self.redact):
                result = runner(job_id=worker.job_id, get_config=self.get_config,
                                client_factory=self.discogs_client_factory or chain.make_rate_limited_client,
                                cancelled=worker.cancelled.is_set,
                                on_progress=lambda note: self._emit(worker, "note", note))
            event = "error" if result.get("error") else "done"
            if event == "error":
                result = result | {"status": "failed"}
        except Exception:
            event = "error"
            result = {"error": self._error(None), "status": "failed"}
        self._finalize(worker, lambda: self._emit(worker, event, result))

    def _recover(self, job_id):
        worker = _Worker(job_id=job_id)
        job = self.job_snapshot(job_id)
        if job["kind"] == "label_enrichment":
            with closing(storage.connect(self._db_path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                with conn:
                    work = taste.label_checkpoint_work(conn, job["progress"].get("work", {}))
                    storage.update_slice_progress(conn, job_id, {"work": work}, redact=self.redact)
        count = 0
        if job["step_id"]:
            self._settle_loading(worker, job["step_id"])
            self._hubs(worker, job["step_id"])
            count = len(self._snapshot(job["step_id"])["picks"])
        self._emit(worker, "error", {"status": "stopped", "pick_count": count,
                                    "error": {"code": "server_restarted", "message": "This job stopped when the app closed.",
                                              "retry_after_s": None, "retry_at": None}})
