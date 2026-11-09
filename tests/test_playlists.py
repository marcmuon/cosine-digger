"""Daily playlists end to end (profile from real digs, fake cosine.club and
YouTube Music), plus the selection rules that keep a playlist varied."""
import dataclasses
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from datetime import datetime

import pytest

from cosine_digger import api
from cosine_digger.config import load_config
from cosine_digger.models import SimilarTracksData, Track
from cosine_digger.playlists import (
    RECIPES, Exclusions, PlanTrack, PlaylistError, interleave, run_playlist, sample_anchors, select,
)
from cosine_digger.profile import Anchor
from cosine_digger.storage import migrate


def t(tid, artist, title):
    return Track(id=tid, name=title, artist=artist, track=title, video_id=f"v{tid}")


SEEDS = {"Seed Artist - First": t("a", "Seed Artist", "First"),
         "Other Seed - Second": t("b", "Other Seed", "Second")}
NEIGHBORS = {
    "a": [t("1", "Near", "One"), t("2", "Owned Artist", "Owned Track"), t("3", "Shared", "Both"),
          t("4", "Muted Artist", "Hidden"), t("5", "Near", "Again"), t("6", "Six", "Six"),
          t("7", "Seven", "Seven"), t("8", "Eight", "Eight")],
    "b": [t("3", "Shared", "Both"), t("9", "Nine", "Nine"), t("10", "Ten", "Ten"),
          t("11", "Eleven", "Eleven"), t("12", "Twelve", "Twelve"), t("a", "Seed Artist", "First")],
}


class Cosine:
    def search(self, query, limit=20):
        return [SEEDS[query]] if query in SEEDS else []

    def lookup(self, url):
        return []

    def similar(self, track_id, limit=50, **filters):
        return SimilarTracksData(
            source_track=Track(id=track_id, name="", artist="", track=""),
            similar_tracks=[x.model_copy(update={"score": 0.9 - i * 0.05})
                            for i, x in enumerate(NEIGHBORS.get(track_id, []))])

    def close(self):
        pass


class Sink:
    def __init__(self):
        self.calls = []

    def replace_playlist(self, name, video_ids, description="", **_):
        from cosine_digger.youtube import ExportReport

        self.calls.append((name, list(video_ids)))
        return ExportReport(playlist_id="PL", playlist_url="https://music.youtube.com/playlist?list=PL",
                            created=not self.calls[:-1], requested=list(video_ids), added=list(video_ids),
                            missing=[], skipped=[], failed=[], verified=True)


def import_seed_library(conn):
    api.import_library(conn, [{"id": track.id, "artist": track.artist, "title": track.track}
                              for track in SEEDS.values()], source="test-library")


def test_daily_never_serves_owned_muted_known_or_already_served(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=5, exa_api_key=None)  # 20% fresh = 1 slot
    conn, client, sink = sqlite3.connect(":memory:"), Cosine(), Sink()
    migrate(conn)
    # The library teaches taste and owns one neighbour; one artist is muted.
    import_seed_library(conn)
    # The library file carries "(Original Mix)": still the same recording as cosine.club's "Owned Track".
    conn.execute("INSERT INTO tracks (path, artist, title) VALUES ('/m/x.flac', 'Owned Artist', 'Owned Track (Original Mix)')")
    conn.execute("INSERT INTO muted_leads (lead_type, lead_value, muted_at) VALUES ('artist', 'Muted Artist', 'x')")
    conn.execute("INSERT INTO fresh_finds (query, source_url, artist, title, video_id, found_at) "
                 "VALUES ('q', 'https://www.boomkat.com/p/x', 'Fresh Face', 'New One', 'vf', 'now')")

    first = run_playlist("daily", cfg=cfg, conn=conn, client=client, sink=sink, now=datetime(2026, 9, 29))
    ids = [x["id"] for x in first["tracks"]]
    assert len(ids) == 5 and first["export"]["verified"]
    assert not {"2", "4", "a", "b"} & set(ids)                    # owned, muted, seeds
    assert len({x["artist"] for x in first["tracks"]}) == 5        # one track per artist
    assert all(v == 0 for v in first["checks"].values())
    fresh = [x for x in first["tracks"] if x["source"] == "fresh"]
    assert fresh and fresh[0]["why"] == "New release spotted on boomkat.com"
    assert sink.calls[0] == ("Digger Daily", [x["video_id"] for x in first["tracks"]])

    second = run_playlist("daily", cfg=cfg, conn=conn, client=client, sink=sink, now=datetime(2026, 9, 30))
    assert second["tracks"] and not set(ids) & {x["id"] for x in second["tracks"]}
    assert all(v == 0 for v in second["checks"].values())


def test_a_cosine_outage_is_not_remembered_as_a_miss(tmp_path, monkeypatch):
    """Seen by review: a 503 while resolving library anchors was cached as
    "not on cosine.club" for a week, so playlists stayed empty after recovery."""
    from cosine_digger.client import CosineHTTPError

    class Down(Cosine):
        def search(self, query, limit=20):
            raise CosineHTTPError(503, "Service Unavailable", {})
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=3, exa_api_key=None)
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    api.import_library(conn, [{"id": "1", "artist": "Seed Artist", "title": "First"},
                              {"id": "2", "artist": "Other Seed", "title": "Second"}], source="mediaserver")
    with pytest.raises(PlaylistError, match="cosine.club didn't answer"):
        run_playlist("daily", cfg=cfg, conn=conn, client=Down(), dry_run=True)
    back = run_playlist("daily", cfg=cfg, conn=conn, client=Cosine(), dry_run=True)
    assert len(back["tracks"]) == 3


class ArtistsOnly(Cosine):
    """cosine.club lacks the library's own tracks but has other tracks by
    the same artists, found by searching the bare artist name."""
    BY_ARTIST = {"Seed Artist": [t("a", "Seed Artist", "First"), t("z", "Seed Artistry", "Not Them")],
                 "Other Seed": [t("b", "Other Seed", "Second")]}

    def __init__(self):
        self.searches = []

    def search(self, query, limit=20):
        self.searches.append(query)
        return self.BY_ARTIST.get(query, [])


LOST = [{"id": "1", "artist": "Seed Artist", "title": "Lost Song"},
        {"id": "2", "artist": "Other Seed", "title": "Gone Song"}]


def test_anchors_missing_from_cosine_dig_from_their_artists_tracks(tmp_path, monkeypatch):
    """Most library tracks aren't on cosine.club while their artists are:
    such an anchor digs from that artist's tracks, the why names the artist,
    and the lookup is cached so the next run searches nothing."""
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=4, exa_api_key=None)
    conn, client = sqlite3.connect(":memory:"), ArtistsOnly()
    migrate(conn)
    api.import_library(conn, LOST, source="mediaserver")

    first = run_playlist("daily", cfg=cfg, conn=conn, client=client, dry_run=True, now=datetime(2026, 9, 29))
    assert len(first["tracks"]) == 4 and all(v == 0 for v in first["checks"].values())
    assert {"Seed Artist", "Other Seed"} <= set(client.searches)
    for x in first["tracks"]:
        assert x["why"].startswith(("Close to Seed Artist", "Close to Other Seed")), x["why"]
        assert "Lost Song" not in x["why"] and "Gone Song" not in x["why"]
    assert "3" in {x["id"] for x in first["tracks"]}  # "Shared" is close to both artists

    client.searches.clear()
    again = run_playlist("daily", cfg=cfg, conn=conn, client=client, dry_run=True, now=datetime(2026, 9, 29))
    assert client.searches == [] and len(again["tracks"]) == 4


@pytest.mark.parametrize("failure", ["503", "timeout", "bad json"])
def test_an_outage_while_looking_up_an_anchor_artist_is_named_and_not_remembered(tmp_path, monkeypatch, failure):
    """Seen by review: with the anchors' own tracks missing, an outage in the
    artist lookup read as "none of your anchors are on cosine.club"."""
    from cosine_digger.client import CosineHTTPError, CosineRequestError, CosineResponseError

    error = {"503": CosineHTTPError(503, "Service Unavailable", {}),
             "timeout": CosineRequestError("Request failed: ReadTimeout"),
             "bad json": CosineResponseError("Invalid JSON response")}[failure]

    class ArtistSearchDown(ArtistsOnly):
        def search(self, query, limit=20):
            if query in self.BY_ARTIST:
                raise error
            return super().search(query, limit)
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=4, exa_api_key=None)
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    api.import_library(conn, LOST, source="mediaserver")
    with pytest.raises(PlaylistError, match="cosine.club didn't answer"):
        run_playlist("daily", cfg=cfg, conn=conn, client=ArtistSearchDown(), dry_run=True)
    back = run_playlist("daily", cfg=cfg, conn=conn, client=ArtistsOnly(), dry_run=True)
    assert len(back["tracks"]) == 4


def test_a_rejected_api_key_is_named_not_mistaken_for_missing_artists(tmp_path, monkeypatch):
    """Seen by review: with the anchors' own tracks cached as misses, a bad key
    surfaced only in the artist lookup and read as "not on cosine.club"."""
    from cosine_digger.client import CosineAuthError

    class KeyRevoked(ArtistsOnly):
        def search(self, query, limit=20):
            if query in self.BY_ARTIST:
                raise CosineAuthError("Unauthorized: check COSINE_API_TOKEN")
            return super().search(query, limit)
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=4, exa_api_key=None)
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    api.import_library(conn, LOST, source="mediaserver")
    with pytest.raises(CosineAuthError, match="COSINE_API_TOKEN"):
        run_playlist("daily", cfg=cfg, conn=conn, client=KeyRevoked(), dry_run=True)
    assert len(run_playlist("daily", cfg=cfg, conn=conn, client=ArtistsOnly(), dry_run=True)["tracks"]) == 4


def test_dry_run_leaves_youtube_and_ledger_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=3, exa_api_key=None)
    conn, client = sqlite3.connect(":memory:"), Cosine()
    migrate(conn)
    import_seed_library(conn)
    out = run_playlist("daily", cfg=cfg, conn=conn, client=client, dry_run=True)
    assert len(out["tracks"]) == 3 and out["export"] is None
    assert conn.execute("SELECT COUNT(*) FROM served_ledger").fetchone()[0] == 0


def _pt(tid, artist, label=None, source="similar"):
    return PlanTrack(track=t(tid, artist, tid), score=1.0, why="because", label=label, source=source)


def test_select_spreads_artists_and_labels_then_relaxes_when_short():
    stream = [_pt("1", "A", "L"), _pt("2", "A", "L"), _pt("3", "B", "L"), _pt("4", "C", "L"),
              _pt("5", "D", "L"), _pt("6", "E")]
    daily = RECIPES["daily"]  # one per artist, three per label
    chosen, _ = select(stream, 4, daily, Exclusions(), fresh_cap=0)
    assert [p.track.id for p in chosen] == ["1", "3", "4", "6"]
    chosen, _ = select(stream, 6, daily, Exclusions(), fresh_cap=0)
    assert len(chosen) == 6  # caps relax before the list comes up short


def test_interleave_gives_each_stream_its_share():
    fresh = [_pt(f"f{i}", f"F{i}", source="fresh") for i in range(10)]
    similar = [_pt(f"s{i}", f"S{i}") for i in range(10)]
    merged = interleave([(fresh, 0.2), (similar, 0.8)])
    assert [p.source for p in merged[:10]].count("fresh") == 2
    assert len(merged) == 20


def test_anchor_sampling_is_one_per_artist_and_stable_per_day():
    anchors = [Anchor(f"Artist {i % 12}", f"T{i}", None, None, "seed", 30 - i) for i in range(30)]
    pick = lambda day: sample_anchors(anchors, 5, random.Random(f"daily:{day}"))
    assert len({a.artist for a in pick("2026-09-29")}) == 5
    assert pick("2026-09-29") == pick("2026-09-29")


def test_label_mutes_and_old_saves_reach_similarity_picks(tmp_path, monkeypatch):
    from cosine_digger.playlists import load_exclusions
    from cosine_digger.profile import build_profile
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    api.quick(list(SEEDS), client=Cosine(), conn=conn, export_playlist=False)
    # The library says "Nine" releases on a label you muted; you once saved "Ten".
    conn.execute("INSERT INTO tracks (path, artist, title, label) VALUES ('/m/9.flac', 'Nine', 'Other', 'Muted Label')")
    conn.execute("INSERT INTO muted_leads (lead_type, lead_value, muted_at) VALUES ('label', 'Muted Label', 'x')")
    rid = conn.execute("INSERT INTO session_results (session_id, track_id, artist, track_name, video_id, hop, "
                       "discovered_at) VALUES ('old', '10', 'Ten', 'Ten', 'v10', 1, 'x')").lastrowid
    conn.execute("INSERT INTO result_events (result_id, event_type, created_at) VALUES (?, 'save', 'x')", (rid,))
    ex = load_exclusions(conn, build_profile(conn))
    assert ex.reason(t("9", "Nine", "Nine")) == "muted"   # no label on the candidate itself
    assert ex.reason(t("10", "Renamed", "Different")) == "known"  # same recording id as the save


def test_a_fresh_copy_never_shortens_the_playlist():
    # The fresh quota turns away the fresh copy of "Dup"; its similarity copy still counts.
    stream = [_pt("f1", "F1", source="fresh"), _pt("x", "Dup", source="fresh"), _pt("x", "Dup")] + \
             [_pt(f"s{i}", f"S{i}") for i in range(3)]
    chosen, _ = select(stream, 5, RECIPES["daily"], Exclusions(), fresh_cap=1)
    assert len(chosen) == 5
    assert [(p.track.id, p.source) for p in chosen][:2] == [("f1", "fresh"), ("x", "similar")]


def test_scheduled_run_fails_when_youtube_did_not_confirm(tmp_path, monkeypatch):
    from cosine_digger import playlists
    monkeypatch.setattr(playlists, "run_playlist", lambda job, **_: {
        "tracks": [{"id": "1"}], "warnings": [],
        "export": {"verified": False, "missing": ["v1"], "failed": [], "playlist_url": "u"}})
    import pytest
    with pytest.raises(playlists.PlaylistError, match="not verified"):
        playlists.make_runner(load_config())("daily", datetime(2026, 9, 29))


def test_a_label_on_any_copy_of_a_recording_reaches_every_copy():
    ex = Exclusions(muted_labels={"muted label"})
    stream = [_pt("x", "Artist"), _pt("x", "Artist", "Muted Label", source="chain"), _pt("y", "Other")]
    chosen, dropped = select(stream, 2, RECIPES["daily"], ex, fresh_cap=0)
    assert [p.track.id for p in chosen] == ["y"] and dropped["muted"] == 2


def test_seed_recordings_stay_known_under_new_metadata():
    from cosine_digger.playlists import load_exclusions
    from cosine_digger.profile import build_profile
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    api.quick(list(SEEDS), client=Cosine(), conn=conn, export_playlist=False)
    ex = load_exclusions(conn, build_profile(conn))
    assert ex.reason(t("a", "Seed Artist (feat. X)", "First (Remaster)")) == "known"


def test_a_muted_label_on_any_copy_mutes_the_recording():
    ex = Exclusions(muted_labels={"muted label"})
    stream = [_pt("x", "Artist", "Allowed Label"), _pt("x", "Artist", "Muted Label", source="chain"), _pt("y", "Other")]
    chosen, dropped = select(stream, 2, RECIPES["daily"], ex, fresh_cap=0)
    assert [p.track.id for p in chosen] == ["y"] and dropped["muted"] == 2


def test_a_small_profile_still_rotates_its_anchors_by_day():
    anchors = [Anchor(f"A{i}", "T", None, None, "seed", 1.0) for i in range(20)]
    firsts = {tuple(a.artist for a in sample_anchors(anchors, 30, random.Random(day))[:10]) for day in range(5)}
    assert len(firsts) == 5


def test_manual_run_refuses_an_overlapping_run_without_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    cfg = dataclasses.replace(load_config(), daily_size=3, exa_api_key=None)
    with api.open_db(cfg.db_path) as conn:
        import_seed_library(conn)
    started, release = Event(), Event()
    sink = Sink()

    def hold_run(message):
        started.set()
        assert release.wait(10), "test did not release the first run"

    def second_run():
        try:
            run_playlist("daily", cfg=cfg, client=Cosine(), sink=sink, log_run=True)
        except PlaylistError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_playlist, "daily", cfg=cfg, client=Cosine(), sink=sink,
                            log_run=True, notify=hold_run)
        try:
            assert started.wait(5), "first run did not start"
            second = pool.submit(second_run)
            try:
                refusal = second.result(timeout=2)
            except TimeoutError:
                refusal = "second run queued instead of refusing"
        finally:
            release.set()
        assert first.result(timeout=5)["export"]["verified"]
        second.result(timeout=5)

    assert refusal == "A daily run is already going; it will finish on its own."
    assert len(sink.calls) == 1
    with api.open_db(cfg.db_path) as conn:
        assert conn.execute("SELECT job, status FROM job_runs").fetchall() == [("daily", "ok")]
