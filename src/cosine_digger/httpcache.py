"""Optional, best-effort HTTP response caches."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from . import storage

logger = logging.getLogger(__name__)


class HttpCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, body: str, ttl_s: int) -> None: ...


class DbHttpCache:
    """Own a SQLite connection confined to one worker, safe across caller threads.

    storage.connect uses SQLite's default thread affinity. All connection work,
    including opening and closing, therefore runs on the same worker thread.
    The caller owns this cache and must close it when finished.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1)
        self._conn: sqlite3.Connection | None = None
        with self._lock:
            try:
                self._worker.submit(self._open, path).result()
            except sqlite3.Error:
                logger.warning("HTTP cache initialization failed", exc_info=True)
                self._worker.shutdown()
            except BaseException:
                self._worker.shutdown()
                raise

    def _open(self, path: str | Path) -> None:
        conn = storage.connect(path)
        try:
            storage.migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def get(self, key: str) -> str | None:
        with self._lock:
            if self._conn is not None:
                try:
                    return self._worker.submit(storage.cache_get, self._conn, key).result()
                except sqlite3.Error:
                    logger.warning("HTTP cache read failed", exc_info=True)
        return None

    def put(self, key: str, body: str, ttl_s: int) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._worker.submit(storage.cache_put, self._conn, key, body, ttl_s).result()
                except sqlite3.Error:
                    logger.warning("HTTP cache write failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            try:
                if self._conn is not None:
                    self._worker.submit(self._conn.close).result()
            except sqlite3.Error:
                logger.warning("HTTP cache close failed", exc_info=True)
            finally:
                self._conn = None
                self._worker.shutdown()


class MemoryHttpCache:
    """Small in-memory cache with the same expiry semantics as SQLite."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        return entry[0] if entry is not None and entry[1] > time.time() else None

    def put(self, key: str, body: str, ttl_s: int) -> None:
        self._entries[key] = (body, time.time() + ttl_s)
