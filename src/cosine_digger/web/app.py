"""Local JSON API, the dig workspace, and replayable legacy job streams."""
from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager, closing, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .. import api, chain, playlists, schedule, session, youtube
from .. import companion as pairing
from ..commands.doctor import run_checks
from ..config import ENV_VARS, SECRET_FIELDS, load_config, save_settings, usable_key
from ..leads import record_event
from ..locks import LockTimeout
from ..models import Track
from ..profile import build_profile
from ..runtime import INSTANCE, STOP_PATH, TOKEN_HEADER, Identity, installed_version
from . import connections, slice_testmode
from .slice_routes import EmptyBody, install_slice


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Feedback(Body):
    session_id: str
    track_id: str
    event: Literal["save", "pass", "dig_from"]


class Export(Body):
    session_id: str
    name: str = Field(min_length=1)


class PlaylistRun(Body):
    dry_run: bool = False
    name: str | None = None


class Login(Body):
    headers: str


class ChromeLogin(Body):
    profile: str | None = None


class Scan(Body):
    path: str | None = None


class Connect(Body):
    key: str | None = Field(default=None, max_length=4096)  # absent: check the key Digger already has


VALIDITY = frozenset({"connected", "rejected", "malformed"})  # check results that describe the key itself


@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"
    result: dict | None = None
    error: str | None = None
    events: list = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, name, data):
        with self.condition:
            if name == "done":
                self.state, self.result = "done", data
            elif name == "error":
                self.state, self.error = "error", data["error"]
            self.events.append((name, data))
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"id": self.id, "kind": self.kind, "state": self.state,
                    "result": self.result, "error": self.error, "events": len(self.events)}

    def stream(self):
        index = 0
        while True:
            with self.condition:
                self.condition.wait_for(lambda: index < len(self.events), timeout=15)
                batch = self.events[index:]
                index += len(batch)
            if not batch:
                yield ": keepalive\n\n"
            for name, data in batch:
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                if name in {"done", "error"}:
                    return


def _version() -> str:
    return installed_version()


# While shutting down, how often to say which jobs are still running.
SHUTDOWN_NOTE_S = 15


class _AccessLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn logs on response.start, outside the app's JSON redactor.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            record.args = (client, method, path.partition("?")[0], version, status)
        return True


def _fixture_providers(cfg, *, scheduler):
    environment = dict(os.environ)
    if not environment.get("COSINE_DIGGER_FIXTURE", "").strip():
        environment.pop("COSINE_DIGGER_FIXTURE", None)
    fixtures = slice_testmode.fixture_from_environment(host=cfg.host, scheduler=scheduler, environ=environment)
    if fixtures is not None:
        slice_testmode.require_temporary_storage(cfg.home, cfg.db_path)
    return fixtures


def create_app(cfg=None, *, scheduler=False, client_factory=None, sink_factory=None,
               discogs_client_factory=None, identity=None, announce=None, chrome=None,
               companion=None) -> FastAPI:
    """Factories accept no arguments (client) and a connection (sink).

    ``announce`` = (host, port, on_ready): once serving, write the runtime
    record that `cosine-digger start/status/stop` read (commands/serve.py).
    ``companion``: a companion.Companion lets paired iPhone/iPad apps in from other
    devices (serve --companion, or COSINE_DIGGER_COMPANION=1 for serve --reload)."""
    cfg = cfg or load_config()
    if companion is None and pairing.enabled() and getattr(identity, "launcher", None) != "start":
        companion = pairing.Companion.at(cfg.home)
    if chrome is None:  # tests pass a stand-in for Chrome's cookie store
        from .. import chrome_login as chrome
    if identity is None:
        instance = os.environ.get("COSINE_DIGGER_INSTANCE", "")  # serve --reload's worker
        identity = Identity(home=cfg.home, db_path=cfg.db_path, launcher="serve",
                            instance=instance if INSTANCE.fullmatch(instance) else None, version=_version())
    announced = threading.Event()
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _AccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(_AccessLogFilter())
    jobs: dict[str, Job] = {}
    jobs_lock = threading.Lock()
    workers: set[threading.Thread] = set()
    accepting = threading.Event()
    accepting.set()
    settings_lock = threading.Lock()
    chrome_lock = threading.Lock()  # one Log in from Chrome at a time
    dig_sends: dict[str, str] = {}  # dig id -> the job of its latest send to YouTube Music
    dig_sends_lock = threading.Lock()  # one send per dig at a time
    loop = None
    fixtures = None
    secrets = {getattr(cfg, key) for key in SECRET_FIELDS if getattr(cfg, key)}
    checked: dict[str, str] = {}  # the last check of each key in use, until that key changes
    logins = {"saved": 0}  # YouTube Music logins saved: a login check speaks only for the login it followed
    # Settings revision: grows with every change to the settings or a verdict (under settings_lock)
    # and goes with every settings answer, so the page shows answers in the order the server took
    # them, whatever order they arrive in. In microseconds, so a restarted Digger's are newer.
    revision = [time.time_ns() // 1000]

    def changed() -> None:
        """The caller holds settings_lock."""
        revision[0] = max(revision[0] + 1, time.time_ns() // 1000)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal loop, fixtures, client_factory, discogs_client_factory
        fixtures = _fixture_providers(cfg, scheduler=scheduler)
        if fixtures is not None:
            client_factory, discogs_client_factory = fixtures.cosine_client, fixtures.discogs_client
        runtime.startup()
        try:
            if scheduler:
                loop = schedule.SchedulerLoop(cfg, playlists.make_runner(cfg), cfg.db_path)
                loop.start()
            if announce is not None:
                host, port, on_ready = announce
                threading.Thread(target=identity.announce, args=(host, port, announced), kwargs={"on_ready": on_ready},
                                 name="digger-announce", daemon=True).start()
            yield
        finally:
            announced.set()
            identity.stopping()
            try:
                if loop is not None:
                    loop.stop()
                # Every running job finishes first: an export stopped between
                # removing and adding tracks would leave a half-replaced playlist.
                with jobs_lock:
                    accepting.clear()
                runtime.stop_accepting()
                # Legacy jobs first: the slice drain releases the database lock,
                # which must stay held until nothing here writes the digs.
                await asyncio.to_thread(_drain)
                await asyncio.to_thread(runtime.drain)
            finally:
                # Only now does the record go: until here a start waits instead
                # of launching a second Digger on digs this one still writes.
                identity.stopped()

    def _drain():
        with jobs_lock:
            running = list(workers)  # admission is closed, so this set only shrinks
        for thread in running:
            while thread.is_alive():
                thread.join(SHUTDOWN_NOTE_S)
                if thread.is_alive():
                    with jobs_lock:
                        left = len(workers)
                    print(f"cosine-digger: waiting for {left} running job(s) to finish before exit", flush=True)

    app = FastAPI(lifespan=lifespan)
    app.state.jobs = jobs

    def redact(value, *, identity_safe=False):
        if isinstance(value, str):
            # A provider may quote a key trimmed or escaped (h11: b'Bearer KEY\\n').
            for secret in tuple(secrets):
                for form in {secret, secret.strip(), repr(secret)[1:-1], repr(secret.strip())[1:-1]}:
                    if len(form) >= 4:
                        value = value.replace(form, "[redacted]")
            return value if identity_safe else api.scrub(value)
        if isinstance(value, dict):
            # Slice metadata and catalog parameters are recording identities.
            # Credential-shaped text is meaningful there; only free-text
            # errors/progress use heuristics, in addition to known-key removal.
            return {(redact(k, identity_safe=True) if identity_safe else k):
                    redact(v, identity_safe=identity_safe and not (
                        k in {"message", "error"} or (k in {"note", "progress"} and isinstance(v, str))))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v, identity_safe=identity_safe) for v in value]
        return value

    def hostname(value):
        try:
            parsed = urlsplit(value if "://" in value else "//" + value)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return None
            _ = parsed.port  # Reject malformed ports as well as malformed hosts.
            return parsed.hostname
        except ValueError:
            return None

    @app.middleware("http")
    async def security(request, call_next):
        # Companion mode: a device elsewhere on the network gets in only with the pairing token,
        # and only to dig, judge and listen. A wrong token is refused from anywhere.
        paired = False
        if companion is not None:
            credential = pairing.presented(request.headers)
            local = pairing.is_loopback(request.client.host if request.client else "")
            paired = companion.accepts(credential)
            if not paired and (credential is not None or not local):
                return JSONResponse({"error": "This device isn't paired with Digger. On the computer running "
                                              "Digger, run cosine-digger companion and pair again."},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            if paired and not local and not companion.allows(request.method, request.url.path):
                return JSONResponse({"error": "Open Digger on your computer for this."}, status_code=403)
        allowed = {"localhost", "127.0.0.1", "::1"}
        if cfg.host != "0.0.0.0":
            allowed.add(cfg.host.strip("[]").lower())
        allowed.update(filter(None, (hostname(h.strip()) for h in
                                     os.environ.get("COSINE_DIGGER_ALLOWED_HOSTS", "").split(",") if h.strip())))
        # The Host check stops DNS rebinding from a browser; a request carrying the pairing token
        # can't come from a rebound page, and a phone reaches Digger by its LAN name or address.
        if not paired and hostname(request.headers.get("host", "")) not in allowed:
            return JSONResponse({"error": "Host is not allowed"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and (not origin.startswith(("http://", "https://")) or hostname(origin) not in allowed):
                return JSONResponse({"error": "Origin is not allowed"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({"error": "Request failed"}, status_code=500)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Always revalidate (a cheap 304): after an update the browser must not keep old code.
            response.headers.setdefault("cache-control", "no-cache")
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": redact(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default error includes input, including pasted credentials.
        return JSONResponse({"error": "Invalid request"}, status_code=422)

    @contextmanager
    def client():
        instance = client_factory() if client_factory else api.make_client(cfg)
        try:
            yield instance
        finally:
            instance.close()

    def sink(conn):
        if fixtures is not None:
            return fixtures.youtube_sink(api.DbIdStore(conn))
        if sink_factory:
            return sink_factory(conn)

        def authenticated():
            if not cfg.ytmusic_auth_path.is_file():
                raise youtube.YouTubeAuthMissing("YouTube Music login missing")
            return youtube.YTMusic(str(cfg.ytmusic_auth_path))

        try:
            return youtube.YTMusicSink(ytm_factory=authenticated, id_store=api.DbIdStore(conn))
        except youtube.YouTubeAuthMissing:
            raise
        except Exception as exc:  # a bad login can quote its cookies: our words only
            raise youtube.YouTubeExportError(api._export_error(exc)) from None

    def start_job(kind, action):
        job = Job(uuid4().hex, kind)
        with jobs_lock:
            if not accepting.is_set():
                raise HTTPException(503, "The app is shutting down")
            jobs[job.id] = job
            # Keep the latest 50; only finished jobs are ever dropped.
            finished = [key for key, old in jobs.items() if old.state != "running"]
            for key in finished[:max(0, len(jobs) - 50)]:
                del jobs[key]

        def work():
            def emit(name, data):
                job.emit(name, redact(data))
            emit("progress", {"message": f"Starting {kind}"})
            try:
                with closing(api.open_db(cfg.db_path)) as conn:
                    result = action(conn, emit)
                result = redact(result)
                for pick in result.get("picks", result.get("tracks", [])):
                    emit("pick", pick)
                emit("done", result)
            except Exception as exc:
                # Our own errors are written for people; others keep their
                # type and (secret-redacted, truncated) message so they can be acted on.
                if isinstance(exc, (youtube.YouTubeAuthMissing, youtube.YouTubeExportError, LockTimeout)):
                    message = api._export_error(exc)
                elif isinstance(exc, playlists.PlaylistError):
                    message = str(exc)
                else:
                    message = f"{type(exc).__name__}: {api.scrub(str(exc))[:300]}"
                emit("error", {"error": message})

        def tracked():
            try:
                work()
            finally:
                with jobs_lock:
                    workers.discard(threading.current_thread())

        # Admission, registration and start happen under one lock, so shutdown
        # only ever sees started threads. Not a daemon: the process never exits
        # under a running export.
        thread = threading.Thread(target=tracked, name=f"digger-{job.id}")
        with jobs_lock:
            if not accepting.is_set():
                del jobs[job.id]
                raise HTTPException(503, "The app is shutting down")
            workers.add(thread)
            thread.start()
        return {"job_id": job.id}

    def get_job(job_id):
        with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def key_result(token, state):
        """A real cosine.club call is evidence about the key it sent: a search
        answered means connected, a 401/403 means rejected ("missing": no key was
        sent, nothing to mark). Only the key still in use is marked; a key that was
        never sent (malformed) isn't. Returns the settings revision from which the
        server holds this answer, or None when it's about a key no longer in use."""
        try:
            sendable = usable_key(token, "COSINE_API_TOKEN")
        except ValueError:
            return None
        with settings_lock:
            if cfg.cosine_token != token:
                return None
            if sendable and state in VALIDITY and checked.get("cosine_token") != state:
                checked["cosine_token"] = state
                changed()
            return revision[0]

    runtime = install_slice(app, get_config=lambda: cfg, key_result=key_result,
                            # Built from the settings the caller read its key from, so a verdict is about the key sent.
                            client_factory=lambda config: client_factory() if client_factory else api.make_client(config),
                            redact=lambda value: redact(value, identity_safe=True),
                            discogs_client_factory=lambda **kwargs: (
                                discogs_client_factory(**kwargs) if discogs_client_factory else
                                chain.make_rate_limited_client(**kwargs)))
    app.state.slice_runtime = runtime

    def running_jobs():
        with jobs_lock:
            legacy = len(workers)
        return legacy + runtime.running()

    @app.get("/api/health")
    def health():
        return {"ok": True, **identity.health(), "jobs": running_jobs()}  # its version: the one it started with

    @app.get("/api/companion")
    def companion_read():
        """The phone's pairing check: answered only to a paired device (or this computer)."""
        if companion is None:
            raise HTTPException(404, "Companion mode is off. Start Digger with cosine-digger serve --companion")
        return {"ok": True, "name": companion.name, "version": identity.version, "has_digs": _has_digs()}

    @app.post(STOP_PATH)
    def lifecycle_stop(request: Request):
        """`cosine-digger stop`: the token is in the 0600 runtime record."""
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1"} or not identity.accepts(request.headers.get(TOKEN_HEADER)):
            raise HTTPException(403, "Not allowed")
        jobs = running_jobs()
        identity.stop_soon()
        return {"ok": True, "jobs": jobs}

    @app.get("/api/profile")
    def profile_read():
        with closing(api.open_db(cfg.db_path)) as conn:
            return build_profile(conn).to_dict()

    @app.get("/api/doctor")
    def doctor(offline: bool = False):
        with settings_lock:
            config = cfg
        result = run_checks(config, offline=offline, fixture_mode=fixtures is not None)
        # Health's cosine.club check is a real call: connected or rejected is a verdict on the
        # key it sent, kept only while that key is in use (a key saved meanwhile keeps its own).
        state = result["checks"].get("cosine_token", {}).get("state")
        if state in ("connected", "rejected"):
            # key_revision: None when another key was saved during the check (it isn't about that one).
            result["checks"]["cosine_token"]["key_revision"] = key_result(config.cosine_token, state)
        return redact(result)

    @app.get("/api/settings")
    def settings_read():
        with settings_lock:
            config, verdicts, taken = cfg, dict(checked), revision[0]
        return _settings_view(config, verdicts, taken)

    def _settings_view(config, verdicts, taken):
        """Settings as the page reads them, from one snapshot of the settings, the key verdicts and
        the revision (taken under settings_lock), so a key is never described with another key's verdict."""
        result = {"revision": taken}
        result |= {k: getattr(config, k) for k in (
            "daily_time", "weekly_day", "weekly_time", "daily_size", "weekly_size")}
        result["music_dir"] = str(config.music_dir) if config.music_dir else None
        result.update({key: {"set": bool(getattr(config, key)),
                             "hint": "…" + getattr(config, key)[-4:] if getattr(config, key) else "",
                             "source": config.sources.get(key, "default"), "env": ENV_VARS[key],
                             # malformed: set, but with a space or line break, so never sent (P9, P15)
                             "malformed": bool(getattr(config, key)) and connections.check(key, getattr(config, key),
                                                                                            offline=True).state == "malformed",
                             "checked": verdicts.get(key)}
                       for key in SECRET_FIELDS})
        # saved: the file holds a login (an empty {} doesn't); checked: the last check, until the next login.
        saved = youtube.has_login(config.ytmusic_auth_path)
        result["youtube"] = {"logged_in": saved, "saved": saved, "checked": verdicts.get("youtube_login") if saved else None,
                             "chrome": chrome.available()}
        # Setup › About names the same folders `cosine-digger status` prints.
        result["data"] = {"home": str(config.home), "db": str(Path(config.db_path).expanduser().resolve())}
        result["has_digs"] = _has_digs()
        return result

    def _has_digs():
        # The key screen gates only a home with no key and no saved digs (A4):
        # False only when the digs are known to be empty; None (unknown) never gates.
        path = Path(cfg.db_path).expanduser().resolve()
        if not path.exists():
            return False
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='digs'").fetchone():
                    return False
                return conn.execute("SELECT EXISTS (SELECT 1 FROM digs)").fetchone()[0] == 1
        except (sqlite3.Error, OSError):
            return None

    @app.put("/api/settings")
    def settings_write(updates: dict):
        valid =SECRET_FIELDS | {"music_dir", "daily_time", "weekly_day", "weekly_time", "daily_size", "weekly_size"}
        if updates.keys() - valid:
            raise HTTPException(400, "Unknown settings keys")
        for key, value in updates.items():
            if key in SECRET_FIELDS and not isinstance(value, str):
                raise HTTPException(400, "Credentials must be strings")
            if key in SECRET_FIELDS:
                try:
                    usable_key(value, key)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from None
                if value.strip():
                    # A5/A7: a key is saved only after its provider accepts it, never over an environment key.
                    raise HTTPException(400, f"Keys are checked before they're saved: POST /api/connections/{key}")
            if key in {"daily_time", "weekly_time"} and (not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)):
                raise HTTPException(400, "Times must use HH:MM")
            if key == "weekly_day" and value not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
                raise HTTPException(400, "Invalid weekday")
            if key in {"daily_size", "weekly_size"} and (type(value) is not int or not 5 <= value <= 100):
                raise HTTPException(400, "Sizes must be integers from 5 to 100")
            if key == "music_dir" and value is not None:
                if not isinstance(value, str) or not Path(value).expanduser().is_dir():
                    raise HTTPException(400, "Music directory must exist")
        updates = {k: v for k, v in updates.items() if k not in SECRET_FIELDS}  # empty keys: keep the saved one
        with settings_lock:
            _apply(updates)
        return settings_read()

    def _apply(updates):
        """Save settings and use them now. The caller holds settings_lock."""
        nonlocal cfg
        save_settings(updates, cfg.home)
        secrets.update(v for k, v in updates.items() if k in SECRET_FIELDS and v)
        effective = {k: v for k, v in updates.items() if cfg.sources.get(k) != "env"}
        if "music_dir" in effective:
            effective["music_dir"] = Path(effective["music_dir"]).expanduser() if effective["music_dir"] else None
        cfg = replace(cfg, **effective, sources={**cfg.sources, **{k: "settings" for k in effective}})
        for key in SECRET_FIELDS & effective.keys():
            checked.pop(key, None)
        changed()
        # Code that still reads keys from the environment (Discogs) must see the new ones.
        for key in SECRET_FIELDS & effective.keys():
            os.environ[ENV_VARS[key]] = effective[key]
        if loop is not None:
            loop.cfg, loop.runner = cfg, playlists.make_runner(cfg)

    @app.post("/api/connections/{name}")
    def connection_check(name: Literal["cosine_token", "discogs_token", "exa_api_key"], body: Connect):
        """Check a key before anything says it works (A5): only a key the provider
        accepts is saved; a refused key, a busy provider or no answer saves nothing.
        No key, or the key already in use: check that key, save nothing; if another key
        was saved meanwhile, that check comes back "superseded". A key from the
        environment can't be replaced here (A7)."""
        current = getattr(cfg, name)
        if body.key is not None and cfg.sources.get(name) == "env":
            return {"name": name, "state": "env", "retry_after_s": None, "reason": None, "settings": settings_read()}
        typed = body.key is not None and body.key.strip() != (current or "").strip()
        result = connections.check(name, body.key if typed else current, offline=fixtures is not None)
        reason = None
        with settings_lock:
            if getattr(cfg, name) != current:
                # Another key was saved while this one was checked: never save over it, and a check of
                # the key that was in use says nothing about the key in use now.
                if typed and result.state == "connected":
                    result, reason = connections.Check("save_failed"), "another key was saved while this one was checked"
                elif not typed:
                    result = connections.Check("superseded")
            elif typed and result.state == "connected":
                try:
                    _apply({name: body.key.strip()})
                except OSError as exc:
                    result, reason = connections.Check("save_failed"), exc.strerror or type(exc).__name__
                else:
                    checked[name] = "connected"
                    changed()
            elif not typed and result.state in VALIDITY and checked.get(name) != result.state:
                # Only a verdict on the key itself sticks; busy, server errors and no answer don't.
                checked[name] = result.state
                changed()
            config, verdicts, taken = cfg, dict(checked), revision[0]
        return {"name": name, "state": result.state, "retry_after_s": result.retry_after_s, "reason": reason,
                "settings": _settings_view(config, verdicts, taken)}

    @app.get("/api/playlists")
    def playlists_read():
        with closing(api.open_db(cfg.db_path)) as conn:
            result = {}
            for kind, name in playlists.DEFAULT_NAMES.items():
                row = conn.execute("SELECT status, finished_at, detail FROM job_runs WHERE job = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
                last = dict(zip(("status", "finished_at", "detail"), row)) if row else None
                playlist_id = api.DbIdStore(conn).get(name)
                # Under start nothing inside Digger runs the feeds on a clock, so
                # there is no next run to show (P14); schedule install runs outside.
                scheduled = loop is not None
                result[kind] = {"name": name, "playlist_url": f"https://music.youtube.com/playlist?list={playlist_id}" if playlist_id else None,
                                "last_run": redact(last), "scheduled": scheduled,
                                "next_run": schedule.next_run(kind, cfg, datetime.now().astimezone()).isoformat()
                                if scheduled else None}
            return result

    @app.get("/api/sessions")
    def sessions_read(limit: int = Query(20, ge=1, le=100)):
        with closing(api.open_db(cfg.db_path)) as conn:
            return [{**{k: row[k] for k in ("id", "name", "seeds", "created_at")},
                     "result_count": row["total_results"]} for row in session.list_sessions(conn)[:limit]]

    @app.get("/api/jobs/{job_id}")
    def job_read(job_id: str):
        if runtime.owns(job_id):
            return runtime.job_snapshot(job_id)
        return get_job(job_id).snapshot()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, request: Request, after: str = "0"):
        if runtime.owns(job_id):
            if not after.isascii() or not after.isdecimal() or len(after) > 20:
                raise HTTPException(400, "Invalid event cursor")
            return runtime.job_events(job_id, after=int(after), last_event_id=request.headers.get("last-event-id"))
        return StreamingResponse(get_job(job_id).stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/feedback")
    def feedback(body: Feedback):
        with closing(api.open_db(cfg.db_path)) as conn:
            row = conn.execute("SELECT id FROM session_results WHERE session_id = ? AND track_id = ? ORDER BY id DESC LIMIT 1",
                               (body.session_id, body.track_id)).fetchone()
            if row is None:
                raise HTTPException(404, "Result not found")
            record_event(conn, row[0], body.event)
        return {"ok": True}

    @app.post("/api/export")
    def export(body: Export):
        with closing(api.open_db(cfg.db_path)) as conn:
            if not session.get_session_results(conn, body.session_id):
                raise HTTPException(404, "Session results not found")

        def action(conn, emit):
            rows = conn.execute("SELECT track_id, artist, track_name, video_id, score, via, hop FROM session_results WHERE session_id = ? ORDER BY hop, score DESC",
                                (body.session_id,)).fetchall()
            picks = [api.Pick(Track(id=r[0], name=r[2], artist=r[1] or "", track=r[2], video_id=r[3]),
                              r[4] or 0, "From your dig", via=r[5] or "", hop=r[6]) for r in rows]
            report = api.export(picks, body.name, sink=sink(conn), conn=conn)
            return {"session_id": body.session_id, "playlist_url": report.playlist_url, "export": report.to_dict()}
        return start_job("playlist", action)

    @app.post("/api/digs/{dig_id}/export")
    def dig_export(dig_id: str, body: EmptyBody):
        with closing(api.open_db(cfg.db_path)) as conn:
            if conn.execute("SELECT 1 FROM digs WHERE id = ?", (dig_id,)).fetchone() is None:
                raise HTTPException(404, "Dig not found")
        with dig_sends_lock:
            with jobs_lock:
                going = jobs.get(dig_sends.get(dig_id, ""))
            if going is not None and going.state == "running":
                return {"job_id": going.id}  # a second press, tab or reload follows the send already going

            def action(conn, emit):
                with settings_lock:
                    login = logins["saved"]
                try:
                    result = api.export_dig(conn, dig_id, sink=sink(conn))
                except youtube.YouTubeExportError as exc:
                    if youtube.SIGNED_OUT in str(exc):
                        # YouTube refused the saved login: the settings say so, as a check of it
                        # would (unless a login was saved meanwhile), so every client offers to
                        # log in again rather than to send again.
                        with settings_lock:
                            if logins["saved"] == login:
                                checked["youtube_login"] = "rejected"
                                changed()
                    raise
                account = youtube.login_account(cfg.ytmusic_auth_path)
                return {**result, "playlist_url": youtube.account_link(result["playlist_url"], account)}
            started = start_job("playlist", action)
            dig_sends[dig_id] = started["job_id"]
            return started

    @app.post("/api/playlists/{kind}/run")
    def playlist_run(kind: Literal["daily", "weekly"], body: PlaylistRun):
        def action(conn, emit):
            with client() as cosine, (fixtures.discogs_client(budget=60)[0]
                                     if fixtures is not None else nullcontext()) as dg:
                return playlists.run_playlist(kind, cfg=replace(cfg, exa_api_key=None) if fixtures else cfg,
                                              conn=conn, client=cosine, discogs_client=dg,
                                              sink=None if body.dry_run else sink(conn),
                                              name=body.name, dry_run=body.dry_run, log_run=True,
                                              notify=lambda message: emit("progress", {"message": api.plain(message)}))
        return start_job("playlist", action)

    @app.post("/api/youtube/login")
    def login(body: Login):
        return save_login(body.headers)

    @app.post("/api/youtube/login/chrome")
    def login_from_chrome(body: ChromeLogin):
        # Blocks while macOS asks to share Chrome's cookie key (up to two minutes), so one
        # at a time: a second click answers at once instead of holding another worker.
        if not chrome_lock.acquire(blocking=False):
            raise HTTPException(409, "Already waiting for macOS. Answer its prompt about Chrome Safe Storage.")
        try:
            # Chrome can be signed in to several Google accounts: logging in again keeps the
            # one the saved login acts as (the first, only when there's none).
            headers = chrome.youtube_headers(body.profile, authuser=youtube.login_account(cfg.ytmusic_auth_path))
        except chrome.ChromeLoginError as exc:  # our own wording, never cookie values
            raise HTTPException(400, str(exc)) from None
        finally:
            chrome_lock.release()
        return save_login(headers)

    def save_login(headers: str):
        with settings_lock:
            try:
                youtube.login(headers, cfg.ytmusic_auth_path)
            except Exception:
                raise HTTPException(400, "Could not save YouTube Music login; check the browser headers") from None
            checked.pop("youtube_login", None)
            changed()
            logins["saved"] += 1
            saved = logins["saved"]
        # Connected only after the read-only library check `youtube status` runs (P17);
        # test mode never calls YouTube, so its login stays "saved".
        state = "saved" if fixtures is not None else youtube.check_login(cfg.ytmusic_auth_path)
        with settings_lock:
            # A login saved meanwhile (another tab) keeps its own verdict: this check may have read either.
            if state != "saved" and logins["saved"] == saved:
                checked["youtube_login"] = state
                changed()
            config, verdicts, taken = cfg, dict(checked), revision[0]
        return {"ok": True, "logged_in": youtube.has_login(config.ytmusic_auth_path), "state": state,
                "settings": _settings_view(config, verdicts, taken)}

    @app.post("/api/library/scan")
    def scan(body: Scan):
        path = Path(body.path).expanduser() if body.path else cfg.music_dir
        if path is None or not path.is_dir():
            raise HTTPException(400, "Music directory must exist")

        def action(conn, emit):
            from ..taste import incremental_scan
            added, total = incremental_scan(path, cfg.db_path, read_tags=True,
                                            on_progress=lambda count: emit("progress", {"message": f"Scanned {count} new files"}))
            return {"added": added, "total": total}
        return start_job("scan", action)

    static = Path(__file__).parent / "static"

    versions: dict[str, tuple[int, int, str]] = {}  # file -> (mtime_ns, size, content digest)

    def versioned(name: str) -> str:
        path, stat = static / name, (static / name).stat()
        known = versions.get(name)
        if known is None or known[:2] != (stat.st_mtime_ns, stat.st_size):
            known = versions[name] = (stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
        return f"/static/{name}?v={known[2]}"

    @app.get("/", response_class=HTMLResponse)
    def index():
        if (static / "index.html").is_file():
            # Each code file's URL names its content, so an update is never shadowed by the browser's
            # cache (it keeps dynamically imported modules even through a hard reload): an import map
            # for every module but slice.js, the page's own script, plus versioned stylesheets.
            html = (static / "index.html").read_text(encoding="utf-8")
            modules = sorted(p.relative_to(static).as_posix() for p in static.rglob("*.js") if p.name != "slice.js")
            importmap = json.dumps({"imports": {f"/static/{name}": versioned(name) for name in modules}})
            html = re.sub(r'"/static/([\w/.-]+\.css)"', lambda match: f'"{versioned(match.group(1))}"', html)
            script = '<script type="module" src="/static/slice.js">'
            return html.replace(script, f'<script type="importmap">{importmap}</script>\n  {script}', 1)
        return "<!doctype html><html><head><title>cosine-digger</title></head><body><p>The UI files are missing.</p></body></html>"

    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")
    else:
        @app.get("/static/{path:path}")
        def missing_static(path: str):
            raise HTTPException(404, "Static file not found")
    return app
