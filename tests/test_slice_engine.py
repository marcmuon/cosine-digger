"""S6 through a small HTTP writer, real providers on mock transports and SQLite.

Production routes and the durable concurrent writer belong to S10/S7. This host
consumes one engine event at a time and composes the real S1 persistence API.
"""

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cosine_digger import api, chain, client as cosine_http, discogs, profile, session, storage
from cosine_digger.cli import app as cli_app
from cosine_digger.httpcache import DbHttpCache
from cosine_digger.playlists import Exclusions, load_exclusions
from cosine_digger.profile import build_feed_profile
from cosine_digger.slice_types import Point, StepPick, build_hubs
from tests.test_slice_chain import Providers, label_scenario, point, track


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr(cosine_http, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(discogs, "pace", lambda backoff=0.0: None)
    monkeypatch.setattr(cosine_http.time, "sleep", lambda _: None)

    def no_network(*args, **kwargs):
        pytest.fail("S6 tests must not make real network calls")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    providers = Providers()

    def client_factory():
        client = cosine_http.CosineClient(token="fixture-key")
        client._client.close()
        client._client = httpx.Client(base_url=cosine_http.BASE_URL,
                                     transport=httpx.MockTransport(providers.cosine))
        return client

    cosine = client_factory()
    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(providers.discogs))
    path = tmp_path / "taste.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
    cache = DbHttpCache(path)
    dg, tracker = chain.make_rate_limited_client(budget=40, token="fixture-token", http_cache=cache)
    runs = {}
    app = FastAPI()

    def emit(conn, identity, event, data):
        return storage.append_slice_event(conn, identity.job_id, event, data, redact=lambda value: value)

    def finish(run, status="done"):
        with closing(storage.connect(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = storage.get_step_snapshot(conn, run.identity.step_id)["snapshot"]
            for item in snapshot["picks"]:
                if item["facts_state"] == "loading":
                    emit(conn, run.identity, "facts", {"rank": item["rank"], "facts": None,
                         "facts_state": "missing" if status == "done" else "unavailable"})
            snapshot = storage.get_step_snapshot(conn, run.identity.step_id)["snapshot"]
            for hub in build_hubs(snapshot["picks"], snapshot["hubs"]):
                emit(conn, run.identity, "hub", {"hub": hub.to_dict()})
            data = {"status": status, "pick_count": len(snapshot["picks"])}
            if status != "done":
                data["error"] = {"code": "provider_unavailable" if status == "failed" else "server_restarted",
                                 "message": "The provider did not answer. Nothing was lost."}
            emit(conn, run.identity, "done" if status == "done" else "error", data)

    @app.post("/moves")
    def submit(body: dict):
        origins = [Point.model_validate(value) for value in body["origins"]]
        try:
            with closing(storage.connect(path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                dig_id = body.get("dig_id") or storage.create_dig(conn, origins)
                identity = api.record_step(conn, dig_id=dig_id, parent_id=body.get("parent_id"),
                    origins=origins, move=body.get("move", "seed"), further=body.get("further", False),
                    rerun_of=body.get("rerun_of"), reuse_empty_step_id=body.get("reuse_empty_step_id"),
                    context=body.get("context"))
        except storage.SliceConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if identity.created or body.get("reuse_empty_step_id"):
            with closing(storage.connect(path)) as conn:
                exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
            run = SimpleNamespace(identity=identity, complete=False, cancelled=False)
            run.iterator = api.run_move(origins, body.get("move", "seed"), body.get("further", False),
                client=cosine, discogs_client=dg if body.get("discogs", False) else None,
                exclusions=exclusions, near_limit=body.get("near_limit", 25),
                further_limit=body.get("further_limit", 25), branch_limit=body.get("branch_limit", 3),
                cancelled=lambda: run.cancelled)
            runs[identity.job_id] = run
        return identity.to_dict()

    @app.post("/next/{job_id}")
    def next_event(job_id: str):
        run = runs[job_id]
        if run.complete:
            return {"complete": True}
        try:
            event = next(run.iterator)
        except StopIteration:
            with closing(storage.connect(path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT COUNT(*) FROM step_picks WHERE step_id=?",
                                     (run.identity.step_id,)).fetchone()[0]
                emit(conn, run.identity, "picks_done", {"pick_count": count})
            run.complete = True
            return {"complete": True}
        except (cosine_http.CosineClientError, discogs.DiscogsError):
            finish(run, "failed")
            run.complete = True
            return {"failed": True}
        with closing(storage.connect(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if event.event == "note" and "origins" in event.data.get("progress", {}):
                storage.resolve_step_origins(conn, run.identity.step_id, event.data["progress"]["origins"])
            payload = emit(conn, run.identity, event.event,
                           {"pick": event.data} if event.event == "pick" else event.data)
        return {"event": event.event, "engine_data": event.data, "persisted": payload}

    @app.post("/finish/{job_id}")
    def end(job_id: str, body: dict):
        run = runs[job_id]
        run.cancelled = True
        run.iterator.close()
        finish(run, body.get("status", "done"))
        return {"ok": True}

    @app.get("/steps/{step_id}")
    def snapshot(step_id: str):
        with closing(storage.connect(path)) as conn:
            return storage.get_step_snapshot(conn, step_id)

    with TestClient(app) as http:
        yield SimpleNamespace(http=http, path=path, providers=providers, cosine=cosine,
                              discogs=dg, tracker=tracker, runs=runs, client_factory=client_factory)
    for run in runs.values():
        run.iterator.close()
    dg.close()
    cosine.close()
    cache.close()


def start(engine, origins, **options):
    response = engine.http.post("/moves", json={"origins": [p.to_dict() for p in origins], **options})
    assert response.status_code == 200, response.text
    return SimpleNamespace(**response.json())


def snapshot(engine, identity):
    response = engine.http.get(f"/steps/{identity.step_id}")
    assert response.status_code == 200
    return response.json()


def next_event(engine, identity):
    response = engine.http.post(f"/next/{identity.job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def drain(engine, identity, *, finish=True):
    events = []
    while True:
        data = next_event(engine, identity)
        if data.get("complete") or data.get("failed"):
            break
        events.append(data)
    if finish and not data.get("failed"):
        response = engine.http.post(f"/finish/{identity.job_id}", json={})
        assert response.status_code == 200, response.text
    return events


def seeded(engine, tid="seed", neighbors=None):
    source = track(tid, video=f"video-{tid}")
    engine.providers.sources[tid] = source
    engine.providers.searches[api.track_label(source)] = [source]
    engine.providers.neighbors[tid] = neighbors or []
    return point(source)


def legacy_hashes(path):
    with closing(storage.connect(path)) as conn:
        return {name: hashlib.sha256(storage.canonical_json({
            "schema": conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0],
            "rows": [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")],
        }).encode()).hexdigest() for name in ("sessions", "session_results", "result_events", "known_tracks")}


def test_ranked_dtos_resolution_notes_and_saved_layout(engine):
    near = [track("shared", score=0.70), track("unmeasured", score=None)]
    first = seeded(engine, "first", near)
    fallback = seeded(engine, "fallback", [near[0].model_copy(update={"score": 0.69})])
    missing = Point(point_id="track:missing artist|missing", kind="track", name="Missing Artist - Missing",
                    artist="Missing Artist", title="Missing", track_key="missing artist|missing")
    file = Point(point_id="track:artist fallback|original", kind="track", name="Artist fallback - Original",
                 artist="Artist fallback", title="Original", library_id=1, indexed=False)
    engine.providers.searches["Artist fallback"] = [engine.providers.sources[fallback.cosine_id]]
    origins = [first, file, missing]
    identity = start(engine, origins)
    assert engine.providers.calls == []  # neither admission nor constructing the iterator calls a provider
    events = drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    resolved = saved["step"]["origins"]
    assert [p["point_id"] for p in resolved] == [p.point_id for p in origins]
    assert [p["resolution"] for p in resolved] == ["exact", "substituted", "missing"]
    assert resolved[1]["cosine_id"] is None and resolved[1]["indexed"] is False
    assert resolved[1]["resolved_tracks"][0]["cosine_id"] == "fallback"
    notes = {n["code"]: n for n in saved["step"]["note"]}
    assert notes["S-N-PARTIAL"]["message"] == "1 of your 3 seeds are on cosine.club."
    assert notes["S-N-SWAP-FILE"]["message"] == (
        "Your file 'Artist fallback - Original' isn't on cosine.club. "
        "Using 1 indexed Artist fallback track instead: Recording fallback.")
    assert notes["S-N-DROPPED"]["params"] == {"seed": missing.name}
    assert [p["rank"] for p in saved["picks"]] == [1, 2]
    shared, unmeasured = saved["picks"]
    assert shared["route"]["seed_scores"] == {first.point_id: 0.70, file.point_id: 0.69, missing.point_id: None}
    assert shared["route"]["close_seed_ids"] == [first.point_id]
    assert shared["route"]["why"] == f"close to {first.name}"
    assert shared["route"]["seed_count"] == 3
    assert unmeasured["route"]["why"] == f"suggested from {first.name}; similarity unavailable"
    assert unmeasured["route"]["nearest_seed"] is None
    assert set(events[0]["engine_data"]["progress"]) == {"origins"}
    pick_fields = {"rank", "track_key", "cosine_id", "artist", "title", "video_id", "score", "batch",
                   "route", "facts", "facts_state"}
    assert all(set(event["engine_data"]) == pick_fields for event in events if event["event"] == "pick")
    batches = [e["engine_data"] for e in events if e["event"] == "batch"]
    floor = min(p["score"] for p in saved["picks"])
    assert batches == [{"hop": 0, "start_rank": 1, "target_count": 25, "label": "near", "count": 2,
                        "score_floor": floor}]
    for item in saved["picks"]:
        StepPick.model_validate(item)
        assert item["id"] == f"{identity.step_id}:{item['rank']}"
        assert item["layout"]["radius"] == pytest.approx(0.08 + 0.77 * (1 - item["score"]) / (1 - floor))
        assert math.tau / 18 < item["layout"]["angle"] < 5 * math.tau / 18
        assert item["facts_state"] == "no_token" and item["facts"] is None
    assert saved["step"]["header"] == f"Sounds like 3 seeds: {first.name}, {file.name}, {missing.name}"
    assert saved["step"]["picks_done_at"] and saved["step"]["status"] == "done"
    with closing(storage.connect(engine.path)) as conn:
        assert storage.get_step_snapshot(conn, identity.step_id)["snapshot"] == saved
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM known_tracks").fetchone()[0] == 0


def test_two_seed_narrow_fused_batch_spans_full_saved_radius(engine):
    # Two disjoint lists give 25 admitted picks in a narrow RRF range (~0.833–1).
    origins = [seeded(engine, tid, [track(f"{tid}-pick-{i}") for i in range(13)])
               for tid in ("first", "second")]
    identity = start(engine, origins)
    events = drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    picks = saved["picks"]
    assert len(picks) == 25 and saved["step"]["layout_version"] == 2
    assert picks[0]["score"] == 1.0
    assert picks[-1]["score"] == pytest.approx(5 / 6)
    assert 0.833 <= picks[-1]["score"] < 0.84
    assert picks[0]["layout"]["radius"] == 0.08
    assert picks[-1]["layout"]["radius"] == 0.85
    [boundary] = [e for e in events if e["event"] == "batch"]
    assert boundary["engine_data"]["score_floor"] == picks[-1]["score"]
    assert boundary["persisted"]["score_floor"] == picks[-1]["score"]
    with closing(storage.connect(engine.path)) as conn:
        [row] = conn.execute("SELECT payload FROM slice_events WHERE job_id=? AND event='batch'",
                             (identity.job_id,)).fetchall()
        assert json.loads(row[0])["score_floor"] == picks[-1]["score"]


@pytest.mark.parametrize("empty", [False, True])
def test_run_move_unit_scores_and_empty_batch_floor(engine, empty):
    origins = [seeded(engine, tid, [] if empty else [track(f"{tid}-pick")])
               for tid in ("first", "second")]
    identity = start(engine, origins)
    events = drain(engine, identity)
    picks = snapshot(engine, identity)["snapshot"]["picks"]
    [boundary] = [e for e in events if e["event"] == "batch"]
    assert boundary["engine_data"]["score_floor"] == (None if empty else 1.0)
    assert boundary["persisted"]["score_floor"] == (None if empty else 1.0)
    assert len(picks) == (0 if empty else 2)
    assert all(p["score"] == 1.0 and p["layout"]["radius"] == 0.08 for p in picks)


def test_run_move_further_batch_uses_its_own_floor(engine):
    source, _, _ = label_scenario(engine, near_count=3, further_count=4)
    identity = start(engine, [point(source)], further=True, discogs=True, branch_limit=1)
    events = drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    boundaries = [e for e in events if e["event"] == "batch"]
    assert [e["engine_data"]["hop"] for e in boundaries] == [0, 1]
    floors = []
    for boundary in boundaries:
        picks = [p for p in saved["picks"] if p["batch"] == boundary["engine_data"]["hop"]]
        floor = min(p["score"] for p in picks)
        floors.append(floor)
        assert boundary["engine_data"]["score_floor"] == boundary["persisted"]["score_floor"] == floor
        assert min(picks, key=lambda p: p["score"])["layout"]["radius"] == 0.85
        assert all(p["layout"]["radius"] == pytest.approx(
            max(0.08, min(0.85, 0.08 + 0.77 * (1 - p["score"]) / (1 - floor)))) for p in picks)
    assert floors[0] != floors[1]


def test_complete_near_prefix_is_visible_before_blocked_further_and_never_changes(engine):
    root, _, branches = label_scenario(engine, near_count=30, labels=(42, 43, 44), further_count=15)
    entered, gate = Event(), Event()
    engine.providers.gates["/labels/42/releases"] = entered, gate
    identity = start(engine, [point(root)], further=True, discogs=True)
    for _ in range(27):  # resolution note, near boundary, complete 25-pick prefix
        next_event(engine, identity)
    prefix = snapshot(engine, identity)["snapshot"]["picks"]
    assert len(prefix) == 25 and not entered.is_set()
    assert not any(provider == "discogs" for provider, _, _ in engine.providers.calls)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(next_event, engine, identity)
        try:
            assert entered.wait(3) and not pending.done()
            assert snapshot(engine, identity)["snapshot"]["picks"] == prefix
            # Provider work holds no write transaction either.
            with closing(storage.connect(engine.path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                storage.create_dig(conn, [point(root)])
        finally:
            gate.set()
        boundary = pending.result(timeout=5)
    assert boundary["engine_data"] == {"hop": 1, "start_rank": 26, "target_count": 25,
                                        "label": "further", "count": 25,
                                        "score_floor": boundary["persisted"]["score_floor"]}
    drain(engine, identity)
    final = snapshot(engine, identity)["snapshot"]
    assert boundary["engine_data"]["score_floor"] == min(p["score"] for p in final["picks"] if p["batch"] == 1)
    assert [p["rank"] for p in final["picks"]] == list(range(1, 51))
    assert [{k: v for k, v in p.items() if k not in ("facts", "facts_state")} for p in final["picks"][:25]] == [
        {k: v for k, v in p.items() if k not in ("facts", "facts_state")} for p in prefix]
    assert not {p.id for p in branches} & {p["cosine_id"] for p in final["picks"]}
    assert final["picks"][25]["route"]["parent_via"]
    assert final["picks"][25]["route"]["parent_pick_ranks"]
    assert final["picks"][25]["route"]["attribution"] == "via_parent"
    assert final["picks"][25]["facts"] is None  # via is not a release fact
    with closing(storage.connect(engine.path)) as conn:
        journal = storage.slice_events(conn, identity.job_id)
    assert [e["event"] for e in journal].count("picks_done") == 1
    completion = next(i for i, e in enumerate(journal) if e["event"] == "picks_done")
    assert not any(e["event"] in ("pick", "batch", "note") for e in journal[completion+1:])
    assert journal[-1]["event"] == "done"


def test_exclusions_precede_limits_and_artist_admission_including_gui_judgments(engine):
    kept, passed = track("kept"), track("passed")
    judged_origin = seeded(engine, "judged", [kept, passed])
    judged = start(engine, [judged_origin])
    drain(engine, judged)
    with closing(storage.connect(engine.path)) as conn:
        api.import_library(conn, [{"id": "owned", "artist": "Owned Artist", "title": "Owned"}], source="test")
        conn.execute("UPDATE tracks SET cosine_track_id='owned-id'")
        conn.commit()
        storage.record_served(conn, "fixture", [{"track_id": "served-id", "video_id": "served-video"}])
        storage.record_known(conn, [{"track_id": "known-id", "artist": "Known Artist", "title": "Known"}], "fixture")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for item, verdict in ((kept, "keep"), (passed, "pass")):
                storage.change_judgment(conn, track_key=api.track_key(item), step_id=judged.step_id,
                                        verdict=verdict, expected_revision=0)
    allowed = [track("allowed", "Owned Artist", "Allowed"), track("other")]
    origin = seeded(engine, "second", [track("owned-id", "Alias", "Alias"),
        track("owned-key", "Owned Artist", "Owned"), track("served-alias", video="served-video"),
        track("known-alias", "Known Artist", "Known"), kept, passed, *allowed])
    before = legacy_hashes(engine.path)
    identity = start(engine, [origin], near_limit=2)
    drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    assert [p["cosine_id"] for p in saved["picks"]] == [p.id for p in allowed]
    # The step says what it left out, with keeps told apart from other known tracks.
    assert [note for note in saved["step"]["note"] if note["code"] == "S-HDR-HIDDEN"] == [{
        "code": "S-HDR-HIDDEN", "params": {"owned": 2, "served": 1, "kept": 1, "passed": 1, "known": 1},
        "message": "Hidden from this step: 2 you own, 1 already in your Daily or Weekly, 1 you kept, 1 you passed, "
                   "1 you've dug from before.", "phase": "near"}]
    assert legacy_hashes(engine.path) == before


@pytest.mark.parametrize("ledger,identity", [
    ("owned", "cosine"), ("served", "cosine"), ("known", "cosine"),
    ("served", "video"), ("known", "video"),
])
def test_near_alias_exclusions_survive_merge_and_precede_artist_admission(engine, ledger, identity):
    first = track("a", "Artist", "Song", .8, video="video-a")
    alias = track("b", "Artist", "Song (Original Mix)", .9, video="video-b")
    allowed = track("allowed", "Artist", "Unheard", .7)
    origin = seeded(engine, neighbors=[first, alias, allowed])
    with closing(storage.connect(engine.path)) as conn:
        if ledger == "owned":
            api.import_library(conn, [{"id": "file", "artist": "Library Artist", "title": "File"}],
                               source="fixture")
            with conn:
                conn.execute("UPDATE tracks SET cosine_track_id='b'")
        else:
            entry = {"track_id": "b"} if identity == "cosine" else {
                "track_id": "previous-recording", "video_id": "video-b"}
            if ledger == "served":
                storage.record_served(conn, "daily", [entry])
            else:
                storage.record_known(conn, [entry], "fixture")
        exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
    assert api.track_key(first) == api.track_key(alias)
    assert exclusions.reason(first) is None and exclusions.reason(alias) == ledger
    if ledger == "served" and identity == "cosine":
        assert "b" in exclusions.served_ids and not exclusions.served_keys
    identity = start(engine, [origin], near_limit=1)
    events = drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    assert [pick["cosine_id"] for pick in saved["picks"]] == [allowed.id]
    assert [event["engine_data"]["cosine_id"] for event in events if event["event"] == "pick"] == [allowed.id]


@pytest.mark.parametrize("representative_link", [None])
def test_runtime_further_uses_first_alias_release_link(engine, monkeypatch, representative_link):
    from cosine_digger.config import AppConfig
    from cosine_digger.web.app import create_app
    from tests.test_slice_api import finished, from_pick, move, root as create_root

    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.delenv("COSINE_DIGGER_TEST_MODE", raising=False)
    monkeypatch.delenv("COSINE_DIGGER_FIXTURE", raising=False)
    source, near, branches = label_scenario(engine, near_count=1, further_count=1)
    first = near[0].model_copy(update={"external_link": representative_link})
    engine.providers.neighbors[source.id] = [first,
        first.model_copy(update={"id": "empty-alias", "external_link": ""}),
        near[0].model_copy(update={"id": "linked-alias"}),
        near[0].model_copy(update={"id": "later-link", "external_link": "https://www.discogs.com/release/999"})]
    bootstrap = track("bootstrap")
    engine.providers.sources[bootstrap.id] = bootstrap
    engine.providers.neighbors[bootstrap.id] = [source]
    home = engine.path.parent
    cfg = AppConfig(home=home, db_path=engine.path, ytmusic_auth_path=home / "auth.json",
                    cosine_token="fixture-key", discogs_token="fixture-token")
    app = create_app(cfg, scheduler=False, client_factory=engine.client_factory,
                     discogs_client_factory=chain.make_rate_limited_client)
    with TestClient(app, base_url="http://localhost") as http:
        world = SimpleNamespace(http=http)
        initial = create_root(world, [point(bootstrap)])
        original = finished(world, initial)["snapshot"]
        origin = from_pick(initial["step_id"], original["picks"][0])
        result = move(world, initial["step_id"], [origin], further=True)
        saved = finished(world, result)["snapshot"]
        pick = saved["picks"][0]
        assert pick["cosine_id"] == first.id and pick["facts_state"] == "ready"
        assert pick["facts"]["labels"][0]["id"] == "42"
        assert [(item["cosine_id"], item["batch"]) for item in saved["picks"]] == [
            (first.id, 0), (engine.providers.neighbors[branches[0].id][0].id, 1)]
        assert not any(note["code"] == "no_further_branches" for note in saved["step"]["note"])
        assert any(provider == "discogs" and path == "/labels/42/releases"
                   for provider, path, _ in engine.providers.calls)
        assert not any(path == "/releases/999" for _, path, _ in engine.providers.calls)


@pytest.mark.parametrize("branch", [False, True])
def test_duplicate_and_sibling_rerun_preserve_original_snapshot_and_judgments(engine, branch):
    a, b, c = track("a"), track("b"), track("c")
    origin = seeded(engine, neighbors=[a, b])
    original = start(engine, [origin])
    drain(engine, original)
    if branch:
        a_origin = point(a, source_step_id=original.step_id, source_rank=1)
        engine.providers.sources[a.id] = a
        engine.providers.neighbors[a.id] = [b, c]
        original = start(engine, [a_origin], move="track", dig_id=original.dig_id, parent_id=original.step_id)
        origin = a_origin
        drain(engine, original)
    options = {"dig_id": original.dig_id, "parent_id": origin.source_step_id,
               "move": "track" if branch else "seed"}
    calls = engine.providers.calls.copy()
    duplicate = start(engine, [origin], **options)
    assert vars(duplicate) == vars(original) | {"created": False}
    assert engine.providers.calls == calls and len(engine.runs) == (2 if branch else 1)
    with closing(storage.connect(engine.path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        storage.change_judgment(conn, track_key=api.track_key(c if branch else a), step_id=original.step_id,
                                verdict="keep", expected_revision=0)
    before = storage.canonical_json(snapshot(engine, original)["snapshot"])
    judgments = snapshot(engine, original)["live"]["judgments"]
    engine.providers.neighbors[origin.cosine_id] = [b, track("new")]
    rerun = start(engine, [origin], rerun_of=original.step_id, **options)
    assert rerun.created and rerun.step_id != original.step_id and rerun.job_id != original.job_id
    assert rerun.number == original.number + 1
    drain(engine, rerun)
    assert storage.canonical_json(snapshot(engine, original)["snapshot"]) == before
    assert snapshot(engine, original)["live"]["judgments"] == judgments
    sibling = snapshot(engine, rerun)["snapshot"]
    assert sibling["step"]["parent_id"] == options["parent_id"]
    assert sibling["step"]["rerun_of"] == original.step_id
    assert sibling["diff"]["new"] == 1
    assert sibling["diff"]["new_track_keys"] == [api.track_key(track("new"))]
    assert sibling["diff"]["same"] == sibling["diff"]["gone"] == 1
    with closing(storage.connect(engine.path)) as conn:
        dig = storage.get_dig(conn, original.dig_id)
        assert dig["root_step_id"] != rerun.step_id
        assert re.fullmatch(r"[a-f0-9]{32}", rerun.dig_id)
        assert re.fullmatch(r"[a-f0-9]{32}", rerun.step_id)
        assert re.fullmatch(r"sl-[a-f0-9]{32}", rerun.job_id)


@pytest.mark.parametrize("failure", ["failed"])
def test_empty_retry_keeps_step_and_job_history_but_allocates_fresh_attempt(engine, failure):
    origin = seeded(engine)
    identity = start(engine, [origin])
    if failure == "failed":
        engine.providers.failures[f"/tracks/{origin.cosine_id}/similar"] = 503
        assert next_event(engine, identity) == {"failed": True}
        engine.providers.failures.clear()
    else:
        if failure == "stopped_after_picks_done":
            drain(engine, identity, finish=False)
            assert snapshot(engine, identity)["snapshot"]["step"]["picks_done_at"]
        response = engine.http.post(f"/finish/{identity.job_id}", json={"status": "stopped"})
        assert response.status_code == 200
    with closing(storage.connect(engine.path)) as conn:
        old_job = storage.canonical_json(storage.get_slice_job(conn, identity.job_id))
        old_events = storage.canonical_json(storage.slice_events(conn, identity.job_id))
    retry = start(engine, [origin], dig_id=identity.dig_id, reuse_empty_step_id=identity.step_id)
    assert not retry.created and retry.step_id == identity.step_id and retry.number == identity.number
    assert retry.job_id != identity.job_id
    restarted = snapshot(engine, retry)
    assert restarted["job"]["attempt"] == 2 and restarted["job"]["cursor"] == 0
    assert restarted["snapshot"]["step"]["picks_done_at"] is None
    assert restarted["snapshot"]["step"]["note"] == [] and restarted["snapshot"]["step"]["error"] is None
    engine.providers.neighbors[origin.cosine_id] = [track("retry-pick")]
    drain(engine, retry)
    assert snapshot(engine, retry)["snapshot"]["picks"][0]["rank"] == 1
    with closing(storage.connect(engine.path)) as conn:
        assert storage.canonical_json(storage.get_slice_job(conn, identity.job_id)) == old_job
        assert storage.canonical_json(storage.slice_events(conn, identity.job_id)) == old_events
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 2
        with pytest.raises(storage.SliceConflict, match="immutable"):
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                storage.append_slice_event(conn, identity.job_id, "note", {"phase": "near"}, redact=lambda x: x)


def test_failure_during_further_preserves_near_and_never_signals_success(engine):
    root, _, branches = label_scenario(engine, near_count=2, further_count=1)
    engine.providers.failures[f"/tracks/{branches[0].id}/similar"] = 503
    identity = start(engine, [point(root)], further=True, discogs=True)
    events = drain(engine, identity)
    assert [e["event"] for e in events] == ["note", "batch", "pick", "pick"]
    final = snapshot(engine, identity)
    assert final["snapshot"]["step"]["status"] == "failed"
    assert len(final["snapshot"]["picks"]) == 2 and not final["snapshot"]["step"]["picks_done_at"]
    with closing(storage.connect(engine.path)) as conn:
        assert [e["event"] for e in storage.slice_events(conn, identity.job_id)][-1] == "error"
    response = engine.http.post("/moves", json={"origins": [point(root).to_dict()], "dig_id": identity.dig_id,
        "further": True, "reuse_empty_step_id": identity.step_id})
    assert response.status_code == 409 and "empty" in response.text


@pytest.mark.parametrize("further_fails", [False, True])
def test_hidden_count_names_each_recording_once_and_survives_a_failed_further(engine, further_fails):
    root, _, branches = label_scenario(engine, near_count=2, further_count=1)
    owned = track("owned-id", "Owned Artist", "Owned", score=0.97, video="owned-video")
    engine.providers.neighbors[root.id].append(owned)  # offered near
    engine.providers.neighbors[branches[0].id].append(owned)  # and again one step further
    with closing(storage.connect(engine.path)) as conn:
        api.import_library(conn, [{"id": "owned", "artist": "Owned Artist", "title": "Owned"}], source="test")
        conn.execute("UPDATE tracks SET cosine_track_id='owned-id'")
        conn.commit()
    if further_fails:
        engine.providers.failures[f"/tracks/{branches[0].id}/similar"] = 503
    identity = start(engine, [point(root)], further=True, discogs=True)
    drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    assert saved["step"]["status"] == ("failed" if further_fails else "done")
    hidden = [note for note in saved["step"]["note"] if note["code"] == "S-HDR-HIDDEN"]
    assert hidden[-1]["params"] == {"owned": 1}
    assert hidden[-1]["message"] == "Hidden from this step: 1 you own."
    assert "owned-id" not in {pick["cosine_id"] for pick in saved["picks"]}


@pytest.mark.parametrize("unresolved", [False, True])
def test_empty_and_missing_are_explained_before_completion(engine, unresolved):
    origin = (Point(point_id="track:absent|missing", kind="track", name="Absent - Missing",
                    artist="Absent", title="Missing") if unresolved else seeded(engine))
    identity = start(engine, [origin], further=True)
    drain(engine, identity)
    saved = snapshot(engine, identity)["snapshot"]
    assert saved["step"]["status"] == "done" and saved["picks"] == []
    notes = saved["step"]["note"]
    assert notes[-1]["code"] == ("S-INS-FACTS-NOTOKEN" if unresolved else "S-E-EMPTY")
    if unresolved:
        assert next(n for n in notes if n["code"] == "S-N-NONE")["message"] == (
            "None of your seeds are on cosine.club, and it has no tracks by Absent yet. "
            "Try another track by them, or paste a cosine.club link.")
    else:
        assert notes[-1]["message"] == "Nothing came back for this move. Try sounds like its artist, or go one step further."
    with closing(storage.connect(engine.path)) as conn:
        journal = storage.slice_events(conn, identity.job_id)
    assert journal[-2]["event"] == "picks_done" and journal[-2]["data"]["pick_count"] == 0


@pytest.mark.parametrize("after", [0])
def test_cancellation_stops_admission_and_future_provider_work(engine, after):
    origin = seeded(engine, neighbors=[track("one"), track("two")])
    cancelled = False
    iterator = api.run_move([origin], "seed", True, client=engine.cosine, discogs_client=engine.discogs,
                           exclusions=Exclusions(), cancelled=lambda: cancelled)
    received = [next(iterator) for _ in range(after)]
    calls = engine.providers.calls.copy()
    cancelled = True
    assert list(iterator) == [] and engine.providers.calls == calls
    assert len([e for e in received if e.event == "pick"]) <= 1
    assert not any(provider == "discogs" for provider, _, _ in calls)
    assert not engine.cosine._client.is_closed and not engine.discogs.is_closed


def test_record_step_joins_transaction_and_admission_failure_rolls_back_every_identity(engine):
    origin = seeded(engine)
    with closing(storage.connect(engine.path)) as conn:
        with pytest.raises(RuntimeError, match="caller-owned"):
            api.record_step(conn, dig_id="missing", parent_id=None, origins=[origin], move="seed", further=False)
        conn.execute("BEGIN IMMEDIATE")
        dig_id = storage.create_dig(conn, [origin])
        api.record_step(conn, dig_id=dig_id, parent_id=None, origins=[origin], move="seed", further=False)
        assert conn.in_transaction
        with closing(storage.connect(engine.path)) as reader:
            assert reader.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 0
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
    assert engine.providers.calls == []


def test_gui_writes_no_legacy_tables_and_fixed_clock_quick_json_is_identical(engine, monkeypatch, tmp_path):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    for module in (session, storage, profile):
        monkeypatch.setattr(module, "datetime", FixedClock)
    origin = seeded(engine, neighbors=[track("near"), track("other")])
    monkeypatch.setattr(api.time, "perf_counter", lambda: 1.0)
    monkeypatch.setattr(session, "uuid4", lambda: UUID(int=1))
    baseline = tmp_path / "before-gui.db"
    # Populate every protected table through the legacy path before taking hashes.
    with closing(storage.connect(engine.path)) as conn:
        result = api.quick([origin.name], client=engine.cosine, conn=conn, export_playlist=False)
        result_id = conn.execute("SELECT id FROM session_results LIMIT 1").fetchone()[0]
        with conn:
            conn.execute("INSERT INTO result_events (result_id,event_type,created_at) VALUES (?,'save',?)",
                         (result_id, "2026-10-01T12:00:00+00:00"))
        assert result["session_id"]
        assert all(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] > 0
                   for name in ("sessions", "session_results", "result_events", "known_tracks"))
        with closing(storage.connect(baseline)) as target:
            conn.backup(target)
    hashes = legacy_hashes(engine.path)

    def forbidden(*args, **kwargs):
        pytest.fail("GUI moves must not call legacy writers or quick")

    with monkeypatch.context() as guard:
        for module, name in ((api, "quick"), (api, "record_dig"), (storage, "record_known"),
                             (session, "create_session"), (session, "record_results")):
            guard.setattr(module, name, forbidden)
        for _ in range(10):
            identity = start(engine, [origin])
            drain(engine, identity)
    assert legacy_hashes(engine.path) == hashes
    with closing(storage.connect(baseline)) as before, closing(storage.connect(engine.path)) as after:
        assert build_feed_profile(before) == build_feed_profile(after)
    # Real CLI execution against equivalent legacy inputs, with all timing and
    # session-identity fields fixed. GUI metadata must never enter this JSON.
    monkeypatch.setattr(session, "uuid4", lambda: UUID(int=2))
    monkeypatch.setattr(api, "make_client", engine.client_factory)
    outputs = []
    for path in (baseline, engine.path):
        monkeypatch.setenv("COSINE_DIGGER_DB", str(path))
        result = CliRunner().invoke(cli_app, ["quick", origin.name, "--no-export", "--json"])
        assert result.exit_code == 0, result.output
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    data = json.loads(outputs[0])
    assert data["timings"] == {"resolve_s": 0.0, "dig_s": 0.0, "total_s": 0.0}
    assert data["picks"] and all("route" not in p and "facts" not in p for p in data["picks"])
