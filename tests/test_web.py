"""Exercise the HTTP boundary through real digging, persistence, and profiles."""
from contextlib import closing
from dataclasses import replace
import json
import os
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from cosine_digger import api
from cosine_digger.config import AppConfig, load_config
from cosine_digger.models import SimilarTracksData, Track
from cosine_digger.web import connections
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import SliceFixtures
from cosine_digger.youtube import ExportReport


class Cosine:
    def search(self, query, limit=20):
        if query == "Seed Artist - First":
            return [Track(id="seed", name="First", track="First", artist="Seed Artist", video_id="vseed")]
        return []

    def lookup(self, url):
        return []

    def similar(self, track_id, limit=50, **filters):
        return SimilarTracksData(
            source_track=Track(id=track_id, name="First", artist="Seed Artist", track="First"),
            similar_tracks=[Track(id=str(i), name=f"Track {i}", track=f"Track {i}",
                                  artist=f"Artist {i}", video_id=f"v{i}", score=0.9 - i * 0.01)
                            for i in range(1, 8)])

    def close(self):
        pass


class Sink:
    def __init__(self, conn):
        self.store = api.DbIdStore(conn)

    def replace_playlist(self, name, video_ids, description="", **_):
        self.store[name] = "PLtest"
        return ExportReport(playlist_id="PLtest", playlist_url="https://music.youtube.com/playlist?list=PLtest",
                            created=True, requested=video_ids, added=video_ids, missing=[],
                            skipped=[], failed=[], verified=True)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    for key in ("COSINE_DIGGER_ALLOWED_HOSTS", "COSINE_DIGGER_TEST_MODE", "COSINE_DIGGER_FIXTURE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("cosine_digger.discogs.pace", lambda *args: None)
    return load_config({"COSINE_DIGGER_HOME": str(tmp_path),
                        "COSINE_DIGGER_DB": str(tmp_path / "taste.db"), "HOME": str(tmp_path)})


@pytest.fixture
def web(cfg):
    with TestClient(create_app(cfg, client_factory=Cosine, sink_factory=Sink),
                    base_url="http://localhost") as client:
        yield client


def recorded(cfg):
    with closing(api.open_db(cfg.db_path)) as conn:
        return api.quick(["Seed Artist - First"], client=Cosine(), conn=conn, export_playlist=False)


def feed_library(cfg):
    with closing(api.open_db(cfg.db_path)) as conn:
        api.import_library(conn, [{"id": "seed", "artist": "Seed Artist", "title": "First"}],
                           source="test-library")


def finish(web, response):
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    stream = web.get(f"/api/jobs/{job_id}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in stream.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return web.get(f"/api/jobs/{job_id}").json(), events


def track_point(artist="Seed Artist", title="First"):
    return {"point_id": f"track:{api._key(artist, title)}", "kind": "track",
            "track_key": api._key(artist, title), "name": f"{artist} - {title}", "artist": artist, "title": title}


def test_reads_and_cli_sessions(web, cfg):
    body = web.get("/api/health").json()
    assert {key: body[key] for key in ("ok", "version", "app", "launcher", "jobs")} == {
        "ok": True, "version": "0.4.0", "app": "cosine-digger", "launcher": "serve", "jobs": 0}
    assert body["pid"] == os.getpid() and len(body["instance"]) == 32
    result = recorded(cfg)
    profile = web.get("/api/profile").json()
    assert any(e["kind"] == "artist" and e["name"] == "Seed Artist" for e in profile["entities"])
    assert any(a["artist"] == "Seed Artist" and a["title"] == "First" for a in profile["anchors"])
    sessions = web.get("/api/sessions?limit=1").json()
    assert sessions[0]["id"] == result["session_id"] and sessions[0]["result_count"] == len(result["picks"])
    assert set(web.get("/api/doctor?offline=1").json()) == {"checks", "config"}
    assert set(web.get("/api/playlists").json()) == {"daily", "weekly"}


def test_fixture_routes_never_read_ambient_settings_or_construct_real_providers(tmp_path, monkeypatch):
    import httpx
    import requests

    from cosine_digger import chain, client, config, discogs, fresh, youtube

    ambient = tmp_path / "ambient-home"
    ambient.mkdir()
    (ambient / "settings.json").write_text(json.dumps({"discogs_token": "synthetic-ambient-secret"}))
    isolated = tmp_path / "fixture-home"
    isolated.mkdir()
    music = isolated / "generated-library"
    music.mkdir()
    (music / "Owned - File.mp3").write_bytes(b"")
    cfg = AppConfig(home=isolated, db_path=isolated / "taste.db", music_dir=music,
                    ytmusic_auth_path=isolated / "auth.json", cosine_token="fixture-only",
                    discogs_token="fixture-only", exa_api_key="fixture-only", daily_size=5, weekly_size=5)
    monkeypatch.setenv("COSINE_DIGGER_TEST_MODE", "1")
    monkeypatch.setenv("COSINE_DIGGER_FIXTURE", "slice-v1")
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(ambient))
    for key in ("DISCOGS_TOKEN", "COSINE_DIGGER_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    for module in (config, client, discogs):
        monkeypatch.setattr(module, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "pace", lambda *args: None)
    ambient_reads, real_providers, attempts = [], [], []
    read_settings = config._read_settings

    def observed_settings(home):
        if home == ambient:
            ambient_reads.append("ambient settings")
        return read_settings(home)

    def denied_provider(name):
        def deny(*args, **kwargs):
            real_providers.append(name)
            raise AssertionError(f"Real {name} provider denied")
        return deny

    def deny_httpx(self, request):
        attempts.append(("httpx", request.url.host))
        raise httpx.ConnectError("External provider request denied", request=request)

    def deny_requests(self, request, **kwargs):
        attempts.append(("requests", requests.utils.urlparse(request.url).hostname))
        raise requests.ConnectionError("External provider request denied")

    monkeypatch.setattr(config, "_read_settings", observed_settings)
    monkeypatch.setattr(client.CosineClient, "__init__", denied_provider("cosine.club"))
    monkeypatch.setattr(chain, "make_rate_limited_client", denied_provider("Discogs"))
    monkeypatch.setattr(youtube, "YTMusic", denied_provider("YouTube"))
    monkeypatch.setattr(youtube.YTMusicSink, "__init__", denied_provider("YouTube sink"))
    monkeypatch.setattr(fresh, "exa_search", denied_provider("Exa"))
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", denied_provider("HTTP transport"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_httpx)
    monkeypatch.setattr(requests.Session, "send", deny_requests)
    with closing(api.open_db(cfg.db_path)) as conn:
        api.import_library(conn, [{"id": "seed-a", "artist": "Fixture Artist A", "title": "Seed A"}],
                           source="test-library")
        cli_pick = SliceFixtures().point("seed-b")
        session_id = api.record_dig(conn, ["CLI seed"], [api.Pick(
            Track(id=cli_pick.cosine_id, name=cli_pick.title, artist=cli_pick.artist, track=cli_pick.title,
                  video_id=cli_pick.video_id), 1, "CLI result")])
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost") as web:
        assert web.get("/api/map").status_code == 404
        assert web.post("/api/dig", json={"seeds": ["Fixture Artist A - Seed A"], "export": False}).status_code == 404
        assert app.state.jobs == {}
        with closing(api.open_db(cfg.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 0
        for path in ("/", "/static/slice.js", "/api/health", "/api/profile", "/api/doctor", "/api/settings",
                     "/api/playlists", "/api/sessions", "/api/digs", "/api/library/labels"):
            assert web.get(path).status_code == 200
        assert web.put("/api/settings", json={"daily_size": 5, "discogs_token": ""}).status_code == 200
        searched = web.get("/api/search", params={"q": "Fixture Artist A - Seed A"}).json()
        seed = next(p for p in searched["items"] if p["library_id"] is None)
        root, _ = finish(web, web.post("/api/digs", json={"seeds": [seed,
            {"point_id": "link:youtube", "kind": "link", "name": "YouTube seed",
             "url": "https://youtu.be/S9OEMBED001"}]}))
        snapshot = web.get(f"/api/steps/{root['step_id']}").json()["snapshot"]
        assert root["state"] == "done" and snapshot["picks"]
        assert web.get(f"/api/digs/{snapshot['step']['dig_id']}").status_code == 200
        pick = next(p for p in snapshot["picks"] if p["cosine_id"] == "shared-track")
        label = {"point_id": "label:501", "kind": "label", "name": "Label A", "discogs_id": "501",
                 "source_step_id": root["step_id"], "source_rank": pick["rank"]}
        child, _ = finish(web, web.post("/api/steps", json={"parent_step_id": root["step_id"],
                              "origins": [label], "move": "label", "further": True}))
        assert child["state"] == "done" and child["result"]["pick_count"]
        judgment_path = "/api/judgments/" + quote(pick["track_key"], safe="")
        judged = web.put(judgment_path, json={"step_id": root["step_id"], "verdict": "keep", "expected_revision": 0})
        assert judged.status_code == 200
        assert web.post("/api/heard", json={"step_id": root["step_id"], "track_key": pick["track_key"]}).status_code == 200
        assert web.request("DELETE", judgment_path, json={"step_id": root["step_id"], "expected_revision": 1}).status_code == 200
        assert web.post("/api/feedback", json={"session_id": session_id, "track_id": cli_pick.cosine_id, "event": "save"}).status_code == 200
        enriched, _ = finish(web, web.post("/api/library/labels/enrich", json={"budget": 1}))
        assert enriched["state"] == "done"
        assert web.post(f"/api/library/labels/jobs/{enriched['id']}/cancel", json={}).status_code == 200
        scanned, _ = finish(web, web.post("/api/library/scan", json={"path": str(music)}))
        assert scanned["state"] == "done" and scanned["result"]["added"] == 1
        for kind in ("daily", "weekly"):
            feed, _ = finish(web, web.post(f"/api/playlists/{kind}/run", json={}))
            assert feed["state"] == "done" and feed["result"]["tracks"], feed
            assert feed["result"]["fresh"] == {"skipped": "no EXA_API_KEY"}
            assert feed["result"]["export"]["verified"] is True
        exported, _ = finish(web, web.post("/api/export", json={"session_id": session_id, "name": "Fixture export"}))
        assert exported["state"] == "done" and exported["result"]["export"]["verified"] is True
        login = web.post("/api/youtube/login", json={"headers":
            "authorization: SAPISIDHASH fixture\ncookie: __Secure-3PAPISID=fixture-cookie\n"
            "origin: https://music.youtube.com\nx-goog-authuser: 0"})
        reply = login.json()
        # The reply carries the server's settings: a login saved in test mode has no verdict.
        assert reply.pop("settings")["youtube"] == {"logged_in": True, "saved": True, "checked": None, "chrome": False}
        assert reply == {"ok": True, "logged_in": True, "state": "saved"}
    assert ambient_reads == [] and real_providers == [] and attempts == []


def test_new_dig_events_and_cli_session_feedback(web, cfg):
    job, events = finish(web, web.post("/api/digs", json={"seeds": [track_point()]}))
    assert job["state"] == "done" and job["kind"] == "step"
    assert events[-1][0] == "done" and any(name == "picks_done" for name, _ in events)
    snapshot = web.get(f"/api/steps/{job['step_id']}").json()["snapshot"]
    picks = snapshot["picks"]
    assert len(picks) == 7 and all(p["route"]["why"].startswith("close to") for p in picks)
    assert [data["pick"]["cosine_id"] for name, data in events if name == "pick"] == [p["cosine_id"] for p in picks]
    assert [data["seq"] for _, data in events] == list(range(1, job["last_seq"] + 1))
    assert web.get(f"/api/jobs/{job['id']}/events").text.endswith("\n\n")
    assert web.get("/api/sessions").json() == []
    result = recorded(cfg)
    assert web.post("/api/feedback", json={"session_id": result["session_id"],
                                         "track_id": result["picks"][0]["id"], "event": "save"}).json() == {"ok": True}
    assert any("save" in e["sources"] for e in web.get("/api/profile").json()["entities"])
    assert web.post("/api/feedback", json={"session_id": "missing", "track_id": "1", "event": "save"}).status_code == 404


@pytest.mark.parametrize("half_tagged", [False, True])
def test_empty_discogs_label_move_never_falls_back_to_library(cfg, half_tagged):
    providers = SliceFixtures()
    providers._directories["/labels/501/releases"] = []
    with closing(api.open_db(cfg.db_path)) as conn, conn:
        conn.executemany("INSERT INTO tracks(path,artist,title,label,source) VALUES (?,?,?,?, 'folder')", [
            ("owned.flac", "Fixture Artist A", None if half_tagged else "Old File", "Label A"),
            ("other.flac", "Fixture Artist A", "Seed A", "Another Label"),
        ])
    configured = replace(cfg, cosine_token="fixture-only", discogs_token="fixture-only")
    with TestClient(create_app(configured, client_factory=providers.cosine_client,
                              discogs_client_factory=providers.discogs_client), base_url="http://localhost") as web:
        root, _ = finish(web, web.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]}))
        parent = web.get(f"/api/steps/{root['step_id']}").json()["snapshot"]
        pick = next(p for p in parent["picks"] if p["cosine_id"] == "shared-track")
        label = {"point_id": "label:501", "kind": "label", "name": "Label A", "discogs_id": "501",
                 "source_step_id": root["step_id"], "source_rank": pick["rank"]}
        before = len(providers.calls)
        job, events = finish(web, web.post("/api/steps", json={"parent_step_id": root["step_id"],
                             "origins": [label], "move": "label", "further": False}))
        snapshot = web.get(f"/api/steps/{job['step_id']}").json()["snapshot"]
    assert job["state"] == "done" and job["error"] is None and events[-1][0] == "done"
    assert snapshot["picks"] == [] and snapshot["step"]["origins"][0]["resolved_tracks"] == []
    assert snapshot["step"]["header"] == "Sounds like the label Label A"
    notes = {note["code"]: note for note in snapshot["step"]["note"]}
    assert notes["S-N-LABEL-FEW"]["params"] == {"m": 0, "Label": "Label A"}
    assert "S-E-EMPTY" in notes and "S-N-NONE" not in notes
    assert [(provider, path) for provider, path, _ in providers.calls[before:]] == [("discogs", "/labels/501/releases")]


@pytest.mark.parametrize("unavailable", [False, True])
def test_missing_seed_is_done_but_unavailable_provider_fails(cfg, unavailable):
    from cosine_digger.client import CosineHTTPError

    class Unresolved(Cosine):
        def search(self, query, limit=20):
            if unavailable:
                raise CosineHTTPError(503, "Service Unavailable", {})
            return []

    with TestClient(create_app(cfg, client_factory=Unresolved), base_url="http://localhost") as web:
        job, events = finish(web, web.post("/api/digs", json={"seeds": [track_point("Missing Artist", "Missing Track")]}))
        snapshot = web.get(f"/api/steps/{job['step_id']}").json()["snapshot"]
    assert snapshot["picks"] == [] and not any(name == "pick" for name, _ in events)
    if unavailable:
        assert job["state"] == "error" and events[-1][0] == "error"
        assert job["error"]["code"] == "cosine_unavailable"
        assert snapshot["step"]["status"] == "failed"
        assert "S-N-NONE" not in {note["code"] for note in snapshot["step"]["note"]}
    else:
        assert job["state"] == "done" and job["error"] is None and events[-1][0] == "done"
        assert snapshot["step"]["status"] == "done"
        assert snapshot["step"]["origins"][0]["resolution"] == "missing"
        note = next(note for note in snapshot["step"]["note"] if note["code"] == "S-N-NONE")
        assert "Missing Artist" in note["message"]


def test_settings_mask_validate_and_preserve_empty_secrets(web, cfg, monkeypatch):
    # A key is saved only through its check (A5); the generic settings write refuses keys.
    refused = web.put("/api/settings", json={"cosine_token": "private-token-1234", "daily_size": 5})
    assert refused.status_code == 400 and "private-token" not in refused.text
    assert not (cfg.home / "settings.json").exists()
    monkeypatch.setitem(connections.CHECKS, "cosine_token", lambda key: connections.Check("connected"))
    response = web.post("/api/connections/cosine_token", json={"key": "private-token-1234"})
    assert response.status_code == 200 and response.json()["state"] == "connected"
    assert response.json()["settings"]["cosine_token"] == {"set": True, "hint": "…1234", "source": "settings",
                                                           "env": "COSINE_API_TOKEN", "malformed": False,
                                                           "checked": "connected"}
    assert "private-token" not in response.text + web.get("/api/settings").text
    assert web.put("/api/settings", json={"cosine_token": "", "daily_size": 5}).json()["cosine_token"]["set"]
    assert json.loads((cfg.home / "settings.json").read_text())["cosine_token"] == "private-token-1234"
    for body in [{"daily_time": "24:00"}, {"weekly_time": "6:00"}, {"weekly_day": "Monday"},
                 {"daily_size": 4}, {"weekly_size": 101}, {"daily_size": True}, {"music_dir": "/does-not-exist"}]:
        assert web.put("/api/settings", json=body).status_code == 400
    assert web.put("/api/settings", json={"music_dir": str(cfg.home), "weekly_day": "fri", "weekly_time": "23:59"}).status_code == 200


def test_playlist_and_export_jobs_use_real_ledger(web, cfg):
    result = recorded(cfg)
    feed_library(cfg)
    job, _ = finish(web, web.post("/api/playlists/daily/run", json={"dry_run": True}))
    assert job["state"] == "done" and job["result"]["tracks"]
    assert job["result"]["export"] is None
    job, events = finish(web, web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved dig"}))
    assert job["state"] == "done" and job["result"]["export"]["verified"]
    assert events[0][0] == "progress" and events[-1][0] == "done" and job["events"] == len(events)
    replay = web.get(f"/api/jobs/{job['id']}/events")
    assert replay.text.endswith("\n\n") and replay.text.count("event: done") == 1
    assert web.get(f"/api/jobs/{job['id']}/events").text == replay.text
    with closing(api.open_db(cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM served_ledger").fetchone()[0] == len(result["picks"])


def test_run_now_shows_on_the_playlist_card_and_counts_for_the_schedule(web, cfg):
    """Seen by review: a manual run left last_run empty, so the card said "Never run"."""
    from datetime import datetime

    from cosine_digger import schedule

    feed_library(cfg)
    assert web.get("/api/playlists").json()["daily"]["last_run"] is None
    finish(web, web.post("/api/playlists/daily/run", json={"dry_run": True}))
    assert web.get("/api/playlists").json()["daily"]["last_run"] is None  # a dry run touches nothing
    job, _ = finish(web, web.post("/api/playlists/daily/run", json={}))
    assert job["state"] == "done" and job["result"]["export"]["verified"]
    last = web.get("/api/playlists").json()["daily"]["last_run"]
    assert last["status"] == "ok" and "verified" in last["detail"]
    finish(web, web.post("/api/playlists/daily/run", json={"name": "Digger Test daily"}))
    assert web.get("/api/playlists").json()["daily"]["last_run"] == last  # another playlist is not the daily
    with closing(api.open_db(cfg.db_path)) as conn:
        now = datetime.now().astimezone()
        assert "daily" not in [job for job, _ in schedule.due_jobs(cfg, now, lambda job: schedule.last_success(conn, job))]


def test_security_applies_to_api_and_static(web, monkeypatch):
    assert web.post("/api/digs", data={"seeds": "x"}).status_code == 415
    assert web.post("/api/digs", json={}, headers={"Origin": "https://foreign.example"}).status_code == 403
    assert web.get("/api/health", headers={"Host": "foreign.example"}).status_code == 403
    assert web.get("/", headers={"Host": "localhost.evil"}).status_code == 403
    assert web.get("/static/nope", headers={"Host": "evil.example"}).status_code == 403
    assert web.get("/api/health", headers={"Host": "[::1]:8765"}).status_code == 200
    assert web.post("/api/digs", json={}, headers={"Origin": "http://localhost:9876"}).status_code == 422
    monkeypatch.setenv("COSINE_DIGGER_ALLOWED_HOSTS", "my-machine,custom.local:8765")
    assert web.get("/api/health", headers={"Host": "custom.local:8765"}).status_code == 200


def test_youtube_login_never_echoes_headers(web, monkeypatch, cfg):
    calls = []
    monkeypatch.setattr("cosine_digger.youtube.login", lambda headers, path: calls.append((headers, path)))
    secret = "Cookie: SID=super-secret-value"
    response = web.post("/api/youtube/login", json={"headers": secret})
    # The stand-in login wrote no file, so there is nothing to check (and nothing reads Connected).
    # The reply carries the server's settings now, and still never the headers.
    assert secret not in response.text
    reply = response.json()
    assert reply.pop("settings")["youtube"] == {"logged_in": False, "saved": False, "checked": None, "chrome": False}
    assert reply == {"ok": True, "logged_in": False, "state": "missing"}
    assert calls == [(secret, cfg.ytmusic_auth_path)]

    def fail(headers, path):
        raise ValueError(headers)
    monkeypatch.setattr("cosine_digger.youtube.login", fail)
    response = web.post("/api/youtube/login", json={"headers": secret})
    assert response.status_code == 400 and secret not in response.text
    response = web.post("/api/youtube/login", json={"headers": {"secret": secret}})
    assert response.status_code == 422 and secret not in response.text


def test_login_from_chrome_saves_a_private_login_and_reports_chrome_problems(cfg, monkeypatch):
    """The route takes headers from Chrome's cookie store (a stand-in here) through the same
    private save and check as pasted headers; a Chrome problem comes back in our own words."""
    from cosine_digger import chrome_login

    class Chrome:
        ChromeLoginError = chrome_login.ChromeLoginError
        headers = ("cookie: __Secure-3PAPISID=chrome-cookie\nx-goog-authuser: 0\n"
                   "origin: https://music.youtube.com")

        @staticmethod
        def available():
            return True

        @classmethod
        def youtube_headers(cls, profile=None, authuser=0):
            if profile == "Signed Out":
                raise chrome_login.ChromeLoginError("Chrome isn't signed in to YouTube Music.")
            return cls.headers

    monkeypatch.setattr("cosine_digger.youtube.check_login", lambda path, **_: "connected")
    with TestClient(create_app(cfg, chrome=Chrome), base_url="http://localhost") as web:
        assert web.get("/api/settings").json()["youtube"]["chrome"] is True
        refused = web.post("/api/youtube/login/chrome", json={"profile": "Signed Out"})
        assert refused.status_code == 400 and refused.json()["error"] == "Chrome isn't signed in to YouTube Music."
        assert not cfg.ytmusic_auth_path.exists()
        reply = web.post("/api/youtube/login/chrome", json={}).json()
        assert reply["state"] == "connected" and reply["settings"]["youtube"]["checked"] == "connected"
        assert "chrome-cookie" not in json.dumps(reply)
    saved = json.loads(cfg.ytmusic_auth_path.read_text())
    assert any(key.lower() == "cookie" and "chrome-cookie" in value for key, value in saved.items())
    assert oct(cfg.ytmusic_auth_path.stat().st_mode & 0o777) == "0o600"


def test_logging_in_from_chrome_again_keeps_the_account_the_saved_login_acts_as(cfg, monkeypatch):
    # Chrome signed in to two Google accounts, YouTube Music on the second: a login again
    # (from Setup, or from a paired phone) mustn't move Digger to Chrome's first account.
    from cosine_digger import chrome_login

    asked = []

    class Chrome:
        ChromeLoginError = chrome_login.ChromeLoginError

        @staticmethod
        def available():
            return True

        @staticmethod
        def youtube_headers(profile=None, authuser=0):
            asked.append(authuser)
            return f"cookie: __Secure-3PAPISID=chrome-cookie\nx-goog-authuser: {authuser}\norigin: https://music.youtube.com"

    monkeypatch.setattr("cosine_digger.youtube.check_login", lambda path, **_: "connected")
    with TestClient(create_app(cfg, chrome=Chrome), base_url="http://localhost") as web:
        assert web.post("/api/youtube/login/chrome", json={}).status_code == 200
        assert asked == [0]  # no login yet: Chrome's first account
        saved = json.loads(cfg.ytmusic_auth_path.read_text())
        saved = {key: ("1" if key.lower() == "x-goog-authuser" else value) for key, value in saved.items()}
        cfg.ytmusic_auth_path.write_text(json.dumps(saved))  # moved to the second account in Setup
        assert web.post("/api/youtube/login/chrome", json={}).status_code == 200
        assert asked == [0, 1]
    saved = json.loads(cfg.ytmusic_auth_path.read_text())
    assert next(value for key, value in saved.items() if key.lower() == "x-goog-authuser") == "1"


def test_a_second_login_from_chrome_while_macos_asks_answers_at_once(cfg):
    import threading
    from cosine_digger import chrome_login

    asked, answer = threading.Event(), threading.Event()

    class Chrome:
        ChromeLoginError = chrome_login.ChromeLoginError

        @staticmethod
        def available():
            return True

        @staticmethod
        def youtube_headers(profile=None, authuser=0):
            asked.set()
            answer.wait(10)  # macOS's prompt, still open
            raise chrome_login.ChromeLoginError("macOS didn't share Chrome's cookie key.")

    with TestClient(create_app(cfg, chrome=Chrome), base_url="http://localhost") as web:
        first = []
        waiting = threading.Thread(target=lambda: first.append(web.post("/api/youtube/login/chrome", json={})))
        waiting.start()
        assert asked.wait(10)
        second = web.post("/api/youtube/login/chrome", json={})
        assert second.status_code == 409 and "Already waiting for macOS" in second.json()["error"]
        assert web.get("/api/health").status_code == 200
        answer.set()
        waiting.join(10)
        assert first[0].status_code == 400
        again = web.post("/api/youtube/login/chrome", json={})  # the lock is free again
        assert again.status_code == 400


def test_the_page_names_code_files_by_content_so_updates_are_never_cached_over(cfg):
    # Chrome kept dynamically imported modules from before an update, even through a hard
    # reload: every module and stylesheet URL now carries its content digest.
    import hashlib
    import re
    from pathlib import Path

    import cosine_digger.web.app as app_module

    static = Path(app_module.__file__).parent / "static"
    with TestClient(create_app(cfg), base_url="http://localhost") as web:
        page = web.get("/")
        assert page.headers["cache-control"] == "no-cache"
        imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', page.text).group(1))["imports"]
        digest = hashlib.sha256((static / "setup.js").read_bytes()).hexdigest()[:12]
        assert imports["/static/setup.js"] == f"/static/setup.js?v={digest}"
        assert "/static/slice.js" not in imports and '<script type="module" src="/static/slice.js">' in page.text
        assert page.text.index('type="importmap"') < page.text.index('src="/static/slice.js"')
        assert re.search(r'data-href="/static/setup\.css\?v=[0-9a-f]{12}"', page.text)
        module = web.get(imports["/static/setup.js"])
        assert module.status_code == 200 and module.headers["cache-control"] == "no-cache"


def test_further_move_streams_batches_and_redacts_provider_errors(cfg, caplog):
    providers = SliceFixtures()
    configured = replace(cfg, cosine_token="fixture-only", discogs_token="fixture-only")
    with TestClient(create_app(configured, client_factory=providers.cosine_client,
                              discogs_client_factory=providers.discogs_client), base_url="http://localhost") as web:
        root, _ = finish(web, web.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]}))
        parent = web.get(f"/api/steps/{root['step_id']}").json()["snapshot"]
        pick = next(p for p in parent["picks"] if p["cosine_id"] == "shared-track")
        origin = {**track_point(pick["artist"], pick["title"]),
                  "source_step_id": root["step_id"], "source_rank": pick["rank"]}
        job, events = finish(web, web.post("/api/steps", json={"parent_step_id": root["step_id"],
                             "origins": [origin], "move": "track", "further": True}))
        snapshot = web.get(f"/api/steps/{job['step_id']}").json()["snapshot"]
        assert job["state"] == "done" and events[-1][0] == "done"
        assert [data["hop"] for name, data in events if name == "batch"] == [0, 1]
        further = [data["pick"] for name, data in events if name == "pick" and data["pick"]["batch"] == 1]
        assert further and all(p["route"]["parent_via"] and p["route"]["parent_pick_ranks"] for p in further)
        assert {p["cosine_id"] for p in further} <= {p["cosine_id"] for p in snapshot["picks"]}

    class Broken(Cosine):
        def search(self, *args, **kwargs):
            raise RuntimeError("Cookie: unknown-provider-secret")
    with TestClient(create_app(cfg, client_factory=Broken), base_url="http://localhost") as web:
        job, events = finish(web, web.post("/api/digs", json={"seeds": [track_point()]}))
        saved = web.get(f"/api/steps/{job['step_id']}").text
        assert job["state"] == "error" and events[-1][0] == "error"
        assert job["error"]["code"] == "move_failed"
        assert "unknown-provider-secret" not in json.dumps(job) + json.dumps(events) + saved + caplog.text


def test_library_scan_is_incremental(web, cfg):
    music = cfg.home / "music"
    music.mkdir()
    (music / "Artist - Track.mp3").write_bytes(b"")
    for expected in [1, 0]:
        job, _ = finish(web, web.post("/api/library/scan", json={"path": str(music)}))
        assert job["state"] == "done" and job["result"] == {"added": expected, "total": 1}


def test_new_dig_and_cli_session_survive_missing_youtube_login(cfg):
    result = recorded(cfg)
    with TestClient(create_app(cfg, client_factory=Cosine), base_url="http://localhost") as web:
        job, events = finish(web, web.post("/api/digs", json={"seeds": [track_point()]}))
        assert job["state"] == "done" and job["result"]["pick_count"]
        assert events[-1][0] == "done"
        exported, _ = finish(web, web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved"}))
        assert exported["state"] == "error" and "login" in exported["error"].lower()
        assert web.get("/api/sessions").json()[0]["result_count"] == len(result["picks"])
        with closing(api.open_db(cfg.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM served_ledger").fetchone()[0] == 0
        assert web.get("/static/absent.js").status_code == 404
        assert web.post("/api/digs", json={}, headers={"Origin": ""}).status_code == 403


def test_a_pasted_key_with_a_newline_never_reaches_the_job_error(cfg, tmp_path, caplog):

    from cosine_digger.client import CosineClient, CosineRequestError
    from cosine_digger.config import save_settings

    save_settings({"cosine_token": "SYNTH-PRIVATE-KEY\n"}, tmp_path)
    loaded = load_config({"COSINE_DIGGER_HOME": str(tmp_path), "HOME": str(tmp_path)})
    assert loaded.cosine_token == "SYNTH-PRIVATE-KEY"
    with CosineClient(token="SYNTH-PRIVATE-KEY\n") as client:
        assert client._client.headers["Authorization"] == "Bearer SYNTH-PRIVATE-KEY"

    class Broken(Cosine):  # h11 quotes the header it refused, escaped
        def search(self, *args, **kwargs):
            raise CosineRequestError("Request failed: Illegal header value b'Bearer SYNTH-PRIVATE-KEY\\n'")

    class Other(Cosine):  # a key the app never saw still hides behind Bearer
        def search(self, *args, **kwargs):
            raise CosineRequestError("Request failed: Illegal header value b'Bearer UNSEEN-KEY '")
    with TestClient(create_app(cfg, client_factory=Cosine), base_url="http://localhost") as web:
        refused = web.put("/api/settings", json={"exa_api_key": "SYNTH INNER\nKEY"})
    assert refused.status_code == 400 and "SYNTH" not in refused.text

    raw = replace(cfg, cosine_token="SYNTH-PRIVATE-KEY\n")
    for factory, secret in ((Broken, "SYNTH-PRIVATE-KEY"), (Other, "UNSEEN-KEY")):
        with TestClient(create_app(raw, client_factory=factory), base_url="http://localhost") as web:
            job, events = finish(web, web.post("/api/digs", json={"seeds": [track_point()]}))
        assert job["state"] == "error" and job["error"]["code"] == "cosine_unreachable"
        assert "CosineRequestError" in caplog.text
        assert secret not in json.dumps(job) + json.dumps(events) + caplog.text
        with closing(api.open_db(cfg.db_path)) as conn:
            assert secret not in str(conn.execute("SELECT payload FROM slice_events WHERE job_id=?", (job["id"],)).fetchall())


def test_export_job_errors_never_carry_provider_text(cfg):
    class Leaky:
        def __init__(self, conn):
            pass

        def replace_playlist(self, *a, **k):
            raise ValueError("Invalid leading whitespace in header value: ' SYNTH_COOKIE_SECRET'")
    result = recorded(cfg)
    with TestClient(create_app(cfg, client_factory=Cosine, sink_factory=Leaky), base_url="http://localhost") as web:
        job, events = finish(web, web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved"}))
    assert job["state"] == "error" and "YouTube Music export failed" in job["error"]
    assert "SYNTH_COOKIE_SECRET" not in json.dumps(job) + json.dumps(events)


def test_a_broken_login_never_reaches_the_job_error(cfg, monkeypatch):
    cfg.ytmusic_auth_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.ytmusic_auth_path.write_text("{}")

    def broken(path):
        raise ValueError("Illegal key 'SYNTH_COOKIE_SECRET/'")
    monkeypatch.setattr("cosine_digger.web.app.youtube.YTMusic", broken)
    result = recorded(cfg)
    with TestClient(create_app(cfg, client_factory=Cosine), base_url="http://localhost") as web:
        job, events = finish(web, web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved"}))
    assert job["state"] == "error" and "SYNTH_COOKIE_SECRET" not in json.dumps(job) + json.dumps(events)


def test_shutdown_waits_for_a_running_export(cfg):
    import threading
    import time as _time
    started = threading.Event()

    class Slow(Sink):
        def replace_playlist(self, *a, **k):
            started.set()
            _time.sleep(0.5)
            return super().replace_playlist(*a, **k)
    result = recorded(cfg)
    app = create_app(cfg, client_factory=Cosine, sink_factory=Slow)
    with TestClient(app, base_url="http://localhost") as web:
        job_id = web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved"}).json()["job_id"]
        assert started.wait(5)
    assert app.state.jobs[job_id].state == "done"  # the app did not exit mid-write


def test_shutdown_outlasts_a_slow_export_and_turns_new_jobs_away(cfg, monkeypatch):
    """No grace period to run out: shutdown waits for the export however long
    it takes, and a job that arrives meanwhile is refused, never half-started."""
    import threading
    import cosine_digger.web.app as web_app
    monkeypatch.setattr(web_app, "SHUTDOWN_NOTE_S", 0.05)
    started, release = threading.Event(), threading.Event()

    class Stuck(Sink):
        def replace_playlist(self, *a, **k):
            started.set()
            release.wait(10)
            return super().replace_playlist(*a, **k)
    result = recorded(cfg)
    app = create_app(cfg, client_factory=Cosine, sink_factory=Stuck)
    late = {}
    with TestClient(app, base_url="http://localhost") as web:
        job_id = web.post("/api/export", json={"session_id": result["session_id"], "name": "Saved"}).json()["job_id"]
        assert started.wait(5)

        def arrive_late():
            import time as _time
            _time.sleep(0.3)  # shutdown has begun and is waiting on the export
            late["status"] = web.post("/api/export", json={"session_id": result["session_id"], "name": "Late"}).status_code
            release.set()
        threading.Thread(target=arrive_late).start()
    assert late["status"] == 503
    assert app.state.jobs[job_id].state == "done"
