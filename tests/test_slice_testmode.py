"""S9 provider integration through S1/S3/S4, before S6/S10 route integration.

All provider HTTP uses MockTransport. The small SQLite adapter below only
records real origin/chain output; there is no fake-result HTTP endpoint.
"""

from contextlib import closing
import builtins
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi.testclient import TestClient

from cosine_digger import api, chain, client, discogs, seeds, storage, taste
from cosine_digger.config import AppConfig, load_config
from cosine_digger.models import Track
from cosine_digger.playlists import load_exclusions
from cosine_digger.profile import build_feed_profile
from cosine_digger.slice_types import MoveBatch, Resolution
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import (
    FixtureConfigurationError,
    SliceFixtures,
    fixture_from_environment,
)

FIND_RELEASE = seeds.discogs_find_release
ENABLED = {"COSINE_DIGGER_TEST_MODE": "1", "COSINE_DIGGER_FIXTURE": "slice-v1"}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.setattr(client, "load_env", lambda: None)
    monkeypatch.setattr(seeds, "discogs_find_release", FIND_RELEASE)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.delenv("COSINE_DIGGER_TEST_MODE", raising=False)
    monkeypatch.delenv("COSINE_DIGGER_FIXTURE", raising=False)
    monkeypatch.delenv("COSINE_DIGGER_ALLOWED_HOSTS", raising=False)
    waits = []
    monkeypatch.setattr(discogs, "pace", lambda backoff=0: waits.append(backoff))
    monkeypatch.setattr(client.time, "sleep", lambda seconds: waits.append(seconds))

    def denied(request):
        raise AssertionError("S9 tests must never make real network calls")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: denied(request))
    yield waits
    if discogs.FactsQueue._shared is not None:
        discogs.FactsQueue._shared.close()


@pytest.fixture
def world(tmp_path):
    fixtures = SliceFixtures()
    cosine = fixtures.cosine_client()
    dg, tracker = fixtures.discogs_client(budget=500)
    path = tmp_path / "taste.db"
    with closing(storage.connect(path)) as conn:
        storage.migrate(conn)
        with conn:
            for row in fixtures.library_rows:
                columns = ",".join(row)
                conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({','.join('?' for _ in row)})",
                             tuple(row.values()))
        value = SimpleNamespace(fixtures=fixtures, cosine=cosine, discogs=dg, tracker=tracker,
                                conn=conn, path=path)
        try:
            yield value
        finally:
            fixtures.release_all()
            cosine.close()
            dg.close()


def emit(world, run, event, data):
    with world.conn:
        world.conn.execute("BEGIN IMMEDIATE")
        return storage.append_slice_event(world.conn, run.identity.job_id, event, data,
                                          redact=lambda value: value)


def save_batch(world, run, batch):
    assert MoveBatch.model_validate(batch.to_dict()) == batch
    start = len(run.candidates) + 1
    emit(world, run, "batch", {"hop": batch.hop, "start_rank": start, "target_count": 25,
                              "label": "near" if batch.hop == 0 else "further"})
    for offset, candidate in enumerate(batch.candidates):
        item = candidate.ranked.track
        run.candidates.append(candidate)
        emit(world, run, "pick", {"pick": {"rank": start + offset, "track_key": api.track_key(item),
            "cosine_id": item.id, "artist": item.artist, "title": item.track, "video_id": item.video_id,
            "score": candidate.ranked.final_score, "batch": batch.hop, "route": candidate.route,
            "facts": candidate.facts.to_dict() if candidate.facts else None,
            "facts_state": candidate.facts_state}})
    for note in batch.notes:
        emit(world, run, "note", note)


def start(world, origins=None, *, move="seed", further=False, discogs_enabled=True, parent=None):
    origins = origins or world.fixtures.seed_points
    dg = world.discogs if discogs_enabled else None
    resolution = seeds.resolve_move_origins(origins, move, client=world.cosine, discogs_client=dg)
    assert Resolution.model_validate(resolution.to_dict()) == resolution
    with world.conn:
        world.conn.execute("BEGIN IMMEDIATE")
        dig_id = parent.identity.dig_id if parent else storage.create_dig(world.conn, origins)
        identity = storage.create_step(world.conn, dig_id=dig_id,
            parent_id=parent.identity.step_id if parent else None, origins=origins, move=move, further=further)
        storage.resolve_step_origins(world.conn, identity.step_id, resolution.origins)
    exclusions = load_exclusions(world.conn, build_feed_profile(world.conn), scope="slice")
    iterator = chain.iter_move_batches(resolution, world.cosine, discogs_client=dg,
                                      exclusions=exclusions, further=further)
    run = SimpleNamespace(identity=identity, resolution=resolution, iterator=iterator,
                          candidates=[], discogs_enabled=discogs_enabled)
    for note in resolution.notes:
        emit(world, run, "note", note)
    save_batch(world, run, next(iterator))
    return run


def snapshot(world, run):
    return storage.get_step_snapshot(world.conn, run.identity.step_id)


@pytest.mark.parametrize("mode", [None, ""])
def test_fixture_env_is_ignored_without_exact_gate(mode):
    env = {"COSINE_DIGGER_FIXTURE": "unknown"}
    if mode is not None:
        env["COSINE_DIGGER_TEST_MODE"] = mode
    assert fixture_from_environment(host="0.0.0.0", scheduler=True, environ=env) is None
    assert fixture_from_environment(host="127.0.0.1", scheduler=False,
                                    environ={"COSINE_DIGGER_TEST_MODE": "1"}) is None


@pytest.mark.parametrize("host,scheduler", [("0.0.0.0", False), ("::", False), ("192.168.1.1", False),
    ("example.com", False), ("127.0.0.1.example.com", False), ("127.0.0.1:9000", False),
    ("127.0.0.1", True), ("127.0.0.1", None), ("127.0.0.1", 0)])
def test_gated_startup_refuses_nonloopback_or_scheduler(host, scheduler):
    with pytest.raises(FixtureConfigurationError, match="loopback binding and a disabled scheduler"):
        fixture_from_environment(host=host, scheduler=scheduler, environ=ENABLED)


def test_unknown_gated_fixture_refuses_startup_without_echoing_value(monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "untrusted-fixture-value")
    with pytest.raises(FixtureConfigurationError, match="^Unknown test fixture$"):
        fixture_from_environment(host="127.0.0.1", scheduler=False)


@pytest.mark.parametrize("outside", ["default", "home", "db", "home-symlink", "db-symlink"])
def test_fixture_startup_refuses_persistent_storage(tmp_path, monkeypatch, outside):
    # Review C1: mimic the configured user library without opening a real home.
    temp_root = tmp_path / "os-temp"
    temp_root.mkdir()
    persistent_home = tmp_path / "fake-user" / ".cosine-digger"
    persistent_home.mkdir(parents=True)
    home, db = temp_root / "home", temp_root / "home" / "taste.db"
    if outside in {"default", "home"}:
        home = persistent_home
    if outside in {"default", "db"}:
        db = persistent_home / "taste.db"
    if outside == "home-symlink":
        home = temp_root / "home-link"
        home.symlink_to(persistent_home, target_is_directory=True)
    if outside == "db-symlink":
        db = temp_root / "db-link" / "taste.db"
        db.parent.symlink_to(persistent_home, target_is_directory=True)
    environment = {"HOME": str(persistent_home.parent), "COSINE_DIGGER_HOME": str(home),
                   "COSINE_API_TOKEN": "fixture-cosine-key", "DISCOGS_TOKEN": "fixture-discogs-key"}
    if outside != "default":
        environment["COSINE_DIGGER_DB"] = str(db)
    cfg = load_config(environment)
    assert cfg.db_path == db
    tables = ("tracks", "digs", "steps", "slice_jobs", "step_picks", "http_cache")
    with closing(storage.connect(db)) as conn:
        storage.migrate(conn)
        with conn:
            conn.execute("INSERT INTO tracks(path,artist,title) VALUES ('sentinel','Existing','Owned')")
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    for name, value in ENABLED.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(FixtureConfigurationError, match="^Test fixtures require temporary home and database paths$"):
        with TestClient(create_app(cfg), base_url="http://localhost") as http:
            point = http.get("/api/search", params={"q": "Fixture Artist A - Seed A", "source": "cosine"}).json()["items"][0]
            response = http.post("/api/digs", json={"seeds": [point]})
            assert response.status_code == 200
            assert "event: done" in http.get(f"/api/jobs/{response.json()['job_id']}/events").text
    with closing(storage.connect(db)) as conn:
        assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before


@pytest.mark.parametrize("keep_environment", [True, False])
@pytest.mark.parametrize("video_id", ["abcdefghijk"])
def test_fixture_oembed_and_doctor_never_escape(tmp_path, monkeypatch, keep_environment, video_id):
    # Review C4: intercept both direct HTTP paths; never send the request.
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db",
                    ytmusic_auth_path=tmp_path / "absent-auth.json",
                    cosine_token="fixture-cosine-key", discogs_token="fixture-discogs-key")
    for name, value in ENABLED.items():
        monkeypatch.setenv(name, value)
    calls = []

    def intercepted(url, **kwargs):
        authorized = kwargs.get("headers", {}).get("Authorization") == f"Bearer {cfg.cosine_token}"
        calls.append((url, authorized))
        return httpx.Response(200, json={"title": "Unindexed Artist - Unindexed Song"})

    monkeypatch.setattr(httpx, "get", intercepted)
    with TestClient(create_app(cfg), base_url="http://localhost") as http:
        if not keep_environment:
            for name in ENABLED:
                monkeypatch.delenv(name)
        response = http.post("/api/digs", json={"seeds": [{"point_id": "link:test", "kind": "link",
            "name": "Unknown link", "url": f"https://youtu.be/{video_id}"}]})
        assert response.status_code == 200
        identity = response.json()
        assert "event: done" in http.get(f"/api/jobs/{identity['job_id']}/events").text
        doctor = http.get("/api/doctor")
        assert doctor.status_code == 200
        assert calls == []
        check = doctor.json()["checks"]["cosine_token"]
        assert check == {"status": "warn", "detail": "set (test fixture; API not checked)", "source": "default"}
        assert cfg.cosine_token not in doctor.text and cfg.discogs_token not in doctor.text
        saved = http.get(f"/api/steps/{identity['step_id']}").json()["snapshot"]
        expected = ["seed-a"] if video_id == "S9OEMBED001" else []
        assert [item["cosine_id"] for item in saved["step"]["origins"][0]["resolved_tracks"]] == expected
        assert len(saved["picks"]) == (25 if expected else 0)


@pytest.mark.parametrize("keep_environment", [True])
def test_fixture_legacy_jobs_deny_all_provider_requests(tmp_path, monkeypatch, keep_environment):
    # FX13: repeat the reviewer's authenticated export through every legacy
    # route. Record only provider hosts, never headers, keys or cookies.
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"authorization": "SAPISIDHASH fixture",
        "cookie": "__Secure-3PAPISID=fixture-cookie", "origin": "https://music.youtube.com",
        "x-goog-visitor-id": "fixture-visitor"}))
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=auth,
                    cosine_token="synthetic-cosine", discogs_token="synthetic-discogs",
                    exa_api_key="synthetic-exa", daily_size=5, weekly_size=5)
    for name, value in {**ENABLED, "COSINE_API_TOKEN": cfg.cosine_token,
                        "DISCOGS_TOKEN": cfg.discogs_token, "EXA_API_KEY": cfg.exa_api_key}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    attempts, auth_reads, fixture_worlds = [], [], []

    def deny_httpx(self, request):
        attempts.append(("httpx", request.url.host))
        raise httpx.ConnectError("External provider request denied", request=request)

    def deny_requests(self, request, **kwargs):
        attempts.append(("requests", requests.utils.urlparse(request.url).hostname))
        raise requests.ConnectionError("External provider request denied")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_httpx)
    monkeypatch.setattr(requests.Session, "send", deny_requests)
    real_open = builtins.open

    def record_auth_read(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file) == auth and "r" in mode:
            auth_reads.append("auth.json")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", record_auth_read)
    fixture_discogs = SliceFixtures.discogs_client

    def observe_fixture_discogs(self, *args, **kwargs):
        if self not in fixture_worlds:
            fixture_worlds.append(self)
        return fixture_discogs(self, *args, **kwargs)

    monkeypatch.setattr(SliceFixtures, "discogs_client", observe_fixture_discogs)
    with closing(api.open_db(cfg.db_path)) as conn:
        api.import_library(conn, [{"id": "seed-a", "artist": "Fixture Artist A", "title": "Seed A"},
                                  {"id": "pending", "artist": "Library Artist B", "title": "Pending File"}],
                           source="test-fixture")
        session_id = api.record_dig(conn, ["Fixture Artist A - Seed A"], [api.Pick(
            Track(id="fixture-export", artist="Export Artist", track="Export Track", name="Export Track",
                  video_id="S9EXPORT001"), 1, "Fixture export")])

    def finish_job(http, response):
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        events = http.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200
        assert "event: done" in events.text or "event: error" in events.text
        return http.get(f"/api/jobs/{job_id}").json()

    with TestClient(create_app(cfg), base_url="http://localhost") as http:
        if not keep_environment:
            for name in ENABLED:
                monkeypatch.delenv(name)
        feeds, weekly_label_hops = {}, []
        for dry_run in (True, False):
            for kind in ("daily", "weekly"):
                before = sum(path == "/labels/501/releases" for world in fixture_worlds
                             for provider, path, _ in world.calls if provider == "discogs")
                feeds[kind, dry_run] = finish_job(http, http.post(
                    f"/api/playlists/{kind}/run", json={"dry_run": dry_run}))
                if kind == "weekly":
                    after = sum(path == "/labels/501/releases" for world in fixture_worlds
                                for provider, path, _ in world.calls if provider == "discogs")
                    weekly_label_hops.append(after > before)
        exported = finish_job(http, http.post("/api/export", json={"session_id": session_id,
                                                                 "name": "Fixture export"}))
        jobs_before = set(http.app.state.jobs)
        retired = http.post("/api/dig", json={"seeds": ["Fixture Artist A - Seed A"],
                                             "deep": True, "export": True})
        assert retired.status_code == 404
        assert set(http.app.state.jobs) == jobs_before
        with closing(storage.connect(cfg.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
        login = http.post("/api/youtube/login", json={"headers":
            "authorization: SAPISIDHASH fixture\ncookie: __Secure-3PAPISID=fixture-login-cookie\n"
            "origin: https://music.youtube.com\nx-goog-authuser: 0"})
        cards = http.get("/api/playlists").json()

    assert attempts == []
    assert auth_reads == []
    assert weekly_label_hops == [True, True]
    playlist_id = "PLsliceFixture"
    playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    for (kind, dry_run), job in feeds.items():
        assert job["state"] == "done"
        result = job["result"]
        assert result["tracks"] and result["fresh"] == {"skipped": "no EXA_API_KEY"}
        if dry_run:
            assert result["export"] is None
        else:
            assert result["playlist_url"] == playlist_url
            assert result["export"]["playlist_id"] == playlist_id
            assert result["export"]["verified"] is True
            assert cards[kind]["playlist_url"] == playlist_url
            assert cards[kind]["last_run"]["status"] == "ok"
    assert exported["state"] == "done"
    assert exported["result"]["export"]["verified"] is True
    assert exported["result"]["export"]["playlist_id"] == playlist_id
    assert exported["result"]["playlist_url"] == playlist_url
    assert login.status_code == 200
    reply = login.json()
    # The reply carries the server's settings: a login saved in test mode has no verdict, and
    # Log in from Chrome isn't offered (tests point its Chrome folder at an empty one).
    assert reply.pop("settings")["youtube"] == {"logged_in": True, "saved": True, "checked": None, "chrome": False}
    assert reply == {"ok": True, "logged_in": True, "state": "saved"}


def test_requests_headers_and_json_cannot_enable_fixtures_or_bypass_security(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "slice-v1")
    selected = fixture_from_environment(host="127.0.0.1", scheduler=False)
    assert selected is None
    calls = []

    def normal_client():
        calls.append("normal")
        raise RuntimeError("Normal provider sentinel")

    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db",
                    ytmusic_auth_path=tmp_path / "absent-auth.json")
    app = create_app(cfg, scheduler=False, client_factory=normal_client)
    headers = {"X-Cosine-Digger-Test-Mode": "1", "X-Cosine-Digger-Fixture": "slice-v1"}
    url = "/api/digs?test_mode=1&fixture=slice-v1&COSINE_DIGGER_TEST_MODE=1&COSINE_DIGGER_FIXTURE=slice-v1"
    seed = {"point_id": "track:seed-a", "kind": "track", "name": "Fixture Artist A - Seed A",
            "artist": "Fixture Artist A", "title": "Seed A"}
    with TestClient(app, base_url="http://127.0.0.1") as http:
        assert http.get("/api/health", headers=headers).status_code == 200
        assert http.get("/api/health", headers=headers | {"Host": "outside.invalid"}).status_code == 403
        assert http.post(url, headers=headers | {"Origin": "https://outside.invalid"}, json={}).status_code == 403
        assert http.post(url, headers=headers, content="{}").status_code == 415
        assert http.post(url, headers=headers, json={"seeds": [seed], "test_mode": True,
                                                    "fixture": "slice-v1"}).status_code == 422
        response = http.post(url, headers=headers, json={"seeds": [seed]})
        assert response.status_code == 200
        events = http.get(f"/api/jobs/{response.json()['job_id']}/events", headers=headers).text
        assert "event: error" in events
        job = http.get(f"/api/jobs/{response.json()['job_id']}", headers=headers).json()
        assert job["state"] == "error" and job["error"]["code"] == "move_failed"
    assert calls == ["normal"]
    assert fixture_from_environment(host="127.0.0.1", scheduler=False) is None
