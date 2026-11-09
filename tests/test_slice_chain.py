"""S4 batches through real provider clients, TestClient and saved SQLite picks.

The small HTTP host composes the merged S1/S3 contracts and the S4 iterator;
production run_move and streaming lifecycle are owned by S6/S7/S10.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cosine_digger import api, chain, client as cosine_http, discogs, seeds, storage
from cosine_digger.httpcache import DbHttpCache
from cosine_digger.models import Track
from cosine_digger.playlists import load_exclusions
from cosine_digger.profile import build_feed_profile
from cosine_digger.slice_types import Point, build_hubs


def track(tid, artist=None, title=None, score=0.9, *, video=None, release=None):
    title = title or f"Recording {tid}"
    return Track(id=tid, artist=artist if artist is not None else f"Artist {tid}",
                 track=title, name=title, score=score, video_id=video,
                 external_link=f"https://www.discogs.com/release/{release}" if release else None)


def point(item, **fields):
    return Point(point_id=f"track:{api.track_key(item)}", kind="track", name=api.track_label(item),
                 cosine_id=item.id, track_key=api.track_key(item), artist=item.artist,
                 title=item.track, video_id=item.video_id, **fields)


def release_payload(number, item, *, labels=(), credits=(), artists=None):
    return {"id": number, "title": "Album", "year": 2024, "styles": ["Techno"],
            "labels": [{"id": label, "name": f"Label {label}"} for label in labels],
            "artists": artists or [], "extraartists": list(credits),
            "tracklist": [{"position": "A1", "type_": "track", "title": item.track}],
            "thumb": "https://i.discogs.com/fixture.jpg"}


class Providers:
    def __init__(self):
        self.sources, self.neighbors, self.searches, self.releases = {}, {}, {}, {}
        self.lookups = {}
        self.calls, self.failures, self.gates = [], {}, {}

    def cosine(self, request):
        path = request.url.path.removeprefix("/api/v1")
        params = dict(request.url.params)
        self.calls.append(("cosine", path, params))
        if path == "/search" and len(params["q"]) > 200:
            return httpx.Response(400, json={"code": "QUERY_TOO_LONG"})
        failure = self.failures.get(params.get("q") or path)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return httpx.Response(failure, json={"code": "BAD_REQUEST" if failure == 400 else "UNAVAILABLE"})
        if path == "/search":
            return httpx.Response(200, json={"data": [item.model_dump() for item in
                self.searches.get(params["q"], [])[:int(params["limit"])]]})
        if path == "/tracks/lookup":
            return httpx.Response(200, json={"data": [item.model_dump() for item in
                self.lookups.get(params["url"], [])]})
        assert path.startswith("/tracks/") and path.endswith("/similar"), path
        tid = path.split("/")[2]
        if tid not in self.sources:
            return httpx.Response(404, json={"code": "TRACK_NOT_FOUND"})
        return httpx.Response(200, json={"data": {"source_track": self.sources[tid].model_dump(),
            "similar_tracks": [item.model_dump() for item in self.neighbors.get(tid, [])
                               [:int(params["limit"])]]}})

    def discogs(self, request):
        path = request.url.path
        self.calls.append(("discogs", path, dict(request.url.params)))
        if path in self.gates:
            entered, release = self.gates[path]
            entered.set()
            assert release.wait(5), f"test did not release {path}"
        if path in self.failures:
            return httpx.Response(self.failures[path], json={})
        payload = self.releases.get(path, {"results": []} if path == "/database/search" else None)
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(404, json={})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr(cosine_http, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(discogs, "pace", lambda backoff=0.0: None)
    monkeypatch.setattr(cosine_http.time, "sleep", lambda _: None)

    def no_network(*args, **kwargs):
        raise AssertionError("S4 tests must not make real network calls")

    monkeypatch.setattr(httpx, "get", no_network)
    providers = Providers()
    cosine = cosine_http.CosineClient(token="fixture-key")
    cosine._client.close()
    cosine._client = httpx.Client(base_url=cosine_http.BASE_URL, transport=httpx.MockTransport(providers.cosine))
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

    def persist_batch(conn, run, batch):
        count = conn.execute("SELECT COUNT(*) FROM step_picks WHERE step_id=?", (run.identity.step_id,)).fetchone()[0]
        emit(conn, run.identity, "batch", {"hop": batch.hop, "start_rank": count + 1,
             "target_count": run.limits[batch.hop], "label": "near" if batch.hop == 0 else "further"})
        for offset, candidate in enumerate(batch.candidates, 1):
            item = candidate.ranked.track
            emit(conn, run.identity, "pick", {"pick": {
                "rank": count + offset, "track_key": api.track_key(item), "cosine_id": item.id,
                "artist": item.artist, "title": item.track, "video_id": item.video_id,
                "score": candidate.ranked.final_score, "batch": batch.hop, "route": candidate.route,
                "facts": candidate.facts.to_dict() if candidate.facts else None, "facts_state": candidate.facts_state}})
        for note in batch.notes:
            emit(conn, run.identity, "note", note)

    def finish(conn, run, *, failed=False):
        snapshot = storage.get_step_snapshot(conn, run.identity.step_id)["snapshot"]
        for pick in snapshot["picks"]:
            if pick["facts_state"] == "loading":
                emit(conn, run.identity, "facts", {"rank": pick["rank"], "facts": None,
                     "facts_state": "unavailable" if failed else "missing"})
        picks = storage.get_step_snapshot(conn, run.identity.step_id)["snapshot"]["picks"]
        for hub in build_hubs(picks, snapshot["hubs"]):
            emit(conn, run.identity, "hub", {"hub": hub.to_dict()})
        if failed:
            emit(conn, run.identity, "error", {"status": "failed", "pick_count": len(picks),
                 "error": {"code": "cosine_unavailable", "message": "cosine.club didn't answer"}})
        else:
            emit(conn, run.identity, "picks_done", {"pick_count": len(picks)})
            emit(conn, run.identity, "done", {"status": "done", "pick_count": len(picks)})

    @app.post("/start")
    def start(body: dict):
        origins = [Point.model_validate(value) for value in body["origins"]]
        resolution = seeds.resolve_move_origins(origins, body.get("move", "seed"), client=cosine,
                                                discogs_client=dg if body.get("discogs", True) else None)
        with closing(storage.connect(path)) as conn, conn:
            exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
            conn.execute("BEGIN IMMEDIATE")
            dig_id = storage.create_dig(conn, origins)
            identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=origins,
                                            move=body.get("move", "seed"), further=body.get("further", True))
            storage.resolve_step_origins(conn, identity.step_id, resolution.origins)
            run = SimpleNamespace(identity=identity, limits=(body.get("near_limit", 25), body.get("further_limit", 25)),
                                  stopped=False)
            run.iterator = chain.iter_move_batches(resolution, cosine,
                discogs_client=dg if body.get("discogs", True) else None, exclusions=exclusions,
                further=body.get("further", True), near_limit=run.limits[0], further_limit=run.limits[1],
                branch_limit=body.get("branch_limit", 3), cancelled=lambda: run.stopped)
            run.near = next(run.iterator)
            persist_batch(conn, run, run.near)
            runs[identity.step_id] = run
        return identity.to_dict()

    @app.post("/advance/{step_id}")
    def advance(step_id: str):
        run = runs[step_id]
        try:
            for batch in run.iterator:
                with closing(storage.connect(path)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    persist_batch(conn, run, batch)
        except cosine_http.CosineClientError:
            failed = True
        else:
            failed = False
        with closing(storage.connect(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            finish(conn, run, failed=failed)
        with closing(storage.connect(path)) as conn:
            return storage.get_step_snapshot(conn, step_id)

    @app.post("/facts/{step_id}/{rank}")
    def ready(step_id: str, rank: int):
        run = runs[step_id]
        candidate = run.near.candidates[rank - 1]
        facts = discogs.fetch_track_facts(candidate.ranked.track, client=dg)
        assert facts is not None
        with closing(storage.connect(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            emit(conn, run.identity, "facts", {"rank": rank, "facts": facts.to_dict(), "facts_state": "ready"})
        candidate.facts, candidate.facts_state = facts, "ready"
        return facts.to_dict()

    @app.get("/snapshot/{step_id}")
    def snapshot(step_id: str):
        with closing(storage.connect(path)) as conn:
            return storage.get_step_snapshot(conn, step_id)

    with TestClient(app) as http:
        yield SimpleNamespace(http=http, providers=providers, cosine=cosine, discogs=dg, tracker=tracker,
                              path=path, cache=cache, runs=runs)
    for run in runs.values():
        run.iterator.close()
    dg.close()
    cosine.close()
    cache.close()


def start(harness, origins, **options):
    response = harness.http.post("/start", json={"origins": [origin.to_dict() for origin in origins], **options})
    assert response.status_code == 200
    return response.json()["step_id"]


def snapshot(harness, step_id):
    response = harness.http.get(f"/snapshot/{step_id}")
    assert response.status_code == 200
    return response.json()["snapshot"]


def advance(harness, step_id):
    response = harness.http.post(f"/advance/{step_id}")
    assert response.status_code == 200
    return response.json()["snapshot"]


def label_scenario(harness, *, near_count=3, labels=(42,), further_count=4):
    p = harness.providers
    root = track("root", "Root Artist", "Root", video="root-video")
    near = [track(f"n{i:02}", release=i, video=f"near-video-{i}", score=0.95-i/100) for i in range(1, near_count+1)]
    p.sources[root.id] = root
    p.neighbors[root.id] = near
    for i, item in enumerate(near, 1):
        p.releases[f"/releases/{i}"] = release_payload(i, item, labels=labels)
    branches = []
    for label in labels:
        source = track(f"source-{label}", f"Band {label}", f"Album {label}")
        branches.append(source)
        p.sources[source.id] = source
        p.releases[f"/labels/{label}/releases"] = {"releases": [
            {"id": label+100, "artist": source.artist, "title": source.track, "year": 2025}],
            "pagination": {"pages": 1}}
        p.releases[f"/releases/{label+100}"] = release_payload(label+100, source,
            artists=[{"name": source.artist}])
        p.searches[f"{source.artist} {source.track}"] = [source]
        p.neighbors[source.id] = [track(f"f{label}-{i}", score=0.9-i/100) for i in range(further_count)]
    return root, near, branches


def stable_pick(pick):
    return {key: value for key, value in pick.items() if key not in {"facts", "facts_state"}}


RATE_RESERVE_NOTE = {"code": "cosine_rate_reserve", "params": {},
    "message": "cosine.club is near its rate limit, so one step further stopped early. The near tracks are kept.",
    "phase": "further"}


def reported_rate_limit(harness, remaining, reset=2000000000):
    window = SimpleNamespace(calls=0)

    def transport(request):
        window.calls += 1
        response = harness.providers.cosine(request)
        value = remaining(request) if callable(remaining) else remaining
        response.headers["x-ratelimit-limit"] = "120"
        if value is not None:
            response.headers["x-ratelimit-remaining"] = str(value)
        if reset is not None:
            response.headers["x-ratelimit-reset"] = str(reset)
        return response

    harness.cosine._client.close()
    harness.cosine._client = httpx.Client(base_url=cosine_http.BASE_URL,
                                         transport=httpx.MockTransport(transport))
    return window


@pytest.mark.parametrize("remaining,reset,expected_branches", [
    (25, 2000000000, 0),
    (90, 2000000000, 3)])
def test_further_reserve_gate_keeps_near_and_starts_only_allowed_branches(
        harness, remaining, reset, expected_branches):
    root, _, sources = label_scenario(harness, labels=(42, 43, 44), further_count=1)
    window = reported_rate_limit(harness, remaining, reset)
    resolution = seeds.resolve_move_origins([point(root)], "seed", client=harness.cosine,
                                            discogs_client=harness.discogs)
    with closing(storage.connect(harness.path)) as conn:
        exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
    iterator = chain.iter_move_batches(resolution, harness.cosine, discogs_client=harness.discogs,
                                       exclusions=exclusions, further=True, branch_limit=3)
    near = next(iterator)
    prefix = near.model_dump()
    after_near = window.calls
    further = next(iterator)
    assert list(iterator) == []
    print(f"O11_C remaining={remaining} reset={reset} total_calls={window.calls} branch_calls={window.calls-after_near}")
    assert after_near == 2
    assert near.model_dump() == prefix
    assert [item.ranked.track.id for item in near.candidates] == ["n01", "n02", "n03"]
    assert window.calls - after_near == 3 * expected_branches
    assert len(further.candidates) == expected_branches
    assert further.notes == ([] if expected_branches else [RATE_RESERVE_NOTE])
    for source in sources:
        assert any(path == f"/tracks/{source.id}/similar" for _, path, _ in harness.providers.calls) == bool(expected_branches)
    assert sum(path.startswith("/labels/") for _, path, _ in harness.providers.calls) == expected_branches


@pytest.mark.parametrize("stop_after", ["resolution", "first_fuse"])
def test_further_reserve_rechecks_before_fuse_and_keeps_fused_branches(harness, stop_after):
    root, _, sources = label_scenario(harness, labels=(42, 43), further_count=1)
    state = SimpleNamespace(remaining=90)

    def remaining(request):
        path = request.url.path.removeprefix("/api/v1")
        if ((stop_after == "resolution" and request.url.params.get("q") == f"{sources[0].artist} {sources[0].track}")
                or (stop_after == "first_fuse" and path == f"/tracks/{sources[0].id}/similar")):
            state.remaining = 25
        return state.remaining

    window = reported_rate_limit(harness, remaining)
    resolution = seeds.resolve_move_origins([point(root)], "seed", client=harness.cosine,
                                            discogs_client=harness.discogs)
    with closing(storage.connect(harness.path)) as conn:
        exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
    iterator = chain.iter_move_batches(resolution, harness.cosine, discogs_client=harness.discogs,
                                       exclusions=exclusions, further=True, branch_limit=2)
    near = next(iterator)
    prefix = near.model_dump()
    further = next(iterator)
    assert list(iterator) == []
    expected = [] if stop_after == "resolution" else ["f42-0"]
    print(f"O11_C stop_after={stop_after} total_calls={window.calls} fused={len(expected)}")
    assert window.calls == (4 if stop_after == "resolution" else 7)
    assert near.model_dump() == prefix
    assert [item.ranked.track.id for item in further.candidates] == expected
    assert further.notes == [RATE_RESERVE_NOTE]
    assert not any(path == f"/tracks/{sources[1].id}/similar" for _, path, _ in harness.providers.calls)


def test_further_seed_move_limits_each_branch_to_fifteen_resolution_calls(harness):
    root, _, _ = label_scenario(harness, near_count=1, labels=(42, 43, 44))
    p = harness.providers
    release_labels = {}
    for label in (42, 43, 44):
        entries = [{"id": label * 100 + rid, "artist": f"Budget Band {label}",
                    "title": f"Album {rid}", "year": 2026 - rid} for rid in range(1, 21)]
        p.releases[f"/labels/{label}/releases"] = {"releases": entries}
        for entry in entries:
            rid = entry["id"]
            release_labels[str(rid)] = label
            p.releases[f"/releases/{rid}"] = {"id": rid, "artists": [{"name": entry["artist"]}],
                "tracklist": [{"title": "First"}, {"title": "Second"}, {"title": "Third"}]}
    resolution = seeds.resolve_move_origins([point(root)], "seed", client=harness.cosine,
                                            discogs_client=harness.discogs)
    with closing(storage.connect(harness.path)) as conn:
        exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
    batches = list(chain.iter_move_batches(resolution, harness.cosine, discogs_client=harness.discogs,
                                          exclusions=exclusions, further=True, branch_limit=3))
    counts = dict.fromkeys((42, 43, 44), 0)
    for provider, path, params in p.calls:
        if provider != "cosine":
            continue
        if path == "/tracks/lookup":
            counts[release_labels[params["url"].rsplit("/", 1)[1]]] += 1
        elif path == "/search" and params["q"].startswith("Budget Band "):
            counts[int(params["q"].split()[2])] += 1
    print(f"O10_D branch_cosine_calls={counts}")
    assert all(0 < count <= 15 for count in counts.values())
    assert [item.ranked.track.id for item in batches[0].candidates] == ["n01"]
    assert not batches[1].candidates
    assert batches[1].notes == [chain._further_note("missing")]


def test_recording_aliases_share_scores_votes_and_seed_sector(harness):
    p = harness.providers
    a, b = track("seed-a"), track("seed-b")
    origins = [point(a), point(b)]
    p.sources.update({a.id: a, b.id: b})
    p.neighbors[a.id] = [track("alias-a", "Same Artist", "Same Song", 0.71),
                         track("alias-a2", "SAME ARTIST", "Same Song (Original Mix)", 0.87)]
    p.neighbors[b.id] = [track("alias-b", "Same Artist", "Same Song", 0.95)]
    step = start(harness, origins, further=False)
    saved = snapshot(harness, step)
    assert len(saved["picks"]) == 1
    pick = saved["picks"][0]
    route = pick["route"]
    assert route["seed_scores"] == {origins[0].point_id: 0.87, origins[1].point_id: 0.95}
    assert route["close_seed_ids"] == [origin.point_id for origin in origins]
    assert route["close_count"] == route["seed_count"] == 2
    assert route["nearest_seed"] == route["layout_seed"] == origins[1].point_id
    assert route["why"] == "close to all 2 seeds"
    assert route["raw_score"] == pytest.approx(2 / 60)
    assert 3.141593 < pick["layout"]["angle"] < 2 * 3.141593
    assert harness.runs[step].near.candidates[0].ranked.match_count == 2


def test_further_recording_aliases_merge_parent_scores_and_branch_contributions(harness):
    p = harness.providers
    _, near, sources = label_scenario(harness, near_count=2, labels=(42, 43), further_count=0)
    a, b = track("seed-a"), track("seed-b")
    origins = [point(a), point(b)]
    p.sources.update({a.id: a, b.id: b})
    p.neighbors[a.id] = [near[0].model_copy(update={"score": 0.71})]
    p.neighbors[b.id] = [near[1].model_copy(update={"score": 0.95})]
    for rank, (parent, source, label) in enumerate(zip(near, sources, (42, 43)), 1):
        p.releases[f"/releases/{rank}"] = release_payload(rank, parent, labels=(label,))
        p.neighbors[source.id] = [
            track(f"alias-{label}", "Same Artist" if rank == 1 else "SAME ARTIST (2)",
                  "Same Song" if rank == 1 else "Same Song (Original Mix)", 0.76 if rank == 1 else 0.88),
            track(f"single-{label}")]

    step = start(harness, origins, branch_limit=2)
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    picks = finished["picks"]
    assert [stable_pick(pick) for pick in picks[:2]] == [stable_pick(pick) for pick in prefix]
    assert [pick["cosine_id"] for pick in picks] == [near[0].id, near[1].id, "alias-42", "single-42", "single-43"]
    further = picks[2]
    route = further["route"]
    assert route["seed_scores"] == {origins[0].point_id: 0.71, origins[1].point_id: 0.95}
    assert route["nearest_seed"] == route["layout_seed"] == origins[1].point_id
    assert route["close_seed_ids"] == [origin.point_id for origin in origins]
    assert route["close_count"] == route["seed_count"] == 2
    assert route["parent_pick_ranks"] == [1, 2]
    assert route["parent_pick_keys"] == [pick["track_key"] for pick in prefix]
    assert route["branch_seed_scores"] == {sources[0].id: 0.76, sources[1].id: 0.88}
    assert route["attribution"] == "via_parent"
    assert route["raw_score"] == pytest.approx(1 / 60 * 1.3 * 0.85)
    assert route["normalization_basis"] == route["raw_score"]
    assert further["score"] == 1.0 and all(pick["score"] < 1.0 for pick in picks[3:])
    with closing(storage.connect(harness.path)) as conn:
        journal = storage.slice_events(conn, harness.runs[step].identity.job_id)
    assert [event["data"]["pick"]["rank"] for event in journal if event["event"] == "pick"] == list(range(1, 6))


@pytest.mark.parametrize("identity", ["cosine"])
@pytest.mark.parametrize("nested_alias", [False])
def test_cross_branch_alias_exclusions_survive_both_merges(harness, identity, nested_alias):
    root, near, sources = label_scenario(harness, near_count=1, labels=(42, 43), further_count=0)
    a = track("a", "Artist", "Song", .8, video="video-a")
    b = track("b", "Artist", "Song (Original Mix)", .9, video="video-b")
    allowed = track("allowed", "Artist", "Unheard", .7)
    harness.providers.neighbors[sources[0].id] = [a, allowed]
    harness.providers.neighbors[sources[1].id] = [b]
    if nested_alias:
        harness.providers.neighbors[sources[1].id].insert(0, track("b-first", "Artist", "Song", .8))
    with closing(storage.connect(harness.path)) as conn:
        entry = {"track_id": "b"} if identity == "cosine" else {
            "track_id": "previous-recording", "video_id": "video-b"}
        storage.record_known(conn, [entry], "fixture")
    step = start(harness, [point(root)], branch_limit=2, further_limit=1)
    prefix = snapshot(harness, step)["picks"]
    saved = advance(harness, step)
    assert [(pick["cosine_id"], pick["batch"]) for pick in saved["picks"]] == [
        (near[0].id, 0), (allowed.id, 1)]
    assert [stable_pick(pick) for pick in saved["picks"][:1]] == [stable_pick(pick) for pick in prefix]


@pytest.mark.parametrize("identity", ["cosine", "video"])
def test_admitted_alias_identities_exclude_further_reappearances(harness, identity):
    root, near, sources = label_scenario(harness, near_count=1, further_count=0)
    alias = track("near-alias", near[0].artist, near[0].track, .8, video="alias-video")
    harness.providers.neighbors[root.id].append(alias)
    repeated = track("near-alias" if identity == "cosine" else "other-id", "Other Artist", "Other Song",
                     video="alias-video" if identity == "video" else None)
    fresh = track("fresh")
    harness.providers.neighbors[sources[0].id] = [repeated, fresh]
    step = start(harness, [point(root)], further_limit=1)
    saved = advance(harness, step)
    assert [pick["cosine_id"] for pick in saved["picks"]] == [near[0].id, fresh.id]


def test_complete_near_is_persisted_while_further_gate_is_closed(harness):
    root, _, _ = label_scenario(harness, near_count=30, labels=(42, 43, 44), further_count=15)
    p = harness.providers
    entered, gate = Event(), Event()
    p.gates["/labels/42/releases"] = entered, gate
    step = start(harness, [point(root)])
    prefix = snapshot(harness, step)["picks"]
    assert len(prefix) == 25 and [pick["rank"] for pick in prefix] == list(range(1, 26))
    assert all(pick["batch"] == 0 and 0 <= pick["score"] <= 1 for pick in prefix)
    assert not entered.is_set() and not any(provider == "discogs" for provider, _, _ in p.calls)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(advance, harness, step)
        try:
            assert entered.wait(3)
            assert not gate.is_set() and not future.done()
            assert snapshot(harness, step)["picks"] == prefix
        finally:
            gate.set()
        finished = future.result(timeout=5)
    assert len(finished["picks"]) == 50
    assert [pick["rank"] for pick in finished["picks"]] == list(range(1, 51))
    assert [stable_pick(pick) for pick in finished["picks"][:25]] == [stable_pick(pick) for pick in prefix]
    assert finished["batches"] == [{"hop": 0, "start_rank": 1, "label": "near", "count": 25},
                                  {"hop": 1, "start_rank": 26, "label": "further", "count": 25}]
    near_step = start(harness, [point(root)], further=False)
    near_only = snapshot(harness, near_step)["picks"]
    assert [(pick["cosine_id"], pick["score"]) for pick in near_only] == [
        (pick["cosine_id"], pick["score"]) for pick in prefix]


def test_admission_does_not_replace_artists_reset_routes_or_admit_sources(harness):
    root, near, sources = label_scenario(harness, labels=(42, 43), further_count=12)
    shared = track("shared", "Cross Route", "Shared", score=None)
    replacements = [track("higher", near[0].artist.upper()+" (2)", "Different", score=1),
                    near[1].model_copy(update={"id": "same-key", "score": 1}),
                    track("same-video", "Video Alias", video=near[2].video_id), root]
    for source in sources:
        harness.providers.neighbors[source.id] = [shared, *replacements, *sources,
                                                   *harness.providers.neighbors[source.id],
                                                   track("anonymous-a", ""), track("anonymous-b", "")]
    step = start(harness, [point(root)], near_limit=10, further_limit=10)
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    picks = finished["picks"]
    assert [stable_pick(pick) for pick in picks[:len(prefix)]] == [stable_pick(pick) for pick in prefix]
    assert not {"higher", "same-key", "same-video", root.id, *[source.id for source in sources]} & {
        pick["cosine_id"] for pick in picks}
    further = [pick for pick in picks if pick["batch"] == 1]
    assert len(further) == 8  # Combined target 20 => four per distinct route, with no fill relaxation.
    assert sum(pick["route"]["branch"]["discogs_id"] == "42" for pick in further) == 4
    assert sum(pick["route"]["branch"]["discogs_id"] == "43" for pick in further) == 4
    cross = next(pick for pick in further if pick["cosine_id"] == "shared")
    assert cross["score"] == 1.0
    assert cross["route"]["raw_score"] / cross["route"]["normalization_basis"] == cross["score"]
    assert cross["route"]["branch_seed_scores"] == {source.id: None for source in sources}
    assert cross["facts"] is None  # A label route is never a release fact.


def test_exclusions_and_source_fallbacks_apply_before_artist_and_limit(harness):
    root, near, sources = label_scenario(harness, further_count=5)
    owned = track("owned", "Owned Artist", "Owned")
    allowed = track("allowed", owned.artist, "Unheard")
    served = track("served-alias", "Served", video="served-video")
    known = track("known-alias", "Known", "Known")
    passed = track("passed", "Passed", "Passed")
    muted = track("muted", "Muted Artist (2)")
    excluded = [owned, served, known, passed, muted]
    with closing(storage.connect(harness.path)) as conn:
        api.import_library(conn, [{"id": "owned", "artist": owned.artist, "title": owned.track}], source="fixture")
        storage.record_served(conn, "fixture", [{"track_id": "served-original", "video_id": "served-video"}])
        storage.record_known(conn, [{"track_id": "known-original", "artist": known.artist, "title": known.track}], "fixture")
        sid = api.record_dig(conn, [], [api.Pick(track=passed, score=1, why="fixture")])
        rid = conn.execute("SELECT id FROM session_results WHERE session_id=?", (sid,)).fetchone()[0]
        conn.execute("INSERT INTO result_events (result_id,event_type,created_at) VALUES (?,'pass',?)", (rid, storage._now_iso()))
        conn.execute("INSERT INTO muted_leads (lead_type,lead_value,muted_at) VALUES ('artist','Muted Artist',?)", (storage._now_iso(),))
        conn.commit()
    harness.providers.neighbors[root.id] = [*excluded, allowed, *near]
    harness.providers.neighbors[sources[0].id] = [*excluded, *harness.providers.neighbors[sources[0].id]]
    step = start(harness, [point(root)], near_limit=4, further_limit=6)
    finished = advance(harness, step)
    ids = {pick["cosine_id"] for pick in finished["picks"]}
    assert allowed.id in ids and not {item.id for item in excluded} & ids
    assert finished["picks"][0]["cosine_id"] == allowed.id

    original = track("missing-original", "Fallback Artist", "Missing", video="original-video")
    stand_in = track("stand-in", original.artist, "Indexed")
    harness.providers.searches[original.artist] = [stand_in]
    harness.providers.sources[stand_in.id] = stand_in
    harness.providers.neighbors[stand_in.id] = [original,
        track("original-key", original.artist, original.track),
        track("original-video-alias", "Alias", video=original.video_id), stand_in,
        track("stand-in-key", stand_in.artist, stand_in.track), track("discovery", original.artist, "Unheard")]
    step = start(harness, [point(original)], further=False, near_limit=5)
    saved = snapshot(harness, step)
    assert saved["step"]["origins"][0]["resolution"] == "substituted"
    assert [pick["cosine_id"] for pick in saved["picks"]] == ["discovery"]


def test_unknown_artists_have_distinct_recording_buckets_and_ties_use_keys(harness):
    root = track("root")
    a, b = track("z-id", "", "Alpha", score=None), track("a-id", "", "Beta", score=None)
    other = track("other-root")
    p = harness.providers
    p.sources.update({root.id: root, other.id: other})
    p.neighbors[root.id], p.neighbors[other.id] = [b, a], [a, b]
    step = start(harness, [point(root), point(other)], further=False)
    picks = snapshot(harness, step)["picks"]
    assert [pick["cosine_id"] for pick in picks] == ["z-id", "a-id"]
    assert picks[0]["score"] == picks[1]["score"] == 1.0


def test_person_branches_preserve_all_roles_parents_nullable_attribution_and_ready_facts(harness):
    p = harness.providers
    a, b = track("root-a"), track("root-b")
    n1, n2 = track("near-a", "Alpha", release=1), track("near-b", "Beta", release=2)
    p.sources.update({a.id: a, b.id: b})
    p.neighbors[a.id] = [n1.model_copy(update={"score": 0.70}), n2.model_copy(update={"score": 0.2})]
    p.neighbors[b.id] = [n2.model_copy(update={"score": 0.81}), n1.model_copy(update={"score": None})]
    for i, item in enumerate((n1, n2), 1):
        p.releases[f"/releases/{i}"] = release_payload(i, item, credits=[
            {"id": 9, "name": "Credited Person", "role": "Artwork, Photography"},
            {"id": i+20, "name": item.artist, "role": "Written-By"}],
            artists=[{"id": i+20, "name": item.artist}])
    source = track("credit-seed", "Credit Band", "Credit Recording")
    p.sources[source.id] = source
    p.searches["Credit Band Credit Album"] = [source]
    p.releases["/artists/9/releases"] = {"releases": [
        {"id": 90, "artist": "Credited Person", "title": "Main Album", "year": 2026, "role": "Main", "type": "release"},
        {"id": 91, "artist": source.artist, "title": "Credit Album", "year": 2025, "role": "Appearance", "type": "release"}],
        "pagination": {"pages": 1}}
    p.releases["/releases/91"] = release_payload(91, source,
        credits=[{"id": 9, "name": "Credited Person", "role": "Artwork, Photography"}],
        artists=[{"id": 50, "name": source.artist}])
    p.neighbors[source.id] = [track("further", "Further Artist", score=0.76)]
    missing = Point(point_id="missing", kind="track", name="Nobody - Missing", artist="Nobody", title="Missing")
    step = start(harness, [point(a), point(b), missing], branch_limit=1)
    for rank in (1, 2):
        assert harness.http.post(f"/facts/{step}/{rank}").status_code == 200
    prefix = snapshot(harness, step)["picks"]
    before = len([call for call in p.calls if call[0] == "discogs" and call[1] in {"/releases/1", "/releases/2"}])
    finished = advance(harness, step)
    assert finished["picks"][:2] == prefix
    assert len([call for call in p.calls if call[0] == "discogs" and call[1] in {"/releases/1", "/releases/2"}]) == before
    further = finished["picks"][2]
    route = further["route"]
    assert route["branch"] == {"kind": "person", "name": "Credited Person", "discogs_id": "9",
                                "roles": ["Artwork", "Photography"]}
    assert route["parent_via"] == "Alpha - Recording near-a"
    assert route["parent_pick_ranks"] == [1, 2]
    assert route["parent_pick_keys"] == [pick["track_key"] for pick in prefix]
    assert route["seed_scores"] == {point(a).point_id: 0.70, point(b).point_id: 0.81, "missing": None}
    assert route["nearest_seed"] == point(b).point_id and route["seed_count"] == 3
    assert route["close_seed_ids"] == [point(a).point_id, point(b).point_id]
    assert route["attribution"] == "via_parent" and route["branch_seed_scores"] == {source.id: 0.76}
    assert route["why"] == "one step further: sounds like records Credited Person is credited on"
    assert further["facts"] is None and finished["hubs"][0]["discogs_id"] == "9"
    assert not any(path in {"/releases/90", "/artists/21/releases", "/artists/22/releases"} for _, path, _ in p.calls)


@pytest.mark.parametrize("long_titles", [False, True])
def test_further_searches_bound_queries_and_skip_bad_requests(harness, long_titles):
    root, _, sources = label_scenario(harness, near_count=1, further_count=1)
    p = harness.providers
    source = sources[0]
    album = "Catalogue " + ("Long Title " * 25 if long_titles else "Album")
    bad_title = "Rejected " + ("Long Recording " * 25 if long_titles else "Recording")
    p.releases["/labels/42/releases"]["releases"][0]["title"] = album
    p.releases["/releases/142"]["tracklist"].insert(0, {"title": bad_title})
    bad_query = f"{source.artist} {bad_title}"[:200]
    p.failures[bad_query] = 400
    step = start(harness, [point(root)], branch_limit=1)
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    assert finished["step"]["status"] == "done"
    assert [stable_pick(pick) for pick in finished["picks"][:1]] == [stable_pick(pick) for pick in prefix]
    assert [pick["cosine_id"] for pick in finished["picks"]] == [prefix[0]["cosine_id"], "f42-0"]
    queries = [params["q"] for provider, path, params in p.calls if provider == "cosine" and path == "/search"]
    assert bad_query in queries and all(len(query) <= 200 for query in queries)


@pytest.mark.parametrize("failure", [429])
def test_further_recording_search_outage_keeps_prefix_and_fails(harness, failure):
    root, _, sources = label_scenario(harness, near_count=1)
    p = harness.providers
    source = sources[0]
    p.releases["/labels/42/releases"]["releases"][0]["title"] = "Catalogue Album"
    p.releases["/releases/142"]["tracklist"].insert(0, {"title": "Unavailable Recording"})
    p.failures[f"{source.artist} Unavailable Recording"] = failure
    step = start(harness, [point(root)], branch_limit=1)
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    assert finished["step"]["status"] == "failed"
    assert [stable_pick(pick) for pick in finished["picks"]] == [stable_pick(pick) for pick in prefix]
    with closing(storage.connect(harness.path)) as conn:
        events = storage.slice_events(conn, harness.runs[step].identity.job_id)
    assert events[-1]["event"] == "error" and not any(event["event"] == "picks_done" for event in events)


def test_hop1_cosine_failure_preserves_committed_prefix_and_ready_facts(harness):
    root, _, sources = label_scenario(harness, labels=(42, 43))
    step = start(harness, [point(root)], branch_limit=2)
    assert harness.http.post(f"/facts/{step}/1").status_code == 200
    prefix = snapshot(harness, step)["picks"]
    harness.providers.failures[f"/tracks/{sources[1].id}/similar"] = 503
    finished = advance(harness, step)
    assert finished["step"]["status"] == "failed"
    assert len(finished["picks"]) == len(prefix)
    assert [stable_pick(pick) for pick in finished["picks"]] == [stable_pick(pick) for pick in prefix]
    assert finished["picks"][0]["facts"] == prefix[0]["facts"]
    assert finished["picks"][0]["facts_state"] == "ready"
    assert all(pick["facts_state"] != "loading" for pick in finished["picks"])
    with closing(storage.connect(harness.path)) as conn:
        events = storage.slice_events(conn, harness.runs[step].identity.job_id)
    assert events[-1]["event"] == "error" and not any(event["event"] == "picks_done" for event in events)
    assert any(path == f"/tracks/{sources[0].id}/similar" for _, path, _ in harness.providers.calls)


@pytest.mark.parametrize("other_branch_resolves", [False, True])
def test_unresolved_branches_emit_only_further_warning(harness, other_branch_resolves):
    root, _, sources = label_scenario(harness, labels=(42, 43), further_count=1)
    p = harness.providers
    p.searches.pop(f"{sources[1].artist} {sources[1].track}")
    if not other_branch_resolves:
        p.searches.clear()
    step = start(harness, [point(root)], branch_limit=2)
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    assert finished["step"]["status"] == "done"
    assert finished["step"]["origins"][0]["resolution"] == "exact"
    assert [stable_pick(pick) for pick in finished["picks"][:len(prefix)]] == [stable_pick(pick) for pick in prefix]
    assert len(finished["picks"]) == len(prefix) + int(other_branch_resolves)
    assert finished["step"]["note"] == ([] if other_branch_resolves else [chain._further_note("missing")])
    assert finished["batches"][-1]["count"] == int(other_branch_resolves)


@pytest.mark.parametrize("problem,code", [("no_token", "S-INS-FACTS-NOTOKEN")])
def test_discogs_expansion_problem_warns_and_finishes_with_near(harness, problem, code):
    root, _, _ = label_scenario(harness)
    if problem == "budget":
        while harness.tracker.remaining:
            harness.tracker.spend()
    elif problem == "unavailable":
        harness.providers.failures.update({f"/releases/{i}": 503 for i in (1, 2, 3)})
    elif problem == "missing":
        harness.providers.releases.clear()
    step = start(harness, [point(root)], discogs=problem != "no_token")
    prefix = snapshot(harness, step)["picks"]
    finished = advance(harness, step)
    assert finished["step"]["status"] == "done" and len(finished["picks"]) == 3
    assert [stable_pick(pick) for pick in finished["picks"]] == [stable_pick(pick) for pick in prefix]
    assert any(note["code"] == code and note["phase"] == "further" for note in finished["step"]["note"])
    assert finished["batches"][-1] == {"hop": 1, "start_rank": 4, "count": 0, "label": "further"}
    if problem in {"no_token", "budget"}:
        assert not any(provider == "discogs" for provider, _, _ in harness.providers.calls)


def test_cancellation_after_near_does_not_start_further_requests(harness):
    root, _, _ = label_scenario(harness)
    step = start(harness, [point(root)])
    run = harness.runs[step]
    run.stopped = True
    assert list(run.iterator) == []
    assert not any(provider == "discogs" for provider, _, _ in harness.providers.calls)
    assert len(snapshot(harness, step)["picks"]) == 3


def test_initial_cosine_failure_is_an_error_and_not_an_empty_batch(harness):
    root, _, _ = label_scenario(harness)
    resolution = seeds.resolve_move_origins([point(root)], "seed", client=harness.cosine, discogs_client=harness.discogs)
    harness.providers.failures["/tracks/root/similar"] = 503
    with closing(storage.connect(harness.path)) as conn:
        exclusions = load_exclusions(conn, build_feed_profile(conn), scope="slice")
    iterator = chain.iter_move_batches(resolution, harness.cosine, discogs_client=harness.discogs,
                                        exclusions=exclusions, further=True)
    with pytest.raises(cosine_http.CosineHTTPError):
        next(iterator)


def test_concurrent_clients_share_one_atomic_budget_and_queue_slots(monkeypatch):
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    calls, lock, barrier = [], Lock(), Barrier(8)

    def request(req):
        with lock:
            calls.append(str(req.url))
        return httpx.Response(200, json={})

    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(request))
    tracker = chain.BudgetTracker(3)
    queue = discogs.FactsQueue()

    def get(number):
        client, shared = chain.make_rate_limited_client(budget_tracker=tracker,
            request_slot=lambda: queue.request_slot(priority="facts", rank=number+1, step_created_at=1))
        assert shared is tracker
        with client:
            barrier.wait(timeout=3)
            try:
                return client.get(f"https://api.discogs.com/releases/{number}").status_code
            except chain.BudgetExhausted:
                return "exhausted"

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(get, range(8)))
        assert results.count(200) == len(calls) == 3 and results.count("exhausted") == 5
        assert tracker.remaining == 0
        with queue.request_slot(priority="foreground"):
            pass  # Exhausted requests released their slots.
    finally:
        queue.close()


@pytest.mark.parametrize("budget", [1])
@pytest.mark.parametrize("shared_cache", [False])
def test_concurrent_discogs_misses_share_cache_before_budget_spend(tmp_path, monkeypatch, budget, shared_cache):
    calls, first_reads, slots, paced = [], [], [], []
    barrier, lock, second_entered = Barrier(2), Lock(), Event()
    tracker, queue = chain.BudgetTracker(budget), discogs.FactsQueue()
    url = "https://api.discogs.com/releases/123"
    payload = {"id": 123, "title": "Cached release"}
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: paced.append(backoff))

    def simultaneous_get(original_get):
        def get(key):
            value = original_get(key)
            with lock:
                initial_miss = len(first_reads) < 2
                if initial_miss:
                    first_reads.append(value)
            if initial_miss:
                assert value is None
                barrier.wait(timeout=5)
            return value
        return get

    @contextmanager
    def slot():
        with queue.request_slot(priority="foreground"):
            number = len(slots)
            slots.append(number)
            if number == 1:
                second_entered.set()
            yield
        if number == 0:
            # Force the waiter into the slot before the first caller can
            # publish a response outside it. This also covers streamed bodies.
            assert second_entered.wait(5)

    def request(req):
        calls.append(str(req.url))
        return httpx.Response(200, stream=httpx.ByteStream(storage.canonical_json(payload).encode()))

    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(request))
    with ExitStack() as stack:
        stack.callback(queue.close)
        cache = DbHttpCache(tmp_path / "cache.db")
        stack.callback(cache.close)
        other_cache = cache if shared_cache else DbHttpCache(tmp_path / "cache.db")
        if other_cache is not cache:
            stack.callback(other_cache.close)
        for item in dict.fromkeys((cache, other_cache)):
            monkeypatch.setattr(item, "get", simultaneous_get(item.get))
        clients = []
        for item in (cache, other_cache):
            client, shared = chain.make_rate_limited_client(http_cache=item, budget_tracker=tracker,
                                                            request_slot=slot)
            assert shared is tracker
            clients.append(stack.enter_context(client))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda client: client.get(url).json(), clients))
        assert results == [payload, payload]
        assert calls == [url] and tracker.remaining == budget - 1
        assert paced == [0] and slots == [0, 1]
        assert clients[0].get(url).json() == payload
        assert calls == [url] and tracker.remaining == budget - 1 and slots == [0, 1]
        with closing(storage.connect(tmp_path / "cache.db")) as conn:
            assert storage.cache_get(conn, "discogs:" + url) == storage.canonical_json(payload)


def test_chain_dig_keeps_legacy_results_and_progress(harness):
    root, _, sources = label_scenario(harness, further_count=1)
    events = []
    results = chain.chain_dig([root.id], harness.cosine, branches=1, limit_per_seed=50,
        max_discogs_calls=40, http_cache=harness.cache, discogs_token="fixture-token",
        on_progress=lambda event, data: events.append((event, data)))
    assert [result.track.id for result in results] == ["n01", "n02", "n03", "f42-0"]
    assert [result.hop for result in results] == [0, 0, 0, 1]
    assert results[0].final_score == 1.0 and results[-1].final_score == 0.850003
    assert results[-1].via == "label: Label 42" and results[-1].provenance_chain == ["hop1:label: Label 42"]
    assert [(event, data) for event, data in events if event != "branches_discovered"] == [
        ("hop_start", {"hop": 0, "seed_count": 1}), ("hop_done", {"hop": 0, "result_count": 3}),
        ("hop_start", {"hop": 1, "result_count": 3}),
        ("branch_start", {"hop": 1, "branch": "Label 42", "branch_type": "label"}),
        ("branch_done", {"hop": 1, "branch": "Label 42", "branch_type": "label", "result_count": 1}),
        ("hop_done", {"hop": 1, "result_count": 1}), ("merging", {"hop_count": 2}),
        ("done", {"total_results": 4})]
    discovered = next(data["branches"] for event, data in events if event == "branches_discovered")
    assert len(discovered) == 1 and discovered[0] == chain.Branch("label", "Label 42", "42", 0.77,
                                                                "Artist n03 - Recording n03")
    assert any(path == f"/tracks/{sources[0].id}/similar" for _, path, _ in harness.providers.calls)
