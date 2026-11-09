"""Companion mode: a paired phone reaches Digger over the network with a token, and only to dig."""
from contextlib import contextmanager
import json
import os
import stat
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest
from typer.testing import CliRunner

from cosine_digger import chain, client as cosine_http, companion as pairing, discogs, storage
from cosine_digger.cli import app as cli_app
from cosine_digger.commands import serve as serve_command
from cosine_digger.config import AppConfig
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.test_slice_api import NOW, finished, from_pick

PHONE = ("192.168.1.50", 50000)
HERE = ("127.0.0.1", 50000)
LAN_HOST = {"Host": "192.168.1.20:8765"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The slice fixture world (fake providers, real routes and SQLite), with companion mode on."""
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    for name in ("COSINE_DIGGER_TEST_MODE", "COSINE_DIGGER_FIXTURE", pairing.ENV):
        monkeypatch.delenv(name, raising=False)
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
                    cosine_token="fixture-cosine-key", discogs_token="fixture-discogs-key")
    token = pairing.load_token(tmp_path)
    app = create_app(cfg, client_factory=providers.cosine_client, discogs_client_factory=providers.discogs_client,
                     companion=pairing.Companion.at(tmp_path))
    yield SimpleNamespace(app=app, token=token, cfg=cfg, providers=providers)
    providers.release_all()


@contextmanager
def device(world, client=PHONE):
    with TestClient(world.app, base_url="http://192.168.1.20:8765", client=client) as http:
        yield http


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_token_is_private_stable_and_rotates(tmp_path):
    first = pairing.load_token(tmp_path)
    assert pairing.TOKEN.fullmatch(first)
    assert pairing.load_token(tmp_path) == first
    assert stat.S_IMODE(os.stat(pairing.token_path(tmp_path)).st_mode) == 0o600
    rotated = pairing.load_token(tmp_path, rotate=True)
    assert rotated != first and pairing.load_token(tmp_path) == rotated
    pairing.token_path(tmp_path).write_text("short\n")
    assert pairing.load_token(tmp_path) not in {"short", rotated}


def test_phone_without_the_token_is_refused_everywhere(world):
    with device(world) as http:
        for method, path, body in (("GET", "/api/digs", None), ("GET", "/api/health", None),
                                   ("POST", "/api/digs", {"seeds": []}), ("GET", "/", None)):
            response = http.request(method, path, json=body, headers=LAN_HOST)
            assert response.status_code == 401, (path, response.text)
            assert response.headers["www-authenticate"] == "Bearer"
            assert "pair" in response.json()["error"]
        wrong = http.get("/api/digs", headers={**LAN_HOST, **bearer("x" * 43)})
        assert wrong.status_code == 401
        basic = http.get("/api/digs", headers={**LAN_HOST, "Authorization": f"Basic {world.token}"})
        assert basic.status_code == 401


def test_paired_phone_can_check_pairing_and_read_digs_by_lan_address(world):
    with device(world) as http:
        response = http.get("/api/companion", headers={**LAN_HOST, **bearer(world.token)})
        assert response.status_code == 200 and response.json()["ok"] is True
        assert http.get("/api/digs", headers={**LAN_HOST, **bearer(world.token)}).json() == {"digs": []}
        # A Bonjour name works too: the token, not the Host header, authenticates a phone.
        named = http.get("/api/digs", headers={"Host": "studio-mac.local:8765", **bearer(world.token)})
        assert named.status_code == 200
        # And so does the tailnet name, from away from home.
        tailnet = http.get("/api/digs", headers={"Host": "studio-mac.tail1234.ts.net:8765", **bearer(world.token)})
        assert tailnet.status_code == 200


def test_a_new_token_takes_effect_in_a_digger_already_running(world):
    with device(world) as http:
        headers = {**LAN_HOST, **bearer(world.token)}
        assert http.get("/api/digs", headers=headers).status_code == 200
        rotated = pairing.load_token(world.cfg.home, rotate=True)
        assert http.get("/api/digs", headers=headers).status_code == 401
        assert http.get("/api/digs", headers={**LAN_HOST, **bearer(rotated)}).status_code == 200
        # A token file that's gone pairs nothing, until the next companion command makes one.
        pairing.token_path(world.cfg.home).unlink()
        assert http.get("/api/digs", headers={**LAN_HOST, **bearer(rotated)}).status_code == 401
        remade = pairing.load_token(world.cfg.home)
        assert http.get("/api/digs", headers={**LAN_HOST, **bearer(remade)}).status_code == 200


def test_the_pairing_check_reads_this_computers_name_once(world, monkeypatch):
    asked = []
    monkeypatch.setattr(pairing, "display_name", lambda: asked.append(1) or "studio-mac")
    with device(world) as http:
        for _ in range(3):
            response = http.get("/api/companion", headers={**LAN_HOST, **bearer(world.token)})
            assert response.json()["name"] == "studio-mac"
    assert len(asked) == 1


def test_paired_phone_cannot_reach_keys_settings_or_the_web_page(world):
    with device(world) as http:
        headers = {**LAN_HOST, **bearer(world.token)}
        for method, path, body in (("PUT", "/api/settings", {"daily_size": 10}),
                                   ("POST", "/api/connections/cosine_token", {"key": "new"}),
                                   ("GET", "/api/doctor", None), ("GET", "/api/profile", None),
                                   ("POST", "/api/youtube/login", {"headers": "cookie: x"}),
                                   ("POST", "/api/export", {"session_id": 1}),
                                   ("POST", "/api/library/scan", {"path": "/"}),
                                   ("POST", "/api/playlists/daily/run", {"dry_run": True}),
                                   ("POST", "/api/library/labels/enrich", {}), ("GET", "/", None),
                                   ("GET", "/static/slice.js", None), ("POST", "/api/lifecycle/stop", {})):
            response = http.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, (path, response.text)
        assert http.get("/api/settings", headers=headers).status_code == 200


def test_paired_request_keeps_origin_and_json_rules(world):
    with device(world) as http:
        headers = {**LAN_HOST, **bearer(world.token)}
        seeds = {"seeds": [world.providers.point("seed-a").to_dict()]}
        assert http.post("/api/digs", json=seeds, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        assert http.post("/api/digs", content=json.dumps(seeds),
                               headers={**headers, "Content-Type": "text/plain"}).status_code == 415


def test_this_computer_keeps_its_rules_and_a_wrong_token_is_refused_here_too(world):
    with device(world, client=HERE) as http:
        assert http.get("/api/digs", headers={"Host": "localhost:8765"}).status_code == 200
        assert http.get("/api/digs", headers={"Host": "evil.example"}).status_code == 403
        assert http.get("/api/doctor?offline=true", headers={"Host": "127.0.0.1:8765"}).status_code == 200
        assert http.get("/api/digs", headers={"Host": "localhost:8765", **bearer("y" * 43)}).status_code == 401
        # The iOS Simulator reaches the computer at 127.0.0.1 and sends its token anyway.
        assert http.get("/api/digs", headers={"Host": "127.0.0.1:8765", **bearer(world.token)}).status_code == 200


def test_companion_route_is_absent_when_companion_mode_is_off(world):
    off = create_app(world.cfg, client_factory=world.providers.cosine_client)
    with TestClient(off, base_url="http://localhost") as http:
        assert http.get("/api/companion").status_code == 404


def test_phone_journey_seed_keep_heard_and_label_branch(world):
    """The phone app's whole journey through the routes it is allowed: seed, results, keep,
    heard, sounds like its label, and the parent step still as it was."""
    with device(world) as phone:
        headers = {**LAN_HOST, **bearer(world.token)}
        http = SimpleNamespace(get=lambda path: phone.get(path, headers=headers))
        found = phone.get("/api/search", params={"q": "Seed A", "source": "all"}, headers=headers).json()
        seed = next(item for item in found["items"] if item["cosine_id"] == "seed-a")
        created = phone.post("/api/digs", json={"seeds": [seed]},
                                  headers={**headers, "Idempotency-Key": "phone-root-1"})
        assert created.status_code == 200, created.text
        again = phone.post("/api/digs", json={"seeds": [seed]},
                                headers={**headers, "Idempotency-Key": "phone-root-1"})
        assert again.json() == created.json()  # a retried tap never makes a second dig
        identity = created.json()
        snapshot = finished(SimpleNamespace(http=http), identity)
        picks = snapshot["snapshot"]["picks"]
        assert len(picks) >= 10
        pick = next(p for p in picks if p["facts_state"] == "ready" and p["facts"]["labels"])
        key = quote(pick["track_key"], safe="")
        kept = phone.put(f"/api/judgments/{key}", headers=headers,
                              json={"step_id": identity["step_id"], "verdict": "keep", "expected_revision": 0})
        assert kept.status_code == 200 and kept.json()["effective"]["verdict"] == "keep"
        stale = phone.put(f"/api/judgments/{key}", headers=headers,
                               json={"step_id": identity["step_id"], "verdict": "pass", "expected_revision": 0})
        assert stale.status_code == 409
        heard = phone.post("/api/heard", headers=headers,
                                json={"track_key": pick["track_key"], "step_id": identity["step_id"]})
        assert heard.status_code == 200
        label = pick["facts"]["labels"][0]
        point = {"point_id": f"label:{label['id']}", "kind": "label", "name": label["name"],
                 "discogs_id": label["id"], "source_step_id": identity["step_id"], "source_rank": pick["rank"]}
        branch = phone.post("/api/steps", headers={**headers, "Idempotency-Key": "phone-label-1"},
                                 json={"parent_step_id": identity["step_id"], "origins": [point], "move": "label",
                                       "further": False, "source_selection_id": f"fact:{pick['rank']}:{point['point_id']}"})
        assert branch.status_code == 200, branch.text
        child = finished(SimpleNamespace(http=http), branch.json())
        assert child["snapshot"]["step"]["parent_id"] == identity["step_id"]
        assert child["snapshot"]["step"]["context"]["source_selection_id"] == f"fact:{pick['rank']}:label:{label['id']}"
        parent = http.get(f"/api/steps/{identity['step_id']}").json()
        assert [p["track_key"] for p in parent["snapshot"]["picks"]] == [p["track_key"] for p in picks]
        assert parent["live"]["judgments"][pick["track_key"]]["verdict"] == "keep"
        assert pick["track_key"] in parent["live"]["heard"]
        dig = http.get(f"/api/digs/{identity['dig_id']}").json()
        assert [step["id"] for step in dig["steps"]] == [identity["step_id"], branch.json()["step_id"]]
        events = phone.get(f"/api/jobs/{branch.json()['job_id']}/events?after=0", headers=headers)
        assert events.status_code == 200 and "event: done" in events.text
        track = from_pick(identity["step_id"], pick).to_dict()
        moved = phone.post("/api/steps", headers=headers, json={
            "parent_step_id": identity["step_id"], "origins": [track], "move": "track", "further": False,
            "source_selection_id": track["point_id"]})
        assert moved.status_code == 200, moved.text


def test_pairing_link_lists_every_address_and_the_token():
    link = pairing.pairing_link(["http://studio.local:8765", "http://192.168.1.20:8765"], "t" * 43, "studio")
    parsed = urlsplit(link)
    assert (parsed.scheme, parsed.netloc) == ("cosinedigger", "pair")
    query = parse_qs(parsed.query)
    assert query["url"] == ["http://studio.local:8765", "http://192.168.1.20:8765"]
    assert query["token"] == ["t" * 43] and query["name"] == ["studio"]
    assert pairing.addresses("127.0.0.1", 8765) == ["http://127.0.0.1:8765"]
    assert pairing.addresses("192.168.1.20", 9000) == ["http://192.168.1.20:9000"]
    assert all(url.startswith("http://") and not url.startswith("http://127.") for url in pairing.addresses("0.0.0.0", 8765))


def test_the_link_names_this_computer_on_its_tailnet_while_tailscale_runs(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    fake = tmp_path / "bin" / "tailscale"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\n[ "$1 $2" = "status --json" ] && cat "$TAILSCALE_STATUS"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TAILSCALE_STATUS", str(status))
    monkeypatch.setattr(pairing, "local_hostname", lambda: "studio.local")
    monkeypatch.setattr(pairing, "lan_addresses", lambda: ["192.168.1.20"])

    status.write_text(json.dumps({"BackendState": "Running", "Self": {"DNSName": "Studio.tail1234.ts.net."}}))
    assert pairing.addresses("0.0.0.0", 8765) == [
        "http://studio.local:8765", "http://192.168.1.20:8765", "http://studio.tail1234.ts.net:8765"]
    # Bound to one address, only that one is offered.
    assert pairing.addresses("192.168.1.20", 8765) == ["http://192.168.1.20:8765"]

    for stopped in [{"BackendState": "Stopped", "Self": {"DNSName": "studio.tail1234.ts.net."}},
                    {"BackendState": "Running", "Self": {"DNSName": "not a name; rm -rf /"}},
                    {"BackendState": "Running"}]:
        status.write_text(json.dumps(stopped))
        assert pairing.tailnet_name() is None
    status.write_text("Tailscale is starting…")
    assert pairing.tailnet_name() is None

    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(pairing, "TAILSCALE_APP_CLI", str(tmp_path / "missing"))
    assert pairing.addresses("0.0.0.0", 8765) == ["http://studio.local:8765", "http://192.168.1.20:8765"]


def test_remote_routes_cover_only_the_dig_surface():
    allowed = pairing.Companion.allows
    assert allowed("GET", "/api/steps/abc") and allowed("PUT", "/api/judgments/a%7Cb")
    assert allowed("GET", "/api/jobs/sl-123/events") and not allowed("GET", "/api/jobs/legacy-123")
    assert not allowed("PUT", "/api/settings") and not allowed("GET", "/api/steps/abc/extra")
    assert not allowed("POST", "/api/heard/extra") and not allowed("GET", "/api/digs/a/b")
    # A dig's own playlist, followed by the job id it answers with; only the snapshot, never the stream.
    assert allowed("POST", "/api/digs/abc/export") and not allowed("POST", "/api/digs/abc/export/x")
    assert allowed("GET", "/api/jobs/" + "0a" * 16) and not allowed("GET", "/api/jobs/" + "0a" * 16 + "/events")
    assert not allowed("POST", "/api/export") and not allowed("POST", "/api/playlists/weekly/run")
    # Log in from this computer's Chrome, never with cookies sent over the network.
    assert allowed("POST", "/api/youtube/login/chrome") and not allowed("POST", "/api/youtube/login")


def test_paired_phone_logs_in_from_chrome_on_this_computer_and_sends_a_dig(world, monkeypatch):
    """The phone asks this computer to log in from its own Chrome (the cookies stay here), then
    sends the dig to its private YouTube Music playlist and follows the send to its link."""
    from cosine_digger import api, chrome_login

    class Chrome:
        ChromeLoginError = chrome_login.ChromeLoginError

        @staticmethod
        def available():
            return True

        @staticmethod
        def youtube_headers(profile=None, authuser=0):
            return f"cookie: __Secure-3PAPISID=chrome-cookie\nx-goog-authuser: {authuser}\norigin: https://music.youtube.com"

    monkeypatch.setattr("cosine_digger.youtube.check_login", lambda path, **_: "connected")
    providers = world.providers
    app = create_app(world.cfg, client_factory=providers.cosine_client, discogs_client_factory=providers.discogs_client,
                     companion=pairing.Companion.at(world.cfg.home), chrome=Chrome,
                     sink_factory=lambda conn: providers.youtube_sink(api.DbIdStore(conn)))
    with TestClient(app, base_url="http://192.168.1.20:8765", client=PHONE) as phone:
        headers = {**LAN_HOST, **bearer(world.token)}
        http = SimpleNamespace(get=lambda path: phone.get(path, headers=headers))
        youtube = phone.get("/api/settings", headers=headers).json()["youtube"]
        assert youtube["saved"] is False and youtube["chrome"] is True
        login = phone.post("/api/youtube/login/chrome", json={}, headers=headers)
        assert login.status_code == 200, login.text
        assert login.json()["state"] == "connected" and "chrome-cookie" not in login.text
        assert "chrome-cookie" in world.cfg.ytmusic_auth_path.read_text()

        identity = phone.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]},
                              headers={**headers, "Idempotency-Key": "phone-yt-1"}).json()
        picks = finished(SimpleNamespace(http=http), identity)["snapshot"]["picks"]
        sent = phone.post(f"/api/digs/{identity['dig_id']}/export", json={}, headers=headers)
        assert sent.status_code == 200, sent.text
        job_id = sent.json()["job_id"]
        for _ in range(200):
            job = http.get(f"/api/jobs/{job_id}").json()
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "done", job
        assert job["result"]["playlist_url"] == providers.youtube_sink().playlist_url
        assert job["result"]["added"] == sum(bool(p["video_id"]) for p in picks)
        assert phone.get(f"/api/jobs/{job_id}/events", headers=headers).status_code == 403
        dig = http.get(f"/api/digs/{identity['dig_id']}").json()
        assert dig["youtube_playlist_url"] == providers.youtube_sink().playlist_url and dig["youtube_sent_at"]


def test_a_send_youtube_refuses_says_signed_out_in_the_settings_too(world):
    """A send refused as signed out leaves the settings saying the login was rejected, so the
    phone offers Connect and Log in from Chrome instead of sending the same way again."""
    import ytmusicapi
    from cosine_digger import youtube

    class Refusing:
        def __getattr__(self, name):
            def refuse(*args, **kwargs):
                raise ytmusicapi.exceptions.YTMusicServerError("Server returned HTTP 401: Unauthorized")
            return refuse

    world.cfg.ytmusic_auth_path.write_text(json.dumps({"cookie": "SAPISID=old", "x-goog-authuser": "0"}))
    providers = world.providers
    app = create_app(world.cfg, client_factory=providers.cosine_client, discogs_client_factory=providers.discogs_client,
                     companion=pairing.Companion.at(world.cfg.home), sink_factory=lambda conn: Refusing())
    with TestClient(app, base_url="http://192.168.1.20:8765", client=PHONE) as phone:
        headers = {**LAN_HOST, **bearer(world.token)}
        http = SimpleNamespace(get=lambda path: phone.get(path, headers=headers))
        assert http.get("/api/settings").json()["youtube"]["saved"] is True
        identity = phone.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]},
                              headers={**headers, "Idempotency-Key": "phone-yt-refused"}).json()
        finished(SimpleNamespace(http=http), identity)
        job_id = phone.post(f"/api/digs/{identity['dig_id']}/export", json={}, headers=headers).json()["job_id"]
        for _ in range(200):
            job = http.get(f"/api/jobs/{job_id}").json()
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "error" and youtube.SIGNED_OUT in job["error"]
        assert http.get("/api/settings").json()["youtube"]["checked"] == "rejected"


def test_the_phone_tells_a_missing_login_from_an_expired_one_by_these_words():
    """The phone app, like the web client, tells these two failed sends apart by their words."""
    from cosine_digger import api, youtube
    assert "login missing" in api._export_error(youtube.YouTubeAuthMissing("x")).lower()
    assert "signed out" in youtube.SIGNED_OUT.lower()


def test_companion_command_prints_the_link_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.setattr(pairing, "addresses", lambda host, port: [f"http://192.168.1.20:{port}"])
    result = CliRunner().invoke(cli_app, ["companion", "--port", "9100"])
    assert result.exit_code == 0, result.output
    token = pairing.load_token(tmp_path)
    assert "cosine-digger serve --companion --port 9100" in result.output
    assert token in result.output and "cosinedigger://pair?" in result.output
    rotated = CliRunner().invoke(cli_app, ["companion", "--rotate"])
    assert rotated.exit_code == 0 and pairing.load_token(tmp_path) != token


def test_serve_companion_listens_everywhere_with_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.delenv("COSINE_DIGGER_HOST", raising=False)
    monkeypatch.delenv(pairing.ENV, raising=False)
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    real = pairing.addresses
    monkeypatch.setattr(pairing, "addresses", lambda host, port: [f"http://192.168.1.20:{port}"]
                        if host == "0.0.0.0" else real(host, port))
    seen = {}

    def fake_create_app(cfg, **kwargs):
        seen.update(host=cfg.host, companion=kwargs.get("companion"))
        return object()

    monkeypatch.setattr(serve_command, "create_app", fake_create_app)
    monkeypatch.setattr(serve_command, "run_server", lambda app, **kwargs: seen.update(bind=kwargs["host"]) or True)
    result = CliRunner().invoke(cli_app, ["serve", "--companion", "--no-scheduler"])
    assert result.exit_code == 0, result.output
    assert seen["host"] == seen["bind"] == "0.0.0.0"
    assert seen["companion"].accepts(pairing.load_token(tmp_path))
    assert pairing.load_token(tmp_path) in result.output
    seen.clear()
    result = CliRunner().invoke(cli_app, ["serve", "--companion", "--no-scheduler", "--host", "127.0.0.1"])
    assert result.exit_code == 0 and seen["bind"] == "127.0.0.1" and "http://127.0.0.1:8765" in result.output
    seen.clear()
    result = CliRunner().invoke(cli_app, ["serve", "--no-scheduler"])
    assert result.exit_code == 0 and seen["bind"] == "127.0.0.1" and seen["companion"] is None


@pytest.mark.parametrize("args", [["--companion", "--owned-by-start", "a" * 32]])
def test_start_never_launches_a_companion_server(tmp_path, monkeypatch, args):
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    result = CliRunner().invoke(cli_app, ["serve", *args])
    assert result.exit_code != 0
