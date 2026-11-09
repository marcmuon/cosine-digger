"""Integration tests for session persistence (session.py) with real in-memory SQLite."""

from __future__ import annotations

import sqlite3


from cosine_digger.session import (
    create_session,
    record_results,
    )
from cosine_digger.taste import _SESSION_SCHEMA, _migrate_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_migrate_empty_database_and_preserve_saved_video():
    from cosine_digger.leads import get_saved_tracks, record_event

    with sqlite3.connect(":memory:") as db:
        _migrate_schema(db)
        sid = create_session(db, "first dig", ["seed"])
        ids = record_results(db, sid, [{
            "track_id": "t1", "artist": "Artist", "track_name": "Track",
            "score": 0.8, "video_id": "video123456", "via": "seed",
        }], hop=0)
        record_event(db, ids[0], "save")
        _migrate_schema(db)
        saved = get_saved_tracks(db)
        assert len(saved) == 1
        assert saved[0]["video_id"] == "video123456"


def test_migrate_legacy_session_database_preserves_feedback():
    from cosine_digger.leads import get_saved_tracks, record_event

    with sqlite3.connect(":memory:") as db:
        db.executescript(_SESSION_SCHEMA.replace("    video_id TEXT,\n", ""))
        sid = create_session(db, "legacy", ["seed"])
        cursor = db.execute(
            "INSERT INTO session_results (session_id, track_id, hop, discovered_at) VALUES (?, 't1', 0, '2026-03-25')",
            (sid,),
        )
        record_event(db, cursor.lastrowid, "save")
        _migrate_schema(db)
        _migrate_schema(db)
        saved = get_saved_tracks(db)
        assert saved[0]["track_id"] == "t1"
        assert saved[0]["video_id"] is None


# ---------------------------------------------------------------------------
# auto_session_name
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# create + resume session
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# record_results dedup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# convergence across sessions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_session_by_name
# ---------------------------------------------------------------------------
