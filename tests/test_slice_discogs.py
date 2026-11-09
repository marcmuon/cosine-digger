"""GUI facts through real parsing, transport/cache, queue and SQLite repositories.

S4 owns transport injection. ordered_factory bridges that one hook at _send,
without replacing the existing transport's cache, budget, pace or retry logic.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from cosine_digger import api, chain, discogs, seeds, storage
from cosine_digger.client import BASE_URL, CosineClient
from cosine_digger.config import AppConfig
from cosine_digger.httpcache import DbHttpCache
from cosine_digger.models import Track
from cosine_digger.playlists import Exclusions
from cosine_digger.slice_types import Point, StepPick, build_hubs
from cosine_digger.web.app import create_app
from tests.test_slice_origins import harness, point  # noqa: F401


def track(number=1, *, artist="Main Artist", title="Recording", link=True):
    return Track(id=f"c{number}", name=title, artist=artist, track=title,
                 external_link=f"https://www.discogs.com/release/{number}" if link else None)


def release(number=1, *, artist="Main Artist", title="Recording"):
    return {"id": number, "title": "Album", "year": 2024,
            "artists": [{"id": 6, "name": artist}],
            "labels": [{"id": 42, "name": "Label A"}], "styles": ["Techno", "Ambient"],
            "extraartists": [{"id": 9, "name": "Shared Person", "role": "Mixed By, Producer"}],
            "tracklist": [{"position": "A1", "type_": "track", "title": title}],
            "thumb": "https://i.discogs.com/fixture.jpg"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.delenv("DISCOGS_USER_AGENT", raising=False)


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 100.0

        def __init__(self):
            self.sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    value = Clock()
    monkeypatch.setattr(discogs.time, "monotonic", lambda: value.now)
    monkeypatch.setattr(discogs.time, "sleep", value.sleep)
    return value


@pytest.fixture
def queue():
    value = discogs.FactsQueue()
    try:
        yield value
    finally:
        value.close()


def network(monkeypatch, handler):
    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(handler))


def ordered_factory(queue, tracker, rank, created_at, *, cache_path=None, clients=None, priority="facts"):
    @contextmanager
    def factory():
        cache = DbHttpCache(cache_path) if cache_path is not None else None
        client, _ = chain.make_rate_limited_client(http_cache=cache, token="fixture-token")
        client._transport._budget_tracker = tracker
        send = client._transport._send

        def ordered_send(request, backoff=0.0):
            with queue.request_slot(priority=priority, rank=rank, step_created_at=created_at):
                return send(request, backoff=backoff)

        client._transport._send = ordered_send
        if clients is not None:
            clients.append((threading.get_ident(), client))
        try:
            yield client
        finally:
            client.close()
            if cache is not None:
                cache.close()

    return factory


def submit(queue, rank, callback, factory, *, job="job", step="step", created=1, item=None):
    queue.submit(job_id=job, step_id=step, rank=rank, step_created_at=created,
                 track=item or track(rank), client_factory=factory, on_result=callback)


def wait_for_slots(queue, count):
    # Synchronize admission, then assert public request/result traces below.
    with queue._condition:
        assert queue._condition.wait_for(lambda: len(queue._waiters) >= count, timeout=3)


@pytest.fixture
def facts_app(harness, monkeypatch):
    monkeypatch.setattr(discogs, "pace", lambda *_: None)
    providers = harness.providers

    def cosine_factory():
        client = CosineClient.__new__(CosineClient)
        client._client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(providers.cosine))
        return client

    def discogs_factory(*, budget, http_cache, token, budget_tracker, request_slot):
        transport = chain._RetryOn429Transport(wrapped=httpx.MockTransport(providers.discogs_response),
            budget_tracker=budget_tracker, http_cache=http_cache, token=token, request_slot=request_slot)
        return httpx.Client(transport=transport), budget_tracker

    cfg = AppConfig(home=harness.db_path.parent, db_path=harness.db_path,
                    cosine_token="fixture-cosine", discogs_token="fixture-discogs")
    app = create_app(cfg, scheduler=False, client_factory=cosine_factory, discogs_client_factory=discogs_factory)
    with TestClient(app, base_url="http://localhost") as http:
        yield http, providers, app.state.slice_runtime


def await_snapshot(http, step_id, predicate=lambda snapshot: snapshot["job"]["state"] != "running"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = http.get(f"/api/steps/{step_id}").json()
        if predicate(snapshot):
            return snapshot
        threading.Event().wait(0.01)
    pytest.fail("Step did not reach the expected state")


def test_real_release_roles_scopes_and_exact_recording():
    payload = release()
    payload["labels"].append({"id": 43, "name": "Other Label"})
    payload["extraartists"] += [
        {"id": 9, "name": "Shared Person (2)", "role": "Artwork [Cover, Insert], Photography"},
        {"id": 6, "name": "Main Artist (2)", "role": "Written-By"},
        {"id": 10, "name": "Track Person", "role": "Bass", "tracks": "A1 to A3"},
        {"id": 11, "name": "Other Recording", "role": "Vocals", "tracks": "B1"},
    ]
    payload["extraartists"] += [{"id": i, "name": f"Player {i}", "role": "Performer"}
                                for i in range(20, 32)]
    payload["tracklist"][0]["extraartists"] = [
        {"id": 9, "name": "Shared Person", "role": "Remix"},
        {"id": 9, "name": "Shared Person", "role": "Guitar, Remix"},
    ]
    payload["tracklist"].append({"title": "Other", "position": "B1", "type_": "track",
                                 "extraartists": [{"id": 99, "name": "Wrong Person", "role": "Producer"}]})
    facts = discogs.parse_release_facts(payload, release_url="https://discogs.com/release/1-Album", track=track())
    assert facts.release_id == "1"
    assert facts.release_url == "https://www.discogs.com/release/1"
    assert [label.id for label in facts.labels] == ["42", "43"]
    assert facts.year == 2024 and facts.styles == ["Techno", "Ambient"]
    assert facts.thumb == "https://i.discogs.com/fixture.jpg"
    shared = [credit.to_dict() for credit in facts.credits if credit.id == "9"]
    assert shared == [
        {"id": "9", "name": "Shared Person", "roles": ["Mixed By", "Producer", "Artwork [Cover, Insert]", "Photography"], "scope": "release"},
        {"id": "9", "name": "Shared Person", "roles": ["Remix", "Guitar"], "scope": "track"},
    ]
    assert next(credit for credit in facts.credits if credit.id == "10").scope == "track"
    assert {credit.id for credit in facts.credits} == {"6", "9", "10", *map(str, range(20, 32))}


def test_compilation_and_nested_track_credits_use_exact_artist_title():
    payload = release(artist="Various")
    payload["tracklist"] = [
        {"type_": "index", "title": "Suite", "artists": [{"id": 7, "name": "Main Artist (2)"}],
         "sub_tracks": [{"type_": "track", "position": "1.1", "title": "  RECORDING  ",
                         "extraartists": [{"id": 8, "name": "Nested Person", "role": "Synthesizer"}]}]},
        {"type_": "track", "title": "Recording", "artists": [{"id": 12, "name": "Other Artist"}],
         "extraartists": [{"id": 13, "name": "Wrong Artist's Person", "role": "Vocals"}]},
        {"type_": "track", "title": "Recording (Remix)", "artists": [{"id": 7, "name": "Main Artist"}],
         "extraartists": [{"id": 14, "name": "Wrong Version's Person", "role": "Remix"}]},
    ]
    facts = discogs.parse_release_facts(payload, release_url="https://www.discogs.com/release/1", track=track())
    assert [artist.to_dict() for artist in facts.artists] == [{"id": "7", "name": "Main Artist"}]
    assert {credit.id for credit in facts.credits} == {"9", "8"}


@pytest.mark.parametrize("recording,payload_title,expected", [
    ("Song (Original Mix)", "Song", ["c1"]),
    ("Song (Album Version)", "Song", ["c1"]),
    ("Song", "Song (Original Mix)", ["c1"]),
    ("Song", "Song (Album Version)", ["c1"]),
    ("Song (Remix)", "Song", []),
    ("Song", "Song (Edit)", []),
])
def test_recording_suffix_preserves_facts_and_person_origins(harness, recording, payload_title, expected):
    item = track(artist="Artist", title=recording)
    payload = release(artist="Artist", title=payload_title)
    payload["extraartists"] = []
    payload["tracklist"][0]["extraartists"] = [{"id": 9, "name": "Person", "role": "Vocals"}]
    harness.providers.discogs.update({
        "/releases/1": payload,
        "/artists/9/releases": {"releases": [
            {"id": 1, "artist": "Artist", "title": "Album", "role": "Appearance", "type": "release"},
        ]},
    })
    harness.providers.lookups[item.external_link] = [item]
    facts = discogs.fetch_track_facts(item, client=harness.discogs)
    assert [credit.id for credit in facts.credits] == (["9"] if expected else [])
    person = Point(point_id="person:9", kind="person", name="Person", discogs_id="9")
    result = seeds.resolve_move_origins([person], "person", client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert [item.id for item in result.resolved_tracks] == expected


@pytest.mark.parametrize("position,selector,expected", [
    ("1", "1-2", False),
    ("1-2", "1-2", True),
    ("2", "1-2", False),
    ("2", "X1, 1-2", False),
    ("2", "1-2, 2", True),
    ("2", "1 to 2", True),
])
def test_single_compound_position_is_not_a_numeric_range(harness, position, selector, expected):
    p = harness.providers
    titles = {"1": "Opening", "1-2": "Credited Song", "2": "Uncredited Song"}
    item = track(11, artist="Artist", title=titles[position])
    p.discogs["/releases/11"] = {"id": 11, "artists": [{"id": 7, "name": "Artist"}],
        "tracklist": [{"position": number, "title": title} for number, title in titles.items()],
        "extraartists": [{"id": 9, "name": "Person", "role": "Producer", "tracks": selector}]}
    p.discogs["/artists/9/releases"] = {"releases": [
        {"id": 11, "artist": "Artist", "title": "Album", "role": "Producer", "type": "release"}]}
    p.lookups[item.external_link] = [item]
    facts = discogs.fetch_track_facts(item, client=harness.discogs)
    person = Point(point_id="person:9", kind="person", name="Person", discogs_id="9")
    result = seeds.resolve_move_origins([person], "person", client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert [credit.id for credit in facts.credits] == (["9"] if expected else [])
    assert [recording.id for recording in result.resolved_tracks] == ([item.id] if expected else [])
    if expected:
        assert facts.credits[0].scope == "track" and facts.credits[0].roles == ["Producer"]


@pytest.mark.parametrize("selector,positions,expected", [
    ("1-1 to 1-3", ["1-1", "1-2", "1-3"], True),
    ("1.1 to 1.3", ["1.1", "1.2", "1.3"], True),
    ("1-1 to 2-1", ["1-1", "1-2", "2-1"], True),
    ("1-1 to 1.3", ["1-1", "1-2", "1.3"], True),
    ("1.1 to 1.3", ["1.1", "1.2", "1.3", "1.4"], True),
    ("1-3 to 1-1", ["1-1", "1-2", "1-3"], False),
    ("9-1 to 9-3", ["1-1", "1-2", "1-3"], False),
    ("1-2", ["1-1", "1-2", "1-3"], True),
    ("A1 to A3", ["A1", "A2", "A3"], True),
    ("1.2 to 1.3", ["1.0", "1.1", "1.2", "1.3"], False),
    ("X1, 1.1 to 1.3", ["1.1", "1.2", "1.3"], True),
])
@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
def test_ordered_credit_ranges_preserve_person_origins(harness, selector, positions, expected, nested):
    item = track(artist="Artist", title="Recording")
    payload = release(artist="Artist")
    payload["tracklist"] = [{"position": position, "title": "Recording" if index == 1 else f"Other {index}"}
                            for index, position in enumerate(positions)]
    if nested:
        payload["tracklist"] = [{"type_": "index", "title": "Suite", "sub_tracks": payload["tracklist"]}]
    payload["extraartists"] = [{"id": 9, "name": "Person", "role": "Producer", "tracks": selector}]
    harness.providers.discogs.update({
        "/releases/1": payload,
        "/artists/9/releases": {"releases": [
            {"id": 1, "artist": "Artist", "title": "Album", "role": "Producer", "type": "release"},
        ]},
    })
    harness.providers.lookups[item.external_link] = [item]
    facts = discogs.fetch_track_facts(item, client=harness.discogs)
    assert [credit.id for credit in facts.credits] == (["9"] if expected else [])
    person = Point(point_id="person:9", kind="person", name="Person", discogs_id="9")
    resolved = seeds.resolve_move_origins([person], "person", client=harness.cosine,
                                          discogs_client=harness.discogs)
    assert [recording.id for recording in resolved.resolved_tracks] == (["c1"] if expected else [])


@pytest.mark.parametrize("selector,position,expected", [
    ("1-1 to 1-3", "1-2", True),
    ("1.1 to 1.3", "1.2", True),
    ("1-1 to 2-3", "1-2", False),
    ("1-1 to 1.3", "1-2", False),
    ("1-1 to 1-3", "1.2", False),
    ("1-1 to 1-3", "2-2", False),
    ("1.1 to 1.3", "1.4", False),
    ("1-3 to 1-1", "1-2", False),
])
def test_original_compound_credit_range_repro(harness, selector, position, expected):
    item = track(10, artist="Artist")
    payload = release(10, artist="Artist")
    payload["tracklist"][0]["position"] = position
    payload["extraartists"] = [{"id": 9, "name": "Producer", "role": "Producer", "tracks": selector}]
    p = harness.providers
    p.discogs.update({
        "/releases/10": payload,
        "/artists/9/releases": {"releases": [
            {"id": 10, "title": "Album", "artist": "Artist", "year": 2026,
             "role": "Producer", "type": "release"},
        ]},
    })
    p.lookups[item.external_link] = [item]
    p.searches["Artist Album"] = [item]
    p.searches["Artist - Recording"] = [item]
    facts = discogs.fetch_track_facts(item, client=harness.discogs)
    person = Point(point_id="person:9", kind="person", name="Producer", discogs_id="9")
    result = seeds.resolve_move_origins([person], "person", client=harness.cosine,
                                        discogs_client=harness.discogs)
    assert ([credit.id for credit in facts.credits], [recording.id for recording in result.resolved_tracks]) == (
        (["9"], [item.id]) if expected else ([], []))
    if expected:
        assert facts.credits[0].scope == "track"


@pytest.mark.parametrize("field,bad", [
    ("images", [None]),
    ("labels", [None]),
    ("extraartists", [None]),
])
def test_malformed_facts_warn_in_further_and_settle_unavailable(harness, queue, field, bad):
    source = track(100, artist="Seed", title="Source", link=False)
    near = track().model_copy(update={"score": 0.9})
    payload = release() | {field: bad}
    harness.providers.sources[source.id] = source
    harness.providers.neighbors[source.id] = [near]
    harness.providers.discogs["/releases/1"] = payload
    with pytest.raises(discogs.DiscogsResponseError):
        discogs.fetch_track_facts(near, client=harness.discogs)
    events = list(api.run_move([point(source)], "seed", True, client=harness.cosine,
                              discogs_client=harness.discogs, exclusions=Exclusions()))
    assert [event.data["cosine_id"] for event in events if event.event == "pick"] == [near.id]
    assert any(event.data.get("code") == "S-MAP-DISCOGS-DOWN" for event in events)
    results = []

    @contextmanager
    def factory():
        with httpx.Client(transport=httpx.MockTransport(harness.providers.discogs_response)) as client:
            yield client

    submit(queue, 1, results.append, factory, item=near)
    queue.settle("job")
    assert len(results) == 1 and results[0]["facts_state"] == "unavailable"


@pytest.mark.parametrize("field,bad", [("images", [None])])
def test_malformed_facts_preserve_near_and_finish_step(facts_app, field, bad):
    http, providers, runtime = facts_app
    bootstrap = track(101, artist="Bootstrap", title="Track", link=False)
    source = track(100, artist="Seed", title="Source", link=False)
    near = track().model_copy(update={"score": 0.9})
    providers.sources.update({bootstrap.id: bootstrap, source.id: source})
    providers.neighbors.update({bootstrap.id: [source], source.id: [near]})
    providers.discogs["/releases/1"] = release() | {field: bad}
    root = http.post("/api/digs", json={"seeds": [point(bootstrap).to_dict()]})
    assert root.status_code == 200, root.text
    initial = root.json()
    assert await_snapshot(http, initial["step_id"])["job"]["state"] == "done"
    origin = point(source, source_step_id=initial["step_id"], source_rank=1)
    response = http.post("/api/steps", json={"parent_step_id": initial["step_id"],
        "origins": [origin.to_dict()], "move": "track", "further": True})
    assert response.status_code == 200, response.text
    result = response.json()
    saved = await_snapshot(http, result["step_id"])
    assert saved["job"]["state"] == "done"
    assert [(pick["cosine_id"], pick["facts_state"]) for pick in saved["snapshot"]["picks"]] == [(near.id, "unavailable")]
    with closing(storage.connect(runtime.get_config().db_path)) as conn:
        events = storage.slice_events(conn, result["job_id"])
    assert any(event["data"].get("code") == "S-MAP-DISCOGS-DOWN" for event in events)
    assert events[-1]["event"] == "done"


@pytest.mark.parametrize("unsafe", ["javascript:alert(1)", "https://i.discogs.com.evil.test/a.jpg",
                                   "https://i.discogs.com@evil.test/a.jpg", "https://i.discogs.com:999/a.jpg",
                                   "https://i.discogs.com/a\n.jpg", "//i.discogs.com/a.jpg"])
def test_thumb_urls_are_safe_and_primary_image_fallback_is_verified(unsafe):
    payload = release()
    payload.update(thumb=unsafe, year=True, images=[
        {"type": "secondary", "uri150": "https://i.discogs.com/secondary.jpg"},
        {"type": "primary", "uri150": "https://i.discogs.com/primary.jpg"},
    ])
    facts = discogs.parse_release_facts(payload, release_url="https://discogs.com/release/1?view=test#info", track=track())
    assert facts.thumb == "https://i.discogs.com/primary.jpg" and facts.year is None
    assert facts.release_url == "https://www.discogs.com/release/1"
    payload.pop("images")
    assert discogs.parse_release_facts(payload, release_url=facts.release_url, track=track()).thumb is None


@pytest.mark.parametrize("url", ["https://discogs.com.evil.test/release/1", "https://evil.test/release/1",
                                "file:///release/1", "https://user:password@www.discogs.com/release/1"])
def test_parser_refuses_untrusted_release_urls(url):
    with pytest.raises(discogs.DiscogsResponseError):
        discogs.parse_release_facts(release(), release_url=url, track=track())


def test_parser_refuses_payload_for_another_release():
    with pytest.raises(discogs.DiscogsResponseError, match="does not match"):
        discogs.parse_release_facts(release(2), release_url="https://discogs.com/release/1", track=track())


@pytest.mark.parametrize("mode,expected_paths,expected_id", [
    ("follow", ["/masters/1", "/releases/2"], "2"),
    ("no-main", ["/masters/1"], "1"),
])
def test_master_facts_follow_main_release_only_when_credits_are_missing(monkeypatch, clock, mode, expected_paths, expected_id):
    paths = []
    master = release()
    master["extraartists"] = []
    if mode != "no-main":
        master["main_release"] = 2
    if mode == "own-track-credit":
        master["tracklist"][0]["sub_tracks"] = [{"title": "Recording", "type_": "track",
            "extraartists": [{"id": 77, "name": "Master Person", "role": "Photography"}]}]

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/masters/1":
            return httpx.Response(200, json=master)
        return httpx.Response(404 if mode == "missing-main" else 200, json=release(2))

    network(monkeypatch, respond)
    client, tracker = chain.make_rate_limited_client(budget=2, token="fixture-token")
    item = track().model_copy(update={"external_link": "https://www.discogs.com/master/1"})
    with client:
        facts = discogs.fetch_track_facts(item, client=client)
    assert paths == expected_paths and facts.release_id == expected_id
    assert tracker.remaining == 2 - len(paths)
    assert [credit.id for credit in facts.credits] == (["9"] if mode == "follow" else ["77"] if mode == "own-track-credit" else [])


@pytest.mark.parametrize("matched", [True, False])
def test_search_verifies_payload_recording_and_never_uses_first_hit(monkeypatch, clock, matched):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/database/search":
            assert request.url.params["artist"] == "Main Artist"
            assert request.url.params["track"] == "Recording"
            return httpx.Response(200, json={"results": [
                {"id": 1, "type": "release", "title": "Main Artist - Recording", "uri": "https://evil.test/a"},
                {"id": 2, "type": "release", "title": "Album"},
                {"id": 3, "type": "release", "title": "Album"},
            ]})
        number = int(request.url.path.rsplit("/", 1)[1])
        payload = release(number, artist="Wrong Artist" if number == 1 else "Main Artist",
                          title="Recording" if number == 3 and matched else "Other Recording")
        return httpx.Response(200, json=payload)

    network(monkeypatch, respond)
    client, _ = chain.make_rate_limited_client(budget=4, token="fixture-token")
    with client:
        facts = discogs.fetch_track_facts(track(link=False), client=client)
    assert paths == ["/database/search", "/releases/1", "/releases/2", "/releases/3"]
    assert (facts.release_id if facts else None) == ("3" if matched else None)


def test_shared_origin_budget_retry_pace_cache_and_exhaustion(monkeypatch, clock, queue, tmp_path):
    path = tmp_path / "cache.db"
    requests, results = [], []

    def respond(request):
        requests.append((request.url.path, clock.now))
        if request.url.path == "/releases/1" and sum(url == "/releases/1" for url, _ in requests) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json=release(int(request.url.path.split("/")[-1])))

    network(monkeypatch, respond)
    tracker = chain.BudgetTracker(4)
    # Origin and branch metadata spend the SAME job tracker before pick facts.
    for number in (100, 101):
        with ordered_factory(queue, tracker, None, None, cache_path=path, priority="foreground")() as client:
            discogs._fetch_discogs_payload("release", str(number), client=client)
    with queue.request_slot(priority="foreground"):
        for rank, number in [(1, 1), (2, 2), (3, 1)]:
            submit(queue, rank, results.append, ordered_factory(queue, tracker, rank, 1, cache_path=path), item=track(number))
    queue.seal("job")
    queue.settle("job")
    assert [result["facts_state"] for result in results] == ["ready", "unavailable", "ready"]
    assert results[1]["note"]["code"] == "discogs_budget_exhausted"
    assert [path for path, _ in requests] == ["/releases/100", "/releases/101", "/releases/1", "/releases/1"]
    assert [time for _, time in requests] == pytest.approx([100, 101.1, 102.2, 107.2])
    assert clock.sleeps == pytest.approx([1.1, 1.1, 5.0]) and tracker.remaining == 0


@pytest.mark.parametrize("status,state", [(404, "missing"), (401, "no_token")])
def test_failures_settle_without_failing_picks_or_leaking_provider_data(monkeypatch, clock, queue, status, state):
    calls, results = [], []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"message": "private-provider-detail"})

    network(monkeypatch, respond)
    submit(queue, 1, results.append, ordered_factory(queue, chain.BudgetTracker(2), 1, 1))
    queue.settle("job")
    assert len(results) == 1 and results[0]["facts_state"] == state and results[0]["facts"] is None
    assert "private-provider-detail" not in json.dumps(results)
    assert len(calls) == (2 if status == 429 else 1)


def test_no_token_and_unsafe_link_never_fetch_arbitrary_urls(monkeypatch, clock, queue):
    calls, results = [], []
    network(monkeypatch, lambda request: (calls.append(str(request.url)), httpx.Response(200, json={"results": []}))[1])
    submit(queue, 1, results.append, lambda: None)
    queue.settle("job")
    assert results == [{"rank": 1, "facts_state": "no_token", "facts": None, "note": None}] and calls == []
    item = track().model_copy(update={"external_link": "https://www.discogs.com.evil.test/release/1"})
    submit(queue, 1, results.append, ordered_factory(queue, chain.BudgetTracker(1), 1, 2), job="safe", step="safe", created=2, item=item)
    queue.settle("safe")
    assert results[-1]["facts_state"] == "missing"
    assert len(calls) == 1 and calls[0].startswith("https://api.discogs.com/database/search?")


def test_settle_generation_failure_preserves_ready_facts_and_drains_callbacks(monkeypatch, clock, queue):
    first_done, in_flight, release_gate, settled = (threading.Event() for _ in range(4))
    results, clients = [], []

    def respond(request):
        if request.url.path == "/releases/2":
            in_flight.set()
            assert release_gate.wait(3)
        return httpx.Response(200, json=release(int(request.url.path.split("/")[-1])))

    def callback(result):
        results.append(result)
        if result["rank"] == 1:
            first_done.set()

    network(monkeypatch, respond)
    tracker = chain.BudgetTracker(3)
    try:
        for rank in (1, 2, 3):
            submit(queue, rank, callback, ordered_factory(queue, tracker, rank, 1, clients=clients))
        assert first_done.wait(3) and in_flight.wait(3)
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(lambda: (queue.settle("job", unavailable_reason="generation failed"), settled.set()))
            with queue._condition:
                assert queue._condition.wait_for(lambda: queue._jobs["job"].unavailable_reason is not None, timeout=3)
            assert not settled.is_set()
            release_gate.set()
            future.result(timeout=3)
    finally:
        release_gate.set()
    assert [result["rank"] for result in results] == [1, 2, 3]
    assert [result["facts_state"] for result in results] == ["ready", "unavailable", "unavailable"]
    assert all(client.is_closed for _, client in clients)
    with pytest.raises(ValueError, match="sealed"):
        submit(queue, 4, callback, lambda: None)


def test_failed_job_facts_publish_without_waiting_for_other_job(queue):
    entered, release_gate, first_done = (threading.Event() for _ in range(3))
    results = []

    @contextmanager
    def blocked_factory():
        def respond(request):
            entered.set()
            assert release_gate.wait(5)
            return httpx.Response(200, json=release())

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            yield client

    def callback(job, result):
        results.append((job, result["rank"], result["facts_state"]))
        if job == "failed" and result["rank"] == 1:
            first_done.set()

    submit(queue, 1, lambda result: callback("failed", result), lambda: None, job="failed", step="failed")
    assert first_done.wait(3)
    submit(queue, 1, lambda result: callback("other", result), blocked_factory, job="other", step="other")
    assert entered.wait(3)
    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            for rank in (2, 3):
                submit(queue, rank, lambda result: callback("failed", result), lambda: None,
                       job="failed", step="failed")
            settled = worker.submit(queue.settle, "failed", unavailable_reason="generation_failed")
            settled.result(timeout=1)
            assert results == [("failed", 1, "no_token"), ("failed", 2, "unavailable"), ("failed", 3, "unavailable")]
        finally:
            release_gate.set()
    queue.settle("other")
    assert results[-1] == ("other", 1, "ready")


def test_failed_job_settles_while_its_worker_serves_another_job(monkeypatch, clock, queue):
    entered, gate, helping = (threading.Event() for _ in range(3))
    owners, helper_owner, results = {}, [], []
    tracker = chain.BudgetTracker(5)

    def respond(request):
        if request.url.path == "/releases/8":
            entered.set()
            assert gate.wait(5)
        return httpx.Response(200, json=release(int(request.url.path.rsplit("/", 1)[1])))

    network(monkeypatch, respond)

    def factory(rank):
        @contextmanager
        def resource():
            owners[rank] = threading.get_ident()
            with ordered_factory(queue, tracker, rank, 1)() as client:
                yield client
        return resource

    @contextmanager
    def lower_factory():
        helper_owner.append(threading.get_ident())
        helping.set()
        with ordered_factory(queue, tracker, 1, 1)() as client:
            yield client

    try:
        submit(queue, 8, lambda result: results.append((8, result)), factory(8), job="8", step="8")
        assert entered.wait(3)
        for count, rank in enumerate((7, 6, 5), 1):
            submit(queue, rank, lambda result, rank=rank: results.append((rank, result)),
                   factory(rank), job=str(rank), step=str(rank))
            wait_for_slots(queue, count)
        submit(queue, 1, lambda result: results.append((1, result)), lower_factory, job="1", step="1")
        assert helping.wait(3)
        parent = next(rank for rank, owner in owners.items() if owner == helper_owner[0])
        with ThreadPoolExecutor(max_workers=1) as worker:
            try:
                settled = worker.submit(queue.settle, str(parent), unavailable_reason="generation_failed")
                settled.result(timeout=1)
                assert [(rank, result["facts_state"]) for rank, result in results] == [(parent, "unavailable")]
            finally:
                gate.set()
    finally:
        gate.set()
    for job in ("1", "5", "6", "7", "8"):
        queue.settle(job)
    assert sum(rank == parent for rank, _ in results) == 1
    assert tracker.remaining == 1  # The cancelled parent's request never started.


def test_failed_step_terminal_does_not_wait_for_other_step_facts(facts_app):
    http, providers, runtime = facts_app
    entered, gate = threading.Event(), threading.Event()
    with closing(storage.connect(runtime.get_config().db_path)) as conn, conn:
        conn.execute("""CREATE TRIGGER fail_generation BEFORE UPDATE OF picks_done_at ON steps
                        WHEN json_extract(OLD.origins,'$[0].cosine_id') = 'c1001'
                        BEGIN SELECT RAISE(ABORT,'fixture generation failure'); END""")

    def slow_metadata(request):
        entered.set()
        assert gate.wait(5)
        return release()

    first = track(1000, artist="First Seed", title="Source", link=False)
    second = track(1001, artist="Second Seed", title="Source", link=False)
    providers.sources.update({first.id: first, second.id: second})
    providers.neighbors.update({first.id: [track().model_copy(update={"score": 0.9})],
        second.id: [track(rank, artist=f"Artist {rank}").model_copy(update={"score": 0.9}) for rank in (2, 3, 4)]})
    providers.discogs["/releases/1"] = slow_metadata
    try:
        response = http.post("/api/digs", json={"seeds": [point(first).to_dict()]})
        assert response.status_code == 200, response.text
        running = response.json()
        assert entered.wait(3)
        response = http.post("/api/digs", json={"seeds": [point(second).to_dict()]})
        assert response.status_code == 200, response.text
        failed = response.json()
        runtime._jobs[failed["job_id"]].thread.join(timeout=1)
        snapshot = http.get(f"/api/steps/{failed['step_id']}").json()
        assert snapshot["job"]["state"] == "error"
        assert [pick["facts_state"] for pick in snapshot["snapshot"]["picks"]] == ["unavailable"] * 3
        assert http.get(f"/api/jobs/{running['job_id']}").json()["state"] == "running"
        with closing(storage.connect(runtime.get_config().db_path)) as conn:
            events = storage.slice_events(conn, failed["job_id"])
        assert [event["data"]["rank"] for event in events if event["event"] == "facts"] == [1, 2, 3]
        assert events[-1]["event"] == "error"
    finally:
        gate.set()
    assert await_snapshot(http, running["step_id"])["job"]["state"] == "done"


def test_three_digs_use_bounded_facts_workers(facts_app, monkeypatch):
    http, providers, runtime = facts_app
    gate = threading.Event()
    starts = []
    original_start = threading.Thread.start

    def start_thread(worker):
        if worker.name.startswith("discogs-facts-"):
            starts.append(worker.name)
        return original_start(worker)

    monkeypatch.setattr(threading.Thread, "start", start_thread)

    def metadata(request):
        assert gate.wait(15)
        return {"id": int(request.url.path.rsplit("/", 1)[1]), "title": "Release", "tracklist": []}

    jobs = []
    try:
        for number in range(3):
            source = track(1000 + number, artist=f"Seed {number}", title="Source", link=False)
            providers.sources[source.id] = source
            providers.neighbors[source.id] = [
                track(number * 25 + rank, artist=f"Artist {number}-{rank}").model_copy(update={"score": 0.9 - rank / 1000})
                for rank in range(1, 26)
            ]
            for rank in range(1, 26):
                providers.discogs[f"/releases/{number * 25 + rank}"] = metadata
            response = http.post("/api/digs", json={"seeds": [point(source).to_dict()]})
            assert response.status_code == 200, response.text
            jobs.append(response.json())
        for job in jobs:
            snapshot = await_snapshot(http, job["step_id"],
                lambda snapshot: snapshot["snapshot"]["step"]["picks_done_at"])
            assert len(snapshot["snapshot"]["picks"]) == 25 and snapshot["job"]["state"] == "running"
        queue = runtime._facts
        with queue._condition:
            assert sum(job.pending for job in queue._jobs.values()) == 75
            assert 1 <= len(queue._workers) <= 4
        assert len(starts) <= 4
    finally:
        gate.set()
    for job in jobs:
        snapshot = await_snapshot(http, job["step_id"])
        assert snapshot["job"]["state"] == "done"
        assert all(pick["facts_state"] == "ready" for pick in snapshot["snapshot"]["picks"])
        with closing(storage.connect(runtime.get_config().db_path)) as conn:
            events = storage.slice_events(conn, job["job_id"])
        assert [event["data"]["rank"] for event in events if event["event"] == "facts"] == list(range(1, 26))
        assert events[-1]["event"] == "done"


def test_parsed_facts_persist_and_build_only_exact_shared_hubs(monkeypatch, clock, queue, tmp_path):
    path = tmp_path / "slice.db"
    origin = Point(point_id="track:seed|recording", kind="track", name="Seed - Recording",
                   artist="Seed", title="Recording", cosine_id="seed")
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            dig_id = storage.create_dig(conn, [origin])
            identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=[origin], move="seed", further=False)
            storage.append_slice_event(conn, identity.job_id, "batch", {"hop": 0, "start_rank": 1, "target_count": 25, "label": "near"}, redact=lambda data: data)
            for rank in range(1, 4):
                storage.append_slice_event(conn, identity.job_id, "pick", {"pick": {
                    "rank": rank, "track_key": f"artist {rank}|recording", "cosine_id": f"c{rank}",
                    "artist": f"Artist {rank}", "title": "Recording", "score": 0.8, "batch": 0,
                    "facts": None, "facts_state": "loading", "route": {
                        "layout_seed": origin.point_id, "move": "label", "via": "label:Route Only",
                        "parent_via": "person:Inferred Person"}}}, redact=lambda data: data)

    def respond(request):
        number = int(request.url.path.rsplit("/", 1)[1])
        payload = release(number, artist=f"Artist {number}")
        payload["artists"][0]["id"] = 100 + number
        if number == 3:
            payload["artists"].append({"id": 9, "name": "Shared Person (2)"})
        payload["labels"].append({"id": 9999, "name": "Not On Label"})
        payload["extraartists"] += [
            {"id": 100 + number, "name": f"Artist {number} (2)", "role": "Producer"},
            {"id": 999, "name": f"Artist {number}", "role": "Written-By"},
            {"id": 9, "name": "Shared Person", "role": "Photography"},
            {"id": 200 + number, "name": f"Singleton {number}", "role": "Vocals"},
            {"id": 700, "name": "Unknown", "role": "Performer"},
        ]
        return httpx.Response(200, json=payload)

    def persist(result):
        with closing(storage.connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                storage.append_slice_event(conn, identity.job_id, "facts", result, redact=lambda data: data)
                snapshot = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
                for hub in build_hubs(snapshot["picks"], snapshot["hubs"]):
                    storage.append_slice_event(conn, identity.job_id, "hub", {"hub": hub.to_dict()}, redact=lambda data: data)

    network(monkeypatch, respond)
    tracker = chain.BudgetTracker(3)
    for rank in range(1, 4):
        submit(queue, rank, persist, ordered_factory(queue, tracker, rank, 1, cache_path=path),
               job=identity.job_id, step=identity.step_id, item=track(rank, artist=f"Artist {rank}"))
    queue.settle(identity.job_id)
    with closing(storage.connect(path)) as conn:
        snapshot = storage.get_step_snapshot(conn, identity.step_id)["snapshot"]
        events = storage.slice_events(conn, identity.job_id)
    assert [pick["facts_state"] for pick in snapshot["picks"]] == ["ready"] * 3
    assert [(hub["hub_key"], hub["pick_ranks"]) for hub in snapshot["hubs"]] == [("label:42", [1, 2, 3]), ("person:9", [1, 2])]
    facts_events = [event for event in events if event["event"] == "facts"]
    assert [event["data"]["rank"] for event in facts_events] == [1, 2, 3]
    hubs_events = [event["data"]["hub"] for event in events if event["event"] == "hub"]
    for key in ("label:42", "person:9"):
        positions = [hub["layout"] for hub in hubs_events if hub["hub_key"] == key]
        assert positions[0] == positions[-1]
    picks = [StepPick.model_validate(pick).model_copy(update={"facts": None, "facts_state": "missing"})
             for pick in snapshot["picks"]]
    assert build_hubs(picks, []) == []  # Neither route nor parent_via supplies a fact.
