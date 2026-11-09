"""S12 first run in real Chrome against the served app: quiet optional cards,
track digs with a malformed or refused Discogs token, an empty YouTube Music
login, and Feeds without the in-app scheduler.

cosine.club is StubCosine on loopback (Health checks the key there), digs use
the slice fixtures, and the Discogs, Exa and YouTube Music checks answer from
the test. Nothing reaches the network. Each test ends with the key canary.
"""

import json
import re
import secrets
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger import youtube
from cosine_digger.config import AppConfig, ENV_VARS, SECRET_FIELDS
from cosine_digger.web import connections
from cosine_digger.web.app import create_app
from cosine_digger.web.slice_testmode import SliceFixtures
from tests import slice_browser
from tests.lifecycle_support import free_port, release_ports
from tests.slice_browser import launch, route_youtube_stub, served_app
from tests.stub_cosine import StubCosine

COSINE_KEY = "first-run-cosine-0042"
# Amber and the two reds of app.css (--amber, --red, the fail status) in any colour a visible box resolves.
WARM_STYLES = r"""roots => roots.flatMap((root) => [root, ...root.querySelectorAll('*')]).flatMap((node) => {
  if (!node.getClientRects().length) return [];
  return [null, '::before', '::after'].flatMap((pseudo) => {
    const style = getComputedStyle(node, pseudo);
    if (pseudo && style.content === 'none') return [];
    return ['color', 'background-color', 'border-top-color', 'border-right-color', 'border-bottom-color',
      'border-left-color', 'box-shadow', 'text-shadow']
      .filter((name) => /rgba?\((255, 182, 39|255, 90, 90|255, 128, 128)\b/.test(style.getPropertyValue(name)))
      .map((name) => `${node.id || node.className || node.tagName}${pseudo || ''} ${name}`);
  });
})"""


class BodyRecorder:
    """Pass-through ASGI app that keeps every response body, event streams included, for the canary."""

    def __init__(self, app, bodies):
        self.app, self.bodies = app, bodies

    async def __call__(self, scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.body":
                self.bodies.append(message.get("body", b""))
            await send(message)
        await self.app(scope, receive, record)


class RefusingDiscogs(SliceFixtures):
    """The slice fixtures' Discogs, answering one token with 401 as Discogs does."""

    def __init__(self, token):
        super().__init__()
        self.token, self.refused = token, 0

    def _discogs(self, request):
        if request.headers.get("Authorization") == f"Discogs token={self.token}":
            self.refused += 1
            return httpx.Response(401, json={"message": "You must authenticate to access this resource."})
        return super()._discogs(request)


@contextmanager
def first_run(home, monkeypatch, *, discogs=None, discogs_from="env", auth_file=None, providers=None, at="#setup"):
    """A home with a cosine.club key from the environment (plus what a test adds), served and open in Chrome."""
    providers = providers or SliceFixtures()
    cfg = AppConfig(home=home, db_path=home / "taste.db", ytmusic_auth_path=home / "auth.json",
                    cosine_token=COSINE_KEY, discogs_token=discogs,
                    sources={"cosine_token": "env", **({"discogs_token": discogs_from} if discogs else {})})
    for key in SECRET_FIELDS:
        # The app puts a saved key in the environment; pytest puts it back.
        monkeypatch.setenv(ENV_VARS[key], getattr(cfg, key) if cfg.sources.get(key) == "env" else "")
    world = SimpleNamespace(cfg=cfg, providers=providers, accept=set(), keys_checked=[], headers_sent=[],
                            login_check="connected", bodies=[], console=[])

    def check(key):
        world.keys_checked.append(key)
        return connections.Check("connected" if key in world.accept else "rejected")

    def login(headers, path):
        world.headers_sent.append(headers)
        path.write_text(json.dumps({"cookie": "first-run-cookie"}))  # a login's shape, never the pasted headers

    for name in ("discogs_token", "exa_api_key"):
        monkeypatch.setitem(connections.CHECKS, name, check)
    monkeypatch.setattr(youtube, "login", login)
    monkeypatch.setattr(youtube, "check_login", lambda path, **kwargs: world.login_check)
    monkeypatch.setattr(slice_browser, "create_app",
                        lambda *args, **kwargs: BodyRecorder(create_app(*args, **kwargs), world.bodies))
    home.mkdir(parents=True, exist_ok=True)
    if auth_file is not None:
        cfg.ytmusic_auth_path.write_text(auth_file)
    stub = StubCosine(free_port())
    monkeypatch.setenv("COSINE_DIGGER_API_BASE", stub.base)
    try:
        with stub, served_app(home, monkeypatch, providers=providers, cfg=cfg) as server, \
                sync_playwright() as playwright:
            browser = launch(playwright)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            external, errors = [], []

            def local_only(route):
                if route.request.url.startswith(server.url + "/"):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()

            context.route("**/*", local_only)
            page = context.new_page()
            route_youtube_stub(page)
            page.set_default_timeout(10000)
            page.on("console", lambda message: world.console.append(message.text))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.url + "/" + at)
            page.wait_for_selector('#device[data-shell-state="ready"]')
            page.evaluate("""async () => {
              window.shell = await (await import('/static/slice.js')).boot();
              window.store = shell.store;
              window.selectors = (await import('/static/state.js')).selectors;
            }""")
            world.page, world.url, world.stub = page, server.url, stub
            try:
                yield world
            finally:
                page.evaluate("shell.destroy()")
                context.close()
                browser.close()
            assert errors == []
            assert external == [], "First-run tests attempted external network access"
    finally:
        release_ports()


def assert_no_key_left(world, *keys):
    """The canary: a key never comes back in a response body, a console message or browser storage."""
    bodies = b"".join(world.bodies)
    storage = world.page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    for key in (COSINE_KEY, *keys):
        found = [sink for sink, seen in (("a response body", key.encode() in bodies),
                                         ("the console", any(key in message for message in world.console)),
                                         ("browser storage", key in storage)) if seen]
        assert found == [], f"A key came back in {found}"


def card_state(page, card):
    return page.locator(f"#{card}-state")


def dig(world, seed="seed-a"):
    """A track dig from one seed through the store's own dig actions; returns its settled picks."""
    step = world.page.evaluate("""async point => {
      await store.actions.openNewDig({chips: [point]});
      return store.actions.startDig();
    }""", world.providers.point(seed).to_dict())
    world.page.wait_for_function("id => ['done', 'failed', 'stopped'].includes(store.getState().steps.byId[id]?.status)",
                                 arg=step["step_id"])
    assert world.page.evaluate("id => store.getState().steps.byId[id].status", step["step_id"]) == "done"
    return world.page.evaluate("id => selectors.orderedPicks(store.getState(), id)", step["step_id"])


def facts_of(page, pick):
    page.locator(f'#step-region .steplist-row[data-pick-id="{pick["id"]}"] .steplist-title').click()
    return page.locator('[data-section="facts"]')


def test_optional_items_quiet(tmp_path, monkeypatch):
    providers = SliceFixtures()
    providers.library_rows = ()
    with first_run(tmp_path, monkeypatch, providers=providers) as world:
        page = world.page
        expect(card_state(page, "cosine")).to_have_text("Connected · key ends …0042")
        expect(page.locator("#cosine-source")).to_have_text("From COSINE_API_TOKEN")
        assert world.stub.count("search") and all(seen.with_key for seen in world.stub.seen)
        expect(page.locator("#health-needed")).to_have_text("Needed to dig: cosine.club key ✓")
        for card in ("discogs", "yt", "exa"):
            expect(card_state(page, card)).to_have_text("Not connected")
            expect(card_state(page, card)).to_have_attribute("data-tone", "muted")
        expect(card_state(page, "music")).to_have_text("No folder yet")
        rows = page.locator("#health-list .check")
        expect(rows).to_have_count(4)
        for row in rows.all():
            expect(row).to_have_attribute("data-status", "off")
            expect(row.locator(".check__status")).to_have_text("not connected")
        quiet = page.locator("#card-discogs, #card-youtube, #card-music, #card-exa, "
                             'section[aria-labelledby="health-heading"]')
        assert quiet.evaluate_all(WARM_STYLES) == []
        shown = "\n".join(quiet.evaluate_all("nodes => nodes.map((node) => node.innerText)"))
        assert "WARN" not in shown.upper()
        assert not re.search(r"\b\d+ of \d+\b", shown)
        # Sources and the database path wait in the collapsed Details.
        health = page.locator('section[aria-labelledby="health-heading"]').inner_text()
        assert "source" not in health and str(world.cfg.db_path) not in health
        assert page.locator("#health-details").evaluate("details => !details.open")
        assert world.keys_checked == []  # opening Setup checks no optional key (Exa's check is a paid search)
        assert_no_key_left(world)


def test_bad_discogs_token_track_dig(tmp_path, monkeypatch):
    # DISCOGS_TOKEN="bad token" is never sent: the card says so, and a track dig still finds picks.
    with first_run(tmp_path / "malformed", monkeypatch, discogs="bad token") as world:
        page = world.page
        # From the environment there is no field to paste into: the line says where to fix it.
        expect(card_state(page, "discogs")).to_have_text(
            "This token has a space or line break in it. To fix it, run cosine-digger stop, change DISCOGS_TOKEN, "
            "then run cosine-digger start.")
        expect(page.locator("#discogs-source")).to_have_text("From DISCOGS_TOKEN")
        expect(page.locator("#discogs-connect")).to_be_hidden()
        # Set up but broken is the one optional state that turns amber.
        expect(page.locator('#health-list .check[data-check="discogs_token"]')).to_have_attribute("data-status", "warn")
        assert page.locator("#card-discogs").evaluate_all(WARM_STYLES)
        picks = dig(world)
        assert picks and {pick["facts_state"] for pick in picks} == {"no_token"}
        assert not any(provider == "discogs" for provider, _, _ in world.providers.calls)
        expect(facts_of(page, picks[0])).to_contain_text("Label, year and credits come from Discogs (not connected).")
        assert_no_key_left(world)

    # A token Discogs refuses (401): the track dig still finds picks, just without Facts.
    refused = "refused-discogs-token"
    with first_run(tmp_path / "refused", monkeypatch, discogs=refused, discogs_from="settings",
                   providers=RefusingDiscogs(refused)) as world:
        page = world.page
        expect(card_state(page, "discogs")).to_have_text("Token saved")
        picks = dig(world)
        assert picks and world.providers.refused
        # Discogs answered and refused: Facts read as not connected, never "Discogs isn't answering" (P9).
        assert {pick["facts_state"] for pick in picks} == {"no_token"}
        expect(facts_of(page, picks[0])).to_contain_text("Label, year and credits come from Discogs (not connected).")
        page.evaluate("store.actions.openRoute({page: 'setup'})")
        page.locator("#discogs-check").click()
        expect(card_state(page, "discogs")).to_have_text(
            "Discogs didn't accept that token. Make a new one at discogs.com/settings/developers.")
        # A valid token typed into the card (the canary) connects, and the next dig has Facts again.
        canary = "canary" + secrets.token_hex(12)
        world.accept.add(canary)
        page.locator("#discogs-connect").click()
        page.locator("#set-discogs").fill(canary)
        page.locator("#discogs-go").click()
        expect(card_state(page, "discogs")).to_have_text(f"Connected · token ends …{canary[-4:]}")
        assert world.keys_checked == [refused, canary]
        assert f"…{canary[-4:]}".encode() in b"".join(world.bodies)  # the recorder saw the masked settings
        ready = [pick for pick in dig(world) if pick["facts_state"] == "ready"]
        assert ready
        expect(facts_of(page, ready[0]).locator(".inspector-facts").first).to_be_visible()
        assert_no_key_left(world, canary)


def test_empty_login_not_connected(tmp_path, monkeypatch):
    with first_run(tmp_path, monkeypatch, auth_file="{}") as world:
        page = world.page
        row = page.locator('#health-list .check[data-check="youtube_login"]')
        # An empty {} file holds no login: never Connected, on the card or in Health.
        expect(card_state(page, "yt")).to_have_text("Not connected")
        expect(row).to_have_attribute("data-status", "off")
        expect(row.locator(".check__status")).to_have_text("not connected")
        secret = secrets.token_hex(12)
        headers = f"cookie: SAPISID={secret}"
        page.locator("#yt-connect").click()
        world.login_check = "rejected"
        page.locator("#yt-headers").fill(headers)
        page.locator("#yt-go").click()
        expect(card_state(page, "yt")).to_have_text(
            "YouTube Music didn't accept this login. Copy fresh headers and log in again.")
        expect(row.locator(".check__status")).to_have_text("rejected")
        world.login_check = "unreachable"
        page.locator("#yt-headers").fill(headers)
        page.locator("#yt-go").click()
        expect(card_state(page, "yt")).to_have_text("Couldn't reach YouTube Music to check the login. Try again.")
        assert world.headers_sent == [headers, headers]
        assert_no_key_left(world, secret)


def test_feeds_no_next_run(tmp_path, monkeypatch):
    with first_run(tmp_path, monkeypatch, at="#feeds") as world:
        page = world.page
        feeds = page.request.get(world.url + "/api/playlists").json()
        assert [feed["next_run"] for feed in feeds.values()] == [None, None]  # served without the scheduler
        expect(page.locator("#feeds-lede")).to_have_text(
            "Digger can write two private YouTube Music playlists from your keeps, your passes and your library. "
            "Looking around in a dig never changes them.")
        expect(page.locator("#feeds-how")).to_have_text(
            "Digger refreshes them when you press Run now. To refresh them on their own while Digger runs, "
            "start it with cosine-digger start --scheduler.")
        expect(page.locator("#feeds-last")).to_have_text("Last run: none yet")
        expect(page.locator("#feeds-next-daily")).to_be_hidden()
        expect(page.locator("#feeds-next-weekly")).to_be_hidden()
        for kind in ("daily", "weekly"):
            card = page.locator(f'[data-feed-kind="{kind}"]')
            # No YouTube Music login yet: Connect stands where Run now was.
            expect(card.locator('[data-slot="connect"]')).to_have_text("CONNECT YOUTUBE MUSIC", use_inner_text=True)
            expect(card.locator('[data-slot="run"]')).to_be_hidden()
            expect(card.locator('[data-slot="next"]')).to_be_hidden()
            expect(card.locator('[data-slot="when"]')).to_be_hidden()
        shown = page.locator("#feeds-region").inner_text()
        assert not re.search(r"\bnext\b", shown, re.I)
        assert not re.search(r"\b(?:SUN|MON|TUE|WED|THU|FRI|SAT) \d\d:\d\d\b", shown)
        assert_no_key_left(world)
