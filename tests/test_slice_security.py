"""All slice routes use the production security and strict JSON boundary."""
from contextlib import closing
from dataclasses import replace
import asyncio
import io
import json
import logging
import os
import threading
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner
import uvicorn
from uvicorn.logging import AccessFormatter
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from cosine_digger import api, chain, storage
from cosine_digger.models import SimilarTracksData, Track
from cosine_digger.slice_types import Point
from cosine_digger.web import connections
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import FixtureConfigurationError, SliceFixtures
from tests.test_slice_api import slice_app, root, finished, from_pick
from tests.test_slice_streaming import eventually


MUTATORS = [
    ("POST", "/api/digs", {"seeds": [{"point_id": "track:seed", "kind": "track", "name": "Seed"}]}),
    ("POST", "/api/steps", {"parent_step_id": "missing", "origins": [{"point_id": "track:seed", "kind": "track", "name": "Seed"}], "move": "track", "further": False}),
    ("PUT", "/api/judgments/a%7Cb", {"step_id": "missing", "expected_revision": 0, "verdict": "keep"}),
    ("DELETE", "/api/judgments/a%7Cb", {"step_id": "missing", "expected_revision": 0}),
    ("POST", "/api/heard", {"track_key": "a|b", "step_id": "missing"}),
    ("POST", "/api/library/labels/enrich", {"budget": 100}),
    ("POST", "/api/library/labels/jobs/sl-missing/cancel", {}),
]


@pytest.mark.parametrize("method,path,body", MUTATORS)
def test_every_mutator_has_host_origin_json_strict_validation_and_redaction(slice_app, method, path, body):
    client = slice_app.http
    for host in ("evil.example", "localhost@evil.example", "localhost:broken"):
        assert client.request(method, path, json=body, headers={"Host": host}).status_code == 403
    for origin in ("https://evil.example", "null", "http://localhost@evil.example", "http://localhost/path"):
        assert client.request(method, path, json=body, headers={"Origin": origin}).status_code == 403
    assert client.request(method, path, content=json.dumps(body), headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.request(method, path, content=json.dumps(body)).status_code == 415
    injected = {**body, "unknown": "Cookie: synthetic-private-cookie\nAuthorization: Bearer synthetic-private-key"}
    response = client.request(method, path, json=injected, headers={"Origin": "http://localhost"})
    assert response.status_code == 422 and response.json() == {"error": "Invalid request"}
    response = client.request(method, path, content='{"unknown":"synthetic-private-key",', headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "synthetic-private" not in response.text
    for invalid in ([], None, "synthetic-private-key"):
        response = client.request(method, path, content=json.dumps(invalid), headers={"Content-Type": "application/json"})
        assert response.status_code == 422 and "synthetic-private" not in response.text
    with closing(storage.connect(slice_app.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["/api/search?q=seed&source=cosine", "/api/digs", "/api/digs/missing",
    "/api/steps/missing", "/api/jobs/sl-missing", "/api/jobs/sl-missing/events", "/api/library/labels",
    "/", "/static/style.css"])
def test_read_static_and_sse_routes_check_host(slice_app, path):
    assert slice_app.http.get(path, headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("path,body", [
    ("/api/library/labels/enrich", {"budget": True}),
    ("/api/library/labels/enrich", {"budget": "100"}),
    ("/api/library/labels/enrich", {"budget": 0}),
    ("/api/library/labels/enrich", {"budget": 501}),
    ("/api/digs", {"seeds": [{"point_id": "x", "kind": "track", "name": "Seed", "library_id": True}]}),
    ("/api/digs", {"seeds": [{"point_id": "x", "kind": "track", "name": "Seed", "source_rank": True}]}),
    ("/api/digs", {"seeds": [{"point_id": "x", "kind": "track", "name": "Seed", "indexed": 1}]}),
    ("/api/steps", {"origins": [{"point_id": "x", "kind": "track", "name": "Seed"}], "move": "track", "further": "false"}),
])
def test_bools_and_strings_cannot_masquerade_as_typed_values(slice_app, path, body):
    assert slice_app.http.post(path, json=body).status_code == 422
    with closing(storage.connect(slice_app.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0


def test_parent_rank_fact_and_keeps_membership_are_revalidated_before_persistence(slice_app):
    world = slice_app
    initial = root(world)
    snap = finished(world, initial)["snapshot"]
    pick = snap["picks"][0]
    point = from_pick(initial["step_id"], pick).to_dict()
    body = {"parent_step_id": initial["step_id"], "origins": [point], "move": "track", "further": False}
    for change in ({"source_step_id": "missing"}, {"source_rank": 999}, {"cosine_id": "forged"}, {"point_id": "track:forged"}):
        assert world.http.post("/api/steps", json={**body, "origins": [{**point, **change}]}).status_code == 422
    assert world.http.post("/api/steps", json={**body, "parent_step_id": "missing"}).status_code == 422
    other = root(world)
    finished(world, other)
    assert world.http.post("/api/steps", json={**body, "parent_step_id": other["step_id"]}).status_code == 422
    fake_fact = Point(point_id="label:999", kind="label", name="Fake Label", discogs_id="999",
                      source_step_id=initial["step_id"], source_rank=pick["rank"])
    assert world.http.post("/api/steps", json={**body, "origins": [fake_fact.to_dict()], "move": "label"}).status_code == 422
    assert world.http.post("/api/steps", json={**body, "move": "these", "scope": "step", "requested_count": 1}).status_code == 422
    assert world.http.get(f"/api/digs/{initial['dig_id']}").json()["step_count"] == 1
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 2


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "file:///etc/passwd", "https://discogs.com.evil.test/release/1",
    "https://www.discogs.com@evil.test/release/1", "https://cosine.club/track/../secret", "https://youtube.com/other", "https://cosine.club:443/track/seed"])
def test_arbitrary_links_never_start_jobs_or_fetch_urls(slice_app, url):
    world = slice_app
    assert world.http.get("/api/search", params={"q": url}).status_code == 422
    point = Point(point_id="link:untrusted", kind="link", name="Link", url=url)
    assert world.http.post("/api/digs", json={"seeds": [point.to_dict()]}).status_code == 422
    assert not world.providers.calls
    assert world.http.get("/api/digs").json() == {"digs": []}


def test_durable_snapshots_journal_search_and_admission_errors_are_safe(slice_app, monkeypatch, capsys):
    world = slice_app
    # Identity metadata removes configured values; unknown credentials below
    # are exercised as provider error text, rather than invented song names.
    text = "Cookie: fixture-discogs-key\nBearer fixture-cosine-key"
    seed = world.providers.point("seed-a").model_copy(update={"name": text})
    initial = root(world, [seed], "Authorization: synthetic-request-secret")
    snap = finished(world, initial)
    surfaces = [world.http.get(f"/api/steps/{initial['step_id']}").text,
                world.http.get(f"/api/digs/{initial['dig_id']}").text,
                world.http.get(f"/api/jobs/{initial['job_id']}").text,
                world.http.get(f"/api/jobs/{initial['job_id']}/events").text]
    def fail(*args):
        raise RuntimeError("Authorization: synthetic-provider-secret\nCookie: synthetic-cookie")
    monkeypatch.setattr(world.providers, "cosine_client", fail)
    # create_app captured the bound factory; change the production forwarding collaborator.
    monkeypatch.setattr(world.app.state.slice_runtime, "client_factory", fail)
    failed = root(world)
    finished(world, failed, "error")
    surfaces += [world.http.get(f"/api/jobs/{failed['job_id']}").text,
                 world.http.get(f"/api/jobs/{failed['job_id']}/events").text]
    world.app.state.slice_runtime.stop_accepting()
    rejected = world.http.post("/api/digs", json={"seeds": [seed.to_dict()]})
    assert rejected.status_code == 503
    with closing(storage.connect(world.cfg.db_path)) as conn:
        surfaces += [" ".join(row[0] for row in conn.execute("SELECT payload FROM slice_events")),
                     " ".join(row[0] for row in conn.execute("SELECT progress FROM slice_jobs"))]
        assert conn.execute("SELECT COUNT(*) FROM digs").fetchone()[0] == 2
    surfaces.append(capsys.readouterr().out)
    assert "[redacted]" in "".join(surfaces)
    for secret in ("synthetic-cookie", "fixture-discogs-key", "fixture-cosine-key", "synthetic-provider-secret", "synthetic-request-secret"):
        assert all(secret not in value for value in surfaces)


def test_redaction_preserves_persisted_identity_and_search(slice_app):
    # Review C6: ordinary music metadata can resemble a credential header.
    class MetadataClient:
        def __init__(self):
            self.queries = []
            self.source = Track(id="source", name="Seed Artist - Token: Dawn",
                                artist="Seed Artist", track="Token: Dawn")
            self.pick = Track(id="neighbor", name="Band - The Bearer Of Light",
                              artist="Token: Ensemble", track="The Bearer Of Light", score=0.95)

        def search(self, query, **kwargs):
            self.queries.append(query)
            if query == "known-metadata":
                return [Track(id="known", name="Configured value", artist=cfg.cosine_token, track="Token: Remix")]
            return [self.pick] if query == self.pick.track else []

        def lookup(self, url):
            return []

        def similar(self, track_id, **kwargs):
            return SimilarTracksData(source_track=self.source, similar_tracks=[self.pick])

        def close(self):
            pass

    provider = MetadataClient()
    cfg = replace(slice_app.cfg, db_path=slice_app.cfg.home / "identity.db", discogs_token=None)
    with TestClient(create_app(cfg, client_factory=lambda: provider), base_url="http://localhost") as client:
        response = client.post("/api/digs", json={"seeds": [Point(
            point_id="track:seed artist|token: dawn", kind="track", name=provider.source.name,
            artist=provider.source.artist, title=provider.source.track,
            track_key=api.track_key(provider.source), cosine_id="source").to_dict()]})
        assert response.status_code == 200, response.text
        identity = response.json()
        runtime = client.app.state.slice_runtime
        eventually(lambda: runtime.job_snapshot(identity["job_id"])["state"] != "running")
        assert runtime.job_snapshot(identity["job_id"])["state"] == "done"
        saved = client.get(f"/api/steps/{identity['step_id']}").json()["snapshot"]
        assert saved["step"]["origins"][0]["title"] == "Token: Dawn"
        assert saved["step"]["origins"][0]["resolved_tracks"][0]["title"] == "Token: Dawn"
        assert saved["step"]["header"] == "Sounds like Seed Artist - Token: Dawn"
        with closing(storage.connect(cfg.db_path)) as conn:
            assert conn.execute("SELECT artist,title,track_key FROM step_picks WHERE step_id=?",
                                (identity["step_id"],)).fetchone() == (
                                    "Token: Ensemble", "The Bearer Of Light", api.track_key(provider.pick))
            with conn:
                row_id = conn.execute("INSERT INTO tracks(path,artist,title) VALUES ('synthetic-row','Band','Token: Remix')").lastrowid
        [item] = client.get("/api/search", params={"q": provider.pick.track, "source": "cosine"}).json()["items"]
        assert (item["artist"], item["title"], item["track_key"]) == (
            "Token: Ensemble", "The Bearer Of Light", api.track_key(provider.pick))
        protected = client.get("/api/search", params={"q": "known-metadata", "source": "cosine"})
        assert cfg.cosine_token not in protected.text
        assert protected.json()["items"][0]["artist"] == "[redacted]"
        assert protected.json()["items"][0]["title"] == "Token: Remix"
        search = client.get("/api/search", params={"q": "Token", "source": "library"}).json()
        assert search["items"][0]["title"] == "Token: Remix"
        response = client.post("/api/digs", json={"seeds": [Point(
            point_id=f"library:{row_id}", kind="track", name="Token: Remix", library_id=row_id).to_dict()]})
        assert response.status_code == 200, response.text
        library = response.json()
        eventually(lambda: runtime.job_snapshot(library["job_id"])["state"] != "running")
        assert runtime.job_snapshot(library["job_id"])["state"] == "done"
        origin = client.get(f"/api/steps/{library['step_id']}").json()["snapshot"]["step"]["origins"][0]
        assert (origin["name"], origin["artist"], origin["title"]) == ("Band - Token: Remix", "Band", "Token: Remix")
        assert "Band - Token: Remix" in provider.queries
        assert "Band - Token: [redacted]" not in provider.queries
        events = client.get(f"/api/jobs/{identity['job_id']}/events").text
        assert "The Bearer Of Light" in events and "token: ensemble|the bearer of light" in events
        # The canonical identity returned to the UI still targets a real row.
        judgment = client.put("/api/judgments/" + saved["picks"][0]["track_key"], json={
            "step_id": identity["step_id"], "verdict": "keep", "expected_revision": 0})
        assert judgment.status_code == 200


def test_slice_notes_progress_and_errors_keep_credential_redaction(slice_app, capsys):
    world = slice_app
    runtime = world.app.state.slice_runtime

    def runner(*, on_progress, **kwargs):
        on_progress({"code": "synthetic-progress", "phase": "enrichment",
                     "params": {"seed": "The Bearer Of Light", "label": "Token: Records",
                                "artist": f"Artist {world.cfg.cosine_token}"},
                     "message": "Authorization: synthetic-unknown-auth\nCookie: synthetic-unknown-cookie",
                     "progress": {"message": "Bearer synthetic-unknown-progress", "cursor": 1}})
        return {"error": {"code": "synthetic-error", "message": "Bearer synthetic-unknown-error"}, "status": "failed"}

    job_id = runtime.submit_enrichment(budget=1, runner=runner)["job_id"]
    eventually(lambda: runtime.job_snapshot(job_id)["state"] == "error")
    with closing(storage.connect(world.cfg.db_path)) as conn:
        events = storage.slice_events(conn, job_id)
        note = events[0]["data"]
        assert note["params"]["seed"] == "The Bearer Of Light"
        assert note["params"]["label"] == "Token: Records"
        assert note["params"]["artist"] == "Artist [redacted]"
        stored = [row[0] for row in conn.execute("SELECT payload FROM slice_events WHERE job_id=?", (job_id,))]
        stored += [row[0] for row in conn.execute("SELECT progress FROM slice_jobs WHERE id=?", (job_id,))]
    surfaces = [*stored, world.http.get(f"/api/jobs/{job_id}").text,
                world.http.get(f"/api/jobs/{job_id}/events").text, capsys.readouterr().out]
    for marker in (world.cfg.cosine_token, "synthetic-unknown-auth", "synthetic-unknown-cookie",
                   "synthetic-unknown-progress", "synthetic-unknown-error"):
        assert all(marker not in surface for surface in surfaces)
    assert all("[redacted]" in surface for surface in surfaces[:-1])


def connect(world, monkeypatch, **keys):
    """Save keys the way the browser does: each passes a (stand-in) provider check first."""
    for name, key in keys.items():
        monkeypatch.setitem(connections.CHECKS, name, lambda key: connections.Check("connected"))
        response = world.http.post(f"/api/connections/{name}", json={"key": key})
        assert response.status_code == 200 and response.json()["state"] == "connected", response.text
    return response.json()["settings"]


def test_settings_reach_new_jobs_and_fixture_selection_only_happens_at_startup(slice_app, monkeypatch):
    world = slice_app
    tokens = []
    factory = world.providers.discogs_client
    def provider(**kwargs):
        tokens.append(kwargs["token"])
        return factory(**kwargs)
    monkeypatch.setattr(world.app.state.slice_runtime, "discogs_client_factory", provider)
    settings = connect(world, monkeypatch, discogs_token="new-fixture-discogs-key", cosine_token="new-fixture-cosine-key")
    assert {key: settings["discogs_token"][key] for key in ("set", "hint")} == {"set": True, "hint": "…-key"}
    finished(world, root(world))
    assert tokens and set(tokens) == {"new-fixture-discogs-key"}
    assert world.app.state.slice_runtime.get_config().cosine_token == "new-fixture-cosine-key"
    # No query/header/env change can enable a fixture on an already running app.
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "unknown-fixture")
    assert world.http.get("/api/search?q=Seed&source=cosine&fixture=unknown",
                          headers={"COSINE_DIGGER_TEST_MODE": "1"}).status_code == 200
    finished(world, root(world))


@pytest.mark.parametrize("gate,selection", [(None, "unknown"), ("0", "slice-v1"), ("1", None), ("1", ""), ("1", "   ")])
def test_missing_blank_or_disabled_fixture_selection_keeps_normal_providers(slice_app, tmp_path, monkeypatch, gate, selection):
    for name, value in (("COSINE_DIGGER_TEST_MODE", gate), ("COSINE_DIGGER_FIXTURE", selection)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    providers = SliceFixtures()
    calls = []
    def client():
        calls.append(True)
        return providers.cosine_client()
    cfg = replace(slice_app.cfg, home=tmp_path / "normal", db_path=tmp_path / "normal" / "taste.db")
    with TestClient(create_app(cfg, client_factory=client, discogs_client_factory=providers.discogs_client), base_url="http://localhost") as http:
        assert http.get("/api/search?q=Seed&source=cosine").status_code == 200
    assert calls == [True]


def test_gated_fixture_startup_uses_real_routes_and_fails_closed(slice_app, tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "slice-v1")
    cfg = replace(slice_app.cfg, home=tmp_path / "gated", db_path=tmp_path / "gated" / "taste.db")
    def denied():
        pytest.fail("Explicit startup fixture must replace normal providers")
    app = create_app(cfg, client_factory=denied, discogs_client_factory=denied)
    with TestClient(app, base_url="http://localhost") as client:
        point = client.get("/api/search?q=Fixture%20Artist%20A%20-%20Seed%20A&source=cosine").json()["items"][0]
        initial = client.post("/api/digs", json={"seeds": [point]}).json()
        assert eventually(lambda: client.get(f"/api/jobs/{initial['job_id']}").json()["state"] == "done")
        assert client.get(f"/api/steps/{initial['step_id']}").json()["snapshot"]["picks"]
    for host, scheduler, selection in (("0.0.0.0", False, "slice-v1"), ("127.0.0.1", True, "slice-v1"), ("127.0.0.1", False, "unknown")):
        monkeypatch.setenv("COSINE_DIGGER_FIXTURE", selection)
        with pytest.raises(FixtureConfigurationError):
            with TestClient(create_app(replace(cfg, host=host), scheduler=scheduler)):
                pass


def test_enrichment_checkpoints_redact_in_flight_recovery_and_resume(slice_app, monkeypatch, capsys):
    world = slice_app
    runtime = world.app.state.slice_runtime
    # These first two identities collide if resumed from redacted metadata.
    pairs = [(f"Artist {world.cfg.cosine_token}", "Album"),
             (f"Artist {world.cfg.discogs_token}", "Album"),
             ("Pending Artist", f"Album {world.cfg.cosine_token}")]
    with closing(storage.connect(world.cfg.db_path)) as conn:
        with conn:
            conn.executemany("INSERT INTO tracks(path,artist,album,title,mtime) VALUES (?,?,?,?,?)",
                             [(f"synthetic-{i}", artist, album, "Track", f"2026-09-0{3-i}")
                              for i, (artist, album) in enumerate(pairs)])

    class Interrupted(BaseException):
        pass

    entered, release = threading.Event(), threading.Event()
    calls = []

    def respond(request):
        calls.append(request.url.params["q"])
        if len(calls) == 2:
            entered.set()
            assert release.wait(5)
            raise Interrupted
        results = [] if len(calls) == 1 else [
            {"id": 42, "title": " - ".join(pairs[1 if len(calls) == 3 else 2]),
             "label": ["Safe Label"]}]
        return httpx.Response(200, json={"results": results})

    def provider(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond)), chain.BudgetTracker(kwargs["budget"])

    original_run = runtime._run_enrichment

    def interrupted_run(worker, runner):
        try:
            original_run(worker, runner)
        except Interrupted:
            # Simulate a killed worker without an unhandled-thread warning.
            pass

    monkeypatch.setattr(runtime, "_run_enrichment", interrupted_run)
    monkeypatch.setattr(runtime, "discogs_client_factory", provider)
    first = world.http.post("/api/library/labels/enrich", json={"budget": 100}).json()["job_id"]
    try:
        assert entered.wait(5)
        in_flight = runtime.job_snapshot(first)
        surfaces = [json.dumps(in_flight), world.http.get(f"/api/jobs/{first}").text]
        assert in_flight["progress"]["work"]["processed_pairs"] == 1
        assert in_flight["progress"]["work"]["requests_used"] == 2
    finally:
        release.set()
    assert eventually(lambda: first not in runtime._jobs)
    runtime._recover(first)
    assert runtime.job_snapshot(first)["state"] == "stopped"
    surfaces += [world.http.get(f"/api/jobs/{first}").text,
                 world.http.get(f"/api/jobs/{first}/events").text]

    response = world.http.post("/api/library/labels/enrich", json={"budget": 100, "resume_job_id": first})
    assert response.status_code == 200
    second = response.json()["job_id"]
    done = eventually(lambda: (job if (job := runtime.job_snapshot(second))["state"] != "running" else None))
    assert done["state"] == "done"
    assert second != first and done["attempt"] == 2
    assert calls == [" ".join(pairs[i]) for i in (0, 1, 1, 2)]
    assert {key: done["result"][key] for key in
            ("processed_pairs", "total_pairs", "filled_tracks", "missing_tracks", "requests_used", "requests_total")} == {
                "processed_pairs": 3, "total_pairs": 3, "filled_tracks": 2,
                "missing_tracks": 1, "requests_used": 2, "requests_total": 4}
    surfaces += [world.http.get(f"/api/jobs/{second}").text,
                 world.http.get(f"/api/jobs/{second}/events").text]
    with closing(storage.connect(world.cfg.db_path)) as conn:
        surfaces += [" ".join(row[0] for row in conn.execute("SELECT progress FROM slice_jobs")),
                     " ".join(row[0] for row in conn.execute("SELECT payload FROM slice_events"))]
    captured = capsys.readouterr()
    surfaces += [captured.out, captured.err]
    assert all(secret not in surface for secret in (world.cfg.cosine_token, world.cfg.discogs_token)
               for surface in surfaces)


@pytest.mark.asyncio
async def test_search_omits_secrets_from_uvicorn_response_access_logs(slice_app, monkeypatch):
    world = slice_app
    connect(world, monkeypatch, exa_api_key="synthetic-exa-key")
    # Search strings use known-value redaction, including old configured keys.
    # Register the cookie marker too; credential heuristics belong to messages.
    connect(world, monkeypatch, exa_api_key="synthetic-youtube-cookie")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)

    class Transport(asyncio.Transport):
        def __init__(self):
            self.body = bytearray()
            self.closed = asyncio.Event()

        def get_extra_info(self, name, default=None):
            return {"peername": ("127.0.0.1", 12345), "sockname": ("127.0.0.1", 9876)}.get(name, default)

        def write(self, data):
            self.body.extend(data)

        def is_closing(self):
            return self.closed.is_set()

        def close(self):
            self.closed.set()

    secrets = [world.cfg.cosine_token, world.cfg.discogs_token, "synthetic-exa-key", "synthetic-youtube-cookie"]
    queries = [*secrets[:3], f"Cookie: {secrets[3]}"]
    for query in queries:
        transport = Transport()
        state = ServerState()
        protocol = H11Protocol(uvicorn.Config(world.app, log_config=None, lifespan="off"), state, {})
        protocol.connection_made(transport)
        target = "/api/search?" + urlencode({"q": query, "source": "cosine"})
        protocol.data_received(f"GET {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        try:
            await asyncio.wait_for(transport.closed.wait(), 5)
            await asyncio.gather(*state.tasks)
            assert b"HTTP/1.1 200" in transport.body
            assert all(secret.encode() not in transport.body for secret in secrets)
        finally:
            protocol.connection_lost(None)
    logs = output.getvalue()
    assert logs.count("GET /api/search HTTP/1.1") == len(queries)
    assert "?" not in logs and all(secret not in logs for secret in secrets)


@pytest.fixture
def serve_launch(tmp_path, monkeypatch):
    from cosine_digger.cli import app
    from uvicorn.importer import import_from_string

    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    for name in ("COSINE_DIGGER_HOST", "COSINE_DIGGER_PORT", "COSINE_DIGGER_TEST_MODE", "COSINE_DIGGER_FIXTURE",
                 "COSINE_API_TOKEN", "DISCOGS_TOKEN", "EXA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COSINE_API_TOKEN", "synthetic-serve-cosine-key")
    monkeypatch.setenv("DISCOGS_TOKEN", "synthetic-serve-discogs-key")
    launches, configs, scheduled = [], [], []

    class Scheduler:
        def __init__(self, cfg, runner, db_path):
            scheduled.append(cfg)

        def start(self):
            pass

        def stop(self):
            pass

    def run(application, **kwargs):
        launches.append(kwargs)
        instance = import_from_string(application)() if kwargs.get("factory") else application
        with TestClient(instance, base_url="http://localhost") as client:
            configs.append(instance.state.slice_runtime.get_config())
            assert client.get("/api/health").status_code == 200
            if os.environ.get("COSINE_DIGGER_TEST_MODE") == "1":
                response = client.get("/api/search?q=Fixture%20Artist%20A%20-%20Seed%20A&source=cosine")
                assert response.status_code == 200 and response.json()["items"][0]["cosine_id"] == "seed-a"

    monkeypatch.setattr("cosine_digger.web.app.schedule.SchedulerLoop", Scheduler)
    monkeypatch.setattr("cosine_digger.commands.serve.uvicorn.run", run)
    monkeypatch.setattr("cosine_digger.commands.serve.run_server", lambda application, **kwargs: run(
        application, **{key: kwargs[key] for key in ("host", "port", "log_level")}) or True)
    # No real port is probed: these launches never bind one.
    monkeypatch.setattr("cosine_digger.runtime.Identity.announce", lambda *args, **kwargs: None)
    return app, launches, configs, scheduled


@pytest.mark.parametrize("reload", [False, True])
@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1"])
def test_serve_refuses_fixture_non_loopback_before_uvicorn_launch(serve_launch, monkeypatch, reload, host):
    app, launches, _, _ = serve_launch
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "slice-v1")
    options = ["serve", "--host", host, "--no-scheduler"] + (["--reload"] if reload else [])
    result = CliRunner().invoke(app, options)
    assert isinstance(result.exception, FixtureConfigurationError)
    assert launches == []


@pytest.mark.parametrize("selection", ["slice-v1", "unknown"])
def test_fixture_cli_doctor_never_sends_configured_token(serve_launch, monkeypatch, selection):
    app, _, _, _ = serve_launch
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", selection)
    calls = []

    def intercepted(url, **kwargs):
        calls.append(url)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", intercepted)
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert calls == []
    check = json.loads(result.output)["checks"]["cosine_token"]
    assert check["status"] == "warn" and check["detail"] == "set (test fixture; API not checked)"
    assert "synthetic-serve-cosine-key" not in result.output


@pytest.mark.parametrize("fixture", [False, True])
def test_serve_reload_factory_uses_effective_bind(serve_launch, monkeypatch, fixture):
    app, launches, configs, scheduled = serve_launch
    if fixture:
        monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
        monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "slice-v1")
    result = CliRunner().invoke(app, ["serve", "--reload", "--host", "localhost", "--port", "9876"])
    assert result.exit_code == 0, result.output
    assert launches[0]["host"] == configs[0].host == "localhost"
    assert launches[0]["port"] == configs[0].port == 9876
    assert launches[0]["reload"] is True and launches[0]["factory"] is True
    assert scheduled == []
    assert "COSINE_DIGGER_HOST" not in os.environ and "COSINE_DIGGER_PORT" not in os.environ


@pytest.mark.parametrize("options,scheduler", [([], True), (["--no-scheduler"], False), (["--reload"], False)])
def test_serve_preserves_default_bind_and_scheduler(serve_launch, options, scheduler):
    app, launches, configs, scheduled = serve_launch
    result = CliRunner().invoke(app, ["serve", *options])
    assert result.exit_code == 0, result.output
    assert launches[0]["host"] == configs[0].host == "127.0.0.1"
    assert launches[0]["port"] == configs[0].port == 8765
    assert bool(scheduled) is scheduler
