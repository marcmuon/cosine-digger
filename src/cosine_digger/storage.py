"""Versioned SQLite storage and small repository helpers.

Connections leave foreign-key enforcement off for legacy out-of-order inserts.
Migrations own their transactions; call them before starting application writes.
Legacy write helpers commit; slice writes participate in the caller transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .slice_types import Hub, Point, StepIdentity, StepPick, build_hubs, island_position, pick_layout


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    filename TEXT,
    folder TEXT,
    size_bytes INTEGER,
    mtime TEXT,
    artist TEXT,
    album TEXT,
    title TEXT,
    track_no TEXT,
    label TEXT,
    year TEXT,
    genre TEXT,
    duration_s REAL,
    bitrate INTEGER,
    source TEXT,
    title_source TEXT,
    cosine_verified INTEGER DEFAULT NULL,
    cosine_track_id TEXT DEFAULT NULL,
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS taste_signals (
    id INTEGER PRIMARY KEY,
    signal_type TEXT NOT NULL,
    signal_value TEXT NOT NULL,
    track_count INTEGER DEFAULT 0,
    album_count INTEGER DEFAULT 0,
    earliest_mtime TEXT,
    latest_mtime TEXT,
    recency_score_90d REAL DEFAULT 0.0,
    recency_score_365d REAL DEFAULT 0.0,
    recency_score_all REAL DEFAULT 0.0,
    UNIQUE(signal_type, signal_value)
);

CREATE TABLE IF NOT EXISTS discogs_cache (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    discogs_release_id TEXT,
    discogs_label TEXT,
    discogs_label_id TEXT,
    discogs_year TEXT,
    discogs_genre TEXT,
    discogs_url TEXT,
    looked_up_at TEXT DEFAULT (datetime('now')),
    UNIQUE(artist, album)
);

CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_mtime ON tracks(mtime);
CREATE INDEX IF NOT EXISTS idx_signals_type_value ON taste_signals(signal_type, signal_value);
"""


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    seeds TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hops_completed INTEGER DEFAULT 0,
    total_results INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    track_id TEXT NOT NULL,
    artist TEXT,
    track_name TEXT,
    video_id TEXT,
    score REAL,
    hop INTEGER NOT NULL,
    via TEXT,
    branch_id INTEGER REFERENCES session_branches(id),
    discovered_at TEXT NOT NULL,
    UNIQUE(session_id, track_id)
);

CREATE TABLE IF NOT EXISTS session_branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    hop INTEGER NOT NULL,
    branch_type TEXT NOT NULL,
    branch_value TEXT NOT NULL,
    branch_id_ext TEXT,
    branch_score REAL,
    explored INTEGER DEFAULT 0,
    explored_at TEXT,
    parent_branch_id INTEGER REFERENCES session_branches(id)
);

CREATE TABLE IF NOT EXISTS session_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    result_id INTEGER NOT NULL REFERENCES session_results(id),
    reaction TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_value TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL,
    session_id TEXT REFERENCES sessions(id),
    discovered_at TEXT NOT NULL,
    UNIQUE(source_type, source_value, target_type, target_value, edge_type)
);

CREATE TABLE IF NOT EXISTS result_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (result_id) REFERENCES session_results(id)
);

CREATE TABLE IF NOT EXISTS muted_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_type TEXT NOT NULL,
    lead_value TEXT NOT NULL,
    muted_at TEXT NOT NULL,
    UNIQUE(lead_type, lead_value)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON discovery_edges(source_type, source_value);
CREATE INDEX IF NOT EXISTS idx_edges_target ON discovery_edges(target_type, target_value);
CREATE INDEX IF NOT EXISTS idx_results_session ON session_results(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON session_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_branches_session ON session_branches(session_id);
CREATE INDEX IF NOT EXISTS idx_result_events_result ON result_events(result_id);
CREATE INDEX IF NOT EXISTS idx_muted_leads ON muted_leads(lead_type, lead_value);
"""


_RELAUNCH_SCHEMA = """
CREATE TABLE served_ledger (
    track_id TEXT NOT NULL,
    video_id TEXT,
    artist TEXT,
    title TEXT,
    playlist TEXT NOT NULL,
    served_at TEXT NOT NULL,
    PRIMARY KEY(track_id, playlist, served_at)
);
CREATE INDEX idx_served_ledger_track_id ON served_ledger(track_id);
CREATE INDEX idx_served_ledger_video_id ON served_ledger(video_id);

CREATE TABLE http_cache (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_s INTEGER NOT NULL
);

CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    scheduled_for TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE fresh_finds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT,
    source_url TEXT,
    artist TEXT,
    title TEXT,
    track_id TEXT,
    video_id TEXT,
    found_at TEXT NOT NULL,
    UNIQUE(source_url, artist, title)
);

CREATE TABLE sink_playlists (
    sink TEXT NOT NULL,
    name TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(sink, name)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a DB with WAL and a ten-second lock wait, creating its parents."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except BaseException:
        conn.close()
        raise
    return conn


def _execute_schema(conn: sqlite3.Connection, script: str) -> None:
    # executescript implicitly commits an existing transaction. Execute complete
    # statements separately so schema, column additions and user_version commit
    # together, and a failed migration leaves no partial DDL behind.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def _migration_v1(conn: sqlite3.Connection) -> None:
    _execute_schema(conn, _SCHEMA + _SESSION_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    for column, column_type in (
        ("title_source", "TEXT"),
        ("cosine_verified", "INTEGER DEFAULT NULL"),
        ("cosine_track_id", "TEXT DEFAULT NULL"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {column_type}")
    result_columns = {row[1] for row in conn.execute("PRAGMA table_info(session_results)")}
    if "video_id" not in result_columns:
        conn.execute("ALTER TABLE session_results ADD COLUMN video_id TEXT")


def _migration_v2(conn: sqlite3.Connection) -> None:
    _execute_schema(conn, _RELAUNCH_SCHEMA)


def _migration_v3(conn: sqlite3.Connection) -> None:
    # Recordings you already know (the resolved tracks behind your seeds);
    # generated playlists never serve them.
    _execute_schema(conn, """
CREATE TABLE known_tracks (
    track_id TEXT PRIMARY KEY,
    video_id TEXT,
    artist TEXT,
    title TEXT,
    source TEXT NOT NULL,
    added_at TEXT NOT NULL
);
""")


_SLICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS digs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  dig_id TEXT NOT NULL REFERENCES digs(id),
  number INTEGER NOT NULL CHECK(number > 0),
  parent_id TEXT REFERENCES steps(id),
  rerun_of TEXT REFERENCES steps(id),
  origins TEXT NOT NULL CHECK(json_valid(origins) AND json_array_length(origins) BETWEEN 1 AND 10),
  context TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context)),
  move TEXT NOT NULL CHECK(move IN ('seed','track','label','artist','person','these')),
  further INTEGER NOT NULL CHECK(further IN (0,1)),
  status TEXT NOT NULL CHECK(status IN ('running','done','failed','stopped')),
  note TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(note)),
  error TEXT CHECK(error IS NULL OR json_valid(error)),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  picks_done_at TEXT,
  current_job_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  layout_version INTEGER NOT NULL DEFAULT 1,
  island_x REAL NOT NULL,
  island_y REAL NOT NULL,
  UNIQUE(dig_id, number)
);
CREATE TABLE IF NOT EXISTS step_picks (
  step_id TEXT NOT NULL REFERENCES steps(id),
  rank INTEGER NOT NULL CHECK(rank > 0),
  track_key TEXT NOT NULL,
  cosine_id TEXT NOT NULL,
  artist TEXT NOT NULL,
  title TEXT NOT NULL,
  video_id TEXT,
  score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
  batch INTEGER NOT NULL CHECK(batch IN (0,1)),
  route TEXT NOT NULL CHECK(json_valid(route)),
  facts TEXT CHECK(facts IS NULL OR json_valid(facts)),
  facts_state TEXT NOT NULL DEFAULT 'loading'
    CHECK(facts_state IN ('loading','ready','missing','unavailable','no_token')),
  radius REAL NOT NULL CHECK(radius BETWEEN 0.08 AND 0.85),
  angle REAL NOT NULL CHECK(angle >= 0 AND angle < 6.283185307179586),
  PRIMARY KEY(step_id, rank),
  UNIQUE(step_id, track_key)
);
CREATE TABLE IF NOT EXISTS step_hubs (
  step_id TEXT NOT NULL REFERENCES steps(id),
  hub_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('label','person')),
  name TEXT NOT NULL,
  discogs_id TEXT,
  pick_ranks TEXT NOT NULL CHECK(json_valid(pick_ranks) AND json_array_length(pick_ranks) >= 2),
  radius REAL NOT NULL DEFAULT 0.95 CHECK(radius = 0.95),
  angle REAL NOT NULL CHECK(angle >= 0 AND angle < 6.283185307179586),
  PRIMARY KEY(step_id, hub_key)
);
CREATE TABLE IF NOT EXISTS judgments (
  track_key TEXT PRIMARY KEY,
  verdict TEXT NOT NULL CHECK(verdict IN ('keep','pass')),
  step_id TEXT NOT NULL REFERENCES steps(id),
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heard (
  track_key TEXT PRIMARY KEY,
  first_step_id TEXT NOT NULL REFERENCES steps(id),
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slice_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('step','label_enrichment')),
  step_id TEXT REFERENCES steps(id),
  attempt INTEGER NOT NULL CHECK(attempt > 0),
  state TEXT NOT NULL CHECK(state IN ('running','done','error','stopped')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  last_seq INTEGER NOT NULL DEFAULT 0,
  progress TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(progress)),
  result TEXT CHECK(result IS NULL OR json_valid(result)),
  error TEXT CHECK(error IS NULL OR json_valid(error)),
  CHECK((kind = 'step' AND step_id IS NOT NULL) OR
        (kind = 'label_enrichment' AND step_id IS NULL))
);
CREATE TABLE IF NOT EXISTS slice_events (
  job_id TEXT NOT NULL REFERENCES slice_jobs(id),
  seq INTEGER NOT NULL CHECK(seq > 0),
  event TEXT NOT NULL CHECK(event IN ('pick','batch','facts','hub','note','picks_done','done','error')),
  payload TEXT NOT NULL CHECK(json_valid(payload)),
  at TEXT NOT NULL,
  PRIMARY KEY(job_id, seq)
);
CREATE TABLE IF NOT EXISTS judgment_controls (
  track_key TEXT PRIMARY KEY,
  revision INTEGER NOT NULL CHECK(revision > 0),
  suppress_legacy INTEGER NOT NULL CHECK(suppress_legacy IN (0,1)),
  history TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(history)),
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_dig_number ON steps(dig_id, number);
CREATE INDEX IF NOT EXISTS idx_steps_parent ON steps(parent_id, number);
CREATE INDEX IF NOT EXISTS idx_steps_fingerprint ON steps(dig_id, parent_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_step_picks_track ON step_picks(track_key, step_id);
CREATE INDEX IF NOT EXISTS idx_step_hubs_key ON step_hubs(hub_key, step_id);
CREATE INDEX IF NOT EXISTS idx_slice_jobs_step ON slice_jobs(step_id, attempt);
"""


def _migration_v4(conn: sqlite3.Connection) -> None:
    _execute_schema(conn, _SLICE_SCHEMA)


_MIGRATIONS = (_migration_v1, _migration_v2, _migration_v3, _migration_v4)
SCHEMA_VERSION = len(_MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply newer migrations, each atomically; current DBs execute no DDL.

    Run before application writes, with no transaction open. A write lock and
    version recheck serialize upgrades made by concurrent connections.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, migration in enumerate(_MIGRATIONS, start=1):
        if target <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if target > version:
                migration(conn)
                conn.execute(f"PRAGMA user_version={target}")
                version = target
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_served(conn: sqlite3.Connection, playlist: str, tracks: list[dict]) -> str:
    """Append a serving event for each track, with one timestamp per batch;
    returns that timestamp (the batch's handle for ``unrecord_served``)."""
    served_at = _now_iso()
    with conn:
        conn.executemany(
            """INSERT INTO served_ledger
               (track_id, video_id, artist, title, playlist, served_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(track_id, playlist, served_at) DO NOTHING""",
            [(track["track_id"], track.get("video_id"), track.get("artist"),
              track.get("title"), playlist, served_at) for track in tracks],
        )
    return served_at


def unrecord_served(conn: sqlite3.Connection, playlist: str, served_at: str) -> None:
    """Drop one batch, for an export that certainly changed nothing."""
    with conn:
        conn.execute("DELETE FROM served_ledger WHERE playlist = ? AND served_at = ?", (playlist, served_at))


def record_known(conn: sqlite3.Connection, tracks: list[dict], source: str) -> None:
    """Remember recordings the user already knows (e.g. the tracks behind a seed)."""
    with conn:
        conn.executemany(
            """INSERT INTO known_tracks (track_id, video_id, artist, title, source, added_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(track_id) DO UPDATE SET video_id = COALESCE(excluded.video_id, video_id)""",
            [(t["track_id"], t.get("video_id"), t.get("artist"), t.get("title"), source, _now_iso())
             for t in tracks if t.get("track_id")],
        )


def served_keys(conn: sqlite3.Connection) -> set[str]:
    """Return every track and video ID ever served, across all playlists."""
    return {row[0] for row in conn.execute(
        "SELECT track_id FROM served_ledger UNION "
        "SELECT video_id FROM served_ledger WHERE video_id IS NOT NULL"
    )}


def library_sources(conn: sqlite3.Connection) -> list[dict]:
    """Library rows by where they came from: "file" for scanned files (and rows
    no importer claims), else the importer ("plex"), with the latest import
    time (UTC, SQLite format)."""
    rows = conn.execute(
        "SELECT CASE WHEN filename IS NOT NULL OR COALESCE(source, '') = '' THEN 'file' ELSE source END AS origin,"
        " COUNT(*), MAX(imported_at) FROM tracks GROUP BY origin ORDER BY COUNT(*) DESC, origin"
    ).fetchall()
    return [{"source": origin, "count": count, "imported_at": imported_at} for origin, count, imported_at in rows]


def cache_get(conn: sqlite3.Connection, key: str, now: float | None = None) -> str | None:
    """Read an unexpired body; now is an optional Unix timestamp for testing."""
    if now is None:
        now = time.time()
    row = conn.execute(
        "SELECT body FROM http_cache WHERE key = ? AND fetched_at + ttl_s > ?",
        (key, now),
    ).fetchone()
    return row[0] if row is not None else None


def cache_put(conn: sqlite3.Connection, key: str, body: str, ttl_s: int) -> None:
    """Replace a cached response and restart its TTL."""
    with conn:
        conn.execute(
            """INSERT INTO http_cache (key, body, fetched_at, ttl_s) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET body=excluded.body,
               fetched_at=excluded.fetched_at, ttl_s=excluded.ttl_s""",
            (key, body, time.time(), ttl_s),
        )


def job_start(conn: sqlite3.Connection, job: str, scheduled_for: str | None) -> int:
    """Persist a running job and return its run ID."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO job_runs (job, scheduled_for, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (job, scheduled_for, _now_iso()),
        )
    return cursor.lastrowid


def job_finish(conn: sqlite3.Connection, run_id: int, status: str, detail: str | None) -> None:
    """Finish a run; use status='success' for a successful completion."""
    with conn:
        conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (_now_iso(), status, detail, run_id),
        )


def last_success(conn: sqlite3.Connection, job: str) -> str | None:
    """Return the latest successful completion timestamp for a job, in UTC."""
    row = conn.execute(
        "SELECT finished_at FROM job_runs WHERE job = ? AND status = 'success' "
        "AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
        (job,),
    ).fetchone()
    return row[0] if row is not None else None


def playlist_id_get(conn: sqlite3.Connection, sink: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT playlist_id FROM sink_playlists WHERE sink = ? AND name = ?", (sink, name)
    ).fetchone()
    return row[0] if row is not None else None


def playlist_id_put(conn: sqlite3.Connection, sink: str, name: str, playlist_id: str,
                    updated_at: str | None = None) -> str:
    """Record the mapping, as of ``updated_at`` (default now); returns it."""
    updated_at = updated_at or _now_iso()
    with conn:
        conn.execute(
            """INSERT INTO sink_playlists (sink, name, playlist_id, updated_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(sink, name) DO UPDATE SET
               playlist_id=excluded.playlist_id, updated_at=excluded.updated_at""",
            (sink, name, playlist_id, updated_at),
        )
    return updated_at


# Slice writes deliberately differ from the legacy commit-owning helpers above:
# the caller owns BEGIN IMMEDIATE/commit/rollback, including worker admission.
# Abort that transaction on any repository error. No provider work belongs here.
class SliceConflict(ValueError):
    """A revision, replay cursor or immutable snapshot conflicts with a write."""


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=lambda model: model.to_dict())


def _slice_write(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("slice writes require a caller-owned write transaction")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.execute(sql, params)]


def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _step(conn: sqlite3.Connection, step_id: str) -> dict:
    step = _row(conn, "SELECT * FROM steps WHERE id=?", (step_id,))
    if step is None:
        raise ValueError("step does not exist")
    return step


def _points(origins: list[Point | dict]) -> list[Point]:
    points = [Point.model_validate(point.to_dict() if isinstance(point, Point) else point) for point in origins]
    if not 1 <= len(points) <= 10 or len({point.point_id for point in points}) != len(points):
        raise ValueError("origins must contain one to ten unique points")
    return points


def _requested_point(point: dict) -> dict:
    # library_source only words a seed-box result: origins an earlier build saved with it still match.
    return {key: value for key, value in point.items()
            if key not in ("resolved_tracks", "resolution", "library_source")}


def _step_context(points: list[Point], move: str, parent: dict | None, supplied: dict) -> dict:
    allowed = {"header_id", "header_params", "source_point", "source_selection_id", "these_scope", "requested_count"}
    if supplied.keys() - allowed:
        raise ValueError("unknown step context fields")
    scope = supplied.get("these_scope")
    if move == "seed":
        header_id = "S-HDR-SEED-1" if len(points) == 1 else "S-HDR-SEED-N"
    elif move == "these":
        header_id = "S-HDR-THESE-DIG" if scope == "dig" else "S-HDR-THESE-STEP"
    else:
        header_id = f"S-HDR-{move.upper()}"
    if move in ("seed", "track") and len(points) == 1:
        params = {"Artist - Title": points[0].name}
    elif move == "seed":
        params = {"n": len(points), **{f"s{i+1}": point.name for i, point in enumerate(points[:3])},
                  "k": max(0, len(points) - 3)}
    elif move == "these":
        params = {"k": len(points)}
        if scope == "step" and parent:
            params["n"] = parent["number"]
    else:
        params = {move.title(): points[0].name}
    source = None
    if parent:
        if move == "these":
            source = json.loads(parent["origins"])[0] | {"source_step_id": parent["id"], "source_rank": None}
        else:
            source = points[0].to_dict()
    result = {"header_id": header_id, "header_params": params, "source_point": source,
              "these_scope": scope, "requested_count": len(points) if move == "these" else None} | supplied
    if move == "these" and _positive_int(result["requested_count"], "requested_count") < len(points):
        raise ValueError("requested_count cannot be smaller than submitted keeps")
    return result


def _display_name(point: dict) -> str:
    """A pasted link reads as the track it found (P12); the link stays the point's identity and source."""
    tracks = point.get("resolved_tracks") or []
    if point.get("kind") == "link" and point.get("resolution") == "exact" and len(tracks) == 1:
        return f"{tracks[0]['artist']} - {tracks[0]['title']}" if tracks[0].get("artist") else tracks[0]["title"]
    return point["name"]


def _dig_name(names: list[str]) -> str:
    return names[0] + (f" + {len(names) - 1} more" if len(names) > 1 else "")


def create_dig(conn: sqlite3.Connection, seeds: list[Point | dict]) -> str:
    """Create metadata in the same caller transaction as its original root."""
    _slice_write(conn)
    points = _points(seeds)
    name = _dig_name([point.name for point in points])
    dig_id = uuid4().hex
    conn.execute("INSERT INTO digs (id,name,created_at) VALUES (?,?,?)", (dig_id, name, _now_iso()))
    return dig_id


def move_fingerprint(*, parent_id: str | None, move: str, further: bool,
                     origins: list[Point], these_scope: str | None = None) -> str:
    ids = [point.point_id for point in origins]
    body = {"parent_id": parent_id, "move": move, "further": further,
            "origin_point_ids": sorted(ids) if move == "these" else ids, "these_scope": these_scope}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_point_source(conn: sqlite3.Connection, *, parent_id: str, point: Point,
                          these_scope: str | None = None) -> None:
    """Validate provenance against saved picks, ready facts, hubs or origins."""
    from .slice_types import fact_key

    parent = _step(conn, parent_id)
    if not point.source_step_id:
        raise ValueError("branch point requires source_step_id")
    source = _step(conn, point.source_step_id)
    if source["dig_id"] != parent["dig_id"] or (these_scope != "dig" and source["id"] != parent_id):
        raise ValueError("point source does not belong to the parent")
    if point.source_rank is not None:
        pick = _row(conn, "SELECT * FROM step_picks WHERE step_id=? AND rank=?",
                    (source["id"], point.source_rank))
        if pick is None:
            raise ValueError("source rank does not belong to the step")
        if point.kind == "track":
            if (point.point_id != f"track:{pick['track_key']}" or point.track_key != pick["track_key"]
                    or (point.cosine_id is not None and point.cosine_id != pick["cosine_id"])
                    or point.name != (f"{pick['artist']} - {pick['title']}" if pick["artist"] else pick["title"])):
                raise ValueError("track point does not match the source rank")
            return
        # The recording's known artist is a valid name-only artist source;
        # resolution still has to find an exact Discogs identity in S3.
        if (point.kind == "artist" and point.discogs_id is None and point.name == pick["artist"]
                and point.point_id == fact_key("artist", None, pick["artist"])):
            return
        fields = {"label": "labels", "artist": "artists", "person": "credits"}
        facts = json.loads(pick["facts"]) if pick["facts_state"] == "ready" else {}
        for entity in facts.get(fields.get(point.kind), []):
            if (point.point_id == fact_key(point.kind, entity.get("id"), entity["name"])
                    and point.discogs_id == entity.get("id") and point.name == entity["name"]):
                return
        raise ValueError("point is not a verified fact of the source rank")
    hub = _row(conn, "SELECT * FROM step_hubs WHERE step_id=? AND hub_key=?",
               (source["id"], point.point_id))
    if hub and point.kind == hub["kind"] and point.discogs_id == hub["discogs_id"] and point.name == hub["name"]:
        return
    for origin in json.loads(source["origins"]):
        if (point.kind == "artist" and point.discogs_id is None and point.name == origin.get("artist")
                and point.point_id == fact_key("artist", None, point.name)):
            return
        if all(point.to_dict()[key] == origin.get(key) for key in
               ("point_id", "kind", "name", "track_key", "cosine_id", "discogs_id", "library_id")):
            return
    raise ValueError("point is not a saved hub or origin")


def _insert_slice_job(conn: sqlite3.Connection, *, job_id: str, kind: str,
                      step_id: str | None, attempt: int, progress: dict, at: str) -> None:
    conn.execute("""INSERT INTO slice_jobs (id,kind,step_id,attempt,state,started_at,progress)
                    VALUES (?,?,?,?,'running',?,?)""",
                 (job_id, kind, step_id, attempt, at, canonical_json(progress)))


def create_step(conn: sqlite3.Connection, *, dig_id: str, parent_id: str | None,
                origins: list[Point | dict], move: str, further: bool,
                rerun_of: str | None = None, reuse_empty_step_id: str | None = None,
                context: dict | None = None) -> StepIdentity:
    """Allocate/deduplicate one step and attempt; api.record_step delegates here."""
    _slice_write(conn)
    points = _points(origins)
    if move not in ("seed", "track", "label", "artist", "person", "these") or type(further) is not bool:
        raise ValueError("invalid move or further flag")
    if move not in ("seed", "these") and len(points) != 1:
        raise ValueError("branch moves have exactly one origin")
    if move not in ("seed", "these") and points[0].kind != move:
        raise ValueError("branch point kind must match its move")
    if move == "these" and any(point.kind != "track" for point in points):
        raise ValueError("these origins must be tracks")
    if not _row(conn, "SELECT id FROM digs WHERE id=?", (dig_id,)):
        raise ValueError("dig does not exist")
    parent = _step(conn, parent_id) if parent_id is not None else None
    if parent and parent["dig_id"] != dig_id:
        raise ValueError("parent must belong to the same dig")
    # Detect malformed legacy/manual slice relationships as well as cross-dig
    # parents. Fresh UUIDs cannot point to themselves or to their descendants.
    seen = set()
    ancestor = parent
    while ancestor:
        if ancestor["id"] in seen or ancestor["dig_id"] != dig_id:
            raise ValueError("invalid cyclic or cross-dig ancestry")
        seen.add(ancestor["id"])
        ancestor = _step(conn, ancestor["parent_id"]) if ancestor["parent_id"] else None
    original_id = rerun_of or reuse_empty_step_id
    if rerun_of and reuse_empty_step_id:
        raise ValueError("cannot combine re-run and Retry")
    original = _step(conn, original_id) if original_id else None
    if original:
        if (original["dig_id"] != dig_id or original["parent_id"] != parent_id
                or original["move"] != move or bool(original["further"]) != further
                or [_requested_point(p) for p in json.loads(original["origins"])] !=
                [_requested_point(p.to_dict()) for p in points]):
            raise ValueError("re-run or Retry must match the original move")
        if original["status"] == "running":
            raise SliceConflict("the original step is still running")
        if rerun_of and original["status"] != "done" and not _row(
            conn, "SELECT rank FROM step_picks WHERE step_id=?", (original_id,)
        ):
            raise SliceConflict("empty failed or stopped steps require Retry")
        if context is None:
            context = json.loads(original["context"])
    context = dict(context or {})
    scope = context.get("these_scope")
    if scope not in (None, "step", "dig") or (move == "these" and scope is None):
        raise ValueError("invalid these scope")
    if move != "these" and scope is not None:
        raise ValueError("these scope applies only to keeps")
    if (parent is None) != (move == "seed"):
        raise ValueError("seed moves are roots; other moves require a parent")
    context = _step_context(points, move, parent, context)
    if parent:
        for point in points:
            validate_point_source(conn, parent_id=parent_id, point=point, these_scope=scope)
            if move == "these" and not original and effective_judgment(conn, point.track_key)["verdict"] != "keep":
                raise ValueError("these origins must be effective keeps")
    fingerprint = move_fingerprint(parent_id=parent_id, move=move, further=further,
                                   origins=points, these_scope=scope)
    if original and fingerprint != original["fingerprint"]:
        raise ValueError("re-run or Retry scope must match the original")
    if not original:
        duplicate = _row(conn, """SELECT * FROM steps WHERE dig_id=? AND parent_id IS ?
                                  AND fingerprint=? ORDER BY number LIMIT 1""",
                         (dig_id, parent_id, fingerprint))
        if duplicate:
            return StepIdentity(dig_id=dig_id, step_id=duplicate["id"], number=duplicate["number"],
                                job_id=duplicate["current_job_id"], created=False)
    steps = _rows(conn, "SELECT * FROM steps WHERE dig_id=? ORDER BY number", (dig_id,))
    roots = [step for step in steps if step["parent_id"] is None]
    if parent is None and roots and not original:
        raise ValueError("additional roots require Re-run")
    at, job_id = _now_iso(), "sl-" + uuid4().hex
    if reuse_empty_step_id:
        if original["status"] not in ("failed", "stopped") or _row(
            conn, "SELECT rank FROM step_picks WHERE step_id=?", (original_id,)
        ) or _row(conn, "SELECT hub_key FROM step_hubs WHERE step_id=?", (original_id,)):
            raise SliceConflict("Retry requires an empty failed or stopped step")
        # Older code admitted siblings of retryable empty originals. Freeze any
        # older terminal diff before this Retry can change its source snapshot.
        for sibling in _rows(conn, """SELECT s.id,j.id AS job_id,j.progress FROM steps s
                                      JOIN slice_jobs j ON j.id=s.current_job_id
                                      WHERE s.rerun_of=? AND j.state!='running'""", (original_id,)):
            progress = json.loads(sibling["progress"])
            if "diff" not in progress:
                progress["diff"] = _rerun_diff(conn, original_id, _pick_values(conn, sibling["id"]))
                conn.execute("UPDATE slice_jobs SET progress=? WHERE id=?",
                             (canonical_json(progress), sibling["job_id"]))
        attempt = _row(conn, "SELECT MAX(attempt) AS n FROM slice_jobs WHERE step_id=?", (original_id,))["n"] + 1
        conn.execute("""UPDATE steps SET origins=?,context=?,status='running',note='[]',error=NULL,
                        started_at=?,finished_at=NULL,picks_done_at=NULL,current_job_id=? WHERE id=?""",
                     (canonical_json(points), canonical_json(context), at, job_id, original_id))
        step_id, number, created = original_id, original["number"], False
    else:
        number = max((step["number"] for step in steps), default=0) + 1
        step_id, created, attempt = uuid4().hex, True, 1
        if not roots and parent is None:
            x, y = 0.0, 0.0
        else:
            anchor = (parent["island_x"], parent["island_y"]) if parent else (roots[0]["island_x"] - 420, roots[0]["island_y"])
            grand = _step(conn, parent["parent_id"]) if parent and parent["parent_id"] else None
            x, y = island_position(parent=anchor, grandparent=(grand["island_x"], grand["island_y"]) if grand else None,
                                   sibling_index=sum(step["parent_id"] == parent_id for step in steps),
                                   existing=[(step["island_x"], step["island_y"]) for step in steps])
        conn.execute("""INSERT INTO steps (id,dig_id,number,parent_id,rerun_of,origins,context,
                        move,further,status,started_at,current_job_id,fingerprint,layout_version,island_x,island_y)
                        VALUES (?,?,?,?,?,?,?,?,?,'running',?,?,?,2,?,?)""",
                     (step_id, dig_id, number, parent_id, rerun_of, canonical_json(points), canonical_json(context),
                      move, int(further), at, job_id, fingerprint, x, y))
    _insert_slice_job(conn, job_id=job_id, kind="step", step_id=step_id, attempt=attempt, progress={}, at=at)
    return StepIdentity(dig_id=dig_id, step_id=step_id, number=number, job_id=job_id, created=created)


def _running_job(conn: sqlite3.Connection, job_id: str) -> tuple[dict, dict | None]:
    job = _row(conn, "SELECT * FROM slice_jobs WHERE id=?", (job_id,))
    if not job:
        raise ValueError("slice job does not exist")
    if job["state"] != "running":
        raise SliceConflict("terminal jobs are immutable")
    step = _step(conn, job["step_id"]) if job["step_id"] else None
    if step and (step["current_job_id"] != job_id or step["status"] != "running"):
        raise SliceConflict("job is not the step's active attempt")
    return job, step


def resolve_step_origins(conn: sqlite3.Connection, step_id: str, origins: list[Point | dict]) -> None:
    _slice_write(conn)
    step = _step(conn, step_id)
    _running_job(conn, step["current_job_id"])
    points = _points(origins)
    if [_requested_point(point.to_dict()) for point in points] != [_requested_point(point) for point in json.loads(step["origins"])]:
        raise ValueError("resolution cannot replace requested points")
    if step["picks_done_at"] or _row(conn, "SELECT rank FROM step_picks WHERE step_id=?", (step_id,)):
        raise SliceConflict("origins must resolve before the first pick")
    conn.execute("UPDATE steps SET origins=? WHERE id=?", (canonical_json(points), step_id))
    if step["parent_id"] is None:
        # A dig named after a pasted link takes the name of the track the link found (P12).
        requested = [point["name"] for point in json.loads(step["origins"])]
        conn.execute("UPDATE digs SET name=? WHERE id=? AND name=?",
                     (_dig_name([_display_name(point.to_dict()) for point in points]), step["dig_id"],
                      _dig_name(requested)))


def create_enrichment_job(conn: sqlite3.Connection, *, progress: dict | None = None,
                          resume_job_id: str | None = None) -> str:
    _slice_write(conn)
    active = _row(conn, "SELECT id FROM slice_jobs WHERE kind='label_enrichment' AND state='running'")
    if active:
        return active["id"]
    progress = dict(progress or {})
    attempt = 1
    if resume_job_id:
        previous = get_slice_job(conn, resume_job_id)
        if previous["kind"] != "label_enrichment" or previous["state"] == "running":
            raise ValueError("resume requires a terminal enrichment job")
        progress["work"] = (previous["progress"].get("work", {}) | progress.get("work", {})
                            | {"resume_job_id": resume_job_id})
        attempt = previous["attempt"] + 1
    job_id = "sl-" + uuid4().hex
    _insert_slice_job(conn, job_id=job_id, kind="label_enrichment", step_id=None, attempt=attempt,
                      progress=progress, at=_now_iso())
    return job_id


def update_slice_progress(conn: sqlite3.Connection, job_id: str, progress: dict, *, redact: Callable) -> None:
    """Checkpoints retain the reserved idempotency request, even after work updates."""
    _slice_write(conn)
    job, _ = _running_job(conn, job_id)
    current = json.loads(job["progress"])
    progress = redact(progress)
    if "request" in current and "request" in progress and progress["request"] != current["request"]:
        raise SliceConflict("request identity is immutable")
    conn.execute("UPDATE slice_jobs SET progress=? WHERE id=?",
                 (canonical_json(current | progress), job_id))


def _pick_values(conn: sqlite3.Connection, step_id: str) -> list[dict]:
    values = []
    for row in _rows(conn, "SELECT * FROM step_picks WHERE step_id=? ORDER BY rank", (step_id,)):
        row["id"] = f"{step_id}:{row['rank']}"
        row["route"] = json.loads(row["route"])
        row["facts"] = json.loads(row["facts"]) if row["facts"] else None
        row["layout"] = {"radius": row.pop("radius"), "angle": row.pop("angle")}
        values.append(row)
    return values


def _hub_values(conn: sqlite3.Connection, step_id: str) -> list[dict]:
    values = []
    for row in _rows(conn, "SELECT * FROM step_hubs WHERE step_id=? ORDER BY kind,hub_key", (step_id,)):
        row.pop("step_id")
        row["pick_ranks"] = json.loads(row["pick_ranks"])
        row["layout"] = {"radius": row.pop("radius"), "angle": row.pop("angle")}
        values.append(row)
    return values


def _step_batches(conn: sqlite3.Connection, job_id: str, picks: list[dict]) -> list[dict]:
    batches = []
    for row in _rows(conn, "SELECT payload FROM slice_events WHERE job_id=? AND event='batch' ORDER BY seq", (job_id,)):
        event = json.loads(row["payload"])
        batches.append({"hop": event["hop"], "start_rank": event["start_rank"], "label": event["label"],
                        "count": sum(pick["batch"] == event["hop"] for pick in picks)})
    return batches


def _facts_progress(picks: list[dict]) -> dict:
    return {"completed": sum(pick["facts_state"] != "loading" for pick in picks), "total": len(picks)}


def _rerun_diff(conn: sqlite3.Connection, original_id: str, picks: list[dict]) -> dict:
    original = _pick_values(conn, original_id)
    old, new = {pick["track_key"] for pick in original}, {pick["track_key"] for pick in picks}
    return {"same": len(old & new), "new": len(new - old), "gone": len(old - new),
            "new_track_keys": [pick["track_key"] for pick in picks if pick["track_key"] not in old],
            "gone_picks": [pick for pick in original if pick["track_key"] not in new]}


def _positive_int(value, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _persist_pick(conn: sqlite3.Connection, step: dict, data: dict) -> dict:
    from .api import _key

    if step["picks_done_at"]:
        raise SliceConflict("pick generation has finished")
    raw = dict(data["pick"])
    rank = _positive_int(raw["rank"], "rank")
    count = conn.execute("SELECT COUNT(*) FROM step_picks WHERE step_id=?", (step["id"],)).fetchone()[0]
    if rank != count + 1:
        raise ValueError("pick ranks must be contiguous and append-only")
    batches = _rows(conn, "SELECT payload FROM slice_events WHERE job_id=? AND event='batch' ORDER BY seq",
                    (step["current_job_id"],))
    batch = json.loads(batches[-1]["payload"]) if batches else None
    if batch is None or batch["hop"] != raw["batch"]:
        raise ValueError("pick must belong to the current batch")
    floor = batch.get("score_floor", 0.0)
    if floor is None:
        raise ValueError("score_floor cannot be null for a batch with picks")
    layout = pick_layout(step["id"], rank, raw["score"], _points(json.loads(step["origins"])),
                         raw["route"].get("layout_seed"), score_floor=floor,
                         layout_version=step["layout_version"]).to_dict()
    expected = {"id": f"{step['id']}:{rank}", "step_id": step["id"], "layout": layout}
    if any(key in raw and raw[key] != value for key, value in expected.items()):
        raise ValueError("pick identity or saved layout does not match the step")
    pick = StepPick.model_validate(raw | expected).to_dict()
    if pick["track_key"] != _key(pick["artist"], pick["title"]):
        raise ValueError("track_key must be the canonical recording identity")
    conn.execute("""INSERT INTO step_picks (step_id,rank,track_key,cosine_id,artist,title,video_id,
                    score,batch,route,facts,facts_state,radius,angle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (step["id"], rank, pick["track_key"], pick["cosine_id"], pick["artist"], pick["title"],
                  pick["video_id"], pick["score"], pick["batch"], canonical_json(pick["route"]),
                  canonical_json(pick["facts"]) if pick["facts"] is not None else None,
                  pick["facts_state"], layout["radius"], layout["angle"]))
    return {"pick": pick}


def _persist_facts(conn: sqlite3.Connection, step: dict, data: dict) -> dict:
    from .slice_types import Facts

    rank = _positive_int(data["rank"], "rank")
    pick = _row(conn, "SELECT facts_state FROM step_picks WHERE step_id=? AND rank=?", (step["id"], rank))
    if not pick:
        raise ValueError("fact rank does not belong to the step")
    if pick["facts_state"] != "loading":
        raise SliceConflict("facts have already settled")
    state, facts = data["facts_state"], data.get("facts")
    if state not in ("ready", "missing", "unavailable", "no_token") or (state == "ready") != (facts is not None):
        raise ValueError("invalid terminal facts state or payload")
    facts = Facts.model_validate(facts).to_dict() if facts is not None else None
    conn.execute("UPDATE step_picks SET facts_state=?,facts=? WHERE step_id=? AND rank=?",
                 (state, canonical_json(facts) if facts is not None else None, step["id"], rank))
    counts = _row(conn, """SELECT COUNT(*) AS total,SUM(facts_state!='loading') AS completed
                          FROM step_picks WHERE step_id=?""", (step["id"],))
    return {"rank": rank, "facts_state": state, "facts": facts, **counts}


def _persist_hub(conn: sqlite3.Connection, step: dict, data: dict) -> dict:
    hub = Hub.model_validate(data["hub"]).to_dict()
    expected = {value.hub_key: value.to_dict() for value in
                build_hubs(_pick_values(conn, step["id"]), _hub_values(conn, step["id"]))}
    if expected.get(hub["hub_key"]) != hub:
        raise ValueError("hub must match committed ready shared facts and saved geometry")
    conn.execute("""INSERT INTO step_hubs (step_id,hub_key,kind,name,discogs_id,pick_ranks,radius,angle)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(step_id,hub_key)
                    DO UPDATE SET pick_ranks=excluded.pick_ranks""",
                 (step["id"], hub["hub_key"], hub["kind"], hub["name"], hub["discogs_id"],
                  canonical_json(hub["pick_ranks"]), hub["layout"]["radius"], hub["layout"]["angle"]))
    return {"hub": hub}


def append_slice_event(conn: sqlite3.Connection, job_id: str, event: str, data: dict, *, redact: Callable) -> dict:
    """Apply state and append its canonical journal row in the caller transaction.

    The runtime must commit before notifying SSE waiters. Its redactor is
    mandatory: safe emitted bytes and safe persisted state come from one input.
    """
    _slice_write(conn)
    job, step = _running_job(conn, job_id)
    data = redact(json.loads(canonical_json(data)))
    if not isinstance(data, dict) or {"job_id", "step_id", "seq", "at"} & data.keys():
        raise ValueError("event data cannot override journal identity")
    at = _now_iso()
    if event in ("pick", "batch", "facts", "hub", "picks_done") and not step:
        raise ValueError("this event requires a step job")
    if event == "pick":
        data = _persist_pick(conn, step, data)
    elif event == "facts":
        data = _persist_facts(conn, step, data)
    elif event == "hub":
        data = _persist_hub(conn, step, data)
    elif event == "batch":
        hop = _positive_int(data["hop"], "hop", minimum=0)
        start = _positive_int(data["start_rank"], "start_rank")
        target = _positive_int(data["target_count"], "target_count", minimum=0)
        prior = [json.loads(row["payload"])["hop"] for row in _rows(
            conn, "SELECT payload FROM slice_events WHERE job_id=? AND event='batch' ORDER BY seq", (job_id,)
        )]
        count = conn.execute("SELECT COUNT(*) FROM step_picks WHERE step_id=?", (step["id"],)).fetchone()[0]
        if (step["picks_done_at"] or hop != len(prior) or hop > int(step["further"])
                or start != count + 1 or data["label"] != ("near" if hop == 0 else "further")):
            raise ValueError("invalid or repeated batch boundary")
        # Pre-O14 writers can omit the floor and use the full score range.
        # run_move always supplies the actual admitted batch minimum.
        floor = data.get("score_floor", 0.0)
        if floor is not None and (type(floor) not in (int, float) or not math.isfinite(floor)
                                  or not 0 <= floor <= 1):
            raise ValueError("score_floor must be null or finite and between zero and one")
        data = {"hop": hop, "start_rank": start, "target_count": target, "label": data["label"],
                "score_floor": floor}
    elif event == "note":
        if data.get("phase") not in ("resolve", "near", "further", "facts", "waiting", "enrichment"):
            raise ValueError("invalid note phase")
        if step:
            notes = json.loads(step["note"]) + [data]
            conn.execute("UPDATE steps SET note=? WHERE id=?", (canonical_json(notes), step["id"]))
        if "progress" in data:
            work = json.loads(job["progress"]).get("work", {}) | data["progress"]
            update_slice_progress(conn, job_id, {"work": work}, redact=redact)
    elif event == "picks_done":
        picks = _pick_values(conn, step["id"])
        if step["picks_done_at"]:
            raise SliceConflict("picks_done is emitted once per attempt")
        if _positive_int(data["pick_count"], "pick_count", minimum=0) != len(picks):
            raise ValueError("picks_done count does not match committed picks")
        data = {"pick_count": len(picks), "batches": _step_batches(conn, job_id, picks), "facts": _facts_progress(picks)}
        conn.execute("UPDATE steps SET picks_done_at=? WHERE id=?", (at, step["id"]))
    elif event in ("done", "error"):
        status = data.get("status")
        if status not in (("done", "stopped") if event == "done" else ("failed", "stopped")):
            raise ValueError("invalid terminal status")
        if step:
            picks, hubs = _pick_values(conn, step["id"]), _hub_values(conn, step["id"])
            if any(pick["facts_state"] == "loading" for pick in picks):
                raise SliceConflict("terminal events require settled facts")
            if {hub["hub_key"]: hub for hub in hubs} != {hub.hub_key: hub.to_dict() for hub in build_hubs(picks, hubs)}:
                raise SliceConflict("terminal events require committed shared hubs")
            if event == "done" and not step["picks_done_at"]:
                raise SliceConflict("done requires picks_done")
            if _positive_int(data["pick_count"], "pick_count", minimum=0) != len(picks):
                raise ValueError("terminal count does not match committed picks")
            data["snapshot_url"] = f"/api/steps/{step['id']}"
            if event == "done":
                data.update(batches=_step_batches(conn, job_id, picks), facts=_facts_progress(picks), hub_count=len(hubs))
                data.setdefault("warning_codes", [])
            if step["rerun_of"]:
                progress = json.loads(job["progress"]) | {"diff": _rerun_diff(conn, step["rerun_of"], picks)}
                conn.execute("UPDATE slice_jobs SET progress=? WHERE id=?", (canonical_json(progress), job_id))
            conn.execute("UPDATE steps SET status=?,finished_at=?,error=? WHERE id=?",
                         (status, at, canonical_json(data["error"]) if event == "error" else None, step["id"]))
        state = "stopped" if status == "stopped" else ("done" if event == "done" else "error")
        conn.execute("UPDATE slice_jobs SET state=?,finished_at=?,result=?,error=? WHERE id=?",
                     (state, at, canonical_json(data) if event == "done" else None,
                      canonical_json(data["error"]) if event == "error" else None, job_id))
    else:
        raise ValueError("unknown slice event")
    payload = {"job_id": job_id, "step_id": job["step_id"], "seq": job["last_seq"] + 1, "at": at, **data}
    conn.execute("INSERT INTO slice_events (job_id,seq,event,payload,at) VALUES (?,?,?,?,?)",
                 (job_id, payload["seq"], event, canonical_json(payload), at))
    conn.execute("UPDATE slice_jobs SET last_seq=? WHERE id=?", (payload["seq"], job_id))
    return payload


def get_slice_job(conn: sqlite3.Connection, job_id: str) -> dict:
    job = _row(conn, """SELECT id,kind,state,step_id,attempt,last_seq,progress,result,error
                        FROM slice_jobs WHERE id=?""", (job_id,))
    if not job:
        raise ValueError("slice job does not exist")
    for key in ("progress", "result", "error"):
        job[key] = json.loads(job[key]) if job[key] is not None else None
    return job


def slice_events(conn: sqlite3.Connection, job_id: str, *, after: int = 0,
                 last_event_id: str | None = None) -> list[dict]:
    """Return committed journal rows in cursor order, including terminal replay."""
    after = _positive_int(after, "after", minimum=0)
    if last_event_id is not None:
        prefix, separator, seq = last_event_id.rpartition(":")
        if prefix != job_id or not separator or not seq.isascii() or not seq.isdecimal():
            raise ValueError("malformed or foreign Last-Event-ID")
        after = max(after, int(seq))
    if after > get_slice_job(conn, job_id)["last_seq"]:
        raise SliceConflict("cursor is ahead of the journal")
    return [{"event": row["event"], "id": f"{job_id}:{row['seq']}", "data": json.loads(row["payload"])}
            for row in _rows(conn, "SELECT seq,event,payload FROM slice_events WHERE job_id=? AND seq>? ORDER BY seq",
                             (job_id, after))]


def legacy_judgments(conn: sqlite3.Connection) -> dict[str, dict]:
    """Read the canonical latest save/pass once for a collection of tracks."""
    from .api import _key

    judgments = {}
    for row in _rows(conn, """SELECT e.event_type,e.created_at,r.session_id,r.artist,r.track_name
                             FROM result_events e JOIN session_results r ON r.id=e.result_id
                             WHERE e.event_type IN ('save','pass') ORDER BY e.created_at DESC,e.id DESC"""):
        key = _key(row["artist"] or "", row["track_name"] or "")
        if key not in judgments:
            judgments[key] = {"verdict": "keep" if row["event_type"] == "save" else "pass", "step_id": None,
                              "at": row["created_at"], "source": "legacy", "legacy_session_id": row["session_id"]}
    return judgments


def effective_judgment(conn: sqlite3.Connection, track_key: str, *, legacy: dict | None = None) -> dict:
    """GUI verdict, explicit suppression, then latest legacy save/pass."""
    gui = _row(conn, "SELECT verdict,step_id,at FROM judgments WHERE track_key=?", (track_key,))
    if gui:
        return {**gui, "source": "gui", "legacy_session_id": None}
    control = _row(conn, "SELECT suppress_legacy FROM judgment_controls WHERE track_key=?", (track_key,))
    if not control or not control["suppress_legacy"]:
        if legacy is None:
            legacy = legacy_judgments(conn)
        if track_key in legacy:
            return legacy[track_key]
    return {"verdict": None, "step_id": None, "at": None, "source": "none", "legacy_session_id": None}


def _track_membership(conn: sqlite3.Connection, track_key: str, step_id: str) -> None:
    _step(conn, step_id)
    if not _row(conn, "SELECT rank FROM step_picks WHERE step_id=? AND track_key=?", (step_id, track_key)):
        raise ValueError("track does not belong to the supplied step")


def change_judgment(conn: sqlite3.Connection, *, track_key: str, step_id: str,
                    expected_revision: int, verdict: str | None = None,
                    undo_change_id: str | None = None) -> dict:
    """PUT, toggle-off, or revision-checked undo; never edits legacy events."""
    _slice_write(conn)
    _track_membership(conn, track_key, step_id)
    _positive_int(expected_revision, "expected_revision", minimum=0)
    if verdict not in (None, "keep", "pass") or (undo_change_id and verdict is not None):
        raise ValueError("invalid judgment operation")
    control = _row(conn, "SELECT * FROM judgment_controls WHERE track_key=?", (track_key,))
    revision = control["revision"] if control else 0
    if revision != expected_revision:
        raise SliceConflict("This judgment changed elsewhere; try again.")
    before = _row(conn, "SELECT * FROM judgments WHERE track_key=?", (track_key,))
    suppression = bool(control["suppress_legacy"]) if control else False
    history = json.loads(control["history"]) if control else []
    effective = effective_judgment(conn, track_key)
    at, change_id = _now_iso(), None
    if undo_change_id is not None:
        latest = next((change for change in reversed(history) if not change["undone"]), None)
        if not latest or latest["change_id"] != undo_change_id:
            raise SliceConflict("only the most recent applied change can be undone")
        restore, suppression = latest["before_gui"], latest["before_suppress_legacy"]
        if restore:
            _track_membership(conn, track_key, restore["step_id"])
        conn.execute("DELETE FROM judgments WHERE track_key=?", (track_key,))
        if restore:
            conn.execute("INSERT INTO judgments (track_key,verdict,step_id,at) VALUES (?,?,?,?)",
                         (track_key, restore["verdict"], restore["step_id"], restore["at"]))
        latest["undone"] = True
        change_id = undo_change_id
    elif effective["verdict"] == verdict:
        return {"track_key": track_key, "effective": effective, "revision": revision, "change_id": None}
    else:
        change_id = uuid4().hex
        history.append({"change_id": change_id, "before_gui": before,
                        "before_suppress_legacy": suppression, "after_revision": revision + 1, "undone": False})
        conn.execute("DELETE FROM judgments WHERE track_key=?", (track_key,))
        if verdict is not None:
            conn.execute("INSERT INTO judgments (track_key,verdict,step_id,at) VALUES (?,?,?,?)",
                         (track_key, verdict, step_id, at))
        suppression = verdict is None
    revision += 1
    conn.execute("""INSERT INTO judgment_controls (track_key,revision,suppress_legacy,history,at)
                    VALUES (?,?,?,?,?) ON CONFLICT(track_key) DO UPDATE SET revision=excluded.revision,
                    suppress_legacy=excluded.suppress_legacy,history=excluded.history,at=excluded.at""",
                 (track_key, revision, int(suppression), canonical_json(history), at))
    return {"track_key": track_key, "effective": effective_judgment(conn, track_key),
            "revision": revision, "change_id": change_id}


def record_heard(conn: sqlite3.Connection, *, track_key: str, step_id: str) -> dict:
    _slice_write(conn)
    _track_membership(conn, track_key, step_id)
    conn.execute("""INSERT INTO heard (track_key,first_step_id,at) VALUES (?,?,?)
                    ON CONFLICT(track_key) DO NOTHING""", (track_key, step_id, _now_iso()))
    return _row(conn, "SELECT * FROM heard WHERE track_key=?", (track_key,))


@contextmanager
def _slice_read(conn: sqlite3.Connection) -> Iterator[None]:
    """Read snapshot, overlays and cursor from a single SQLite snapshot."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if own:
            conn.rollback()  # End only the read transaction this helper owns.


def _step_header(conn: sqlite3.Connection, step: dict) -> str:
    points, move = step["origins"], step["move"]
    name = _display_name(points[0])
    if move == "seed" and len(points) > 1:
        names = ", ".join(_display_name(point) for point in points[:3])
        if len(points) > 3:
            names += f" and {len(points) - 3} more"
        header = f"Sounds like {len(points)} seeds: {names}"
    elif move in ("seed", "track"):
        header = f"Sounds like {name}"
    elif move in ("label", "artist"):
        header = f"Sounds like the {move} {name}"
    elif move == "person":
        header = f"Sounds like records {name} is credited on"
    elif step["context"].get("these_scope") == "dig":
        header = f"Sounds like your {len(points)} {'keep' if len(points) == 1 else 'keeps'} in this dig"
    else:
        parent = _step(conn, step["parent_id"])
        header = f"Sounds like your {len(points)} {'keep' if len(points) == 1 else 'keeps'} in step {parent['number']}"
    return header + (", one step further" if step["further"] else "")


def _step_value(conn: sqlite3.Connection, row: dict) -> dict:
    step = {key: value for key, value in row.items() if key not in ("fingerprint", "current_job_id")}
    for key in ("origins", "context", "note", "error"):
        step[key] = json.loads(step[key]) if step[key] is not None else None
    step["further"] = bool(step["further"])
    step["header"] = _step_header(conn, step)
    return step


def get_step_content(conn: sqlite3.Connection, step_id: str) -> dict:
    """Read runtime step data and picks/hubs without live overlays or history."""
    with _slice_read(conn):
        return {"step": _step_value(conn, _step(conn, step_id)),
                "picks": _pick_values(conn, step_id), "hubs": _hub_values(conn, step_id)}


def get_step_snapshot(conn: sqlite3.Connection, step_id: str) -> dict:
    """Frozen snapshot is separate from global live marks and the event cursor."""
    with _slice_read(conn):
        row = _step(conn, step_id)
        step = _step_value(conn, row)
        picks, hubs = _pick_values(conn, step_id), _hub_values(conn, step_id)
        job = get_slice_job(conn, row["current_job_id"])
        batches = _step_batches(conn, job["id"], picks)
        diff = None
        if row["rerun_of"]:
            if job["state"] != "running":
                diff = job["progress"].get("diff")
            if diff is None:
                diff = _rerun_diff(conn, row["rerun_of"], picks)
        live = {"judgments": {}, "heard": {}, "also_in": {}, "rerun_steps": []}
        legacy = legacy_judgments(conn) if picks else {}
        for pick in picks:
            key = pick["track_key"]
            control = _row(conn, "SELECT revision FROM judgment_controls WHERE track_key=?", (key,))
            live["judgments"][key] = {**effective_judgment(conn, key, legacy=legacy), "revision": control["revision"] if control else 0}
            heard = _row(conn, "SELECT * FROM heard WHERE track_key=?", (key,))
            if heard:
                live["heard"][key] = heard
            appearances = _rows(conn, """SELECT d.id AS dig_id,d.name AS dig_name,s.id AS step_id,
                                         s.number,p.rank,s.* FROM step_picks p JOIN steps s ON s.id=p.step_id
                                         JOIN digs d ON d.id=s.dig_id WHERE p.track_key=?
                                         ORDER BY d.created_at,d.id,s.number""", (key,))
            live["also_in"][key] = [{**{key: appearance[key] for key in
                                       ("dig_id", "dig_name", "step_id", "number", "rank")},
                                      "header": _step_value(conn, appearance)["header"]}
                                     for appearance in appearances if appearance["step_id"] not in (step_id, row["rerun_of"])]
        live["rerun_steps"] = [{"step_id": sibling["id"], "number": sibling["number"]}
                               for sibling in _rows(conn, "SELECT id,number FROM steps WHERE rerun_of=? ORDER BY number", (step_id,))]
        return {"snapshot": {"step": step, "picks": picks, "hubs": hubs, "batches": batches, "diff": diff},
                "live": live, "job": {"id": job["id"], "state": job["state"], "attempt": job["attempt"], "cursor": job["last_seq"]}}


def get_dig(conn: sqlite3.Connection, dig_id: str, *, legacy: dict | None = None) -> dict:
    with _slice_read(conn):
        dig = _row(conn, "SELECT * FROM digs WHERE id=?", (dig_id,))
        if not dig:
            raise ValueError("dig does not exist")
        rows = _rows(conn, "SELECT * FROM steps WHERE dig_id=? ORDER BY number", (dig_id,))
        dig["root_step_id"] = next((row["id"] for row in rows if row["parent_id"] is None), None)
        dig["last_step_id"] = rows[-1]["id"] if rows else None
        dig["step_count"] = len(rows)
        kept = set()
        dig["steps"] = []
        if legacy is None:
            legacy = legacy_judgments(conn) if rows else {}
        for row in rows:
            picks = _pick_values(conn, row["id"])
            verdicts = {pick["track_key"]: effective_judgment(conn, pick["track_key"], legacy=legacy)["verdict"] for pick in picks}
            kept.update(key for key, verdict in verdicts.items() if verdict == "keep")
            dig["steps"].append({**_step_value(conn, row), "current_job_id": row["current_job_id"],
                                 "counts": {"picks": len(picks), "kept": sum(v == "keep" for v in verdicts.values()),
                                            "passed": sum(v == "pass" for v in verdicts.values())}})
        dig["kept_count"] = len(kept)
        return dig


def list_digs(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    _positive_int(limit, "limit")
    with _slice_read(conn):
        rows = _rows(conn, "SELECT id FROM digs ORDER BY created_at DESC,id LIMIT ?", (limit,))
        legacy = legacy_judgments(conn) if rows else {}
        return [{key: value for key, value in get_dig(conn, row["id"], legacy=legacy).items() if key != "steps"}
                for row in rows]


def dig_queue(conn: sqlite3.Connection, dig_id: str) -> dict:
    """The dig as one listening queue: every step in number order, picks in
    rank order, each track once. Passed tracks are counted, not queued.
    ``at`` is taken before the read: picks finished after it may be missing."""
    at = _now_iso()
    with _slice_read(conn):
        dig = _row(conn, "SELECT * FROM digs WHERE id=?", (dig_id,))
        if not dig:
            raise ValueError("dig does not exist")
        rows = _rows(conn, """SELECT p.track_key,p.cosine_id,p.artist,p.title,p.video_id,p.score
                              FROM step_picks p JOIN steps s ON s.id=p.step_id
                              WHERE s.dig_id=? ORDER BY s.number,p.rank""", (dig_id,))
        legacy = legacy_judgments(conn) if rows else {}
        seen, tracks, passed = set(), [], 0
        for row in rows:
            if row["track_key"] in seen:
                continue
            seen.add(row["track_key"])
            if effective_judgment(conn, row["track_key"], legacy=legacy)["verdict"] == "pass":
                passed += 1
            else:
                tracks.append(row)
        return {"dig": dig, "tracks": tracks, "passed": passed, "at": at}
