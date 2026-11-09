"""A stand-in cosine.club API on loopback for real-process checks.

It answers like cosine.club (200, 401, 429 with Retry-After, 503) or holds
the connection without answering, can delay any path, and records each
request under /api/: its method, path and whether a key was sent, never the
key itself.
Point a Digger at it with COSINE_DIGGER_API_BASE=<stub.base>.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit


def track(tid: str, artist: str, title: str, video: str) -> dict:
    return {"id": tid, "name": f"{artist} - {title}", "artist": artist, "track": title, "score": None,
            "video_id": video, "source": "discogs", "video_uri": f"https://www.youtube.com/watch?v={video}",
            "external_link": None}


SEED = track("stub-seed", "Stub Artist", "Seed Track", "STUBSEED001")
NEIGHBOURS = [track(f"stub-n{i}", f"Neighbour {i}", f"Track {i}", f"STUBNEIGH0{i}") for i in range(1, 7)]
TRACKS = {item["id"]: item for item in [SEED, *NEIGHBOURS]}


@dataclass
class Seen:
    method: str
    path: str
    with_key: bool
    agent: str = ""  # User-Agent, to name the caller when a check fails
    key4: str = ""  # the last 4 characters of the key sent (synthetic keys only), to tell keys apart


@dataclass
class StubCosine:
    port: int
    mode: str = "ok"  # ok | 401 | 429 | 503 | silent
    retry_after: int = 7
    delays: dict[str, float] = field(default_factory=dict)  # "search" | "lookup" | "similar" -> seconds
    refuse: set[str] = field(default_factory=set)  # keys answered with 401, like a revoked key
    seen: list[Seen] = field(default_factory=list)

    def __post_init__(self):
        self.closing = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="stub-cosine", daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def __enter__(self) -> StubCosine:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.closing.set()
        self._server.shutdown()
        self._server.server_close()

    def count(self, kind: str) -> int:
        return sum(1 for item in self.seen if _kind(item.path) == kind)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                if not parts.path.startswith("/api/"):
                    # Digger calls cosine.club only under /api/v1. Anything else came from another
                    # local program (this Mac runs a port prober that sends GET / to new listeners).
                    return self._json(404, {"success": False, "error": "Not found"})
                stub.seen.append(Seen("GET", parts.path, bool(self.headers.get("Authorization")),
                                     self.headers.get("User-Agent", ""),
                                     self.headers.get("Authorization", "").removeprefix("Bearer ")[-4:]))
                kind = _kind(parts.path)
                if stub.mode == "silent":
                    stub.closing.wait(120)
                    return
                if stub.delays.get(kind):
                    time.sleep(stub.delays[kind])
                auth = self.headers.get("Authorization", "")
                if stub.mode == "401" or not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") in stub.refuse:
                    return self._json(401, {"success": False, "error": "Invalid or missing API key"})
                if stub.mode == "429":
                    return self._json(429, {"success": False, "error": "Rate limit exceeded"},
                                      {"Retry-After": str(stub.retry_after)})
                if stub.mode == "503":
                    return self._json(503, {"success": False, "error": "Service unavailable"})
                params = {key: values[0] for key, values in parse_qs(parts.query).items()}
                if kind == "search":
                    query = params.get("q", "").casefold()
                    hits = [item for item in TRACKS.values() if query and query in item["name"].casefold()]
                    return self._json(200, {"success": True, "data": hits[:int(params.get("limit", 20))]})
                if kind == "lookup":
                    hits = [SEED] if "stub-seed" in params.get("url", "") else []
                    return self._json(200, {"success": True, "data": hits})
                if kind == "similar":
                    tid = parts.path.removeprefix("/api/v1/tracks/").removesuffix("/similar")
                    if tid not in TRACKS:
                        return self._json(404, {"success": False, "error": "Track not found"})
                    offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
                    neighbours = [item for item in NEIGHBOURS if item["id"] != tid]
                    return self._json(200, {"success": True, "data": {
                        "source_track": TRACKS[tid], "similar_tracks": neighbours[offset:offset + limit]},
                        "meta": {"total": len(neighbours), "has_more": offset + limit < len(neighbours)}})
                return self._json(404, {"success": False, "error": "Not found"})

            def _json(self, status, payload, headers=None):
                body = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        return Handler


def _kind(path: str) -> str:
    if path == "/api/v1/search":
        return "search"
    if path == "/api/v1/tracks/lookup":
        return "lookup"
    if path.startswith("/api/v1/tracks/") and path.endswith("/similar"):
        return "similar"
    return "other"
