"""S15a: real served shell/search/dig routes, fake providers, isolated SQLite."""

import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger.config import AppConfig
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.slice_browser import launch, route_youtube_stub, served_app


@pytest.fixture
def world(tmp_path, monkeypatch, record_property, request):
    options = getattr(request, "param", {})
    providers = SliceFixtures()
    if options.get("matching_library"):
        seed = providers.point("seed-a")
        providers.library_rows += (
            {"id": 4, "path": "/slice-fixture/library/seed-a.flac", "artist": seed.artist,
             "album": "Fixture Seeds", "title": seed.title, "cosine_verified": options.get("library_indexed")},
            {"id": 5, "path": "/slice-fixture/library/local-recording.flac", "artist": seed.artist,
             "album": "Fixture Seeds", "title": "Local Recording", "cosine_verified": None},
        )
    if options.get("empty_library"):
        providers.library_rows = ()
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=tmp_path / "auth.json",
                    cosine_token="" if options.get("no_key") else "fixture-cosine-key",
                    discogs_token="fixture-discogs-key")
    with served_app(tmp_path, monkeypatch, providers=providers, cfg=cfg) as server, sync_playwright() as playwright:
        if options.get("saved_dig"):
            # A returning user's home: one saved dig, made before the page opens (fixture providers need no key).
            created = httpx.post(server.url + "/api/digs", json={"seeds": [providers.point("seed-b").to_dict()]})
            assert created.status_code == 200, created.text
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors, external, requests = [], [], []

        def local_only(route):
            url = urlsplit(route.request.url)
            if route.request.url.startswith(server.url + "/"):
                requests.append((route.request.method, url.path, parse_qs(url.query)))
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        route_youtube_stub(page)
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server.url + "/#/dig/new")
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.evaluate("""async () => {
          const module = await import('/static/slice.js');
          (await module.boot()).destroy();
          let now = 0, serial = 0;
          const timers = new Map();
          window.testClock = {
            now: () => now,
            setTimeout: (fn, delay) => {const id=++serial; timers.set(id,{fn,at:now+delay}); return id;},
            clearTimeout: id => timers.delete(id),
            setInterval: (fn, delay) => {const id=++serial; timers.set(id,{fn,at:now+delay,interval:delay}); return id;},
            clearInterval: id => timers.delete(id)
          };
          window.advance = ms => {
            const target = now + ms;
            for (let i = 0; i < 1000; i++) {
              const next = [...timers].filter(([,t]) => t.at <= target).sort((a,b) => a[1].at-b[1].at)[0];
              if (!next) {now=target; return;}
              const [id,t] = next; now=t.at;
              if (t.interval) t.at += t.interval; else timers.delete(id);
              t.fn();
            }
            throw new Error('Fixture clock failed to settle');
          };
          window.nativeFetch = fetch.bind(window);
          window.transportFetch = (...args) => nativeFetch(...args);
          window.requestTrace = [];
          window.api = (await import('/static/api.js')).createApi({fetch: (...args) => {
            requestTrace.push({path:args[0], method:args[1]?.method || 'GET', at:testClock.now()});
            return transportFetch(...args);
          }});
          window.shell = await module.boot({api, clock:testClock});
          window.store = shell.store;
          window.stateModule = await import('/static/state.js');
          window.bubbledClicks = 0;
          document.getElementById('new-dig-region').addEventListener('click', () => bubbledClicks++);
        }""")
        page.wait_for_function("store.getState().ui.enrichment?.overview != null")
        expect(page.locator("#seed-input")).to_be_focused()
        server.page, server.requests = page, requests
        record_property("browser_mode", "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome")
        try:
            yield server
        finally:
            server.providers.release_all()
            page.evaluate("async () => (await (await import('/static/slice.js')).boot()).destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "Seed-box tests attempted an external network call"


def chip_ids(page):
    return page.locator(".seedbox-chip").evaluate_all("nodes => nodes.map(node => node.dataset.pointId)")


def hold_seed_search(page, query, last_source):
    delayed = []

    def hold_reply(route):
        delayed.append((route, route.fetch()))
        page.evaluate("window.seedReplyHeld = true")

    page.route(f"**/api/search?*&source={last_source}", hold_reply)
    page.locator("#seed-input").fill(query)
    page.evaluate("advance(250)")
    first_source = "library" if last_source == "cosine" else "cosine"
    page.wait_for_function("source => window.seedReplyHeld && !store.getState().ui.search.pending[source]", arg=first_source)
    assert len(delayed) == 1
    return delayed[0]


def release_seed_search(page, held):
    route, response = held
    route.fulfill(response=response)
    page.wait_for_function("!store.getState().ui.search.pending.library && !store.getState().ui.search.pending.cosine")


@pytest.mark.parametrize("gesture", ["down"])
def test_seed_choice_survives_late_cosine(world, gesture):
    page = world.page
    held = hold_seed_search(page, "File", "cosine")
    library_rows = page.locator('.seedbox-result[data-source="library"]')
    expect(library_rows).to_have_count(3)
    chosen = library_rows.nth(0 if gesture == "pointer_first" else 1)
    if gesture == "down":
        page.locator("#seed-input").press("ArrowDown")
    elif gesture == "up":
        page.locator("#seed-input").press("ArrowDown")
        page.locator("#seed-input").press("ArrowDown")
        page.locator("#seed-input").press("ArrowUp")
    else:
        chosen.hover()
        page.mouse.move(0, 0)
    chosen_id = chosen.get_attribute("data-point-id")
    expect(chosen).to_have_attribute("aria-selected", "true")
    release_seed_search(page, held)
    expect(chosen).to_have_attribute("aria-selected", "true")
    expect(page.locator("#seed-input")).to_have_attribute("aria-activedescendant", chosen.get_attribute("id"))
    expect(page.locator('.seedbox-result[data-source="cosine"]')).to_have_attribute("aria-selected", "false")
    page.locator("#seed-input").press("Enter")
    assert chip_ids(page) == [chosen_id]
