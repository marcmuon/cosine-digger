"""Slice repositories through real file-backed SQLite, never a user database."""

import json
import math
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from cosine_digger import storage
from cosine_digger.models import Track
from cosine_digger.slice_types import (
    Facts, Hub, Point, StepIdentity,
    StepPick, build_hubs, island_position, pick_layout,
)
from fixtures.pre_relaunch_db import build_pre_relaunch_db

SLICE_TABLES = {"digs", "steps", "step_picks", "step_hubs", "judgments", "heard",
                "slice_jobs", "slice_events", "judgment_controls"}


@contextmanager
def write(conn):
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        yield


@pytest.fixture
def db(tmp_path):
    with closing(storage.connect(tmp_path / "slice.db")) as conn:
        assert conn.execute("SELECT json_valid('{}')").fetchone()[0] == 1
        storage.migrate(conn)
        yield conn


def point(name="Seed - Track", **fields):
    artist, title = name.split(" - ", 1)
    from cosine_digger.api import _key
    key = _key(artist, title)
    return Point(point_id=f"track:{key}", kind="track", name=name, track_key=key,
                 cosine_id="seed", artist=artist, title=title, indexed=True, **fields)


def root(conn, seeds=None, further=False):
    seeds = seeds or [point()]
    with write(conn):
        dig_id = storage.create_dig(conn, seeds)
        return storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=seeds, move="seed", further=further)


def emit(conn, identity, event, data, redact=lambda data: data):
    with write(conn):
        return storage.append_slice_event(conn, identity.job_id, event, data, redact=redact)


def batch(conn, identity, hop=0, start_rank=1, score_floor=0.0):
    return emit(conn, identity, "batch", {"hop": hop, "start_rank": start_rank,
                                         "target_count": 25, "label": "near" if hop == 0 else "further",
                                         "score_floor": score_floor})


def pick(conn, identity, rank=1, name="Artist 1 - Title 1", hop=0, score=0.79, state="loading", facts=None):
    from cosine_digger.api import _key
    artist, title = name.split(" - ", 1)
    origins = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]["step"]["origins"]
    route = {"move": "seed", "further": False, "hop": hop, "via": "seed", "branch": None,
             "parent_via": None, "parent_pick_ranks": [], "seed_scores": {p["point_id"]: None for p in origins},
             "nearest_seed": None, "layout_seed": origins[0]["point_id"], "close_seed_ids": [],
             "close_count": 0, "seed_count": len(origins), "attribution": "direct",
             "branch_seed_scores": {}, "why": "suggested from Seed - Track; similarity unavailable",
             "raw_score": 0.041, "normalization_basis": 0.05}
    return emit(conn, identity, "pick", {"pick": {"rank": rank, "track_key": _key(artist, title),
                "cosine_id": f"c{rank}", "artist": artist, "title": title, "video_id": f"v{rank}",
                "score": score, "batch": hop, "route": route, "facts_state": state, "facts": facts}})["pick"]


def shared_facts():
    return Facts(release_id="123", release_url="https://www.discogs.com/release/123",
                 labels=[{"id": "42", "name": "Étiquette"}], year=2024, styles=["Techno"],
                 artists=[{"id": "6", "name": "Main Artist"}],
                 credits=[{"id": "9", "name": "Person", "roles": ["Mixed By", "Producer"], "scope": "release"}],
                 thumb="https://i.discogs.com/example.jpg").to_dict()


def finish(conn, identity):
    snapshot = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
    if not snapshot["step"]["picks_done_at"]:
        emit(conn, identity, "picks_done", {"pick_count": len(snapshot["picks"])})
    for item in snapshot["picks"]:
        if item["facts_state"] == "loading":
            emit(conn, identity, "facts", {"rank": item["rank"], "facts_state": "no_token", "facts": None})
    snapshot = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
    for hub in build_hubs(snapshot["picks"], snapshot["hubs"]):
        if hub.to_dict() not in snapshot["hubs"]:
            emit(conn, identity, "hub", {"hub": hub.to_dict()})
    return emit(conn, identity, "done", {"status": "done", "pick_count": len(snapshot["picks"]),
                                        "batches": snapshot["batches"], "facts": {"completed": len(snapshot["picks"]),
                                        "total": len(snapshot["picks"])}, "hub_count": len(snapshot["hubs"]),
                                        "warning_codes": [], "snapshot_url": f"/api/steps/{identity.step_id}"})


def seed_legacy_history(conn, count):
    """Synthetic save/pass history for the review C3 scaling regressions."""
    at = "2026-01-01T00:00:00+00:00"
    with conn:
        conn.execute("INSERT INTO sessions(id,name,seeds,created_at,updated_at) VALUES('legacy','History','[]',?,?)", (at, at))
        conn.executemany("INSERT INTO session_results(id,session_id,track_id,artist,track_name,hop,discovered_at) "
                         "VALUES(?,'legacy',?,?,?,0,?)",
                         [(i, f"legacy-{i}", f"Legacy Artist {i}", f"Legacy Title {i}", at) for i in range(1, count + 1)])
        conn.executemany("INSERT INTO result_events(result_id,event_type,created_at) VALUES(?,?,?)",
                         [(i, "save" if i % 2 else "pass", at) for i in range(1, count + 1)])


@pytest.mark.parametrize("read", ["step"])
def test_public_read_scans_legacy_judgments_once(db, read):
    identity = root(db)
    batch(db, identity)
    picks = [pick(db, identity, rank=i, name=f"Legacy Artist {i} - Legacy Title {i}", state="no_token")
             for i in range(1, 26)]
    finish(db, identity)
    other = root(db)
    batch(db, other)
    pick(db, other, name="Other - Recording", state="no_token")
    finish(db, other)
    seed_legacy_history(db, 1000)
    with db:
        # Tied timestamps still use the newest event id, after canonicalization.
        db.executemany("INSERT INTO session_results(id,session_id,track_id,artist,track_name,hop,discovered_at) "
                       "VALUES(?,'legacy',?,'  LEGACY ARTIST 1  ','LEGACY TITLE 1',0,'2026-02-01')",
                       [(1001, "new-save"), (1002, "new-pass")])
        db.executemany("INSERT INTO result_events(result_id,event_type,created_at) VALUES(?,?,'2026-02-01')",
                       [(1001, "save"), (1002, "pass")])
    with write(db):
        storage.change_judgment(db, track_key=picks[1]["track_key"], step_id=identity.step_id,
                                verdict="keep", expected_revision=0)
        storage.change_judgment(db, track_key=picks[2]["track_key"], step_id=identity.step_id, expected_revision=0)
    scans = []
    db.set_trace_callback(lambda sql: scans.append(sql) if "FROM result_events e JOIN session_results r" in sql else None)
    try:
        if read == "step":
            result = storage.get_step_snapshot(db, identity.step_id)
            judgments = result["live"]["judgments"]
            assert judgments[picks[0]["track_key"]]["verdict"] == "pass"
            assert judgments[picks[0]["track_key"]]["legacy_session_id"] == "legacy"
            assert judgments[picks[1]["track_key"]]["source"] == "gui"
            assert judgments[picks[2]["track_key"]]["source"] == "none"
        elif read == "dig":
            result = storage.get_dig(db, identity.dig_id)
            assert result["steps"][0]["counts"] == {"picks": 25, "kept": 12, "passed": 12}
        else:
            result = storage.list_digs(db)
            assert len(result) == 2
            assert next(dig for dig in result if dig["id"] == identity.dig_id)["kept_count"] == 12
        assert len(scans) == 1, f"one public read scanned all history {len(scans)} times"
    finally:
        db.set_trace_callback(None)


def source_point(item):
    return point(f"{item['artist']} - {item['title']}", source_step_id=item["step_id"], source_rank=item["rank"]).model_copy(
        update={"cosine_id": item["cosine_id"], "video_id": item["video_id"]})


def branch(conn, identity, origin, **options):
    with write(conn):
        return storage.create_step(conn, dig_id=identity.dig_id, parent_id=identity.step_id,
                                   origins=[origin], move=origin.kind, further=False, **options)


def legacy_snapshot(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} - SLICE_TABLES
    schema = [tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
              if row[2] not in SLICE_TABLES]
    rows = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in tables}
    return schema, rows


def populated_v3(path):
    build_pre_relaunch_db(path)
    with closing(storage.connect(path)) as conn:
        for migration in storage._MIGRATIONS[:3]:
            with write(conn):
                migration(conn)
        conn.execute("PRAGMA user_version=3")
        storage.record_served(conn, "daily", [{"track_id": "served", "video_id": "video"}])
        storage.record_known(conn, [{"track_id": "known"}], "seed")
        storage.cache_put(conn, "fixture", '{"cached":true}', 3600)
        run = storage.job_start(conn, "daily", None)
        storage.job_finish(conn, run, "success", "fixture")
        storage.playlist_id_put(conn, "youtube", "daily", "playlist")
        with conn:
            conn.execute("INSERT INTO fresh_finds (artist,title,found_at) VALUES ('Fresh','Find','2026-10-01T00:00:00+00:00')")


def test_populated_v3_upgrade_and_repeat_leave_all_legacy_schema_and_content_unchanged(tmp_path):
    path = tmp_path / "populated.db"
    populated_v3(path)
    with closing(storage.connect(path)) as conn:
        assert conn.execute("SELECT json_valid('{}')").fetchone()[0] == 1
        before = legacy_snapshot(conn)
        storage.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert legacy_snapshot(conn) == before
        schema = conn.execute("SELECT * FROM sqlite_master ORDER BY type,name").fetchall()
        statements = []
        conn.set_trace_callback(statements.append)
        storage.migrate(conn)
        conn.set_trace_callback(None)
        assert statements == ["PRAGMA user_version"]
        with write(conn):
            storage._migration_v4(conn)
        assert conn.execute("SELECT * FROM sqlite_master ORDER BY type,name").fetchall() == schema
        identity = root(conn)
        batch(conn, identity)
        item = pick(conn, identity, name="Artist 1 - Title 1")
        finish(conn, identity)
        with write(conn):
            storage.change_judgment(conn, track_key=item["track_key"], step_id=identity.step_id,
                                    verdict="keep", expected_revision=0)
            storage.record_heard(conn, track_key=item["track_key"], step_id=identity.step_id)
        assert legacy_snapshot(conn) == before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("action,name", [(sqlite3.SQLITE_CREATE_TABLE, "judgment_controls"),
                                        (sqlite3.SQLITE_CREATE_INDEX, "idx_slice_jobs_step")])
def test_failed_v4_rolls_back_all_ddl_and_version_and_retry_succeeds(tmp_path, action, name):
    path = tmp_path / "rollback.db"
    populated_v3(path)
    with closing(storage.connect(path)) as conn:
        before = legacy_snapshot(conn)
        conn.set_authorizer(lambda operation, obj, *_: sqlite3.SQLITE_DENY if
                            (operation, obj) == (action, name) else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            storage.migrate(conn)
        conn.set_authorizer(None)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not SLICE_TABLES & {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert legacy_snapshot(conn) == before
        storage.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert legacy_snapshot(conn) == before


def test_creation_is_atomic_and_allocates_stable_identity_numbers_and_islands(db):
    with pytest.raises(RuntimeError, match="caller-owned"):
        storage.create_dig(db, [point()])
    with pytest.raises(RuntimeError, match="admission failed"):
        with write(db):
            dig_id = storage.create_dig(db, [point()])
            storage.create_step(db, dig_id=dig_id, parent_id=None, origins=[point()], move="seed", further=False)
            raise RuntimeError("admission failed")
    assert db.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
    first = root(db, [point(), point("Another - Seed")])
    assert re.fullmatch("[a-f0-9]{32}", first.dig_id)
    assert re.fullmatch("[a-f0-9]{32}", first.step_id)
    assert re.fullmatch("sl-[a-f0-9]{32}", first.job_id)
    assert storage.get_dig(db, first.dig_id)["name"] == "Seed - Track + 1 more"
    assert storage.get_step_snapshot(db, first.step_id)["snapshot"]["step"]["island_x"] == 0
    batch(db, first)
    item = pick(db, first)
    second = branch(db, first, source_point(item))
    assert second.number == 2
    assert branch(db, first, source_point(item)).to_dict() == {**second.to_dict(), "created": False}
    position = storage.get_step_snapshot(db, second.step_id)["snapshot"]["step"]
    assert (position["island_x"], position["island_y"]) == (420, 0)
    assert not db.in_transaction


def test_rank_and_parent_and_point_provenance_are_checked_with_foreign_keys_off(db):
    identity, other = root(db), root(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    batch(db, identity)
    item = pick(db, identity)
    for bad_rank in (0, True, 3):
        with pytest.raises(ValueError):
            pick(db, identity, rank=bad_rank)
    with pytest.raises(sqlite3.IntegrityError):
        pick(db, identity, rank=2)  # duplicate recording, never replace rank one
    for parent_id, origin in (("missing", source_point(item)), (other.step_id, source_point(item)),
                              (identity.step_id, source_point(item).model_copy(update={"source_rank": 9})),
                              (identity.step_id, source_point(item).model_copy(update={"track_key": "wrong"})),
                              (identity.step_id, point())):
        with pytest.raises(ValueError):
            with write(db):
                storage.create_step(db, dig_id=identity.dig_id, parent_id=parent_id, origins=[origin], move="track", further=False)
    fake_label = Point(point_id="label:42", kind="label", name="Route label", discogs_id="42",
                       source_step_id=identity.step_id, source_rank=1)
    with pytest.raises(ValueError, match="verified fact"):
        branch(db, identity, fake_label)
    assert db.execute("SELECT COUNT(*) FROM step_picks").fetchone()[0] == 1
    assert storage.get_dig(db, identity.dig_id)["step_count"] == 1
    artist = Point(point_id="artist:name:artist 1", kind="artist", name="Artist 1",
                   source_step_id=identity.step_id, source_rank=1)
    assert branch(db, identity, artist).number == 2


def test_canonical_saved_snapshot_and_layout_survive_reopen_and_live_marks(tmp_path):
    path = tmp_path / "persisted.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        identity = root(conn, [point(), point("Second - Seed"), point("Third - Seed")], further=True)
        requested = [Point.model_validate(value) for value in storage.get_step_snapshot(conn, identity.step_id)["snapshot"]["step"]["origins"]]
        resolved = [p.model_copy(update={"resolution": "exact", "resolved_tracks": []}) for p in requested]
        with write(conn):
            storage.resolve_step_origins(conn, identity.step_id, resolved)
        batch(conn, identity)
        item = pick(conn, identity)
        assert item["layout"]["radius"] == 0.08 + 0.77 * (1 - 0.79)
        assert 0 < item["layout"]["angle"] < math.tau / 3
        batch(conn, identity, hop=1, start_rank=2)
        pick(conn, identity, rank=2, name="Artist 2 - Title 2", hop=1)
        milestone = emit(conn, identity, "picks_done", {"pick_count": 2})
        stamp = milestone["at"]
        assert milestone["batches"] == [{"hop": 0, "start_rank": 1, "label": "near", "count": 1},
                                         {"hop": 1, "start_rank": 2, "label": "further", "count": 1}]
        assert milestone["facts"] == {"completed": 0, "total": 2}
        snapshot = storage.get_step_snapshot(conn, identity.step_id)
        assert snapshot["snapshot"]["step"]["picks_done_at"] == stamp
        assert snapshot["job"]["state"] == "running"
        with pytest.raises(storage.SliceConflict, match="settled"):
            emit(conn, identity, "done", {"status": "done", "pick_count": 2})
        with pytest.raises(storage.SliceConflict, match="once"):
            emit(conn, identity, "picks_done", {"pick_count": 2})
        with pytest.raises(storage.SliceConflict, match="finished"):
            pick(conn, identity, rank=3, name="Artist 3 - Title 3", hop=1)
        with pytest.raises(storage.SliceConflict, match="first pick"):
            with write(conn):
                storage.resolve_step_origins(conn, identity.step_id, resolved)
        finish(conn, identity)
        before = storage.canonical_json(storage.get_step_snapshot(conn, identity.step_id)["snapshot"])
        # Another dig contains the same recording. Its live overlays name that
        # source without changing a byte of the first frozen snapshot.
        other = root(conn)
        batch(conn, other)
        same = pick(conn, other)
        finish(conn, other)
        with write(conn):
            storage.change_judgment(conn, track_key=same["track_key"], step_id=other.step_id,
                                    verdict="keep", expected_revision=0)
            heard = storage.record_heard(conn, track_key=same["track_key"], step_id=other.step_id)
            assert storage.record_heard(conn, track_key=item["track_key"], step_id=identity.step_id) == heard
        with pytest.raises(storage.SliceConflict, match="immutable"):
            emit(conn, identity, "facts", {"rank": 1, "facts_state": "ready", "facts": shared_facts()})
    with closing(storage.connect(path)) as conn:
        snapshot = storage.get_step_snapshot(conn, identity.step_id)
        assert storage.canonical_json(snapshot["snapshot"]) == before
        assert snapshot["live"]["judgments"][item["track_key"]]["step_id"] == other.step_id
        assert snapshot["live"]["heard"][item["track_key"]]["first_step_id"] == other.step_id
        assert snapshot["live"]["also_in"][item["track_key"]][0]["step_id"] == other.step_id
        assert snapshot["snapshot"]["step"]["picks_done_at"] == stamp
        for at in conn.execute("SELECT at FROM slice_events"):
            assert datetime.fromisoformat(at[0]).utcoffset() == timedelta(0)
        assert snapshot["job"]["cursor"] == storage.get_slice_job(conn, identity.job_id)["last_seq"]
        assert all(storage.canonical_json(json.loads(row[0])) == row[0] for row in conn.execute("SELECT payload FROM slice_events"))


def test_events_and_state_rollback_together_and_replay_validates_cursors(db):
    identity = root(db)
    batch(db, identity)
    with pytest.raises(RuntimeError, match="before commit"):
        with write(db):
            storage.append_slice_event(db, identity.job_id, "picks_done", {"pick_count": 0}, redact=lambda value: value)
            raise RuntimeError("before commit")
    assert storage.get_slice_job(db, identity.job_id)["last_seq"] == 1
    assert storage.get_step_snapshot(db, identity.step_id)["snapshot"]["step"]["picks_done_at"] is None
    finish(db, identity)
    events = storage.slice_events(db, identity.job_id)
    assert [item["event"] for item in events] == ["batch", "picks_done", "done"]
    assert [item["data"]["seq"] for item in events] == [1, 2, 3]
    assert storage.slice_events(db, identity.job_id, after=1, last_event_id=events[1]["id"]) == events[2:]
    assert storage.slice_events(db, identity.job_id, after=3) == []
    for cursor in ("sl-other:1", "malformed", f"{identity.job_id}:-1", f"{identity.job_id}:1.2"):
        with pytest.raises(ValueError):
            storage.slice_events(db, identity.job_id, last_event_id=cursor)
    with pytest.raises(storage.SliceConflict, match="ahead"):
        storage.slice_events(db, identity.job_id, after=4)
    for event, data in (("note", {"phase": "near"}), ("picks_done", {"pick_count": 0}),
                        ("batch", {"hop": 0, "start_rank": 1, "target_count": 25, "label": "near"})):
        with pytest.raises(storage.SliceConflict, match="immutable"):
            emit(db, identity, event, data)


def test_ready_facts_create_honest_hubs_with_immutable_positions(db):
    identity = root(db)
    batch(db, identity)
    first = pick(db, identity)
    pick(db, identity, rank=2, name="Artist 2 - Title 2")
    pick(db, identity, rank=3, name="Artist 3 - Title 3")
    facts = shared_facts()
    emit(db, identity, "facts", {"rank": 1, "facts_state": "ready", "facts": facts})
    with pytest.raises(storage.SliceConflict, match="settled"):
        emit(db, identity, "facts", {"rank": 1, "facts_state": "missing", "facts": None})
    emit(db, identity, "facts", {"rank": 2, "facts_state": "ready", "facts": facts})
    snapshot = storage.get_step_snapshot(db, identity.step_id)["snapshot"]
    hubs = build_hubs(snapshot["picks"], [])
    assert {hub.hub_key for hub in hubs} == {"label:42", "person:9"}
    for hub in hubs:
        emit(db, identity, "hub", {"hub": hub})
    positions = {hub.hub_key: hub.layout.angle for hub in hubs}
    emit(db, identity, "facts", {"rank": 3, "facts_state": "ready", "facts": facts})
    snapshot = storage.get_step_snapshot(db, identity.step_id)["snapshot"]
    grown = build_hubs(snapshot["picks"], snapshot["hubs"])
    for hub in grown:
        assert hub.pick_ranks == [1, 2, 3]
        assert hub.layout.angle == positions[hub.hub_key]
        emit(db, identity, "hub", {"hub": hub})
    changed = grown[0].to_dict()
    changed["layout"]["angle"] = (changed["layout"]["angle"] + 1) % math.tau
    with pytest.raises(ValueError, match="saved geometry"):
        emit(db, identity, "hub", {"hub": changed})
    finish(db, identity)
    with pytest.raises(storage.SliceConflict):
        emit(db, identity, "hub", {"hub": grown[0]})
    assert storage.get_step_snapshot(db, identity.step_id)["snapshot"]["picks"][0]["layout"] == first["layout"]


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_empty_failed_or_stopped_steps_require_retry(db, status):
    identity = root(db)
    error = {"code": "interrupted", "message": "Interrupted.", "retry_after_s": None, "retry_at": None}
    emit(db, identity, "error", {"error": error, "status": status, "pick_count": 0})
    before = storage.canonical_json(storage.get_step_snapshot(db, identity.step_id)["snapshot"])
    old_events = storage.slice_events(db, identity.job_id)
    with pytest.raises(storage.SliceConflict, match="Retry"):
        with write(db):
            storage.create_step(db, dig_id=identity.dig_id, parent_id=None, origins=[point()],
                                move="seed", further=False, rerun_of=identity.step_id)
    assert storage.canonical_json(storage.get_step_snapshot(db, identity.step_id)["snapshot"]) == before
    assert db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 1
    with write(db):
        retry = storage.create_step(db, dig_id=identity.dig_id, parent_id=None, origins=[point()],
                                    move="seed", further=False, reuse_empty_step_id=identity.step_id)
    batch(db, retry)
    pick(db, retry)
    finish(db, retry)
    assert retry.step_id == identity.step_id and retry.job_id != identity.job_id
    assert storage.slice_events(db, identity.job_id) == old_events


@pytest.mark.parametrize("status", ["done", "failed"])
@pytest.mark.parametrize("prior_terminal", [False])
def test_terminal_rerun_diff_survives_retry_of_a_preexisting_empty_original(db, status, prior_terminal):
    original = root(db)
    error = {"code": "interrupted", "message": "Interrupted.", "retry_after_s": None, "retry_at": None}
    emit(db, original, "error", {"error": error, "status": "stopped", "pick_count": 0})
    with write(db):
        # Simulate the persisted sibling admitted by the old repository. Relax
        # only admission during setup, then restore the actual stopped source.
        db.execute("UPDATE steps SET status='done' WHERE id=?", (original.step_id,))
        sibling = storage.create_step(db, dig_id=original.dig_id, parent_id=None, origins=[point()],
                                      move="seed", further=False, rerun_of=original.step_id)
        db.execute("UPDATE steps SET status='stopped' WHERE id=?", (original.step_id,))
    batch(db, sibling)
    pick(db, sibling, state="no_token")
    if status == "done":
        finish(db, sibling)
    else:
        emit(db, sibling, "error", {"error": error, "status": status, "pick_count": 1})
    if prior_terminal:
        # Older terminal attempts have no persisted diff; preserve the snapshot
        # they currently expose before Retry can change the original's picks.
        with write(db):
            db.execute("UPDATE slice_jobs SET progress='{}' WHERE id=?", (sibling.job_id,))
    snapshot = storage.get_step_snapshot(db, sibling.step_id)["snapshot"]
    diff = snapshot["diff"]
    assert (diff["same"], diff["new"], diff["gone"]) == (0, 1, 0)
    before = storage.canonical_json(snapshot)
    events = storage.slice_events(db, sibling.job_id)

    with write(db):
        retry = storage.create_step(db, dig_id=original.dig_id, parent_id=None, origins=[point()],
                                    move="seed", further=False, reuse_empty_step_id=original.step_id)
    batch(db, retry)
    pick(db, retry)
    finish(db, retry)
    assert storage.canonical_json(storage.get_step_snapshot(db, sibling.step_id)["snapshot"]) == before
    assert storage.slice_events(db, sibling.job_id) == events
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(storage.connect(path)) as reader:
        assert storage.canonical_json(storage.get_step_snapshot(reader, sibling.step_id)["snapshot"]) == before


@pytest.mark.parametrize("status,count", [("done", 0), ("done", 1)])
def test_rerun_accepts_done_steps_and_failed_or_stopped_steps_with_picks(db, status, count):
    original = root(db)
    if count:
        batch(db, original)
        pick(db, original, state="no_token")
    if status == "done":
        finish(db, original)
    else:
        error = {"code": "interrupted", "message": "Interrupted.", "retry_after_s": None, "retry_at": None}
        emit(db, original, "error", {"error": error, "status": status, "pick_count": count})
    before = storage.canonical_json(storage.get_step_snapshot(db, original.step_id)["snapshot"])
    with write(db):
        sibling = storage.create_step(db, dig_id=original.dig_id, parent_id=None, origins=[point()],
                                      move="seed", further=False, rerun_of=original.step_id)
    finish(db, sibling)
    assert sibling.step_id != original.step_id and sibling.number == 2
    assert storage.canonical_json(storage.get_step_snapshot(db, original.step_id)["snapshot"]) == before


def test_empty_retry_has_a_new_journal_but_rerun_is_a_sibling_with_frozen_diff(db):
    identity = root(db)
    error = {"code": "server_restarted", "message": "The local server restarted.", "retry_after_s": None, "retry_at": None}
    emit(db, identity, "error", {"error": error, "status": "stopped", "pick_count": 0, "snapshot_url": "fixture"})
    old_events = storage.slice_events(db, identity.job_id)
    with write(db):
        retry = storage.create_step(db, dig_id=identity.dig_id, parent_id=None, origins=[point()],
                                    move="seed", further=False, reuse_empty_step_id=identity.step_id)
    assert retry.step_id == identity.step_id and retry.number == 1 and not retry.created
    assert retry.job_id != identity.job_id
    assert storage.get_slice_job(db, retry.job_id)["attempt"] == 2
    assert storage.slice_events(db, retry.job_id) == []
    assert storage.slice_events(db, identity.job_id) == old_events
    batch(db, retry)
    item = pick(db, retry)
    pick(db, retry, rank=2, name="Gone - Track")
    finish(db, retry)
    original_bytes = storage.canonical_json(storage.get_step_snapshot(db, retry.step_id)["snapshot"])
    with pytest.raises(storage.SliceConflict, match="empty"):
        with write(db):
            storage.create_step(db, dig_id=retry.dig_id, parent_id=None, origins=[point()],
                                move="seed", further=False, reuse_empty_step_id=retry.step_id)
    with write(db):
        rerun = storage.create_step(db, dig_id=retry.dig_id, parent_id=None, origins=[point()],
                                    move="seed", further=False, rerun_of=retry.step_id)
    assert rerun.number == 2 and rerun.step_id != retry.step_id and rerun.job_id != retry.job_id
    assert storage.get_dig(db, rerun.dig_id)["root_step_id"] == retry.step_id
    batch(db, rerun)
    pick(db, rerun)
    pick(db, rerun, rank=2, name="New - Track")
    finish(db, rerun)
    snapshot = storage.get_step_snapshot(db, rerun.step_id)
    diff = snapshot["snapshot"]["diff"]
    assert (diff["same"], diff["new"], diff["gone"]) == (1, 1, 1)
    assert diff["gone_picks"][0]["step_id"] == retry.step_id
    assert snapshot["snapshot"]["step"]["parent_id"] is None
    assert snapshot["live"]["also_in"][item["track_key"]] == []
    assert storage.canonical_json(storage.get_step_snapshot(db, retry.step_id)["snapshot"]) == original_bytes
    assert storage.get_step_snapshot(db, retry.step_id)["live"]["rerun_steps"] == [{"step_id": rerun.step_id, "number": 2}]


def test_judgment_revision_suppression_and_undo_preserve_legacy_and_source(db):
    identity = root(db)
    batch(db, identity)
    item = pick(db, identity)
    finish(db, identity)
    with db:
        db.execute("INSERT INTO sessions (id,name,seeds,created_at,updated_at) VALUES ('cli','CLI','[]','x','x')")
        db.execute("""INSERT INTO session_results (session_id,track_id,artist,track_name,hop,discovered_at)
                      VALUES ('cli','legacy','Artist 1','Title 1',1,'x')""")
        result_id = db.execute("SELECT id FROM session_results").fetchone()[0]
        db.execute("INSERT INTO result_events (result_id,event_type,created_at) VALUES (?,'save','2026-10-01')", (result_id,))
    before = legacy_snapshot(db)
    key = item["track_key"]
    assert storage.effective_judgment(db, key) == {"verdict": "keep", "step_id": None, "at": "2026-10-01",
                                                 "source": "legacy", "legacy_session_id": "cli"}
    with write(db):
        noop = storage.change_judgment(db, track_key=key, step_id=identity.step_id, verdict="keep", expected_revision=0)
    assert noop["change_id"] is None and noop["revision"] == 0
    with write(db):
        passed = storage.change_judgment(db, track_key=key, step_id=identity.step_id, verdict="pass", expected_revision=0)
    with pytest.raises(storage.SliceConflict, match="changed elsewhere"):
        with write(db):
            storage.change_judgment(db, track_key=key, step_id=identity.step_id, verdict="keep", expected_revision=0)
    with write(db):
        deleted = storage.change_judgment(db, track_key=key, step_id=identity.step_id, expected_revision=1)
    assert deleted["effective"]["source"] == "none" and deleted["revision"] == 2
    assert db.execute("SELECT suppress_legacy FROM judgment_controls").fetchone()[0] == 1
    with pytest.raises(storage.SliceConflict, match="most recent"):
        with write(db):
            storage.change_judgment(db, track_key=key, step_id=identity.step_id, expected_revision=2,
                                    undo_change_id=passed["change_id"])
    with write(db):
        restored = storage.change_judgment(db, track_key=key, step_id=identity.step_id, expected_revision=2,
                                          undo_change_id=deleted["change_id"])
    assert restored["effective"] == passed["effective"] and restored["revision"] == 3
    with pytest.raises(storage.SliceConflict):
        with write(db):
            storage.change_judgment(db, track_key=key, step_id=identity.step_id, expected_revision=3,
                                    undo_change_id=deleted["change_id"])
    with write(db):
        inherited = storage.change_judgment(db, track_key=key, step_id=identity.step_id, expected_revision=3,
                                           undo_change_id=passed["change_id"])
    assert inherited["effective"]["source"] == "legacy" and inherited["revision"] == 4
    history = json.loads(db.execute("SELECT history FROM judgment_controls").fetchone()[0])
    assert all(change["undone"] for change in history)
    assert legacy_snapshot(db) == before
    with pytest.raises(RuntimeError):
        with write(db):
            storage.change_judgment(db, track_key=key, step_id=identity.step_id, verdict="pass", expected_revision=4)
            raise RuntimeError("rollback")
    assert db.execute("SELECT revision FROM judgment_controls").fetchone()[0] == 4
    with pytest.raises(ValueError, match="belong"):
        with write(db):
            storage.record_heard(db, track_key="unknown", step_id=identity.step_id)


def test_enrichment_journal_redacts_and_keeps_reserved_request_identity(db):
    with write(db):
        job_id = storage.create_enrichment_job(db, progress={"request": {"key": "once", "operation": "enrich",
                                             "body_hash": "hash", "response": {}}, "work": {"cursor": 0}})
        assert storage.create_enrichment_job(db) == job_id
        storage.update_slice_progress(db, job_id, {"work": {"cursor": 1}}, redact=lambda value: value)
    request = storage.get_slice_job(db, job_id)["progress"]["request"]
    with pytest.raises(storage.SliceConflict, match="identity"):
        with write(db):
            storage.update_slice_progress(db, job_id, {"request": {"key": "changed"}}, redact=lambda value: value)
    redact = lambda value: json.loads(json.dumps(value).replace("private-fixture-marker", "[redacted]"))
    identity = StepIdentity(dig_id="", step_id="", number=1, job_id=job_id, created=True)
    note = emit(db, identity, "note", {"code": "fixture", "params": {}, "message": "private-fixture-marker",
                                      "phase": "enrichment", "progress": {"cursor": 2}}, redact=redact)
    assert note["step_id"] is None and note["message"] == "[redacted]"
    assert storage.get_slice_job(db, job_id)["progress"] == {"request": request, "work": {"cursor": 2}}
    assert "private-fixture-marker" not in db.execute("SELECT payload FROM slice_events").fetchone()[0]
    emit(db, identity, "done", {"status": "stopped", "processed_pairs": 2})
    with write(db):
        resumed = storage.create_enrichment_job(db, resume_job_id=job_id)
    assert resumed != job_id
    assert storage.get_slice_job(db, resumed)["attempt"] == 2
    assert storage.get_slice_job(db, resumed)["progress"]["work"] == {"cursor": 2, "resume_job_id": job_id}
    assert "request" not in storage.get_slice_job(db, resumed)["progress"]


def test_layout_bounds_sectors_fan_and_collision_math():
    origins = [point(), point("Other - Seed"), point("Last - Seed")]
    assert pick_layout("s", 1, 1, origins).radius == 0.08
    assert pick_layout("s", 1, 0, origins).radius == 0.85
    for index, origin in enumerate(origins):
        angle = pick_layout("s", 1, 0.8, origins, origin.point_id).angle
        assert math.tau * index / 3 < angle < math.tau * (index + 1) / 3
    assert pick_layout("é", 1, 0.8, origins) == pick_layout("é", 1, 0.8, origins)
    with pytest.raises(ValueError):
        pick_layout("s", 1, 0.8, origins, "unknown")
    occupied = [(0, 0), (420, 0)]
    for index, sign in ((1, 1), (2, -1)):
        x, y = island_position(parent=(0, 0), grandparent=None, sibling_index=index, existing=occupied)
        assert y * sign > 0 and math.hypot(x - 420, y) >= 340
        occupied.append((x, y))
    assert island_position(parent=(420, 0), grandparent=(0, 0), sibling_index=0, existing=occupied) == (840, 0)


def test_batch_score_floor_scales_saved_radii_per_batch(db):
    identity = root(db, [point(), point("Second - Seed")], further=True)
    assert storage.get_step_snapshot(db, identity.step_id)["snapshot"]["step"]["layout_version"] == 2
    boundary = batch(db, identity, score_floor=0.833)
    assert boundary["score_floor"] == 0.833
    near = [pick(db, identity, rank=rank, name=f"Artist {rank} - Title {rank}", score=score,
                 state="no_token") for rank, score in enumerate((1.0, 0.9165, 0.833), 1)]
    assert [p["layout"]["radius"] for p in near] == pytest.approx([0.08, 0.465, 0.85])
    batch(db, identity, hop=1, start_rank=4, score_floor=0.4)
    further = [pick(db, identity, rank=rank, name=f"Artist {rank} - Title {rank}", hop=1,
                    score=score, state="no_token") for rank, score in enumerate((1.0, 0.7, 0.4), 4)]
    assert [p["layout"]["radius"] for p in further] == pytest.approx([0.08, 0.465, 0.85])
    finish(db, identity)
    saved = storage.get_step_snapshot(db, identity.step_id)["snapshot"]["picks"]
    assert saved == near + further
    assert [e["data"]["score_floor"] for e in storage.slice_events(db, identity.job_id)
            if e["event"] == "batch"] == [0.833, 0.4]


def test_unit_score_batch_keeps_every_pick_at_inner_radius(db):
    identity = root(db)
    batch(db, identity, score_floor=1.0)
    picks = [pick(db, identity, rank=rank, name=f"Artist {rank} - Title {rank}",
                  score=1.0, state="no_token") for rank in range(1, 4)]
    finish(db, identity)
    assert [p["layout"]["radius"] for p in picks] == [0.08] * 3


@pytest.mark.parametrize("floor", [-0.01, 1.01, float("nan"), float("inf"), -float("inf"), True, "0.5"])
def test_batch_rejects_invalid_score_floor_without_writes(db, floor):
    identity = root(db)
    with pytest.raises(ValueError):
        batch(db, identity, score_floor=floor)
    assert storage.slice_events(db, identity.job_id) == []
    assert storage.get_slice_job(db, identity.job_id)["last_seq"] == 0


@pytest.mark.parametrize("floor", [0.9, None])
def test_batch_floor_cannot_exceed_a_pick_score_or_be_null_with_picks(db, floor):
    identity = root(db)
    batch(db, identity, score_floor=floor)
    with pytest.raises(ValueError, match="score_floor"):
        pick(db, identity, score=0.8)
    assert storage.get_step_snapshot(db, identity.step_id)["snapshot"]["picks"] == []
    assert storage.get_slice_job(db, identity.job_id)["last_seq"] == 1


def test_layout_version_one_reopens_with_original_saved_geometry(tmp_path):
    path = tmp_path / "version-one.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        identity = root(conn)
        # Emulate a pre-O14 step, including a journal without score_floor.
        with write(conn):
            conn.execute("UPDATE steps SET layout_version=1 WHERE id=?", (identity.step_id,))
        emit(conn, identity, "batch", {"hop": 0, "start_rank": 1, "target_count": 25, "label": "near"})
        with write(conn):
            conn.execute("UPDATE slice_events SET payload=json_remove(payload,'$.score_floor') WHERE job_id=?",
                         (identity.job_id,))
        saved = pick(conn, identity, score=0.79, state="no_token")
        h = 2166136261
        for byte in saved["id"].encode("utf-8"):
            h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
        assert saved["layout"] == {"radius": 0.85 * (1 - 0.79) / 0.5, "angle": math.tau * h / 2**32}
        finish(conn, identity)
        before = storage.canonical_json(storage.get_step_snapshot(conn, identity.step_id)["snapshot"])
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        reopened = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
        assert reopened["step"]["layout_version"] == 1
        assert reopened["picks"][0]["layout"] == saved["layout"]
        assert storage.canonical_json(reopened) == before


def test_hubs_exclude_main_artists_and_repeated_roles_and_via_guesses(db):
    identity = root(db)
    batch(db, identity)
    first = pick(db, identity, name="Main Artist - First", state="ready", facts=shared_facts())
    facts = shared_facts()
    facts["credits"] += [{"id": "6", "name": "Main Artist", "roles": ["Producer"], "scope": "release"},
                         {"id": "9", "name": "Person", "roles": ["Mastered By"], "scope": "track"}]
    second = pick(db, identity, rank=2, name="Other Artist - Second", state="ready", facts=facts)
    hubs = build_hubs([first, second], [])
    assert {hub.hub_key: hub.pick_ranks for hub in hubs} == {"label:42": [1, 2], "person:9": [1, 2]}
    first["facts"], second["facts"] = None, None
    first["facts_state"], second["facts_state"] = "missing", "unavailable"
    first["route"]["via"] = second["route"]["via"] = "label: Guessed"
    assert build_hubs([first, second], []) == []
    bad = hubs[0].to_dict()
    bad["pick_ranks"] = [1, True]
    with pytest.raises(ValidationError):
        Hub.model_validate(bad)


def test_concurrent_duplicate_posts_and_distinct_steps_allocate_once(tmp_path):
    path = tmp_path / "concurrent.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        identity = root(conn)
        batch(conn, identity)
        picks = [pick(conn, identity, rank=i, name=f"Artist {i} - Title {i}") for i in range(1, 4)]
    barrier = threading.Barrier(2)

    def submit(item):
        with closing(storage.connect(path)) as conn:
            barrier.wait(timeout=5)
            return branch(conn, identity, source_point(item))

    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(pool.map(submit, [picks[0], picks[0]]))
        distinct = list(pool.map(submit, picks[1:]))
    assert sum(result.created for result in duplicates) == 1
    assert len({result.job_id for result in duplicates}) == 1
    assert {result.number for result in distinct} == {3, 4}
    with closing(storage.connect(path)) as conn:
        dig = storage.get_dig(conn, identity.dig_id)
        assert [step["number"] for step in dig["steps"]] == [1, 2, 3, 4]
        coords = [(step["island_x"], step["island_y"]) for step in dig["steps"]]
        assert all(math.dist(first, second) >= 340 for i, first in enumerate(coords) for second in coords[i+1:])
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 4


def test_snapshot_cursor_never_runs_ahead_of_rows_during_a_concurrent_commit(tmp_path):
    path = tmp_path / "consistent.db"
    with closing(storage.connect(path)) as reader, closing(storage.connect(path)) as writer:
        storage.migrate(reader)
        identity = root(reader)
        batch(reader, identity)
        committed = []

        def insert_during_snapshot(statement):
            if statement.startswith("SELECT * FROM step_picks") and not committed:
                committed.append(pick(writer, identity))

        reader.set_trace_callback(insert_during_snapshot)
        snapshot = storage.get_step_snapshot(reader, identity.step_id)
        reader.set_trace_callback(None)
        assert len(committed) == 1
        assert snapshot["snapshot"]["picks"] == [] and snapshot["job"]["cursor"] == 1
        fresh = storage.get_step_snapshot(reader, identity.step_id)
        assert fresh["snapshot"]["picks"] == committed and fresh["job"]["cursor"] == 2
        reader.row_factory = sqlite3.Row
        with write(reader):
            original_factory = reader.row_factory
            assert storage.get_dig(reader, identity.dig_id)["step_count"] == 1
            assert reader.in_transaction and reader.row_factory is original_factory


def test_undo_audit_and_first_heard_provenance_survive_reopen(tmp_path):
    path = tmp_path / "undo.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        identity = root(conn)
        batch(conn, identity)
        item = pick(conn, identity)
        finish(conn, identity)
        with write(conn):
            change = storage.change_judgment(conn, track_key=item["track_key"], step_id=identity.step_id,
                                             verdict="keep", expected_revision=0)
            heard = storage.record_heard(conn, track_key=item["track_key"], step_id=identity.step_id)
    with closing(storage.connect(path)) as conn:
        with write(conn):
            undone = storage.change_judgment(conn, track_key=item["track_key"], step_id=identity.step_id,
                                             expected_revision=1, undo_change_id=change["change_id"])
            assert storage.record_heard(conn, track_key=item["track_key"], step_id=identity.step_id) == heard
        assert undone["effective"]["verdict"] is None and undone["revision"] == 2
        assert json.loads(conn.execute("SELECT history FROM judgment_controls").fetchone()[0])[0]["undone"]
