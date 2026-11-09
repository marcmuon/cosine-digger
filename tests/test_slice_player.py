"""S14 through the mounted app, fixture providers, SQLite, and shared YT stub.

These are deterministic embed simulations, not the S25 live embed proof.
Keys belong to S22; its P/Shift+P actions are exercised here from real clicks.
"""

import json
import os
import re
from contextlib import closing

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger import storage
from tests.slice_browser import launch, route_youtube_stub, served_app


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    options = getattr(request, "param", "ready")
    mode = options.get("mode", "ready") if isinstance(options, dict) else options
    dpr = options.get("device_scale_factor", 1) if isinstance(options, dict) else 1
    with served_app(tmp_path, monkeypatch) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=dpr)
        errors, external, deferred = [], [], []

        def local_only(route):
            if route.request.url.startswith(server.url + "/"):
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        route_youtube_stub(page)
        sdk_requests = 0

        def sdk_mode(route):
            nonlocal sdk_requests
            sdk_requests += 1
            if sdk_requests == 1 or mode == "ready":
                route.fallback()
            elif mode == "offline":
                route.abort()
            else:
                deferred.append(route)

        page.route(re.compile(r"^https://www.youtube.com/iframe_api$"), sdk_mode)
        # An explicit harness caller injects the shared clock through boot().
        # No clock/global discovery hook is added to the production modules.
        page.route("**/static/slice.js", lambda route: route.fulfill(
            content_type="text/javascript", body="""
              const sdk = document.createElement('script');
              sdk.src = 'https://www.youtube.com/iframe_api';
              await new Promise(resolve => { sdk.onload = resolve; document.head.append(sdk); });
              if (MODE !== 'ready') { window.savedYT = window.YT; delete window.YT; }
              const device = document.getElementById('device');
              device.id = 'player-harness-device';
              const {boot} = await import('/static/slice.js?player-harness');
              device.id = 'device';
              window.stateModule = await import('/static/state.js');
              const {createApi} = await import('/static/api.js');
              window.playerRequests = [];
              const api = createApi({fetch: (path, options) => {
                playerRequests.push({path, method: options?.method || 'GET', body: options?.body});
                return fetch(path, options);
              }});
              window.shell = await boot({api, clock: __sliceYTStub.clock});
              window.store = shell.store;
              const button = document.createElement('button');
              button.id = 'player-test-gesture'; button.textContent = 'Test gesture';
              button.style.cssText = 'position:absolute;bottom:4px;left:4px;z-index:1';
              button.onclick = async () => {
                window.playerTestDone = false;
                window.playerTestResult = await window.playerTestGesture();
                window.playerTestDone = true;
              };
              document.getElementById('main').append(button);
            """.replace("MODE", json.dumps(mode)),
        ))
        page.goto(server.url + "/#/dig/new")
        page.wait_for_selector('#device[data-shell-state="ready"]')
        server.page, server.context, server.deferred = page, context, deferred
        server.browser_mode = "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome"
        try:
            yield server
        finally:
            page.evaluate("shell.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "Player test attempted a real external request"


def gesture(world, action, *args):
    world.page.evaluate("""({action, args}) => {
      window.playerTestDone = false;
      window.playerTestGesture = () => store.actions[action](...args);
    }""", {"action": action, "args": list(args)})
    world.page.locator("#player-test-gesture").click()
    world.page.wait_for_function("window.playerTestDone")
    return world.page.evaluate("window.playerTestResult")


def start(world, seed="seed-a", *, further=False, terminal=True):
    page = world.page
    page.evaluate("point => store.actions.addChip(point)", world.providers.point(seed).to_dict())
    result = gesture(world, "startDig")
    if further:
        # Further is a captured explicit move from the root's first pick.
        page.wait_for_function("id => stateModule.selectors.orderedPicks(store.getState(), id).length > 0",
                               arg=result["step_id"])
        pick = picks(world, result["step_id"])[0]
        result = gesture(world, "startMove", {"parentStepId": result["step_id"],
                                               "origins": [point_for(pick)], "move": "track", "further": True})
    page.wait_for_function("""({id, terminal}) => {
      const s = store.getState(), step = s.steps.byId[id];
      return step && (terminal ? step.status !== 'running' :
        stateModule.selectors.orderedPicks(s, id).length > 0);
    }""", arg={"id": result["step_id"], "terminal": terminal})
    return result


def picks(world, step_id):
    return world.page.evaluate("id => stateModule.selectors.orderedPicks(store.getState(), id)", step_id)


def point_for(pick):
    return {"point_id": "track:" + pick["track_key"], "kind": "track",
            "name": f"{pick['artist']} - {pick['title']}", "artist": pick["artist"], "title": pick["title"],
            "track_key": pick["track_key"], "cosine_id": pick["cosine_id"], "video_id": pick["video_id"],
            "indexed": True, "source_step_id": pick["step_id"], "source_rank": pick["rank"]}


def player(world):
    return world.page.evaluate("store.getState().player")


def advance(world, milliseconds):
    world.page.evaluate("ms => __sliceYTStub.advance(ms)", milliseconds)


def wait_playing(world, pick):
    world.page.wait_for_function("""id => store.getState().player.current?.pickId === id &&
      store.getState().player.state === 'playing'""", arg=pick["id"])


def play_pick(world, pick):
    gesture(world, "playPick", {"pickId": pick["id"]})
    if pick["video_id"] not in {None, "S9GONE00100", "S9REFUSE101", "Pso5YU_mntg"}:
        wait_playing(world, pick)


def loads(world):
    return world.page.evaluate("__sliceYTStub.calls.filter(c => c.method === 'loadVideoById')")


@pytest.mark.parametrize("after_refusal", [False])
def test_seek_waiting_for_play_keeps_cued_track_paused(world, after_refusal):
    page = world.page
    root = start(world)
    source = picks(world, root["step_id"])
    if after_refusal:
        refused = next(pick for pick in source if pick["video_id"] == "S9GONE00100")
        play_pick(world, refused)
        page.wait_for_function("store.getState().player.state === 'refused'")
    page.evaluate("""() => {
      const p = __sliceYTStub.players[0];
      p.loadVideoById = function(input) { this.video('cueVideoById', input); };
      p.playVideo = function() { this.record('playVideo'); };
    }""")
    if after_refusal:
        advance(world, 1999)
        assert player(world)["current"]["pickId"] == refused["id"]
        advance(world, 1)
        assert player(world)["current"]["pickId"] != refused["id"]
    else:
        gesture(world, "playPick", {"pickId": source[6]["id"]})
    advance(world, 1500)
    assert player(world)["state"] == "paused"
    assert player(world)["phase"] == "gesture"
    assert page.evaluate("__sliceYTStub.players[0].getPlayerState()") == 5
    page.get_by_role("button", name="Forward 15 seconds", exact=True).click()
    assert player(world)["state"] == "paused"
    assert player(world)["position"] == 15
    assert page.evaluate("__sliceYTStub.players[0].getPlayerState()") in {2, 5}
    expect(page.locator(".player-time")).to_have_text("0:15 / 3:00")
    expect(page.locator("#lcd-note")).to_have_text("Press ▶ to start playback.")
    advance(world, 5000)
    assert player(world)["position"] == 15
    assert player(world)["heardSeconds"] == {}
    assert page.evaluate("playerRequests.filter(r => r.path === '/api/heard').length") == 0


@pytest.mark.parametrize("video", ["S9GONE00100"])
def test_reload_refusal_must_not_autoplay_successor(world, video):
    root = start(world)
    source = picks(world, root["step_id"])
    refused = next(pick for pick in source if pick["video_id"] == video)
    play_pick(world, refused)
    world.page.wait_for_function("store.getState().player.state === 'refused'")
    advance(world, 800)
    world.page.reload()
    world.page.wait_for_selector('#device[data-shell-state="ready"]')
    before = player(world)
    advance(world, 6100)
    assert not loads(world), "A restored cue must not load a successor without Play"
    assert world.page.evaluate("__sliceYTStub.calls.filter(c => c.method === 'playVideo').length") == 0
    assert player(world)["current"] == before["current"]
    assert player(world)["queue"] == before["queue"]
    assert player(world)["state"] == "paused"
    assert not player(world)["gesture"]
    expect(world.page.locator("#lcd-note")).to_have_text("Paused after reload. Press ▶ to resume.")
    world.page.get_by_role("button", name="Play", exact=True).click()
    world.page.wait_for_function("store.getState().player.state === 'refused'")
    advance(world, 1999)
    assert player(world)["current"]["pickId"] == refused["id"]
    advance(world, 1)
    world.page.wait_for_function("id => store.getState().player.current.pickId !== id", arg=refused["id"])


@pytest.mark.parametrize("phase", ["ended"])
def test_native_replay_after_queue_end_must_update_playback_state(world, phase):
    if phase == "waiting":
        gate = world.providers.block("hop1")
        root = start(world, further=True, terminal=False)
        assert gate.entered.wait(3)
        world.page.wait_for_function("id => stateModule.selectors.orderedPicks(store.getState(), id).length === 25", arg=root["step_id"])
    else:
        root = start(world)
    source = picks(world, root["step_id"])
    last, first = source[-1], source[0]
    play_pick(world, last)
    world.page.evaluate("p => store.actions.selectPoint(p.step_id, `track:${p.track_key}`)", first)
    world.page.get_by_role("button", name="Next track", exact=True).click()
    world.page.wait_for_function("phase => store.getState().player.phase === phase", arg=phase)
    world.page.evaluate("__sliceYTStub.players[0].playVideo()")
    wait_playing(world, last)
    assert world.page.evaluate("stateModule.selectors.judgingTarget(store.getState()).trackKey") == last["track_key"]
    assert world.page.evaluate("store.getState().ui.route.sel") == "track:" + first["track_key"]
    expect(world.page.locator("#lcd-title")).to_contain_text("▶ " + last["artist"] + " - " + last["title"])
    assert world.page.locator("#lcd-note").get_attribute("data-string-id") not in {"S-LCD-END", "S-LCD-WAITING"}
    advance(world, 29900)
    assert world.page.evaluate("playerRequests.filter(r => r.path === '/api/heard').length") == 0
    advance(world, 100)
    world.page.wait_for_function("key => !!store.getState().heard.byTrack[key]", arg=last["track_key"])
    assert world.page.evaluate("playerRequests.filter(r => r.path === '/api/heard').length") == 1
    # S22 owns key wiring; exercise its shared target/action through real clicks.
    for verdict in ("keep", "pass"):
        target = world.page.evaluate("stateModule.selectors.judgingTarget(store.getState())")
        gesture(world, "judge", {"trackKey": target["trackKey"], "stepId": target["stepId"],
                                   "verdict": verdict, "mode": "key"})
    assert world.page.evaluate("store.getState().ui.route.sel") == "track:" + first["track_key"]
    with closing(storage.connect(world.cfg.db_path)) as conn:
        assert tuple(conn.execute("SELECT step_id, verdict FROM judgments WHERE track_key = ?", (last["track_key"],)).fetchone()) == (root["step_id"], "pass")
        assert conn.execute("SELECT first_step_id FROM heard WHERE track_key = ?", (last["track_key"],)).fetchone()[0] == root["step_id"]


def test_the_selection_follows_the_music_until_you_select_something_else(world):
    # product-ux §5.1 (2026-10-16): a selection that is the playing track moves with
    # the music, so the inspector shows what plays; selecting anything else stops it.
    root = start(world)
    unplayable = {None, "S9GONE00100", "S9REFUSE101", "Pso5YU_mntg"}
    first = next(p for p in picks(world, root["step_id"]) if p["video_id"] not in unplayable)
    page = world.page
    selected = lambda: page.evaluate("store.getState().ui.route.sel")
    in_address = lambda: page.evaluate("stateModule.parseRoute(location.hash).sel")
    page.evaluate("p => store.actions.selectPoint(p.step_id, `track:${p.track_key}`)", first)
    play_pick(world, first)
    page.get_by_role("button", name="Next track", exact=True).click()
    page.wait_for_function("key => (store.getState().player.current?.trackKey || key) !== key", arg=first["track_key"])
    second = page.evaluate("store.getState().player.current.trackKey")
    assert selected() == in_address() == "track:" + second
    page.evaluate("p => store.actions.selectPoint(p.step_id, `track:${p.track_key}`)", first)
    page.get_by_role("button", name="Next track", exact=True).click()
    page.wait_for_function("key => (store.getState().player.current?.trackKey || key) !== key", arg=second)
    assert selected() == in_address() == "track:" + first["track_key"]


def test_press_play_on_missing_video_still_finishes_refusal(world):
    root = start(world)
    missing = next(pick for pick in picks(world, root["step_id"]) if pick["video_id"] is None)
    play_pick(world, missing)
    advance(world, 800)
    world.page.get_by_role("button", name="Play", exact=True).click()
    assert player(world)["state"] == "refused"
    expect(world.page.locator("#lcd-note")).to_contain_text("No video for this track. Skipping in")
    advance(world, 1199)
    assert player(world)["current"]["pickId"] == missing["id"]
    advance(world, 1)
    world.page.wait_for_function("id => store.getState().player.current.pickId !== id", arg=missing["id"])
    assert loads(world)[-1]["at"] == 2000
    assert player(world)["state"] == "playing"
