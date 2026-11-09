"""S12 shell contracts through the served app, real store, and isolated SQLite."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.slice_browser import launch, served_app


COMPONENTS = {
    "player": ["mountPlayer"],
    "seedbox": ["mountSeedbox"],
    "steplist": ["mountStepList", "mountSlimList"],
    "trail": ["mountTrail"],
    "inspector": ["mountInspector"],
    "moves": ["mountMoves"],
    "stepmap": ["mountStepMap"],
    "territory": ["mountTerritory"],
    "keys": ["mountKeys"],
    "feeds": ["mountFeeds"],
    "setup": ["mountSetup"],
    "connect": ["mountConnect"],
}


def component_fixture(name):
    """Contract stand-ins exercise loader wiring without adding future lane files."""
    source = """
      function mounted(entry, root, ctx) {
        window.mountCalls ||= []; window.destroyCalls ||= [];
        const keys = ['store','api','text','h','safeUrl','clampPopover','mounts','clock'];
        const valid = Object.isFrozen(ctx) && Object.isFrozen(ctx.mounts) &&
          keys.every(key => key in ctx) && ctx.mounts[root.id] === root &&
          ['now','setTimeout','clearTimeout','setInterval','clearInterval']
            .every(key => typeof ctx.clock[key] === 'function');
        mountCalls.push({entry, root: root.id, valid});
        let node = null;
        if (entry === 'mountPlayer') {
          node = ctx.h('iframe', {id: 'player', src: '/__shell_video__',
            title: 'Fixture video', tabindex: '-1'});
          document.getElementById('player').replaceWith(node);
        } else if (entry === 'mountKeys') {
          ctx.mounts.keyHints.textContent = ctx.text('S-KS-SEARCH');
          node = ctx.h('button', {type: 'button', 'data-action': 'keys',
            style: {minHeight: '28px'}, onclick: () => ctx.store.actions.toggleSheet()}, '? keys');
          ctx.mounts.keyStrip.append(node);
        }
        return {destroy() {
          destroyCalls.push(entry);
          if (entry === 'mountPlayer') node.replaceWith(ctx.h('div', {id: 'player'}));
          else node?.remove();
        }};
      }
    """
    for entry in COMPONENTS[name]:
        source += f"\nexport function {entry}(root, ctx) {{ return mounted('{entry}', root, ctx); }}\n"
    if name == "stepmap":
        source += "\nexport function drawStep() {}\nexport function hitStep() { return null; }\n"
    return source


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    present = set(getattr(request, "param", ["player", "keys"]))
    with served_app(tmp_path, monkeypatch) as server, sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors, external, requests, missing = [], [], [], []

        def local_only(route):
            url = route.request.url
            if not url.startswith(server.url + "/"):
                external.append(url)
                route.abort()
                return
            path = urlsplit(url).path
            requests.append(path)
            match = re.fullmatch(r"/static/([a-z]+)\.(js|css)", path)
            if match and match[1] in COMPONENTS:
                name, extension = match.groups()
                if name not in present:
                    missing.append(path)
                    route.fulfill(status=404, body="Not merged", content_type="text/plain")
                else:
                    route.fulfill(
                        content_type="text/javascript" if extension == "js" else "text/css",
                        body=component_fixture(name) if extension == "js" else "",
                    )
            elif path == "/__shell_video__":
                route.fulfill(content_type="text/html", body=(
                    "<!doctype html><title>Reserved video paint</title>"
                    "<style>html,body{margin:0;width:100%;height:100%;background:#35c982}</style>"
                ))
            else:
                route.continue_()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server.url + "/#/dig/new")
        page.wait_for_selector('#device[data-shell-state="ready"]')
        page.evaluate("async () => { window.shellModule = await import('/static/slice.js'); window.shell = await shellModule.boot(); }")
        server.page, server.context = page, context
        server.present, server.requests, server.missing = present, requests, missing
        server.browser_mode = "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome"
        try:
            yield server
        finally:
            page.evaluate("window.shell?.destroy()")
            context.close()
            browser.close()
        assert errors == []
        assert external == [], "Shell test attempted an external network call"


def start_step(world):
    result = world.page.evaluate("""async point => {
      shell.store.actions.addChip(point);
      return await shell.store.actions.startDig();
    }""", world.providers.point("seed-a").to_dict())
    world.page.wait_for_function(
        "id => shell.store.getState().steps.byId[id]?.status === 'done'", arg=result["step_id"]
    )
    return result


def install_clock(page):
    page.evaluate("""async () => {
      shell.destroy();
      let now = 0, serial = 0;
      const timers = new Map();
      window.testClock = {
        now:() => now,
        setTimeout:(fn,delay) => {const id=++serial;timers.set(id,{fn,at:now+delay});return id;},
        clearTimeout:id => timers.delete(id),
        setInterval:(fn,delay) => {const id=++serial;timers.set(id,{fn,at:now+delay,interval:delay});return id;},
        clearInterval:id => timers.delete(id)
      };
      window.advance = (ms) => {
        const target = now+ms;
        for (let i=0;i<1000;i++) {
          const next = [...timers].filter(([,t]) => t.at <= target).sort((a,b) => a[1].at-b[1].at)[0];
          if (!next) {now=target;return;}
          const [id,t] = next; now=t.at;
          if (t.interval) t.at += t.interval; else timers.delete(id);
          t.fn();
        }
        throw new Error('Clock failed to settle');
      };
      window.shell = await shellModule.boot({clock:testClock});
    }""")


def test_initial_server_failure_rehydrates_on_recovery(world):
    page = world.page
    start_step(world)
    world.context.route("**/api/health", lambda route: route.fulfill(status=503, json={"detail": "Fixture starting"}))
    install_clock(page)
    assert page.locator("#server-banner").is_visible()
    assert page.evaluate("shell.store.getState().ui.route.stepId") is None
    world.context.unroute("**/api/health")
    page.evaluate("advance(5000)")
    page.wait_for_function("shell.store.getState().ui.server === 'up'")
    assert page.evaluate("shell.store.getState().ui.route.stepId") is not None
    assert not page.locator("#server-banner").is_visible()
    assert page.locator("#about-version").text_content() == (
        "Digger is running where you started cosine-digger serve, in a Terminal window or a container, and stops when that stops. Your settings are in "
        f"{world.cfg.home} and your digs and keeps in {world.cfg.db_path.resolve()}.")


LOST_TODAY = "Lost the local server. Your dig is saved; reconnecting…"
LOST = {
    "start": ("Can't reach Digger at this address. Your digs are saved. In Terminal, cosine-digger start "
              "starts it again, or opens it where it's already running.",
              "Can't reach Digger. Run cosine-digger start in Terminal."),
    "serve": ("Can't reach Digger at this address. Your digs are saved. Start Digger again the way you "
              "started it; this page reconnects when it's back.",
              "Can't reach Digger. Start it again the way you started it."),
}


@pytest.mark.parametrize("launcher", ["start", "serve"])
def test_lost_server_says_how_to_start_digger_again(world, launcher):
    """Today's banner for 10 s, then the advice for the way the last Digger seen was started."""
    page = world.page

    def as_launcher(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "launcher": launcher})

    world.context.route("**/api/health", as_launcher)
    install_clock(page)
    page.wait_for_function("shell.store.getState().ui.server === 'up'")
    data = f"Your settings are in {world.cfg.home} and your digs and keeps in {world.cfg.db_path.resolve()}."
    assert page.locator("#about-version").text_content().endswith(data)
    assert page.locator("#about-version").text_content().startswith(
        "Digger runs as a small server on this computer." if launcher == "start"
        else "Digger is running where you started cosine-digger serve")
    banner, (later, unreachable) = page.locator("#server-banner"), LOST[launcher]

    world.context.unroute("**/api/health")
    world.context.route("**/api/**", lambda route: route.abort())
    page.evaluate("advance(5000)")
    page.wait_for_function("shell.store.getState().ui.server === 'down'")
    assert banner.is_visible() and banner.text_content() == LOST_TODAY
    page.evaluate("advance(9999)")
    assert banner.text_content() == LOST_TODAY
    page.evaluate("advance(1)")
    assert banner.text_content() == later
    message = page.evaluate("shell.api.request('GET', '/api/settings').catch((error) => error.message)")
    assert message == unreachable

    world.context.unroute("**/api/**")
    page.evaluate("advance(5000)")
    page.wait_for_function("shell.store.getState().ui.server === 'up'")
    assert not banner.is_visible()
    world.context.route("**/api/**", lambda route: route.abort())
    page.evaluate("advance(5000)")
    page.wait_for_function("shell.store.getState().ui.server === 'down'")
    assert banner.text_content() == LOST_TODAY, "A new loss starts with today's banner again"
    world.context.unroute("**/api/**")


def test_safe_dom_catalog_local_times_fonts_and_original_tokens(world):
    page = world.page
    result = page.evaluate("""async () => {
      const dom = await import('/static/dom.js');
      const payload='<img src=x onerror=alert(1)>';
      const box=dom.h('div', {'data-action':'fixture',dataset:{pickId:'pick:1'}, 'aria-disabled':false},
        dom.h('a',{href:'javascript:alert(1)'},payload),
        dom.h('a',{href:'https://music.youtube.com/watch?v=fixture',target:'_blank'},'External fixture'),
        dom.h('button',{type:'button',onclick:() => window.clicked=true},payload));
      document.getElementById('step-region').append(box);
      box.querySelector('button').click();
      let htmlRejected=false;
      try {dom.h('div',{innerHTML:payload});} catch {htmlRejected=true;}
      let srcdocRejected=false;
      try {dom.h('iframe',{srcDoc:payload});} catch {srcdocRejected=true;}
      const outside=dom.h('div',{},'Outside-stage fixture'); document.body.append(outside);
      let outsideRejected=false;
      try {dom.clampPopover(outside,{left:0,top:0},document.body);} catch {outsideRejected=true;}
      outside.remove();
      const css=getComputedStyle(document.documentElement);
      const tokens=Object.fromEntries(['--void','--case-hi','--case-mid','--case','--case-2','--case-lo',
        '--panel','--panel-2','--panel-3','--line','--lcd-bg','--lcd','--lcd-soft','--lcd-dim','--lcd-glow',
        '--amber','--cyan','--magenta','--red','--white','--text','--muted','--focus',
        '--f-pixel','--f-lcd','--f-ui','--f-body','--f-mono','--bevel-out','--bevel-in','--hard-shadow','--tap',
        '--st-success','--st-warning','--st-error','--st-pending','--st-info',
        '--mark-selected','--mark-playing','--mark-kept','--mark-passed',
        '--node-pick','--node-label','--node-person','--node-seed'].map(key => [key,css.getPropertyValue(key).trim()]));
      for (const family of ['Press Start 2P','VT323','Silkscreen']) await document.fonts.load('16px "'+family+'"');
      return {
        ids:Object.keys(dom.strings), strings:dom.strings, frozen:Object.isFrozen(dom.strings),
        urls:['javascript:alert(1)','java\\nscript:alert(1)','data:text/html,x','file:///tmp/x',
          'https://user:password@example.com','http://[bad','',null].map(dom.safeUrl),
        safe:dom.safeUrl('/static/favicon.svg'),
        text:box.textContent, injected:box.querySelector('img') !== null,
        href:box.querySelector('a').getAttribute('href'), rel:box.querySelectorAll('a')[1].rel,
        clicked:window.clicked,htmlRejected,srcdocRejected,outsideRejected,aria:box.getAttribute('aria-disabled'),tokens,
        variants:[dom.text('S-SB-BTN',{n:1}),dom.text('S-SB-BTN',{n:2,kind:'seed'}),
          dom.text('S-LCD-FROM',{n:2,sentence:'Sounds like the label Ilian Tape'}),
          dom.text('S-SB-RECENT-META',{when:'15:07',s:1,k:0})],
        times:[dom.formatWhen('2026-10-01T15:07:00',{now:'2026-10-01T22:00:00'}),
          dom.formatWhen('2026-10-02T15:07:00',{now:'2026-10-01T22:00:00'}),dom.formatWhen('bad')],
        fonts:[...document.fonts].map(font => ({family:font.family,status:font.status}))
      };
    }""")
    assert result["frozen"] and result["urls"] == [None] * 8
    assert result["safe"] == world.url + "/static/favicon.svg"
    assert not result["injected"] and result["href"] is None and result["htmlRejected"]
    assert result["srcdocRejected"] and result["aria"] == "false"
    assert result["outsideRejected"]
    assert result["clicked"] and "noopener" in result["rel"] and "<img src=x onerror=alert(1)>" in result["text"]
    assert result["variants"] == ["Dig from 1 track ▸", "Dig from 2 seeds ▸",
                                  "from step 2: sounds like the label Ilian Tape", "15:07 · 1 step"]
    assert result["times"] == ["15:07", "Fri 2 Oct 15:07", ""]
    catalog = (Path(__file__).parents[1] / "docs/slice/product-ux.md").read_text().split("## 8. String catalog")[1].split("## 9.")[0]
    for line in catalog.splitlines():
        match = re.match(r"\| (S-[A-Z0-9-]+) \| (.*) \|$", line)
        if not match:
            continue  # Shorthand families are explicitly expanded below.
        key, value = match.groups()
        assert key in result["ids"]
        if not any(marker in value for marker in ["{", "(", " / "]):
            assert result["strings"][key] == value
    assert {"S-INS-K-" + suffix for suffix in ["TRACK", "LABEL", "PERSON", "ORIGIN", "STEP"]} <= set(result["ids"])
    assert {"S-T-NOTOKEN-" + suffix for suffix in ["L", "A", "C"]} <= set(result["ids"])
    tokens = result["tokens"]
    expected = {
        "--void": "#06070b", "--case-hi": "#7c8497", "--case-mid": "#3a404d",
        "--case": "#2a2f3a", "--case-2": "#1e222b", "--case-lo": "#0a0b0f",
        "--panel": "#10131a", "--panel-2": "#161a23", "--panel-3": "#1c2130",
        "--line": "#2b3242", "--lcd-bg": "#020d06", "--lcd": "#3dff7a",
        "--lcd-soft": "#2fd865", "--lcd-dim": "#1c5a33", "--lcd-glow": "rgba(61, 255, 122, 0.45)",
        "--amber": "#ffb627", "--cyan": "#3fd8ff", "--magenta": "#ff5bd8",
        "--red": "#ff5a5a", "--white": "#f4f6fb", "--text": "#e6e9f1",
        "--muted": "#a7afc0", "--focus": "#ffe14d", "--tap": "36px",
        "--hard-shadow": "3px 3px 0 rgba(0, 0, 0, 0.6)",
    }
    assert {key: tokens[key] for key in expected} == expected
    for alias, primitive in {
        "--st-success": "--lcd", "--st-warning": "--amber", "--st-error": "--red",
        "--st-pending": "--muted", "--st-info": "--cyan", "--mark-selected": "--focus",
        "--mark-playing": "--lcd", "--mark-kept": "--magenta", "--mark-passed": "--muted",
        "--node-pick": "--cyan", "--node-label": "--amber", "--node-person": "--lcd-soft", "--node-seed": "--white",
    }.items():
        assert tokens[alias] == tokens[primitive]
    assert tokens["--f-pixel"].startswith('"Press Start 2P"')
    assert tokens["--f-lcd"].startswith('"VT323"') and tokens["--f-ui"].startswith('"Silkscreen"')
    assert tokens["--f-body"].startswith("system-ui") and tokens["--f-mono"].startswith("ui-monospace")
    assert tokens["--bevel-out"] == "#7c8497 #0a0b0f #0a0b0f #7c8497"
    assert tokens["--bevel-in"] == "#0a0b0f #7c8497 #7c8497 #0a0b0f"
    assert {font["family"] for font in result["fonts"] if font["status"] == "loaded"} >= {"Press Start 2P", "VT323", "Silkscreen"}
