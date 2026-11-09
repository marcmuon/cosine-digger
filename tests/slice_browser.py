"""Shared browser launch and production-server fixtures for slice stories."""

import os
import re
import socket
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn

from cosine_digger import chain, client, discogs, storage
from cosine_digger.config import AppConfig
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.ports import bind_loopback


# Fixture facts name this Discogs image (slice_testmode); browser tests serve it
# locally so fixture pages never make an external request.
FIXTURE_ARTWORK = re.compile(r"^https://i\.discogs\.com/slice-fixture\.jpg$")
PIXEL_GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")


def route_youtube_stub(page, *, clock=None):
    """Intercept only the IFrame API script and the fixture artwork; never contact YouTube.

    clock is an optional trusted JavaScript expression for the test's clock
    (now/setTimeout/clearTimeout/setInterval/clearInterval, plus advance for
    harness control). Otherwise the shared stub supplies a monotonic clock.
    Explicitly inject __sliceYTStub.clock into boot/createStore; the app never
    discovers it through query parameters, environment or a production hook.
    Register before navigation. The harness controls __sliceYTStub.advance(ms)
    and inspects __sliceYTStub.calls/players. Normal pages expose none of these.
    """
    source = (Path(__file__).parent / "fixtures" / "yt_iframe_stub.js").read_text()
    if clock is not None:
        source = source.replace("/* harness clock */ null", f"({clock})")
    page.route(re.compile(r"^https://(?:www\.)?youtube\.com/(?:iframe_api|player_api)(?:\?.*)?$"),
               lambda route: route.fulfill(content_type="text/javascript", body=source))
    page.route(FIXTURE_ARTWORK, lambda route: route.fulfill(content_type="image/gif", body=PIXEL_GIF))


def launch(playwright):
    executable = os.environ.get("SLICE_CHROMIUM_EXE")
    if executable:
        return playwright.chromium.launch(
            headless=True, executable_path=executable, args=["--single-process", "--no-zygote"]
        )
    return playwright.chromium.launch(headless=True, channel="chrome")


@contextmanager
def served_app(tmp_path, monkeypatch, *, providers=None, cfg=None, real_cosine=False, **app_options):
    """Serve the real app on an allocated loopback port, with isolated SQLite.

    The caller owns tmp_path and pytest's monkeypatch lifetime. Providers expose
    SliceFixtures' clients/gates; no real provider HTTP or real app home is used.
    real_cosine: the app builds its own cosine.club clients from the current key,
    as in production; point them at a loopback stand-in with COSINE_DIGGER_API_BASE.
    """
    home = Path(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    providers = providers or SliceFixtures()
    cfg = cfg or AppConfig(home=home, db_path=home / "taste.db", ytmusic_auth_path=home / "auth.json",
                           cosine_token="fixture-cosine-key", discogs_token="fixture-discogs-key",
                           daily_size=10, weekly_size=15)
    assert cfg.home.resolve() == home.resolve()
    assert cfg.db_path.resolve().is_relative_to(home.resolve())
    assert cfg.ytmusic_auth_path.resolve().is_relative_to(home.resolve())
    monkeypatch.setenv("COSINE_DIGGER_HOME", str(home))
    for key in ("COSINE_DIGGER_DB", "COSINE_DIGGER_TEST_MODE", "COSINE_DIGGER_FIXTURE",
                "COSINE_DIGGER_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.setattr(discogs, "load_env", lambda: None)
    monkeypatch.setattr(client, "load_env", lambda: None)
    monkeypatch.setattr(discogs, "pace", lambda *args: None)
    monkeypatch.setattr(client, "_retry_wait", lambda *args: 0)
    monkeypatch.setattr(chain, "make_rate_limited_client", providers.discogs_client)
    original_send = httpx.HTTPTransport.handle_request

    def local_only(transport, request):
        assert request.url.host in {"127.0.0.1", "localhost"}, "Browser tests must use fake providers"
        return original_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", local_only)
    with closing(storage.connect(cfg.db_path)) as conn:
        storage.migrate(conn)
        with conn:
            for row in providers.library_rows:
                columns = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", tuple(row.values()))
    app = create_app(cfg, scheduler=False, client_factory=None if real_cosine else providers.cosine_client,
                     discogs_client_factory=providers.discogs_client, **app_options)
    with socket.socket() as sock:
        port = bind_loopback(sock)
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Fixture app did not start"
            yield SimpleNamespace(app=app, cfg=cfg, providers=providers, url=f"http://127.0.0.1:{port}")
        finally:
            providers.release_all()
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive(), "Fixture app did not drain and stop"
