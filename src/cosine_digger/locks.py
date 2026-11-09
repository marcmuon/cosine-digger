"""Cross-process locks for writers that share one taste DB (the app, its
scheduler, an OS-installed schedule and manual CLI runs)."""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no cross-process locking
    fcntl = None


class LockTimeout(RuntimeError):
    """Another writer held the lock for longer than we were willing to wait."""


@contextmanager
def file_lock(name: str, *, home: Path | None = None, timeout: float = 600, poll: float = 0.25):
    """Hold an exclusive lock named ``name`` (any string) under ``home/locks``."""
    if home is None:
        from .config import load_config

        home = load_config().home
    directory = Path(home).expanduser() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha1(name.encode()).hexdigest()[:16] + ".lock")
    with open(path, "a+") as handle:
        if fcntl is None:
            yield
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"Another cosine-digger process is still writing ({name}); try again later.")
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
