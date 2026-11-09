"""S24 through the served app, actual tags/SQLite and the shared player stub.

These are offline browser simulations. S25 owns the live YouTube proof.
"""

import json
import os
from urllib.parse import urlsplit

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger import youtube
from cosine_digger.commands.doctor import run_checks
from cosine_digger.config import AppConfig, ENV_VARS, SECRET_FIELDS
from cosine_digger.web import connections
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.slice_browser import launch, route_youtube_stub, served_app
from tests.test_slice_library import identity, tagged_file


class SetupProviders(SliceFixtures):
    """Substitute provider HTTP only; keep pacing, retries and checkpoints real."""

    def __init__(self):
        super().__init__()
        self.fail_enrichment = False
        self.retry_enrichment = False
        self.enrichment_queries = []

    def _discogs(self, request):
        if request.url.path == "/database/search" and request.url.params.get("type") == "release":
            self._call("discogs", request)
            query = request.url.params["q"]
            self.enrichment_queries.append(query)
            self._wait("enrichment", request)
            if self.fail_enrichment:
                return httpx.Response(503, json={"message": "Fixture unavailable"})
            if self.retry_enrichment and len(self.enrichment_queries) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "Fixture retry"})
            artist, album = {
                "Fixture Artist A Missing Album A": ("Fixture Artist A", "Missing Album A"),
                "Library Artist B Missing Album B": ("Library Artist B", "Missing Album B"),
                "Alpha Album A": ("Alpha", "Album A"), "Beta Album B": ("Beta", "Album B"),
            }.get(query, (None, None))
            results = [] if artist is None else [{"id": 42, "title": f"{artist} - {album}",
                "label": ["Filled Library Label"], "year": 2025, "genre": ["Electronic"],
                "style": ["Techno"], "resource_url": "https://api.discogs.com/releases/42"}]
            return httpx.Response(200, json={"results": results})
        return super()._discogs(request)


@pytest.fixture
def world(tmp_path, monkeypatch, request, record_property):
    options = getattr(request, "param", {})
    providers = SetupProviders()
    if options.get("empty") or options.get("files"):
        providers.library_rows = ()
    elif options.get("all_labels"):
        providers.library_rows = tuple({**row, "label": "File Label"} for row in providers.library_rows)
    music = tmp_path / "generated-music"
    music.mkdir()
    if options.get("files"):
        tagged_file(music / "raw.m4a", artist="Alpha", album="Album A", title="Raw atom label",
                    label="File Label", year="1999", genre="Original Genre")
        tagged_file(music / "a1.wav", artist="Alpha", album="Album A", title="Missing one")
        tagged_file(music / "a2.wav", artist="Alpha", album="Album A", title="Missing two")
        tagged_file(music / "b.wav", artist="Beta", album="Album B", title="Old missing")
        tagged_file(music / "miss.wav", artist="Gamma", album="Album C", title="No match")
        tagged_file(music / "ineligible.wav", title="No artist or album")
        os.utime(music / "b.wav", (1_000_000_000, 1_000_000_000))
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=tmp_path / "auth.json",
                    cosine_token="fixture-cosine-key", exa_api_key="fixture-exa-key",
                    discogs_token=None if options.get("no_token") else "fixture-discogs-key",
                    daily_size=10, weekly_size=15)
    for key in SECRET_FIELDS:
        # create_app updates these on a save; pytest restores the environment.
        monkeypatch.setenv(ENV_VARS[key], getattr(cfg, key) or "")
    # Provider checks answer from the test, never the network: connected unless a test says otherwise.
    verdicts, login_state = {}, {"state": "connected"}
    for name in list(connections.CHECKS):
        monkeypatch.setitem(connections.CHECKS, name,
                            lambda key, name=name: verdicts.get(name, connections.Check("connected")))

    def health_checks(config, **kwargs):
        # Health's cosine.club check answers as a real one does (its state is the verdict the
        # server records for the key), from the same test verdicts, offline.
        result = run_checks(config, offline=True)
        cosine = result["checks"]["cosine_token"]
        if cosine["status"] == "ok":
            verdict = verdicts.get("cosine_token", connections.Check("connected"))
            cosine.update(state=verdict.state, retry_after_s=verdict.retry_after_s)
        return result

    monkeypatch.setattr("cosine_digger.web.app.run_checks", health_checks)

    def login(headers, path):
        assert path == cfg.ytmusic_auth_path
        if headers != "fixture-browser-headers":
            raise ValueError("Fixture invalid headers")
        path.write_text(json.dumps({"cookie": "fixture-cookie"}))  # A login shape, no real credential.

    monkeypatch.setattr(youtube, "login", login)
    monkeypatch.setattr(youtube, "check_login", lambda path, **kwargs: login_state["state"])
    with served_app(tmp_path, monkeypatch, providers=providers, cfg=cfg) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        requests, errors, external = [], [], []

        def local_only(route):
            if route.request.url.startswith(server.url + "/"):
                path = urlsplit(route.request.url).path
                # Never capture settings secrets, typed keys or pasted login headers.
                body = route.request.post_data_json if route.request.method in {"POST", "PUT"} and path not in {
                    "/api/settings", "/api/youtube/login"} and not path.startswith("/api/connections/") else None
                requests.append((route.request.method, path, body))
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        route_youtube_stub(page)
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server.url + "/#setup")
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.evaluate("""async () => {
          window.shell = await (await import('/static/slice.js')).boot();
          window.store = shell.store;
          window.selectors = (await import('/static/state.js')).selectors;
        }""")
        page.wait_for_function("store.getState().ui.enrichment?.overview != null")
        page.wait_for_selector("#health-list .check")
        page.wait_for_selector("#player-frame iframe")
        server.page, server.context, server.requests = page, context, requests
        server.music, server.file_identity = music, identity(music)
        server.verdicts, server.login_state = verdicts, login_state
        mode = "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome"
        record_property("browser_mode", mode)
        try:
            yield server
        finally:
            providers.release_all()
            page.evaluate("shell.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "Setup tests attempted external network access"


def requests_for(world, method, path):
    return [body for verb, route, body in world.requests if verb == method and route == path]


def card_state(page, card):
    return page.locator(f"#{card}-state")


def test_login_preserves_unsaved_schedule(world):
    page = world.page
    values = {
        "set-daily-size": "22", "set-daily-time": "07:35",
        "set-weekly-size": "40", "set-weekly-time": "08:10",
        "set-weekly-day": "fri", "set-music": str(world.music),
    }
    for field, value in values.items():
        if field == "set-weekly-day":
            page.locator("#" + field).select_option(value)
        else:
            page.locator("#" + field).fill(value)
    page.locator("#discogs-connect").click()
    page.locator("#set-discogs").fill("fixture-saved-discogs")
    world.login_state["state"] = "rejected"
    page.locator("#yt-connect").click()
    page.locator("#yt-headers").fill("fixture-browser-headers")
    page.locator("#yt-go").click()
    expect(card_state(page, "yt")).to_have_text(
        "YouTube Music didn't accept this login. Copy fresh headers and log in again.")
    expect(page.locator("#yt-headers")).to_have_value("")
    world.login_state["state"] = "unreachable"
    page.locator("#yt-headers").fill("fixture-browser-headers")
    page.locator("#yt-go").click()
    expect(card_state(page, "yt")).to_have_text("Couldn't reach YouTube Music to check the login. Try again.")
    # The headers stay for a check that got no answer, so Log in can simply run again.
    expect(page.locator("#yt-headers")).to_have_value("fixture-browser-headers")
    for field, value in values.items():
        expect(page.locator("#" + field)).to_have_value(value)
    expect(page.locator("#set-discogs")).to_have_value("fixture-saved-discogs")
    assert not requests_for(world, "PUT", "/api/settings")

    page.locator("#settings-save").click()
    expect(page.locator("#settings-note")).to_contain_text("Saved:")
    settings = page.request.get(world.url + "/api/settings").json()
    assert {key: settings[key] for key in (
        "daily_size", "daily_time", "weekly_size", "weekly_time", "weekly_day", "music_dir",
    )} == {
        "daily_size": 22, "daily_time": "07:35", "weekly_size": 40,
        "weekly_time": "08:10", "weekly_day": "fri", "music_dir": None,
    }
    # The folder field belongs to Scan now, and an unsent key stays where it was typed.
    expect(page.locator("#set-music")).to_have_value(str(world.music))
    expect(page.locator("#set-discogs")).to_have_value("fixture-saved-discogs")
    expect(card_state(page, "discogs")).to_have_text("Token saved")


@pytest.mark.parametrize("save_other_field", [False, True])
def test_settings_refresh_preserves_dirty_fields_until_saved(world, save_other_field):
    page = world.page
    page.locator("#set-daily-size").fill("22")
    page.locator("#set-music").fill(str(world.music))
    # Simulate another settings writer through the real server, then reload
    # through the store. Clean fields follow the server; edited fields survive.
    response = page.request.put(world.url + "/api/settings", data={
        "daily_size": 13, "music_dir": str(world.cfg.home), "weekly_size": 30,
    })
    assert response.ok
    page.evaluate("store.actions.loadSetup()")
    expect(page.locator("#set-daily-size")).to_have_value("22")
    expect(page.locator("#set-music")).to_have_value(str(world.music))
    expect(page.locator("#set-weekly-size")).to_have_value("30")

    with page.expect_request(lambda request: request.method == "PUT" and
                             urlsplit(request.url).path == "/api/settings") as submitted:
        page.locator("#settings-save").click()
    # Only the schedule's visible edits: the folder is saved by Scan now, keys by their checks.
    assert submitted.value.post_data_json == {"daily_size": 22}
    expect(page.locator("#settings-note")).to_contain_text("Saved:")
    expect(page.locator("#set-music")).to_have_value(str(world.music))

    assert page.request.put(world.url + "/api/settings", data={"daily_size": 14}).ok
    page.evaluate("store.actions.loadSetup()")
    expect(page.locator("#set-daily-size")).to_have_value("14")

    # Save also acknowledges an edit that the refreshed server already has.
    page.locator("#set-daily-size").fill("22")
    assert page.request.put(world.url + "/api/settings", data={"daily_size": 22}).ok
    page.evaluate("store.actions.loadSetup()")
    if save_other_field:
        page.locator("#set-weekly-size").fill("40")
    page.locator("#settings-save").click()
    expect(page.locator("#settings-note")).to_contain_text("Saved:" if save_other_field else "Nothing changed.")
    assert page.request.put(world.url + "/api/settings", data={"daily_size": 16}).ok
    page.evaluate("store.actions.loadSetup()")
    assert page.locator("#set-daily-size").input_value() == "16"
