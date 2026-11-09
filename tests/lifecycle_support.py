"""Isolated users for real-process checks of start/status/stop.

Each process gets a fresh environment (nothing inherited but this test's
interpreter), a scratch HOME whose app home is $HOME/.cosine-digger, an empty
working folder, a port from DIGGER_TEST_PORTS (default 18800-18899), proxies
that refuse any outside request, and a stand-in browser that records URLs and
never opens one. Cleanup stops only servers this user's record names.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from cosine_digger import runtime

_taken: set[int] = set()
_taken_lock = threading.Lock()


def port_range() -> range:
    low, _, high = os.environ.get("DIGGER_TEST_PORTS", "18800-18899").partition("-")
    return range(int(low), int(high) + 1)


def _free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        pass
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port() -> int:
    with _taken_lock:
        for port in port_range():
            if port != 8777 and port not in _taken and _free(port):
                _taken.add(port)
                return port
    raise RuntimeError("No free port left in DIGGER_TEST_PORTS")


def release_ports() -> None:
    """After a test's servers have stopped, its ports can be handed out again."""
    with _taken_lock:
        _taken.clear()


def started(url: str) -> str:
    return (f"Digger is running at {url}\nOpening it in your browser.\n\n"
            "Digger keeps running after you close this window.\n"
            "  cosine-digger stop     stop it\n"
            "  cosine-digger status   check on it\n")


class User:
    def __init__(self, root: Path, **env: str):
        self.root = Path(root)
        self.home = self.root / "home"
        self.app_home = self.home / ".cosine-digger"
        self.db = self.app_home / "taste.db"
        self.cwd = self.root / "cwd"
        self.extra = dict(env)
        for folder in (self.home, self.cwd):
            folder.mkdir(parents=True, exist_ok=True)
        self.browser = self.browser_script("browser.sh", 0)

    def browser_script(self, name: str, status: int) -> Path:
        script = self.root / name
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{self.root}/browser.log"\nexit {status}\n')
        script.chmod(0o755)
        return script

    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                "BROWSER": str(self.browser), "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1,localhost", **self.extra}

    def command(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "cosine_digger", *args]

    def run(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(*args), env=self.env(), cwd=self.cwd, capture_output=True, text=True,
                              timeout=timeout)

    def popen(self, *args: str, **kwargs) -> subprocess.Popen:
        return subprocess.Popen(self.command(*args), env=self.env(), cwd=self.cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, **kwargs)

    def urls(self) -> list[str]:
        log = self.root / "browser.log"
        return log.read_text().splitlines() if log.exists() else []

    def record(self) -> runtime.Record | None:
        return runtime.read_record(self.app_home, self.db)

    def settings(self, **values) -> None:
        self.app_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.app_home / "settings.json").write_text(json.dumps(values))

    def log_text(self) -> str:
        logs = self.app_home / "logs"
        return "".join(path.read_text(errors="replace") for path in sorted(logs.glob("*.log*"))) if logs.exists() else ""

    def wait_for(self, predicate, timeout: float = 30, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"Timed out waiting for {what}")

    def cleanup(self) -> None:
        """Stop the server this user's record names, and only that one."""
        record = self.record()
        if record is None or not runtime.same_process(record):
            return
        if record.launcher == "start":
            self.run("stop", timeout=60)
        if runtime.same_process(record) and f"--owned-by-start {record.instance}" in runtime.process_command(record.pid):
            os.kill(record.pid, signal.SIGTERM)


def servers_on(port: int) -> list[int]:
    """PIDs of start-launched servers for this port (from their command lines)."""
    output = subprocess.run(["/bin/ps", "-ax", "-ww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    marker = f"serve --host 127.0.0.1 --port {port} --no-scheduler --owned-by-start "
    return [int(line.split(None, 1)[0]) for line in output.splitlines() if marker in line]
