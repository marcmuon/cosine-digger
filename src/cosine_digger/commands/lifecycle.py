"""cosine-digger start, status and stop: run Digger in the background, check
on it and stop it. The ownership rules live in runtime.py."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import typer

from .. import runtime
from ..config import SECRET_FIELDS, AppConfig, load_config
from .serve import open_browser

READY_TIMEOUT_S = 60
STARTING_WAIT_S = 15
STOP_WAIT_S = 15
HELP = ("Digger keeps running after you close this window.\n"
        "  cosine-digger stop     stop it\n"
        "  cosine-digger status   check on it")
NO_BROWSER = "Couldn't open your browser. Open that address yourself."


def register(app: typer.Typer) -> None:
    app.command(name="start")(start)
    app.command(name="status")(status)
    app.command(name="stop")(stop)


def _cfg(ctx: typer.Context) -> AppConfig:
    return ctx.obj if isinstance(ctx.obj, AppConfig) else load_config(os.environ)


def _say(*lines: str) -> None:
    for line in lines:
        typer.echo(line)


def _jobs(count: int) -> str:
    return f"{count} running {'job' if count == 1 else 'jobs'}"


def _wait_gone(record: runtime.Record) -> None:
    """Until the recorded process has exited (its PID gone or reused). A start
    time that can't be read keeps the wait going: that is not an exit."""
    while runtime.process_state(record) != "gone":
        time.sleep(0.2)


def start(
    ctx: typer.Context,
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535,
        help="Port to use (default: COSINE_DIGGER_PORT, then settings.json, then the port start last used, then 8765)"),
    scheduler: bool | None = typer.Option(
        None, "--scheduler/--no-scheduler",
        help="Refresh Daily and Weekly on their schedule while Digger runs (default: as the last start, else off)"),
) -> None:
    """Start Digger in the background and open it in your browser."""
    cfg = _cfg(ctx)
    chosen = runtime.choose_port(cfg, port)
    scheduled = scheduler if scheduler is not None else runtime.read_last_scheduler(cfg.home)
    try:
        with runtime.start_lock(cfg.home):
            code = _start(cfg, chosen, scheduled, asked=scheduler is not None)
    except TimeoutError as exc:
        _say(str(exc))
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise typer.Exit(code)


def _start(cfg: AppConfig, port: int, scheduler: bool = False, *, asked: bool = False) -> int:
    def running(record: runtime.Record) -> int:
        code = _open_running(record.url, record.launcher, record.port, record.since, record.version)
        if asked:
            _say("It keeps the schedule setting it was started with. To change it: cosine-digger stop, "
                 f"then cosine-digger start --{'' if scheduler else 'no-'}scheduler")
        return code

    while (record := runtime.read_record(cfg.home, cfg.db_path)) is not None:
        found = runtime.check(record)
        if found.state == "running":
            return running(record)
        if found.state == "stopping":
            _say(f"Digger is stopping. Waiting for {_jobs(found.jobs)} to finish first." if found.jobs
                 else "Digger is stopping. Waiting for it to finish first.")
            _wait_gone(record)
            continue
        if found.state == "stale":
            runtime.discard_record(record)
            break
        _say(f"Digger is already running at {record.url}, but it isn't answering.",
             f"Try cosine-digger start again in a minute. Its log: {runtime.log_dir(cfg.home) / runtime.SERVER_LOG}")
        return 1
    if runtime.database_in_use(cfg.db_path):
        # A Digger holds these digs without a record: an older serve (it answers on this port at
        # once), one still starting (its launcher may have been closed), or one on another port.
        kind, body = runtime.health(runtime.LOOPBACK, port)
        if not (kind == "answer" and runtime.serves_these_digs(body, cfg.db_path, runtime.LOOPBACK, port)):
            _say("Checking what's using your digs…")
            deadline = time.monotonic() + STARTING_WAIT_S
            while time.monotonic() < deadline and runtime.database_in_use(cfg.db_path):
                record = runtime.read_record(cfg.home, cfg.db_path)
                if record is not None and runtime.check(record).state == "running":
                    return running(record)
                time.sleep(0.25)
            kind, body = runtime.health(runtime.LOOPBACK, port)
        if runtime.database_in_use(cfg.db_path):
            if kind == "answer" and runtime.serves_these_digs(body, cfg.db_path, runtime.LOOPBACK, port):
                started = body.get("started_at")
                return _open_running(f"http://{runtime.LOOPBACK}:{port}", body.get("launcher", "serve"), port,
                                     runtime.since(started) if isinstance(started, str) else None, body.get("version"))
            _say("Digger is already running, started with cosine-digger serve, and it's using your digs.",
                 "To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start")
            return 1
    if runtime.port_busy(runtime.LOOPBACK, port):
        # Nothing holds these digs, so whatever answers here (another program, or a Digger with
        # other digs, such as Docker's) isn't a Digger to open.
        _busy(port)
        return 1
    return _launch(cfg, port, scheduler)


def _open_running(url: str, launcher: str, port: int, since: str | None, version: str | None = None) -> int:
    opened = open_browser(url)
    if launcher == "start":
        _say(f"Digger is already running at {url}" + (f" (since {since})." if since else ".")
             + (" Opening it." if opened else ""))
        if not opened:
            _say(NO_BROWSER)
        _say(*_upgrade(version))
        return 0
    _say(f"Digger is already running at {url}, started with cosine-digger serve." + (" Opening it." if opened else ""))
    if not opened:
        _say(NO_BROWSER)
    _say(f"To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start --port {port}")
    return 0


def _upgrade(version: str | None) -> list[str]:
    """An upgrade replaces the installed files, not the Digger that's running: say so (D22)."""
    installed = runtime.installed_version()
    if version and version != installed and "unknown" not in (version, installed):
        return [f"It's version {version}; {installed} is installed. "
                f"To use {installed}: cosine-digger stop, then cosine-digger start"]
    return []


def _busy(port: int) -> None:
    _say(f"Can't start Digger: another program is using port {port}.",
         f"Start it on another port:  cosine-digger start --port {runtime.free_port_after(runtime.LOOPBACK, port)}")


def _launch(cfg: AppConfig, port: int, scheduler: bool = False) -> int:
    logs = runtime.log_dir(cfg.home)
    start_log, server_log = logs / runtime.START_LOG, logs / runtime.SERVER_LOG
    instance = runtime.new_instance()
    # This folder's .env is already in os.environ (the CLI callback read it); the server reads no other.
    env = {**os.environ, "COSINE_DIGGER_NO_DOTENV": "1"}
    env.pop("COSINE_DIGGER_INSTANCE", None)
    # The child stays in this folder, so a relative path (in the environment or in
    # settings.json) opens the same digs here and there, and it writes the record
    # start looks for. -P keeps this folder off its import path.
    command = [sys.executable, "-P", "-m", "cosine_digger", "serve", "--host", runtime.LOOPBACK, "--port", str(port),
               *([] if scheduler else ["--no-scheduler"]), "--owned-by-start", instance]
    launched = time.time()
    with open(start_log, "w", encoding="utf-8") as output:  # emptied only now that a launch is needed
        os.fchmod(output.fileno(), 0o600)
        # Its own session, no Terminal input, output in a file: closing this
        # Terminal (or killing this launcher) never reaches the server.
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        if child.poll() is not None:
            return _crashed(cfg, port, launched, [start_log, server_log])
        record = runtime.read_record(cfg.home, cfg.db_path)
        if record is not None and record.instance == instance and record.pid == child.pid:
            kind, body = runtime.health(record.host, record.port)
            if kind == "answer" and runtime.answers_as(body, instance=instance, pid=child.pid):
                break
        if time.monotonic() >= deadline:
            if not _end(child):
                _say(f"Digger didn't answer within {READY_TIMEOUT_S} seconds, and it hasn't stopped yet "
                     f"(process {child.pid}).", f"Full log: {server_log}")
                return 1
            return _crashed(cfg, port, launched, [start_log, server_log],
                            line=f"It didn't answer within {READY_TIMEOUT_S} seconds.")
        time.sleep(0.1)
    try:
        runtime.write_last_port(cfg.home, port, scheduler)
    except OSError:
        pass  # only remembers the port for next time: a full disk mustn't hide a running Digger
    opened = open_browser(record.url)
    _say(f"Digger is running at {record.url}", "Opening it in your browser." if opened else NO_BROWSER, "", HELP,
         *(["", "Daily and Weekly refresh on their schedule while it runs."] if scheduler else []))
    return 0


def _end(child: subprocess.Popen) -> bool:
    """Stop our own child (never reaped yet, so its PID can't belong to anyone else): SIGTERM,
    then SIGKILL. True once it has exited; it's never reported stopped while it may still run."""
    for signal_child in (child.terminate, child.kill):
        signal_child()
        try:
            child.wait(STOP_WAIT_S)
            return True
        except subprocess.TimeoutExpired:
            pass
    return False


def _crashed(cfg: AppConfig, port: int, launched: float, logs: list[Path], line: str | None = None) -> int:
    path = logs[-1]
    if line is None:
        hide = tuple(value for value in (getattr(cfg, key) for key in SECRET_FIELDS) if value)
        line, path = runtime.last_log_line(logs, since_time=launched, hide=hide)
        if "address already in use" in line.lower():  # taken between the check and the bind
            _busy(port)
            return 1
    _say("Digger stopped while starting:", f"  {line}", f"Full log: {path}")
    return 1


def status(ctx: typer.Context) -> None:
    """Say whether Digger is running, where, and where your data is."""
    cfg = _cfg(ctx)
    data = f"Data: {cfg.home} (digs in {runtime.resolved(cfg.db_path)})"
    record = runtime.read_record(cfg.home, cfg.db_path)
    found = runtime.check(record) if record is not None else None
    if found is not None and found.state == "running":
        _say(f"Digger is running at {record.url} (since {record.since}, version {record.version}).",
             *_upgrade(record.version), data)
    elif found is not None and found.state == "stopping":
        _say(f"Digger is stopping at {record.url}"
             + (f": waiting for {_jobs(found.jobs)} to finish." if found.jobs else "."), data)
    elif found is not None and found.alive:
        _say(f"Digger is running at {record.url} (since {record.since}, version {record.version}), "
             "but it isn't answering.", data)
    elif runtime.database_in_use(cfg.db_path):
        _say("Digger is running, started with cosine-digger serve. Its Terminal shows its address.", data)
    else:
        _say("Digger isn't running. Start it with: cosine-digger start", data)
        raise typer.Exit(3)


def stop(ctx: typer.Context) -> None:
    """Stop the Digger that cosine-digger start launched, once its jobs finish."""
    cfg = _cfg(ctx)
    try:
        with runtime.start_lock(cfg.home):
            outcome = _ask_to_stop(cfg)
    except TimeoutError as exc:
        _say(str(exc))
        raise typer.Exit(1) from None
    if isinstance(outcome, int):
        raise typer.Exit(outcome)
    record, jobs = outcome
    _say("Stopping Digger…" + (f" waiting for {_jobs(jobs)} to finish." if jobs else ""))
    try:
        _wait_gone(record)
    except KeyboardInterrupt:
        _say("", "Digger is still stopping. It stops by itself once its jobs finish.")
        raise typer.Exit(130) from None
    runtime.discard_record(record)  # only if it ended without removing its own
    _say("Digger stopped. Your digs and settings are saved.")


def _ask_to_stop(cfg: AppConfig) -> int | tuple[runtime.Record, int]:
    record = runtime.read_record(cfg.home, cfg.db_path)
    found = runtime.check(record) if record is not None else None
    if found is None or found.state == "stale":
        if found is not None:
            runtime.discard_record(record)
        if runtime.database_in_use(cfg.db_path):
            _say("Digger was started with cosine-digger serve. To stop it, press Ctrl+C in that Terminal.")
            return 1
        _say("Digger wasn't running.")
        return 0
    if record.launcher == "serve":
        _say(f"Digger at {record.url} was started with cosine-digger serve. "
             "To stop it, press Ctrl+C in that Terminal, or stop its container.")
        return 1
    if found.state == "stopping":
        return record, found.jobs
    if found.state == "running":
        if not runtime.launched_by_start(record):
            _say(f"Couldn't confirm that Digger at {record.url} is the one cosine-digger start launched, "
                 "so it wasn't stopped.")
            return 1
        accepted, jobs = runtime.request_stop(record)
        if accepted:
            return record, jobs
        again = runtime.check(record)
        if again.state in {"stale", "stopping"}:
            return record, again.jobs
    _say(f"Digger at {record.url} isn't answering, so it wasn't stopped.", "Try cosine-digger stop again in a minute.")
    return 1
