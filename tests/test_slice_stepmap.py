"""S20: real saved geometry and canvas gestures through the served slice app."""

import math
import os
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger.config import AppConfig
from cosine_digger.web.slice_testmode import FixtureGate, SliceFixtures
from tests.slice_browser import launch, route_youtube_stub, served_app


class MapFixtures(SliceFixtures):
    """Distinct measured argmax sectors and a gate before the third shared fact."""

    def __init__(self):
        super().__init__()
        self.third_fact = FixtureGate()
        for seed_index, letter in enumerate("abc"):
            neighbors = self._neighbors[f"seed-{letter}"][:9]
            for index, item in enumerate(neighbors):
                item["score"] = round(0.97 - 0.052 * index - (0 if index % 3 == seed_index else 0.08), 3)
            self._neighbors[f"seed-{letter}"] = neighbors
        # A credited main artist cannot become a person hub, even on two releases.
        for index in range(2):
            self._releases[f"/releases/{2000 + index}"]["extraartists"].append(
                {"id": 7000, "name": "Discovery Artist 0", "role": "Producer"})

    def _discogs(self, request):
        if request.url.path == "/releases/2002":
            self.third_fact.wait(request)
        return super()._discogs(request)

    def release_all(self):
        self.third_fact.released.set()
        super().release_all()


# Capture the actual paint path, including alpha/dashes; DOM animation probes
# alone cannot prove reduced canvas motion or immutable projected coordinates.
CANVAS_PROBE = """(() => {
  const proto = CanvasRenderingContext2D.prototype;
  window.mapPaints = [];
  window.mapPaintCount = 0;
  for (const name of ['clearRect','beginPath','moveTo','lineTo','arc','rect','closePath',
                       'fill','stroke','fillRect','strokeRect','fillText','strokeText']) {
    const original = proto[name];
    proto[name] = function(...args) {
      if (this.canvas.id === 'stepmap-canvas') {
        if (name === 'clearRect') { window.mapPaints = []; window.mapPaintCount++; }
        window.mapPaints.push({name,args,alpha:this.globalAlpha,fill:this.fillStyle,
          stroke:this.strokeStyle,width:this.lineWidth,dash:this.getLineDash(),font:this.font});
      }
      return original.apply(this,args);
    };
  }
})();"""


@pytest.fixture
def world(tmp_path, monkeypatch, record_property, request):
    options = getattr(request, "param", {})
    providers = MapFixtures() if options.get("sectors") else SliceFixtures()
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=tmp_path / "auth.json",
                    cosine_token="fixture-cosine-key", discogs_token="" if options.get("no_token") else "fixture-discogs-key")
    with served_app(tmp_path, monkeypatch, cfg=cfg, providers=providers) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce",
                                      device_scale_factor=2)
        errors, external, requests = [], [], []

        def local_only(route):
            if route.request.url.startswith(server.url + "/"):
                requests.append((route.request.method, urlsplit(route.request.url).path))
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        route_youtube_stub(page)
        page.add_init_script(CANVAS_PROBE)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(10000)
        page.goto(server.url + "/#/dig/new")
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.evaluate("""async () => {
          window.shell = await (await import('/static/slice.js')).boot();
          window.store = shell.store;
          window.selectors = (await import('/static/state.js')).selectors;
          window.mapModule = await import('/static/stepmap.js');
          window.api = shell.api;
          await document.fonts.ready;
        }""")
        server.page, server.context, server.requests = page, context, requests
        record_property("browser_mode", "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome")
        try:
            yield server
        finally:
            server.providers.release_all()
            page.evaluate("shell.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "Map tests attempted external HTTP"


def start(world, *names):
    points = [world.providers.point(name).to_dict() for name in names or ("seed-a",)]
    return world.page.evaluate("""async points => {
      await store.actions.openNewDig({chips:points});
      return store.actions.startDig();
    }""", points)


def complete(page, step_id, *, terminal=True):
    page.wait_for_function("""({id,terminal}) => {
      const s=store.getState(), step=s.steps.byId[id];
      return step && (terminal ? ['done','failed','stopped'].includes(step.status) : selectors.isPicksComplete(s,id));
    }""", arg={"id": step_id, "terminal": terminal})


def snapshot(page, step_id):
    return page.evaluate("id => api.request('GET', `/api/steps/${id}`)", step_id)["snapshot"]


def canvas(page):
    return page.locator("#stepmap-canvas")


def paint(page):
    return page.evaluate("mapPaints")


def viewport(page):
    box = canvas(page).bounding_box()
    if page.locator('.stepmap-surface #stepmap-legend').count() or page.locator('#stepmap-caption').is_visible():
        # Read the actual fitted projection from its two guide circles. The
        # saved polar data must still paint and hit in that same projection.
        guide = next(entry['args'] for entry in paint(page) if entry['name'] == 'arc' and entry['dash'] == [1, 3])
        return {'cx': guide[0], 'cy': guide[1], 'R': guide[2] / 0.34, 'scale': 1, 'tx': 0, 'ty': 0}
    return {"cx": box["width"] / 2, "cy": box["height"] / 2,
            "R": min(box["width"], box["height"]) / 2 - 24, "scale": 1, "tx": 0, "ty": 0}


def xy(layout, vp):
    return {"x": vp["tx"] + vp["scale"] * (vp["cx"] + layout["radius"] * vp["R"] * math.sin(layout["angle"])),
            "y": vp["ty"] + vp["scale"] * (vp["cy"] - layout["radius"] * vp["R"] * math.cos(layout["angle"]))}


@pytest.mark.parametrize("id_missing", [True], ids=["name"])
def test_nameless_id_credit_does_not_borrow_other_peoples_roles(world, id_missing):
    page = world.page
    for index in range(3):
        release = world.providers._releases[f"/releases/{2000 + index}"]
        for credit in release["extraartists"]:
            if id_missing and credit["name"] == "Person P":
                credit.pop("id", None)
                credit["role"] = "Mixed By" if index == 1 else "Producer"
                if index == 1:
                    credit["name"] = "  PERSON   P (2)  "
        release["extraartists"].append(
            {"name": "Separate Engineer", "role": "Engineer"} if id_missing else
            {"id": 7601, "name": "Person P", "role": "Engineer"})
    root = start(world)
    complete(page, root["step_id"])
    saved = snapshot(page, root["step_id"])
    key = "person:name:person p" if id_missing else "person:601"
    hub = next(hub for hub in saved["hubs"] if hub["hub_key"] == key)
    canvas(page).hover(position=xy(hub["layout"], viewport(page)))
    tip = page.locator("#stepmap-region [role=tooltip]")
    expect(tip).to_have_text("Person P · credited on 3 picks here (Producer, Mixed By)")


@pytest.mark.parametrize("state", ["paused", "idle"])
def test_paused_player_does_not_keep_requesting_animation_frames(world, state):
    page = world.page
    root = start(world)
    complete(page, root["step_id"])
    saved = snapshot(page, root["step_id"])
    played = saved["picks"][12] if state == "paused" else next(p for p in reversed(saved["picks"]) if p["video_id"])
    page.emulate_media(reduced_motion="no-preference")
    page.evaluate("""() => {
      window.mapAnimationFrames = 0;
      const raf = window.requestAnimationFrame;
      window.requestAnimationFrame = callback => raf.call(window, t => {
        window.mapAnimationFrames++; callback(t);
      });
    }""")
    page.evaluate("id => store.actions.playPick({pickId:id})", played["id"])
    page.wait_for_function("__sliceYTStub.players[0]?.getPlayerState() === 1")
    page.wait_for_timeout(100)
    assert page.evaluate("mapAnimationFrames") > 0
    page.evaluate("__sliceYTStub.players[0].pauseVideo()" if state == "paused" else "__sliceYTStub.players[0].setState(0)")
    page.wait_for_function("state => store.getState().player.state === state", arg=state)
    page.wait_for_timeout(250)
    page.evaluate("mapAnimationFrames = 0")
    page.wait_for_timeout(350)
    assert page.evaluate("mapAnimationFrames") == 0
