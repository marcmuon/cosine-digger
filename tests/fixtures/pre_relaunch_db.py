"""Frozen pre-relaunch schema and representative user data.

Keep the SQL independent of production schema constants so upgrades are tested
against the database users already have on disk.
"""

import sqlite3
from pathlib import Path


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



def build_pre_relaunch_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA + _SESSION_SCHEMA)
        stamp = "2026-09-28T10:00:00+00:00"
        conn.executemany(
            "INSERT INTO sessions (id, name, seeds, created_at, updated_at, "
            "hops_completed, total_results) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [("session-1", "First dig", '["seed-a"]', stamp, stamp, 1, 3),
             ("session-2", "Second dig", '["seed-b"]', stamp, stamp, 2, 2)],
        )
        conn.executemany(
            "INSERT INTO session_results (id, session_id, track_id, artist, "
            "track_name, video_id, score, hop, via, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i, "session-1" if i <= 3 else "session-2", f"track-{i}",
              f"Artist {i}", f"Title {i}", f"video-{i}", i / 10, 1,
              "seed-a" if i <= 3 else "seed-b", stamp) for i in range(1, 6)],
        )
        conn.executemany(
            "INSERT INTO result_events (result_id, event_type, created_at) VALUES (?, ?, ?)",
            [(1, "save", stamp), (2, "pass", stamp), (3, "dig_from", stamp)],
        )
        conn.execute(
            "INSERT INTO muted_leads (lead_type, lead_value, muted_at) VALUES (?, ?, ?)",
            ("artist", "Muted Artist", stamp),
        )
        conn.executemany(
            "INSERT INTO tracks (id, path, filename, artist, title, title_source, "
            "cosine_verified, cosine_track_id, mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i, f"/fixture/music/{i}.flac", f"{i}.flac", f"Artist {i}",
              f"Title {i}", "id3", 1, f"track-{i}", stamp) for i in range(1, 4)],
        )
        conn.commit()
    finally:
        conn.close()
