"""Profile behavior through the real SQLite schema and registered CLI."""
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

import pytest
from typer.testing import CliRunner

from cosine_digger import storage
from cosine_digger.cli import app
from cosine_digger.profile import build_profile

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "taste.db"
    conn = storage.connect(path)
    storage.migrate(conn)
    yield conn, path
    conn.close()


def session(conn, sid, seeds=(), timestamp=STAMP):
    conn.execute(
        "INSERT INTO sessions(id, name, seeds, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (sid, sid, json.dumps(seeds), timestamp, timestamp),
    )


def result(conn, sid, artist, *, title="Track", event=None, via=None, timestamp=STAMP):
    track_id = f"{sid}-{artist}-{title}"
    cursor = conn.execute(
        """INSERT INTO session_results(session_id, track_id, artist, track_name, video_id,
                                       hop, via, discovered_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (sid, track_id, artist, title, f"video-{track_id}", via, timestamp),
    )
    if event:
        conn.execute(
            "INSERT INTO result_events(result_id, event_type, created_at) VALUES (?, ?, ?)",
            (cursor.lastrowid, event, timestamp),
        )
    return track_id


def library(conn, artist, count=1, recency=0.8, timestamp=STAMP):
    conn.execute(
        """INSERT INTO taste_signals(signal_type, signal_value, track_count,
                                     recency_score_365d, latest_mtime) VALUES ('artist', ?, ?, ?, ?)""",
        (artist, count, recency, timestamp),
    )


def test_mixed_evidence_ranking_anchors_and_mutes(db):
    conn, _ = db
    session(conn, "dig", ["Seed - Track", "Passed - Track", "Unrated - Track",
                          "https://example.com/Artist - URL"])
    seed_id = result(conn, "dig", "Seed")
    saved_id = result(conn, "dig", "Saved", event="save", via="label: Muted (2)")
    result(conn, "dig", "Passed", event="pass")
    result(conn, "dig", "Digging", event="dig_from", via="label: Alive")
    result(conn, "dig", "Only passed", event="pass")
    library(conn, "Library Only", count=4)
    conn.execute(
        "INSERT INTO muted_leads(lead_type, lead_value, muted_at) VALUES ('label', '  MUTED ', ?)",
        (STAMP,),
    )
    conn.execute(
        """INSERT INTO taste_signals(signal_type, signal_value, track_count, recency_score_365d)
           VALUES ('label', 'Muted', 20, 1)"""
    )
    conn.execute(
        """INSERT INTO tracks(path, artist, title, label, cosine_track_id, mtime)
           VALUES ('library.flac', 'Library Only', 'Library Track', 'Muted', 'library-id', ?)""",
        (STAMP,),
    )
    profile = build_profile(conn, now=NOW)
    artists = {entity.key: entity for entity in profile.top("artist", 20)}
    assert artists["saved"].weight == 4 > artists["seed"].weight == 3
    assert artists["passed"].weight == 1 < artists["unrated"].weight == 3
    assert artists["digging"].sources == {"dig_from": 5}
    assert artists["passed"].sources == {"dig_seed": 3, "pass": -2}
    assert artists["library only"].sources == {"library": pytest.approx(math.log1p(4) * 1.3)}
    assert "only passed" not in artists
    assert "https://example.com/artist" not in artists
    assert [entity.key for entity in profile.top("label", 20)] == ["alive"]
    assert profile.top("label", 1)[0].weight == 2.5
    assert all(edge.target_key != "muted" for edge in profile.edges)
    saved = next(anchor for anchor in profile.anchors if anchor.source == "save")
    assert (saved.track_id, saved.video_id) == (saved_id, f"video-{saved_id}")
    seed = next(anchor for anchor in profile.anchors if anchor.artist == "Seed")
    assert seed.track_id == seed_id
    assert next(anchor for anchor in profile.anchors if anchor.artist == "Unrated").track_id is None
    owned = next(anchor for anchor in profile.anchors if anchor.source == "library")
    assert owned.weight == pytest.approx(artists["library only"].weight)  # its only anchor
    assert profile.stats["sessions"] == 1
    assert profile.stats["saves"] == 1
    assert profile.stats["passes"] == 2
    assert profile.stats["library_tracks"] == 1
    assert profile.stats["generated_at"] == profile.generated_at == STAMP
    assert all(entity.weight == pytest.approx(sum(entity.sources.values())) for entity in profile.entities)


def test_event_decay_uses_reaction_time_and_library_keeps_its_recency(db):
    conn, _ = db
    old = (NOW - timedelta(days=45)).isoformat()
    session(conn, "old", ["Old Seed - Track"], old)
    session(conn, "new", ["New Seed - Track"])
    result(conn, "old", "Old Save", event="save", via="label: Old Label", timestamp=old)
    result(conn, "old", "New Save", event="save", via="label: New Label")
    conn.execute("UPDATE session_results SET discovered_at = ?", (old,))
    library(conn, "Library", count=2, timestamp=old)
    profile = build_profile(conn, now=NOW, half_life_days=45)
    entities = {entity.key: entity for entity in profile.entities}
    assert entities["old seed"].weight == entities["new seed"].weight / 2
    assert entities["old save"].weight == entities["new save"].weight / 2
    assert entities["old label"].weight == entities["new label"].weight / 2
    assert entities["library"].weight == pytest.approx(math.log1p(2) * 1.3)
    later = build_profile(conn, now=NOW + timedelta(days=45))
    for entity in later.entities:
        factor = 1 if entity.key == "library" else 0.5
        assert entity.weight == pytest.approx(entities[entity.key].weight * factor)


def test_leads_edges_and_latest_display_are_shared_and_deterministic(db):
    conn, _ = db
    old = (NOW - timedelta(days=45)).isoformat()
    session(conn, "one", ["ARTIST (2) - Track"], old)
    session(conn, "two", ["Different - Track"])
    result(conn, "one", "ARTIST (2)", via="label: Label", timestamp=old)
    result(conn, "two", "Artist", via="label: LABEL")
    library(conn, "artist", count=1, timestamp=old)
    conn.execute(
        "INSERT INTO tracks(path, artist, label, mtime) VALUES ('one.flac', 'artist', 'Label', ?)",
        (old,),
    )
    conn.execute(
        """INSERT INTO discovery_edges(source_type, source_value, target_type, target_value,
                                       edge_type, weight, discovered_at)
           VALUES ('artist', 'Artist (2)', 'label', 'LABEL', 'released_on', 2, ?)""", (old,),
    )
    for sid in ("one", "two"):
        conn.execute(
            """INSERT INTO session_branches(session_id, hop, branch_type, branch_value)
               VALUES (?, 1, 'artist', 'Branch Artist')""", (sid,),
        )
    changes = conn.total_changes
    conn.row_factory = sqlite3.Row
    profile = build_profile(conn, now=NOW)
    assert conn.total_changes == changes
    assert conn.row_factory is sqlite3.Row
    assert profile.to_dict() == build_profile(conn, now=NOW).to_dict()
    artist = next(entity for entity in profile.entities if entity.key == "artist")
    assert (artist.name, artist.last_seen) == ("Artist", STAMP)
    assert profile.top("label", 1)[0].sources == {"lead": 1.5}
    assert profile.top("label", 1)[0].name == "LABEL"
    assert next(entity for entity in profile.entities if entity.key == "branch artist").weight == 1.5
    assert len(profile.edges) == 1
    assert profile.edges[0].weight == 5
    assert profile.edges[0].source_key == "artist"
    assert profile.edges[0].target_key == "label"
    assert profile.edges[0].kind == "released_on"
    assert profile.stats["counts_per_source"]["lead"] == 2
    # Branch-derived evidence survives an absent result table on a partial DB.
    conn.execute("DROP TABLE session_results")
    partial = build_profile(conn, now=NOW)
    assert next(entity for entity in partial.entities if entity.key == "branch artist").weight == 1.5


def test_library_anchors_two_per_artist_verified_then_recent(db):
    conn, _ = db
    library(conn, "Library", count=205)
    conn.executemany(
        "INSERT INTO tracks(path, artist, title, cosine_track_id, mtime) VALUES (?, 'Library', ?, ?, ?)",
        [(f"{i}.flac", f"Track {i:03d}", str(i) if i == 7 else None, f"2026-01-{1 + i % 28:02d}T00:00:00+00:00")
         for i in range(205)],
    )
    session(conn, "saved")
    result(conn, "saved", "Saved", event="save")
    profile = build_profile(conn, now=NOW)
    owned = [anchor for anchor in profile.anchors if anchor.source == "library"]
    assert profile.anchors[0].source == "save"
    assert [(a.title, a.track_id) for a in owned] == [("Track 007", "7"), ("Track 027", None)]
    assert all(a.weight == pytest.approx(math.log1p(205) * 1.3 / 2) for a in owned)


def test_anchor_limit_is_global(db):
    conn, _ = db
    for i in range(205):
        library(conn, f"Artist {i:03d}", count=1)
        conn.execute("INSERT INTO tracks(path, artist, title) VALUES (?, ?, 'Song')", (f"{i}.flac", f"Artist {i:03d}"))
    session(conn, "saved")
    result(conn, "saved", "Saved", event="save")
    profile = build_profile(conn, now=NOW)
    assert len(profile.anchors) == 200 and profile.anchors[0].source == "save"


def test_empty_databases_and_cli(db, monkeypatch, tmp_path):
    conn, path = db
    assert build_profile(conn, now=NOW).entities == []
    with sqlite3.connect(":memory:") as bare:
        empty = build_profile(bare, now=NOW)
        assert (empty.entities, empty.anchors, empty.edges) == ([], [], [])
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(path))
    runner = CliRunner()
    assert "Run a dig or `taste-scan`" in runner.invoke(app, ["profile"]).output
    session(conn, "cli", ["Seed - Track"])
    result(conn, "cli", "Saved", event="save", via="label: Label")
    conn.commit()
    output = runner.invoke(app, ["profile", "--json"])
    assert output.exit_code == 0, output.output
    payload = json.loads(output.output)
    assert payload["stats"]["saves"] == 1
    assert payload["entities"][0]["key"] == "saved"
    assert payload["anchors"][0]["video_id"] == "video-cli-Saved-Track"
    human = runner.invoke(app, ["profile", "--top", "1"])
    assert human.exit_code == 0, human.output
    assert "Top artists" in human.output and "Top labels" in human.output
    assert "save" in human.output and "Saved" in human.output
    assert "Seed" not in human.output
    missing = tmp_path / "missing" / "taste.db"
    monkeypatch.setenv("COSINE_DIGGER_DB", str(missing))
    assert "Run a dig or `taste-scan`" in runner.invoke(app, ["profile"]).output
    output = runner.invoke(app, ["profile", "--json"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["entities"] == []
    assert not missing.exists()


def test_catalogue_placeholders_are_not_taste(db):
    conn, _ = db
    library(conn, "Various Artists", count=50)
    library(conn, "Real Artist", count=3)
    conn.execute("""INSERT INTO taste_signals(signal_type, signal_value, track_count, recency_score_365d)
                    VALUES ('label', '[no label]', 40, 1)""")
    conn.execute("INSERT INTO tracks(path, artist, title, label) VALUES ('a', 'Various Artists', 'Comp', 'Not On Label')")
    profile = build_profile(conn, now=NOW)
    assert [e.name for e in profile.entities] == ["Real Artist"]
    assert not profile.edges and not profile.anchors


def test_repeats_merge_and_negative_artists_lose_their_anchors(db):
    conn, _ = db
    session(conn, "many", ["Loud - Hit"] * 250 + ["Quiet - Gem"])
    session(conn, "meh", ["Meh - Track"])
    result(conn, "meh", "Meh", title="One", event="pass")
    result(conn, "meh", "Meh", title="Two", event="pass")
    profile = build_profile(conn, now=NOW)
    titles = [(a.artist, a.title) for a in profile.anchors]
    assert titles.count(("Loud", "Hit")) == 1 and ("Quiet", "Gem") in titles
    assert all(a.artist != "Meh" for a in profile.anchors)
