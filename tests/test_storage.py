"""Storage integration tests using real SQLite files and legacy user data."""

import sqlite3
import sys
import warnings
from contextlib import closing

import pytest

from cosine_digger import storage, taste
from fixtures.pre_relaunch_db import build_pre_relaunch_db


NEW_TABLES = {"served_ledger", "http_cache", "job_runs", "fresh_finds", "sink_playlists", "known_tracks"}
SLICE_TABLES = {"digs", "steps", "step_picks", "step_hubs", "judgments", "heard",
                "slice_jobs", "slice_events", "judgment_controls"}


def schema_snapshot(conn):
    return conn.execute("SELECT * FROM sqlite_master ORDER BY type, name").fetchall()


def table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_preserves_entire_legacy_database_and_is_noop_on_repeat(tmp_path):
    path = tmp_path / "existing-user.db"
    build_pre_relaunch_db(path)
    with closing(storage.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        before = {
            name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for name in table_names(conn)
        }
        assert {name: len(before[name]) for name in (
            "sessions", "session_results", "result_events", "muted_leads", "tracks"
        )} == {"sessions": 2, "session_results": 5, "result_events": 3,
              "muted_leads": 1, "tracks": 3}

        storage.migrate(conn)

        for name, rows in before.items():
            assert conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() == rows
        assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert NEW_TABLES | SLICE_TABLES <= table_names(conn)
        after = schema_snapshot(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        storage.migrate(conn)
        conn.set_trace_callback(None)
        assert schema_snapshot(conn) == after
        # An unchanged sqlite_master alone would miss repeated CREATE IF NOT EXISTS.
        assert statements == ["PRAGMA user_version"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with taste.TasteProfile(path) as profile:
        assert profile.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
        assert profile.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("version,denied_table", [(0, "session_branches"), (1, "fresh_finds")])
def test_failed_migration_rolls_back_ddl_and_version_then_can_retry(tmp_path, version, denied_table):
    with closing(storage.connect(tmp_path / "rollback.db")) as conn:
        if version == 1:
            storage.migrate(conn)
            for name in NEW_TABLES:
                conn.execute(f"DROP TABLE {name}")
            conn.execute("PRAGMA user_version=1")
        before = schema_snapshot(conn)

        def deny_create(action, name, *args):
            if action == sqlite3.SQLITE_CREATE_TABLE and name == denied_table:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_create)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            storage.migrate(conn)
        conn.set_authorizer(None)

        assert not conn.in_transaction
        assert schema_snapshot(conn) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        storage.migrate(conn)
        assert NEW_TABLES | SLICE_TABLES <= table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_upgrade_adds_missing_legacy_columns_without_losing_rows(tmp_path):
    path = tmp_path / "older-user.db"
    build_pre_relaunch_db(path)
    with closing(storage.connect(path)) as conn:
        for column in ("title_source", "cosine_verified", "cosine_track_id"):
            conn.execute(f"ALTER TABLE tracks DROP COLUMN {column}")
        conn.execute("ALTER TABLE session_results DROP COLUMN video_id")
        tracks = conn.execute("SELECT id, path, artist, title FROM tracks").fetchall()
        results = conn.execute("SELECT id, session_id, track_id FROM session_results").fetchall()
        storage.migrate(conn)
        assert conn.execute("SELECT id, path, artist, title FROM tracks").fetchall() == tracks
        assert conn.execute("SELECT id, session_id, track_id FROM session_results").fetchall() == results
        assert {"title_source", "cosine_verified", "cosine_track_id"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(tracks)")
        }
        assert conn.execute("SELECT video_id FROM session_results").fetchall() == [(None,)] * 5


def test_repositories_round_trip_and_persist(tmp_path):
    path = tmp_path / "nested" / "taste.db"
    with closing(storage.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        storage.migrate(conn)
        assert storage.served_keys(conn) == set()
        track = {"track_id": "track-1", "video_id": "video-1", "artist": "Artist", "title": "Title"}
        storage.record_served(conn, "weekly", [track, track, {"track_id": "track-2"}])
        storage.record_served(conn, "daily", [track])
        assert conn.execute("SELECT COUNT(*) FROM served_ledger").fetchone()[0] == 3
        assert conn.execute(
            "SELECT artist, title FROM served_ledger WHERE playlist='daily'"
        ).fetchone() == ("Artist", "Title")
        assert storage.served_keys(conn) == {"track-1", "track-2", "video-1"}

        assert storage.cache_get(conn, "absent") is None
        storage.cache_put(conn, "request", '{"first": true}', 10)
        fetched_at = conn.execute("SELECT fetched_at FROM http_cache").fetchone()[0]
        assert storage.cache_get(conn, "request", now=fetched_at + 9) == '{"first": true}'
        assert storage.cache_get(conn, "request", now=fetched_at + 10) is None
        assert storage.cache_get(conn, "request", now=fetched_at + 11) is None
        storage.cache_put(conn, "request", "replacement", 60)
        assert storage.cache_get(conn, "request") == "replacement"
        storage.cache_put(conn, "zero-ttl", "expired", 0)
        assert storage.cache_get(conn, "zero-ttl") is None

        assert storage.last_success(conn, "weekly") is None
        run = storage.job_start(conn, "weekly", "2026-09-29T10:00:00+00:00")
        assert conn.execute(
            "SELECT status, scheduled_for, finished_at FROM job_runs WHERE id=?", (run,)
        ).fetchone() == ("running", "2026-09-29T10:00:00+00:00", None)
        storage.job_finish(conn, run, "success", "served 2 tracks")
        finished_at = conn.execute("SELECT finished_at FROM job_runs WHERE id=?", (run,)).fetchone()[0]
        failed = storage.job_start(conn, "weekly", None)
        assert failed > run
        storage.job_finish(conn, failed, "failed", "request timed out")
        assert storage.last_success(conn, "weekly") == finished_at
        assert storage.last_success(conn, "other") is None
        assert conn.execute("SELECT detail FROM job_runs WHERE id=?", (failed,)).fetchone()[0] == "request timed out"

        assert storage.playlist_id_get(conn, "youtube", "weekly") is None
        storage.playlist_id_put(conn, "youtube", "weekly", "old")
        storage.playlist_id_put(conn, "youtube", "weekly", "updated")
        storage.playlist_id_put(conn, "othersink", "weekly", "separate-sink")

    with closing(storage.connect(path)) as conn:
        assert storage.served_keys(conn) == {"track-1", "track-2", "video-1"}
        assert storage.cache_get(conn, "request") == "replacement"
        assert storage.last_success(conn, "weekly") == finished_at
        assert storage.playlist_id_get(conn, "youtube", "weekly") == "updated"
        assert storage.playlist_id_get(conn, "othersink", "weekly") == "separate-sink"
        assert conn.execute("SELECT COUNT(*) FROM sink_playlists").fetchone()[0] == 2


def test_scan_without_tag_reader_warns_once_and_builds_versioned_db(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    for name in ("one.mp3", "two.mp3"):
        (music / name).write_bytes(b"fixture")
    path = tmp_path / "state" / "taste.db"
    monkeypatch.setitem(sys.modules, "mutagen", None)
    monkeypatch.setattr(taste, "_tag_reader_warning_emitted", False)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        assert taste.incremental_scan(music, path) == (2, 2)
        (music / "three.mp3").write_bytes(b"fixture")
        assert taste.incremental_scan(music, path) == (1, 3)
    assert [str(w.message) for w in emitted] == ["tag reader unavailable — filename-only scan"]
    with taste.TasteProfile(path) as profile:
        assert profile.conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert [row[0] for row in profile.conn.execute("SELECT source FROM tracks")] == ["filename"] * 3
