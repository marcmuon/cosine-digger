"""Feed isolation through SQLite repositories, planning and real export flow.

The complete ten-move HTTP scenario lands with S10, after slice routes exist.
"""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from cosine_digger import api, playlists, storage
from cosine_digger.config import load_config
from cosine_digger.models import SimilarTracksData, Track
from cosine_digger.profile import build_feed_profile, build_profile
from cosine_digger.slice_types import Facts, Point, build_hubs
from cosine_digger.youtube import ExportReport

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
OLD = (NOW - timedelta(days=45)).isoformat()


def track(tid, artist, title):
    return Track(id=tid, name=title, artist=artist, track=title, video_id=f"v-{tid}")


LIBRARY = [track("a", "Library A", "Home A"), track("b", "Library B", "Home B")]
OWNED = track("owned", "Owned Artist", "Owned")
BLOCKED = [OWNED, track("served", "Served Artist", "Served"),
           track("known", "Known Artist", "Known"), track("passed", "Passed Artist", "Passed"),
           track("dug-from", "Dug Artist", "Dug"), track("muted", "Muted Artist", "Muted")]
CANDIDATES = [track(f"c{i}", f"Candidate {i}", f"Track {i}") for i in range(1, 7)]
KEPT_NEIGHBORS = [track(f"k{i}", f"Kept Neighbor {i}", f"Discovery {i}") for i in range(1, 7)]


class Cosine:
    def __init__(self):
        self.calls = []

    def search(self, query, limit=20):
        return [item for item in LIBRARY if api.track_label(item) == query]

    def lookup(self, url):
        return []

    def similar(self, track_id, limit=50, **filters):
        self.calls.append(track_id)
        neighbors = (BLOCKED + CANDIDATES if track_id in {"a", "b"}
                     else KEPT_NEIGHBORS if track_id == "c1" else [])
        return SimilarTracksData(source_track=track(track_id, "Source", "Source"),
                                 similar_tracks=[item.model_copy(update={"score": 0.95 - i * 0.02})
                                                 for i, item in enumerate(neighbors[:limit])])

    def close(self):
        pass


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    monkeypatch.setattr(storage, "_now_iso", lambda: STAMP)

    def no_network(*args, **kwargs):
        raise AssertionError("S5 tests must not call the network")

    monkeypatch.setattr("httpx.Client.send", no_network)
    return replace(load_config(), daily_size=6, weekly_size=8, exa_api_key=None, discogs_token=None)


@pytest.fixture
def db(cfg):
    with closing(api.open_db(cfg.db_path)) as conn:
        yield conn


def library(conn, items=LIBRARY):
    api.import_library(conn, [{"id": item.id, "artist": item.artist, "title": item.track,
                               "added_at": STAMP} for item in items], source="folder")
    with conn:
        conn.executemany("UPDATE tracks SET cosine_track_id=?,cosine_verified=1 WHERE path=?",
                         [(item.id, f"folder:{item.id}") for item in items])


def legacy(conn, item, events=(), *, via=None, seeds=()):
    # Legacy feedback is one verdict per result; repeated recordings live in
    # separate sessions, as they do through CLI recording and feedback.
    for event in events or [None]:
        sid = f"legacy-{conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]}"
        with conn:
            conn.execute("INSERT INTO sessions(id,name,seeds,created_at,updated_at) VALUES (?,?,?,?,?)",
                         (sid, sid, json.dumps(seeds), STAMP, STAMP))
            rid = conn.execute("INSERT INTO session_results(session_id,track_id,artist,track_name,video_id,"
                               "hop,via,discovered_at) VALUES (?,?,?,?,?,0,?,?)",
                               (sid, item.id, item.artist, item.track, item.video_id, via, STAMP)).lastrowid
            if event is not None:
                conn.execute("INSERT INTO result_events(result_id,event_type,created_at) VALUES (?,?,?)",
                             (rid, *event))
    return rid


def step(conn, items, *, labels=None, via="label: Move Route"):
    origin = Point(point_id="track:browser|origin", kind="track", name="Browser - Origin",
                   track_key=api._key("Browser", "Origin"), artist="Browser", title="Origin")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        dig_id = storage.create_dig(conn, [origin])
        identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=[origin],
                                       move="seed", further=False)

        def emit(event, data):
            return storage.append_slice_event(conn, identity.job_id, event, data, redact=lambda data: data)

        emit("batch", {"hop": 0, "start_rank": 1, "target_count": len(items), "label": "near"})
        for rank, item in enumerate(items, 1):
            facts = (Facts(labels=[{"id": str(i), "name": name}
                                   for i, name in enumerate(labels[item.id])]).to_dict()
                     if labels is not None and item.id in labels else None)
            emit("pick", {"pick": {"rank": rank, "track_key": api.track_key(item), "cosine_id": item.id,
                 "artist": item.artist, "title": item.track, "video_id": item.video_id, "score": 0.8,
                 "batch": 0, "route": {"move": "seed", "further": False, "hop": 0, "via": via,
                     "branch": None, "parent_via": None, "parent_pick_ranks": [],
                     "seed_scores": {origin.point_id: 0.8}, "nearest_seed": origin.point_id,
                     "layout_seed": origin.point_id, "close_seed_ids": [origin.point_id],
                     "close_count": 1, "seed_count": 1, "attribution": "direct",
                     "branch_seed_scores": {}, "why": "close to Browser - Origin"},
                 "facts_state": "ready" if facts is not None else "no_token", "facts": facts}})
        emit("picks_done", {"pick_count": len(items)})
        snapshot = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
        for hub in build_hubs(snapshot["picks"], snapshot["hubs"]):
            emit("hub", {"hub": hub.to_dict()})
        emit("done", {"status": "done", "pick_count": len(items)})
    return identity.step_id


def judge(conn, step_id, item, verdict, revision=0, **options):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return storage.change_judgment(conn, track_key=api.track_key(item), step_id=step_id,
                                       verdict=verdict, expected_revision=revision, **options)


def legacy_contents(conn):
    return {name: conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("sessions", "session_results", "result_events", "known_tracks")}


def plan(kind, conn, cfg):
    result = playlists.run_playlist(kind, cfg=cfg, conn=conn, client=Cosine(), dry_run=True, now=NOW)
    return {key: value for key, value in result.items() if key != "timings"}


def test_feed_uses_judgments_and_library_only(db, cfg):
    library(db, LIBRARY + [OWNED])
    storage.record_served(db, "previous", [{"track_id": "served", "video_id": "v-served",
                                           "artist": "Served Artist", "title": "Served"}])
    storage.record_known(db, [{"track_id": "known", "video_id": "v-known",
                              "artist": "Known Artist", "title": "Known"}], "seed")
    legacy(db, BLOCKED[3], [("pass", STAMP)])
    legacy(db, BLOCKED[4], [("dig_from", STAMP)], seeds=["Legacy Browser - Seed"])
    with db:
        db.executemany("INSERT INTO muted_leads(lead_type,lead_value,muted_at) VALUES (?,?,?)",
                       [("artist", "Muted Artist", STAMP), ("label", "Muted Label", STAMP)])
    original_tables = legacy_contents(db)
    original_cli = build_profile(db, now=NOW).to_dict()
    original_feed = build_feed_profile(db, now=NOW)
    original_ex = playlists.load_exclusions(db, original_feed, scope="feed")
    before = {kind: plan(kind, db, cfg) for kind in ("daily", "weekly")}
    assert {item["id"] for item in before["daily"]["tracks"]} == {item.id for item in CANDIDATES}
    assert [original_ex.reason(item) for item in BLOCKED] == ["owned", "served", "known", "passed", "known", "muted"]

    sid = step(db, CANDIDATES, labels={item.id: ["Muted Label", "Verified Label"] for item in CANDIDATES})
    step(db, CANDIDATES[:2], labels={item.id: ["Other Label"] for item in CANDIDATES[:2]})
    with db:
        db.execute("BEGIN IMMEDIATE")
        storage.record_heard(db, track_key=api.track_key(CANDIDATES[0]), step_id=sid)
    assert build_feed_profile(db, now=NOW).to_dict() == original_feed.to_dict()
    assert asdict(playlists.load_exclusions(db, original_feed, scope="feed")) == asdict(original_ex)
    assert {kind: plan(kind, db, cfg) for kind in before} == before
    assert legacy_contents(db) == original_tables

    judge(db, sid, CANDIDATES[0], "keep")
    after_keep = plan("daily", db, cfg)
    assert after_keep["tracks"] != before["daily"]["tracks"]
    assert any(item["id"].startswith("k") for item in after_keep["tracks"])
    feed = build_feed_profile(db, now=NOW)
    entities = {entity.key: entity for entity in feed.entities}
    assert entities["candidate 1"].sources == {"save": 4.0}
    assert entities["verified label"].sources == {"save": 1.0}
    assert "other label" not in entities and "move route" not in entities
    judge(db, sid, CANDIDATES[1], "pass")
    for kind in before:
        result = plan(kind, db, cfg)
        assert not {"c1", "c2", *(item.id for item in BLOCKED)} & {item["id"] for item in result["tracks"]}
        assert all(value == 0 for value in result["checks"].values())
    assert legacy_contents(db) == original_tables
    assert build_profile(db, now=NOW).to_dict() == original_cli


def test_browsing_only_feeds_are_empty_and_cannot_meet_count_floors(db, cfg):
    for item in LIBRARY:
        legacy(db, item, [("dig_from", STAMP)], via="label: Browsed Label", seeds=[api.track_label(item)])
    with db:
        db.execute("INSERT INTO discovery_edges(source_type,source_value,target_type,target_value,"
                   "edge_type,weight,discovered_at) VALUES ('artist','Library A','label','Browse Edge',"
                   "'released_on',10,?)", (STAMP,))
        db.execute("INSERT INTO session_branches(session_id,hop,branch_type,branch_value) "
                   "VALUES ('legacy-0',1,'artist','Unjudged Branch')")
    sid = step(db, CANDIDATES, labels={item.id: ["Browse Facts"] for item in CANDIDATES})
    with db:
        db.execute("BEGIN IMMEDIATE")
        storage.record_heard(db, track_key=api.track_key(CANDIDATES[0]), step_id=sid)
    assert build_profile(db, now=NOW).entities
    assert build_profile(db, now=NOW).stats["counts_per_source"]["lead"] > 0
    feed = build_feed_profile(db, now=NOW)
    assert (feed.entities, feed.anchors, feed.edges) == ([], [], [])
    for kind in ("daily", "weekly"):
        client = Cosine()
        with pytest.raises(playlists.PlaylistError, match="keep a track or scan a music folder"):
            playlists.run_playlist(kind, cfg=cfg, conn=db, client=client, dry_run=True, now=NOW)
        assert client.calls == []


@pytest.mark.parametrize("scope", ["feed", "slice"])
@pytest.mark.parametrize("metadata_source", ["via", "edge"])
def test_browsed_legacy_labels_do_not_change_feed_filters_or_spread(db, cfg, scope, metadata_source):
    library(db)
    with db:
        db.execute("INSERT INTO muted_leads(lead_type,lead_value,muted_at) "
                   "VALUES ('label','Browsed Label',?)", (STAMP,))
    profile = build_feed_profile(db, now=NOW)
    exclusions = playlists.load_exclusions(db, profile, scope=scope)
    before = {kind: plan(kind, db, cfg) for kind in ("daily", "weekly")}
    candidate = CANDIDATES[0]
    assert exclusions.reason(candidate) is None

    if metadata_source == "via":
        legacy(db, track("browsed", candidate.artist, "Unjudged"), via="label: Browsed Label")
    else:
        with db:
            db.execute("INSERT INTO discovery_edges(source_type,source_value,target_type,target_value,"
                       "edge_type,weight,discovered_at) VALUES ('artist',?,'label','Browsed Label',"
                       "'released_on',1,?)", (candidate.artist, STAMP))

    assert build_feed_profile(db, now=NOW).to_dict() == profile.to_dict()
    after = playlists.load_exclusions(db, profile, scope=scope)
    assert after == exclusions
    assert after.reason(candidate) is None and after.label_for(candidate) is None
    assert {kind: plan(kind, db, cfg) for kind in before} == before
    # The unrestricted inference remains part of the legacy CLI contract.
    cli = playlists.load_exclusions(db, build_profile(db, now=NOW), scope="legacy")
    assert cli.reason(candidate) == "muted" and cli.label_for(candidate) == "Browsed Label"


@pytest.mark.parametrize("scope", ["feed", "slice"])
def test_label_inference_uses_library_and_effective_judgment_metadata(db, scope):
    library(db, [OWNED])
    with db:
        db.execute("UPDATE tracks SET label='Library Label'")
        db.execute("INSERT INTO muted_leads(lead_type,lead_value,muted_at) "
                   "VALUES ('label','Old Label',?)", (STAMP,))
    item = track("taste", "Taste", "Song")
    legacy(db, item, [("save", OLD)], via="label: Old Label")
    legacy(db, item, [("save", STAMP)], via="label: Effective Label")
    candidate = track("other", "Taste", "Another Song")

    def exclusions():
        return playlists.load_exclusions(db, build_feed_profile(db, now=NOW), scope=scope)

    effective = exclusions()
    assert effective.artist_labels == {"owned artist": "Library Label", "taste": "Effective Label"}
    assert effective.reason(candidate) is None
    cli = playlists.load_exclusions(db, build_profile(db, now=NOW))
    sid = step(db, [item], labels={item.id: ["GUI Label"]})
    judge(db, sid, item, "pass")
    assert exclusions().artist_labels == {"owned artist": "Library Label", "taste": "GUI Label"}
    assert exclusions().reason(candidate) is None
    judge(db, sid, item, None, revision=1)
    assert exclusions().artist_labels == {"owned artist": "Library Label"}
    assert exclusions().reason(candidate) is None
    assert playlists.load_exclusions(db, build_profile(db, now=NOW)) == cli


@pytest.mark.parametrize("latest", ["save", "pass"])
def test_legacy_precedence_gui_override_suppression_and_undo(db, monkeypatch, latest):
    item = track("taste", "Taste", "Song")
    legacy(db, item, [("save", OLD), ("pass", STAMP)], via="label: Legacy Label")
    duplicate = track("duplicate", "Taste (2)", "Song (Original Mix)")
    legacy(db, duplicate, [(latest, STAMP), ("pass" if latest == "save" else "save", OLD)],
           via="label: Winning Legacy Label")
    feed = build_feed_profile(db, now=NOW)
    assert storage.effective_judgment(db, api.track_key(item))["verdict"] == ("keep" if latest == "save" else "pass")
    assert feed.stats["saves"] == (latest == "save") and feed.stats["passes"] == (latest == "pass")
    if latest == "save":
        assert {entity.key: entity.weight for entity in feed.entities} == {"taste": 4.0, "winning legacy label": 2.0}
        assert len(feed.anchors) == 1 and feed.anchors[0].track_id == "duplicate"
    else:
        assert not feed.entities and not feed.anchors

    sid = step(db, [item], labels={item.id: ["Fact One", "Fact Two"]})
    verdict = "pass" if latest == "save" else "keep"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_now_iso", lambda: OLD)
        change = judge(db, sid, item, verdict)
    legacy(db, duplicate, [(latest, (NOW + timedelta(days=1)).isoformat())], via="label: Winning Legacy Label")
    feed = build_feed_profile(db, now=NOW)
    assert feed.stats["saves"] == (verdict == "keep") and feed.stats["passes"] == (verdict == "pass")
    if verdict == "keep":
        assert {entity.key: entity.weight for entity in feed.entities} == {"taste": 2.0, "fact one": 0.5, "fact two": 0.5}
        assert len(feed.anchors) == 1 and feed.anchors[0].track_id == item.id
    else:
        assert not feed.entities and not feed.anchors

    tables = legacy_contents(db)
    suppression = judge(db, sid, item, None, revision=1)
    assert suppression["effective"]["verdict"] is None
    assert not build_feed_profile(db, now=NOW).entities
    ex = playlists.load_exclusions(db, build_feed_profile(db, now=NOW), scope="feed")
    assert not ex.passed_keys and not ex.passed_ids
    assert ex.reason(item) == "known"  # old saves stay independently known
    judge(db, sid, item, None, revision=2, undo_change_id=suppression["change_id"])
    assert build_feed_profile(db, now=NOW).to_dict() == feed.to_dict()
    judge(db, sid, item, None, revision=3, undo_change_id=change["change_id"])
    assert build_feed_profile(db, now=NOW).stats["saves"] == (latest == "save")
    assert legacy_contents(db) == tables


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_feed_verdicts_use_one_snapshot_during_concurrent_toggle_off(db, cfg, caller_transaction):
    item = track("taste", "Taste", "Song")
    legacy(db, item, [("save", STAMP)], via="label: Legacy Label")
    sid = step(db, [item])
    judge(db, sid, item, "pass")
    before = build_feed_profile(db, now=NOW).to_dict()
    assert before["stats"]["saves"] == 0 and before["stats"]["passes"] == 1
    db.row_factory = sqlite3.Row

    with closing(storage.connect(cfg.db_path)) as writer:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        if caller_transaction:
            db.execute("BEGIN")
        changes = []

        def toggle_after_controls(statement):
            # This SELECT follows the fully consumed controls query. Commit the
            # toggle on another connection before the GUI rows are read.
            if statement.startswith("SELECT j.track_key,j.verdict") and not changes:
                changes.append(judge(writer, sid, item, None, revision=1))

        db.set_trace_callback(toggle_after_controls)
        try:
            observed = build_feed_profile(db, now=NOW).to_dict()
        finally:
            db.set_trace_callback(None)
        assert len(changes) == 1 and changes[0]["effective"]["source"] == "none"
        assert observed == before  # No transient resurrection of the +4 save.
        assert db.in_transaction is caller_transaction and db.row_factory is sqlite3.Row
        if caller_transaction:
            assert storage.effective_judgment(db, api.track_key(item))["verdict"] == "pass"
            db.rollback()

    after = build_feed_profile(db, now=NOW)
    assert after.stats["saves"] == after.stats["passes"] == 0
    assert not after.entities and not after.anchors


def test_decay_split_labels_and_negative_artists_use_judged_source_facts(db, monkeypatch):
    items = [track("old-keep", "Taste", "Old"), track("new-keep", "Taste", "New"),
             track("pass", "Taste", "No"), track("pass-only", "Passed Only", "No")]
    labels = {item.id: ["First Label", "Second Label"] for item in items[:3]}
    sid = step(db, items, labels=labels)
    step(db, items[:1], labels={items[0].id: ["Other Copy Label"]})
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_now_iso", lambda: OLD)
        judge(db, sid, items[0], "keep")
    judge(db, sid, items[1], "keep")
    judge(db, sid, items[2], "pass")
    judge(db, sid, items[3], "pass")
    profile = build_feed_profile(db, now=NOW)
    entities = {entity.key: entity for entity in profile.entities}
    assert entities["taste"].weight == 4.0 and entities["taste"].sources == {"pass": -2.0, "save": 6.0}
    assert entities["first label"].weight == entities["second label"].weight == 1.0
    assert set(entities) == {"taste", "first label", "second label"}
    assert {(anchor.title, anchor.weight) for anchor in profile.anchors} == {("Old", 2.0), ("New", 4.0)}
    later = build_feed_profile(db, now=NOW + timedelta(days=45))
    assert {entity.key: entity.weight for entity in later.entities} == {key: entity.weight / 2 for key, entity in entities.items()}


def test_scoped_exclusions_preserve_ledgers_and_do_not_change_cli_defaults(db):
    library(db, [OWNED])
    for item in BLOCKED[1:3]:
        if item.id == "served":
            storage.record_served(db, "old", [{"track_id": item.id, "video_id": item.video_id,
                                               "artist": item.artist, "title": item.track}])
        else:
            storage.record_known(db, [{"track_id": item.id, "video_id": item.video_id,
                                      "artist": item.artist, "title": item.track}], "seed")
    keep, passed = CANDIDATES[:2]
    items = [OWNED, *BLOCKED[1:3], keep, passed]
    sid = step(db, items)
    cli = build_profile(db, now=NOW)
    old_ex = playlists.load_exclusions(db, cli)
    old_tables = legacy_contents(db)
    for item in items:
        judge(db, sid, item, "keep" if item == keep else "pass")
    profile = build_feed_profile(db, now=NOW)
    feed_ex = playlists.load_exclusions(db, profile, scope="feed")
    assert feed_ex == playlists.load_exclusions(db, profile, scope="slice")
    assert playlists.load_exclusions(db, cli) == old_ex
    assert [feed_ex.reason(item) for item in items] == ["owned", "served", "passed", "known", "passed"]
    for item, reason in ((keep, "known"), (passed, "passed")):
        assert feed_ex.reason(item.model_copy(update={"artist": "Renamed", "track": "Different"})) == reason
        assert feed_ex.reason(item.model_copy(update={"id": "other", "artist": "Renamed", "track": "Different"})) == reason
    for item in items:
        judge(db, sid, item, None, revision=1)
    ex = playlists.load_exclusions(db, build_feed_profile(db, now=NOW), scope="feed")
    assert [ex.reason(item) for item in items] == ["owned", "served", "known", None, None]
    assert legacy_contents(db) == old_tables


def test_effective_exclusions_retain_legacy_recording_ids_and_seed_history(db):
    seeded = track("old-seed", "Seed Artist", "Seed Track")
    legacy(db, seeded, seeds=[api.track_label(seeded)])
    first = track("old-pass", "Taste", "Song")
    second = track("new-pass", "Taste", "Song (Original Mix)")
    legacy(db, first, [("pass", OLD)])
    legacy(db, second, [("pass", STAMP)])
    gui_copy = track("gui-copy", "Taste", "Song")
    sid = step(db, [gui_copy])
    profile = build_feed_profile(db, now=NOW)
    assert not profile.anchors
    for scope in ("feed", "slice"):
        ex = playlists.load_exclusions(db, profile, scope=scope)
        assert ex.reason(seeded.model_copy(update={"artist": "Renamed", "track": "New title"})) == "known"
        assert ex.reason(first.model_copy(update={"artist": "Renamed", "track": "New title"})) == "passed"
        assert ex.reason(second.model_copy(update={"artist": "Renamed", "track": "New title"})) == "passed"
    judge(db, sid, gui_copy, "keep")
    profile = build_feed_profile(db, now=NOW)
    ex = playlists.load_exclusions(db, profile, scope="feed")
    assert not ex.passed_ids and not ex.passed_keys
    assert ex.reason(first.model_copy(update={"artist": "Renamed", "track": "New title"})) == "known"
    # A new unjudged copy supplies no new feed metadata or identity aliases.
    step(db, [track("unjudged-copy", "Taste", "Song")], labels={"unjudged-copy": ["New Label"]})
    assert playlists.load_exclusions(db, build_feed_profile(db, now=NOW), scope="feed") == ex
    assert build_feed_profile(db, now=NOW).to_dict() == profile.to_dict()
    judge(db, sid, gui_copy, None, revision=1)
    ex = playlists.load_exclusions(db, build_feed_profile(db, now=NOW), scope="feed")
    assert ex.reason(first) is None and ex.reason(second) is None


def test_library_math_anchor_limits_and_read_only_compatibility(db, cfg):
    items = [track(f"l{i}", f"Artist {i:03d}", "Home") for i in range(205)]
    library(db, items + [track("second", "Artist 000", "Second"), track("third", "Artist 000", "Third")])
    expected = build_profile(db, now=NOW).to_dict()
    assert build_feed_profile(db, now=NOW).to_dict() == expected
    assert len(expected["anchors"]) == 200
    assert sum(anchor["artist"] == "Artist 000" for anchor in expected["anchors"]) == 2
    with closing(sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        assert build_feed_profile(reader, now=NOW).to_dict() == expected
        assert reader.total_changes == 0 and reader.row_factory is sqlite3.Row
    with closing(sqlite3.connect(":memory:")) as bare:
        profile = build_feed_profile(bare, now=NOW)
        assert not profile.entities and not profile.anchors and not profile.edges and bare.total_changes == 0


@pytest.mark.parametrize("verdict", [None, "keep", "pass"])
def test_export_rechecks_effective_judgments_and_keeps_the_verified_ledger(db, cfg, monkeypatch, verdict):
    library(db, LIBRARY + BLOCKED)
    sid = step(db, CANDIDATES)
    real_plan = playlists.plan_playlist

    def plan_then_change(*args, **kwargs):
        result = real_plan(*args, **kwargs)
        if verdict is not None:
            judge(db, sid, CANDIDATES[0], verdict)
        else:
            step(db, CANDIDATES, labels={item.id: ["Late Muted Label"] for item in CANDIDATES})
            with db:
                db.execute("INSERT INTO muted_leads(lead_type,lead_value,muted_at) "
                           "VALUES ('label','Late Muted Label',?)", (STAMP,))
        return result

    class Sink:
        def replace_playlist(self, name, video_ids, description="", **kwargs):
            self.videos = video_ids
            return ExportReport(playlist_id="test", playlist_url="https://music.youtube.com/playlist?list=test",
                                created=True, requested=video_ids, added=video_ids, missing=[], skipped=[],
                                failed=[], verified=True)

    monkeypatch.setattr(playlists, "plan_playlist", plan_then_change)
    sink = Sink()
    result = playlists.run_playlist("daily", cfg=cfg, conn=db, client=Cosine(), sink=sink, now=NOW)
    expected = {item.video_id for item in CANDIDATES if verdict is None or item.id != "c1"}
    assert set(sink.videos) == expected
    assert result["export"]["verified"] and result["export"]["missing"] == result["export"]["failed"] == []
    assert result["export"]["omitted"] == ([] if verdict is None else ["c1"])
    assert {row[0] for row in db.execute("SELECT video_id FROM served_ledger")} == expected
    assert db.execute("SELECT COUNT(*) FROM known_tracks").fetchone()[0] == 0
    monkeypatch.setattr(playlists, "plan_playlist", real_plan)
    after = plan("daily", db, cfg)
    if verdict == "keep":
        assert after["tracks"] and all(item["id"].startswith("k") for item in after["tracks"])
    else:
        assert after["tracks"] == []
