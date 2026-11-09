"""S3 foundation integration: real providers' parsers, HTTP and saved snapshots.

The small test host composes the S1/S3 entry points; production move routes and
batch admission arrive in S4/S6/S10. No provider request leaves MockTransport.
"""

from contextlib import closing
from threading import Event, Lock
from time import sleep
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cosine_digger import api, chain, client as cosine_http, discogs, seeds, storage
from cosine_digger.fusion import COSINE_WORKERS, fuse_move_tracks, fuse_similar_tracks, move_why
from cosine_digger.models import Track
from cosine_digger.playlists import load_exclusions
from cosine_digger.profile import build_feed_profile
from cosine_digger.slice_types import Point
from tests.test_slice_api import slice_app

FIND_RELEASE = seeds.discogs_find_release


def track(tid, artist=None, title=None, score=None, *, link=None, video=None):
    title = title or f"Recording {tid}"
    return Track(id=tid, name=title, artist=artist or f"Artist {tid}", track=title,
                 score=score, external_link=link, video_id=video)


def point(item, **fields):
    return Point(point_id=f"track:{api.track_key(item)}", kind="track", name=api.track_label(item),
                 artist=item.artist, title=item.track, track_key=api.track_key(item),
                 cosine_id=item.id, video_id=item.video_id, **fields)


class Providers:
    def __init__(self):
        self.searches = {}
        self.lookups = {}
        self.sources = {}
        self.neighbors = {}
        self.discogs = {}
        self.failures = {}
        self.calls = []

    def cosine(self, request):
        path = request.url.path.removeprefix("/api/v1")
        params = dict(request.url.params)
        self.calls.append(("cosine", path, params))
        if path == "/search" and len(params["q"]) > 200:
            return httpx.Response(400, json={"code": "QUERY_TOO_LONG"})
        key = params.get("q") or params.get("url") or path
        failure = self.failures.get(key)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return httpx.Response(failure, json={"code": "TRACK_NOT_FOUND" if failure == 404 else "UNAVAILABLE"})
        if path == "/search":
            data = self.searches.get(params["q"], [])[:int(params["limit"])]
        elif path == "/tracks/lookup":
            data = self.lookups.get(params["url"], [])
        else:
            tid = path.split("/")[2]
            if tid not in self.sources:
                return httpx.Response(404, json={"code": "TRACK_NOT_FOUND"})
            data = {"source_track": self.sources[tid].model_dump(),
                    "similar_tracks": [item.model_dump() for item in self.neighbors.get(tid, [])
                                       [:int(params["limit"])]]}
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"data": [item.model_dump() for item in data]})

    def discogs_response(self, request):
        path = request.url.path
        self.calls.append(("discogs", path, dict(request.url.params)))
        if path in self.failures:
            return httpx.Response(self.failures[path], json={})
        payload = self.discogs.get(path, {"results": []} if path == "/database/search" else None)
        if callable(payload):
            payload = payload(request)
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(404, json={})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr(cosine_http, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(cosine_http.time, "sleep", lambda _: None)
    monkeypatch.setattr(seeds, "discogs_find_release", FIND_RELEASE)
    monkeypatch.setattr(seeds, "_youtube_title", lambda _: None)

    def no_network(*args, **kwargs):
        raise AssertionError("S3 tests must not make real network calls")

    monkeypatch.setattr(httpx, "get", no_network)
    providers = Providers()
    cosine = cosine_http.CosineClient(token="fixture-key")
    cosine._client.close()
    cosine._client = httpx.Client(base_url=cosine_http.BASE_URL, transport=httpx.MockTransport(providers.cosine))
    dg = httpx.Client(transport=httpx.MockTransport(providers.discogs_response))
    db_path = tmp_path / "taste.db"
    with closing(storage.connect(db_path)) as conn:
        storage.migrate(conn)
    app = FastAPI()

    @app.post("/resolve")
    def resolve(body: dict):
        return seeds.resolve_move_origins([Point.model_validate(item) for item in body["origins"]],
                    body.get("move", "seed"), client=cosine,
                    discogs_client=dg if body.get("discogs", True) else None,
                    seed_budget=body.get("seed_budget", 10),
                    cosine_budget=body.get("cosine_budget", 40)).to_dict()

    @app.post("/root")
    def root(body: dict):
        origins = [Point.model_validate(item) for item in body["origins"]]
        resolution = seeds.resolve_move_origins(origins, "seed", client=cosine, discogs_client=dg)
        candidates = fuse_move_tracks([item.id for item in resolution.resolved_tracks], cosine,
                                     seed_to_origin=resolution.seed_to_origin,
                                     origin_order=[item.point_id for item in resolution.origins],
                                     limit_per_seed=body.get("limit_per_seed", 100))
        with closing(storage.connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            dig_id = storage.create_dig(conn, origins)
            identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=origins,
                                           move="seed", further=False)
            storage.resolve_step_origins(conn, identity.step_id, resolution.origins)

            def emit(event, data):
                storage.append_slice_event(conn, identity.job_id, event, data, redact=lambda value: value)

            for note in resolution.notes:
                emit("note", note)
            emit("batch", {"hop": 0, "start_rank": 1, "target_count": 25, "label": "near"})
            exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
            names = {origin.point_id: origin.name for origin in resolution.origins}
            rank = 0
            for candidate in candidates:
                if exclusions.reason(candidate.ranked.track) is not None:
                    continue
                rank += 1
                item = candidate.ranked.track
                candidate.route["why"] = move_why(candidate.route, names)
                emit("pick", {"pick": {"rank": rank, "track_key": api.track_key(item), "cosine_id": item.id,
                     "artist": item.artist, "title": item.track, "video_id": item.video_id,
                     "score": candidate.ranked.final_score, "batch": 0, "route": candidate.route,
                     "facts": None, "facts_state": "missing"}})
            emit("picks_done", {"pick_count": rank})
            emit("done", {"status": "done", "pick_count": rank})
        with closing(storage.connect(db_path)) as conn:
            return storage.get_step_snapshot(conn, identity.step_id)

    with TestClient(app) as http:
        yield SimpleNamespace(http=http, providers=providers, cosine=cosine, discogs=dg, db_path=db_path)
    cosine.close()
    dg.close()


def resolve(harness, origins, move="seed", **options):
    response = harness.http.post("/resolve", json={"origins": [item.to_dict() for item in origins],
                                                  "move": move, **options})
    assert response.status_code == 200
    return response.json()


def root(harness, origins, **options):
    response = harness.http.post("/root", json={"origins": [item.to_dict() for item in origins], **options})
    assert response.status_code == 200
    return response.json()["snapshot"]


def test_explicit_text_hit_wins_before_album_and_album_text_uses_verified_release(harness):
    p = harness.providers
    hit = track("exact", "Artist", "Opening", link="https://www.discogs.com/release/10")
    p.searches["Artist - Opening"] = [track("wrong", "Artist", "Opening (Remix)"), hit]
    requested = Point(point_id="typed:opening", kind="track", name="Artist - Opening")
    assert [item["id"] for item in resolve(harness, [requested])["resolved_tracks"]] == ["exact"]
    assert not any(provider == "discogs" for provider, _, _ in p.calls)
    other = track("other", "Artist", "Elsewhere", link="https://www.discogs.com/release/11")
    p.searches["Artist - Album"] = [other]
    p.searches["Artist Album"] = [hit, other]
    p.discogs["/releases/10"] = {"id": 10, "title": "Album", "artists": [{"name": "Artist (2)"}]}
    p.discogs["/releases/11"] = {"id": 11, "title": "Other", "artists": [{"name": "Artist"}]}
    album = Point(point_id="typed:album", kind="album", name="Artist - Album", artist="Artist", title="Album")
    assert [item["id"] for item in resolve(harness, [album])["resolved_tracks"]] == ["exact"]
    typed_album = Point(point_id="typed:ambiguous", kind="track", name="Artist - Album")
    assert [item["id"] for item in resolve(harness, [typed_album])["resolved_tracks"]] == ["exact"]


def test_discogs_release_tracklist_fallback_is_exact_and_keeps_remixes_separate(harness):
    p = harness.providers
    p.discogs["/masters/10"] = {"id": 10, "artists": [{"name": "Artist (2)"}],
        "tracklist": [{"title": "Opening"}, {"title": "Ending"}]}
    p.searches["Artist Opening"] = [track("wrong", "Artist", "Opening (Remix)"),
                                   track("right", "Artist", "Opening (Original Mix)")]
    p.searches["Artist Ending"] = [track("other", "Artist Extended", "Ending")]
    origin = Point(point_id="link:master", kind="link", name="Album", url="https://www.discogs.com/master/10")
    assert [item["id"] for item in resolve(harness, [origin])["resolved_tracks"]] == ["right"]


def test_unindexed_file_names_only_exact_artist_substitutes_and_leaves_library_untouched(harness):
    p = harness.providers
    original = track("absent", "Ｒ‐Ｚｏｎｅ", "Missing File")
    substitutes = [track("s1", "R-Zone", "One"), track("s2", "R-Zone", "Two")]
    p.searches["Ｒ‐Ｚｏｎｅ"] = [track("wrong", "R-Zone Expanded", "No"), *substitutes,
                                      track("s3", "R-Zone", "Three")]
    p.sources.update({item.id: item for item in substitutes})
    p.neighbors["s1"] = [track("alias", original.artist, original.track, 0.95), track("new", score=0.8)]
    with closing(storage.connect(harness.db_path)) as conn, conn:
        conn.execute("INSERT INTO tracks(id,path,artist,title,cosine_verified) VALUES (1,?,?,?,0)",
                     ("fixture-file", original.artist, original.track))
        before = tuple(conn.execute("SELECT * FROM tracks").fetchone())
    requested = point(original, library_id=1, indexed=False).model_copy(update={"cosine_id": None})
    snapshot = root(harness, [requested])
    origin = snapshot["step"]["origins"][0]
    assert origin["cosine_id"] is None and origin["indexed"] is False
    assert origin["resolution"] == "substituted"
    assert [item["cosine_id"] for item in origin["resolved_tracks"]] == ["s1", "s2"]
    note = next(note for note in snapshot["step"]["note"] if note["code"] == "S-N-SWAP-FILE")
    assert note["message"] == "Your file 'Ｒ‐Ｚｏｎｅ - Missing File' isn't on cosine.club. Using 2 indexed R-Zone tracks instead: One, Two."
    assert [note["code"] for note in snapshot["step"]["note"]] == ["S-N-SWAP-FILE"]
    assert [pick["cosine_id"] for pick in snapshot["picks"]] == ["new"]
    with closing(storage.connect(harness.db_path)) as conn:
        assert tuple(conn.execute("SELECT * FROM tracks").fetchone()) == before
        for table in ("sessions", "session_results", "result_events", "session_branches", "known_tracks"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("source", ["library", "track"])
def test_explicit_missing_track_does_not_become_a_same_named_album(harness, source):
    p = harness.providers
    original = track("absent", "Artist", "Missing")
    album_track = track("album-track", "Artist", "Other Song", link="https://www.discogs.com/release/10")
    substitutes = [track("s1", "Artist", "One"), track("s2", "Artist", "Two")]
    p.searches["Artist - Missing"] = [album_track]
    p.searches["Artist Missing"] = [album_track]
    p.searches["Artist"] = substitutes
    p.discogs["/releases/10"] = {"id": 10, "title": "Missing", "artists": [{"name": "Artist"}],
        "tracklist": [{"title": "Other Song"}]}
    requested = point(original).model_copy(update={"cosine_id": None})
    if source == "library":
        with closing(storage.connect(harness.db_path)) as conn, conn:
            conn.execute("INSERT INTO tracks(id,path,artist,title,cosine_verified) VALUES (1,'fixture-file','Artist','Missing',0)")
            before = tuple(conn.execute("SELECT * FROM tracks").fetchone())
        requested.library_id, requested.indexed = 1, False
    elif source == "cosine":
        requested.cosine_id = original.id
    elif source == "youtube":
        requested.kind, requested.url = "link", "https://www.youtube.com/watch?v=fixture-video"
    result = resolve(harness, [requested])
    assert result["origins"][0]["resolution"] == "substituted"
    assert [item["id"] for item in result["resolved_tracks"]] == [item.id for item in substitutes]
    code = "S-N-SWAP-FILE" if source == "library" else "S-N-SWAP-TEXT"
    note = next(note for note in result["notes"] if note["code"] == code)
    assert note["params"]["seed"] == original.artist + " - " + original.track
    assert note["params"]["titles"] == "One, Two"
    assert not any(provider == "discogs" for provider, _, _ in p.calls)
    if source == "library":
        with closing(storage.connect(harness.db_path)) as conn:
            assert tuple(conn.execute("SELECT * FROM tracks").fetchone()) == before


@pytest.mark.parametrize("stage,failure", [
    ("exact", 429),
    ("fallback", 503),
    ("id", httpx.ConnectError("fixture unreachable")),
    ("link", httpx.ReadTimeout("fixture timeout")),
])
def test_unavailable_is_failure_never_an_unindexed_seed_or_artist_swap(harness, failure, stage):
    p = harness.providers
    requested = Point(point_id="typed:missing", kind="track", name="Artist - Missing", artist="Artist", title="Missing")
    p.searches["Artist"] = [track("stand-in", "Artist", "Other")]
    key = "Artist - Missing" if stage == "exact" else "Artist"
    if stage == "id":
        requested = requested.model_copy(update={"cosine_id": "missing"})
        key = "/tracks/missing/similar"
    elif stage == "link":
        requested = requested.model_copy(update={"kind": "link", "url": "https://www.youtube.com/watch?v=missing"})
        key = requested.url
    p.failures[key] = failure
    error = cosine_http.CosineHTTPError if isinstance(failure, int) else cosine_http.CosineRequestError
    with pytest.raises(error):
        harness.http.post("/resolve", json={"origins": [requested.to_dict()], "move": "seed"})
    if stage in {"exact", "id", "link"}:
        assert not any(params.get("q") == "Artist" for _, _, params in p.calls)
    with closing(storage.connect(harness.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 0


def test_confirmed_missing_id_may_swap_and_no_stand_in_is_honestly_empty(harness):
    requested = Point(point_id="track:missing", kind="track", name="Artist - Missing", artist="Artist",
                      title="Missing", cosine_id="not-in-index", indexed=None)
    harness.providers.searches["Artist"] = [track("stand-in", "Artist", "Other")]
    result = resolve(harness, [requested], "track")
    assert result["origins"][0]["indexed"] is None
    assert result["origins"][0]["resolution"] == "substituted"
    assert next(note for note in result["notes"] if note["code"] == "S-N-SWAP-TEXT")["params"]["titles"] == "Other"
    harness.providers.searches.clear()
    empty = root(harness, [requested])
    assert empty["step"]["status"] == "done" and empty["picks"] == []
    assert [note["code"] for note in empty["step"]["note"]] == ["S-N-NONE"]


def test_partial_origins_store_all_nullable_scores_and_stable_argmax_sector(harness):
    p = harness.providers
    a, b = track("a"), track("b")
    origins = [point(a), point(b), Point(point_id="typed:missing", kind="track", name="Absent - Missing")]
    p.sources.update({item.id: item for item in [a, b]})
    p.neighbors["a"] = [track("near", score=0.81), track("unmeasured", score=None)]
    p.neighbors["b"] = [track("near", score=0.81)]
    snapshot = root(harness, origins)
    near, unmeasured = snapshot["picks"]
    assert near["route"]["seed_scores"] == {origins[0].point_id: 0.81, origins[1].point_id: 0.81, origins[2].point_id: None}
    assert near["route"]["nearest_seed"] == near["route"]["layout_seed"] == origins[0].point_id
    assert near["route"]["seed_count"] == 3 and near["route"]["close_count"] == 2
    assert near["route"]["why"] == "close to 2 of your 3 seeds"
    assert 0 < near["layout"]["angle"] < 2 * 3.141593 / 3
    assert unmeasured["route"]["seed_scores"] == dict.fromkeys(item.point_id for item in origins)
    assert unmeasured["route"]["nearest_seed"] is None
    assert unmeasured["route"]["why"] == f"suggested from {origins[0].name}; similarity unavailable"
    assert snapshot["step"]["note"][0]["code"] == "S-N-PARTIAL"
    assert snapshot["step"]["note"][0]["params"] == {"k": 2, "n": 3}
    assert [note["code"] for note in snapshot["step"]["note"]] == ["S-N-PARTIAL", "S-N-DROPPED"]


def test_grouped_maximum_survives_merge_and_nulls_do_not_lose_rrf_votes(harness):
    p = harness.providers
    sources = [track("a1"), track("a2"), track("b")]
    p.sources.update({item.id: item for item in sources})
    p.neighbors["a1"] = [track("x", score=0.71), track("only-null", score=None)]
    p.neighbors["a2"] = [track("x", score=0.94)]
    p.neighbors["b"] = [track("x", score=0.70), track("below", score=0.699999)]
    candidates = fuse_move_tracks(["a1", "a2", "a1", "b"], harness.cosine,
                                 seed_to_origin={"a1": "album", "a2": "album", "b": "track"},
                                 origin_order=["missing", "album", "track"], limit_per_seed=2)
    x = candidates[0]
    assert x.ranked.track.id == "x" and x.ranked.match_count == 2
    assert x.route["seed_scores"] == {"missing": None, "album": 0.94, "track": 0.70}
    assert x.route["raw_score"] == pytest.approx(2 / 60)
    assert x.ranked.final_score == 1.0
    by_id = {candidate.ranked.track.id: candidate for candidate in candidates}
    assert by_id["only-null"].route["layout_seed"] == "album"
    assert by_id["only-null"].route["nearest_seed"] is None
    assert by_id["below"].route["close_count"] == 0
    assert move_why(by_id["below"].route, {"track": "Track Name"}) == "nearest to Track Name"
    assert [params["limit"] for _, path, params in p.calls if path.endswith("/similar")] == ["2", "2", "2"]


@pytest.mark.parametrize("scores,expected", [([0.70, 0.70], "close to all 2 seeds"),
    ([0.70, 0.699999], "close to First"), ([0.699999, None], "nearest to First"),
    ([None, None], "suggested from First; similarity unavailable"), ([0.0, 0.0], "nearest to First")])
def test_threshold_boundary_zero_nulls_and_ties(harness, scores, expected):
    p = harness.providers
    p.sources.update({tid: track(tid) for tid in ("a", "b")})
    p.neighbors.update({tid: [track("x", score=score)] for tid, score in zip(("a", "b"), scores)})
    candidate = fuse_move_tracks(["a", "b"], harness.cosine, seed_to_origin={"a": "one", "b": "two"},
                                 origin_order=["one", "two"])[0]
    assert move_why(candidate.route, {"one": "First", "two": "Second"}) == expected
    assert candidate.route["nearest_seed"] == (None if scores == [None, None] else "one")
    assert candidate.route["close_seed_ids"] == [origin for origin, score in zip(("one", "two"), scores)
                                               if score is not None and score >= 0.70]


def test_rrf_ties_are_canonical_and_artist_admission_waits_for_exclusions(harness):
    p = harness.providers
    p.sources.update({tid: track(tid, video=f"video-{tid}") for tid in ("a", "b")})
    p.neighbors["a"] = [track("z", "Zed", "First", 0.99), track("alternative", "Zed", "Second", 0.8)]
    p.neighbors["b"] = [track("a", "Alpha", "First", 0.1),
                         track("recording-alias", "ARTIST A", "Recording a (Original Mix)", 0.99),
                         track("upload-alias", score=0.99, video="video-a"),
                         track("candidate", "Alpha", "Other", 0.1)]
    # All three source identities are excluded before their ranks can vote.
    candidates = fuse_move_tracks(["a", "b"], harness.cosine,
                                 seed_to_origin={"a": "one", "b": "two"}, origin_order=["one", "two"])
    assert [candidate.ranked.track.id for candidate in candidates] == ["candidate", "z", "alternative"]
    assert candidates[0].ranked.final_score == candidates[1].ranked.final_score


@pytest.mark.parametrize("membership", ["lookup", "tracklist"])
def test_label_recording_must_belong_to_newest_release(harness, membership):
    p = harness.providers
    url = "https://www.discogs.com/release/10"
    old = track("old", "Artist", "Old Recording", link="https://www.discogs.com/release/99")
    newest = track("new", "Artist", "New Recording", link=url)
    p.discogs["/labels/42/releases"] = {"releases": [
        {"id": 10, "artist": "Artist", "title": "New Album", "year": 2026}]}
    p.searches["Artist New Album"] = [old]
    if membership in {"lookup", "tracklist", "wrong_lookup"}:
        p.discogs["/releases/10"] = {"id": 10, "artists": [{"name": "Artist"}],
            "tracklist": [{"title": "New Recording"}]}
    if membership in {"lookup", "missing_tracklist"}:
        p.lookups[url] = [newest]
    elif membership == "wrong_lookup":
        p.lookups[url] = [old]
    elif membership == "tracklist":
        p.searches["Artist New Recording"] = [newest]
    label = Point(point_id="label:42", kind="label", name="New Label", discogs_id="42")
    result = resolve(harness, [label], "label")
    assert [item["id"] for item in result["resolved_tracks"]] == (
        ["new"] if membership in {"lookup", "tracklist"} else [])
    assert any(path == "/tracks/lookup" or path == "/releases/10" for _, path, _ in p.calls)
    if membership == "tracklist":
        assert any(path == "/releases/10" for _, path, _ in p.calls)


def test_label_seeds_are_newest_discogs_recordings_not_owned_label_tracks(harness):
    p = harness.providers
    releases = [{"id": i, "artist": f"Release Artist {i}", "title": f"Album {i}", "year": year}
                for i, year in [(1, 2025), (2, 2026), (3, 2026), (4, 2024), (5, 2023), (6, 2022)]]
    p.discogs["/labels/42/releases"] = {"releases": releases, "pagination": {"pages": 1}}
    for release in releases:
        source = track(f"s{release['id']}", release["artist"])
        p.searches[f"{release['artist']} {release['title']}"] = [source]
        p.discogs[f"/releases/{release['id']}"] = {"artists": [{"name": source.artist}],
            "tracklist": [{"title": source.track}]}
    p.searches["Release Artist 4 Album 4"] = [track("wrong", "Release Artist 40")]
    with closing(storage.connect(harness.db_path)) as conn, conn:
        conn.execute("INSERT INTO tracks(path,artist,title,label,cosine_track_id) VALUES ('owned','Owned','Home','Sol Records','owned')")
        before = tuple(conn.execute("SELECT * FROM tracks").fetchone())
    label = Point(point_id="label:42", kind="label", name="Sol Records", discogs_id="42")
    result = resolve(harness, [label], "label")
    assert [item["id"] for item in result["resolved_tracks"]] == ["s2", "s3", "s1", "s5", "s6"]
    assert "owned" not in result["seed_to_origin"]
    assert not any(note["code"] == "S-N-LABEL-FEW" for note in result["notes"])
    assert next(params for provider, path, params in p.calls if path == "/labels/42/releases") == {
        "sort": "year", "sort_order": "desc", "page": "1", "per_page": "20"}
    assert any(params.get("q") == "Release Artist 6 Album 6" for _, _, params in p.calls)
    assert [(path, params) for provider, path, params in p.calls if provider == "discogs"] == [
        ("/labels/42/releases", {"sort": "year", "sort_order": "desc", "page": "1", "per_page": "20"}),
        *[(f"/releases/{i}", {}) for i in (2, 3, 1, 4, 5, 6)]]
    linked = resolve(harness, [Point(point_id="link:label", kind="link", name="Sol Records",
                                     url="https://www.discogs.com/label/42-Sol-Records")])
    assert [item["id"] for item in linked["resolved_tracks"]] == ["s2", "s3", "s1", "s5", "s6"]
    with closing(storage.connect(harness.db_path)) as conn:
        assert tuple(conn.execute("SELECT * FROM tracks").fetchone()) == before


def label_catalog(harness, entries, indexed=()):
    """Give the real clients a catalogue and independently verifiable recordings."""
    p = harness.providers
    p.discogs["/labels/42/releases"] = {"releases": entries, "pagination": {"pages": 1}}
    for entry in entries:
        rid = entry["id"]
        source = track(f"source-{rid}", entry["artist"], f"Recording {rid}")
        p.discogs[f"/releases/{rid}"] = {"id": rid, "artists": [{"name": source.artist}],
            "tracklist": [{"title": source.track}, {"title": f"Second Recording {rid}"}]}
        if rid in indexed:
            p.searches[f"{entry['artist']} {entry['title']}"] = [
                source, track(f"second-{rid}", source.artist, f"Second Recording {rid}")]
            p.sources[source.id] = source
            p.neighbors[source.id] = [track(f"pick-{rid}", score=0.9)]
    return Point(point_id="label:42", kind="label", name="Walkback Label", discogs_id="42")


def catalog_entries(count):
    return [{"id": i, "artist": f"Release Artist {i}", "title": f"Album {i}", "year": 2026 - i}
            for i in range(1, count + 1)]


@pytest.mark.parametrize("remaining", [4])
@pytest.mark.parametrize("workers", [1])
def test_discography_budget_preserves_prepared_and_resolved_origins(harness, monkeypatch, remaining, workers):
    origin = label_catalog(harness, catalog_entries(8), indexed=range(1, 9))
    tracker = chain.BudgetTracker(40)
    for _ in range(40 - remaining):
        tracker.spend()
    monkeypatch.setattr(seeds, "COSINE_WORKERS", workers)
    monkeypatch.setattr(discogs, "pace", lambda *args: None)
    monkeypatch.setattr(harness.discogs, "_transport", chain._RetryOn429Transport(
        wrapped=httpx.MockTransport(harness.providers.discogs_response), budget_tracker=tracker))

    snapshot = root(harness, [origin])
    expected = [f"source-{i}" for i in range(1, remaining)]
    assert snapshot["step"]["status"] == "done"
    assert [item["cosine_id"] for item in snapshot["step"]["origins"][0]["resolved_tracks"]] == expected
    assert [pick["cosine_id"] for pick in snapshot["picks"]] == [tid.replace("source", "pick") for tid in expected]
    few = [note for note in snapshot["step"]["note"] if note["code"] == "S-N-LABEL-FEW"]
    assert [note["params"]["m"] for note in few] == ([remaining - 1] if remaining < 6 else [])
    assert [path for provider, path, _ in harness.providers.calls if provider == "discogs"] == [
        "/labels/42/releases", *[f"/releases/{i}" for i in range(1, remaining)]]
    assert tracker.remaining == 0


def resolution_calls(providers):
    return [(path, params) for provider, path, params in providers.calls
            if provider == "cosine" and path in {"/tracks/lookup", "/search"}]


def counted_cosine(harness):
    window, lock = SimpleNamespace(calls=0), Lock()

    def transport(request):
        with lock:
            window.calls += 1
            remaining = 120 - window.calls
        response = harness.providers.cosine(request)
        response.headers.update({"x-ratelimit-limit": "120", "x-ratelimit-remaining": str(remaining),
                                 "x-ratelimit-reset": "2000000000"})
        return response

    harness.cosine._client.close()
    harness.cosine._client = httpx.Client(base_url=cosine_http.BASE_URL,
                                         transport=httpx.MockTransport(transport))
    return window


def long_release_seed(harness, source, indexed):
    p = harness.providers
    recordings = [track(f"entry-{i}", "Album Artist", f"Entry {i}") for i in range(1, 151)]
    resource = "master" if source == "master" else "release"
    url = f"https://www.discogs.com/{resource}/150"
    p.discogs[f"/{resource}s/150"] = {"id": 150, "title": "Long Album",
        "artists": [{"name": "Album Artist"}],
        "tracklist": [{"title": item.track} for item in recordings]}
    p.discogs["/database/search"] = {"results": [
        {"title": "Album Artist - Long Album", "uri": "/release/150"}]}
    for item in recordings:
        if int(item.id.split("-")[1]) in indexed:
            p.searches[f"{item.artist} {item.track}"] = [item]
            p.sources[item.id] = item
    if source in {"release", "master", "album_url"}:
        origin = Point(point_id="album:150", kind="album" if source == "album_url" else "link",
                       name="Album Artist - Long Album", url=url)
    elif source == "album":
        origin = Point(point_id="album:150", kind="album", name="Album Artist - Long Album",
                       artist="Album Artist", title="Long Album")
    else:
        origin = Point(point_id="album:150", kind="track", name="Album Artist - Long Album")
    return origin


@pytest.mark.parametrize("source", ["release"])
def test_sparse_release_does_not_starve_later_resolvable_chip(harness, source):
    origin = long_release_seed(harness, source, indexed=())
    other = track("explicit", "Other Artist", "Indexed Song")
    p = harness.providers
    p.searches["Other Artist - Indexed Song"] = [other]
    p.sources[other.id] = other
    origins = [origin, point(other).model_copy(update={"cosine_id": None})]
    result = seeds.resolve_move_origins(origins, "seed", client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert [item.id for item in result.resolved_tracks] == [other.id]
    queries = [params["q"] for path, params in resolution_calls(p) if path == "/search"]
    assert queries.index("Album Artist Entry 1") < queries.index("Other Artist - Indexed Song")
    assert queries.index("Other Artist - Indexed Song") < queries.index("Album Artist Entry 2")
    assert len(resolution_calls(p)) == 60
    assert [len(item["resolved_tracks"]) for item in root(harness, origins)["step"]["origins"]] == [0, 1]


def test_cancellation_during_further_origin_stops_requests(harness):
    p = harness.providers
    source = track("seed")
    near = track("near", score=0.9, link="https://www.discogs.com/release/10")
    p.sources[source.id] = source
    p.neighbors[source.id] = [near]
    p.discogs["/releases/10"] = {"id": 10, "labels": [{"id": 42, "name": "Label"}]}
    cancelled = Event()
    at_cancel = []
    releases = []
    for rid in range(100, 120):
        releases.append({"id": rid, "artist": f"Artist {rid}", "title": f"Album {rid}", "year": 2026})
        p.discogs[f"/releases/{rid}"] = {"id": rid, "artists": [{"name": f"Artist {rid}"}],
            "tracklist": [{"title": f"Song {rid}"}]}

    def listing(request):
        cancelled.set()
        at_cancel.append(len(p.calls))
        return {"releases": releases}

    p.discogs["/labels/42/releases"] = listing
    events = list(api.run_move([point(source)], "seed", True, client=harness.cosine,
                              discogs_client=harness.discogs, exclusions=load_empty_exclusions(harness),
                              cancelled=cancelled.is_set))
    assert [event.data["cosine_id"] for event in events if event.event == "pick"] == [near.id]
    assert at_cancel
    assert p.calls[at_cancel[0]:] == []
    assert not any(event.event == "batch" and event.data["hop"] == 1 for event in events)


@pytest.mark.parametrize("source", ["release"])
@pytest.mark.parametrize("indexed", [()])
def test_release_seed_budget_keeps_verified_recordings_and_totals(harness, source, indexed):
    origin = long_release_seed(harness, source, indexed)
    window = counted_cosine(harness)
    result = seeds.resolve_move_origins([origin], "seed", client=harness.cosine,
                                        discogs_client=harness.discogs)
    searched = 57 if source in {"album", "text"} else 59
    expected = [f"entry-{i}" for i in indexed if i <= searched]
    print(f"O11_B source={source} indexed={indexed} budget_calls={window.calls} verified={len(expected)}")
    assert window.calls == len(resolution_calls(harness.providers)) == 60
    assert [item.id for item in result.resolved_tracks] == expected
    assert result.origins[0].resolution == ("exact" if expected else "missing")
    album_notes = [note for note in result.notes if note["code"] == "S-N-ALBUM"]
    if expected or source == "release":
        assert album_notes[0]["params"] == {"Album": origin.name, "t": 150, "u": len(expected)}
    else:
        assert album_notes == []  # Missing album text retains the existing missing-seed copy.
    assert any(note["code"] == "S-N-NONE" for note in result.notes) == (not expected)


def test_unindexed_label_stops_at_forty_cosine_calls_with_empty_notes(harness):
    origin = label_catalog(harness, catalog_entries(20))
    result = seeds.resolve_move_origins([origin], "label", client=harness.cosine,
                                        discogs_client=harness.discogs, cosine_budget=40)
    calls = resolution_calls(harness.providers)
    print(f"O10_A cosine_calls={len(calls)}")
    assert len(calls) <= 40
    assert result.resolved_tracks == []
    assert [note["code"] for note in result.notes] == ["S-N-LABEL-FEW", "S-E-EMPTY"]
    assert result.notes[0]["params"] == {"m": 0, "Label": origin.name}


def test_two_label_moves_share_a_120_call_window_without_rate_limit(harness):
    origin = label_catalog(harness, catalog_entries(20))
    lock = Lock()
    window = SimpleNamespace(calls=0, rate_limited=0)

    def transport(request):
        with lock:
            window.calls += 1
            remaining = 120 - window.calls
            if remaining < 0:
                window.rate_limited += 1
        if remaining < 0:
            return httpx.Response(429, json={"code": "RATE_LIMITED"})
        response = harness.providers.cosine(request)
        response.headers.update({"x-ratelimit-limit": "120", "x-ratelimit-remaining": str(remaining)})
        return response

    harness.cosine._client.close()
    harness.cosine._client = httpx.Client(base_url=cosine_http.BASE_URL,
                                         transport=httpx.MockTransport(transport))
    counts = []
    for _ in range(2):
        before = window.calls
        result = seeds.resolve_move_origins([origin], "label", client=harness.cosine,
                                            discogs_client=harness.discogs, cosine_budget=40)
        counts.append(window.calls - before)
        assert result.resolved_tracks == []
    print(f"O10_C cosine_calls={counts} rate_limited={window.rate_limited}")
    assert all(count <= 40 for count in counts)
    assert window.rate_limited == 0


@pytest.mark.parametrize("linked", [False])
def test_root_discography_chips_share_one_cosine_call_budget(harness, linked):
    first = label_catalog(harness, catalog_entries(20))
    harness.providers.discogs["/labels/43/releases"] = harness.providers.discogs["/labels/42/releases"]
    if linked:
        first = Point(point_id="link:label", kind="link", name=first.name,
                      url="https://www.discogs.com/label/42")
    second = Point(point_id="label:43", kind="label", name="Another Label", discogs_id="43")
    result = seeds.resolve_move_origins([first, second], "seed", client=harness.cosine,
                                        discogs_client=harness.discogs, cosine_budget=40)
    assert result.resolved_tracks == []
    assert len(resolution_calls(harness.providers)) == 40
    assert [point.resolution for point in result.origins] == ["missing", "unavailable"]
    assert [note["params"]["m"] for note in result.notes if note["code"] == "S-N-LABEL-FEW"] == [0]
    assert next(note for note in result.notes if note["code"] == "S-N-UNCHECKED")["params"] == {
        "seed": second.name}
    assert not any(note["code"] in {"S-N-NONE", "S-N-DROPPED"} for note in result.notes)


@pytest.mark.parametrize("source", ["saved_id"])
def test_budget_exhaustion_still_verifies_saved_cosine_id(harness, source):
    p = harness.providers
    label = label_catalog(harness, catalog_entries(20))
    label = Point(point_id="link:label", kind="link", name=label.name,
                  url="https://www.discogs.com/label/42")
    saved = track("saved", "Known Artist", "Known Song")
    p.sources[saved.id] = saved
    assert harness.cosine.similar(saved.id, limit=1).source_track.id == saved.id
    p.calls.clear()
    requested = (point(saved) if source == "saved_id" else
                 Point(point_id="link:cosine", kind="link", name=api.track_label(saved),
                       url=f"https://cosine.club/track/{saved.id}"))
    result = seeds.resolve_move_origins([label, requested], "seed", client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert [item.id for item in result.resolved_tracks] == [saved.id]
    assert result.origins[1].resolution == "exact"
    assert result.seed_to_origin == {saved.id: requested.point_id}
    assert len(resolution_calls(p)) == 60
    assert p.calls[-1][:2] == ("cosine", f"/tracks/{saved.id}/similar")
    assert not any(note["code"] in {"S-N-NONE", "S-N-UNCHECKED"} for note in result.notes)


@pytest.mark.parametrize("source", ["track",
                                    "album"])
def test_budget_exhaustion_marks_later_chips_unchecked(harness, source):
    p = harness.providers
    first = label_catalog(harness, catalog_entries(20))
    saved = track("saved", "Known Artist", "Known Song")
    p.sources[saved.id] = saved
    p.searches["Known Artist - Known Song"] = [saved]
    requested = {
        "track": point(saved).model_copy(update={"cosine_id": None}),
        "album": Point(point_id="album:later", kind="album", name="Known Artist - Album",
                       artist="Known Artist", title="Album"),
        "release": Point(point_id="link:release", kind="link", name="Release link",
                         url="https://www.discogs.com/release/99"),
        "youtube": Point(point_id="link:youtube", kind="link", name="YouTube link",
                         url="https://www.youtube.com/watch?v=saved"),
        "label": Point(point_id="label:43", kind="label", name="Another Label", discogs_id="43"),
        "label_link": Point(point_id="link:label", kind="link", name="Label link",
                            url="https://www.discogs.com/label/43"),
        "artist": Point(point_id="artist:7", kind="artist", name="Known Artist", discogs_id="7"),
        "artist_link": Point(point_id="link:artist", kind="link", name="Artist link",
                             url="https://www.discogs.com/artist/7"),
        "person": Point(point_id="person:9", kind="person", name="Person", discogs_id="9"),
    }[source]
    snapshot = root(harness, [first, requested])
    assert snapshot["step"]["status"] == "done" and snapshot["picks"] == []
    assert [origin["resolution"] for origin in snapshot["step"]["origins"]] == ["missing", "unavailable"]
    assert snapshot["step"]["origins"][1]["resolved_tracks"] == []
    assert len(resolution_calls(p)) == 60
    assert not any(path == "/tracks/saved/similar" or params.get("q") == "Known Artist - Known Song"
                   for _, path, params in p.calls)
    notes = snapshot["step"]["note"]
    assert next(note for note in notes if note["code"] == "S-N-UNCHECKED") == {
        "code": "S-N-UNCHECKED", "params": {"seed": requested.name}, "phase": "resolve",
        "message": f"'{requested.name}' wasn't checked: this step used all its cosine.club lookups on your other seeds.",
    }
    assert not any(note["code"] in {"S-N-NONE", "S-N-DROPPED"} for note in notes)
    assert [note["params"] for note in notes if note["code"] == "S-N-LABEL-FEW"] == [
        {"m": 0, "Label": first.name}]


def recording_catalog(harness, indexed):
    entries = catalog_entries(12)
    # Equal years keep the provider's returned order, even after concurrency.
    entries[8]["year"] = entries[9]["year"]
    entries[8], entries[9] = entries[9], entries[8]
    origin = label_catalog(harness, entries)
    for entry in entries:
        rid = entry["id"]
        payload = harness.providers.discogs[f"/releases/{rid}"]
        payload["tracklist"].insert(0, {"title": f"Unindexed {rid}"})
        if rid in indexed:
            for prefix, title in [("source", f"Recording {rid}"), ("second", f"Second Recording {rid}")]:
                item = track(f"{prefix}-{rid}", entry["artist"], title)
                harness.providers.searches[f"{entry['artist']} {title}"] = [item]
    return origin


def delayed_cosine(harness, delay):
    """Keep the real HTTP client and parser; only the mock wire has latency."""
    lock = Lock()
    activity = SimpleNamespace(active=0, peak=0)

    def transport(request):
        with lock:
            activity.active += 1
            activity.peak = max(activity.peak, activity.active)
        try:
            sleep(delay)  # Captured before the fixture disables retry sleeps.
            return harness.providers.cosine(request)
        finally:
            with lock:
                activity.active -= 1

    harness.cosine._client.close()
    harness.cosine._client = httpx.Client(base_url=cosine_http.BASE_URL,
                                         transport=httpx.MockTransport(transport))
    return activity


def serial_resolution(harness, origin, monkeypatch):
    with monkeypatch.context() as serial:
        serial.setattr(seeds, "COSINE_WORKERS", 1, raising=False)
        serial.setattr(seeds, "ordered_map", lambda fn, items: [fn(item) for item in items], raising=False)
        return seeds.resolve_move_origins([origin], "label", client=harness.cosine,
                                          discogs_client=harness.discogs, cosine_budget=40).to_dict()


@pytest.mark.parametrize("indexed", [range(8, 13)])
def test_label_concurrent_walkback_matches_serial_repeatedly(harness, monkeypatch, indexed):
    origin = recording_catalog(harness, indexed)
    expected = serial_resolution(harness, origin, monkeypatch)
    ordered_ids = [rid for rid in (8, 10, 9) if rid in indexed]  # Ten four-call releases exhaust O10.
    assert [item["id"] for item in expected["resolved_tracks"]] == [f"source-{rid}" for rid in ordered_ids]
    few = [note for note in expected["notes"] if note["code"] == "S-N-LABEL-FEW"]
    assert [note["params"]["m"] for note in few] == ([] if len(ordered_ids) == 5 else [len(ordered_ids)])
    delayed_cosine(harness, 0.001)
    for _ in range(4):
        result = seeds.resolve_move_origins([origin], "label", client=harness.cosine,
                                           discogs_client=harness.discogs, cosine_budget=40).to_dict()
        assert result == expected


def test_parallel_discography_budget_still_ends_walk_with_note(harness, monkeypatch):
    pacing = []
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: pacing.append(backoff))
    origin = recording_catalog(harness, indexed=range(8, 13))
    source = track("root", "Near Artist", "Root")
    near = track("near", "Near Artist", "Near", 0.9, link="https://www.discogs.com/release/100")
    p = harness.providers
    p.sources[source.id] = source
    p.neighbors[source.id] = [near]
    p.discogs["/releases/100"] = {"id": 100, "artists": [{"name": near.artist}],
        "tracklist": [{"title": near.track}], "labels": [{"id": 42, "name": origin.name}]}
    tracker = chain.BudgetTracker(40)
    for _ in range(35):
        tracker.spend()  # The move shares the 40-request allowance with earlier work.
    transport = chain._RetryOn429Transport(wrapped=httpx.MockTransport(p.discogs_response),
                                           budget_tracker=tracker)
    with httpx.Client(transport=transport) as dg:
        resolution = seeds.resolve_move_origins([point(source)], "seed", client=harness.cosine,
                                               discogs_client=dg)
        batches = list(chain.iter_move_batches(resolution, harness.cosine, discogs_client=dg,
            exclusions=load_empty_exclusions(harness), further=True, branch_limit=1))
    assert tracker.remaining == 0
    assert [item.ranked.track.id for item in batches[0].candidates] == ["near"]
    assert not batches[1].candidates
    assert batches[1].notes == [chain._further_note("budget")]
    assert len(pacing) == sum(provider == "discogs" for provider, _, _ in p.calls) == 5


@pytest.mark.parametrize("status", [400])
def test_concurrent_recording_rejection_is_a_miss_but_outages_fail(harness, status):
    origin = label_catalog(harness, catalog_entries(1))
    p = harness.providers
    p.failures["Release Artist 1 Recording 1"] = status
    p.searches["Release Artist 1 Second Recording 1"] = [track("second", "Release Artist 1", "Second Recording 1")]
    if status == 400:
        result = seeds.resolve_move_origins([origin], "label", client=harness.cosine, discogs_client=harness.discogs)
        assert [item.id for item in result.resolved_tracks] == ["second"]
    else:
        with pytest.raises(cosine_http.CosineHTTPError) as raised:
            seeds.resolve_move_origins([origin], "label", client=harness.cosine, discogs_client=harness.discogs)
        assert raised.value.status_code == status


@pytest.mark.parametrize("move", ["seed"])
def test_label_walkback_order_deduplicates_releases_and_stops_after_five(harness, move):
    entries = catalog_entries(12)
    entries[1]["year"] = entries[2]["year"] = 2026
    entries = [entries[0], entries[2], entries[1], *entries[3:]]
    entries.insert(3, dict(entries[1]))
    entries.insert(5, dict(entries[4]))
    origin = label_catalog(harness, entries, indexed=range(4, 13))
    result = seeds.resolve_move_origins([origin], move, client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert [item.id for item in result.resolved_tracks] == [f"source-{i}" for i in range(4, 9)]
    assert not any(note["code"] == "S-N-LABEL-FEW" for note in result.notes)
    examined = [path for provider, path, _ in harness.providers.calls
                if provider == "discogs" and path.startswith("/releases/")]
    assert sorted(examined) == sorted(f"/releases/{i}" for i in [3, 2, 1, 4, 5, 6, 7, 8])
    assert not any(path.startswith("/artists/") for _, path, _ in harness.providers.calls)
    batches = list(chain.iter_move_batches(result, harness.cosine, discogs_client=harness.discogs,
                                           exclusions=load_empty_exclusions(harness), further=False))
    assert {item.ranked.track.id for item in batches[0].candidates} == {f"pick-{i}" for i in range(4, 9)}


def load_empty_exclusions(harness):
    with closing(storage.connect(harness.db_path)) as conn:
        return load_exclusions(conn, build_feed_profile(conn), scope="slice")


@pytest.mark.parametrize("kind", ["label"])
@pytest.mark.parametrize("further", [False])
def test_empty_discography_run_move_uses_own_note_and_other_moves(harness, kind, further):
    origin = Point(point_id=f"{kind}:42", kind=kind, name="Empty Name", discogs_id="42")
    directory = "/labels/42/releases" if kind == "label" else "/artists/42/releases"
    harness.providers.discogs[directory] = {"releases": []}
    events = list(api.run_move([origin], kind, further, client=harness.cosine,
        discogs_client=harness.discogs, exclusions=load_empty_exclusions(harness)))
    assert not any(event.event == "pick" for event in events)
    notes = [event.data for event in events if event.event == "note"]
    own = next(note for note in notes if note["code"] == {
        "label": "S-N-LABEL-FEW", "artist": "S-HDR-ARTIST", "person": "S-HDR-PERSON"}[kind])
    if kind == "label":
        assert own["params"] == {"m": 0, "Label": "Empty Name"}
        assert own["message"] == ("Only 0 of the newest releases on Empty Name are on cosine.club, "
                                  "so this step leans on them.")
    else:
        assert own["message"] == ("Sounds like the artist Empty Name" if kind == "artist" else
                                  "Sounds like records Empty Name is credited on")
    empty = next(note for note in notes if note["code"] == "S-E-EMPTY")
    assert empty["params"] == {"alt 1": "sounds like this track" if kind == "artist" else
                                         "sounds like its artist", "alt 2": "go one step further"}
    assert empty["message"] == (f"Nothing came back for this move. Try {empty['params']['alt 1']}, "
                                "or go one step further.")
    assert not any(note["code"] in {"S-N-NONE", "S-N-DROPPED"} for note in notes)
    assert not any("seed" in note["message"].lower() for note in notes)


@pytest.mark.parametrize("further", [False])
def test_unresolved_track_run_move_names_alternative_moves(harness, further):
    origin = point(track("absent", "Absent Artist", "Missing Recording"))
    events = list(api.run_move([origin], "track", further, client=harness.cosine,
        discogs_client=harness.discogs, exclusions=load_empty_exclusions(harness)))
    assert not any(event.event == "pick" for event in events)
    resolved = events[0].data["progress"]["origins"][0]
    assert resolved["resolution"] == "missing" and resolved["resolved_tracks"] == []
    notes = [event.data for event in events if event.event == "note"]
    empty = next(note for note in notes if note["code"] == "S-E-EMPTY")
    assert empty["params"] == {"alt 1": "sounds like its artist", "alt 2": "go one step further"}
    assert empty["message"] == "Nothing came back for this move. Try sounds like its artist, or go one step further."
    assert not any(note["code"] in {"S-N-NONE", "S-N-DROPPED"} for note in notes)


@pytest.mark.parametrize("further", [False])
def test_unresolved_child_track_http_saves_empty_note_before_completion(slice_app, further):
    from tests.test_slice_api import finished, from_pick, move, read_events, root as http_root

    world = slice_app
    initial = http_root(world)
    parent = finished(world, initial)["snapshot"]
    pick = next(pick for pick in parent["picks"] if pick["cosine_id"] == "shared-track")
    origin = from_pick(initial["step_id"], pick)
    for tid, item in list(world.providers._tracks.items()):
        if item["artist"] == pick["artist"]:
            del world.providers._tracks[tid]

    result = move(world, initial["step_id"], [origin], further=further)
    saved = finished(world, result)["snapshot"]
    assert saved["step"]["status"] == "done" and saved["picks"] == []
    assert saved["step"]["parent_id"] == initial["step_id"]
    resolved = saved["step"]["origins"][0]
    assert resolved["resolution"] == "missing" and resolved["resolved_tracks"] == []
    empty = next(note for note in saved["step"]["note"] if note["code"] == "S-E-EMPTY")
    assert empty["params"] == {"alt 1": "sounds like its artist", "alt 2": "go one step further"}
    assert empty["message"] == "Nothing came back for this move. Try sounds like its artist, or go one step further."
    journal = list(read_events(world.http.get(f"/api/jobs/{result['job_id']}/events")))
    empty_at = next(index for index, event in enumerate(journal)
                    if event["event"] == "note" and event["data"]["code"] == "S-E-EMPTY")
    assert empty_at < next(index for index, event in enumerate(journal) if event["event"] == "picks_done")
    assert journal[-1]["event"] == "done"
    assert not any(event["event"] == "note" and event["data"]["code"] in {"S-N-NONE", "S-N-DROPPED"}
                   for event in journal)
    assert world.http.get(f"/api/steps/{initial['step_id']}").json()["snapshot"] == parent


def test_further_label_walkback_uses_real_batches_and_obeys_shared_budget(harness, monkeypatch):
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: None)
    origin = label_catalog(harness, catalog_entries(12), indexed=range(2, 13))
    for rid in range(1, 13):
        harness.providers.discogs[f"/releases/{rid}"]["tracklist"] = [
            {"title": f"Recording {rid}"}]
    source = track("root", "Near Artist", "Root")
    near = track("near", "Near Artist", "Near", 0.9, link="https://www.discogs.com/release/100")
    harness.providers.sources[source.id] = source
    harness.providers.neighbors[source.id] = [near]
    harness.providers.discogs["/releases/100"] = {"id": 100,
        "artists": [{"name": near.artist}], "tracklist": [{"title": near.track}],
        "labels": [{"id": 42, "name": origin.name}]}
    tracker = chain.BudgetTracker(40)
    transport = chain._RetryOn429Transport(wrapped=httpx.MockTransport(harness.providers.discogs_response),
                                           budget_tracker=tracker)
    with httpx.Client(transport=transport) as dg:
        resolution = seeds.resolve_move_origins([point(source)], "seed", client=harness.cosine, discogs_client=dg)
        iterator = chain.iter_move_batches(resolution, harness.cosine, discogs_client=dg,
            exclusions=load_empty_exclusions(harness), further=True, branch_limit=1)
        first = next(iterator)
        assert [candidate.ranked.track.id for candidate in first.candidates] == ["near"]
        assert not any(provider == "discogs" for provider, _, _ in harness.providers.calls)
        second = next(iterator)
        assert {candidate.ranked.track.id for candidate in second.candidates} == {f"pick-{i}" for i in range(2, 7)}
        assert {candidate.route["branch"]["kind"] for candidate in second.candidates} == {"label"}
        assert tracker.remaining == 32  # Parent facts + directory + six distinct releases.
        while tracker.remaining > 2:
            tracker.spend()
        harness.providers.calls.clear()
        batches = list(chain.iter_move_batches(resolution, harness.cosine, discogs_client=dg,
            exclusions=load_empty_exclusions(harness), further=True, branch_limit=1))
    assert tracker.remaining == 0
    assert [candidate.ranked.track.id for candidate in batches[0].candidates] == ["near"]
    assert not batches[1].candidates
    assert batches[1].notes == [chain._further_note("budget")]
    assert [path for provider, path, _ in harness.providers.calls if provider == "discogs"] == [
        "/releases/100", "/labels/42/releases"]


def test_artist_move_uses_main_discography_and_exact_discogs_identity(harness):
    p = harness.providers
    p.discogs["/database/search"] = {"results": [{"id": 8, "title": "Klaus Wiese"}, {"id": 9, "title": "Klaus (2)"}]}
    releases = [{"id": i, "artist": "Klaus", "title": f"Album {i}", "year": 2026 - i,
                 "role": "Appearance" if i == 1 else "Main", "type": "release"} for i in range(1, 8)]
    p.discogs["/artists/9/releases"] = {"releases": releases}
    for release in releases:
        source = track(f"s{release['id']}", "Klaus")
        p.searches[f"Klaus {release['title']}"] = [source]
        p.discogs[f"/releases/{release['id']}"] = {"artists": [{"name": source.artist}],
            "tracklist": [{"title": source.track}]}
    artist = Point(point_id="artist:name:klaus", kind="artist", name="klaus")
    assert [item["id"] for item in resolve(harness, [artist], "artist")["resolved_tracks"]] == [f"s{i}" for i in range(2, 7)]
    assert not any(params.get("q") == "Klaus Album 1" for _, _, params in p.calls)
    assert all(params.get("sort_order") == "desc" for provider, path, params in p.calls if path == "/artists/9/releases")
    assert [(path, params) for provider, path, params in p.calls if provider == "discogs"] == [
        ("/database/search", {"type": "artist", "q": "klaus", "per_page": "100", "page": "1"}),
        ("/artists/9/releases", {"sort": "year", "sort_order": "desc", "page": "1", "per_page": "20"}),
        *[(f"/releases/{i}", {}) for i in range(2, 7)]]


def test_ambiguous_exact_artist_is_a_typed_picker_not_an_arbitrary_discography(harness):
    p = harness.providers
    p.discogs["/database/search"] = lambda request: {
        "results": [{"id": int(request.url.params["page"]), "title": "Klaus (2)"}], "pagination": {"pages": 2}}
    artist = Point(point_id="artist:name:klaus", kind="artist", name="Klaus")
    result = resolve(harness, [artist], "artist")
    assert result["resolved_tracks"] == []
    assert result["notes"] == [{"code": "S-PICK-ARTIST", "phase": "resolve", "message": "Which artist?",
                                "params": {"Artist": "Klaus", "choices": [{"id": "1", "name": "Klaus (2)"},
                                                                           {"id": "2", "name": "Klaus (2)"}]}}]
    assert not any(path.startswith("/artists/") for _, path, _ in p.calls)


def person_catalog(harness, entries):
    p = harness.providers

    def page(request):
        number, size = int(request.url.params["page"]), int(request.url.params["per_page"])
        return {"releases": entries[(number - 1) * size:number * size],
                "pagination": {"pages": max(1, (len(entries) + size - 1) // size)}}

    p.discogs["/artists/9/releases"] = page
    for entry in entries:
        if entry["role"] == "Main":
            continue
        rid = entry["id"]
        source = track(f"source-{rid}", entry["artist"], f"Recording {rid}")
        p.discogs[f"/releases/{rid}"] = {"id": rid, "artists": [{"id": 10, "name": source.artist}],
            "tracklist": [{"title": source.track, "extraartists": [
                {"id": 9, "name": "Person", "role": entry["role"]}]}]}
        p.searches[f"{entry['artist']} {entry['title']}"] = [source]
        p.sources[source.id] = source
        p.neighbors[source.id] = [track(f"pick-{rid}", score=0.9)]
    return Point(point_id="person:9", kind="person", name="Person", discogs_id="9")


@pytest.mark.parametrize("main_count,pages", [(25, 1)])
def test_person_pages_past_main_releases_to_newest_credits(harness, main_count, pages):
    main = [{**entry, "role": "Main", "type": "release"} for entry in catalog_entries(main_count)]
    credits = [{**entry, "id": 1000 + i, "role": "Remix" if i < 12 else "Producer", "type": "release",
                "year": 2010 - i if i < 12 else 2026 - (i - 12)}
               for i, entry in enumerate(catalog_entries(24))]
    older = [{**entry, "id": 2000 + i, "artist": f"Older Artist {i}",
              "role": "Appearance", "type": "release", "year": 1900}
             for i, entry in enumerate(catalog_entries(200))]
    person = person_catalog(harness, [*main, *credits, *older])
    snapshot = root(harness, [person])
    expected = [f"source-{i}" for i in range(1012, 1017)]
    assert snapshot["step"]["status"] == "done"
    assert [item["cosine_id"] for item in snapshot["step"]["origins"][0]["resolved_tracks"]] == expected
    header = next(note for note in snapshot["step"]["note"] if note["code"] == "S-HDR-PERSON")
    assert header["message"] == "Sounds like records Person is credited on"
    assert [item["cosine_id"] for item in header["params"]["releases"]] == expected
    assert all(item["credits"] == [{"id": "9", "name": "Person", "roles": ["Producer"], "scope": "track"}]
               for item in header["params"]["releases"])
    assert {item["cosine_id"] for item in snapshot["picks"]} == {f"pick-{i}" for i in range(1012, 1017)}
    assert [(path, params) for provider, path, params in harness.providers.calls if provider == "discogs"] == [
        *[("/artists/9/releases", {"sort": "year", "sort_order": "desc", "page": str(page), "per_page": "100"})
          for page in range(1, pages + 1)],
        *[(f"/releases/{i}", {}) for i in range(1012, 1017)]]
    assert pages <= 5


def test_person_stops_after_five_pages_without_credits(harness):
    entries = [{**entry, "role": "Main", "type": "release"} for entry in catalog_entries(650)]
    entries.append({"id": 651, "artist": "Other Artist", "title": "Credit", "role": "Remix", "type": "release"})
    person = person_catalog(harness, entries)
    events = list(api.run_move([person], "person", False, client=harness.cosine,
        discogs_client=harness.discogs, exclusions=load_empty_exclusions(harness)))
    assert not any(event.event == "pick" for event in events)
    notes = [event.data for event in events if event.event == "note"]
    assert [note["code"] for note in notes] == ["S-HDR-PROGRESS", "S-HDR-PERSON", "S-E-EMPTY"]
    assert notes[-1]["params"] == {"alt 1": "sounds like its artist", "alt 2": "go one step further"}
    assert harness.providers.calls == [
        ("discogs", "/artists/9/releases", {"sort": "year", "sort_order": "desc", "page": str(page), "per_page": "100"})
        for page in range(1, 6)]


@pytest.mark.parametrize("stop", ["failure"])
def test_person_keeps_read_credits_when_later_page_stops(harness, monkeypatch, stop):
    from cosine_digger.httpcache import DbHttpCache

    entries = [{**entry, "role": "Main" if entry["id"] <= 95 else "Remix", "type": "release"}
               for entry in catalog_entries(105)]
    person = person_catalog(harness, entries)
    p = harness.providers
    answer = p.discogs_response

    def fail_second_page(request):
        if request.url.path == "/artists/9/releases" and request.url.params["page"] == "2":
            p.calls.append(("discogs", request.url.path, dict(request.url.params)))
            return httpx.Response(503, json={})
        return answer(request)

    pacing = []
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: pacing.append(backoff))
    tracker = chain.BudgetTracker(6 if stop == "budget" else 40)
    with closing(DbHttpCache(harness.db_path)) as cache:
        monkeypatch.setattr(harness.discogs, "_transport", chain._RetryOn429Transport(
            wrapped=httpx.MockTransport(fail_second_page), budget_tracker=tracker, http_cache=cache))
        # Cached facts remain usable after the directory exhausts the shared budget.
        for rid in range(96, 101):
            discogs._fetch_discogs_payload("release", str(rid), client=harness.discogs)
        p.calls.clear()
        pacing.clear()
        snapshot = root(harness, [person])
    assert snapshot["step"]["status"] == "done"
    assert [item["cosine_id"] for item in snapshot["step"]["origins"][0]["resolved_tracks"]] == [
        f"source-{i}" for i in range(96, 101)]
    assert {item["cosine_id"] for item in snapshot["picks"]} == {f"pick-{i}" for i in range(96, 101)}
    pages = 1 if stop == "budget" else 2
    assert [(path, params) for provider, path, params in p.calls if provider == "discogs"] == [
        ("/artists/9/releases", {"sort": "year", "sort_order": "desc", "page": str(page), "per_page": "100"})
        for page in range(1, pages + 1)]
    assert len(pacing) == pages
    assert tracker.remaining == (0 if stop == "budget" else 33)


def test_person_move_resolves_the_credited_track_not_an_uncredited_album_neighbor(harness):
    p = harness.providers
    p.discogs["/artists/9/releases"] = {"releases": [
        {"id": 10, "artist": "Other Artist", "title": "Album", "role": "Appearance", "type": "release"}]}
    p.discogs["/releases/10"] = {"id": 10, "artists": [{"id": 8, "name": "Other Artist"}], "tracklist": [
        {"position": "A1", "title": "Not Credited"}, {"position": "A2", "title": "Credited",
         "extraartists": [{"id": 9, "name": "Person", "role": "Sleeve Notes"}]}]}
    p.searches["Other Artist Album"] = [track("wrong", "Other Artist", "Not Credited")]
    p.searches["Other Artist Credited"] = [track("right", "Other Artist", "Credited")]
    origin = Point(point_id="person:9", kind="person", name="Person", discogs_id="9")
    result = resolve(harness, [origin], "person")
    assert [item["id"] for item in result["resolved_tracks"]] == ["right"]
    credit = result["notes"][0]["params"]["releases"][0]["credits"][0]
    assert credit["scope"] == "track" and credit["roles"] == ["Sleeve Notes"]


@pytest.mark.parametrize("kind", ["label", "artist"])
def test_discogs_origins_require_token_and_provider_failure_is_not_a_miss(harness, kind):
    origin = Point(point_id=f"{kind}:9", kind=kind, name="Name", discogs_id="9")
    with pytest.raises(discogs.DiscogsAuthError):
        resolve(harness, [origin], kind, discogs=False)
    assert harness.providers.calls == []
    path = "/labels/9/releases" if kind == "label" else "/artists/9/releases"
    harness.providers.failures[path] = 503
    with pytest.raises(discogs.DiscogsHTTPError):
        resolve(harness, [origin], kind)


def test_album_discogs_outage_does_not_become_an_artist_substitution(harness):
    harness.providers.failures["/database/search"] = 503
    origin = Point(point_id="album:missing", kind="album", name="Artist - Album", artist="Artist", title="Album")
    with pytest.raises(discogs.DiscogsHTTPError):
        resolve(harness, [origin])
    assert not any(params.get("q") == "Artist" for _, _, params in harness.providers.calls)


@pytest.mark.parametrize("source", ["exact"])
def test_gui_origin_searches_respect_provider_query_limit(harness, source):
    p = harness.providers
    artist = "Artist " + "Long Name " * 25 if source in {"fallback", "artist_text"} else "Artist"
    title = "Recording " + "Long Title " * 25
    hit = track("indexed", artist, title, link="https://www.discogs.com/release/10")
    requested = Point(point_id="track:long", kind="track", name=f"{artist} - {title}", artist=artist, title=title)
    if source == "exact":
        p.searches[f"{artist} - {title}"[:200]] = [hit]
    elif source == "album":
        requested.kind = "album"
        p.searches[f"{artist} {title}"[:200]] = [hit]
        p.discogs["/releases/10"] = {"id": 10, "title": title, "artists": [{"name": artist}]}
    elif source == "release":
        requested.kind, requested.url = "link", hit.external_link
        p.searches[f"{artist} {title}"[:200]] = [hit]
        p.discogs["/releases/10"] = {"id": 10, "artists": [{"name": artist}],
            "tracklist": [{"title": title}]}
    else:
        p.searches[artist[:200]] = [hit]
        if source == "artist_text":
            requested = Point(point_id="typed:artist", kind="track", name=artist)
    result = resolve(harness, [requested])
    assert [item["id"] for item in result["resolved_tracks"]] == [hit.id]
    queries = [params["q"] for provider, path, params in p.calls if provider == "cosine" and path == "/search"]
    assert queries and all(len(query) <= 200 for query in queries)


def test_more_than_ten_keeps_use_saved_ids_and_preserve_viewed_parent(harness):
    p = harness.providers
    source = track("source")
    p.sources[source.id] = source
    kept = [track(f"keep{i:02}", score=0.9) for i in range(12)]
    p.neighbors[source.id] = kept
    saved = root(harness, [point(source)])
    step_id, dig_id = saved["step"]["id"], saved["step"]["dig_id"]
    with closing(storage.connect(harness.db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in kept:
            storage.change_judgment(conn, track_key=api.track_key(item), step_id=step_id,
                                    expected_revision=0, verdict="keep")
        points = [point(item, source_step_id=step_id, source_rank=i + 1) for i, item in enumerate(kept)]
        # Simulates the upstream server's newest-first eligible snapshot.
        newest = list(reversed(points))
        p.calls.clear()
        result = resolve(harness, newest, "these")
        assert [item["id"] for item in result["resolved_tracks"]] == [f"keep{i:02}" for i in range(11, 1, -1)]
        assert result["notes"][0]["message"] == "Using your 10 most recent keeps of 12."
        assert p.calls == []
        viewed = storage.create_step(conn, dig_id=dig_id, parent_id=step_id, origins=[points[0]], move="track", further=False)
        identity = storage.create_step(conn, dig_id=dig_id, parent_id=viewed.step_id,
                    origins=[Point.model_validate(item) for item in result["origins"]], move="these", further=False,
                    context={"these_scope": "dig", "requested_count": 12})
        row = conn.execute("SELECT parent_id FROM steps WHERE id=?", (identity.step_id,)).fetchone()
        assert row[0] == viewed.step_id


def test_legacy_grouped_fusion_and_seed_output_remain_unchanged(harness):
    p = harness.providers
    p.sources.update({tid: track(tid) for tid in ("a1", "a2")})
    p.neighbors["a1"] = [track("x", score=0.71), track("null", score=None)]
    p.neighbors["a2"] = [track("x", score=0.94)]
    before = [item.model_dump() for item in fuse_similar_tracks(["a1", "a2"], harness.cosine,
                                                               seed_groups={"a1": "album", "a2": "album"})]
    gui = fuse_move_tracks(["a1", "a2"], harness.cosine, seed_to_origin={"a1": "album", "a2": "album"}, origin_order=["album"])
    assert gui[0].route["seed_scores"] == {"album": 0.94}
    after = [item.model_dump() for item in fuse_similar_tracks(["a1", "a2"], harness.cosine,
                                                              seed_groups={"a1": "album", "a2": "album"})]
    assert before == after
    assert before[0]["seed_scores"] == {"a1": 0.71} and len(before) == 1
    p.searches["Artist"] = [track("indexed", "Artist", "Other")]
    notes = []
    assert seeds.resolve_seed("Artist - Missing", harness.cosine, notify=notes.append,
                              discogs_client=harness.discogs) == []
    assert notes == []
    requested = Point(point_id="track:missing", kind="track", name="Artist - Missing", artist="Artist", title="Missing")
    assert resolve(harness, [requested])["origins"][0]["resolution"] == "substituted"


def test_legacy_artist_and_album_failures_keep_their_default_empty_output(harness):
    p = harness.providers
    p.failures["Artist"] = 503
    notes = []
    assert seeds.resolve_artist("Artist", harness.cosine, notify=notes.append) == []
    assert notes == ["[yellow]cosine.club didn't answer (503); try again in a minute[/]"]
    p.failures["/database/search"] = 503
    assert seeds.discogs_find_release("Artist", "Album", client=harness.discogs) is None
