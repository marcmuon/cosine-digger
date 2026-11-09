"""Step 3 first run against a stand-in cosine.club: the key screen, the seed box and a dig
step, each with cosine.club answering 200, 401, 429, 503 or not at all (P1-P3, P9, P11,
P12, P16, A4, A5).

The app builds its own cosine.club clients from the key in use, as in production
(served_app real_cosine=True); they reach only the loopback stub. Keys are synthetic,
and every browser session checks that no key reaches an API reply, the console, browser
storage or any file in the home but settings.json.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
import re
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger import client, youtube
from cosine_digger.commands import doctor as doctor_command
from cosine_digger.commands.doctor import run_checks
from cosine_digger.config import AppConfig
from cosine_digger.web import connections, slice_jobs, slice_routes
from cosine_digger.web.slice_testmode import SliceFixtures
from tests import slice_browser
from tests.lifecycle_support import free_port, release_ports
from tests.slice_browser import launch, route_youtube_stub, served_app
from tests.stub_cosine import StubCosine

KEY = "cc-synthetic-key-0001"
NEW_KEY = "cc-synthetic-key-0002"
SAME_END_KEY = "cc-synthetic-alt-0001"  # another key that ends like KEY
SECRETS = [KEY, NEW_KEY, SAME_END_KEY]
LINK = "https://cosine.club/track/stub-seed"
SEED_NAME = "Stub Artist - Seed Track"
NOTHING_SELECTED = "▶ PLAY STEP or P plays this step · ↓ selects a track"
STATES = {"200": "ok", "401": "401", "429": "429", "503": "503", "no_answer": "silent"}
SURFACES = ["key_screen", "search", "step"]

# Boots the real shell once, on the YouTube stub's clock, with an api whose fetch keeps each
# reply's text for the key canary. The production modules gain no test hook.
HARNESS = """
  const sdk = document.createElement('script');
  sdk.src = 'https://www.youtube.com/iframe_api';
  await new Promise(resolve => { sdk.onload = resolve; document.head.append(sdk); });
  const device = document.getElementById('device');
  device.id = 'key-harness-device';
  const {boot} = await import('/static/slice.js?key-harness');
  device.id = 'device';
  window.stateModule = await import('/static/state.js');
  const {createApi} = await import('/static/api.js');
  window.replies = [];
  const api = createApi({fetch: async (path, options) => {
    const response = await fetch(path, options);
    response.clone().text().then(body => replies.push(body), () => {});
    return response;
  }});
  window.shell = await boot({api, clock: __sliceYTStub.clock});
  window.store = shell.store;
"""


@pytest.fixture
def stub(monkeypatch):
    """cosine.club on loopback; the app's own clients and key checks reach only it."""
    # Short waits, so "no answer" answers in about a second (the stub holds the request).
    monkeypatch.setattr(connections, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(client, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(client, "INTERACTIVE_TIMEOUT_S", 1.0)
    # CosineClient falls back to COSINE_API_TOKEN and a saved key is written there: start
    # and end without it.
    monkeypatch.setenv("COSINE_API_TOKEN", "")
    monkeypatch.delenv("COSINE_API_TOKEN")
    with StubCosine(free_port()) as server:
        monkeypatch.setenv("COSINE_DIGGER_API_BASE", server.base)
        yield server
    release_ports()


@contextmanager
def digger(tmp_path, monkeypatch, *, key, source="settings"):
    """A new user's Digger: no library, no Discogs token, no YouTube login; key is the
    cosine.club key in use (saved in settings, or from the environment), or None."""
    providers = SliceFixtures()
    providers.library_rows = ()
    home = Path(tmp_path) / "home"
    cfg = AppConfig(home=home, db_path=home / "taste.db", ytmusic_auth_path=home / "auth.json",
                    cosine_token=key, discogs_token=None,
                    sources={"cosine_token": source} if key else {})
    with served_app(home, monkeypatch, providers=providers, cfg=cfg, real_cosine=True) as server:
        server.home = home
        yield server


@contextmanager
def chrome(server, path):
    with sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors, external, console, requests = [], [], [], []

        def local_only(route):
            if route.request.url.startswith(server.url + "/"):
                # Method and path only: a key check's body holds the key.
                requests.append((route.request.method, route.request.url.removeprefix(server.url).split("?")[0]))
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console.append(message.text))
        route_youtube_stub(page)
        page.route("**/static/slice.js", lambda route: route.fulfill(content_type="text/javascript", body=HARNESS))
        page.goto(server.url + path)
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.wait_for_function("window.store && store.getState().ui.server === 'up'")
        ui = SimpleNamespace(page=page, requests=requests, console=console)
        try:
            yield ui
            assert_no_key_left(server, ui)
        finally:
            page.evaluate("shell.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "First-run tests attempted an external request"


def assert_no_key_left(server, ui):
    """The canary: a key appears only in settings.json, never in a reply, the console or storage."""
    shown = ui.page.evaluate("""keys => {
      const seen = [JSON.stringify({...localStorage}), JSON.stringify({...sessionStorage}), ...replies];
      return keys.filter(key => seen.some(text => text.includes(key)));
    }""", SECRETS)
    assert shown == []
    assert not [line for line in ui.console if any(key in line for key in SECRETS)]
    assert set(files_with_key(server)) <= {"settings.json"}


def files_with_key(server, keys=SECRETS):
    return sorted(path.name for path in server.home.rglob("*")
                  if path.is_file() and any(key.encode() in path.read_bytes() for key in keys))


def settings(server):
    return httpx.get(server.url + "/api/settings").json()["cosine_token"]


def api_calls(stub):
    """Requests to cosine.club's API. Other programs on a test machine probe new loopback
    listeners (a Go-http-client sending GET / was seen); those aren't Digger's."""
    return [seen for seen in stub.seen if seen.path.startswith("/api/v1/")]


def search(page, query):
    page.locator("#seed-input").fill(query)
    page.evaluate("__sliceYTStub.advance(250)")
    page.wait_for_function("!store.getState().ui.search.pending.library && !store.getState().ui.search.pending.cosine")


def cosine_note(page):
    return page.locator('.seedbox-section[data-source="cosine"] .seedbox-note')


def cosine_results(page):
    return page.locator('.seedbox-result[data-source="cosine"]')


def wait_step(page, step_id=None):
    """Wait until the step (default: the one on screen) has finished; return its id."""
    step_id = step_id or page.evaluate("store.getState().ui.route.stepId")
    page.wait_for_function("id => ['done', 'failed', 'stopped'].includes(store.getState().steps.byId[id]?.status)",
                           arg=step_id)
    return step_id


def start_dig(page, stub, mode):
    """Add cosine.club's match for the seed (cosine.club answering), then start the dig with
    cosine.club answering `mode`."""
    search(page, "Seed Track")
    cosine_results(page).filter(has_text="Seed Track").click()
    expect(page.locator(".seedbox-chip")).to_have_count(1)
    stub.mode = mode
    page.locator('[data-action="start-dig"]').click()
    page.wait_for_function("store.getState().ui.route.stepId != null")
    return wait_step(page)


def connect(page, key):
    page.locator("#connect-key").fill(key)
    page.locator('[data-action="connect"]').click()


def wait_server_step(server, step_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        step = httpx.get(f"{server.url}/api/steps/{step_id}").json()["snapshot"]["step"]
        if step["status"] != "running":
            return step
        time.sleep(0.1)
    raise AssertionError("The step did not finish")


KEY_SCREEN = {
    "401": "! cosine.club didn't accept that key. Copy it again from your account page.",
    "429": "! cosine.club asked Digger to wait 7 seconds. Try again then.",
    "503": "! cosine.club had a server error while checking the key. Try again in 30 seconds.",
    "no_answer": "! Couldn't reach cosine.club to check the key. Check your connection, then try again.",
}
SEARCH_NOTES = {
    "401": "! cosine.club didn't accept your key. Fix it to search and dig. Fix key",
    "429": "! cosine.club is busy. Try again in 7 s.",
    "503": "! cosine.club didn't answer: server error. Try again in 30 s.",
    "no_answer": "! Couldn't reach cosine.club. Check your connection.",
}
STEP_ERRORS = {
    "401": ("! cosine.club didn't accept your key, so this step didn't run. Nothing was lost.", "fix-key", "Fix key"),
    "429": ("! cosine.club didn't answer: rate limited. Nothing was lost.", "retry", r"Retry in [1-7] s"),
    "503": ("! cosine.club didn't answer: server error. Nothing was lost.", "retry", r"Retry in (2\d|30) s"),
    "no_answer": ("! Couldn't reach cosine.club. Nothing was lost.", "retry", "Retry"),
}


def key_screen(server, ui, stub, state):
    page = ui.page
    # A4: a home with no key and no saved digs opens on the key screen.
    page.wait_for_function("location.hash === '#connect'")
    expect(page.locator("#connect-heading")).to_have_text("Connect cosine.club")
    expect(page.locator("#connect-key")).to_be_focused()
    expect(page.locator("#key-notice")).to_be_hidden()
    if state == "200":
        # Nothing to check, or a key that can't be sent: no request.
        page.locator('[data-action="connect"]').click()
        expect(page.locator("#connect-status")).to_have_text("Paste your key first.")
        connect(page, "cc synthetic")
        expect(page.locator("#connect-status")).to_have_text(
            "That key has a space or line break in it. Copy it again and paste it here.")
        assert api_calls(stub) == []
    stub.mode = STATES[state]
    connect(page, KEY)
    if state == "no_answer":
        expect(page.locator('[data-action="connect"]')).to_have_text("Checking…")
        expect(page.locator('[data-action="connect"]')).to_be_disabled()
    if state != "200":
        expect(page.locator("#connect-status")).to_have_text(KEY_SCREEN[state])
        assert page.evaluate("location.hash") == "#connect"
        # A5: only a key cosine.club accepted is saved.
        assert settings(server)["set"] is False
        assert files_with_key(server) == []
        assert stub.count("search") == 1
        if state != "no_answer":
            expect(page.locator('[data-action="connect-retry"]')).to_be_hidden()
            return
        # No answer offers Try again, which checks the same key once more.
        stub.mode = "ok"
        page.locator('[data-action="connect-retry"]').click()
    page.wait_for_function("location.hash === '#/dig/new'")
    expect(page.locator("#toast-region")).to_contain_text("Connected to cosine.club.")
    saved = settings(server)
    assert (saved["set"], saved["source"], saved["checked"], saved["hint"]) == (True, "settings", "connected", "…0001")
    assert files_with_key(server) == ["settings.json"]
    assert page.locator("#connect-key").input_value() == ""
    search(page, "Seed Track")
    expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()


def search_surface(server, ui, stub, state):
    page = ui.page
    stub.mode = STATES[state]
    search(page, "Seed Track")
    if state == "200":
        expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()
        expect(cosine_note(page)).to_be_hidden()
        assert settings(server)["checked"] == "connected"
        return
    expect(cosine_note(page)).to_have_text(SEARCH_NOTES[state])
    expect(cosine_results(page)).to_have_count(0)
    if state == "401":
        # The key in use is known to be refused: Start says so instead of making a dig that fails.
        page.wait_for_function("stateModule.selectors.cosineKey(store.getState()).state === 'rejected'")
        assert settings(server)["checked"] == "rejected"
        page.locator("#seed-input").fill(LINK)
        expect(page.locator("#seed-link-result")).to_be_visible()
        page.locator("#seed-input").press("Enter")
        page.locator("#seed-input").press("Enter")
        expect(page.locator("#seed-blocked")).to_have_text(SEARCH_NOTES["401"])
        assert ("POST", "/api/digs") not in ui.requests
    elif state == "429":
        # The note counts down on the app's clock, then searches once more by itself.
        page.evaluate("__sliceYTStub.advance(1000)")
        expect(cosine_note(page)).to_have_text("! cosine.club is busy. Try again in 6 s.")
        searches = stub.count("search")
        stub.mode = "ok"
        # 6 s to the end of the wait, then the search's usual 250 ms pause.
        page.evaluate("__sliceYTStub.advance(6250)")
        expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()
        expect(cosine_note(page)).to_be_hidden()
        assert stub.count("search") == searches + 1
    else:
        # A wait the server didn't name, or no answer, never marks the key itself.
        assert settings(server)["checked"] is None


def step_surface(server, ui, stub, state):
    page = ui.page
    step_id = start_dig(page, stub, STATES[state])
    step = page.evaluate("id => store.getState().steps.byId[id]", step_id)
    body = page.locator(".steplist-body").filter(has=page.locator(".steplist-error"))
    if state == "200":
        assert step["status"] == "done"
        expect(page.locator(".steplist-row").first).to_be_visible()
        expect(page.locator(".steplist-error")).to_have_count(0)
        expect(page.locator(".steplist-sentence")).to_have_text("Sounds like " + SEED_NAME)
        return
    line, action, label = STEP_ERRORS[state]
    assert step["status"] == "failed"
    expect(page.locator(".steplist-error")).to_have_text(line)
    expect(body).to_contain_text("Your other steps and the player keep working.")
    expect(body.locator(f'[data-action="{action}"]')).to_have_text(re.compile(f"^{label}$"))
    if state == "401":
        # Retry would fail the same way, so the key is the way back.
        expect(body.locator('[data-action="retry"]')).to_have_count(0)
        assert settings(server)["checked"] == "rejected"
    else:
        expect(body.locator('[data-action="fix-key"]')).to_have_count(0)
        expect(body.locator(f'[data-action="{action}"]')).to_be_enabled(enabled=state == "no_answer")


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("surface", SURFACES)
def test_key_states(tmp_path, monkeypatch, stub, surface, state):
    key = None if surface == "key_screen" else KEY
    with digger(tmp_path, monkeypatch, key=key) as server, \
            chrome(server, "/" if surface == "key_screen" else "/#/dig/new") as ui:
        {"key_screen": key_screen, "search": search_surface, "step": step_surface}[surface](server, ui, stub, state)
        assert all(seen.with_key for seen in api_calls(stub)), stub.seen


@pytest.mark.parametrize("surface", ["step", "search"])
def test_fix_key_returns(tmp_path, monkeypatch, stub, surface):
    """FIX KEY opens the key screen; a key cosine.club accepts brings you back where you were,
    and the next call uses it (the stub keeps refusing the old one)."""
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        if surface == "step":
            # The step fails before the page first reads the dig, so no live error event arrives:
            # what the page reads afterwards has to turn Retry into Fix key.
            first_read = []

            def after_failure(route):
                if not first_read:
                    first_read.append(route.request.url)
                    dig = httpx.get(server.url + "/api/digs/" + route.request.url.rsplit("/", 1)[-1]).json()
                    wait_server_step(server, dig["root_step_id"])
                route.fallback()

            page.route(re.compile(r".*/api/digs/[0-9a-f]+$"), after_failure)
            step_id = start_dig(page, stub, "401")
            assert first_read
            origin = page.evaluate("location.hash")
            fix = page.locator('.steplist-body [data-action="fix-key"]')
        else:
            stub.refuse = {KEY}
            search(page, "Seed Track")
            origin = "#/dig/new"
            fix = cosine_note(page).locator('[data-action="fix-key"]')
        stub.mode, stub.refuse = "ok", {KEY}
        fix.click()
        page.wait_for_function("location.hash === '#connect'")
        expect(page.locator("#connect-key")).to_be_focused()
        connect(page, NEW_KEY)
        page.wait_for_function("hash => location.hash === hash", arg=origin)
        # Back on a new dig, the search runs again after its usual 250 ms pause.
        page.evaluate("__sliceYTStub.advance(250)")
        expect(page.locator("#toast-region")).to_contain_text("Connected to cosine.club.")
        assert files_with_key(server, [NEW_KEY]) == ["settings.json"]
        assert files_with_key(server, [KEY]) == []
        if surface == "step":
            # Back on the failed step with Retry focused; Retry now digs with the new key.
            retry = page.locator('.steplist-body [data-action="retry"]')
            expect(retry).to_be_focused()
            retry.press("Enter")
            page.wait_for_function("""() => {
              const s = store.getState();
              return s.steps.byId[s.ui.route.stepId]?.status === 'done';
            }""")
            expect(page.locator(".steplist-row").first).to_be_visible()
            assert step_id in {step["id"] for step in page.evaluate("Object.values(store.getState().steps.byId)")}
        else:
            # The search runs again with the new key.
            expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()
            expect(cosine_note(page)).to_be_hidden()


def test_deep_link_with_digs(tmp_path, monkeypatch, stub):
    """A4: a home with saved digs and no key opens them (deep link or not); the key screen is
    a notice there, never a gate, and nothing asks cosine.club anything."""
    with digger(tmp_path, monkeypatch, key=KEY) as server:
        created = httpx.post(server.url + "/api/digs", json={"seeds": [
            {"point_id": "link:" + LINK, "kind": "link", "name": LINK, "url": LINK}]})
        assert created.status_code == 200, created.text
        made = created.json()
        assert wait_server_step(server, made["step_id"])["status"] == "done"
    asked = len(api_calls(stub))
    # The same home, started again without a key.
    with digger(tmp_path, monkeypatch, key=None) as server:
        deep = f"/#/dig/{made['dig_id']}/step/{made['step_id']}?view=list"
        with chrome(server, deep) as ui:
            page = ui.page
            assert page.evaluate("location.hash").startswith(f"#/dig/{made['dig_id']}/step/{made['step_id']}")
            expect(page.locator(".steplist-sentence")).to_have_text("Sounds like " + SEED_NAME)
            expect(page.locator("#key-notice")).to_contain_text(
                "Add your cosine.club key to search and start new digs. Your saved digs still open and play.")
            # Saved digs still play.
            page.locator('#transport [data-action="play"]').click()
            page.wait_for_function("store.getState().player.state === 'playing'")
            page.evaluate("location.hash = '#/dig/new'")
            expect(page.locator("#seed-input")).to_be_visible()
            assert page.evaluate("location.hash") == "#/dig/new"
            expect(page.locator("#key-notice")).to_be_visible()
            page.locator('#key-notice [data-action="add-key"]').click()
            page.wait_for_function("location.hash === '#connect'")
            expect(page.locator("#key-notice")).to_be_hidden()
        assert len(api_calls(stub)) == asked


def test_link_seed_first_play(tmp_path, monkeypatch, stub):
    """Minimum credentials: only a cosine.club key. Paste a link, dig, press Play, keep it."""
    with digger(tmp_path, monkeypatch, key=None) as server, chrome(server, "/") as ui:
        page = ui.page
        page.wait_for_function("location.hash === '#connect'")
        connect(page, KEY)
        page.wait_for_function("location.hash === '#/dig/new'")
        expect(page.locator("#seed-input")).to_be_focused()
        page.locator("#seed-input").fill(LINK)
        expect(page.locator("#seed-link-result")).to_be_visible()
        page.locator("#seed-input").press("Enter")
        expect(page.locator('[data-action="start-dig"]')).to_have_text("Dig from 1 seed ▸")
        # The order P12 must survive: every read of the dig comes before the link resolves, and
        # the page learns of the resolution only from a later snapshot (its follow cursor is then
        # past the resolve note, so the note never arrives live). The first read of the step
        # passes at once; later ones wait until the step is done.
        stub.delays["similar"] = 0.5
        step_reads = []

        def resolved_later(route):
            step_reads.append(route.request.url)
            if len(step_reads) > 1:
                wait_server_step(server, route.request.url.rsplit("/", 1)[-1])
            route.fallback()

        page.route(re.compile(r".*/api/steps/[0-9a-f]+$"), resolved_later)
        page.locator("#seed-input").press("Enter")
        page.wait_for_function("store.getState().ui.route.stepId != null")
        step_id = wait_step(page)
        dig_id = page.evaluate("store.getState().ui.route.digId")
        assert len(step_reads) > 1 and page.evaluate("id => store.getState().steps.byId[id].status", step_id) == "done"
        # P12: the dig and its first step read as the track the link found, not as the link.
        expect(page.locator(".steplist-sentence")).to_have_text("Sounds like " + SEED_NAME)
        expect(page.locator(".trail-name")).to_have_text(SEED_NAME)
        assert httpx.get(f"{server.url}/api/digs/{dig_id}").json()["name"] == SEED_NAME
        # P16: nothing is selected yet, so the hint names what works without a selection.
        expect(page.locator("#lcd-from")).to_have_text(NOTHING_SELECTED)
        page.locator('#transport [data-action="play"]').click()
        page.wait_for_function("store.getState().player.state === 'playing'")
        first = page.evaluate("""id => {
          const s = store.getState();
          return s.picks.byId[s.player.current.pickId];
        }""", step_id)
        assert first["step_id"] == step_id and first["rank"] == 1
        loads = page.evaluate("JSON.stringify(__sliceYTStub.calls.filter(c => c.method === 'loadVideoById'))")
        assert first["video_id"] in loads
        page.keyboard.press("k")
        page.wait_for_function("key => store.getState().judgments.byTrack[key]?.verdict === 'keep'",
                               arg=first["track_key"])
        assert_no_key_left(server, ui)
        page.reload()
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.wait_for_function("window.store && store.getState().ui.server === 'up'")
        expect(page.locator(".steplist-sentence")).to_have_text("Sounds like " + SEED_NAME)
        page.wait_for_function("key => store.getState().judgments.byTrack[key]?.verdict === 'keep'",
                               arg=first["track_key"])
        assert files_with_key(server) == ["settings.json"]


# Key verdicts under concurrent saves and proxies (R2 review). Each test forces the order
# another tab or the environment would produce, and fails on the code before the fix.

LINK_SEED = {"point_id": "link:" + LINK, "kind": "link", "name": LINK, "url": LINK}


def save_key(server, key):
    reply = httpx.post(server.url + "/api/connections/cosine_token", json={"key": key}).json()
    assert reply["state"] == "connected"


def test_stand_in_is_never_reached_through_a_proxy(tmp_path, monkeypatch, stub):
    """The loopback stand-in is plain http, so a proxy from the environment would read the
    Authorization header: the key check, the client and doctor all skip it."""
    with StubCosine(free_port()) as proxy:
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        assert connections.check("cosine_token", KEY).state == "connected"
        with client.CosineClient(KEY) as cosine:
            assert cosine.search("Seed Track", retry=False)
        cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", cosine_token=KEY)
        assert run_checks(cfg, offline=False, fixture_mode=False)["checks"]["cosine_token"]["status"] == "ok"
        assert proxy.seen == []
    assert [(seen.path, seen.key4) for seen in api_calls(stub)] == [("/api/v1/search", "0001")] * 3


@pytest.mark.parametrize("call", ["search", "step"])
def test_calls_send_the_key_their_verdict_is_about(tmp_path, monkeypatch, stub, call):
    """Another tab saves a new key after a search or a step read the key in use, before it
    calls cosine.club: the call sends the key it read, so a 401 is never pinned on the wrong
    key and a refused key never hides behind a stale verdict."""
    with digger(tmp_path, monkeypatch, key=KEY) as server:
        fired = []

        def other_tab_saves(real):
            def hook(*args, **kwargs):
                if not fired:
                    fired.append(call)
                    save_key(server, NEW_KEY)
                    stub.refuse = {NEW_KEY}  # and cosine.club refuses it from now on
                return real(*args, **kwargs)
            return hook

        if call == "search":
            monkeypatch.setattr(slice_routes, "usable_key", other_tab_saves(slice_routes.usable_key))
            reply = httpx.get(server.url + "/api/search", params={"q": "Seed Track", "source": "cosine"}).json()
            assert reply["notes"] == [] and reply["items"]
        else:
            monkeypatch.setattr(slice_jobs, "load_exclusions", other_tab_saves(slice_jobs.load_exclusions))
            created = httpx.post(server.url + "/api/digs", json={"seeds": [LINK_SEED]}).json()
            assert wait_server_step(server, created["step_id"])["status"] == "done"
        assert fired == [call]
        calls = [seen.key4 for seen in api_calls(stub)]
        assert calls[0] == "0002" and set(calls[1:]) == {"0001"}, calls  # the save's check, then KEY only
        assert settings(server)["hint"] == "…0002" and settings(server)["checked"] == "connected"


def test_recheck_overtaken_by_a_save_is_superseded(tmp_path, monkeypatch, stub):
    """Checking the key in use while another tab saves a new one: the check describes a key no
    longer in use, so it never answers connected (the page would announce it)."""
    with digger(tmp_path, monkeypatch, key=KEY) as server:
        real = connections.CHECKS["cosine_token"]

        def overtaken(key):
            if key == KEY:
                save_key(server, NEW_KEY)
            return real(key)

        monkeypatch.setitem(connections.CHECKS, "cosine_token", overtaken)
        reply = httpx.post(server.url + "/api/connections/cosine_token", json={}).json()
        assert reply["state"] == "superseded"
        assert (reply["settings"]["cosine_token"]["hint"], reply["settings"]["cosine_token"]["checked"]) == \
            ("…0002", "connected")


def test_search_refused_for_a_replaced_key_searches_again(tmp_path, monkeypatch, stub):
    """cosine.club refuses the key a search sent, but another tab has meanwhile saved a key it
    accepts: the page reads the settings, finds a new usable key and searches once more,
    instead of offering FIX KEY for a key that works."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page, fired = ui.page, []
        real = slice_routes.usable_key

        def other_tab_saves(*args):
            if not fired:
                fired.append(True)
                save_key(server, NEW_KEY)
            return real(*args)

        monkeypatch.setattr(slice_routes, "usable_key", other_tab_saves)
        search(page, "Seed Track")
        generation = page.evaluate("store.getState().ui.search.generation")
        page.wait_for_function("g => store.getState().ui.search.generation > g", arg=generation)
        page.evaluate("__sliceYTStub.advance(250)")
        expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()
        expect(cosine_note(page)).to_be_hidden()
        assert [seen.key4 for seen in api_calls(stub)] == ["0002", "0001", "0002"]


def test_environment_key_check_offers_try_again_after_a_wait(tmp_path, monkeypatch, stub):
    """A key from the environment has no field or Connect button: when its check is told to
    wait (429) or meets a server error, Try again checks it once more."""
    stub.mode = "429"
    with digger(tmp_path, monkeypatch, key=KEY, source="env") as server, chrome(server, "/#connect") as ui:
        page = ui.page
        expect(page.locator("#connect-status")).to_have_text(KEY_SCREEN["429"])
        expect(page.locator('[data-action="connect"]')).to_be_hidden()
        stub.mode = "ok"
        page.locator('[data-action="connect-retry"]').click()
        page.wait_for_function("location.hash === '#/dig/new'")
        assert [seen.key4 for seen in api_calls(stub)] == ["0001", "0001"]
        assert settings(server)["checked"] == "connected" and settings(server)["source"] == "env"


# Deep review of the whole change (2026-10-06) and the R2 re-check. Each test forces the order a
# second tab, a restart or a failing read would produce, and fails on the code before its fix.

def hold_first(page, pattern):
    """Answer the first request matching pattern from the server as it is now, but hand the answer
    to the page only when the test says so (an answer that arrives late)."""
    held = []

    def hold(route):
        if held:
            route.fallback()
        else:
            held.append((route, route.fetch().text()))

    page.route(pattern, hold)
    return held


def wait_held(page, held):
    deadline = time.monotonic() + 10
    while not held:
        assert time.monotonic() < deadline, "The request never came"
        page.wait_for_timeout(50)


def release(held):
    route, body = held[0]
    route.fulfill(status=200, content_type="application/json", body=body)


def test_a_step_started_without_a_key_never_sends_one_saved_meanwhile(tmp_path, monkeypatch, stub):
    """R2 re-check #4: a step that read no key built a client that fell back to the environment,
    where a key saved meanwhile is written, so it sent that key and its refusal was pinned on no
    key. The step fails for the key it read (none) and sends nothing."""
    with digger(tmp_path, monkeypatch, key=None) as server:
        fired = []
        real = slice_jobs.load_exclusions

        def other_tab_saves(*args, **kwargs):
            if not fired:
                fired.append(True)
                save_key(server, NEW_KEY)
                stub.refuse = {NEW_KEY}  # and cosine.club refuses it from now on
            return real(*args, **kwargs)

        monkeypatch.setattr(slice_jobs, "load_exclusions", other_tab_saves)
        created = httpx.post(server.url + "/api/digs", json={"seeds": [LINK_SEED]}).json()
        step = wait_server_step(server, created["step_id"])
        assert fired and (step["status"], step["error"]["code"]) == ("failed", "cosine_key_missing")
        assert [seen.key4 for seen in api_calls(stub)] == ["0002"]  # the save's own check only
        assert (settings(server)["hint"], settings(server)["checked"]) == ("…0002", "connected")


def test_health_check_is_a_verdict_on_the_key_it_sent(tmp_path, monkeypatch, stub):
    """R2 re-check #10: Health's cosine.club check is a real call. A refusal marks the key it sent;
    a key saved while it ran keeps its own verdict."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server:
        check = httpx.get(server.url + "/api/doctor").json()["checks"]["cosine_token"]
        assert check["state"] == "rejected" and settings(server)["checked"] == "rejected"
        real, fired = doctor_command.api_base, []

        def other_tab_saves():
            if not fired:
                fired.append(True)
                save_key(server, NEW_KEY)
            return real()

        monkeypatch.setattr(doctor_command, "api_base", other_tab_saves)
        assert httpx.get(server.url + "/api/doctor").json()["checks"]["cosine_token"]["state"] == "rejected"
        assert (settings(server)["hint"], settings(server)["checked"]) == ("…0002", "connected")
        assert [seen.key4 for seen in api_calls(stub)] == ["0001", "0002", "0001"]


def test_a_late_health_answer_never_speaks_for_a_replacement_key(tmp_path, monkeypatch, stub):
    """R2 re-check #10: Health checks the key in use while the user connects another and comes
    back to Setup; Health's late refusal of the old key showed Key rejected for the new one."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        held = hold_first(page, "**/api/doctor*")
        page.evaluate("location.hash = '#setup'")
        wait_held(page, held)
        assert settings(server)["checked"] == "rejected"  # the server's verdict on the key Health sent
        page.evaluate("location.hash = '#connect'")
        connect(page, NEW_KEY)
        page.wait_for_function("location.hash === '#setup'")
        release(held)
        expect(page.locator("#health-refresh")).to_be_enabled()
        expect(page.locator("#cosine-state")).to_have_text("Connected · key ends …0002")
        expect(page.locator("#cosine-fix")).to_be_hidden()
        expect(page.locator("#health-needed")).to_have_text("Needed to dig: cosine.club key ✓")
        assert [seen.key4 for seen in api_calls(stub)] == ["0001", "0002"]


def test_an_older_settings_read_never_undoes_an_accepted_key(tmp_path, monkeypatch, stub):
    """Deep A2 #1: a settings read asked before a key was accepted answered after it, and put the
    old "no key" back: Start was blocked again for a key cosine.club had just accepted."""
    with digger(tmp_path, monkeypatch, key=None) as server, chrome(server, "/") as ui:
        page = ui.page
        page.wait_for_function("location.hash === '#connect'")
        held = hold_first(page, "**/api/settings")
        page.evaluate("store.actions.loadSettings().then(() => { window.staleRead = true; }); null")
        wait_held(page, held)
        connect(page, KEY)
        page.wait_for_function("location.hash === '#/dig/new'")
        release(held)
        page.wait_for_function("window.staleRead === true")
        assert page.evaluate("stateModule.selectors.cosineKey(store.getState()).usable") is True
        expect(page.locator("#key-notice")).to_be_hidden()
        search(page, "Seed Track")
        expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()


def test_environment_key_is_checked_again_after_a_restart(tmp_path, monkeypatch, stub):
    """Deep A2 #2: the key screen checked a key from the environment once per visit. After its own
    advice (stop, change the variable, start), it kept the old key's refusal and never checked
    the new key."""
    stub.refuse = {KEY}
    any_port = slice_browser.bind_loopback

    def reusable(sock):
        # Linux lets the restarted Digger bind this port while the first one's closed connections
        # linger only if both sockets set SO_REUSEADDR; macOS needs it on the new one alone.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return any_port(sock)

    monkeypatch.setattr(slice_browser, "bind_loopback", reusable)
    with ExitStack() as later:
        first = ExitStack()
        later.callback(first.close)
        server = first.enter_context(digger(tmp_path, monkeypatch, key=KEY, source="env"))
        port = int(server.url.rsplit(":", 1)[1])
        with chrome(server, "/#connect") as ui:
            page = ui.page
            expect(page.locator("#connect-status")).to_have_text(KEY_SCREEN["401"])
            first.close()  # cosine-digger stop
            page.evaluate("__sliceYTStub.advance(5000)")  # the next health poll finds Digger gone
            page.wait_for_function("store.getState().ui.server === 'down'")

            def same_address(sock):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return port

            # cosine-digger start, with the variable now naming another key.
            monkeypatch.setattr(slice_browser, "bind_loopback", same_address)
            later.enter_context(digger(tmp_path, monkeypatch, key=NEW_KEY, source="env"))
            page.evaluate("__sliceYTStub.advance(5000)")
            page.wait_for_function("location.hash === '#/dig/new'")
            expect(page.locator("#toast-region")).to_contain_text("Connected to cosine.club.")
        assert [seen.key4 for seen in api_calls(stub)] == ["0001", "0002"]


@pytest.mark.parametrize("answer", ["401", "503"])
def test_setup_health_check_is_what_start_knows(tmp_path, monkeypatch, stub, answer):
    """Deep A2 #3: Setup's Health check said Key rejected while Start still took the key for usable
    and made a dig that could only fail (P11); a server error there still read Connected."""
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        search(page, "Seed Track")  # cosine.club accepts the key
        assert settings(server)["checked"] == "connected"
        if answer == "401":
            # A saved dig, so Start a dig stays open without a usable key (A4).
            made = httpx.post(server.url + "/api/digs", json={"seeds": [LINK_SEED]}).json()
            assert wait_server_step(server, made["step_id"])["status"] == "done"
            stub.refuse = {KEY}  # then the key is revoked
        else:
            stub.mode = "503"
        page.evaluate("location.hash = '#setup'")
        expect(page.locator("#health-refresh")).to_be_enabled()
        if answer == "503":
            note = "cosine.club didn't answer: server error. Try again in 30 s."
            expect(page.locator("#cosine-state")).to_have_text(note)
            expect(page.locator("#health-needed")).to_have_text("Needed to dig: cosine.club key · " + note)
            assert settings(server)["checked"] == "connected"  # no verdict on the key itself
            return
        expect(page.locator("#cosine-state")).to_have_text("Key rejected")
        expect(page.locator("#cosine-fix")).to_have_text("Fix key")
        page.evaluate("location.hash = '#/dig/new'")
        page.locator("#seed-input").fill(LINK)
        expect(page.locator("#seed-link-result")).to_be_visible()
        page.locator("#seed-input").press("Enter")
        page.locator("#seed-input").press("Enter")
        expect(page.locator("#seed-blocked")).to_have_text(SEARCH_NOTES["401"])
        assert ("POST", "/api/digs") not in ui.requests


def test_a_refusal_stands_while_the_key_state_cant_be_read(tmp_path, monkeypatch, stub):
    """Deep A2 #4: cosine.club refused the key, and the settings read after it failed: the page
    kept the key it knew as usable, so Start made a dig that could only fail."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        page.route("**/api/settings", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"detail": "database is locked"}'))
        search(page, "Seed Track")
        expect(cosine_note(page)).to_have_text(SEARCH_NOTES["401"])
        page.wait_for_function("stateModule.selectors.cosineKey(store.getState()).state === 'rejected'")
        page.locator("#seed-input").fill(LINK)
        expect(page.locator("#seed-link-result")).to_be_visible()
        page.locator("#seed-input").press("Enter")
        page.locator("#seed-input").press("Enter")
        expect(page.locator("#seed-blocked")).to_have_text(SEARCH_NOTES["401"])
        assert ("POST", "/api/digs") not in ui.requests
        # A read that works settles it with the server's own verdict.
        page.unroute("**/api/settings")
        page.evaluate("store.actions.loadSettings()")
        page.wait_for_function("store.getState().ui.keyDoubt === null")
        assert settings(server)["checked"] == "rejected"


def test_a_late_acceptance_keeps_the_page_chosen_meanwhile(tmp_path, monkeypatch, stub):
    """Deep A2 #5: a key accepted after the user had moved on took them back to Start a dig."""
    with digger(tmp_path, monkeypatch, key=None) as server, chrome(server, "/") as ui:
        page, held = ui.page, []
        page.wait_for_function("location.hash === '#connect'")
        page.route("**/api/connections/cosine_token", lambda route: held.append(route))
        connect(page, KEY)
        wait_held(page, held)
        page.evaluate("location.hash = '#feeds'")
        page.wait_for_function("store.getState().ui.route.page === 'feeds'")
        held[0].continue_()  # cosine.club accepts the key now
        expect(page.locator("#page-toasts")).to_contain_text("Connected to cosine.club.")
        assert page.evaluate("location.hash") == "#feeds"
        assert settings(server)["checked"] == "connected"


def test_a_replacement_key_ending_like_the_old_one_searches_again(tmp_path, monkeypatch, stub):
    """Deep A2 #6: the rerun after a refusal told keys apart by their last four characters, so a
    replacement ending the same way left Fix key up for a key that works."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page, fired = ui.page, []
        real = slice_routes.usable_key

        def other_tab_saves(*args):
            if not fired:
                fired.append(True)
                save_key(server, SAME_END_KEY)
            return real(*args)

        monkeypatch.setattr(slice_routes, "usable_key", other_tab_saves)
        search(page, "Seed Track")
        generation = page.evaluate("store.getState().ui.search.generation")
        page.wait_for_function("g => store.getState().ui.search.generation > g", arg=generation)
        page.evaluate("__sliceYTStub.advance(250)")
        expect(cosine_results(page).filter(has_text="Seed Track")).to_be_visible()
        expect(cosine_note(page)).to_be_hidden()
        assert len(api_calls(stub)) == 3  # the save's check, the refused search, the search again


def test_still_searching_ends_when_the_searches_do(tmp_path, monkeypatch, stub):
    """Deep A2 #7: Enter pressed while both sources searched said "Still searching" and kept saying
    it after the search had ended in a refusal."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        page.locator("#seed-input").fill("Seed Track")
        page.locator("#seed-input").press("Enter")  # before either source has answered
        expect(page.locator("#seed-hint")).to_have_text("Still searching. Press Enter again when a match appears.")
        page.evaluate("__sliceYTStub.advance(250)")
        page.wait_for_function("!store.getState().ui.search.pending.library && !store.getState().ui.search.pending.cosine")
        expect(cosine_note(page)).to_have_text(SEARCH_NOTES["401"])
        expect(page.locator("#seed-hint")).to_be_hidden()


YT_HEADERS = "accept: */*\nauthorization: SAPISIDHASH 1_fakehash\ncookie: SID=fake-sid; __Secure-3PAPISID=fake-papisid\n" \
             "x-goog-authuser: 0\n"


def test_a_login_check_speaks_only_for_the_login_it_followed(tmp_path, monkeypatch, stub):
    """Deep A1 #4: two YouTube Music logins saved close together (two tabs): the first one's slower
    check finished last, and its verdict stood for the second login."""
    first_check, go_on = threading.Event(), threading.Event()
    verdicts = iter(["rejected", "connected"])

    def check_login(path, **kwargs):
        verdict = next(verdicts)
        if verdict == "rejected":
            first_check.set()
            assert go_on.wait(10)
        return verdict

    monkeypatch.setattr(youtube, "check_login", check_login)
    with digger(tmp_path, monkeypatch, key=KEY) as server:
        def login():
            return httpx.post(server.url + "/api/youtube/login", json={"headers": YT_HEADERS}, timeout=15).json()

        with ThreadPoolExecutor(1) as pool:
            older = pool.submit(login)
            assert first_check.wait(10)
            newer = login()
            go_on.set()
            older = older.result()
        assert (older["state"], newer["state"]) == ("rejected", "connected")
        assert older["settings"]["youtube"]["checked"] == "connected"
        assert httpx.get(server.url + "/api/settings").json()["youtube"]["checked"] == "connected"


# Deep re-check (2026-10-06): settings answers in the order the server took them (its settings
# revision), whichever request they answer and whenever they arrive.

SETTINGS_FAIL = {"status": 500, "content_type": "application/json", "body": '{"detail": "database is locked"}'}


def test_a_read_from_before_a_refusal_never_clears_it(tmp_path, monkeypatch, stub):
    """Deep re-check #2 (A2 #4): a settings read the server answered before cosine.club refused the
    key reached the page after the read that followed the refusal had failed, and took the
    refused key for usable again."""
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        search(page, "Seed Track")  # cosine.club accepts the key
        held = hold_first(page, "**/api/settings")
        page.evaluate("store.actions.loadSettings().then(() => { window.oldRead = true; }); null")
        wait_held(page, held)  # answered now, with the key connected
        stub.refuse = {KEY}
        page.route("**/api/settings", lambda route: route.fulfill(**SETTINGS_FAIL))
        search(page, "Stub Artist")
        expect(cosine_note(page)).to_have_text(SEARCH_NOTES["401"])
        page.wait_for_function("store.getState().ui.keyDoubt === 'rejected'")
        release(held)
        page.wait_for_function("window.oldRead === true")
        assert page.evaluate("store.getState().ui.keyDoubt") == "rejected"
        assert page.evaluate("stateModule.selectors.cosineKey(store.getState()).usable") is False
        assert settings(server)["checked"] == "rejected"


def test_a_late_settings_save_answer_never_undoes_a_newer_verdict(tmp_path, monkeypatch, stub):
    """Deep re-check #3: a settings save's answer, taken while the key still read connected, came
    after Health's refusal had shown and put the connected key back: Start took it for usable."""
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        search(page, "Seed Track")  # cosine.club accepts the key
        held = hold_first(page, "**/api/settings")
        page.evaluate("store.actions.saveSettings({daily_size: 25}).then(() => { window.saved = true; }); null")
        wait_held(page, held)  # saved now; its answer says connected
        stub.refuse = {KEY}  # the key is revoked, and Health finds out
        page.evaluate("store.actions.refreshDoctor()")
        page.wait_for_function("store.getState().ui.settings.cosine_token.checked === 'rejected'")
        release(held)
        page.wait_for_function("window.saved === true")
        assert page.evaluate("store.getState().ui.settings.cosine_token.checked") == "rejected"
        assert page.evaluate("store.getState().ui.settings.daily_size") == 25  # the save itself shows
        assert page.evaluate("stateModule.selectors.cosineKey(store.getState()).usable") is False


def test_a_late_health_refusal_never_casts_doubt_on_a_replacement_key(tmp_path, monkeypatch, stub):
    """Deep re-check #4: Health refused the old key, its answer came after another key was
    accepted, and with the settings read after it failing, the new key was taken for refused."""
    stub.refuse = {KEY}
    with digger(tmp_path, monkeypatch, key=KEY) as server, chrome(server, "/#/dig/new") as ui:
        page = ui.page
        held = hold_first(page, "**/api/doctor*")
        page.evaluate("store.actions.refreshDoctor().then(() => { window.healthDone = true; }); null")
        wait_held(page, held)
        assert settings(server)["checked"] == "rejected"  # the server's verdict on the key Health sent
        page.evaluate("location.hash = '#connect'")
        connect(page, NEW_KEY)
        page.wait_for_function("location.hash === '#/dig/new'")
        page.route("**/api/settings", lambda route: route.fulfill(**SETTINGS_FAIL))
        release(held)
        page.wait_for_function("window.healthDone === true")
        assert page.evaluate("store.getState().ui.keyDoubt") is None
        assert page.evaluate("stateModule.selectors.cosineKey(store.getState()).usable") is True
        page.unroute("**/api/settings")
        assert (settings(server)["hint"], settings(server)["checked"]) == ("…0002", "connected")

