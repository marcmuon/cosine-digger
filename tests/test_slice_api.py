"""S10 through production routes, SQLite, real engines and fake HTTP providers."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import threading
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from cosine_digger import api, chain, client as cosine_http, discogs, playlists, seeds, storage
from cosine_digger.config import AppConfig
from cosine_digger.profile import build_feed_profile
from cosine_digger.slice_types import Point
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.test_slice_library import tagged_file, identity as file_identity
from tests.test_slice_streaming import eventually, make_runtime, read_events

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def slice_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.delenv("COSINE_DIGGER_TEST_MODE", raising=False)
    monkeypatch.delenv("COSINE_DIGGER_FIXTURE", raising=False)
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "pace", lambda *args: None)
    monkeypatch.setattr(cosine_http, "_retry_wait", lambda *args: 0)
    monkeypatch.setattr(storage, "_now_iso", lambda: NOW.isoformat())
    send = httpx.HTTPTransport.handle_request

    def local_only(transport, request):
        assert request.url.host in {"localhost", "127.0.0.1"}, "Tests must use fake providers"
        return send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", local_only)
    providers = SliceFixtures()
    monkeypatch.setattr(chain, "make_rate_limited_client", providers.discogs_client)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=tmp_path / "auth.json",
                    cosine_token="fixture-cosine-key", discogs_token="fixture-discogs-key", daily_size=10, weekly_size=15)
    app = create_app(cfg, client_factory=providers.cosine_client, discogs_client_factory=providers.discogs_client,
                     sink_factory=lambda conn: providers.youtube_sink(api.DbIdStore(conn)))
    with TestClient(app, base_url="http://localhost") as client:
        yield SimpleNamespace(app=app, http=client, cfg=cfg, providers=providers)
        providers.release_all()


def post(world, path, body, key=None):
    result = world.http.post(path, json=body, headers={"Idempotency-Key": key} if key else {})
    assert result.status_code == 200, result.text
    return result.json()


def root(world, seeds=None, key=None):
    return post(world, "/api/digs", {"seeds": [p.to_dict() for p in (seeds or [world.providers.point("seed-a")])]}, key)


def finished(world, result, state="done"):
    job = eventually(lambda: (item if (item := world.http.get(f"/api/jobs/{result['job_id']}").json())["state"] != "running" else None))
    assert job["state"] == state, job
    response = world.http.get(f"/api/steps/{result['step_id']}")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["job"]["cursor"] == job["last_seq"]
    return snapshot


def from_pick(step_id, pick):
    return Point(point_id=f"track:{pick['track_key']}", kind="track", track_key=pick["track_key"],
                 name=f"{pick['artist']} - {pick['title']}", artist=pick["artist"], title=pick["title"],
                 cosine_id=pick["cosine_id"], video_id=pick["video_id"], indexed=True,
                 source_step_id=step_id, source_rank=pick["rank"])


def move(world, parent, origins, kind="track", further=False, **options):
    return post(world, "/api/steps", {"parent_step_id": parent, "origins": [p.to_dict() for p in origins],
                                      "move": kind, "further": further, **options})


def judgment(world, step, pick, verdict, revision=0):
    path = "/api/judgments/" + quote(pick["track_key"], safe="")
    response = world.http.put(path, json={"step_id": step, "verdict": verdict, "expected_revision": revision})
    assert response.status_code == 200, response.text
    return response.json()


def test_three_seeds_persist_scores_and_why(slice_app):
    world = slice_app
    result = root(world, world.providers.seed_points, "three-seeds")
    snap = finished(world, result)
    origins = snap["snapshot"]["step"]["origins"]
    assert len(origins) == 3 and all(p["resolution"] == "exact" for p in origins)
    assert [[t["cosine_id"] for t in p["resolved_tracks"]] for p in origins] == [["seed-a"], ["seed-b"], ["seed-c"]]
    ids = [point["point_id"] for point in origins]
    picks = snap["snapshot"]["picks"]
    assert len(picks) == 25
    for pick in picks:
        route = pick["route"]
        assert set(route["seed_scores"]) == set(ids)
        scores = route["seed_scores"]
        measured = [key for key in ids if scores[key] is not None]
        assert route["nearest_seed"] == max(measured, key=lambda key: scores[key])
        assert route["close_seed_ids"] == [key for key in ids if scores[key] is not None and scores[key] >= 0.70]
        assert route["close_count"] == len(route["close_seed_ids"]) and route["seed_count"] == 3
        assert "3 seeds" in route["why"]
    journal = list(read_events(world.http.get(f"/api/jobs/{result['job_id']}/events")))
    assert [e["data"]["pick"]["rank"] for e in journal if e["event"] == "pick"] == list(range(1, 26))
    assert journal[-1]["event"] == "done" and snap["snapshot"]["step"]["picks_done_at"]
    job = world.http.get(f"/api/jobs/{result['job_id']}").json()
    assert "origins" not in job["progress"].get("work", {})
    summary = world.http.get(f"/api/digs/{result['dig_id']}").json()["steps"][0]
    assert summary["origins"] == origins and summary["picks_done_at"] == snap["snapshot"]["step"]["picks_done_at"]


def test_unresolved_origin_retains_nullable_score_slot(slice_app):
    world = slice_app
    points = world.providers.seed_points[:2] + [world.providers.point("missing")]
    result = root(world, points)
    snap = finished(world, result)["snapshot"]
    assert [p["point_id"] for p in snap["step"]["origins"]] == [p.point_id for p in points]
    assert snap["step"]["origins"][2]["resolution"] == "missing"
    assert snap["step"]["origins"][2]["resolved_tracks"] == []
    for pick in snap["picks"]:
        assert pick["route"]["seed_scores"][points[2].point_id] is None
        assert pick["route"]["seed_count"] == 3 and pick["route"]["close_count"] == 2
        assert pick["route"]["why"] == "close to 2 of your 3 seeds"


def test_move_parent_rank_batches_and_snapshot(slice_app):
    world = slice_app
    initial = root(world)
    original = finished(world, initial)["snapshot"]
    pick = next(p for p in original["picks"] if p["cosine_id"] == "shared-track")
    point = from_pick(initial["step_id"], pick)
    result = move(world, initial["step_id"], [point], further=True)
    snap = finished(world, result)["snapshot"]
    assert snap["step"]["parent_id"] == initial["step_id"] and snap["step"]["number"] == 2
    assert [p["rank"] for p in snap["picks"]] == list(range(1, len(snap["picks"]) + 1))
    assert [b["hop"] for b in snap["batches"]] == [0, 1]
    assert snap["batches"][1]["start_rank"] == snap["batches"][0]["count"] + 1
    assert all(p["batch"] == 0 for p in snap["picks"][:snap["batches"][0]["count"]])
    assert len({p["track_key"] for p in snap["picks"]}) == len(snap["picks"])
    assert len({p["artist"] for p in snap["picks"]}) == len(snap["picks"])
    assert pick["track_key"] not in {p["track_key"] for p in snap["picks"]}
    for candidate in snap["picks"]:
        if candidate["batch"] == 1:
            assert candidate["route"]["attribution"] == "via_parent"
            assert candidate["route"]["parent_pick_ranks"] and candidate["route"]["parent_via"]
    duplicate = move(world, initial["step_id"], [point], further=True)
    assert duplicate == {**result, "created": False}
    assert world.http.get(f"/api/digs/{initial['dig_id']}").json()["step_count"] == 2
    assert world.http.get(f"/api/steps/{initial['step_id']}").json()["snapshot"] == original


@pytest.mark.parametrize("operation", ["rerun_of", "retry_step_id"])
@pytest.mark.parametrize("supply_token", [False, True])
def test_source_selection_survives_rerun_and_retry(slice_app, monkeypatch, operation, supply_token):
    world = slice_app
    for release in world.providers._releases.values():
        release["labels"] = [{"id": 501, "name": "Label A"}]
    parent = root(world, [world.providers.point("label")])
    parent_snapshot = finished(world, parent)["snapshot"]
    assert parent_snapshot["step"]["origins"][0]["point_id"] == "label:501"
    assert any(hub["hub_key"] == "label:501" for hub in parent_snapshot["hubs"])
    point = world.providers.point("label").model_copy(update={"source_step_id": parent["step_id"]})
    token = "origin:label:501"
    original_cosine = world.providers._cosine

    def unavailable(request):
        if request.url.path.endswith("/similar"):
            return httpx.Response(503, json={"error": "Fixture unavailable"})
        return original_cosine(request)

    state = "error" if operation == "retry_step_id" else "done"
    if operation == "retry_step_id":
        monkeypatch.setattr(world.providers, "_cosine", unavailable)
    child = move(world, parent["step_id"], [point], "label", source_selection_id=token)
    original = finished(world, child, state)["snapshot"]
    assert original["step"]["context"]["source_selection_id"] == token
    if operation == "retry_step_id":
        assert not original["picks"]
    # Hub and origin resource identity still deduplicates; the first token wins.
    duplicate = move(world, parent["step_id"], [point], "label", source_selection_id="label:501")
    assert duplicate == {**child, "created": False}
    assert world.http.get(f"/api/steps/{child['step_id']}").json()["snapshot"] == original
    old_job = world.http.get(f"/api/jobs/{child['job_id']}").json()
    monkeypatch.setattr(world.providers, "_cosine", original_cosine)
    body = {"parent_step_id": parent["step_id"], "origins": original["step"]["origins"],
            "move": "label", "further": False, operation: child["step_id"]}
    # Neither a replacement token nor an omitted token can change the original source.
    if supply_token:
        body["source_selection_id"] = "label:501"
    result = post(world, "/api/steps", body)
    updated = finished(world, result)["snapshot"]
    assert updated["step"]["context"] == original["step"]["context"]
    if operation == "retry_step_id":
        assert result["step_id"] == child["step_id"] and result["created"] is False
        assert world.http.get(f"/api/jobs/{result['job_id']}").json()["attempt"] == 2
    else:
        assert result["step_id"] != child["step_id"] and result["created"] is True
        assert world.http.get(f"/api/steps/{child['step_id']}").json()["snapshot"] == original
    assert world.http.get(f"/api/jobs/{child['job_id']}").json() == old_job
    assert world.http.get(f"/api/steps/{parent['step_id']}").json()["snapshot"] == parent_snapshot


@pytest.mark.parametrize("token", [
    "origin:label:999", "fact:1:label:999", "label:999", "fact:no-rank:label:501",
    "fact:0:label:501", "", "origin:label:501" + "x" * 1024, True, 501, None,
])
def test_invalid_source_selection_is_rejected_without_creating_a_step(slice_app, token):
    world = slice_app
    parent = root(world)
    finished(world, parent)
    point = world.providers.point("label").model_copy(update={"source_step_id": parent["step_id"]})
    with closing(storage.connect(world.cfg.db_path)) as conn:
        before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                  for table in ("steps", "slice_jobs", "slice_events")}
    calls = list(world.providers.calls)
    response = world.http.post("/api/steps", json={"parent_step_id": parent["step_id"],
        "origins": [point.to_dict()], "move": "label", "further": False, "source_selection_id": token})
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid request"}
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in before} == before
    assert world.providers.calls == calls


def test_rerun_sibling_preserves_original(slice_app):
    world = slice_app
    initial = root(world)
    original = finished(world, initial)
    pick = original["snapshot"]["picks"][0]
    judgment(world, initial["step_id"], pick, "keep")
    original = world.http.get(f"/api/steps/{initial['step_id']}").json()
    fingerprint = hashlib.sha256(storage.canonical_json(original["snapshot"]).encode()).hexdigest()
    body = {"origins": original["snapshot"]["step"]["origins"], "move": "seed", "further": False,
            "parent_step_id": None, "rerun_of": initial["step_id"]}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: post(world, "/api/steps", body, "rerun-once"), range(2)))
    assert replies[0] == replies[1]
    result = replies[0]
    assert result["step_id"] != initial["step_id"] and result["dig_id"] == initial["dig_id"]
    sibling = finished(world, result)["snapshot"]
    assert sibling["step"]["parent_id"] is None and sibling["step"]["rerun_of"] == initial["step_id"]
    assert sibling["diff"]["gone"] and sibling["diff"]["same"]
    returned = world.http.get(f"/api/steps/{initial['step_id']}").json()
    assert hashlib.sha256(storage.canonical_json(returned["snapshot"]).encode()).hexdigest() == fingerprint
    assert returned["live"]["judgments"] == original["live"]["judgments"]
    assert returned["live"]["rerun_steps"] == [{"step_id": result["step_id"], "number": 2}]
    # Gone rows retain their original source step and remain valid targets.
    gone = sibling["diff"]["gone_picks"][0]
    assert judgment(world, gone["step_id"], gone, "pass", 1)["effective"]["step_id"] == initial["step_id"]


def test_label_move_uses_newest_discogs_releases(slice_app):
    world = slice_app
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.execute("INSERT INTO tracks(path,artist,album,title,label) VALUES ('owned','Owned Label Artist','Old Album','Old File','Label A')")
    initial = root(world)
    snap = finished(world, initial)["snapshot"]
    pick = next(p for p in snap["picks"] if p["cosine_id"] == "shared-track")
    label = Point(point_id="label:501", kind="label", name="Label A", discogs_id="501",
                  source_step_id=initial["step_id"], source_rank=pick["rank"])
    result = move(world, initial["step_id"], [label], "label")
    child = finished(world, result)["snapshot"]
    assert child["step"]["header"] == "Sounds like the label Label A"
    resolved = child["step"]["origins"][0]["resolved_tracks"]
    assert [item["cosine_id"] for item in resolved] == [f"label-source-{i}" for i in range(5)]
    assert "owned-label-track" not in {t["cosine_id"] for t in resolved}
    assert ("discogs", "/labels/501/releases", {"per_page": "20", "page": "1", "sort": "year", "sort_order": "desc"}) in world.providers.calls
    assert all("Label A" in p["route"]["why"] and "released on" not in p["route"]["why"].lower() for p in child["picks"])


@pytest.mark.parametrize("kind,provider,path,params,workers", [
    ("label", "discogs", "/labels/42/releases", {"page": "1"}, 1),
    ("label", "cosine", "/tracks/lookup", None, seeds.COSINE_WORKERS),
    ("label", "cosine", "/search", {"q": "Unindexed Artist Album 1"}, 1),
    ("person", "discogs", "/masters/1", None, seeds.COSINE_WORKERS),
])
def test_cancelled_root_stops_origin_requests(slice_app, monkeypatch, kind, provider, path, params, workers):
    world = slice_app
    p = world.providers
    directory = "/labels/42/releases" if kind == "label" else "/artists/42/releases"
    p._directories[directory] = []
    for rid in range(1, 21):
        p._directories[directory].append({"id": rid, "artist": "Unindexed Artist", "title": f"Album {rid}",
            "year": 2026, "role": "Producer", "type": "master" if kind == "person" else "release"})
        payload = {"id": rid, "artists": [{"id": 6, "name": "Unindexed Artist"}],
                   "tracklist": [{"title": f"Track {rid}-{i}"} for i in range(1, 7)]}
        if kind == "person":
            p._releases[f"/masters/{rid}"] = {**payload, "main_release": rid + 100}
            p._releases[f"/releases/{rid + 100}"] = {**payload, "id": rid + 100,
                "extraartists": [{"id": 42, "name": "Person", "role": "Producer"}]}
        else:
            p._releases[f"/releases/{rid}"] = payload
    at_cancel = []

    def intercept(provider_name, send, request):
        response = send(request)
        if provider_name == "discogs" and request.url.path == directory:
            page = int(request.url.params["page"])
            response = httpx.Response(200, json={
                "releases": p._directories[directory][(page - 1) * 10:page * 10],
                "pagination": {"pages": 2}})
        if (provider_name == provider and request.url.path.removeprefix("/api/v1") == path
                and all(request.url.params.get(key) == value for key, value in (params or {}).items())
                and not at_cancel):
            # There is no step-cancel endpoint; use the runtime's existing
            # cancellation signal while exercising its real worker and writer.
            next(iter(world.app.state.slice_runtime._jobs.values())).cancelled.set()
            at_cancel.append(len(p.calls))
        return response

    cosine, dg = p._cosine, p._discogs
    monkeypatch.setattr(p, "_cosine", lambda request: intercept("cosine", cosine, request))
    monkeypatch.setattr(p, "_discogs", lambda request: intercept("discogs", dg, request))
    monkeypatch.setattr(seeds, "COSINE_WORKERS", workers)
    origin = Point(point_id=f"{kind}:42", kind=kind, name="Person", discogs_id="42")
    identity = root(world, [origin, p.point("seed-a")])
    snapshot = finished(world, identity)["snapshot"]
    assert at_cancel, "The requested cancellation boundary must be reached"
    assert p.calls[at_cancel[0]:] == []
    assert snapshot["picks"] == []
    journal = list(read_events(world.http.get(f"/api/jobs/{identity['job_id']}/events")))
    assert not any(event["event"] in {"pick", "batch", "note"} for event in journal)
    assert journal[-1]["event"] == "done"


@pytest.mark.parametrize("field", ["library_id", "source_rank"])
@pytest.mark.parametrize("value", [2**63, 2**80])
def test_sqlite_point_integer_overflow_is_rejected_before_admission(slice_app, field, value):
    world = slice_app
    if field == "library_id":
        path = "/api/digs"
        body = {"seeds": [{"point_id": "library:huge", "kind": "track", "name": "Title", field: value}]}
    else:
        initial = root(world)
        snapshot = finished(world, initial)["snapshot"]
        origin = from_pick(initial["step_id"], snapshot["picks"][0]).to_dict()
        path = "/api/steps"
        body = {"parent_step_id": initial["step_id"], "move": "track", "further": False,
                "origins": [{**origin, field: value}]}
    tables = ("digs", "steps", "slice_jobs", "slice_events")
    with closing(storage.connect(world.cfg.db_path)) as conn:
        before = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]
    calls = list(world.providers.calls)

    response = world.http.post(path, json=body)
    assert response.status_code == 422, response.text
    assert response.json() == {"error": "Invalid request"}
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables] == before
    assert world.providers.calls == calls


def test_sqlite_point_integer_maximum_reaches_storage(slice_app):
    world = slice_app
    maximum = 2**63 - 1
    source = world.providers.point("seed-a")
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.execute("INSERT INTO tracks(id,path,artist,title,cosine_verified,cosine_track_id) VALUES (?,?,?,?,1,?)",
                     (maximum, "boundary.flac", source.artist, source.title, source.cosine_id))
    identity = root(world, [Point(point_id=f"library:{maximum}", kind="track", name=source.name,
                                  library_id=maximum)])
    snapshot = finished(world, identity)["snapshot"]
    assert snapshot["step"]["origins"][0]["library_id"] == maximum
    assert snapshot["step"]["origins"][0]["resolved_tracks"][0]["cosine_id"] == source.cosine_id
    origin = from_pick(identity["step_id"], snapshot["picks"][0]).to_dict()
    response = world.http.post("/api/steps", json={
        "parent_step_id": identity["step_id"], "move": "track", "further": False,
        "origins": [{**origin, "source_rank": maximum}]})
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid move or source provenance"}
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("further", [False, True])
def test_empty_resolved_artist_suggests_other_moves(slice_app, further):
    world = slice_app
    initial = root(world)
    parent = finished(world, initial)["snapshot"]
    pick = next(item for item in parent["picks"] if item["cosine_id"] == "shared-track")
    origin = world.providers.point("artist").model_copy(update={
        "source_step_id": initial["step_id"], "source_rank": pick["rank"]})
    world.providers._neighbors["artist-source-0"] = []
    identity = move(world, initial["step_id"], [origin], "artist", further=further)
    snapshot = finished(world, identity)["snapshot"]
    assert snapshot["picks"] == []
    assert [item["cosine_id"] for item in snapshot["step"]["origins"][0]["resolved_tracks"]] == ["artist-source-0"]
    empty = next(note for note in snapshot["step"]["note"] if note["code"] == "S-E-EMPTY")
    second = "sounds like its label" if further else "go one step further"
    assert empty["params"] == {"alt 1": "sounds like this track", "alt 2": second}
    assert empty["message"] == f"Nothing came back for this move. Try sounds like this track, or {second}."
    journal = list(read_events(world.http.get(f"/api/jobs/{identity['job_id']}/events")))
    assert next(i for i, event in enumerate(journal) if event["event"] == "note"
                and event["data"]["code"] == "S-E-EMPTY") < next(
                    i for i, event in enumerate(journal) if event["event"] == "picks_done")
    assert journal[-1]["event"] == "done"


def test_unindexed_library_seed_names_artist_substitutes(slice_app):
    world = slice_app
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        for row in world.providers.library_rows:
            fields = list(row)
            conn.execute(f"INSERT INTO tracks({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(row.values()))
    response = world.http.get("/api/search", params={"q": "Unindexed", "source": "library"}).json()
    chip = response["items"][0]
    assert chip["indexed"] is False and chip["cosine_id"] is None
    result = post(world, "/api/digs", {"seeds": [{**chip, "cosine_id": "shared-track", "indexed": True}]})
    snap = finished(world, result)["snapshot"]
    origin = snap["step"]["origins"][0]
    assert origin["resolution"] == "substituted" and origin["cosine_id"] is None and origin["indexed"] is False
    assert {t["artist"] for t in origin["resolved_tracks"]} == {"Fixture Artist A"}
    note = next(n for n in snap["step"]["note"] if n["code"] == "S-N-SWAP-FILE")
    assert "Fixture Artist A" in note["message"] and "Seed A" in note["message"] and "Indexed Stand-in" in note["message"]
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert conn.execute("SELECT cosine_track_id,cosine_verified FROM tracks WHERE id=2").fetchone() == (None, 0)
        assert conn.execute("SELECT COUNT(*) FROM known_tracks").fetchone()[0] == 0
    missing = root(world, [world.providers.point("missing")])
    assert not finished(world, missing)["snapshot"]["picks"]
    down = root(world, [world.providers.point("provider-failure")])
    assert finished(world, down, "error")["snapshot"]["step"]["status"] == "failed"


@pytest.mark.parametrize(("operation", "seed", "state"), [
    ("retry_step_id", "provider-failure", "error"),
    ("rerun_of", "seed-a", "done"),
])
def test_steps_saved_before_library_source_still_retry_and_rerun(slice_app, operation, seed, state):
    world = slice_app
    initial = root(world, [world.providers.point(seed)])
    original = finished(world, initial, state)["snapshot"]
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:  # rows as an older Digger saved them
        origins = [{key: value for key, value in point.items() if key != "library_source"}
                   for point in json.loads(conn.execute(
                       "SELECT origins FROM steps WHERE id=?", (initial["step_id"],)).fetchone()[0])]
        conn.execute("UPDATE steps SET origins=? WHERE id=?", (storage.canonical_json(origins), initial["step_id"]))
    requested = [{key: value for key, value in point.items() if key not in ("resolved_tracks", "resolution")}
                 for point in original["step"]["origins"]]
    assert all("library_source" not in point for point in requested)
    result = post(world, "/api/steps", {"parent_step_id": None, "origins": requested, "move": "seed",
                                        "further": False, operation: initial["step_id"]})
    assert (result["step_id"] == initial["step_id"]) == (operation == "retry_step_id")
    finished(world, result, state)


@pytest.mark.parametrize(("operation", "seed", "state"), [
    ("retry_step_id", "provider-failure", "error"),
    ("rerun_of", "seed-a", "done"),
])
def test_library_verification_change_preserves_retry_and_rerun_origins(slice_app, operation, seed, state):
    world = slice_app
    source = world.providers.point(seed)
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        library_id = conn.execute(
            "INSERT INTO tracks(path,artist,title,cosine_verified) VALUES (?,?,?,0)",
            ("library.flac", source.artist, source.title),
        ).lastrowid
    chip = world.http.get("/api/search", params={"q": source.title, "source": "library"}).json()["items"][0]
    initial = post(world, "/api/digs", {"seeds": [chip]})
    original = finished(world, initial, state)["snapshot"]
    if operation == "retry_step_id":
        assert original["step"]["status"] == "failed" and not original["picks"]
    origins = original["step"]["origins"]
    assert origins[0]["indexed"] is False and origins[0]["cosine_id"] is None
    old_job = world.http.get(f"/api/jobs/{initial['job_id']}").json()
    old_events = world.http.get(f"/api/jobs/{initial['job_id']}/events").text
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.execute("UPDATE tracks SET cosine_verified=1,cosine_track_id=? WHERE id=?", (seed, library_id))
    body = {"parent_step_id": None, "origins": origins, "move": "seed", "further": False,
            operation: initial["step_id"]}
    for change in ({"point_id": "library:other"}, {"library_id": library_id + 1},
                   {"cosine_id": "seed-b"}, {"indexed": True}, {"title": "Forged title"},
                   {"discogs_id": "999"}, {"source_step_id": initial["step_id"]}):
        response = world.http.post("/api/steps", json={**body, "origins": [{**origins[0], **change}]})
        assert response.status_code == 422, response.text
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 1
    result = post(world, "/api/steps", body)
    assert result["dig_id"] == initial["dig_id"] and result["job_id"] != initial["job_id"]
    updated = finished(world, result, state)["snapshot"]
    assert updated["step"]["origins"] == origins
    assert world.http.get(f"/api/jobs/{initial['job_id']}").json() == old_job
    assert world.http.get(f"/api/jobs/{initial['job_id']}/events").text == old_events
    if operation == "retry_step_id":
        assert result["step_id"] == initial["step_id"] and result["created"] is False
        assert world.http.get(f"/api/jobs/{result['job_id']}").json()["attempt"] == 2
        assert updated["step"]["number"] == 1
    else:
        assert result["step_id"] != initial["step_id"] and result["created"] is True
        assert updated["step"]["parent_id"] is None and updated["step"]["rerun_of"] == initial["step_id"]
        assert updated["step"]["number"] == 2
        assert world.http.get(f"/api/steps/{initial['step_id']}").json()["snapshot"] == original
    # A fresh root must still derive facts from the current library row.
    forged = {**origins[0], "name": "Forged name", "artist": "Forged artist", "title": "Forged title",
              "cosine_id": "seed-b", "discogs_id": "999", "indexed": False,
              "resolution": "missing", "resolved_tracks": []}
    fresh = post(world, "/api/digs", {"seeds": [forged]})
    canonical = finished(world, fresh, state)["snapshot"]["step"]["origins"][0]
    assert canonical["library_id"] == library_id and canonical["indexed"] is True
    assert canonical["cosine_id"] == seed and canonical["discogs_id"] is None
    assert (canonical["name"], canonical["artist"], canonical["title"]) == (source.name, source.artist, source.title)
    if state == "done":
        assert canonical["resolution"] == "exact" and canonical["resolved_tracks"][0]["cosine_id"] == seed


def test_a_saved_library_origin_never_carries_its_seed_box_source(slice_app):
    """Seen by review (deep A1 #3): library_source (how a result reads in the seed box) was saved
    with the step, so an older Digger, whose requests can't carry that field, refused Re-run and
    Retry on it. It stays in search results only; a request that carries it is still accepted."""
    world = slice_app
    source = world.providers.point("seed-a")
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.execute("INSERT INTO tracks(path,artist,title,source,cosine_verified) VALUES ('plex:1',?,?,'plex',0)",
                     (source.artist, source.title))
    chip = world.http.get("/api/search", params={"q": source.title, "source": "library"}).json()["items"][0]
    assert chip["library_source"] == "plex"
    made = post(world, "/api/digs", {"seeds": [chip]})
    origins = finished(world, made)["snapshot"]["step"]["origins"]
    with closing(storage.connect(world.cfg.db_path)) as conn:
        saved = json.loads(conn.execute("SELECT origins FROM steps WHERE id=?", (made["step_id"],)).fetchone()[0])
    assert [point.get("library_source") for point in origins + saved] == [None, None]
    rerun = post(world, "/api/steps", {"parent_step_id": None, "origins": [{**origins[0], "library_source": "plex"}],
                                       "move": "seed", "further": False, "rerun_of": made["step_id"]})
    assert finished(world, rerun)["snapshot"]["step"]["origins"] == origins


def test_ten_unjudged_gui_moves_preserve_feeds_and_legacy_tables(slice_app):
    world = slice_app
    with closing(storage.connect(world.cfg.db_path)) as conn:
        api.import_library(conn, [{"id": f"seed-{letter}", "artist": f"Fixture Artist {letter.upper()}",
                                   "title": f"Seed {letter.upper()}", "added_at": NOW.isoformat()} for letter in "ab"] +
                                 [{"id": "old", "artist": "Owned Label Artist", "title": "Old File", "label": "Label A"}], source="folder")
        api.record_dig(conn, ["Legacy Browsing - Seed"], [])
        storage.record_served(conn, "prior", [{"track_id": "near-10", "artist": "Discovery Artist 10", "title": "Discovery 10"}])
        storage.record_known(conn, [{"track_id": "near-11", "artist": "Discovery Artist 11", "title": "Discovery 11"}], "manual")
        with conn:
            conn.execute("INSERT INTO muted_leads(lead_type,lead_value,muted_at) VALUES ('artist','Discovery Artist 12',?)", (NOW.isoformat(),))
        tables = ("sessions", "session_results", "result_events", "session_branches", "known_tracks")
        def contents():
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
        def plans():
            with closing(world.providers.cosine_client()) as client:
                return {kind: {k: v for k, v in playlists.run_playlist(kind, cfg=world.cfg, conn=conn, client=client,
                              dry_run=True, now=NOW).items() if k != "timings"} for kind in ("daily", "weekly")}
        before, feed = plans(), build_feed_profile(conn, now=NOW).to_dict()
        legacy = contents()
        initial = root(world)
        original = finished(world, initial)["snapshot"]
        for pick in original["picks"][:9]:
            result = move(world, initial["step_id"], [from_pick(initial["step_id"], pick)])
            finished(world, result)
        assert world.http.get(f"/api/digs/{initial['dig_id']}").json()["step_count"] == 10
        assert plans() == before and contents() == legacy
        assert build_feed_profile(conn, now=NOW).to_dict() == feed
        pick = next(p for p in original["picks"] if p["cosine_id"] == "shared-track")
        judgment(world, initial["step_id"], pick, "keep")
        after = plans()
        assert after["daily"]["tracks"] != before["daily"]["tracks"]
        assert {e.key: e.sources for e in build_feed_profile(conn, now=NOW).entities}["discovery artist 0"] == {"save": 4.0}
        assert {e.key: e.sources for e in build_feed_profile(conn, now=NOW).entities}["label a"]["save"] == 2.0
        passed = original["picks"][1]
        judgment(world, initial["step_id"], passed, "pass")
        for plan in plans().values():
            assert not {"seed-a", "seed-b", "owned-label-track", "near-10", "near-11", "near-12", "shared-track", passed["cosine_id"]} & {t["id"] for t in plan["tracks"]}
            assert all(value == 0 for value in plan["checks"].values())
        assert contents() == legacy


def test_judgment_source_step_and_live_marks(slice_app):
    world = slice_app
    first = root(world)
    one = finished(world, first)["snapshot"]
    second = root(world)
    two = finished(world, second)["snapshot"]
    pick = one["picks"][0]
    change = judgment(world, first["step_id"], pick, "keep")
    assert change["effective"]["step_id"] == first["step_id"] and change["revision"] == 1
    assert judgment(world, second["step_id"], pick, "keep", 1) == {**change, "change_id": None}
    path = "/api/judgments/" + quote(pick["track_key"], safe="")
    assert world.http.put(path, json={"step_id": second["step_id"], "verdict": "pass", "expected_revision": 0}).status_code == 409
    passed = judgment(world, second["step_id"], pick, "pass", 1)
    assert passed["effective"]["step_id"] == second["step_id"]
    undo = world.http.request("DELETE", path, json={"step_id": second["step_id"], "expected_revision": 2, "undo_change_id": passed["change_id"]})
    assert undo.status_code == 200 and undo.json()["effective"]["step_id"] == first["step_id"]
    assert world.http.request("DELETE", path, json={"step_id": second["step_id"], "expected_revision": 3, "undo_change_id": passed["change_id"]}).status_code == 409
    heard = post(world, "/api/heard", {"track_key": pick["track_key"], "step_id": first["step_id"]})
    assert post(world, "/api/heard", {"track_key": pick["track_key"], "step_id": second["step_id"]}) == heard
    assert heard["first_step_id"] == first["step_id"]
    assert post(world, "/api/heard", {"track_key": pick["track_key"], "step_id": first["step_id"]}) == heard
    assert world.http.post("/api/heard", json={"track_key": "fake|track", "step_id": first["step_id"]}).status_code == 422
    # Toggle-off suppresses inherited CLI verdicts; Undo restores actual legacy provenance.
    track = api.Track(id=pick["cosine_id"], name=pick["title"], track=pick["title"], artist=pick["artist"])
    with closing(storage.connect(world.cfg.db_path)) as conn:
        session_id = api.record_dig(conn, ["Other - Seed"], [api.Pick(track, 0.8, "Legacy")])
        result_id = conn.execute("SELECT id FROM session_results WHERE session_id=?", (session_id,)).fetchone()[0]
        from cosine_digger.leads import record_event
        record_event(conn, result_id, "save")
    cleared = world.http.request("DELETE", path, json={"step_id": first["step_id"], "expected_revision": 3}).json()
    assert cleared["effective"]["source"] == "none" and cleared["revision"] == 4
    restored = world.http.request("DELETE", path, json={"step_id": first["step_id"], "expected_revision": 4, "undo_change_id": cleared["change_id"]}).json()
    assert restored["effective"]["source"] == "gui" and restored["effective"]["step_id"] == first["step_id"]
    final = world.http.request("DELETE", path, json={"step_id": first["step_id"], "expected_revision": 5, "undo_change_id": change["change_id"]}).json()
    assert final["effective"]["source"] == "legacy" and final["effective"]["step_id"] is None
    assert final["effective"]["legacy_session_id"] == session_id


def test_return_snapshot_and_live_overlays(slice_app):
    world = slice_app
    first = root(world)
    original = finished(world, first)
    frozen = storage.canonical_json(original["snapshot"])
    second = root(world)
    other = finished(world, second)
    pick = original["snapshot"]["picks"][0]
    judgment(world, second["step_id"], pick, "keep")
    post(world, "/api/heard", {"track_key": pick["track_key"], "step_id": second["step_id"]})
    returned = world.http.get(f"/api/steps/{first['step_id']}").json()
    assert storage.canonical_json(returned["snapshot"]) == frozen
    assert returned["live"]["judgments"][pick["track_key"]]["step_id"] == second["step_id"]
    assert returned["live"]["heard"][pick["track_key"]]["first_step_id"] == second["step_id"]
    appearance = returned["live"]["also_in"][pick["track_key"]][0]
    assert appearance == {"dig_id": second["dig_id"], "dig_name": other["snapshot"]["step"]["origins"][0]["name"],
                          "step_id": second["step_id"], "number": 1, "rank": pick["rank"], "header": other["snapshot"]["step"]["header"]}
    assert returned["job"] == original["job"]


def test_folder_scan_search_and_owned_exclusion(slice_app, tmp_path):
    world = slice_app
    folder = tmp_path / "music"
    folder.mkdir()
    tagged_file(folder / "01.m4a", artist="Owned Label Artist", album="Old Album", title="Old File", label="Label A")
    tagged_file(folder / "02.wav", artist="Fixture Artist A", album="Missing Album A", title="Unindexed File")
    tagged_file(folder / "03.wav", artist="Library Artist B", album="Missing Album B", title="Pending File")
    tagged_file(folder / "04.wav", artist="Discovery Artist 0", album="Tagged", title="Discovery 0", label="Tagged Label")
    tagged_file(folder / "05.wav", title="Ineligible")
    before = file_identity(folder)
    scan = post(world, "/api/library/scan", {"path": str(folder)})
    job = eventually(lambda: (j if (j := world.http.get(f"/api/jobs/{scan['job_id']}").json())["state"] != "running" else None))
    assert job["result"] == {"added": 5, "total": 5} and not scan["job_id"].startswith("sl-")
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.execute("UPDATE tracks SET cosine_verified=0 WHERE title='Unindexed File'")
        conn.execute("UPDATE tracks SET cosine_verified=1,cosine_track_id='owned-label-track' WHERE title='Old File'")
    found = world.http.get("/api/search", params={"q": "  File ", "source": "library"}).json()
    assert found["query"] == "File" and len(found["items"]) == 3
    flags = {p["title"]: p["indexed"] for p in found["items"]}
    assert flags == {"Old File": True, "Unindexed File": False, "Pending File": None}
    initial = root(world)
    snap = finished(world, initial)["snapshot"]
    assert not {"owned-label-track", "shared-track"} & {p["cosine_id"] for p in snap["picks"]}
    assert world.http.get("/api/library/labels").json()["missing_tracks"] == 3
    enriched = post(world, "/api/library/labels/enrich", {"budget": 10})
    job = eventually(lambda: (j if (j := world.http.get(f"/api/jobs/{enriched['job_id']}").json())["state"] != "running" else None))
    assert job["state"] == "done" and job["result"]["filled_tracks"] == 2
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert dict(conn.execute("SELECT title,label FROM tracks")) == {"Old File": "Label A", "Unindexed File": "Filled Library Label",
                 "Pending File": "Filled Library Label", "Discovery 0": "Tagged Label", "Ineligible": None}
    assert file_identity(folder) == before


def test_keeps_moves_recompute_scope_newest_ten_and_provenance(slice_app):
    world = slice_app
    first = root(world)
    snapshot = finished(world, first)["snapshot"]
    kept = snapshot["picks"][:12]
    for pick in kept:
        judgment(world, first["step_id"], pick, "keep")
    chosen = sorted(kept, key=lambda pick: pick["track_key"])[:10]
    result = move(world, first["step_id"], [from_pick(first["step_id"], p) for p in reversed(chosen)],
                  "these", scope="step", requested_count=12)
    snap = finished(world, result)["snapshot"]
    assert [p["track_key"] for p in snap["step"]["origins"]] == [p["track_key"] for p in chosen]
    assert snap["step"]["header"] == "Sounds like your 10 keeps in step 1"
    assert next(note for note in snap["step"]["note"] if note["code"] == "S-N-KEEPS-CAP")["message"] == "Using your 10 most recent keeps of 12."
    # Dig scope can point at earlier steps while the viewed step remains parent.
    all_keeps = move(world, result["step_id"], [from_pick(first["step_id"], p) for p in chosen],
                     "these", scope="dig", requested_count=12)
    dig = finished(world, all_keeps)["snapshot"]
    assert dig["step"]["parent_id"] == result["step_id"]
    assert dig["step"]["header"] == "Sounds like your 10 keeps in this dig"
    assert {p["source_step_id"] for p in dig["step"]["origins"]} == {first["step_id"]}
    body = {"parent_step_id": first["step_id"], "origins": [from_pick(first["step_id"], p).to_dict() for p in chosen],
            "move": "these", "further": False, "scope": "step", "requested_count": 11}
    assert world.http.post("/api/steps", json=body).status_code == 422


def test_idempotency_preserves_terminal_identity_and_distinct_duplicate_request_keys(slice_app):
    world = slice_app
    first = root(world, key="root-once")
    snap = finished(world, first)["snapshot"]
    assert root(world, key="root-once") == first
    point = from_pick(first["step_id"], snap["picks"][0])
    body = {"parent_step_id": first["step_id"], "origins": [point.to_dict()], "move": "track", "further": False}
    original = post(world, "/api/steps", body, "first-move")
    frozen = finished(world, original)["snapshot"]
    duplicate = post(world, "/api/steps", body, "another-transport")
    assert duplicate == {**original, "created": False}
    assert post(world, "/api/steps", body, "another-transport") == duplicate
    assert world.http.post("/api/steps", json={**body, "further": True}, headers={"Idempotency-Key": "another-transport"}).status_code == 409
    assert world.http.get(f"/api/steps/{original['step_id']}").json()["snapshot"] == frozen
    assert world.http.get(f"/api/digs/{first['dig_id']}").json()["step_count"] == 2
    assert set(first) == {"dig_id", "step_id", "job_id", "created"}


def test_legacy_move_idempotency_survives_optional_source_selection(slice_app):
    world = slice_app
    parent = root(world)
    parent_snapshot = finished(world, parent)["snapshot"]
    point = from_pick(parent["step_id"], parent_snapshot["picks"][0])
    body = {"parent_step_id": parent["step_id"], "origins": [point.to_dict()],
            "move": "track", "further": False, "scope": None, "requested_count": None,
            "rerun_of": None, "retry_step_id": None}
    original = post(world, "/api/steps", body, "legacy-move")
    snapshot = finished(world, original)["snapshot"]
    # A pre-token request hashes these same wire fields, with the old defaults.
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        progress = storage.get_slice_job(conn, original["job_id"])["progress"]
        progress["request"]["body_hash"] = hashlib.sha256(storage.canonical_json(body).encode()).hexdigest()
        conn.execute("UPDATE slice_jobs SET progress=? WHERE id=?",
                     (storage.canonical_json(progress), original["job_id"]))
    calls = list(world.providers.calls)
    assert post(world, "/api/steps", body, "legacy-move") == original
    assert world.http.get(f"/api/steps/{original['step_id']}").json()["snapshot"] == snapshot
    assert world.http.get(f"/api/digs/{parent['dig_id']}").json()["step_count"] == 2
    assert world.providers.calls == calls


@pytest.mark.parametrize("verified", [None, 0])
def test_search_library_match_shows_indexed_without_database_writes(slice_app, verified):
    world = slice_app
    provider_point = world.providers.point("seed-a")
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.executemany("INSERT INTO tracks(path,artist,title,cosine_verified,cosine_track_id) VALUES (?,?,?,?,?)", [
            ("match", provider_point.artist.upper(), provider_point.title, verified, "outdated-id"),
            ("unknown", "Unknown Artist", "Other Seed A", None, None),
            ("absent", "Absent Artist", "Other Seed A", 0, None),
            ("known", "Known Artist", "Other Seed A", 1, "saved-id"),
        ])
        before = storage.canonical_json([tuple(row) for row in conn.execute("SELECT * FROM tracks ORDER BY id")]).encode()
        before_dump = tuple(conn.iterdump())
    library = world.http.get("/api/search", params={"q": "Seed A", "source": "library"}).json()
    match_id = next(p["library_id"] for p in library["items"] if p["artist"] == provider_point.artist.upper())
    assert next(p for p in library["items"] if p["library_id"] == match_id)["indexed"] == (
        None if verified is None else False)
    response = world.http.get("/api/search", params={"q": "Seed A", "source": "all"})
    assert response.status_code == 200
    items = response.json()["items"]
    [cosine] = [p for p in items if p["library_id"] is None]
    match = next(p for p in items if p["library_id"] == match_id)
    assert match["indexed"] is True and match["cosine_id"] == cosine["cosine_id"] == "seed-a"
    assert match["video_id"] == cosine["video_id"] == provider_point.video_id
    assert match["point_id"] == f"library:{match_id}"
    assert next(p for p in items if p["artist"] == "Unknown Artist")["indexed"] is None
    assert next(p for p in items if p["artist"] == "Absent Artist")["indexed"] is False
    known = next(p for p in items if p["artist"] == "Known Artist")
    assert known["indexed"] is True and known["cosine_id"] == "saved-id"
    assert world.http.get("/api/search", params={"q": "Seed A", "source": "library"}).json() == library
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert storage.canonical_json([tuple(row) for row in conn.execute("SELECT * FROM tracks ORDER BY id")]).encode() == before
        assert tuple(conn.iterdump()) == before_dump


def test_library_search_puts_the_artist_first_and_names_imports(slice_app):
    world = slice_app
    rows = [("plex:1", None, "Burial Hex", "Ritual", None, "plex"),
            ("/m/b.flac", "b.flac", "Zed", "Burial Ground", None, "id3"),
            ("/m/c.flac", "c.flac", "Kode9", "Burial Remix", None, "id3"),
            ("/m/d.flac", "d.flac", "Aaa", "Untitled", "Burial EP", "id3"),
            ("plex:2", None, "Burial", "Near Dark", None, "Plex"),
            ("other:3", None, "Burial", "Shell of Light", None, "My Import"),
            ("/m/f.mp3", "f.mp3", "burial", "Archangel", None, "id3")]
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        conn.executemany("INSERT INTO tracks(path,filename,artist,title,album,source) VALUES (?,?,?,?,?,?)", rows)
    items = world.http.get("/api/search", params={"q": "burial", "source": "library"}).json()["items"]
    assert [(p["artist"], p["title"], p["library_source"]) for p in items] == [
        ("burial", "Archangel", "file"), ("Burial", "Near Dark", "plex"),  # the artist's own tracks first,
        ("Burial", "Shell of Light", "import"),                            # importer names normalized
        ("Burial Hex", "Ritual", "plex"),                                   # then artists starting with it,
        ("Kode9", "Burial Remix", "file"), ("Zed", "Burial Ground", "file"),  # titles starting with it,
        ("Aaa", "Untitled", "file")]                                        # and album matches last
    assert not world.providers.calls


def test_search_limits_no_token_no_taste_writes_and_label_controls(slice_app, tmp_path):
    world = slice_app
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        for i in range(10):
            conn.execute("INSERT INTO tracks(path,title,cosine_verified) VALUES (?,?,?)", (f"file-{i}", f"Pending {i}", None))
    library = world.http.get("/api/search?q=%20Pending%20&source=library").json()
    assert library["query"] == "Pending" and len(library["items"]) == 8 and library["more"] is False
    assert all(p["indexed"] is None and p["library_id"] for p in library["items"])
    assert not world.providers.calls
    assert len(world.http.get("/api/search?q=Discovery&source=cosine").json()["items"]) == 8
    for query in ("x", "x" * 201):
        assert world.http.get("/api/search", params={"q": query}).status_code == 422
    assert world.http.get("/api/search?q=seed&source=bad").status_code == 422
    cfg = replace(world.cfg, home=tmp_path / "no-token", db_path=tmp_path / "no-token" / "taste.db", cosine_token=None, discogs_token=None)
    def denied(*args, **kwargs):
        pytest.fail("No-token paths must never open providers")
    with TestClient(create_app(cfg, client_factory=denied, discogs_client_factory=denied), base_url="http://localhost") as client:
        search = client.get("/api/search?q=Seed&source=cosine").json()
        assert search["items"] == [] and search["notes"][0]["code"] == "S-SB-NOKEY"
        assert client.post("/api/library/labels/enrich", json={}).json() == {
            "job_id": None, "skipped": True, "note": {"code": "S-SET-FILL-NOTOKEN"}}
        assert client.get("/api/library/labels").json()["active_job_id"] is None
        with closing(storage.connect(cfg.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
    with TestClient(create_app(cfg, client_factory=world.providers.cosine_client, discogs_client_factory=denied), base_url="http://localhost") as client:
        result = client.post("/api/digs", json={"seeds": [world.providers.point("label").to_dict()]})
        assert result.status_code == 422 and result.json()["error"] == "Sounds like its label needs a Discogs token. Add one in Setup."
        assert client.get("/api/digs").json() == {"digs": []}
        initial = client.post("/api/digs", json={"seeds": [world.providers.point("seed-a").to_dict()]}).json()
        assert eventually(lambda: client.get(f"/api/jobs/{initial['job_id']}").json()["state"] == "done")
        point = Point(point_id="artist:name:fixture artist a", kind="artist", name="Fixture Artist A", source_step_id=initial["step_id"])
        result = client.post("/api/steps", json={"parent_step_id": initial["step_id"], "origins": [point.to_dict()], "move": "artist", "further": False})
        assert result.status_code == 422 and result.json()["error"] == "Sounds like its artist needs a Discogs token. Add one in Setup."
        assert client.get(f"/api/digs/{initial['dig_id']}").json()["step_count"] == 1


def test_label_enrichment_http_cancel_checkpoint_and_resume(slice_app):
    world = slice_app
    with closing(storage.connect(world.cfg.db_path)) as conn, conn:
        for row in world.providers.library_rows[1:]:
            fields = list(row)
            conn.execute(f"INSERT INTO tracks({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(row.values()))
    gate = world.providers.block("enrichment")
    first = post(world, "/api/library/labels/enrich", {"budget": 2})
    assert gate.entered.wait(5)
    assert post(world, "/api/library/labels/enrich", {"budget": 100}) == first
    assert post(world, f"/api/library/labels/jobs/{first['job_id']}/cancel", {})["status"] == "stopping"
    assert post(world, f"/api/library/labels/jobs/{first['job_id']}/cancel", {})["status"] == "stopping"
    gate.released.set()
    assert eventually(lambda: world.http.get(f"/api/jobs/{first['job_id']}").json()["state"] == "stopped")
    assert post(world, f"/api/library/labels/jobs/{first['job_id']}/cancel", {})["status"] == "stopped"
    before = world.http.get(f"/api/jobs/{first['job_id']}").json()
    resumed = post(world, "/api/library/labels/enrich", {"budget": 5, "resume_job_id": first["job_id"]})
    assert resumed["job_id"] != first["job_id"]
    assert eventually(lambda: world.http.get(f"/api/jobs/{resumed['job_id']}").json()["state"] == "done")
    after = world.http.get(f"/api/jobs/{resumed['job_id']}").json()
    assert after["attempt"] == 2 and after["progress"]["work"]["resume_job_id"] == first["job_id"]
    assert world.http.get(f"/api/jobs/{first['job_id']}").json() == before
    assert world.http.get("/api/library/labels").json()["missing_tracks"] == 0
    assert list(read_events(world.http.get(f"/api/jobs/{resumed['job_id']}/events")))[-1]["event"] == "done"


def test_production_http_near_arrives_before_further_and_reconnects(make_runtime, monkeypatch):
    from tests.test_slice_streaming import test_http_near_arrives_before_further_and_reconnects
    test_http_near_arrives_before_further_and_reconnects(make_runtime, monkeypatch)


def test_dig_export_sends_every_step_to_the_digs_own_playlist_outside_the_ledger(slice_app):
    world = slice_app
    initial = root(world)
    first = finished(world, initial)["snapshot"]["picks"]
    shared = next(p for p in first if p["cosine_id"] == "shared-track")
    second = finished(world, move(world, initial["step_id"], [from_pick(initial["step_id"], shared)]))["snapshot"]["picks"]
    passed = next(p for p in first if (p["video_id"] or "").startswith("S9VIDEO"))
    judgment(world, initial["step_id"], passed, "pass")

    def send(dig_id):
        job = post(world, f"/api/digs/{dig_id}/export", {})
        done = eventually(lambda: (item if (item := world.http.get(f"/api/jobs/{job['job_id']}").json())["state"] != "running"
                                   else None))
        assert done["state"] == "done", done
        return done["result"]

    unique = {}
    for pick in first + second:  # step order, then rank; a track's first appearance wins
        unique.setdefault(pick["track_key"], pick)
    unique = list(unique.values())
    expected = [p["video_id"] for p in unique if p["video_id"] and p["track_key"] != passed["track_key"]]
    assert len(unique) < len(first) + len(second)  # the steps overlap
    assert {"S9REFUSE101", "Pso5YU_mntg"} <= set(expected)  # Digger's player can't embed these; YouTube Music plays them
    url = world.providers.youtube_sink().playlist_url
    assert send(initial["dig_id"]) == {"dig_id": initial["dig_id"], "playlist_url": url, "sent_at": NOW.isoformat(),
                                       "created": True, "verified": True, "sent": len(expected), "added": len(expected),
                                       "missing": 0, "passed": 1, "no_video": sum(not p["video_id"] for p in unique)}
    key = api.dig_playlist_key(initial["dig_id"])
    assert world.providers._playlists[key] == expected
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM served_ledger").fetchone()[0] == 0
    dig = world.http.get(f"/api/digs/{initial['dig_id']}").json()
    assert (dig["youtube_playlist_url"], dig["youtube_sent_at"]) == (url, NOW.isoformat())
    assert send(initial["dig_id"])["created"] is False
    # A second dig from the same seed has the same name but its own playlist.
    other = root(world)
    finished(world, other)
    assert world.http.get(f"/api/digs/{other['dig_id']}").json()["youtube_playlist_url"] is None
    assert send(other["dig_id"])["created"] is True
    assert world.providers._playlists[key] == expected
    assert [d["youtube_playlist_url"] for d in world.http.get("/api/digs").json()["digs"]] == [url, url]
    assert world.http.post("/api/digs/missing/export", json={}).status_code == 404
    # A private playlist opens only in the account that owns it, so its link names the login's account.
    world.cfg.ytmusic_auth_path.write_text(json.dumps({"cookie": "fixture", "x-goog-authuser": "1"}))
    assert world.http.get(f"/api/digs/{initial['dig_id']}").json()["youtube_playlist_url"] == url + "&authuser=1"
    assert send(initial["dig_id"])["playlist_url"] == url + "&authuser=1"


def test_a_second_send_of_a_dig_follows_the_send_already_going(slice_app, monkeypatch):
    world = slice_app
    initial = root(world)
    finished(world, initial)
    gate, fixture_sink = threading.Event(), world.providers.youtube_sink

    class Held:
        def __init__(self, store):
            self.sink = fixture_sink(store)

        def replace_playlist(self, *args, **kwargs):
            gate.wait(5)
            return self.sink.replace_playlist(*args, **kwargs)

    monkeypatch.setattr(world.providers, "youtube_sink", Held)
    path = f"/api/digs/{initial['dig_id']}/export"
    first = post(world, path, {})
    assert post(world, path, {}) == first  # a second press, tab or reload
    gate.set()
    assert eventually(lambda: world.http.get(f"/api/jobs/{first['job_id']}").json()["state"] == "done")
    assert post(world, path, {}) != first  # once it's done, a press sends again
