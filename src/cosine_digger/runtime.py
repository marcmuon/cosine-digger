"""Who owns a running Digger: its private runtime record, process identity
checks, the start lock, port choice and the background server's logs.

A server writes its record (``run/server-<db key>.json``, 0600, in a 0700
folder under the app home) once its own /api/health answers with its instance
id and pid. The record says "stopping" from the moment shutdown begins and is
removed only after every job has drained, so it covers the whole life of the
process that holds the digs. Nothing here signals another process: `stop`
proves the record still names a live Digger (same PID, same process start
time, the instance on its command line, and the same identity answering on
loopback) and then asks that Digger, with the record's private token, to stop
itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
import hashlib
import http.client
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - no start/stop support on Windows
    fcntl = None

APP_ID = "cosine-digger"
DEFAULT_PORT = 8765
LOOPBACK = "127.0.0.1"
LAUNCHER_FILE = "launcher.json"
SERVER_LOG = "digger.log"
START_LOG = "start.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
LIFECYCLE_COMMANDS = frozenset({"start", "status", "stop"})
STOP_PATH = "/api/lifecycle/stop"
TOKEN_HEADER = "X-Digger-Token"
INSTANCE = re.compile(r"[0-9a-f]{32}")

log = logging.getLogger(__name__)


def new_instance() -> str:
    return secrets.token_hex(16)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def run_dir(home: Path) -> Path:
    return _private_dir(_private_dir(Path(home).expanduser()) / "run")


def log_dir(home: Path) -> Path:
    return _private_dir(_private_dir(Path(home).expanduser()) / "logs")


def url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def probe_host(host: str) -> str:
    """Where this machine reaches a server bound to ``host``."""
    return LOOPBACK if host in {"0.0.0.0", "", "::"} else host.strip("[]")


def resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def data_key(db_path: Path) -> str:
    """Which digs a Digger serves (16 hex digits of its database's path); its record is named by it."""
    return hashlib.sha1(str(resolved(db_path)).encode()).hexdigest()[:16]


def record_path(home: Path, db_path: Path) -> Path:
    return Path(home).expanduser() / "run" / f"server-{data_key(db_path)}.json"


def since(started_at: str, now: datetime | None = None) -> str:
    """14:00 today, else Oct 5, 14:00."""
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return "an unknown time"
    now = now or datetime.now(started.tzinfo)
    if started.date() == now.date():
        return started.strftime("%H:%M")
    return f"{started.strftime('%b')} {started.day}, {started.strftime('%H:%M')}"


@dataclass(frozen=True)
class Record:
    pid: int
    process_start: str
    instance: str
    launcher: str  # "start" or "serve"
    host: str
    port: int
    version: str
    started_at: str  # ISO 8601 with offset
    home: str
    db: str
    token: str
    state: str = "running"  # "running" or "stopping"

    @property
    def url(self) -> str:
        return f"http://{url_host(self.host)}:{self.port}"

    @property
    def since(self) -> str:
        return since(self.started_at)


def _valid(record: Record) -> bool:
    texts = ("process_start", "instance", "launcher", "host", "version", "started_at", "home", "db", "token", "state")
    return (type(record.pid) is int and record.pid > 0 and type(record.port) is int and 0 < record.port < 65536
            and all(isinstance(getattr(record, key), str) and getattr(record, key) for key in texts)
            and record.launcher in {"start", "serve"} and record.state in {"running", "stopping"})


def _atomic_json(path: Path, payload: dict) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, prefix=".tmp-") as handle:
            temp = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


@contextmanager
def _flock(path: Path, timeout: float, message: str):
    """An exclusive advisory lock on ``path``. The file is never removed, so
    every process always locks the same inode."""
    with open(path, "a+") as handle:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(message) from None
                    time.sleep(0.05)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_lock(home: Path):
    """Serialises every read-check-write of the records in one app home."""
    return _flock(run_dir(home) / "record.lock", 30, "Another Digger is updating its runtime record.")


def start_lock(home: Path, timeout: float = 90):
    """Spans start's discovery, launch and readiness (and stop's request), so
    concurrent starts take turns and the second finds the first."""
    return _flock(run_dir(home) / "start.lock", timeout, "Another cosine-digger start or stop is still running.")


def read_record(home: Path, db_path: Path) -> Record | None:
    """The record of the Digger for this home and database. A record that names
    another home or database (copied or moved here) is never trusted."""
    try:
        data = json.loads(record_path(home, db_path).read_text())
        record = Record(**{item.name: data[item.name] for item in fields(Record) if item.name in data})
        if not _valid(record):
            return None
        if resolved(Path(record.home)) != resolved(home) or resolved(Path(record.db)) != resolved(db_path):
            return None
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return None
    return record


def write_record(record: Record) -> None:
    home = Path(record.home)
    run_dir(home)
    _atomic_json(record_path(home, Path(record.db)), asdict(record))


def discard_record(record: Record) -> None:
    """Remove ``record`` if the file still names the same instance."""
    home = Path(record.home)
    with record_lock(home):
        current = read_record(home, Path(record.db))
        if current is not None and current.instance == record.instance:
            record_path(home, Path(record.db)).unlink(missing_ok=True)


def read_last_port(home: Path) -> int | None:
    try:
        port = json.loads((Path(home).expanduser() / "run" / LAUNCHER_FILE).read_text()).get("port")
    except (OSError, ValueError, AttributeError):
        return None
    return port if type(port) is int and 0 < port < 65536 else None


def read_last_scheduler(home: Path) -> bool:
    """Whether the last start ran the Daily/Weekly scheduler (off unless asked)."""
    try:
        return json.loads((Path(home).expanduser() / "run" / LAUNCHER_FILE).read_text()).get("scheduler") is True
    except (OSError, ValueError, AttributeError):
        return False


def write_last_port(home: Path, port: int, scheduler: bool = False) -> None:
    _atomic_json(run_dir(home) / LAUNCHER_FILE, {"port": port, "scheduler": scheduler})


def installed_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("cosine-digger")
    except PackageNotFoundError:
        return "unknown"


def choose_port(cfg, requested: int | None) -> int:
    """--port, else the configured port (COSINE_DIGGER_PORT, then
    settings.json), else the port start last used, else 8765."""
    if requested is not None:
        return requested
    if cfg.sources.get("port") in {"env", "settings"}:
        return cfg.port
    return read_last_port(cfg.home) or DEFAULT_PORT


def _ps(*args: str) -> str | None:
    ps = "/bin/ps" if Path("/bin/ps").exists() else "ps"
    try:
        # TZ: ps prints lstart in local time, so a laptop that changes time zone (or a DST
        # change) would read another start time for the same process.
        result = subprocess.run([ps, *args], capture_output=True, text=True, timeout=5,
                                env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "TZ": "UTC0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def process_start(pid: int) -> str | None:
    """A value that differs between any two processes that held this PID."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            starttime = stat[stat.rindex(")") + 2:].split()[19]
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except (OSError, ValueError, IndexError):
            return None
        return f"{boot}:{starttime}"
    value = " ".join((_ps("-o", "lstart=", "-p", str(pid)) or "").split())
    return value or None


def process_command(pid: int) -> str:
    if pid <= 0:
        return ""
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    return (_ps("-ww", "-o", "command=", "-p", str(pid)) or "").strip()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_state(record: Record) -> str:
    """"same" while the recorded process runs; "gone" once it exited or its PID
    belongs to another process; "unknown" while it is alive but its start time
    can't be read. Nothing is discarded, overwritten or reported as stopped on
    "unknown"."""
    if not pid_alive(record.pid):
        return "gone"
    started = process_start(record.pid)
    if started is None:
        return "unknown" if pid_alive(record.pid) else "gone"
    if started == record.process_start:
        return "same"
    # A start time that reads differently (a record written before TZ was pinned) doesn't settle
    # it for a server start launched: its command line carries the record's 128-bit instance id.
    return "same" if launched_by_start(record) else "gone"


def same_process(record: Record) -> bool:
    return process_state(record) == "same"


def health(host: str, port: int, timeout: float = 2.0) -> tuple[str, dict | None]:
    """("answer", body) for any HTTP answer, else ("refused" | "timeout" | "error", None)."""
    url = f"http://{url_host(probe_host(host))}:{port}/api/health"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url, headers={"Accept": "application/json"})
    except httpx.ConnectError:
        return "refused", None
    except httpx.TimeoutException:
        return "timeout", None
    except httpx.HTTPError:
        return "error", None
    try:
        body = response.json() if response.is_success else {}
    except ValueError:
        body = {}
    return "answer", body if isinstance(body, dict) else {}


def answers_as(body: dict | None, *, instance: str, pid: int | None) -> bool:
    """This exact Digger answered (any pid when ``pid`` is None)."""
    return (bool(body) and body.get("app") == APP_ID and body.get("instance") == instance
            and (pid is None or body.get("pid") == pid))


def wait_for_self(host: str, port: int, *, instance: str, pid: int | None, stop: threading.Event,
                  timeout: float) -> bool:
    """Poll until /api/health answers as this Digger; False on stop or timeout."""
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        kind, body = health(host, port, timeout=1.0)
        if kind == "answer" and answers_as(body, instance=instance, pid=pid):
            return True
        if time.monotonic() >= deadline:
            return False
        stop.wait(0.1)
    return False


def serves_these_digs(body: dict | None, db_path: Path, host: str, port: int) -> bool:
    """Asked while a Digger holds these digs without a record: did that Digger answer? One that
    names itself must be Digger and name these digs. An older one says only ok and its version (a
    shape other programs use too), so it counts only when its own offline check names these digs'
    database by its full path, as Digger 0.4's serve does."""
    if not body:
        return False
    if "app" in body:
        return body["app"] == APP_ID and body.get("data") == data_key(db_path)
    return (body.get("ok") is True and isinstance(body.get("version"), str)
            and _older_digger_db(host, port) == resolved(db_path))


def _older_digger_db(host: str, port: int, timeout: float = 3.0) -> Path | None:
    """The database an older Digger's offline check names (its /api/doctor config), when it names
    one by its full path; that check never calls cosine.club."""
    url = f"http://{url_host(probe_host(host))}:{port}/api/doctor"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url, params={"offline": "true"}, headers={"Accept": "application/json"})
        body = response.json() if response.is_success else None
    except (httpx.HTTPError, ValueError):
        return None
    config = body.get("config") if isinstance(body, dict) else None
    named = config.get("db_path") if isinstance(config, dict) else None
    if not isinstance(named, str) or not Path(named).is_absolute():
        return None
    try:
        return resolved(Path(named))
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class Check:
    state: str  # "running" | "stopping" | "unresponsive" | "stale" | "mismatch"
    record: Record
    health: dict | None = None

    @property
    def alive(self) -> bool:
        """The recorded process still exists, whatever it answers."""
        return self.state != "stale"

    @property
    def jobs(self) -> int:
        value = (self.health or {}).get("jobs")
        return value if type(value) is int and value >= 0 else 0


def check(record: Record) -> Check:
    """Is the recorded Digger still the process that wrote the record?"""
    process = process_state(record)
    if process == "gone":
        return Check("stale", record)
    kind, body = health(record.host, record.port)
    if kind == "answer" and answers_as(body, instance=record.instance, pid=record.pid):
        stopping = record.state == "stopping" or body.get("stopping") is True
        return Check("stopping" if stopping else "running", record, body)
    if kind == "answer" and process == "same":
        return Check("mismatch", record, body)
    return Check("stopping" if record.state == "stopping" else "unresponsive", record)


def launched_by_start(record: Record) -> bool:
    """Only a server `start` launched is stopped by `stop`, and only while its
    command line still carries the record's instance id."""
    return record.launcher == "start" and f"--owned-by-start {record.instance}" in process_command(record.pid)


def request_stop(record: Record) -> tuple[bool, int]:
    """Ask the verified Digger to stop itself: (accepted, running jobs).

    The health check and the stop request share one TCP connection that is never
    reopened, so the token only reaches the process that has just answered as
    this Digger, never a program that took the port in between."""
    connection = http.client.HTTPConnection(probe_host(record.host), record.port, timeout=5)
    try:
        connection.request("GET", "/api/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read() or b"null") if response.status == 200 else None
        if not answers_as(body if isinstance(body, dict) else None, instance=record.instance, pid=record.pid):
            return False, 0
        connection.auto_open = 0  # a closed connection raises instead of reconnecting
        connection.request("POST", STOP_PATH, body=b"{}",
                           headers={TOKEN_HEADER: record.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read() or b"null") if response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        return False, 0
    finally:
        connection.close()
    if not isinstance(body, dict):
        return False, 0
    jobs = body.get("jobs")
    return body.get("ok") is True, jobs if type(jobs) is int and jobs >= 0 else 0


def port_busy(host: str, port: int) -> bool:
    """Something listens there, or something stops us binding it."""
    try:
        with socket.create_connection((probe_host(host), port), timeout=0.5):
            return True
    except OSError:
        pass
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def free_port_after(host: str, port: int) -> int:
    for candidate in range(port + 1, min(port + 50, 65535) + 1):
        if not port_busy(host, candidate):
            return candidate
    return min(port + 1, 65535)


def database_lock_path(db_path: Path) -> Path:
    """The slice server's lock for this database (SliceRuntime.startup)."""
    db = resolved(db_path)
    return db.parent / "locks" / (hashlib.sha1(f"slice-server:{db}".encode()).hexdigest()[:16] + ".lock")


def database_in_use(db_path: Path) -> bool:
    """True while a running Digger holds this database. Creates nothing; the
    shared probe lasts microseconds and a starting server retries for 2 s."""
    path = database_lock_path(db_path)
    if fcntl is None or not path.is_file():
        return False
    try:
        handle = open(path, "r")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


# --- the background server's logs -------------------------------------------

_LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} [A-Z]+ [\w.]+: ")
_WRAP_UP = ("Application startup failed. Exiting.", "Waiting for application shutdown.",
            "Application shutdown complete.", "Finished server process", "Shutting down",
            "Waiting for background tasks to complete.", "Waiting for connections to close.")


class _QuietHealth(logging.Filter):
    """Open tabs poll /api/health every 5 s; don't log each poll."""

    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) == 5 and args[1] == "GET" and str(args[2]).partition("?")[0] == "/api/health")


def log_config(path: Path) -> dict:
    """Uvicorn's and the app's logging in one rotating file (4 x 1 MB at most)."""
    handler = {"class": "logging.handlers.RotatingFileHandler", "filename": str(path),
               "maxBytes": LOG_MAX_BYTES, "backupCount": LOG_BACKUPS, "encoding": "utf-8"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"quiet_health": {"()": _QuietHealth}},
        "formatters": {
            "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
            "access": {"()": "uvicorn.logging.AccessFormatter", "use_colors": False,
                       "fmt": '%(asctime)s INFO access: %(client_addr)s "%(request_line)s" %(status_code)s'},
        },
        "handlers": {
            "file": {**handler, "formatter": "plain"},
            "access": {**handler, "formatter": "access", "filters": ["quiet_health"]},
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            # Request lines would put search terms and provider URLs in the log.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
        "root": {"handlers": ["file"], "level": "INFO"},
    }


class LogStream(io.TextIOBase):
    """sys.stdout or sys.stderr for a background Digger: each line becomes a
    log record, so the rotating log is the only place output grows."""

    def __init__(self, logger: logging.Logger, level: int):
        super().__init__()
        self._logger, self._level, self._buffer = logger, level, ""
        self._lock = threading.Lock()

    def writable(self):
        return True

    def write(self, text):
        with self._lock:
            *lines, self._buffer = (self._buffer + text).split("\n")
        for line in lines:
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        with self._lock:
            line, self._buffer = self._buffer, ""
        if line.strip():
            self._logger.log(self._level, line.rstrip())


def detach_output() -> None:
    """Python output goes to the rotating log from now on."""
    logging.raiseExceptions = False  # a full disk must never loop through stderr
    logging.captureWarnings(True)
    sys.stdout = LogStream(logging.getLogger("cosine_digger.stdout"), logging.INFO)
    sys.stderr = LogStream(logging.getLogger("cosine_digger.stderr"), logging.WARNING)


def silence_startup_output() -> None:
    """Once ready, stop writing to start.log: it only ever holds startup."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    for fd in (1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    os.close(devnull)


def last_log_line(paths: list[Path], *, since_time: float, hide: tuple[str, ...] = ()) -> tuple[str, Path]:
    """The most telling line of the newest log written since ``since_time``:
    the last line that isn't uvicorn's wrap-up, scrubbed of keys."""
    from .config import scrub

    written = [p for p in paths if p.is_file() and p.stat().st_size and p.stat().st_mtime >= since_time - 1]
    if not written:
        return "(nothing was logged)", paths[-1]
    path = max(written, key=lambda p: p.stat().st_mtime)
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "(the log couldn't be read)", path
    chosen = ""
    for raw in reversed(lines):
        line = _LOG_PREFIX.sub("", raw).strip()
        if line and not line.startswith(_WRAP_UP):
            chosen = line
            break
    for value in hide:
        if value and len(value) >= 4:
            chosen = chosen.replace(value, "[redacted]")
    return scrub(chosen)[:300] or "(nothing was logged)", path


# --- the server's own identity ----------------------------------------------

class Identity:
    """What a running server says about itself, and its runtime record."""

    def __init__(self, *, home: Path, db_path: Path, launcher: str, instance: str | None = None,
                 version: str = "unknown"):
        self.home = Path(home).expanduser()
        self.db = resolved(db_path)
        self.launcher = launcher
        self.instance = instance or new_instance()
        self.version = version
        self.pid = os.getpid()
        self.token = secrets.token_urlsafe(32)
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.record: Record | None = None
        self.ready = threading.Event()
        self.closing = threading.Event()
        self._lock = threading.Lock()

    def health(self) -> dict:
        # version: the one this process started with (an upgrade replaces the files, not this process).
        return {"app": APP_ID, "instance": self.instance, "pid": self.pid, "launcher": self.launcher,
                "started_at": self.started_at, "stopping": self.closing.is_set(), "version": self.version,
                "data": data_key(self.db)}

    def announce(self, host: str, port: int, stop: threading.Event, *, timeout: float = 60, on_ready=None) -> None:
        """Write the record once this process's own /api/health answers. The
        record of another live Digger on the same digs (one still draining
        after a stop) is never overwritten: wait until it has gone."""
        if not wait_for_self(host, port, instance=self.instance, pid=self.pid, stop=stop, timeout=timeout):
            if not stop.is_set():
                log.warning("This Digger's own /api/health never answered at %s:%s; "
                            "cosine-digger status won't find it.", host, port)
            return
        started = process_start(self.pid)
        if started is None:
            log.warning("Couldn't read this process's start time; cosine-digger status won't find it.")
            return
        while not stop.is_set():
            with self._lock:
                if self.closing.is_set():
                    return
                try:
                    with record_lock(self.home):
                        current = read_record(self.home, self.db)
                        if current is None or current.instance == self.instance or process_state(current) == "gone":
                            self.record = Record(
                                pid=self.pid, process_start=started, instance=self.instance,
                                launcher=self.launcher, host=probe_host(host), port=port, version=self.version,
                                started_at=self.started_at, home=str(self.home), db=str(self.db), token=self.token)
                            write_record(self.record)
                except (OSError, TimeoutError) as exc:
                    log.error("Couldn't write the runtime record in %s: %s", self.home / "run", exc)
                    return
            if self.record is not None:
                self.ready.set()
                if on_ready is not None:
                    on_ready()
                return
            stop.wait(0.5)

    def stopping(self) -> None:
        """Shutdown has begun: say so in the record (kept until stopped())."""
        self.closing.set()
        with self._lock:
            if self.record is None or self.record.state == "stopping":
                return
            try:
                with record_lock(self.home):
                    current = read_record(self.home, self.db)
                    if current is not None and current.instance == self.instance:
                        self.record = replace(self.record, state="stopping")
                        write_record(self.record)
            except (OSError, TimeoutError) as exc:
                log.error("Couldn't update the runtime record: %s", exc)

    def stopped(self) -> None:
        """Every job has drained: remove the record if it is still ours."""
        self.closing.set()
        with self._lock:
            if self.record is None:
                return
            try:
                with record_lock(self.home):
                    current = read_record(self.home, self.db)
                    if current is not None and current.instance == self.instance:
                        record_path(self.home, self.db).unlink(missing_ok=True)
            except (OSError, TimeoutError) as exc:
                log.error("Couldn't remove the runtime record: %s", exc)
            self.record = None

    def accepts(self, token: str | None) -> bool:
        return (self.launcher == "start" and bool(token) and token.isascii()
                and secrets.compare_digest(token, self.token))

    def stop_soon(self) -> None:
        """Stop this process the way a SIGTERM would (uvicorn closes HTTP, then
        the app's lifespan drains every job), after the reply has gone out."""
        self.stopping()
        threading.Timer(0.2, os.kill, (self.pid, signal.SIGTERM)).start()
