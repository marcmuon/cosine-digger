"""api.quick end to end through real seed resolution, fusion, session recording
and the playlist-id store, with fake cosine.club and YouTube Music providers."""
import sqlite3

import pytest

from cosine_digger import api
from cosine_digger.models import SimilarTracksData, Track
from cosine_digger.storage import migrate


def t(tid, artist, title, video=None):
    return Track(id=tid, name=title, artist=artist, track=title, video_id=video or f"v{tid}")


SEED_A = t("a", "Seed Artist", "First")
SEED_B = t("b", "Other Seed", "Second")
NEIGHBORS = {
    "a": [t("1", "Near", "One", ), t("2", "Owned Artist", "Owned Track"), t("3", "Shared", "Both"), t("b", "Other Seed", "Second")],
    "b": [t("3", "Shared", "Both"), t("4", "Far", "Four")],
}


class Cosine:
    def search(self, query, limit=20):
        return {"Seed Artist - First": [SEED_A], "Other Seed - Second": [SEED_B]}.get(query, [])[:limit]

    def lookup(self, url):
        return []

    def similar(self, track_id, limit=50, **filters):
        return SimilarTracksData(source_track=Track(id=track_id, name="", artist="", track=""),
                                 similar_tracks=[x.model_copy(update={"score": 0.9 - i * 0.05})
                                                 for i, x in enumerate(NEIGHBORS.get(track_id, []))])

    def close(self):
        pass


class Sink:
    def __init__(self):
        self.calls = []

    def replace_playlist(self, name, video_ids, description="", **_):
        self.calls.append((name, list(video_ids)))
        from cosine_digger.youtube import ExportReport
        return ExportReport(playlist_id="PL1", playlist_url="https://music.youtube.com/playlist?list=PL1",
                            created=True, requested=list(video_ids), added=list(video_ids), missing=[],
                            skipped=[], failed=[], verified=True)


def db():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


class DeepCosine(Cosine):
    """Follow a real artist branch without needing any Discogs requests."""
    def search(self, query, limit=20):
        if query == "Near":
            return [t("branch", "Near", "Branch seed")]
        return super().search(query, limit)

    def similar(self, track_id, limit=50, **filters):
        if track_id == "branch":
            return SimilarTracksData(
                source_track=t("branch", "Near", "Branch seed"),
                similar_tracks=[t("deep", "Deep Artist", "Deep Pick").model_copy(update={"score": 0.9})],
            )
        return super().similar(track_id, limit, **filters)


@pytest.fixture
def offline_digs(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Fake-client digs must not make network requests")
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", unexpected_request)


@pytest.mark.parametrize("seed", [
    "artist.bandcamp.com/track/song",
    "https://artist.bandcamp.com/album/album",
    "https://[artist.bandcamp.com/track/song",
    "https://example.com/artist.bandcamp.com/track/song",
])
def test_quick_keeps_valid_seed_when_bandcamp_is_unresolved(offline_digs, seed):
    conn = db()
    try:
        out = api.quick([seed, "Seed Artist - First"], client=Cosine(), conn=conn,
                        export_playlist=False)
        assert [s["ok"] for s in out["seeds"]] == [False, True]
        assert out["picks"] and out["session_id"]
        if seed != "artist.bandcamp.com/track/song":
            assert any("Could not parse Bandcamp URL" in note for note in out["seeds"][0]["notes"])
    finally:
        conn.close()


@pytest.mark.parametrize("prefix", ["", "https://", "//"])
def test_quick_resolves_bandcamp_host_and_track_without_query(offline_digs, prefix):
    class Bandcamp(Cosine):
        def search(self, query, limit=20):
            return [SEED_A] if query == "artist first song" else []

    out = api.quick([f"{prefix}artist.bandcamp.com/track/first-song?from=feed#player"],
                    client=Bandcamp(), record=False, export_playlist=False)
    assert out["seeds"][0]["ok"] and out["picks"]


def test_quick_artist_fallback_finds_exact_artist_below_title_hits(offline_digs):
    class Ranked(Cosine):
        def search(self, query, limit=20):
            hits = [t(str(i), "Unrelated", "Seed Artist") for i in range(30)] + [SEED_A]
            return hits[:limit] if query == "Seed Artist" else []

    out = api.quick([], fallback_artists=["Seed Artist"], client=Ranked(),
                    record=False, export_playlist=False)
    assert out["seeds"][0]["tracks"] == [{"id": "a", "label": "Seed Artist - First", "video_id": "va"}]
    assert out["picks"]


@pytest.mark.parametrize("deep", [False])
@pytest.mark.parametrize("identity", ["id", "key"])
def test_quick_excludes_owned_before_limit(offline_digs, deep, identity):
    conn = db()
    api.import_library(conn, [{"id": "owned", "artist": "Owned Artist", "title": "Owned Track"}], source="test")
    if identity == "id":
        conn.execute("UPDATE tracks SET cosine_track_id = '2', artist = 'Different', title = 'Metadata'")
        conn.commit()
    out = api.quick(["Seed Artist - First"], client=DeepCosine(), conn=conn,
                    export_playlist=False, deep=deep, limit=2)
    assert "2" not in {p["id"] for p in out["picks"]}
    assert len(out["picks"]) == 2
    assert out["excluded"] == {"owned": 1}
    assert out["excluded_count"] == 1


@pytest.mark.parametrize("deep", [False])
@pytest.mark.parametrize("identity", ["id", "key"])
def test_quick_pass_then_redig_excludes_recording(offline_digs, deep, identity):
    from cosine_digger.leads import record_event

    conn = db()
    first = api.quick(["Seed Artist - First"], client=DeepCosine(), conn=conn,
                      export_playlist=False, deep=deep)
    assert "2" in {p["id"] for p in first["picks"]}
    result_id = conn.execute("SELECT id FROM session_results WHERE session_id = ? AND track_id = '2'",
                             (first["session_id"],)).fetchone()[0]
    record_event(conn, result_id, "pass")

    class ChangedMetadata(DeepCosine):
        def similar(self, track_id, limit=50, **filters):
            data = super().similar(track_id, limit, **filters)
            update = ({"artist": "Renamed Artist", "track": "Renamed Title", "video_id": "new-video"}
                      if identity == "id" else {"id": "new-id", "video_id": "new-video"})
            return data.model_copy(update={"similar_tracks": [
                track.model_copy(update=update) if track.id == "2" else track
                for track in data.similar_tracks
            ]})

    out = api.quick(["Seed Artist - First"], client=ChangedMetadata(), conn=conn,
                    export_playlist=False, deep=deep)
    assert not {"2", "new-id"} & {p["id"] for p in out["picks"]}
    assert out["picks"]  # unpassed discoveries remain eligible
    assert out["excluded"] == {"passed": 1}
    assert out["excluded_count"] == 1


@pytest.mark.parametrize("deep", [False, True])
def test_quick_uses_served_muted_and_known_rules(offline_digs, deep):
    from cosine_digger.leads import mute_lead
    from cosine_digger.storage import record_known, record_served

    conn = db()
    record_served(conn, "Earlier playlist", [{"track_id": "1"}])
    mute_lead(conn, "artist", "Owned Artist")
    record_known(conn, [{"track_id": "3"}], source="seed")
    out = api.quick(["Seed Artist - First"], client=DeepCosine(), conn=conn,
                    export_playlist=False, deep=deep, limit=1)
    assert not {"1", "2", "3"} & {p["id"] for p in out["picks"]}
    assert len(out["picks"]) == 1
    assert out["excluded"] == {"served": 1, "muted": 1, "known": 1}
    assert out["excluded_count"] == 3


def test_quick_deep_stores_each_picks_actual_hop(offline_digs):
    conn = db()
    out = api.quick(["Seed Artist - First"], client=DeepCosine(), conn=conn,
                    export_playlist=False, deep=True)
    expected = {p["id"]: p["hop"] for p in out["picks"]}
    assert set(expected.values()) == {0, 1}
    stored = dict(conn.execute("SELECT track_id, hop FROM session_results WHERE session_id = ?",
                               (out["session_id"],)))
    assert stored == expected


def test_quick_resolves_digs_explains_records_and_exports():
    conn, sink = db(), Sink()
    out = api.quick(["Seed Artist - First", "Other Seed - Second", "Nobody - Nothing"],
                    playlist="Digger Test", client=Cosine(), sink=sink, conn=conn)
    ids = [p["id"] for p in out["picks"]]
    assert ids[0] == "3"                      # surfaced by both seeds ranks first
    assert "a" not in ids and "b" not in ids  # seeds never come back as picks
    shared = out["picks"][0]
    assert shared["why"].startswith("Close to") and len(shared["matched_seeds"]) == 2
    assert [s["ok"] for s in out["seeds"]] == [True, True, False]
    assert out["playlist_url"].endswith("PL1") and out["export"]["verified"]
    assert sink.calls == [("Digger Test", [f"v{i}" for i in ids])]
    rows = conn.execute("SELECT COUNT(*) FROM session_results WHERE session_id = ?", (out["session_id"],)).fetchone()
    assert rows[0] == len(ids)
    # Every exported pick is in the served ledger, so playlists never repeat it.
    from cosine_digger.storage import served_keys
    assert set(ids) <= served_keys(conn)


def test_dig_drops_owned_and_served_recordings():
    seeds = api.resolve_seeds(["Seed Artist - First"], Cosine())
    picks = api.dig(seeds, Cosine(), exclude_ids={"v1"}, exclude_keys={api._key("Owned Artist", "owned track")})
    assert [p.track.id for p in picks] == ["3", "b"]  # "1" served (by video id), "2" owned


def test_missing_youtube_login_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(tmp_path / "absent.json"))
    out = api.quick(["Seed Artist - First"], client=Cosine(), conn=db())
    assert out["picks"] and out["playlist_url"] is None
    assert "youtube login" in out["export_error"]


def test_a_cosine_track_url_dig_teaches_the_profile():
    from cosine_digger.profile import build_profile

    class Named(Cosine):  # cosine.club names the source track in similar()
        def similar(self, track_id, limit=50, **filters):
            data = super().similar(track_id, limit, **filters)
            return data.model_copy(update={"source_track": {"a": SEED_A}.get(track_id, data.source_track)})
    conn = db()
    out = api.quick(["https://cosine.club/track/a"], client=Named(), conn=conn, export_playlist=False)
    assert out["seeds"][0]["tracks"][0]["label"] == "Seed Artist - First" and out["picks"]
    stored = conn.execute("SELECT seeds FROM sessions WHERE id = ?", (out["session_id"],)).fetchone()[0]
    assert "Seed Artist - First" in stored
    assert [(a.artist, a.title) for a in build_profile(conn).anchors if a.source == "seed"] == [("Seed Artist", "First")]


def test_imported_library_is_owned_and_shapes_the_profile():
    from cosine_digger.profile import build_profile
    conn = db()
    n = api.import_library(conn, [
        {"id": "1", "artist": "Owned Artist", "title": "Owned Track", "label": "Home Label",
         "added_at": "2026-09-01T00:00:00+00:00"},
        {"id": "2", "artist": "Owned Artist", "title": "Second"},
        {"id": "3", "artist": "", "title": "No artist"},
    ], source="mediaserver")
    assert n == 2
    assert api.import_library(conn, [{"id": "1", "artist": "Owned Artist", "title": "Owned Track"}],
                              source="mediaserver") == 1  # re-import updates, never duplicates
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    profile = build_profile(conn)
    assert profile.top("artist", 1)[0].name == "Owned Artist"
    assert {a.title for a in profile.anchors if a.source == "library"} == {"Owned Track", "Second"}
    out = api.dig(api.resolve_seeds(["Seed Artist - First"], Cosine()), Cosine(),
                  exclude_keys={api._key(a, t) for a, t in conn.execute("SELECT artist, title FROM tracks")})
    assert "2" not in [p.track.id for p in out]  # the owned neighbour never comes back


def test_failed_export_still_ledgers_its_tracks_and_says_nothing_secret():
    from cosine_digger.storage import served_keys

    class Leaky:
        def replace_playlist(self, *a, **k):
            raise RuntimeError("Cookie: SID=super-secret-cookie")
    conn = db()
    out = api.quick(["Seed Artist - First"], client=Cosine(), sink=Leaky(), conn=conn, playlist="P")
    assert "super-secret-cookie" not in out["export_error"]
    assert {p["id"] for p in out["picks"]} <= served_keys(conn)  # never re-served, even unconfirmed


def test_url_seeded_dig_teaches_the_profile():
    from cosine_digger.profile import build_profile

    class ByUrl(Cosine):
        def lookup(self, url):
            return [SEED_A] if "youtube" in url else []
    conn = db()
    api.quick(["https://www.youtube.com/watch?v=abc"], client=ByUrl(), conn=conn, export_playlist=False)
    profile = build_profile(conn)
    assert [e.name for e in profile.top("artist", 3)] == ["Seed Artist"]


def test_ledger_takes_back_an_export_that_changed_nothing_and_skips_tracks_served_meanwhile():
    import pytest

    from cosine_digger.storage import record_served, served_keys
    from cosine_digger.youtube import YouTubeExportError

    class Unreadable:
        def replace_playlist(self, *a, **k):
            raise YouTubeExportError("Could not read the existing playlist; nothing was changed.", changed=False)
    conn = db()
    picks = api.quick(["Seed Artist - First"], client=Cosine(), conn=conn, export_playlist=False)["picks"]
    as_picks = [api.Pick(Track(id=p["id"], name=p["title"], artist=p["artist"], track=p["title"],
                               video_id=p["video_id"]), 1, "why") for p in picks]
    with pytest.raises(YouTubeExportError):
        api.export(as_picks, "Digger Daily", sink=Unreadable(), conn=conn)
    assert served_keys(conn) == set()  # a failed read must not burn these tracks

    record_served(conn, "Manual", [{"track_id": as_picks[0].track.id}])  # another export got there first
    sink = Sink()
    report = api.export(as_picks, "Digger Daily", sink=sink, conn=conn, drop_served=True)
    assert as_picks[0].track.video_id not in sink.calls[0][1] and report.omitted == [as_picks[0].track.id]


def test_live_export_without_a_connection_keeps_its_mapping_and_ledger(monkeypatch, tmp_path):
    """No conn: the configured DB still holds the playlist id and served tracks."""
    from contextlib import closing
    from tests.test_youtube import FakeYTMusic

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    youtube = FakeYTMusic()
    monkeypatch.setattr("cosine_digger.youtube._authenticated_ytm", lambda: youtube)
    picks = [api.Pick(track=t("1", "Near", "One"), score=1.0, why="x")]
    first, second = api.export(picks, "P"), api.export(picks, "P")
    assert first.playlist_id == second.playlist_id and second.verified
    assert [c[0] for c in youtube.calls].count("create") == 1
    with closing(sqlite3.connect(tmp_path / "taste.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM served_ledger WHERE track_id = '1'").fetchone()[0] == 2


def test_quick_command_exits_nonzero_when_youtube_rejects_the_writes(monkeypatch, tmp_path):
    import json

    from typer.testing import CliRunner

    from cosine_digger.cli import app
    from tests.test_youtube import FakeYTMusic

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    youtube = FakeYTMusic()
    youtube.reject_add = {1}
    monkeypatch.setattr("cosine_digger.youtube._authenticated_ytm", lambda: youtube)
    monkeypatch.setattr(api, "make_client", lambda cfg=None: Cosine())
    result = CliRunner().invoke(app, ["quick", "Seed Artist - First", "--playlist", "Digger Test", "--json"])
    out = json.loads(result.stdout)
    assert result.exit_code == 1
    assert out["picks"] and out["export"]["verified"] is False and out["export"]["failed"]


def test_no_writer_commits_between_the_final_rule_check_and_the_claim(tmp_path):
    """Seen in review: a library import landed right after the final check and
    an owned track was exported with every plan check at zero."""
    from contextlib import closing

    from cosine_digger.storage import connect, served_keys

    path = tmp_path / "taste.db"
    conn = connect(path)
    migrate(conn)
    picks = [api.Pick(track=t("1", "Near", "One"), score=1.0, why="x")]
    outcome = {}

    def recheck(kept):
        with closing(sqlite3.connect(path, timeout=0)) as importer:
            try:
                importer.execute("INSERT INTO tracks (path, artist, title) VALUES ('/m/1.flac', 'Near', 'One')")
                importer.commit()
                outcome["import"] = "committed"
            except sqlite3.OperationalError as exc:
                outcome["import"] = str(exc)
        return kept

    api.export(picks, "Digger Daily", sink=Sink(), conn=conn, recheck=recheck)
    assert outcome["import"] == "database is locked"  # the import waits until after the claim
    assert served_keys(conn) >= {"1"} and not conn.in_transaction
    conn.close()


def test_a_recording_youtube_put_in_is_never_served_again():
    from cosine_digger.storage import served_keys
    from cosine_digger.youtube import ExportReport

    class Swapping(Sink):
        def replace_playlist(self, name, video_ids, description="", **_):
            return ExportReport("PL1", "https://music.youtube.com/playlist?list=PL1", True, list(video_ids),
                                list(video_ids), [], [], [], True,
                                inserted=[{"video_id": "tris", "artist": "Lanark Artefax", "title": "Tris"}])
    conn = db()
    api.export([api.Pick(track=t("1", "Near", "One"), score=1.0, why="x")], "Digger Daily", sink=Swapping(), conn=conn)
    assert {"1", "yt:tris", "tris"} <= served_keys(conn)
    assert conn.execute("SELECT artist, title FROM served_ledger WHERE video_id = 'tris'").fetchone() == ("Lanark Artefax", "Tris")


def test_expired_youtube_login_is_named_without_provider_text():
    from ytmusicapi.exceptions import YTMusicServerError

    class SignedOut:
        def replace_playlist(self, *a, **k):
            raise YTMusicServerError("Server returned HTTP 401: Unauthorized.\n"
                                     "You must be signed in. Cookie: SID=super-secret-cookie")
    out = api.quick(["Seed Artist - First"], client=Cosine(), sink=SignedOut(), conn=db(), playlist="P")
    assert "signed out" in out["export_error"] and "cosine-digger youtube login" in out["export_error"]
    assert "super-secret-cookie" not in out["export_error"]
