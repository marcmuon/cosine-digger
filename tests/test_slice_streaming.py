"""Production app and real run_move: HTTP, crashes, repositories and queue.

Only provider HTTP is fake. The original S7 lifecycle and replay assertions
remain, with production resolution notes and further-request accounting.
"""

import json
import logging
import multiprocessing
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cosine_digger import chain, discogs, storage
import cosine_digger.client as client_module
from cosine_digger.api import _key
from cosine_digger.client import BASE_URL, CosineClient, CosineRequestError
from cosine_digger.config import AppConfig
from cosine_digger.locks import LockTimeout
from cosine_digger.slice_types import Facts, Point
from cosine_digger.web import slice_jobs
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_jobs import SliceRuntime
from cosine_digger.web.slice_library import run_label_enrichment
from tests.ports import bind_loopback
from tests.test_slice_chain import Providers as MoveProviders, point, release_payload, track
from tests.test_slice_storage import batch


def eventually(check, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not arrive before deadline")


def seed():
    return Point(point_id="track:seed|track", kind="track", name="Seed - Track",
                 artist="Seed", title="Track", track_key="seed|track", cosine_id="seed", indexed=True)


def facts(rank):
    return Facts(release_id=str(rank), release_url=f"https://www.discogs.com/release/{rank}",
                 labels=[{"id": "42", "name": "Shared Label"}],
                 artists=[{"id": str(rank + 100), "name": f"Artist {rank}"}],
                 credits=[{"id": "9", "name": "Shared Person", "roles": ["Mixed By"], "scope": "release"}]).to_dict()


class FakeCosine(CosineClient):
    def __init__(self, calls=None, engine=None):
        self.thread = threading.get_ident()
        self.calls = calls if calls is not None else []
        self.calls.append(("open", self.thread))
        self.engine = engine or Engine(further=False)
        self._client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(self.engine.cosine))

    def close(self):
        assert self.thread == threading.get_ident()
        self.calls.append(("close", self.thread))
        self._client.close()


class Engine:
    """Scenario controls for provider HTTP, never an alternative move iterator."""
    def __init__(self, *, further=True, hop_gate=None, finish_gate=None, count=2,
                 ready=(), fail=False, note=None):
        self.further, self.hop_gate, self.finish_gate = further, hop_gate, finish_gate
        self.count, self.ready, self.fail, self.note = count, ready, fail, note
        self.calls = []

    @staticmethod
    def track(tid, rank=None):
        if tid == "bootstrap":
            artist, title = "Bootstrap", "Track"
        elif tid == "seed":
            artist, title = "Seed", "Track"
        elif tid == "branch":
            artist, title = "Branch Source", "Branch Track"
        else:
            rank = 0 if tid == "anchor" else rank
            artist, title = f"Artist {rank}", f"Title {rank}"
        return {"id": tid, "artist": artist, "track": title, "name": f"{artist} - {title}",
                "video_id": f"v{rank}" if rank is not None else None}

    def cosine(self, request):
        path = request.url.path.removeprefix("/api/v1")
        if path == "/search":
            hits = [self.track("branch")] if "Branch" in request.url.params.get("q", "") else []
            return httpx.Response(200, json={"data": hits})
        if path == "/tracks/lookup":
            return httpx.Response(200, json={"data": []})  # Discogs URLs aren't indexed here
        tid = path.removeprefix("/tracks/").removesuffix("/similar")
        source = self.track(tid)
        if request.url.params.get("limit") == "1":
            if tid != "bootstrap":
                self.calls.append(threading.get_ident())
            items = []
        elif tid == "bootstrap":
            items = [self.track("anchor")]
            if self.note:
                items[0].update(track=self.note["message"], name=f"Artist 0 - {self.note['message']}")
        elif tid == "branch":
            if self.fail:
                return httpx.Response(429, json={"message": "Authorization: injected-fixture-secret"})
            items = [self.track(f"c{self.count + 1}", self.count + 1)]
        else:
            items = [self.track(f"c{rank}", rank) for rank in range(1, self.count + 1)]
        return httpx.Response(200, json={"data": {"source_track": source,
            "similar_tracks": [dict(item, score=0.9 - (i + 1) * 0.05) for i, item in enumerate(items)]}})


class Providers:
    def __init__(self, gate=None, fail=False, handler=None):
        self.gate = gate
        self.fail = fail
        self.handler = handler
        self.requests = []
        self.clients = []
        self.closed = []
        self.trackers = []
        self.tokens = []

    def __call__(self, budget=100, http_cache=None, token=None, *, budget_tracker=None, request_slot=None):
        tracker = budget_tracker if budget_tracker is not None else chain.BudgetTracker(budget)
        self.trackers.append(tracker)
        self.tokens.append(token)
        owner = threading.get_ident()
        self.clients.append(owner)

        def response(request):
            assert threading.get_ident() == owner
            self.requests.append(str(request.url))
            if self.handler is not None:
                return self.handler(request)
            if self.fail:
                return httpx.Response(503, json={"error": "Authorization: injected-fixture-secret"})
            if request.url.path.startswith(("/labels/", "/artists/")):
                if request.url.path == "/labels/42/releases":
                    if self.engine.hop_gate:
                        assert self.engine.hop_gate.wait(15)
                    releases = [{"id": 900, "artist": "Branch Source", "title": "Branch Track", "year": 2026}]
                else:
                    releases = []
                return httpx.Response(200, json={"releases": releases, "pagination": {"pages": 1}})
            rank = (int(request.url.params["artist"].rsplit(" ", 1)[1]) if request.url.path == "/database/search"
                    else int(request.url.path.rsplit("/", 1)[1]))
            if request.url.path == "/database/search":
                return httpx.Response(200, json={"results": [{"type": "release", "id": rank}]})
            if rank == 900:  # The label's newest release really holds the branch recording.
                return httpx.Response(200, json={"id": 900, "labels": [{"id": 42, "name": "Shared Label"}],
                    "artists": [{"id": 1900, "name": "Branch Source"}],
                    "tracklist": [{"position": "A1", "title": "Branch Track"}]})
            return httpx.Response(200, json={"id": rank, "labels": [{"id": 42, "name": "Shared Label"}],
                "artists": [{"id": rank + 100, "name": f"Artist {rank}"}],
                "extraartists": [{"id": 9, "name": "Shared Person", "role": "Mixed By"}],
                "tracklist": [{"position": "A1", "title": f"Title {rank}"}]})

        transport = chain._RetryOn429Transport(wrapped=httpx.MockTransport(response),
                                               budget_tracker=tracker, http_cache=http_cache, token=token)
        send = transport._send
        if request_slot:
            def send_in_slot(request, backoff=0.0):
                # Hold the facts worker before admission to the shared HTTP
                # slot, allowing the foreground real engine to finish hop 1.
                rank = (int(request.url.params["artist"].rsplit(" ", 1)[1]) if request.url.path == "/database/search"
                        else int(request.url.path.rsplit("/", 1)[1]) if request.url.path.startswith("/releases/") else 0)
                if (self.gate and threading.current_thread().name != "slice-job-worker"
                        and rank and rank not in self.engine.ready):
                    assert self.gate.wait(15)
                with request_slot():
                    return send(request, backoff)
            transport._send = send_in_slot
        client = httpx.Client(transport=transport)
        close = client.close

        def close_owned():
            assert threading.get_ident() == owner
            self.closed.append(owner)
            close()
        client.close = close_owned
        return client, tracker


def production_runtime(cfg, engine, providers, calls=None):
    providers.engine = engine
    app = create_app(cfg, client_factory=lambda: FakeCosine(calls, engine), discogs_client_factory=providers)
    runtime = app.state.slice_runtime
    runtime._test_app = app
    runtime._test_providers = providers
    app.state.test_engine = engine
    if engine.finish_gate:
        emit = runtime._emit
        def hold_boundary(worker, event, data):
            if event == "picks_done":
                assert engine.finish_gate.wait(15)
            return emit(worker, event, data)
        runtime._emit = hold_boundary
    return runtime


def host(runtime, *, further=False):
    return runtime._test_app


@contextmanager
def server(app):
    with socket.socket() as sock:
        port = bind_loopback(sock)
        sock.listen()
        served = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=served.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            eventually(lambda: served.started)
            yield f"http://127.0.0.1:{port}"
        finally:
            served.should_exit = True
            thread.join(15)
            assert not thread.is_alive()


def read_events(response):
    event = {}
    for line in response.iter_lines():
        if not line:
            if "data" in event:
                yield event
            event = {}
        elif line.startswith("id: "):
            event["id"] = line[4:]
        elif line.startswith("event: "):
            event["event"] = line[7:]
        elif line.startswith("data: "):
            event["data"] = json.loads(line[6:])


def start(client, key=None, *, further=False, runtime=None):
    headers = {"Idempotency-Key": key} if key else {}
    point = seed().to_dict()
    if isinstance(client, TestClient) and client.app.state.test_engine.note:
        point["name"] = client.app.state.test_engine.note["message"]
    if further:
        if not hasattr(client, "_slice_bootstrap"):
            bootstrap = Point(point_id="track:bootstrap|track", kind="track", name="Bootstrap - Track", cosine_id="bootstrap")
            response = client.post("/api/digs", json={"seeds": [bootstrap.to_dict()]},
                                   headers={"Idempotency-Key": key + "-origin"} if key else {})
            assert response.status_code == 200, response.text
            original = response.json()
            snapshot = eventually(lambda: (snap if (snap := client.get(f"/api/steps/{original['step_id']}").json())["job"]["state"] != "running" else None))
            pick = snapshot["snapshot"]["picks"][0]
            client._slice_bootstrap = (original, Point(point_id=f"track:{pick['track_key']}", kind="track",
                name=f"{pick['artist']} - {pick['title']}", artist=pick["artist"], title=pick["title"],
                track_key=pick["track_key"], cosine_id=pick["cosine_id"], video_id=pick["video_id"],
                source_step_id=original["step_id"], source_rank=pick["rank"]).to_dict())
            if runtime:
                runtime._test_app.state.test_engine.calls.clear()
                runtime._test_providers.requests.clear()
                runtime._test_providers.trackers.clear()
        original, point = client._slice_bootstrap
        response = client.post("/api/steps", json={"parent_step_id": original["step_id"], "origins": [point],
                               "move": "track", "further": True}, headers=headers)
    else:
        response = client.post("/api/digs", json={"seeds": [point]}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def repeat_move(client, step_id, *, retry=False):
    original = client.get(f"/api/steps/{step_id}").json()["snapshot"]["step"]
    return client.post("/api/steps", json={"parent_step_id": original["parent_id"], "origins": original["origins"],
        "move": original["move"], "further": original["further"],
        ("retry_step_id" if retry else "rerun_of"): step_id})


def replay(client, job_id, **kwargs):
    response = client.get(f"/api/jobs/{job_id}/events", **kwargs)
    assert response.status_code == 200, response.text
    return list(read_events(response))


@pytest.fixture
def make_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(discogs, "pace", lambda *args: None)
    monkeypatch.setattr("cosine_digger.client._retry_wait", lambda *args: 0)
    monkeypatch.delenv("COSINE_DIGGER_TEST_MODE", raising=False)
    runtimes = []
    gates = []

    def make(engine=None, providers=None, cfg=None):
        cfg = cfg or AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="configured-fixture-token")
        engine = engine or Engine(further=False)
        providers = providers or Providers()
        calls = []
        get_config = cfg if callable(cfg) else lambda: cfg
        runtime = production_runtime(get_config(), engine, providers, calls)
        runtime.get_config = get_config
        runtimes.append(runtime)
        gates.extend(g for g in (engine.hop_gate, engine.finish_gate, providers.gate) if g is not None)
        return runtime, engine, providers, calls

    yield make
    for gate in gates:
        gate.set()
    for runtime in runtimes:
        runtime.drain()


def test_http_near_arrives_before_further_and_reconnects(make_runtime, monkeypatch):
    hop_gate, facts_gate = threading.Event(), threading.Event()
    runtime, engine, providers, _ = make_runtime(Engine(hop_gate=hop_gate), Providers(facts_gate))
    with server(host(runtime, further=True)) as url, httpx.Client(base_url=url, timeout=10) as client:
        identity = start(client, "root-request", further=True, runtime=runtime)
        job_id, step_id = identity["job_id"], identity["step_id"]
        events_url = f"/api/jobs/{job_id}/events"
        with client.stream("GET", events_url) as response:
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            prefix = []
            for event in read_events(response):
                prefix.append(event)
                if event["event"] == "pick" and event["data"]["pick"]["rank"] == 2:
                    break
            assert not hop_gate.is_set()
            seen = client.get(f"/api/steps/{step_id}").json()
            assert [p["rank"] for p in seen["snapshot"]["picks"]] == [1, 2]
            assert seen["job"]["cursor"] == prefix[-1]["data"]["seq"]
            assert seen["snapshot"]["step"]["origins"][0]["resolution"] == "exact"
            original_picks = seen["snapshot"]["picks"]
        # A disconnected listener never cancels generation.
        assert runtime.job_snapshot(job_id)["state"] == "running"
        assert start(client, "root-request", further=True) == identity
        changed = seed().model_copy(update={"name": "Another seed"})
        assert client.post("/api/digs", json={"seeds": [changed.to_dict()]},
                           headers={"Idempotency-Key": "root-request"}).status_code == 409
        # Lose every notification: a connection already open must still find
        # later commits by polling the journal, including its terminal event.
        monkeypatch.setattr(runtime._wake, "notify_all", lambda: None)
        received = list(prefix)
        with client.stream("GET", events_url, params={"after": 0},
                           headers={"Last-Event-ID": prefix[-1]["id"]}) as response:
            hop_gate.set()
            for event in read_events(response):
                received.append(event)
                if event["event"] == "picks_done":
                    break
            assert received[-1]["data"]["pick_count"] == 3
            assert received[-1]["data"]["facts"] == {"completed": 0, "total": 3}
            assert not facts_gate.is_set()
            partial = client.get(f"/api/steps/{step_id}").json()
            assert partial["job"]["state"] == "running"
            assert partial["snapshot"]["step"]["picks_done_at"] == received[-1]["data"]["at"]
            summary = client.get(f"/api/digs/{identity['dig_id']}").json()["steps"][-1]
            assert summary["picks_done_at"] == partial["snapshot"]["step"]["picks_done_at"]
        facts_gate.set()
        eventually(lambda: runtime.job_snapshot(job_id)["state"] == "done")
        # Both cursor inputs apply; max(after, header) avoids double delivery.
        tail = replay(client, job_id, params={"after": received[-1]["data"]["seq"]},
                      headers={"Last-Event-ID": prefix[-1]["id"]})
        received.extend(tail)
        journal = replay(client, job_id)
        assert received == journal
        assert [e["data"]["seq"] for e in journal] == list(range(1, len(journal) + 1))
        assert all(e["id"] == f"{job_id}:{e['data']['seq']}" for e in journal)
        assert [e["data"]["pick"]["rank"] for e in journal if e["event"] == "pick"] == [1, 2, 3]
        assert [e["event"] for e in journal].count("picks_done") == 1
        assert journal[-1]["event"] == "done"
        assert [e["data"]["rank"] for e in journal if e["event"] == "facts"] == [1, 2, 3]
        assert [e["event"] for e in journal[-3:]] == ["hub", "hub", "done"]
        final = client.get(f"/api/steps/{step_id}").json()
        assert final["job"]["cursor"] == len(journal) == runtime.job_snapshot(job_id)["last_seq"]
        assert [p["facts_state"] for p in final["snapshot"]["picks"]] == ["ready"] * 3
        assert [h["pick_ranks"] for h in final["snapshot"]["hubs"]] == [[1, 2, 3], [1, 2, 3]]
        for before, after in zip(original_picks, final["snapshot"]["picks"]):
            assert {k: v for k, v in before.items() if k not in ("facts", "facts_state")} == {
                k: v for k, v in after.items() if k not in ("facts", "facts_state")}
        assert replay(client, job_id, params={"after": len(journal)}) == []
        frozen = storage.canonical_json(final["snapshot"])
        assert storage.canonical_json(client.get(f"/api/steps/{step_id}").json()["snapshot"]) == frozen
        assert len(engine.calls) == 1
        assert len({id(t) for t in providers.trackers}) == 1
        assert providers.trackers[0].remaining == 40 - len(providers.requests)
        del client._slice_bootstrap
        sibling_dig = start(client, "other-dig", further=True, runtime=runtime)
        eventually(lambda: runtime.job_snapshot(sibling_dig["job_id"])["state"] == "done")
        returned = client.get(f"/api/steps/{step_id}").json()
        assert storage.canonical_json(returned["snapshot"]) == frozen
        assert returned["live"]["also_in"]["artist 1|title 1"][0]["step_id"] == sibling_dig["step_id"]


def test_cursor_rejection_poll_recovery_and_terminal_close(make_runtime):
    runtime, _, _, _ = make_runtime()
    with TestClient(host(runtime), base_url="http://localhost") as client:
        identity = start(client)
        eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] != "running")
        job = runtime.job_snapshot(identity["job_id"])
        job_id = identity["job_id"]
        for cursor in ("another:1", f"{job_id}:-1", f"{job_id}:no", f"{job_id}:1.0", ""):
            assert client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": cursor}).status_code == 400
        assert client.get(f"/api/jobs/{job_id}/events?after=-1").status_code == 400
        for cursor in ("wrong", "true", "1.0", "", "9" * 21):
            assert client.get(f"/api/jobs/{job_id}/events", params={"after": cursor}).status_code == 400
        assert client.get(f"/api/jobs/{job_id}/events?after={job['last_seq'] + 1}").status_code == 409
        assert client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": f"{job_id}:999"}).status_code == 409
        assert client.get("/api/jobs/sl-missing/events").status_code == 404
        assert client.get("/api/jobs/sl-missing").status_code == 404
        assert not runtime.owns("legacy-id")
        assert client.get(f"/api/jobs/{job_id}").json() == job
        snapshot = client.get(f"/api/steps/{identity['step_id']}").json()
        assert snapshot["job"]["cursor"] == job["last_seq"]
        assert replay(client, job_id, headers={"Last-Event-ID": f"{job_id}:{job['last_seq']}"}) == []
        assert replay(client, job_id)[-1]["event"] == "done"


@pytest.mark.parametrize("linked_pick", ["neighbor"])
def test_runtime_retains_discogs_links_for_pick_aliases_and_similar_sources(tmp_path, monkeypatch, linked_pick):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.delenv("COSINE_DIGGER_TEST_MODE", raising=False)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "pace", lambda *args: None)

    def no_network(*args, **kwargs):
        pytest.fail("facts handoff tests must not make real network calls")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    providers = MoveProviders()
    roots = [track("seed", release=99), track("other-seed")]
    picked = track("picked", "Pick Artist", "Pick Recording", video="picked-video",
                   release=42 if linked_pick == "neighbor" else None)
    alias = picked.model_copy(update={"id": "alias", "external_link": "https://www.discogs.com/release/42",
                                     "track": "Pick Recording (Original Mix)" if linked_pick == "original_mix_alias"
                                              else picked.track})
    providers.sources = {item.id: item for item in roots}
    providers.neighbors = {roots[0].id: [picked], roots[1].id: [alias] if linked_pick != "neighbor" else []}
    providers.releases["/releases/42"] = release_payload(42, picked, labels=(7,),
        artists=[{"id": 8, "name": picked.artist}])
    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(providers.discogs))
    trackers = []

    def discogs_factory(**kwargs):
        client, tracker = chain.make_rate_limited_client(**kwargs)
        trackers.append(tracker)
        return client, tracker

    move_clients = []
    move_client = slice_jobs._MoveClient

    def remember_client(client, **kwargs):
        wrapped = move_client(client, **kwargs)
        move_clients.append(wrapped)
        return wrapped

    monkeypatch.setattr(slice_jobs, "_MoveClient", remember_client)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="configured-fixture-token")
    app = create_app(cfg, client_factory=lambda: FakeCosine(engine=providers),
                     discogs_client_factory=discogs_factory)
    runtime = app.state.slice_runtime
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/digs", json={"seeds": [point(item).to_dict() for item in roots]})
        assert response.status_code == 200, response.text
        identity = response.json()
        eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] != "running")
        assert runtime.job_snapshot(identity["job_id"])["state"] == "done"
        saved = client.get(f"/api/steps/{identity['step_id']}").json()
        [pick] = saved["snapshot"]["picks"]
        assert pick["cosine_id"] == picked.id  # Fusion keeps the first recording alias.
        assert pick["facts_state"] == "ready"
        assert pick["facts"]["release_id"] == "42"
        assert pick["facts"]["release_url"] == "https://www.discogs.com/release/42"
        assert pick["facts"]["labels"] == [{"id": "7", "name": "Label 7"}]
        assert [(path, params) for provider, path, params in providers.calls if provider == "discogs"] == [
            ("/releases/42", {})]
        assert len(trackers) == 2 and all(item is trackers[0] for item in trackers)
        assert trackers[0].remaining == 39
        # The origin occurs only as a similar source, and its private link is retained too.
        assert move_clients[0].tracks[roots[0].id].external_link == roots[0].external_link
        events = replay(client, identity["job_id"])
        with closing(storage.connect(cfg.db_path)) as conn:
            journal = storage.slice_events(conn, identity["job_id"])
            assert storage.get_step_snapshot(conn, identity["step_id"]) == saved
        assert [(event["event"], event["data"]) for event in events] == [
            (event["event"], event["data"]) for event in journal]
        [public_pick] = [event["data"]["pick"] for event in events if event["event"] == "pick"]
        pick_fields = {"id", "step_id", "rank", "track_key", "cosine_id", "artist", "title", "video_id",
                       "score", "batch", "route", "facts", "facts_state", "layout"}
        assert set(public_pick) == set(pick) == pick_fields
        assert public_pick["facts_state"] == "loading" and public_pick["facts"] is None
        assert "https://www.discogs.com/release/42" not in storage.canonical_json(public_pick)
        public_data = storage.canonical_json([saved, events, journal])
        assert "external_link" not in public_data and roots[0].external_link not in public_data
        assert events[-1]["event"] == "done"


@pytest.mark.parametrize("method,error_type", [
    ("similar", httpx.ReadTimeout),
])
def test_gui_move_retries_first_transport_failure_once(make_runtime, monkeypatch, method, error_type):
    engine = Engine(further=False)
    send = engine.cosine
    attempts = []
    path = {"similar": "/tracks/seed/similar", "search": "/search", "lookup": "/tracks/lookup"}[method]

    def respond(request):
        if request.url.path.removeprefix("/api/v1") == path and (
                method != "similar" or request.url.params.get("limit") == "1"):
            attempts.append((time.monotonic(), str(request.url)))
            if len(attempts) == 1:
                raise error_type("synthetic dropped connection", request=request)
            if method != "similar":
                return httpx.Response(200, json={"data": [engine.track("seed")]})
        return send(request)

    engine.cosine = respond
    runtime, _, _, _ = make_runtime(engine)
    origin = seed()
    if method == "search":
        origin = origin.model_copy(update={"cosine_id": None})
    elif method == "lookup":
        url = "https://www.youtube.com/watch?v=fixture-video"
        origin = Point(point_id=f"link:{url}", kind="link", name=url, url=url)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        response = client.post("/api/digs", json={"seeds": [origin.to_dict()]})
        assert response.status_code == 200, response.text
        identity = response.json()
        events = replay(client, identity["job_id"])
        assert events[-1]["event"] == "done"
        assert runtime.job_snapshot(identity["job_id"])["state"] == "done"
        assert len(runtime._snapshot(identity["step_id"])["picks"]) == 2
        assert len(attempts) == 2 and attempts[0][1] == attempts[1][1]
        assert attempts[1][0] - attempts[0][0] >= 0.95


def test_gui_second_transport_failure_reads_unreachable(make_runtime):
    engine = Engine(further=False)
    attempts = []

    def respond(request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("synthetic dropped connection", request=request)

    engine.cosine = respond
    runtime, _, _, _ = make_runtime(engine)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        identity = start(client)
        events = replay(client, identity["job_id"])
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["error"] == {
            "code": "cosine_unreachable", "message": "Couldn't reach cosine.club. Nothing was lost.",
            "retry_after_s": None, "retry_at": None,
        }
        assert runtime._snapshot(identity["step_id"])["step"]["status"] == "failed"


@pytest.mark.parametrize("status", [404])
def test_gui_http_errors_get_no_additional_transport_retry(make_runtime, monkeypatch, status):
    # A far-away reset makes the existing CLI client fail 429 immediately.
    monkeypatch.setattr(client_module, "_rate_limit_state", (None, None))
    engine = Engine(further=False)
    attempts = []

    def respond(request):
        attempts.append(str(request.url))
        return httpx.Response(status, headers={"x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(time.time() + 60), "Retry-After": "60"}, json={"code": "SYNTHETIC_ERROR"})

    engine.cosine = respond
    runtime, _, _, _ = make_runtime(engine)
    origin = seed().model_copy(update={"cosine_id": None})
    with TestClient(host(runtime), base_url="http://localhost") as client:
        response = client.post("/api/digs", json={"seeds": [origin.to_dict()]})
        assert response.status_code == 200, response.text
        events = replay(client, response.json()["job_id"])
        assert len(attempts) == 1
        assert events[-1]["event"] == "error" and events[-1]["data"]["error"]["code"] == "cosine_unavailable"


def test_failed_step_logs_one_redacted_warning(make_runtime, tmp_path, monkeypatch, caplog):
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", cosine_token="synthetic-private-marker")
    runtime, engine, _, calls = make_runtime(Engine(further=False), cfg=cfg)
    attempts = []

    def factory(config):
        client = FakeCosine(calls, engine)

        def fail(*args, **kwargs):
            attempts.append(1)
            raise CosineRequestError(
                "connection dropped synthetic-private-marker\nAuthorization: fixture-header-secret\n"
                "https://cosine.club/example?token=fixture-query-secret")

        client.similar = fail
        return client

    monkeypatch.setattr(runtime, "client_factory", factory)
    with caplog.at_level(logging.WARNING, logger=slice_jobs.__name__):
        with TestClient(host(runtime), base_url="http://localhost") as client:
            identity = start(client)
            assert replay(client, identity["job_id"])[-1]["event"] == "error"
            records = [r for r in caplog.records if r.name == slice_jobs.__name__ and r.levelno == logging.WARNING]
            assert len(records) == 1 and len(attempts) == 2
            message = records[0].getMessage()
            assert identity["step_id"] in message and "CosineRequestError" in message
            assert "connection dropped" in message and "[redacted]" in message
            assert all(secret not in message for secret in (
                "synthetic-private-marker", "fixture-header-secret", "fixture-query-secret"))
            assert "\n" not in message and "\r" not in message


@pytest.mark.parametrize("delay,countdown", [(47, 47), (47.2, 48)])
def test_long_cosine_rate_limit_fails_step_with_server_countdown(make_runtime, monkeypatch, delay, countdown):
    clock = SimpleNamespace(now=2_000_000_000.0, waits=[])

    def sleep(seconds):
        clock.waits.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(client_module, "time", SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(client_module, "_rate_limit_state", (None, None), raising=False)
    requests = []
    engine = Engine(further=False)

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(429, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(2_000_000_000 + delay),
            "Set-Cookie": "synthetic-response-secret",
        }, json={"code": "RATE_LIMITED"})

    engine.cosine = respond
    runtime, _, _, _ = make_runtime(engine)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        before = datetime.now(timezone.utc)
        identity = start(client)
        events = replay(client, identity["job_id"])
        after = datetime.now(timezone.utc)
        assert requests == ["/api/v1/tracks/seed/similar"]
        assert clock.waits == []
        error = events[-1]["data"]["error"]
        assert events[-1]["event"] == "error"
        assert error["retry_after_s"] == countdown
        retry_at = datetime.fromisoformat(error["retry_at"])
        assert countdown <= (retry_at - before).total_seconds()
        assert (retry_at - after).total_seconds() <= countdown
        saved = client.get(f"/api/steps/{identity['step_id']}").json()
        assert saved["snapshot"]["step"]["status"] == "failed"
        assert saved["snapshot"]["step"]["error"] == error
        assert "synthetic-response-secret" not in json.dumps([saved, events])


def test_generation_failure_settles_facts_redacts_journal_and_preserves_prefix(make_runtime, tmp_path):
    gate = threading.Event()
    note = {"code": "S-N-CLOSEST", "params": {"seed": "Authorization: injected-fixture-secret"},
            "message": "Bearer injected-fixture-secret", "phase": "resolve"}
    engine = Engine(hop_gate=gate, fail=True, ready=(1,), note=note)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", cosine_token="injected-fixture-secret",
                    discogs_token="configured-fixture-token")
    runtime, _, providers, _ = make_runtime(engine, Providers(gate), cfg=cfg)
    with TestClient(host(runtime, further=True), base_url="http://localhost") as client:
        identity = start(client, further=True, runtime=runtime)
        eventually(lambda: len(runtime._snapshot(identity["step_id"])["picks"]) == 2)
        # Rank 1's facts aren't gated: let them land before the failure settles the rest
        # (on a slow runner the failure could otherwise settle them as unavailable first).
        eventually(lambda: runtime._snapshot(identity["step_id"])["picks"][0]["facts_state"] == "ready")
        gate.set()
        eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] == "error")
        events = replay(client, identity["job_id"])
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["error"]["retry_after_s"] == 30
        assert not any(e["event"] == "picks_done" for e in events)
        assert sum(e["event"] in ("done", "error") for e in events) == 1
        picks = runtime._snapshot(identity["step_id"])["picks"]
        assert picks[0]["facts_state"] == "ready" and picks[0]["facts"] == facts(1)
        assert picks[1]["facts_state"] in ("ready", "unavailable")
        assert all(p["facts_state"] != "loading" for p in picks)
        with closing(storage.connect(runtime.get_config().db_path)) as conn:
            stored = " ".join(row[0] for row in conn.execute("SELECT payload FROM slice_events"))
        assert "injected-fixture-secret" not in stored
        assert "[redacted]" in stored
        assert "injected-fixture-secret" not in client.get(f"/api/jobs/{identity['job_id']}").text
        frozen = storage.canonical_json(runtime._snapshot(identity["step_id"]))
        runtime._facts_result(slice_jobs._Worker(job_id=identity["job_id"]), identity["step_id"],
                              {"rank": 1, "facts_state": "missing", "facts": None, "note": None})
        assert storage.canonical_json(runtime._snapshot(identity["step_id"])) == frozen
        assert replay(client, identity["job_id"]) == events


def test_no_token_empty_and_provider_outage_do_not_fail_good_picks(make_runtime, tmp_path):
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db")
    runtime, _, providers, _ = make_runtime(cfg=cfg)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        identity = start(client)
        eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] == "done")
        assert [p["facts_state"] for p in runtime._snapshot(identity["step_id"])["picks"]] == ["no_token"] * 2
        assert providers.requests == providers.clients == []
    # A later lifespan takes a fresh dispatcher and never mutates old facts.
    old = storage.canonical_json(runtime._snapshot(identity["step_id"]))
    runtime2, _, _, _ = make_runtime(Engine(further=False, count=0), cfg=cfg)
    with TestClient(host(runtime2), base_url="http://localhost") as client:
        empty = start(client)
        eventually(lambda: runtime2.job_snapshot(empty["job_id"])["state"] == "done")
        assert [e["event"] for e in replay(client, empty["job_id"]) if e["event"] != "note"] == ["batch", "picks_done", "done"]
    runtime3, _, providers, _ = make_runtime(Engine(further=False), Providers(fail=True))
    with TestClient(host(runtime3), base_url="http://localhost") as client:
        outage = start(client)
        eventually(lambda: runtime3.job_snapshot(outage["job_id"])["state"] == "done")
        assert [p["facts_state"] for p in runtime3._snapshot(outage["step_id"])["picks"]] == ["unavailable"] * 2
        assert runtime3._snapshot(outage["step_id"])["hubs"] == []
        assert storage.canonical_json(runtime3._snapshot(identity["step_id"])) == old


def assert_unavailable_step_notes(client, runtime, identity, codes, *, count=4, terminal="done"):
    eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] != "running")
    saved = client.get(f"/api/steps/{identity['step_id']}").json()
    events = replay(client, identity["job_id"])
    notes = saved["snapshot"]["step"]["note"]
    assert [note["code"] for note in notes if note["phase"] == "facts"] == codes
    journal_notes = [{key: value for key, value in event["data"].items()
                      if key not in {"job_id", "step_id", "seq", "at"}}
                     for event in events if event["event"] == "note"]
    assert journal_notes == notes
    assert [pick["facts_state"] for pick in saved["snapshot"]["picks"]] == ["unavailable"] * count
    facts_events = [event["data"] for event in events if event["event"] == "facts"]
    assert sorted(event["rank"] for event in facts_events) == list(range(1, count + 1))
    assert all(event["facts_state"] == "unavailable" and event["facts"] is None for event in facts_events)
    assert facts_events[-1]["completed"] == facts_events[-1]["total"] == count
    assert events[-1]["event"] == terminal
    assert sum(event["event"] in {"done", "error"} for event in events) == 1
    assert [event["data"]["seq"] for event in events] == list(range(1, len(events) + 1))
    assert saved["job"]["cursor"] == len(events)
    if terminal == "done":
        assert events[-1]["data"]["facts"] == {"completed": count, "total": count}
        assert events[-1]["data"]["hub_count"] == len(saved["snapshot"]["hubs"]) == 0
    with closing(storage.connect(runtime._db_path)) as conn:
        assert storage.get_step_snapshot(conn, identity["step_id"]) == saved
        assert storage.slice_events(conn, identity["job_id"]) == events
    assert replay(client, identity["job_id"], params={"after": len(events)}) == []
    return saved, events


def test_step_deduplicates_budget_facts_notes_and_preserves_resolve_notes(make_runtime):
    class LabelEngine(Engine):
        def cosine(self, request):
            if request.url.path.endswith("/tracks/lookup"):
                items = [self.track("seed")] if request.url.params["url"].endswith("/release/901") else []
                return httpx.Response(200, json={"data": items})
            return super().cosine(request)

    def label_response(request):
        # Twenty pagination requests and twenty release reads spend the real
        # shared 40-request budget during resolution, before facts admission.
        if request.url.path == "/labels/42/releases":
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"pagination": {"pages": 20}, "releases": [
                {"id": 900 + page, "artist": "Seed", "title": f"Album {page}", "year": 2027 - page}]})
        rank = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": rank, "artists": [{"id": 7, "name": "Seed"}],
            "tracklist": [{"position": "A1", "title": "Track" if rank == 901 else f"Unindexed {rank}"}]})

    runtime, _, providers, _ = make_runtime(LabelEngine(further=False, count=4),
                                           Providers(handler=label_response))
    origin = Point(point_id="label:42", kind="label", name="Fixture Label", discogs_id="42")
    with TestClient(host(runtime), base_url="http://localhost") as client:
        response = client.post("/api/digs", json={"seeds": [origin.to_dict()]})
        assert response.status_code == 200, response.text
        identity = response.json()
        saved, events = assert_unavailable_step_notes(client, runtime, identity, ["discogs_budget_exhausted"])
        assert len(providers.requests) == 40
        assert len({id(tracker) for tracker in providers.trackers}) == 1
        assert providers.trackers[0].remaining == 0
        assert saved["snapshot"]["step"]["note"][:2] == [
            {"code": "S-HDR-PROGRESS", "params": {"i": 0, "n": 25},
             "message": "listening for neighbours · 0 of 25", "phase": "resolve"},
            {"code": "S-N-LABEL-FEW", "params": {"m": 1, "Label": "Fixture Label"},
             "message": "Only 1 of the newest releases on Fixture Label are on cosine.club, so this step leans on them.",
             "phase": "resolve"},
        ]
        assert events[-1]["data"]["warning_codes"] == ["S-HDR-PROGRESS", "S-N-LABEL-FEW", "discogs_budget_exhausted"]
        # A late result carrying the same warning cannot rewrite a saved step.
        runtime._facts_result(slice_jobs._Worker(job_id=identity["job_id"]), identity["step_id"],
                              discogs._facts_unavailable(1, "budget"))
        assert client.get(f"/api/steps/{identity['step_id']}").json() == saved
        assert replay(client, identity["job_id"]) == events


@pytest.mark.parametrize("lost_facts_write", [False])
def test_step_deduplicates_interrupted_facts_notes_across_callbacks_and_finalization(
        make_runtime, monkeypatch, lost_facts_write):
    gate = threading.Event()
    runtime, _, _, _ = make_runtime(Engine(further=False, count=4), Providers(gate))
    settle = discogs.FactsQueue.settle

    def interrupt(queue, job_id, unavailable_reason=None):
        return settle(queue, job_id, unavailable_reason="interrupted")

    monkeypatch.setattr(discogs.FactsQueue, "settle", interrupt)
    attempts = []
    emit = runtime._emit

    def lose_facts_write(worker, event, data):
        if lost_facts_write and event == "facts" and data["rank"] == 2:
            attempts.append(event)
            if len(attempts) == 1:
                # The callback saved its note, but this pick stays loading for
                # the job-level fallback. Then retry that fallback once too.
                raise RuntimeError("fixture facts write failure")
            if len(attempts) == 2:
                error = sqlite3.OperationalError("fixture writer contention")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
        return emit(worker, event, data)

    monkeypatch.setattr(runtime, "_emit", lose_facts_write)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        identity = start(client)
        eventually(lambda: runtime._snapshot(identity["step_id"])["step"]["picks_done_at"])
        eventually(lambda: runtime._facts._jobs[identity["job_id"]].unavailable_reason)
        gate.set()
        _, events = assert_unavailable_step_notes(client, runtime, identity, ["facts_interrupted"],
                                                  terminal="error" if lost_facts_write else "done")
        if lost_facts_write:
            assert len(attempts) == 3
            assert [event["data"]["rank"] for event in events if event["event"] == "facts"] == [1, 3, 4, 2]


def _crash_server(sock, path, stage):
    """Spawn target: every provider is fake; SIGKILL owns only this process."""
    cfg = AppConfig(home=Path(path), db_path=Path(path) / "taste.db", discogs_token="configured-fixture-token")
    blocked = threading.Event()
    engine = Engine(further=stage == "before", hop_gate=blocked if stage == "before" else None,
                    count=0 if stage == "empty" else 2, ready=(1,) if stage in ("before", "after") else ())
    discogs.pace = lambda *args: None
    runtime = production_runtime(cfg, engine, Providers(None if stage == "hubs" else blocked))
    if stage in ("empty", "hubs"):
        emit = runtime._emit

        def pause_after_boundary(worker, event, data):
            payload = emit(worker, event, data)
            if (stage == "empty" and event == "picks_done") or (stage == "hubs" and event == "facts" and data["rank"] == 2):
                blocked.wait(30)
            return payload
        runtime._emit = pause_after_boundary
    uvicorn.Server(uvicorn.Config(host(runtime, further=engine.further), log_level="critical",
                                  access_log=False)).run(sockets=[sock])


@pytest.mark.parametrize("stage", ["before", "after"])
def test_owned_server_crash_restart_retry_and_attempt_scoped_replay(make_runtime, tmp_path, stage):
    with socket.socket() as sock:
        port = bind_loopback(sock)
        sock.listen()
        process = multiprocessing.get_context("spawn").Process(target=_crash_server, args=(sock, str(tmp_path), stage))
        process.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
                def live():
                    try:
                        return client.get("/openapi.json").status_code == 200
                    except httpx.TransportError:
                        return False
                eventually(live)
                identity = start(client, "crash-root", further=stage == "before")

                def prefix_arrived():
                    snap = client.get(f"/api/steps/{identity['step_id']}").json()
                    if stage == "before":
                        return snap if len(snap["snapshot"]["picks"]) == 2 else None
                    if stage == "hubs":
                        picks = snap["snapshot"]["picks"]
                        return snap if len(picks) == 2 and all(p["facts_state"] == "ready" for p in picks) else None
                    return snap if snap["snapshot"]["step"]["picks_done_at"] else None
                before = eventually(prefix_arrived)
                assert before["job"]["state"] == "running"
                process.kill()
                process.join(5)
                assert not process.is_alive()
                # Read the durable state only after the kill: the live server may
                # commit more events (facts, hubs) between the snapshot and the kill.
                with closing(storage.connect(tmp_path / "taste.db")) as conn:
                    old_prefix = storage.slice_events(conn, identity["job_id"])
                    killed = storage.get_step_snapshot(conn, identity["step_id"])["snapshot"]
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="configured-fixture-token")
    gate = threading.Event()
    runtime, engine, providers, _ = make_runtime(Engine(further=stage == "before", finish_gate=gate, count=0), cfg=cfg)
    with TestClient(host(runtime, further=engine.further), base_url="http://localhost") as client:
        recovered = client.get(f"/api/steps/{identity['step_id']}").json()
        assert recovered["job"]["id"] == identity["job_id"]
        assert recovered["job"]["state"] == recovered["snapshot"]["step"]["status"] == "stopped"
        assert recovered["snapshot"]["step"]["error"]["code"] == "server_restarted"
        assert recovered["snapshot"]["step"]["picks_done_at"] == killed["step"]["picks_done_at"]
        assert len(recovered["snapshot"]["picks"]) == len(killed["picks"]) >= len(before["snapshot"]["picks"])
        for old, new in zip(killed["picks"], recovered["snapshot"]["picks"]):
            assert old["route"] == new["route"] and old["layout"] == new["layout"]
            assert new["facts_state"] == ("unavailable" if old["facts_state"] == "loading" else old["facts_state"])
            if old["facts_state"] == "ready":
                assert old["facts"] == new["facts"]
        assert engine.calls == [] and providers.requests == []
        if stage == "hubs":
            assert before["snapshot"]["hubs"] == []
            assert [hub["pick_ranks"] for hub in recovered["snapshot"]["hubs"]] == [[1, 2], [1, 2]]
        journal = replay(client, identity["job_id"])
        assert journal[:len(old_prefix)] == old_prefix
        assert sum(e["event"] in ("done", "error") for e in journal) == 1
        assert journal[-1]["data"]["error"]["code"] == "server_restarted"
        assert replay(client, identity["job_id"], params={"after": before["job"]["cursor"]}) == [
            event for event in journal if event["data"]["seq"] > before["job"]["cursor"]]
        assert start(client, "crash-root", further=stage == "before") == identity
        if stage == "empty":
            retried = repeat_move(client, identity["step_id"], retry=True).json()
            assert retried["step_id"] == identity["step_id"] and retried["job_id"] != identity["job_id"]
            attempt = eventually(lambda: client.get(f"/api/steps/{identity['step_id']}").json()
                                 if runtime.job_snapshot(retried["job_id"])["last_seq"] else None)
            assert attempt["snapshot"]["step"]["picks_done_at"] is None
            assert attempt["job"]["attempt"] == 2
            assert client.get(f"/api/jobs/{retried['job_id']}/events",
                              headers={"Last-Event-ID": journal[-1]["id"]}).status_code == 400
            runtime._facts_result(slice_jobs._Worker(job_id=identity["job_id"]), identity["step_id"],
                                  {"rank": 1, "facts_state": "missing", "facts": None, "note": None})
            assert runtime._snapshot(identity["step_id"])["step"]["picks_done_at"] is None
            gate.set()
            eventually(lambda: runtime.job_snapshot(retried["job_id"])["state"] == "done")
            attempt_events = replay(client, retried["job_id"])
            assert [e["event"] for e in attempt_events if e["event"] != "note"] == ["batch", "picks_done", "done"]
            assert replay(client, identity["job_id"]) == journal
        else:
            frozen = storage.canonical_json(recovered["snapshot"])
            rerun = repeat_move(client, identity["step_id"]).json()
            assert rerun["step_id"] != identity["step_id"] and client.get(f"/api/steps/{rerun['step_id']}").json()["snapshot"]["step"]["number"] == (3 if stage == "before" else 2)
            gate.set()
            eventually(lambda: runtime.job_snapshot(rerun["job_id"])["state"] == "done")
            assert storage.canonical_json(client.get(f"/api/steps/{identity['step_id']}").json()["snapshot"]) == frozen
    # Recovery repeated on a new lifespan never appends another terminal.
    runtime.startup()
    assert runtime.job_snapshot(identity["job_id"])["last_seq"] == len(journal)


def test_second_server_cannot_recover_live_jobs_and_shutdown_drains(make_runtime):
    gate = threading.Event()
    runtime, engine, _, calls = make_runtime(Engine(further=False, finish_gate=gate))
    runtime.startup()
    with TestClient(host(runtime), base_url="http://localhost") as client:
        identity = start(client)
        eventually(lambda: len(runtime._snapshot(identity["step_id"])["picks"]) == 2)
        rival = SliceRuntime(get_config=runtime.get_config, client_factory=FakeCosine, redact=runtime.redact)
        with pytest.raises(LockTimeout):
            rival.startup()
        assert runtime.job_snapshot(identity["job_id"])["state"] == "running"
        runtime.stop_accepting()
        response = client.post("/api/digs", json={"seeds": [seed().to_dict()]})
        assert response.status_code == 503
        with ThreadPoolExecutor(max_workers=1) as pool:
            draining = pool.submit(runtime.drain)
            assert not draining.done()
            gate.set()
            draining.result(timeout=10)
        assert runtime.job_snapshot(identity["job_id"])["state"] == "done"
        assert [event for event, thread in calls] == ["open", "close"]
        assert len(engine.calls) == 1


def test_thread_start_and_callback_failure_roll_back_without_provider_calls(make_runtime, monkeypatch):
    runtime, engine, providers, calls = make_runtime()
    runtime.startup()

    def create(conn):
        dig_id = storage.create_dig(conn, [seed()])
        identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=[seed()], move="seed", further=False)
        raise ValueError("invalid admission")

    with pytest.raises(ValueError, match="invalid admission"):
        runtime.submit_step(create)
    original = threading.Thread.start

    def reject_worker(thread):
        if thread.name == "slice-job-worker":
            raise RuntimeError("Authorization: injected-fixture-secret")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", reject_worker)
    with pytest.raises(HTTPException) as rejected:
        runtime.submit_step(create)
    assert rejected.value.status_code == 503
    assert "injected-fixture-secret" not in rejected.value.detail
    with closing(storage.connect(runtime.get_config().db_path)) as conn:
        assert [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("digs", "steps", "slice_jobs", "slice_events")] == [0, 0, 0, 0]
    assert engine.calls == providers.requests == calls == []


def test_admission_commits_before_worker_and_serializes_shutdown(make_runtime):
    runtime, engine, _, _ = make_runtime(Engine(further=False))
    runtime.startup()
    admitted, commit = threading.Event(), threading.Event()

    def create(conn):
        dig_id = storage.create_dig(conn, [seed()])
        identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=[seed()], move="seed", further=False)
        admitted.set()
        assert commit.wait(8)
        return identity, True

    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(runtime.submit_step, create)
        assert admitted.wait(5)
        stopping = pool.submit(runtime.stop_accepting)
        assert engine.calls == [] and not stopping.done()
        with closing(storage.connect(runtime.get_config().db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
        commit.set()
        identity = submission.result(timeout=5)
        stopping.result(timeout=5)
    runtime.drain()
    assert runtime.job_snapshot(identity.job_id)["state"] == "done"
    with pytest.raises(HTTPException) as rejected:
        runtime.submit_step(create)
    assert rejected.value.status_code == 503


def test_failed_commit_releases_reservation_without_durable_job_or_provider(make_runtime, monkeypatch):
    runtime, engine, providers, calls = make_runtime()
    runtime.startup()
    connect = storage.connect

    class CommitFails:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            self.conn.rollback()
            raise sqlite3.OperationalError("injected commit failure")

    def create(conn):
        dig_id = storage.create_dig(conn, [seed()])
        identity = storage.create_step(conn, dig_id=dig_id, parent_id=None, origins=[seed()], move="seed", further=False)
        storage.update_slice_progress(conn, identity.job_id, {"request": {"key": "rolled-back"}}, redact=runtime.redact)
        return identity, True

    with monkeypatch.context() as patched:
        patched.setattr(storage, "connect", lambda path: CommitFails(connect(path)))
        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            runtime.submit_step(create)
    runtime.drain()
    with closing(connect(runtime.get_config().db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
    assert engine.calls == providers.requests == calls == []


def test_shared_queue_stays_open_until_other_runtime_workers_drain(make_runtime, tmp_path):
    first, _, _, _ = make_runtime(cfg=AppConfig(home=tmp_path / "one", db_path=tmp_path / "one" / "taste.db"))
    first.startup()
    gate = threading.Event()
    second, _, _, _ = make_runtime(Engine(further=False, finish_gate=gate),
                                   cfg=AppConfig(home=tmp_path / "two", db_path=tmp_path / "two" / "taste.db"))
    with TestClient(host(second), base_url="http://localhost") as client:
        identity = start(client)
        eventually(lambda: len(second._snapshot(identity["step_id"])["picks"]) == 2)
        queue = first._facts
        assert second._facts is queue
        first.drain()
        assert not queue._closed
        gate.set()
        eventually(lambda: second.job_snapshot(identity["job_id"])["state"] == "done")
    assert queue._closed


def test_enrichment_single_admission_cancel_checkpoint_and_new_resume_attempt(make_runtime, tmp_path):
    runtime, _, _, _ = make_runtime()
    runtime.startup()
    entered, response = threading.Event(), threading.Event()
    calls = []

    def runner(*, job_id, get_config, client_factory, cancelled, on_progress):
        calls.append(job_id)
        entered.set()
        assert response.wait(10)  # One already in-flight request may finish.
        assert cancelled()
        with closing(storage.connect(get_config().db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                storage.update_slice_progress(conn, job_id, {"work": {"cursor": 1, "remaining_pairs": 2}}, redact=runtime.redact)
        on_progress({"code": "S-SET-FILL-STOPPED", "params": {}, "message": "Stopped after 1 of 3; 1 labels filled. The next run picks up here.",
                     "phase": "enrichment"})
        return {"status": "stopped", "filled_tracks": 1, "remaining_pairs": 2}

    first = runtime.submit_enrichment(budget=100, resume_job_id=None, runner=runner)
    assert entered.wait(5)
    assert runtime.submit_enrichment(budget=100, runner=runner) == first
    assert runtime.cancel_enrichment(first["job_id"]) == {"job_id": first["job_id"], "status": "stopping"}
    assert runtime.cancel_enrichment(first["job_id"])["status"] == "stopping"
    assert runtime.job_snapshot(first["job_id"])["state"] == "running"
    response.set()
    eventually(lambda: runtime.job_snapshot(first["job_id"])["state"] == "stopped")
    assert runtime.cancel_enrichment(first["job_id"])["status"] == "stopped"
    assert calls == [first["job_id"]]

    def resumed(**kwargs):
        job = runtime.job_snapshot(kwargs["job_id"])
        assert job["attempt"] == 2 and job["progress"]["work"]["cursor"] == 1
        assert job["progress"]["work"]["resume_job_id"] == first["job_id"]
        assert job["progress"]["work"]["budget"] == 20
        return {"status": "done", "filled_tracks": 3, "remaining_pairs": 0}

    second = runtime.submit_enrichment(budget=20, resume_job_id=first["job_id"], runner=resumed)
    assert second["job_id"] != first["job_id"]
    eventually(lambda: runtime.job_snapshot(second["job_id"])["state"] == "done")
    assert runtime.job_snapshot(first["job_id"])["result"]["filled_tracks"] == 1
    with TestClient(host(runtime), base_url="http://localhost") as client:
        assert [e["event"] for e in replay(client, first["job_id"])] == ["note", "done"]
        assert replay(client, first["job_id"])[-1]["data"]["status"] == "stopped"


def test_real_label_adapter_journals_only_committed_checkpoint_and_no_token_skip(make_runtime, tmp_path):
    runtime, _, _, _ = make_runtime()
    runtime.startup()
    # No network is needed for a cached album, but it still goes through the
    # real S8 runner, its DB commit/checkpoint, and the runtime's event writer.
    with closing(storage.connect(tmp_path / "taste.db")) as conn:
        with conn:
            conn.execute("INSERT INTO tracks (path,artist,album,title) VALUES ('fixture','Library','Album','Track')")
            conn.execute("INSERT INTO discogs_cache (artist,album,discogs_label) VALUES ('Library','Album','Cached Label')")
        # The legacy positive row has no match provenance, so the GUI re-verifies it
        # from the HTTP-cached search payload (still no network request).
        search = httpx.Request("GET", f"{discogs.BASE_URL}/database/search",
                               params={"q": "Library Album", "type": "release", "per_page": 3})
        storage.cache_put(conn, "discogs:" + str(search.url), json.dumps({"results": [
            {"id": 7, "title": "Library - Album", "label": ["Cached Label"], "genre": [], "style": []}]}), 604800)
    job_id = runtime.submit_enrichment(budget=1, runner=run_label_enrichment)["job_id"]
    eventually(lambda: runtime.job_snapshot(job_id)["state"] != "running")
    assert runtime.job_snapshot(job_id)["state"] == "done"
    with closing(storage.connect(tmp_path / "taste.db")) as conn:
        assert conn.execute("SELECT label FROM tracks").fetchone()[0] == "Cached Label"
        job = storage.get_slice_job(conn, job_id)
        assert job["progress"]["work"]["pending_pairs"] == []
        assert job["result"]["filled_tracks"] == 1
        assert storage.slice_events(conn, job_id)[-1]["event"] == "done"
    runtime.drain()
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db")
    without, _, providers, _ = make_runtime(cfg=cfg)
    without.startup()
    skipped = without.submit_enrichment(budget=1, runner=run_label_enrichment)
    assert skipped == {"job_id": None, "skipped": True, "note": {"code": "S-SET-FILL-NOTOKEN"}}
    assert providers.requests == []


@pytest.mark.parametrize("kind", ["step", "enrichment"])
@pytest.mark.parametrize("failed", [False])
def test_terminal_database_lock_retries_until_commit(make_runtime, tmp_path, monkeypatch, kind, failed):
    # Review C1: hold a real competing writer past the worker's busy timeout.
    # Shorten only that timeout so the regression does not take ten seconds.
    connect = storage.connect

    def impatient_connect(path):
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout=25")
        return conn

    monkeypatch.setattr(storage, "connect", impatient_connect)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db",
                    discogs_token="configured-fixture-token" if kind == "enrichment" else None)
    runtime, _, _, _ = make_runtime(Engine(further=False, count=0), cfg=cfg)
    if failed:
        def fail(*args, **kwargs):
            raise ValueError("synthetic provider failure")
        if kind == "step":
            monkeypatch.setattr(runtime, "client_factory", fail)
        else:
            monkeypatch.setattr("cosine_digger.web.slice_routes.run_label_enrichment", fail)
    terminal, locked, busy = threading.Event(), threading.Event(), threading.Event()
    crashes, attempts = [], []
    monkeypatch.setattr(threading, "excepthook", lambda args: crashes.append(args.exc_type.__name__))
    emit = runtime._emit

    def scheduled_emit(worker, event, data):
        if event in ("done", "error"):
            attempts.append(event)
            if len(attempts) == 1:
                terminal.set()
                assert locked.wait(5)
        try:
            return emit(worker, event, data)
        except sqlite3.OperationalError:
            busy.set()
            raise

    monkeypatch.setattr(runtime, "_emit", scheduled_emit)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        if kind == "step":
            identity = start(client)
        else:
            response = client.post("/api/library/labels/enrich", json={"budget": 1})
            assert response.status_code == 200, response.text
            identity = response.json()
        job_id = identity["job_id"]
        assert terminal.wait(5)
        with closing(connect(runtime._db_path)) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            locked.set()
            try:
                assert busy.wait(5)
                time.sleep(0.1)  # Permit the old worker's finally block to run.
                assert job_id in runtime._jobs, "a running job lost its terminal writer"
                assert runtime._jobs[job_id].thread.is_alive()
                assert runtime.job_snapshot(job_id)["state"] == "running"
                assert not any(row["event"] in ("done", "error") for row in runtime._replay(job_id, 0)[0])
            finally:
                competitor.rollback()
        expected = "error" if failed else "done"
        eventually(lambda: runtime.job_snapshot(job_id)["state"] == expected)
        eventually(lambda: job_id not in runtime._jobs)
        events = replay(client, job_id)
        assert [row["event"] for row in events if row["event"] in ("done", "error")] == [expected]
        assert events[-1]["event"] == expected
        assert client.get(f"/api/jobs/{job_id}/events?after={events[-1]['data']['seq']}").text == ""
        assert crashes == []
        assert len(attempts) >= 2
        if kind == "step":
            saved = client.get(f"/api/steps/{identity['step_id']}").json()
            assert saved["snapshot"]["step"]["status"] == ("failed" if failed else "done")
            # Release the gate before any explicitly requested next attempt.
            monkeypatch.setattr(runtime, "_emit", emit)
            assert repeat_move(client, identity["step_id"], retry=failed).status_code == 200
        else:
            monkeypatch.setattr(runtime, "_emit", emit)
            next_job = client.post("/api/library/labels/enrich", json={"budget": 1}).json()["job_id"]
            assert next_job != job_id


def test_drain_stops_locked_terminal_retry_after_grace(make_runtime, tmp_path, monkeypatch):
    # Shutdown joins every worker. A terminal write held off by another
    # process's lock must not keep the server open; restart recovery settles it.
    connect = storage.connect

    def impatient_connect(path):
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout=25")
        return conn

    monkeypatch.setattr(storage, "connect", impatient_connect)
    monkeypatch.setattr(slice_jobs, "DRAIN_FINAL_WRITE_GRACE_SECONDS", 0.2)
    runtime, _, _, _ = make_runtime(Engine(further=False, count=0),
                                    cfg=AppConfig(home=tmp_path, db_path=tmp_path / "taste.db"))
    terminal, locked, busy = threading.Event(), threading.Event(), threading.Event()
    crashes = []
    monkeypatch.setattr(threading, "excepthook", lambda args: crashes.append(args.exc_type.__name__))
    emit = runtime._emit

    def scheduled_emit(worker, event, data):
        if event in ("done", "error") and not terminal.is_set():
            terminal.set()
            assert locked.wait(5)
        try:
            return emit(worker, event, data)
        except sqlite3.OperationalError:
            busy.set()
            raise

    monkeypatch.setattr(runtime, "_emit", scheduled_emit)
    with TestClient(host(runtime), base_url="http://localhost") as client:
        job_id = start(client)["job_id"]
        assert terminal.wait(5)
        with closing(connect(runtime._db_path)) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            locked.set()
            try:
                assert busy.wait(5)
                worker = runtime._jobs[job_id]
                started = time.monotonic()
                runtime.drain()
                assert time.monotonic() - started < 3
                assert not worker.thread.is_alive()
                assert job_id not in runtime._jobs
            finally:
                competitor.rollback()
        assert runtime.job_snapshot(job_id)["state"] == "running"
    assert crashes == []
