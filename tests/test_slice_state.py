"""S11's actual ES modules, served production routes and fixture provider HTTP."""

import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.slice_browser import launch, served_app


@pytest.fixture
def world(tmp_path, monkeypatch):
    with served_app(tmp_path, monkeypatch) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context()
        failures = []

        def local_only(route):
            if route.request.url.startswith(server.url + "/"):
                if route.request.url.split("?", 1)[0].endswith("/__state_test__"):
                    route.fulfill(content_type="text/html", body="<!doctype html><title>S11 module harness</title>")
                else:
                    route.continue_()
            else:
                failures.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        server.page = page
        server.context = context
        page.goto(server.url + "/__state_test__#/dig/new")
        install(page)
        try:
            yield server
        finally:
            page.evaluate("window.store?.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert failures == [], "A browser tried to call a real external service"


def install(page, setup=""):
    page.evaluate("""async (setup) => {
      window.stateModule = await import('/static/state.js');
      window.apiModule = await import('/static/api.js');
      window.nativeFetch = window.fetch.bind(window);
      window.transportFetch = (...args) => nativeFetch(...args);
      window.requests = [];
      window.historyCalls = [];
      window.announcements = [];
      for (const method of ['pushState', 'replaceState']) {
        const original = history[method].bind(history);
        history[method] = (...args) => { historyCalls.push([method, args[2]]); return original(...args); };
      }
      window.makeStore = (extra = {}) => {
        store?.destroy();
        window.api = apiModule.createApi({fetch: (...args) => {
          requests.push({method: args[1]?.method || 'GET', path: args[0]});
          return transportFetch(...args);
        }, ...extra.api});
        window.store = stateModule.createStore({api, storage: localStorage, sessionStorage,
          history, location, ...extra.store});
        let serial = -1;
        store.subscribe((s) => {
          if (s.ui.announcement && s.ui.announcement.serial !== serial) {
            serial = s.ui.announcement.serial;
            announcements.push(s.ui.announcement);
          }
        });
        return store;
      };
      window.store = null;
      if (setup) eval(setup); else makeStore();
      await store.actions.hydrate();
    }""", setup)


def start(world, name="seed-a"):
    return world.page.evaluate("""async point => {
      await store.actions.addChip(point);
      const [first, second] = await Promise.all([store.actions.startDig(), store.actions.startDig()]);
      if (first.step_id !== second.step_id) throw new Error('Double start created two roots');
      return first;
    }""", world.providers.point(name).to_dict())


def wait_complete(page, step_id, *, terminal=True):
    page.wait_for_function("""({id, terminal}) => {
      const s = store.getState(), step = s.steps.byId[id];
      return step && (terminal ? step.status !== 'running' : stateModule.selectors.isPicksComplete(s, id));
    }""", arg={"id": step_id, "terminal": terminal})


def wait_until(page, script, arg, timeout=30):
    # page.evaluate awaits an async script; wait_for_function would treat its
    # pending Promise as truthy and return before the condition holds.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(script, arg):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"not true within {timeout} s: {script}")


def picks(page, step_id):
    return page.evaluate("id => stateModule.selectors.orderedPicks(store.getState(), id)", step_id)


def from_pick(pick):
    return {"point_id": "track:" + pick["track_key"], "kind": "track", "name": f"{pick['artist']} - {pick['title']}",
            "artist": pick["artist"], "title": pick["title"], "track_key": pick["track_key"],
            "cosine_id": pick["cosine_id"], "video_id": pick["video_id"], "indexed": True,
            "source_step_id": pick["step_id"], "source_rank": pick["rank"]}


def test_lost_post_acknowledgment_reuses_key_and_conflict_reload_keeps_audio_intent(world):
    page = world.page
    page.evaluate("""() => {
      window.rootKeys = []; window.loseReply = true;
      transportFetch = async (path, init) => {
        const response = await nativeFetch(path, init);
        if (path === '/api/digs' && init.method === 'POST') {
          rootKeys.push(init.headers['Idempotency-Key']);
          if (loseReply) { loseReply = false; await response.text(); throw new TypeError('Lost response'); }
        }
        return response;
      };
    }""")
    root = start(world)
    wait_complete(page, root["step_id"])
    keys = page.evaluate("rootKeys")
    assert len(keys) == 2 and keys[0] == keys[1]
    digs = page.evaluate("api.request('GET', '/api/digs')")["digs"]
    assert len(digs) == 1 and digs[0]["step_count"] == 1
    shared = picks(page, root["step_id"])[0]
    page.evaluate("""point => {
      store.actions.selectPoint(point.source_step_id, point);
      store.actions.playPick({pickId: `${point.source_step_id}:${point.source_rank}`});
    }""", from_pick(shared))
    page.evaluate("""pick => api.request('PUT', `/api/judgments/${encodeURIComponent(pick.track_key)}`,
      {step_id: pick.step_id, verdict: 'keep', expected_revision: 0})""", shared)
    result = page.evaluate("""pick => store.actions.judge({trackKey: pick.track_key, stepId: pick.step_id, verdict: 'pass'})""", shared)
    assert result is None
    assert page.evaluate("store.getState().ui.toast.message") == "This judgment changed elsewhere; try again."
    # The selection was the playing track, so it follows the music past the skipped one (product-ux §5.1).
    assert page.evaluate("store.getState().ui.route.sel") == "track:" + page.evaluate("store.getState().player.current.trackKey")
    assert page.evaluate("store.getState().player.current.pickId") != shared["id"]
    assert page.evaluate("key => store.getState().judgments.revisions[key]", shared["track_key"]) == 1
    assert page.evaluate("id => stateModule.selectors.marks(store.getState(), id).kept", shared["id"])
    assert page.evaluate("store.getState().judgments.undo") == []
    page.evaluate("store.actions.setServer('down')")
    puts = page.evaluate("requests.filter(r => r.method === 'PUT').length")
    page.evaluate("""pick => store.actions.judge({trackKey: pick.track_key, stepId: pick.step_id, verdict: 'keep', mode: 'toggle'})""", shared)
    assert page.evaluate("requests.filter(r => r.method === 'PUT').length") == puts
    page.evaluate("""pick => store.actions.markHeard({trackKey: pick.track_key, stepId: pick.step_id})""", shared)
    assert page.evaluate("requests.filter(r => r.path === '/api/heard').length") == 0
    page.evaluate("store.actions.setServer('up')")
    assert page.evaluate("key => store.getState().heard.byTrack[key].first_step_id", shared["track_key"]) == root["step_id"]


def test_fx6_07_bounded_streams(world):
    page = world.page
    gate = world.providers.block("facts")
    root = start(world)
    wait_complete(page, root["step_id"], terminal=False)
    assert gate.entered.wait(3)
    sources = picks(page, root["step_id"])
    children = page.evaluate("""async points => Promise.all(points.map(point => api.request('POST', '/api/steps', {
      parent_step_id: point.source_step_id, origins: [point], move: 'track', further: false})))""",
        [from_pick(pick) for pick in sources[:5]])
    wait_until(page, """async ids => (await Promise.all(ids.map(async id =>
      (await api.request('GET', `/api/steps/${id}`)).snapshot.step))).every(step => step.picks_done_at)
    """, [child["step_id"] for child in children])
    # Instrument stream lifetime without opening six sockets in the regression.
    # Jobs and snapshots still come from production HTTP and isolated SQLite.
    page.evaluate("""() => {
      window.streams = []; window.maxStreams = 0;
      class Source {
        constructor(url) {
          this.url = url; this.closed = false; streams.push(this);
          maxStreams = Math.max(maxStreams, streams.filter(source => !source.closed).length);
        }
        addEventListener() {}
        close() { this.closed = true; }
      }
      makeStore({api: {EventSource: Source}});
    }""")
    page.evaluate("store.actions.hydrate()")
    assert page.evaluate("streams.filter(source => !source.closed).length") == 1
    page.evaluate("store.actions.openRoute({page: 'feeds'})")
    assert page.evaluate("streams.filter(source => !source.closed).length") == 0
    page.evaluate("id => store.actions.playPick({pickId: `${id}:1`})", root["step_id"])
    page.evaluate("r => store.actions.openRoute({page: 'dig', digId: r.dig_id, stepId: r.step_id})", children[0])
    assert page.evaluate("streams.filter(source => !source.closed).length") == 2
    assert page.evaluate("maxStreams") <= 2
    gate.released.set()
    wait_complete(page, children[1]["step_id"])
    assert page.evaluate("store.getState().ui.route.stepId") == children[0]["step_id"]
    assert page.evaluate("store.getState().player.current.stepId") == root["step_id"]
