"""S21: saved islands, shared state and camera paint through the served app."""

import math
import os
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.slice_browser import launch, route_youtube_stub, served_app


# Observe the real canvas, including paths, alpha and timing. DOM animations
# alone cannot show a camera pan or a static reduced-motion playback ring.
PAINT_PROBE = """(() => {
  const proto = CanvasRenderingContext2D.prototype, paths = new WeakMap();
  window.territoryPaint = []; window.territoryFrames = [];
  for (const name of ['clearRect','beginPath','moveTo','lineTo','arc','closePath',
                       'fill','stroke','fillRect','strokeRect','fillText','strokeText']) {
    const original = proto[name];
    proto[name] = function(...args) {
      if (this.canvas.id === 'territory-canvas') {
        if (name === 'clearRect') {
          window.territoryPaint = [];
          window.territoryFrames.push({at:performance.now(), stepId:this.canvas.dataset.stepId,
            calls:window.territoryPaint});
          if (window.territoryFrames.length > 120) window.territoryFrames.shift();
        }
        if (name === 'beginPath') paths.set(this, []);
        if (['moveTo','lineTo','arc','closePath'].includes(name))
          (paths.get(this) || []).push({name,args});
        window.territoryPaint.push({name,args,alpha:this.globalAlpha,fill:this.fillStyle,
          stroke:this.strokeStyle,width:this.lineWidth,dash:this.getLineDash(),
          path:name === 'stroke' ? [...(paths.get(this) || [])] : null});
      }
      return original.apply(this,args);
    };
  }
})();"""


def attach(page):
    page.wait_for_selector('#device[data-shell-state="ready"]')
    page.evaluate("""async () => {
      window.shell = await (await import('/static/slice.js')).boot();
      window.store = shell.store;
      window.selectors = (await import('/static/state.js')).selectors;
      window.api = shell.api;
      await document.fonts.ready;
    }""")


@pytest.fixture
def world(tmp_path, monkeypatch, record_property):
    with served_app(tmp_path, monkeypatch) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      reduced_motion="reduce", device_scale_factor=2)
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
        page.add_init_script(PAINT_PROBE)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(10000)
        page.goto(server.url + "/#/dig/new")
        attach(page)
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
        assert external == [], "Territory tests attempted external HTTP"


def start(world, *names):
    points = [world.providers.point(name).to_dict() for name in names or ("seed-a",)]
    return world.page.evaluate("""async points => {
      await store.actions.openNewDig({chips:points});
      return store.actions.startDig();
    }""", points)


def complete(page, identity):
    page.wait_for_function("""id => {
      const step = store.getState().steps.byId[id];
      return step && ['done','failed','stopped'].includes(step.status);
    }""", arg=identity["step_id"])


def snapshot(page, identity):
    return page.evaluate("id => api.request('GET', `/api/steps/${id}`)", identity["step_id"])["snapshot"]


def point(pick):
    return {"point_id": "track:" + pick["track_key"], "kind": "track", "name": pick["artist"] + " - " + pick["title"],
            "track_key": pick["track_key"], "artist": pick["artist"], "title": pick["title"],
            "cosine_id": pick["cosine_id"], "video_id": pick["video_id"],
            "source_step_id": pick["step_id"], "source_rank": pick["rank"]}


def branch(page, pick):
    identity = page.evaluate("""point => store.actions.startMove({parentStepId:point.source_step_id,
      origins:[point],move:'track',further:false})""", point(pick))
    complete(page, identity)
    return identity


def paint(page):
    return page.evaluate("territoryPaint")


def guide(calls):
    arcs = [entry["args"] for entry in calls if entry["name"] == "arc" and entry["dash"] == [1, 3]]
    for a, b in zip(arcs, arcs[1:]):
        if a[:2] == b[:2] and math.isclose(b[2], a[2] * 2):
            return a
    raise AssertionError("Open island did not draw both saved-score guide rings")


def camera(page, saved):
    x, y, radius, *_ = guide(paint(page))
    scale = radius / (0.34 * 160)
    return {"scale": scale, "tx": x - saved["step"]["island_x"] * scale,
            "ty": y - saved["step"]["island_y"] * scale}


@pytest.mark.parametrize("collapsed", [False, True], ids=["open", "collapsed"])
@pytest.mark.parametrize("state", ["paused"])
def test_paused_player_does_not_keep_requesting_animation_frames(world, state, collapsed):
    page = world.page
    root = start(world)
    complete(page, root)
    saved = snapshot(page, root)
    if collapsed:
        branch(page, saved["picks"][8])
    page.locator("#view-map").click()
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
