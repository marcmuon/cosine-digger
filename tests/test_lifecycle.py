"""cosine-digger start/status/stop on real processes, as isolated users.

Each test runs the installed command path (python -m cosine_digger) with a
scratch HOME, an empty working folder, ports from DIGGER_TEST_PORTS and a
stand-in browser (tests/lifecycle_support.py).
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
import pytest

from cosine_digger import config, runtime
from cosine_digger.commands import lifecycle
from cosine_digger.web.app import _version
from tests.lifecycle_support import User, free_port, release_ports, servers_on, started
from tests.stub_cosine import StubCosine

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="start/stop are for macOS and Linux")

HELP = ("Digger keeps running after you close this window.\n"
        "  cosine-digger stop     stop it\n"
        "  cosine-digger status   check on it\n")
STOPPED = "Digger stopped. Your digs and settings are saved.\n"


@pytest.fixture
def user(tmp_path):
    person = User(tmp_path / "user")
    yield person
    person.cleanup()
    release_ports()


def url_of(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def data_line(person: User) -> str:
    return f"Data: {person.app_home} (digs in {runtime.resolved(person.db)})\n"


def fake_record(person: User, *, pid: int, process_start: str, port: int, launcher: str = "start") -> runtime.Record:
    record = runtime.Record(pid=pid, process_start=process_start, instance="0" * 32, launcher=launcher,
                            host="127.0.0.1", port=port, version="0.4.0",
                            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                            home=str(person.app_home), db=str(runtime.resolved(person.db)), token="t" * 43)
    runtime.write_record(record)
    return record


class OtherApp:
    """An unrelated program on a port: answers every GET with 200."""

    def __init__(self, port: int, body: bytes = b'{"ok": true, "name": "something else"}'):
        payload = body

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answers(self) -> bool:
        return httpx.get(f"http://127.0.0.1:{self.server.server_address[1]}/", trust_env=False).status_code == 200

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_start_status_stop_round_trip(user):
    port = free_port()
    url = url_of(port)
    first = user.run("start", "--port", str(port))
    assert (first.returncode, first.stdout, first.stderr) == (0, started(url), "")
    assert user.urls() == [url]
    record = user.record()
    assert record.launcher == "start" and record.port == port and record.state == "running"
    assert oct((user.app_home / "run").stat().st_mode & 0o777) == "0o700"
    assert oct(runtime.record_path(user.app_home, user.db).stat().st_mode & 0o777) == "0o600"
    for _ in range(3):
        assert httpx.get(f"{url}/api/health", trust_env=False).json()["instance"] == record.instance

    status = user.run("status")
    assert (status.returncode, status.stdout) == (
        0, f"Digger is running at {url} (since {record.since}, version {_version()}).\n" + data_line(user))

    again = user.run("start")  # no --port: the port start last used
    assert (again.returncode, again.stdout) == (0, f"Digger is already running at {url} (since {record.since}). Opening it.\n")
    assert user.urls() == [url, url]
    assert servers_on(port) == [record.pid]
    assert "/api/health" not in user.log_text()

    stop = user.run("stop")
    assert (stop.returncode, stop.stdout) == (0, "Stopping Digger…\n" + STOPPED)
    assert not runtime.same_process(record) and user.record() is None
    assert user.db.is_file() and json.loads((user.app_home / "run" / "launcher.json").read_text()) == {"port": port, "scheduler": False}

    stopped_again = user.run("stop")
    assert (stopped_again.returncode, stopped_again.stdout) == (0, "Digger wasn't running.\n")
    gone = user.run("status")
    assert (gone.returncode, gone.stdout) == (3, "Digger isn't running. Start it with: cosine-digger start\n" + data_line(user))


def test_start_remembers_the_scheduler_choice(user):
    # Daily and Weekly refresh only while a scheduler runs; a plain start after a
    # reboot must keep the choice, or the playlists silently stop.
    port = free_port()
    url = url_of(port)

    def scheduled() -> list[bool]:
        return [feed["scheduled"] for feed in httpx.get(f"{url}/api/playlists", trust_env=False).json().values()]

    try:
        first = user.run("start", "--port", str(port), "--scheduler")
        assert first.returncode == 0, first.stdout + first.stderr
        assert first.stdout.endswith("Daily and Weekly refresh on their schedule while it runs.\n")
        assert scheduled() == [True, True]
        assert user.run("stop").returncode == 0
        again = user.run("start")  # the remembered port and scheduler
        assert again.returncode == 0 and scheduled() == [True, True]
        kept = user.run("start", "--no-scheduler")  # already running: says how to change it
        assert kept.returncode == 0 and "It keeps the schedule setting it was started with" in kept.stdout
        assert scheduled() == [True, True]
        assert user.run("stop").returncode == 0
        off = user.run("start", "--no-scheduler")
        assert off.returncode == 0 and scheduled() == [False, False]
    finally:
        user.run("stop")


def test_concurrent_starts_give_one_server(user):
    port = free_port()
    launches = [user.popen("start", "--port", str(port)) for _ in range(3)]
    outputs = [launch.communicate(timeout=180)[0] for launch in launches]
    assert [launch.returncode for launch in launches] == [0, 0, 0]
    record = user.record()
    assert sorted(outputs) == sorted([started(url_of(port))] + 2 * [
        f"Digger is already running at {url_of(port)} (since {record.since}). Opening it.\n"])
    assert servers_on(port) == [record.pid]


def _terminal(person: User, argv: list[str]) -> tuple[int, int]:
    """Run argv as the session leader of a new pseudo-terminal, like a Terminal tab."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(person.cwd)
            os.execve(argv[0], argv, person.env())
        finally:
            os._exit(127)
    return pid, fd


def _read_until(fd: int, marker: str, timeout: float) -> str:
    text, deadline = "", time.monotonic() + timeout
    while marker not in text and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text += chunk.decode(errors="replace")
    return text


def test_closing_the_terminal_leaves_digger_running(user):
    port = free_port()
    # A shell that stays open after start returns, as a Terminal window does.
    shell = ["/bin/sh", "-c", '"$0" "$@"; exec sleep 120', *user.command("start", "--port", str(port))]
    pid, fd = _terminal(user, shell)
    output = _read_until(fd, "check on it", 120)
    assert started(url_of(port)).replace("\n", "\r\n") in output
    record = user.record()
    os.close(fd)  # the window closes: the kernel hangs up the shell's session
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGHUP
    time.sleep(1)
    assert runtime.check(record).state == "running"


def test_closing_the_terminal_before_digger_answers_leaves_it_starting(user):
    port = free_port()
    pid, fd = _terminal(user, user.command("start", "--port", str(port)))
    user.wait_for(lambda: servers_on(port), 30, "the server process")
    os.close(fd)  # hang up while the launcher still waits for the server's first answer
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGHUP
    record = user.wait_for(lambda: (r := user.record()) and runtime.check(r).state == "running" and r, 30, "a record")
    assert servers_on(port) == [record.pid]
    status_now = user.run("status")
    assert status_now.stdout.startswith(f"Digger is running at {url_of(port)} (since ")
    assert user.run("start").stdout.startswith(f"Digger is already running at {url_of(port)}")


def test_stop_waits_for_a_running_dig_and_keeps_it(user):
    with StubCosine(free_port(), delays={"similar": 5.0}) as stub:
        user.extra.update(COSINE_API_TOKEN="synthetic-cosine-key-0001", COSINE_DIGGER_API_BASE=stub.base)  # gitleaks:allow
        port = free_port()
        assert user.run("start", "--port", str(port)).returncode == 0
        with httpx.Client(base_url=url_of(port), trust_env=False, timeout=30) as web:
            items = web.get("/api/search", params={"q": "Stub Artist", "source": "cosine"}).json()["items"]
            seed = next(item for item in items if item["cosine_id"] == "stub-seed")
            dig = web.post("/api/digs", json={"seeds": [seed]})
            assert dig.status_code in (200, 201, 202), dig.text
            job_id = dig.json()["job_id"]
        user.wait_for(lambda: stub.count("similar"), 30, "the dig to reach cosine.club")
        record = user.record()
        began = time.monotonic()
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (
            0, "Stopping Digger… waiting for 1 running job to finish.\n" + STOPPED)
        assert time.monotonic() - began > 2  # it waited for the dig
        assert not runtime.same_process(record)
        # Only API calls count: a local port scanner may GET / on any new port.
        assert all(seen.with_key for seen in stub.seen if seen.path.startswith("/api/")), stub.seen
    with closing(sqlite3.connect(user.db)) as conn:
        assert conn.execute("SELECT state FROM slice_jobs WHERE id=?", (job_id,)).fetchone() == ("done",)
        assert conn.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 1
    assert "synthetic-cosine-key-0001" not in user.log_text()


def test_stale_record_is_cleared_and_nothing_is_signalled(user):
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    fake_record(user, pid=gone.pid, process_start="Thu Jan  1 00:00:00 1970", port=free_port())
    stop = user.run("stop")
    assert (stop.returncode, stop.stdout) == (0, "Digger wasn't running.\n")
    assert user.record() is None
    port = free_port()
    assert user.run("start", "--port", str(port)).stdout == started(url_of(port))


def test_a_reused_pid_or_an_unrelated_app_is_never_stopped(user):
    decoy = subprocess.Popen(["/bin/sleep", "300"])
    other = OtherApp(free_port())
    try:
        # The PID now belongs to another program (different start time).
        fake_record(user, pid=decoy.pid, process_start="Thu Jan  1 00:00:00 1970", port=free_port())
        assert user.run("stop").stdout == "Digger wasn't running.\n"
        assert decoy.poll() is None

        # Same PID and start time, but nothing of ours answers: report, never signal.
        quiet = free_port()
        fake_record(user, pid=decoy.pid, process_start=runtime.process_start(decoy.pid), port=quiet)
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (
            1, f"Digger at {url_of(quiet)} isn't answering, so it wasn't stopped.\nTry cosine-digger stop again in a minute.\n")
        start = user.run("start")
        assert start.returncode == 1 and start.stdout.startswith(f"Digger is already running at {url_of(quiet)}, but it isn't answering.")

        # Same PID and start time, and another program answers on the port.
        busy = other.server.server_address[1]
        fake_record(user, pid=decoy.pid, process_start=runtime.process_start(decoy.pid), port=busy)
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (
            1, f"Digger at {url_of(busy)} isn't answering, so it wasn't stopped.\nTry cosine-digger stop again in a minute.\n")
        assert decoy.poll() is None and other.answers()
        assert user.urls() == []
    finally:
        other.close()
        decoy.terminate()
        decoy.wait()


@pytest.mark.parametrize("body", [
    b'{"ok": true, "name": "something else"}',
    b'{"ok": true, "version": "2.1.0"}',  # the shape a Digger before identity fields answers with
    json.dumps({"app": runtime.APP_ID, "instance": "3" * 32, "pid": 1, "data": "0" * 16}).encode(),
], ids=["another program", "ok and a version", "a Digger with other digs"])
def test_a_busy_port_names_another_port_and_opens_nothing(user, body):
    """Nothing holds these digs, so whatever answers on the port isn't this Digger: not even an
    answer shaped like an older Digger's (deep A1 #1), nor another Digger, such as Docker's (B #9)."""
    other = OtherApp(free_port(), body)
    port = other.server.server_address[1]
    try:
        start = user.run("start", "--port", str(port))
        assert start.returncode == 1
        assert re.fullmatch(rf"Can't start Digger: another program is using port {port}\.\n"
                            r"Start it on another port:  cosine-digger start --port (\d+)\n", start.stdout)
        assert user.urls() == [] and user.record() is None and other.answers()
    finally:
        other.close()


def test_start_opens_a_running_serve_digger_instead_of_starting_another(user):
    port = free_port()
    url = url_of(port)
    serve = user.popen("serve", "--host", "127.0.0.1", "--port", str(port), "--no-scheduler")
    try:
        record = user.wait_for(lambda: user.record(), 60, "serve's record")
        assert record.launcher == "serve"
        start = user.run("start")
        assert (start.returncode, start.stdout) == (0, (
            f"Digger is already running at {url}, started with cosine-digger serve. Opening it.\n"
            f"To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start --port {port}\n"))
        assert user.urls() == [url]
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (1, (
            f"Digger at {url} was started with cosine-digger serve. "
            "To stop it, press Ctrl+C in that Terminal, or stop its container.\n"))
        assert runtime.check(record).state == "running" and servers_on(port) == []
    finally:
        serve.send_signal(signal.SIGINT)  # Ctrl+C in its Terminal
        serve.communicate(timeout=60)
    assert user.record() is None


def test_a_crash_while_starting_shows_the_last_log_line(user):
    user.app_home.mkdir(mode=0o700, parents=True)
    user.db.write_bytes(b"this is not a database\n" * 200)
    port = free_port()
    start = user.run("start", "--port", str(port))
    lines = start.stdout.splitlines()
    assert start.returncode == 1 and len(lines) == 3, start.stdout
    assert lines[0] == "Digger stopped while starting:"
    assert lines[1].startswith("  ") and "not a database" in lines[1]
    assert lines[2].startswith("Full log: ") and lines[2].endswith(os.path.join("logs", "digger.log"))
    assert servers_on(port) == [] and user.record() is None and user.urls() == []


def test_a_browser_that_fails_still_leaves_the_address(user):
    user.browser = user.browser_script("browser-fails.sh", 1)
    port = free_port()
    start = user.run("start", "--port", str(port))
    assert (start.returncode, start.stdout) == (0, (
        f"Digger is running at {url_of(port)}\nCouldn't open your browser. Open that address yourself.\n\n" + HELP))
    assert user.urls() == [url_of(port)]  # tried once, never fell back to another browser


def test_port_order_and_dotenv(user):
    p1, p2, p3, p4 = (free_port() for _ in range(4))

    def run_on(*args):
        result = user.run("start", *args)
        assert result.returncode == 0, result.stdout + result.stderr
        port = user.record().port
        assert user.run("stop").returncode == 0
        return port

    user.settings(port=p1)
    assert run_on() == p1                      # settings.json
    user.extra["COSINE_DIGGER_PORT"] = str(p2)
    assert run_on() == p2                      # COSINE_DIGGER_PORT beats settings.json
    assert run_on("--port", str(p4)) == p4     # --port beats both
    del user.extra["COSINE_DIGGER_PORT"]
    user.settings()
    assert run_on() == p4                      # the port start last used
    # start, status and stop read this folder's .env as serve does; the environment wins over it.
    (user.cwd / ".env").write_text(f"COSINE_DIGGER_PORT={p3}\nCOSINE_API_TOKEN=dotenv-synthetic-key-0003\n")  # gitleaks:allow
    start = user.run("start")
    assert (start.returncode, start.stdout) == (0, started(url_of(p3)))
    assert user.record().port == p3
    key = httpx.get(f"{url_of(p3)}/api/settings", trust_env=False).json()["cosine_token"]
    assert (key["set"], key["hint"], key["source"]) == (True, "…0003", "env")
    assert user.run("status").returncode == 0
    assert user.run("stop").returncode == 0
    user.extra["COSINE_DIGGER_PORT"] = str(p2)
    assert run_on() == p2                      # COSINE_DIGGER_PORT in the environment beats the .env
    del user.extra["COSINE_DIGGER_PORT"]
    (user.cwd / ".env").unlink()
    fresh = config.load_config({"HOME": str(user.root / "nobody")})
    assert runtime.choose_port(fresh, None) == runtime.DEFAULT_PORT


def test_status_and_stop_read_the_folders_dotenv_too(user):
    # The data folder and a relative database come only from this folder's .env: status and stop
    # must read it to find the Digger start launched from here, and stop must end that process.
    port, home, db = free_port(), user.root / "dotenv-home", user.cwd / "digs" / "taste.db"
    db.parent.mkdir()
    (user.cwd / ".env").write_text(f"COSINE_DIGGER_HOME={home}\nCOSINE_DIGGER_DB=digs/taste.db\n")
    record = None
    try:
        assert user.run("start", "--port", str(port)).returncode == 0
        record = runtime.read_record(home, db)
        assert record is not None and record.port == port and runtime.same_process(record)
        assert user.record() is None                # nothing under the default data folder
        status = user.run("status")
        assert status.returncode == 0, status.stdout
        assert status.stdout.startswith(f"Digger is running at {url_of(port)} (since ")
        assert status.stdout.endswith(f"Data: {home} (digs in {runtime.resolved(db)})\n")
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (0, "Stopping Digger…\n" + STOPPED)
        assert not runtime.same_process(record)
    finally:  # the user fixture's cleanup looks only in the default data folder
        if (record is not None and runtime.same_process(record)
                and f"--owned-by-start {record.instance}" in runtime.process_command(record.pid)):
            os.kill(record.pid, signal.SIGTERM)


def test_a_saved_key_stays_editable_under_start(user):
    user.settings(cosine_token="saved-synthetic-key-0001")  # gitleaks:allow
    port = free_port()
    with StubCosine(free_port()) as stub:
        user.extra["COSINE_DIGGER_API_BASE"] = stub.base
        assert user.run("start", "--port", str(port)).returncode == 0
        with httpx.Client(base_url=url_of(port), trust_env=False) as web:
            key = web.get("/api/settings").json()["cosine_token"]
            assert (key["set"], key["hint"], key["source"]) == (True, "…0001", "settings")
            # Replaced the way the key screen does it: checked with cosine.club, then saved.
            reply = web.post("/api/connections/cosine_token", json={"key": "replaced-synthetic-key-0002"}).json()  # gitleaks:allow
            assert reply["state"] == "connected" and reply["settings"]["cosine_token"]["source"] == "settings"
    saved = json.loads((user.app_home / "settings.json").read_text())
    assert saved["cosine_token"] == "replaced-synthetic-key-0002"  # a saved key, not an environment one
    assert "synthetic-key" not in user.log_text()


def test_a_record_found_under_another_home_never_stops_that_digger(user, tmp_path):
    other = User(tmp_path / "other")
    try:
        port = free_port()
        assert other.run("start", "--port", str(port)).returncode == 0
        theirs = other.record()
        mine = runtime.record_path(user.app_home, user.db)
        mine.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mine.write_text(runtime.record_path(other.app_home, other.db).read_text())
        assert user.record() is None  # it names the other home and database
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (0, "Digger wasn't running.\n")
        assert servers_on(port) == [theirs.pid] and other.record() == theirs
    finally:
        other.cleanup()


@pytest.mark.parametrize("server", ["answers as another Digger"])
def test_the_stop_token_only_travels_on_the_connection_that_answered(server):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0" if server == "closes the connection" else "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.partition("?")[0] != "/api/health":
                # stop asks only /api/health; anything else came from another local program
                # (this Mac runs a port prober that sends GET / to new listeners).
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            seen.append(("GET", self.client_address[1], None))
            instance = "2" * 32 if server == "answers as another Digger" else "1" * 32
            self.reply({"app": runtime.APP_ID, "instance": instance, "pid": 4242})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append(("POST", self.client_address[1], self.headers.get(runtime.TOKEN_HEADER)))
            self.reply({"ok": True, "jobs": 2})

    listener = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    record = runtime.Record(pid=4242, process_start="-", instance="1" * 32, launcher="start", host="127.0.0.1",
                            port=listener.server_address[1], version="0.4.0", started_at="2026-10-06T04:00:00-07:00",
                            home="/nowhere", db="/nowhere/taste.db", token="private-stop-token")
    try:
        accepted = runtime.request_stop(record)
    finally:
        listener.shutdown()
        listener.server_close()
        release_ports()
    if server == "keeps the connection":
        assert accepted == (True, 2)
        (_, health_port, _), (_, stop_port, token) = seen
        assert health_port == stop_port and token == "private-stop-token"
    else:
        assert accepted == (False, 0)
        assert [kind for kind, _, _ in seen] == ["GET"]  # the token never left


def test_an_unreadable_start_time_is_never_taken_for_an_exit(user, monkeypatch):
    port = free_port()
    assert user.run("start", "--port", str(port)).returncode == 0
    record = user.record()
    monkeypatch.setattr(runtime, "process_start", lambda pid: None)  # as if ps timed out
    assert runtime.process_state(record) == "unknown"
    assert runtime.check(record).state == "running"  # /api/health still proves who it is
    assert runtime.check(replace(record, port=free_port())).state == "unresponsive"  # never "stale"
    waiting = threading.Thread(target=lifecycle._wait_gone, args=(record,), daemon=True)
    waiting.start()
    waiting.join(1.0)
    assert waiting.is_alive()  # stop keeps waiting instead of saying "Digger stopped."
    stop = user.run("stop")  # its own process, where the start time reads normally
    assert stop.stdout.endswith(STOPPED)
    waiting.join(10)
    assert not waiting.is_alive()


@pytest.mark.parametrize("source", ["environment", "settings.json"])
def test_relative_data_paths_mean_the_same_under_start(tmp_path, source):
    person = User(tmp_path / "relative", **({"COSINE_DIGGER_DB": "digs/taste.db"} if source == "environment" else {}))
    if source == "settings.json":
        person.settings(db_path="digs/taste.db")
    (person.cwd / "digs").mkdir()
    port = free_port()
    try:
        result = person.run("start", "--port", str(port))
        assert (result.returncode, result.stdout) == (0, started(url_of(port)))
        db = (person.cwd / "digs" / "taste.db").resolve()
        status = person.run("status")
        assert status.stdout.splitlines()[1] == f"Data: {person.app_home} (digs in {db})"
        assert db.is_file()
    finally:
        stop = person.run("stop")
        release_ports()
    assert stop.stdout.endswith(STOPPED)


class OlderServe(OtherApp):
    """A Digger from before start existed, run with cosine-digger serve: it holds its digs' lock
    (as its slice server does) and answers its health, settings and offline check (/api/doctor,
    whose config names its database) as Digger 0.4 does."""

    def __init__(self, port: int, db: Path, *, holds: bool = True, health: dict | None = None):
        self.lock = None
        if holds:
            lock = runtime.database_lock_path(db)
            lock.parent.mkdir(parents=True, exist_ok=True)
            self.lock = open(lock, "a+")
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX)
        answers = {"/api/health": health or {"ok": True, "version": "0.3.9"},
                   "/api/settings": {"daily_time": "07:00", "cosine_token": {"set": False, "hint": ""}},
                   "/api/doctor": {"checks": {"db": {"status": "ok", "detail": f"{db}; exists=true; user_version=9"}},
                                   "config": {"db_path": str(db)}}}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps(answers.get(self.path.split("?")[0], {"detail": "Not Found"})).encode()
                self.send_response(200 if self.path.split("?")[0] in answers else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        super().close()
        if self.lock is not None:
            self.lock.close()


def test_an_older_serve_holding_the_digs_is_opened_at_once(user):
    """Deep B #6, what Digger's owner meets at merge: the serve already running holds the digs and
    has no record. start opened it only after a silent 15-second wait; now at once."""
    port = free_port()
    url = url_of(port)
    older = OlderServe(port, user.db)
    try:
        began = time.monotonic()
        start = user.run("start", "--port", str(port))
        assert (start.returncode, start.stdout) == (0, (
            f"Digger is already running at {url}, started with cosine-digger serve. Opening it.\n"
            f"To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start --port {port}\n"))
        assert time.monotonic() - began < lifecycle.STARTING_WAIT_S and user.urls() == [url]
        status = user.run("status")
        assert (status.returncode, status.stdout) == (
            0, "Digger is running, started with cosine-digger serve. Its Terminal shows its address.\n" + data_line(user))
        stop = user.run("stop")
        assert (stop.returncode, stop.stdout) == (
            1, "Digger was started with cosine-digger serve. To stop it, press Ctrl+C in that Terminal.\n")
        # Run on another port, it can't be found by its address: start says what it's checking.
        elsewhere = user.run("start", "--port", str(free_port()))
        assert (elsewhere.returncode, elsewhere.stdout) == (1, (
            "Checking what's using your digs…\n"
            "Digger is already running, started with cosine-digger serve, and it's using your digs.\n"
            "To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start\n"))
        assert user.urls() == [url] and user.record() is None
        assert httpx.get(f"{url}/api/health", trust_env=False).json()["version"] == "0.3.9"  # still running
    finally:
        older.close()


@pytest.mark.parametrize("answer", ["an older Digger with other digs", "a program naming another app"])
def test_only_the_digger_holding_these_digs_is_opened(user, tmp_path, answer):
    """Deep re-check #1: while an older serve on one port held these digs, start on another port
    opened whatever answered there in an older Digger's shape: one with other digs, or even one
    naming another app. Now only an answer that names these digs' database counts."""
    holder = OlderServe(free_port(), user.db)  # these digs' own serve, on a port start doesn't use
    port = free_port()
    other = (OlderServe(port, tmp_path / "other" / "taste.db", holds=False) if answer.startswith("an older") else
             OlderServe(port, user.db, holds=False, health={"app": "something-else", "ok": True, "version": "2.0"}))
    try:
        start = user.run("start", "--port", str(port))
        assert (start.returncode, start.stdout) == (1, (
            "Checking what's using your digs…\n"
            "Digger is already running, started with cosine-digger serve, and it's using your digs.\n"
            "To switch to cosine-digger start, press Ctrl+C in that Terminal, then run cosine-digger start\n"))
        assert user.urls() == [] and user.record() is None
        assert httpx.get(f"{url_of(port)}/api/health", trust_env=False).status_code == 200  # left running
    finally:
        other.close()
        holder.close()


def test_a_start_time_that_reads_differently_still_names_its_digger(user):
    """Deep B #1: ps printed a process's start in local time, so after a time-zone change stop
    took a running Digger for gone, dropped its record and could no longer stop it."""
    port = free_port()
    assert user.run("start", "--port", str(port)).returncode == 0
    record = user.record()
    runtime.write_record(replace(record, process_start="Thu Jan  1 00:00:00 1970"))  # as read in another zone
    status = user.run("status")
    assert status.stdout.startswith(f"Digger is running at {url_of(port)} (since ")
    stop = user.run("stop")
    assert (stop.returncode, stop.stdout) == (0, "Stopping Digger…\n" + STOPPED)
    assert not runtime.pid_alive(record.pid) and user.record() is None


def test_start_names_an_upgrade_the_running_digger_doesnt_have(user):
    """Deep B #4: after uv tool upgrade, the Digger still running is the old version; start says
    so and how to switch, instead of opening the old one silently."""
    port = free_port()
    assert user.run("start", "--port", str(port)).returncode == 0
    record = user.record()
    runtime.write_record(replace(record, version="0.3.9"))  # as if upgraded since it started
    again = user.run("start")
    installed = _version()
    assert installed != "unknown"
    upgrade = f"It's version 0.3.9; {installed} is installed. To use {installed}: cosine-digger stop, then cosine-digger start\n"
    assert (again.returncode, again.stdout) == (0, (
        f"Digger is already running at {url_of(port)} (since {record.since}). Opening it.\n" + upgrade))
    status = user.run("status")  # deep re-check #5: status says it too (D22)
    assert (status.returncode, status.stdout) == (0, (
        f"Digger is running at {url_of(port)} (since {record.since}, version 0.3.9).\n" + upgrade + data_line(user)))
