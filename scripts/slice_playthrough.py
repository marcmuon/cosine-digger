# /// script
# dependencies = ["playwright"]
# ///
"""Complete slice journeys through the CLI, HTTP, SQLite and mounted player.

Run with uv after uv sync --frozen --extra dev. The PEP 723 environment owns
Playwright; the CLI subprocess uses the repository's synced environment.
"""

from __future__ import annotations

import argparse
from base64 import b64decode
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.request import ProxyHandler, build_opener
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("state", "shell", "player", "seeds-list", "trail", "inspector", "map",
          "territory", "keys", "feeds", "setup")
# Fixture HTTP retains production Discogs pacing. Allow load while bounding
# a step wait, the rest of each journey, and the complete pytest subprocess.
STEP_STALL_SECONDS = 60
STEP_WAIT_SECONDS = 240
JOURNEY_TIMEOUT_SECONDS = STEP_WAIT_SECONDS + 180
PLAYTHROUGH_TIMEOUT_SECONDS = len(GROUPS) * (JOURNEY_TIMEOUT_SECONDS + 60)
LEGACY = ("sessions", "session_results", "session_branches", "result_events")
API_SCRIPT = re.compile(r"^https://(?:www\.)?youtube\.com/(?:iframe_api|player_api)(?:\?.*)?$")
# Fixture facts name this Discogs image; stub runs serve it locally.
FIXTURE_ARTWORK = re.compile(r"^https://i\.discogs\.com/slice-fixture\.jpg$")
PIXEL_GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
LIVE_KEYS = ("COSINE_API_TOKEN", "DISCOGS_TOKEN")
JOURNEYS = {"PT01": "seeds-list", "PT02": "seeds-list", "PT03": "player",
            "PT04": "player", "PT05": "player", "PT06": "state", "PT07": "trail",
            "PT08": "inspector", "PT09": "inspector", "PT10": "territory",
            "PT11": "keys", "PT12": "shell", "PT13": "setup"}
JOURNEY_GROUPS = {"PT03": ("player", "trail"), "PT07": ("trail", "state"),
                  "PT08": ("inspector", "map", "keys"),
                  "PT09": ("inspector", "seeds-list", "map"),
                  "PT10": ("map", "territory"), "PT13": ("feeds", "setup")}
CANVAS_PROBE = """(() => {
  window.canvasPaint={};window.canvasFrames={};
  const proto=CanvasRenderingContext2D.prototype;
  for(const name of ['clearRect','arc','fillRect','strokeRect','fillText','stroke','setLineDash']) {
    const original=proto[name];proto[name]=function(...args){
      const id=this.canvas.id;
      if(['stepmap-canvas','territory-canvas','spectrum'].includes(id)) {
        if(name==='clearRect'){canvasPaint[id]=[];canvasFrames[id]=(canvasFrames[id]||0)+1;}
        (canvasPaint[id]||=[]).push({name,args,alpha:this.globalAlpha,fill:this.fillStyle,stroke:this.strokeStyle,dash:this.getLineDash()});
      }return original.apply(this,args);
    };
  }
})();"""


def fixture_cli(arguments):
    """Harness-only controls, over stdin, around the ordinary serve command.

    Runs in the synced repo environment, never in the PEP 723 environment.
    The production fixture gate is checked before injecting any controls.
    There are no test endpoints, query switches or production clock hooks.
    """
    import threading
    import wave
    import httpx
    from cosine_digger import api, chain, client, discogs, storage
    from cosine_digger.cli import app
    from cosine_digger.web import slice_testmode
    from mutagen.id3 import TALB, TIT2, TPE1, TPUB
    from mutagen.wave import WAVE

    fixture = slice_testmode.fixture_from_environment(host="127.0.0.1", scheduler=False)
    if fixture is None:
        raise RunFailure("Harness controls require the explicit fixture gate")
    home = Path(os.environ["COSINE_DIGGER_HOME"])
    slice_testmode.require_temporary_storage(home, Path(os.environ["COSINE_DIGGER_DB"]))
    original_factory = slice_testmode.fixture_from_environment

    def factory(**kwargs):
        return fixture if original_factory(**kwargs) is not None else None

    slice_testmode.fixture_from_environment = factory
    # As in tests/slice_browser.py: substitute waits, not engine/HTTP/storage.
    discogs.pace = lambda *args: None
    client._retry_wait = lambda *args: 0
    chain.make_rate_limited_client = fixture.discogs_client
    original_send=httpx.HTTPTransport.handle_request
    def local_only(transport,request):
        # Legacy feed anchor lookup uses httpx.get directly. Substitute that
        # transport with the same fixture payloads, rather than a network call.
        if request.url.host=="api.discogs.com":
            return fixture._discogs(request)
        if request.url.host in {"cosine.club","www.youtube.com"}:
            return fixture._cosine(request)
        if request.url.host not in {"127.0.0.1","localhost"}:
            raise RunFailure("Stub server attempted an external request")
        return original_send(transport,request)
    httpx.HTTPTransport.handle_request=local_only
    retry_failure=[False]
    original_cosine=fixture._cosine
    def cosine_response(request):
        if retry_failure[0] and request.url.path.endswith('/tracks/retry-once/similar') and request.url.params.get('limit')!='1':
            fixture._call('cosine',request)
            return httpx.Response(429,headers={'Retry-After':'2'},json={'code':'RATE_LIMITED','retry_after_s':2})
        return original_cosine(request)
    fixture._cosine=cosine_response

    def controls():
        for line in sys.stdin:
            try:
                message = json.loads(line)
                operation, value = message["operation"], message.get("value")
                if operation == "block":
                    fixture.block(value)
                    result = True
                elif operation == "entered":
                    result = fixture._gates[value].entered.wait(5)
                elif operation == "release":
                    fixture._gates[value].released.set()
                    result = True
                elif operation == "release-all":
                    fixture.release_all()
                    result = True
                elif operation == "points":
                    result = [fixture.point(name).to_dict() for name in value]
                    for name, point in zip(value, result):
                        if name == "unindexed":
                            with closing(storage.connect(home / "taste.db")) as conn:
                                identity = conn.execute("SELECT id FROM tracks WHERE path=?", (fixture.library_rows[1]["path"],)).fetchone()
                            if identity:
                                point.update(library_id=identity[0], point_id=f"library:{identity[0]}")
                elif operation == "fixture-library":
                    with closing(storage.connect(home / "taste.db")) as conn, conn:
                        for row in fixture.library_rows:
                            data = {k:v for k,v in row.items() if k != "id"}
                            if not conn.execute("SELECT id FROM tracks WHERE path=?",(data["path"],)).fetchone():
                                conn.execute(f"INSERT INTO tracks ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
                    result = True
                elif operation == "trace":
                    result = list(fixture.calls)
                elif operation == "retry-failure":
                    retry_failure[0]=bool(value)
                    result=True
                elif operation == "feed-library":
                    # Fixture planners must not resolve anchors from the copied
                    # source library. import_library also recomputes its signals.
                    # Keep the served ledger and legacy tables for no-write checks.
                    with closing(storage.connect(home / "taste.db")) as conn, conn:
                        conn.execute("DELETE FROM tracks")
                        api.import_library(conn,[{"id":"seed-a","artist":"Fixture Artist A","title":"Seed A"}],source="playthrough-feed")
                    result=True
                elif operation == "legacy-keep":
                    from cosine_digger.leads import record_event
                    track=api.Track(id=value['cosine_id'],name=value['title'],track=value['title'],artist=value['artist'],video_id=value['video_id'])
                    with closing(storage.connect(home / 'taste.db')) as conn:
                        session=api.record_dig(conn,['Old CLI - Seed'],[api.Pick(track,0.8,'Legacy')])
                        identity=conn.execute('SELECT id FROM session_results WHERE session_id=?',(session,)).fetchone()[0]
                        record_event(conn,identity,'save')
                    result=session
                elif operation == "rerun-change":
                    for letter in "abc":
                        fixture._neighbors[f"seed-{letter}"] = fixture._neighbors[f"seed-{letter}"][1:]
                    result = True
                elif operation == "long-strings":
                    artist='Discovery Artist With An Exceptionally Long Name '*3
                    title='An Unusually Long Tagged Recording Title And Mix Version '*4
                    data={'artist':artist,'track':title,'name':artist+' - '+title}
                    fixture._tracks['shared-track'].update(data)
                    for neighbors in fixture._neighbors.values():
                        for item in neighbors:
                            if item['id']=='shared-track':item.update(data)
                    release=fixture._releases['/releases/2000']
                    release['title']=title;release['artists'][0]['name']=artist
                    release['tracklist'][0].update(title=title,artists=release['artists'])
                    result=True
                elif operation == "library":
                    folder = home / "generated-library"
                    for i, (artist, album, title, label) in enumerate([
                        ("Owned Label Artist", "Old Album", "Old File", "Label A"),
                        ("Fixture Artist A", "Missing Album A", "Unindexed File", None),
                        ("Library Artist B", "Missing Album B", "Pending File", None),
                    ]):
                        path = folder / f"generated-{i}.wav"
                        with wave.open(str(path), "wb") as stream:
                            stream.setparams((1, 2, 8000, 80, "NONE", "not compressed"))
                            stream.writeframes(b"\0\0" * 80)
                        audio = WAVE(path)
                        audio.add_tags()
                        for frame, text_value in [(TPE1, artist), (TALB, album), (TIT2, title), (TPUB, label)]:
                            if text_value:
                                audio.tags.add(frame(encoding=3, text=[text_value]))
                        audio.save()
                    result = str(folder)
                elif operation == "no-library":
                    # Only the copied, temporary DB; never a configured folder.
                    with closing(storage.connect(home / "taste.db")) as conn, conn:
                        conn.execute("DELETE FROM tracks")
                    result = True
                elif operation == "fresh-dig-home":
                    # PT01's first-run screen needs an empty GUI history even
                    # when the read-only source already contains saved digs.
                    with closing(storage.connect(home / "taste.db")) as conn, conn:
                        for table in ("slice_events", "slice_jobs", "step_hubs", "step_picks",
                                      "heard", "judgments", "judgment_controls", "steps", "digs"):
                            conn.execute(f'DELETE FROM "{table}"')
                    result = True
                elif operation == "clear-http-cache":
                    with closing(storage.connect(home / 'taste.db')) as conn,conn:
                        conn.execute('DELETE FROM http_cache')
                    result=True
                elif operation == "missing-labels":
                    with closing(storage.connect(home / 'taste.db')) as conn,conn:
                        conn.execute('UPDATE tracks SET label=NULL WHERE path IN (?,?)',
                          tuple(str((home/'generated-library'/f'generated-{i}.wav').resolve()) for i in (1,2)))
                        conn.execute('DELETE FROM http_cache')
                    result=True
                else:
                    raise RunFailure("Unknown harness control")
                print("SLICE-CONTROL " + json.dumps({"id":message["id"],"ok": True, "result": result}), flush=True)
            except Exception:
                print("SLICE-CONTROL " + json.dumps({"id":message.get("id"),"ok":False}), flush=True)

    threading.Thread(target=controls, daemon=True).start()
    app(args=arguments)


class Sandbox:
    """Lifecycle and private pipe of exactly the process this runner owns."""

    def __init__(self, environment, port, directory, report, *, live):
        self.environment, self.port, self.directory, self.report, self.live = environment, port, directory, report, live
        self.url = f"http://127.0.0.1:{port}"
        self.db = Path(environment["COSINE_DIGGER_DB"])
        self.process = None
        self.replies = queue.Queue()
        self.control_id = 0

    def start(self):
        self.replies = queue.Queue()
        args = ["serve", "--host", "127.0.0.1", "--port", str(self.port), "--no-scheduler"]
        command = [str(ROOT / ".venv/bin/cosine-digger"), *args] if self.live else [
            str(ROOT / ".venv/bin/python"), str(Path(__file__).resolve()), "--fixture-cli", *args]
        self.process = subprocess.Popen(command, cwd=self.directory, env=self.environment,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, bufsize=1)
        # Drain CLI logs continuously: a full stdout pipe would block uvicorn.
        def drain(pipe):
            for line in pipe:
                if line.startswith("SLICE-CONTROL "):
                    self.replies.put(json.loads(line[len("SLICE-CONTROL "):]))
        self.reader = threading.Thread(target=drain,args=(self.process.stdout,),daemon=True)
        self.reader.start()
        self.report["framework"]["pid"] = self.process.pid
        self.report["framework"].setdefault("processes", []).append({"pid": self.process.pid, "port": self.port})
        deadline = time.monotonic() + 20
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                with opener.open(self.url + "/api/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.05)
        raise RunFailure("Sandbox CLI did not become healthy")

    def control(self, operation, value=None):
        if self.live:
            raise RunFailure("Fixture controls are unavailable in live mode")
        self.control_id += 1
        self.process.stdin.write(json.dumps({"id":self.control_id,"operation": operation, "value": value}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                answer=self.replies.get(timeout=max(0,deadline-time.monotonic()))
            except queue.Empty:
                break
            if answer["id"] == self.control_id:
                if answer.get("ok"):
                    return answer["result"]
                raise RunFailure("Temporary fixture control rejected operation")
        raise RunFailure("Temporary fixture control did not acknowledge")

    def stop(self, *, crash=False):
        if self.process is None:
            return
        process = self.process
        interrupted = False

        def defer_interrupt(*_):
            nonlocal interrupted
            interrupted = True

        # Ctrl-C may arrive during release, wait, join or pipe closure. Defer
        # it until this owned child is reaped and both pipes are closed.
        previous_sigint = signal.signal(signal.SIGINT, defer_interrupt)
        try:
            forced = False
            try:
                if process.poll() is None:
                    if crash:
                        process.kill()
                    else:
                        if not self.live:
                            try:
                                self.control("release-all")
                            except (RunFailure, OSError):
                                check(self.report, None, "fixture release acknowledged", True, False)
                        process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    forced = True
                    process.kill()
                    process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                self.reader.join(timeout=2)
                for pipe in (process.stdin, process.stdout):
                    pipe.close()
                self.process = None
            check(self.report, None, "spawned process reaped", True, process.poll() is not None)
            check(self.report, None, "graceful server cleanup" if not crash else "explicit owned-process crash", False, forced)
            with socket.socket() as probe:
                check(self.report, None, "allocated port released", True, probe.connect_ex(("127.0.0.1", self.port)) != 0)
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
        if interrupted:
            raise KeyboardInterrupt


class RunFailure(Exception):
    """Only fixed, credential-free messages may reach artifacts/stdout."""


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def logical_counts(conn):
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in tables}


def counts_at(path):
    try:
        with closing(readonly(path)) as conn, conn:
            conn.execute("BEGIN")  # All counts belong to one read snapshot, including WAL.
            return logical_counts(conn)
    except sqlite3.OperationalError:
        raise RunFailure("Read-only SQLite snapshot unavailable; WAL sidecars must be readable or creatable. "
                         "Use --db with an accessible source; immutable mode would ignore concurrent writes.") from None


def backup_database(source, destination):
    with closing(readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def legacy_hashes(path):
    with closing(readonly(path)) as conn, conn:
        conn.execute("BEGIN")
        tables = logical_counts(conn)
        result = {}
        for name in LEGACY:
            if name not in tables:
                result[name] = None
                continue
            digest = hashlib.sha256()
            # Sort row encodings, so rowid/order changes do not imply data changes.
            rows = sorted(json.dumps(list(row), ensure_ascii=True, default=lambda b: b.hex())
                          for row in conn.execute(f'SELECT * FROM "{name}"'))
            for row in rows:
                digest.update((row + "\n").encode())
            result[name] = digest.hexdigest()
        return result


def free_port():
    # DIGGER_TEST_PORTS (e.g. 18800-18899) keeps checks inside an agreed range.
    low, _, high = os.environ.get("DIGGER_TEST_PORTS", "").strip().partition("-")
    candidates = range(int(low), int(high or low) + 1) if low else [0] * 100
    for wanted in candidates:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", wanted))
            except OSError:
                continue
            port = sock.getsockname()[1]
        if port != 8777:
            return port
    raise RuntimeError("No free port left in DIGGER_TEST_PORTS")


def main_env_file():
    try:
        result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT,
                                capture_output=True, text=True, check=True, timeout=5)
        return (ROOT / result.stdout.strip()).resolve().parent / ".env"
    except (OSError, subprocess.SubprocessError):
        raise RunFailure("Cannot locate the main checkout environment") from None


def read_env_file(path):
    """Read only provider keys; no interpolation, subprocesses or credential echo.

    Supports export, quoted values, and whitespace-introduced inline comments.
    Unknown settings/auth/library paths are intentionally never copied.
    """
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    except OSError:
        raise RunFailure("Cannot read live provider environment") from None
    values = {}
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() not in LIVE_KEYS:
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or value[end + 1:].strip().partition("#")[0].strip():
                raise RunFailure("Invalid live provider environment syntax")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def server_environment(home, *, live=False, env_file=None):
    # Do not inherit auth, configured library, proxies, fixture flags or .env.
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")
                   if key in os.environ}
    environment.update({"COSINE_DIGGER_HOME": str(home), "COSINE_DIGGER_DB": str(home / "taste.db"),
                        "COSINE_DIGGER_YTMUSIC_AUTH": str(home / "unused-auth.json"),
                        "COSINE_DIGGER_MUSIC_DIR": str(home / "generated-library"),
                        "PYTHONDONTWRITEBYTECODE": "1"})
    if live:
        values = read_env_file(env_file if env_file is not None else main_env_file())
        for key in LIVE_KEYS:
            environment[key] = os.environ.get(key, "").strip() or values.get(key, "")
        if any(not environment[key].strip() for key in LIVE_KEYS):
            raise RunFailure("Live provider keys are missing")
    else:
        environment.update({"COSINE_DIGGER_TEST_MODE": "1", "COSINE_DIGGER_FIXTURE": "slice-v1",
                            "COSINE_API_TOKEN": "slice-fixture-cosine-key",
                            "DISCOGS_TOKEN": "slice-fixture-discogs-key", "EXA_API_KEY": ""})
    return environment


def check(report, group, name, expected, observed, *, passed=None):
    if passed is None:
        passed = expected == observed
    item = {"name": name, "status": "PASS" if passed else "FAIL",
            "expected": expected, "observed": observed, "source": report["source"]}
    target = report["framework"] if group is None else report["groups"][group]
    target["checks"].append(item)
    return passed


@contextmanager
def sandbox_server(source, report, *, live=False, env_file=None):
    """Copy only SQLite, run the real CLI, then reap only our own process."""
    before = counts_at(source)
    report["framework"]["source_counts_before"] = before
    home = None
    try:
        with tempfile.TemporaryDirectory(prefix="slice-playthrough-") as temporary:
            home = Path(temporary) / "home"
            home.mkdir(mode=0o700)
            (home / "generated-library").mkdir()
            db = home / "taste.db"
            backup_database(source, db)
            check(report, None, "sqlite backup logical counts", before, counts_at(db))
            environment = server_environment(home, live=live, env_file=env_file)
            (home / "settings.json").write_text(json.dumps({"daily_size": 10, "weekly_size": 15}))
            executable = ROOT / ".venv" / "bin" / "cosine-digger"
            if not executable.is_file():
                raise RunFailure("Repository environment missing; run uv sync --frozen --extra dev")
            port = free_port()
            report["framework"].update({"port": port, "scheduler": False, "backup_method": "sqlite3.Connection.backup",
                                        "fixture_gate": not live, "stub_gate": not live})
            server = Sandbox(environment, port, temporary, report, live=live)
            try:
                server.start()
                check(report, None, "loopback health before browser", 200, 200)
                yield server
            finally:
                server.stop()
    finally:
        if home is not None:
            check(report, None, "temporary home removed", False, home.exists())
        after = counts_at(source)
        report["framework"]["source_counts_after"] = after
        check(report, None, "read-only source logical counts", before, after)
        if after != before:
            report["limitations"].append("Source logical counts changed concurrently; sandbox had a read-only source connection.")


def launch(playwright):
    executable = os.environ.get("SLICE_CHROMIUM_EXE")
    if executable:
        return playwright.chromium.launch(headless=True, executable_path=executable,
                                          args=["--single-process", "--no-zygote"])
    return playwright.chromium.launch(headless=True, channel="chrome")


def route_youtube_stub(page):
    # Deliberately inline: the standalone PEP 723 runner cannot import tests/.
    source = (ROOT / "tests" / "fixtures" / "yt_iframe_stub.js").read_text()
    page.route(API_SCRIPT, lambda route: route.fulfill(content_type="text/javascript", body=source))
    page.route(FIXTURE_ARTWORK, lambda route: route.fulfill(content_type="image/gif", body=PIXEL_GIF))


def boot(page, *, stub):
    page.wait_for_selector('#device[data-shell-state="ready"]',timeout=60000)
    if stub:
        page.wait_for_function("!!window.__sliceYTStub")
    page.wait_for_function("!!window.shell && !!window.selectors")
    page.evaluate("document.fonts.ready")


def route_shell(page, *, stub=True, offline=False):
    """Boot the mounted app once with an explicitly supplied harness clock."""
    page.route(re.compile(r"/static/slice\.js$"), lambda route: route.fulfill(
        content_type="text/javascript", body="""
          SDK_PRELOAD
          OFFLINE
          const device=document.getElementById('device');device.id='harness-device';
          const {boot}=await import('/static/slice.js?playthrough');device.id='device';
          const {createApi}=await import('/static/api.js');
          window.selectors=(await import('/static/state.js')).selectors;
          window.requestTrace=[];window.transportFetch=(...args)=>fetch(...args);
          const api=createApi({fetch:(path,init)=>{
            const entry={path:path.split('?')[0],method:init?.method || 'GET',at:performance.now()};
            requestTrace.push(entry);
            return transportFetch(path,init).then(response=>{entry.status=response.status;return response;},
              error=>{entry.failure=error.name;throw error;});
          }});
          CLOCK
          window.shell=await boot(OPTIONS);window.store=shell.store;
        """.replace("SDK_PRELOAD", """const sdk=document.createElement('script');sdk.src='https://www.youtube.com/iframe_api';
          await new Promise(resolve=>{sdk.onload=resolve;document.head.append(sdk);});""" if not offline else "")
        .replace("OFFLINE", "delete window.YT;" if offline else "")
        .replace("CLOCK", """
          const nativeClock=__sliceYTStub.clock;
          // A browser timeout starts async work without blocking the clock.
          // Keep the same behavior while advancing deterministic stub time.
          window.harnessClock={...nativeClock,
            setTimeout:(fn,ms)=>nativeClock.setTimeout(()=>{void fn();},ms),
            setInterval:(fn,ms)=>nativeClock.setInterval(()=>{void fn();},ms)};
        """ if stub else "")
        .replace("OPTIONS", "{api,clock:harnessClock}" if stub else "{api}")))


def picks(page, step_id=None):
    return page.evaluate("id => selectors.orderedPicks(store.getState(),id || store.getState().ui.route.stepId)", step_id)


def point_for(pick):
    return {"point_id": "track:" + pick["track_key"], "kind": "track",
            "name": pick["artist"] + " - " + pick["title"],
            **{key: pick[key] for key in ("track_key", "cosine_id", "artist", "title", "video_id")},
            "indexed": True, "source_step_id": pick["step_id"], "source_rank": pick["rank"]}


def row(page, pick, *, slim=False):
    return page.locator(f'#{"slim-list" if slim else "step"}-region .steplist-row[data-pick-id="{pick["id"]}"]')


def select_pick(page, pick):
    page.evaluate("p => store.actions.selectPoint(p.source_step_id,p)", point_for(pick))
    row(page, pick, slim=page.evaluate("store.getState().ui.route.view === 'map'")).focus()


def open_step(page, identity, view="list"):
    page.evaluate("x => store.actions.openRoute({page:'dig',digId:x.dig_id,stepId:x.step_id,view:x.view})", {**identity, "view": view})
    page.wait_for_function("id => store.getState().ui.route.stepId === id", arg=identity["step_id"])


def player(page):
    return page.evaluate("store.getState().player")


def advance(page, milliseconds):
    page.evaluate("ms => __sliceYTStub.advance(ms)", milliseconds)


def gesture(page, action, *args):
    # A real click, with normal autoplay policy. Removed before paint evidence.
    page.evaluate("""({action,args}) => {
      window.gestureDone=false;const b=document.createElement('button');b.id='harness-action';b.textContent='Run';
      b.style.cssText='position:absolute;right:8px;bottom:8px;z-index:1;width:60px;height:36px';
      b.onclick=async()=>{window.gestureResult=await store.actions[action](...args);window.gestureDone=true;b.remove();};
      document.getElementById('main').append(b);
    }""", {"action": action, "args": list(args)})
    page.locator("#harness-action").click()
    page.wait_for_function("gestureDone")
    return page.evaluate("gestureResult")


def play_pick(page, pick):
    gesture(page, "playPick", {"pickId": pick["id"]})
    if pick["video_id"] not in (None, "S9GONE00100", "S9REFUSE101", "Pso5YU_mntg"):
        page.wait_for_function("id => store.getState().player.current?.pickId === id && store.getState().player.state === 'playing'", arg=pick["id"])


def snapshot(page, identity):
    return page.evaluate("id => shell.api.request('GET', `/api/steps/${id}`)", identity["step_id"])


def start_fixture(page, server, *names, complete=True, further=False):
    points = server.control("points", list(names or ("seed-a",)))
    identity = page.evaluate("""async points => {
      await store.actions.openNewDig({chips:points});return store.actions.startDig();
    }""", points)
    if complete:
        wait_step(page, identity)
    if further:
        origin = picks(page, identity["step_id"])[0]
        identity = page.evaluate("p => store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:true})", point_for(origin))
        if complete:
            wait_step(page, identity)
    return identity


def record_db(report, group, db, query, params=()):
    with closing(readonly(db)) as conn:
        rows = [list(row) for row in conn.execute(query, params)]
    report["groups"][group].setdefault("db_traces", []).append({"query": query, "parameters": list(params), "rows": rows})
    return rows


def wait_fixture_steps(read_steps, pause, *, clock=None):
    """Reset the stall clock on status or settled facts, with a fixed bound."""
    clock = time.monotonic if clock is None else clock
    started = changed = clock()
    previous = None
    facts_seen = {}
    while True:
        response, steps = read_steps()
        statuses = {step["id"]: step["status"] for step in steps}
        facts = {step["id"]: step.get("facts_completed", 0) for step in steps}
        now = clock()
        if statuses != previous or any(count > facts_seen.get(step_id, 0) for step_id, count in facts.items()):
            changed = now
        previous = statuses
        facts_seen = {step_id: max(count, facts_seen.get(step_id, 0)) for step_id, count in facts.items()}
        elapsed = now - started
        details = (f"elapsed {elapsed:.1f}s; last statuses: {json.dumps(statuses, sort_keys=True)}; "
                   f"facts progress: {json.dumps(facts, sort_keys=True)}")
        if elapsed >= STEP_WAIT_SECONDS:
            raise RunFailure(f"Fixture steps exceeded wait bound {STEP_WAIT_SECONDS}s "
                             f"(stall window {STEP_STALL_SECONDS}s); {details}")
        if steps and all(status == "done" for status in statuses.values()):
            return response
        if any(status not in (None, "running", "done") for status in statuses.values()):
            raise RunFailure(f"Fixture step did not finish successfully; {details}")
        if now - changed >= STEP_STALL_SECONDS:
            raise RunFailure(f"Fixture steps stalled for {STEP_STALL_SECONDS}s; {details}")
        pause(100)


def wait_step(page, identity):
    def read_steps():
        steps = page.evaluate("""id => {
          const s=shell.store.getState(), picks=(s.picks.byStep[id]||[]).map(key=>s.picks.byId[key]);
          return [{id, status:s.steps.byId[id]?.status ?? null,
            facts_completed:picks.filter(pick=>pick.facts_state!=='loading').length}];
        }""", identity["step_id"])
        return None, steps

    wait_fixture_steps(read_steps, page.wait_for_timeout)


def wait_http_steps(page, path):
    # These jobs were created directly over HTTP and have no store follower.
    # evaluate awaits the request; a wait_for_function async predicate can
    # treat the returned Promise as truthy before its boolean result settles.
    facts = {}
    next_facts_poll = time.monotonic()

    def read_steps():
        nonlocal next_facts_poll
        response = page.evaluate("path => shell.api.request('GET', path)", path)
        steps = [response["snapshot"]["step"]] if "snapshot" in response else response["steps"]
        if any(step["status"] not in ("running", "done") for step in steps):
            raise RunFailure("HTTP fixture step did not finish successfully")
        if "snapshot" in response:
            snapshots = [response["snapshot"]]
        else:
            snapshots = []
            # Dig summaries omit facts. Read each running step at most once
            # every two seconds; retain its count between these small polls.
            if time.monotonic() >= next_facts_poll:
                for step in steps:
                    if step["status"] == "running":
                        snapshot = page.evaluate("path => shell.api.request('GET', path)",
                                                 "/api/steps/" + step["id"])["snapshot"]
                        snapshots.append(snapshot)
                next_facts_poll = time.monotonic() + 2
        for snapshot in snapshots:
            facts[snapshot["step"]["id"]] = sum(pick["facts_state"] != "loading" for pick in snapshot["picks"])
        return response, [step | {"facts_completed": facts.get(step["id"], 0)} for step in steps]

    return wait_fixture_steps(read_steps, page.wait_for_timeout)


def root_step(page):
    identity = page.evaluate("""async () => {
      const points = await shell.api.request('GET', '/api/search?q=Fixture%20Artist');
      const hits = points.items.filter(point => ['seed-a','seed-b','seed-c'].includes(point.cosine_id));
      if (hits.length !== 3) throw new Error('Three fixture seeds missing');
      hits.sort((a,b) => a.cosine_id.localeCompare(b.cosine_id)).forEach(shell.store.actions.addChip);
      return await shell.store.actions.startDig();
    }""")
    wait_step(page, identity)
    return identity


def state_group(page, server, directory, report):
    db = server.db
    root = root_step(page)
    original = page.evaluate("id => shell.api.request('GET', `/api/steps/${id}`)", root["step_id"])
    legacy_pick=picks(page,root["step_id"])[0]
    legacy_session=server.control("legacy-keep",legacy_pick)
    page.reload()
    boot(page,stub=True)
    legacy_before = legacy_hashes(db)
    selection = page.evaluate("""() => {
      const s=shell.store.getState(), pick=s.picks.byId[s.picks.byStep[s.ui.route.stepId][6]];
      const point={point_id:'track:'+pick.track_key,kind:'track',name:pick.artist+' - '+pick.title,
        track_key:pick.track_key,cosine_id:pick.cosine_id,artist:pick.artist,title:pick.title,
        video_id:pick.video_id,indexed:true,source_step_id:pick.step_id,source_rank:pick.rank};
      shell.store.actions.selectPoint(pick.step_id,point);
      shell.store.actions.saveScroll(pick.step_id,321);
      shell.store.actions.setView('map');
      return point;
    }""")
    source = picks(page, root["step_id"])
    play_pick(page, source[6])
    page.evaluate("store.actions.setView('list')")
    select_pick(page, source[0])
    selection = point_for(source[0])
    # Keep the selected label-bearing row visible for the keyboard return.
    # PT07 separately exercises restoration of a nonzero saved scroll.
    page.locator("#step-region").evaluate("el => {el.scrollTop=0}")
    scroll = page.locator("#step-region").evaluate("el=>el.scrollTop")
    page.wait_for_function("({id,scroll})=>store.getState().ui.scrollByStep[id]===scroll",arg={"id":root["step_id"],"scroll":scroll})
    queue = player(page)["queue"]
    before = page.evaluate("({player:shell.store.getState().player, history:history.length})")
    page.keyboard.press("l")
    page.wait_for_function("id => store.getState().ui.route.stepId !== id", arg=root["step_id"])
    child = page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    wait_step(page, child)
    after = page.evaluate("({player:shell.store.getState().player, history:history.length, state:shell.store.getState()})")
    check(report, "state", "move preserves source player/queue", before["player"], after["player"])
    check(report, "state", "step navigation pushes once", before["history"] + 1, after["history"])
    check(report, "state", "move parent persists", root["step_id"], after["state"]["steps"]["byId"][child["step_id"]]["parent_id"])
    check(report, "state", "roundtrip label move uses selected fact", ["label", 1],
          [after["state"]["steps"]["byId"][child["step_id"]]["move"],
           after["state"]["steps"]["byId"][child["step_id"]]["origins"][0]["source_rank"]])
    selected_child = picks(page, child["step_id"])[0]
    select_pick(page, selected_child)
    location = page.url
    page.keyboard.press("k")
    page.wait_for_function("key => store.getState().judgments.byTrack[key]?.verdict==='keep' && !store.getState().judgments.pending[key]", arg=source[6]["track_key"])
    verdict = record_db(report, "state", db, "SELECT verdict,step_id FROM judgments WHERE track_key=?", (source[6]["track_key"],))
    check(report, "state", "K targets playing source and preserves selection URL and queue",
          [[["keep", root["step_id"]]], location, queue], [verdict, page.url, player(page)["queue"]])
    screenshot(page, directory, report, "C09-1440", group="state")
    page.keyboard.press("z")
    page.wait_for_function("key => !store.getState().judgments.pending[key] && !store.getState().judgments.byTrack[key]?.verdict", arg=source[6]["track_key"])
    judgment_checks(page, selected_child, report)
    legacy_target={"trackKey":legacy_pick["track_key"],"stepId":root["step_id"],"verdict":"keep","mode":"toggle"}
    gesture(page,"judge",legacy_target)
    effective=page.evaluate("key=>store.getState().judgments.byTrack[key]",legacy_pick["track_key"])
    control=record_db(report,"state",db,"SELECT suppress_legacy FROM judgment_controls WHERE track_key=?",(legacy_pick["track_key"],))
    check(report,"state","toggle-off suppresses inherited legacy keep",[None,[[1]]],[effective["verdict"],control])
    gesture(page,"undoJudgment")
    effective=page.evaluate("key=>store.getState().judgments.byTrack[key]",legacy_pick["track_key"])
    check(report,"state","undo restores actual legacy provenance",["keep","legacy",None,legacy_session],
          [effective["verdict"],effective["source"],effective["step_id"],effective["legacy_session_id"]])
    page.evaluate("history.back()")
    page.wait_for_function("id => shell.store.getState().ui.route.stepId === id", arg=root["step_id"])
    returned = page.evaluate("shell.store.getState()")
    check(report, "state", "native Back restores selection and scroll", [selection["point_id"], scroll, "list"],
          [returned["ui"]["route"]["sel"], page.locator("#step-region").evaluate("el=>el.scrollTop"), returned["ui"]["route"]["view"]])
    current = page.evaluate("id => shell.api.request('GET', `/api/steps/${id}`)", root["step_id"])
    check(report, "state", "return keeps frozen snapshot hash", snapshot_hash(original), snapshot_hash(current))
    screenshot(page, directory, report, "C14-1440", group="state")
    page.evaluate("history.forward()")
    page.wait_for_function("id => shell.store.getState().ui.route.stepId === id", arg=child["step_id"])
    page.evaluate("__sliceYTStub.players[0].seekTo(42,true);__sliceYTStub.players[0].pauseVideo()")
    advance(page, 100)
    page.evaluate("shell.destroy()")  # production teardown persists queue/route
    page.reload()
    boot(page, stub=True)
    restored = page.evaluate("shell.store.getState()")
    check(report, "state", "reload restores route and paused source queue",
          [child["step_id"], "paused", before["player"]["queue"], False],
          [restored["ui"]["route"]["stepId"], restored["player"]["state"], restored["player"]["queue"], restored["player"]["gesture"]])
    events = []
    with closing(readonly(db)) as conn:
        child["job_id"] = after["state"]["steps"]["byId"][child["step_id"]]["current_job_id"]
        for identity in (root, child):
            trace = [{"seq": row[0], "event": row[1]} for row in conn.execute(
                "SELECT seq,event FROM slice_events WHERE job_id=? ORDER BY seq", (identity["job_id"],))]
            check(report, "state", "durable event cursor " + identity["step_id"],
                  list(range(1, len(trace) + 1)), [event["seq"] for event in trace])
            check(report, "state", "browser per-job cursor " + identity["step_id"], len(trace),
                  page.evaluate("id => shell.store.getState().steps.lastSeq[id]", identity["job_id"]))
            replay = page.evaluate(r"""async id => {
              const before=JSON.stringify(shell.store.getState());
              const raw=await (await fetch(`/api/jobs/${id}/events`)).text();
              const events=raw.split('\n\n').flatMap(block => {
                const name=block.split('\n').find(line => line.startsWith('event: '));
                const data=block.split('\n').find(line => line.startsWith('data: '));
                return name && data ? [{...JSON.parse(data.slice(6)),event:name.slice(7)}] : [];
              });
              return {dropped:events.filter(event => shell.store.actions.applyJobEvent(event) === false).length,
                unchanged:before === JSON.stringify(shell.store.getState())};
            }""", identity["job_id"])
            check(report, "state", "duplicate HTTP journal replay " + identity["step_id"],
                  {"dropped": len(trace), "unchanged": True}, replay)
            events.append({"job_id": identity["job_id"], "trace": trace})
        ranks = [row[0] for row in conn.execute("SELECT rank FROM step_picks WHERE step_id=? ORDER BY rank", (root["step_id"],))]
    check(report, "state", "three-seed rank prefix", list(range(1, 26)), ranks)
    check(report, "state", "GUI leaves legacy contents unchanged", legacy_before, legacy_hashes(db))
    report["groups"]["state"].update({"events": events, "ranks": ranks,
        "db_queries": ["SELECT seq,event FROM slice_events WHERE job_id=? ORDER BY seq",
                       "SELECT rank FROM step_picks WHERE step_id=? ORDER BY rank"],
        "snapshot_sha256": snapshot_hash(original), "legacy_sha256": legacy_hashes(db)})
    intent_checks(page, root, source, report)


def snapshot_hash(envelope):
    return hashlib.sha256(json.dumps(envelope["snapshot"], sort_keys=True).encode()).hexdigest()


def judgment_checks(page, pick, report):
    target = {"trackKey":pick["track_key"],"stepId":pick["step_id"],"mode":"toggle"}
    page.evaluate("""() => {
      window.pt06Actions=[];window.pt06Observer=e=>{
        const control=e.type==='click' && e.target.closest('.steplist-row [data-action="keep"],.steplist-row [data-action="pass"]');
        const x=e.type==='keydown' && e.key.toLowerCase()==='x';
        if(!control && !x)return;
        const s=store.getState();pt06Actions.push({
          action:control ? control.dataset.action==='keep' ? 'row Keep' : 'row Pass' : 'X',
          pick_id:control ? control.closest('.steplist-row').dataset.pickId : s.player.current?.pickId,
          trusted:e.isTrusted});
      };
      document.addEventListener('click',pt06Observer,true);
      document.addEventListener('keydown',pt06Observer,true);
    }""")
    select_pick(page, pick)

    def wait_verdict(verdict, track_key=pick["track_key"]):
        page.wait_for_function("""({key,verdict}) => !store.getState().judgments.pending[key] &&
          (store.getState().judgments.byTrack[key]?.verdict || null)===(verdict ?? null)""",
          arg={"key":track_key,"verdict":verdict})

    controls = row(page, pick)
    for verdict in ("keep", "pass"):
        for expected in (verdict, None):
            controls.locator(f'[data-action="{verdict}"]').click()
            wait_verdict(expected)
            check(report,"state",f"PT06 row {verdict.title()} button " + ("sets verdict" if expected else "toggles off"),
                  expected,page.evaluate("key=>store.getState().judgments.byTrack[key]?.verdict || null",pick["track_key"]))
    for verdict in ("keep", "pass"):
        controls.locator(f'[data-action="{verdict}"]').click()
        wait_verdict(verdict)
    page.locator('#toast-region [data-action="undo"]').click()
    wait_verdict("keep")
    check(report,"state","keep to pass undo restores keep","keep",page.evaluate("key=>store.getState().judgments.byTrack[key].verdict",pick["track_key"]))
    page.keyboard.press("z")
    wait_verdict(None)
    check(report,"state","undo restores none",None,page.evaluate("key=>store.getState().judgments.byTrack[key]?.verdict || null",pick["track_key"]))
    page.evaluate("""() => {window.savedTransport=transportFetch;transportFetch=(path,init)=>
      path.startsWith('/api/judgments/') ? Promise.resolve(new Response('{"detail":"Fixture unavailable"}',
        {status:503,headers:{'Content-Type':'application/json'}})) : savedTransport(path,init);}""")
    controls.locator('[data-action="keep"]').click()
    page.wait_for_function("""key => !store.getState().judgments.pending[key] &&
      document.getElementById('toast-region').textContent.includes("Couldn't save that keep:")""",arg=pick["track_key"])
    check(report,"state","failed save rolls back optimistic judgment",[None,True],
          [page.evaluate("key=>store.getState().judgments.byTrack[key]?.verdict || null",pick["track_key"]),
           "Couldn't save that keep:" in page.locator("#toast-region").inner_text()])
    page.evaluate("()=>{transportFetch=savedTransport;}")
    # Two intentions on the same track traverse the store's serialized writes.
    page.evaluate("""async target => {
      await Promise.all([store.actions.judge({...target,verdict:'keep'}),store.actions.judge({...target,verdict:'pass'})]);
    }""",target)
    check(report,"state","latest serialized judgment wins","pass",page.evaluate("key=>store.getState().judgments.byTrack[key].verdict",pick["track_key"]))
    gesture(page,"undoJudgment")
    gesture(page,"undoJudgment")
    # A second client changes the real DB without updating this client's
    # revision. Its next write must get 409, reconcile and retain navigation.
    before=page.evaluate("({url:location.href,queue:store.getState().player.queue})")
    page.evaluate("""async p=>{
      await shell.api.request('PUT',`/api/judgments/${encodeURIComponent(p.track_key)}`,
        {step_id:p.step_id,verdict:'keep',expected_revision:store.getState().judgments.revisions[p.track_key]||0});
    }""",pick)
    controls.locator('[data-action="pass"]').click()
    page.wait_for_function("""key => !store.getState().judgments.pending[key] &&
      document.getElementById('toast-region').textContent.includes('This judgment changed elsewhere; try again.')""",arg=pick["track_key"])
    check(report,"state","external revision race reconciles without stale write",["keep",True,before],
          [page.evaluate("key=>store.getState().judgments.byTrack[key].verdict",pick["track_key"]),
           "This judgment changed elsewhere; try again." in page.locator('#toast-region').text_content(),
           page.evaluate("({url:location.href,queue:store.getState().player.queue})")])
    playing = player(page)["current"]
    page.keyboard.press("x")
    wait_verdict("pass", playing["trackKey"])
    page.wait_for_function("id=>store.getState().player.current?.pickId!==id && store.getState().player.state==='playing'",
                           arg=playing["pickId"])
    check(report,"state","PT06 X passes playing source and skips it",
          ["pass",playing["stepId"],True],
          [page.evaluate("key=>store.getState().judgments.byTrack[key].verdict",playing["trackKey"]),
           page.evaluate("key=>store.getState().judgments.byTrack[key].step_id",playing["trackKey"]),
           player(page)["current"]["pickId"]!=playing["pickId"]])
    page.keyboard.press("z")
    wait_verdict(None, playing["trackKey"])
    # Return the queue cursor to the source track for the reload roundtrip.
    page.keyboard.press("[")
    page.wait_for_function("id=>store.getState().player.current?.pickId===id && store.getState().player.state==='playing'",
                           arg=playing["pickId"])
    report["groups"]["state"]["judgment_actions"] = page.evaluate("pt06Actions")
    page.evaluate("""() => {
      document.removeEventListener('click',pt06Observer,true);
      document.removeEventListener('keydown',pt06Observer,true);
    }""")


def intent_checks(page, root, source, report):
    open_step(page,root)
    before=player(page)["queue"]
    page.evaluate("""p => {
      window.realTransport=transportFetch;let count=0;
      transportFetch=async(path,init)=>{const response=await realTransport(path,init);
        if(path==='/api/steps' && ++count===1){window.heldReply=true;await new Promise(r=>window.releaseReply=r);}return response;};
      window.firstMove=store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:false});
    }""",point_for(source[8]))
    page.wait_for_function("window.heldReply")
    newer=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:false})",point_for(source[9]))
    older=page.evaluate("async()=>{releaseReply();const r=await firstMove;transportFetch=realTransport;return r;}")
    wait_step(page,newer)
    wait_step(page,older)
    check(report,"state","latest user intent wins late response/background completion",newer["step_id"],page.evaluate("store.getState().ui.route.stepId"))
    check(report,"state","concurrent moves preserve source queue",before,player(page)["queue"])
    report["groups"]["state"]["intent_trace"]={"first":older,"latest":newer,"viewed":page.evaluate("store.getState().ui.route.stepId")}


def geometry(page):
    return page.evaluate("""() => Object.fromEntries(['player','player-frame','player-region','main',
      'key-strip','trail-region','stepmap-region','slim-list-region'].map(id => {
        const r=document.getElementById(id).getBoundingClientRect();
        return [id,{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}];
      }))""")


def screenshot(page, directory, report, name, selector=None, *, group="shell", cdp=None):
    path = directory / (name + ".png")
    report["groups"][group].setdefault("captures", {})[name] = page.evaluate("""() => {
      const s=window.store?.getState(), id=s?.ui.route.stepId;
      return {step_id:id || null,step_number:s?.steps.byId[id]?.number ?? null};
    }""")
    if cdp is not None:
        # Playwright's screenshot resets emulation to its configured viewport.
        path.write_bytes(b64decode(cdp.send("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": False})["data"]))
    else:
        (page.locator(selector) if selector else page).screenshot(path=str(path))
    report["groups"][group].setdefault("paths", []).append(path.name)
    return path


def shell_group(page, directory, report):
    if not page.evaluate("shell.store.getState().ui.route.stepId"):
        root_step(page)
    page.evaluate("""() => {
      shell.store.actions.setView('list');
      window.reservedPlayer = document.getElementById('player');
      if (reservedPlayer.tagName !== 'IFRAME') {
        window.reservationStub = new YT.Player('player',{width:200,height:200});
      }
      window.reservedPlayer=document.getElementById('player');
    }""")
    page.wait_for_function("document.querySelector('#player').contentDocument?.readyState === 'complete'")
    measurements = {}
    for width, trail, right, height in [(1100, 56, 300, 260), (1280, 240, 320, 280), (1440, 260, 360, 300)]:
        page.set_viewport_size({"width": width, "height": 900})
        rects = geometry(page)
        measurements[str(width)] = rects
        check(report, "shell", f"desktop bands/grid {width}", [200, 200, 204, 204, 224, 36, trail, right, height],
              [rects["player"]["width"], rects["player"]["height"], rects["player-frame"]["width"], rects["player-frame"]["height"],
               rects["player-region"]["height"], rects["key-strip"]["height"], rects["trail-region"]["width"],
               rects["stepmap-region"]["width"], rects["stepmap-region"]["height"]])
        check(report, "shell", f"stage/strip clear at {width}", True,
              rects["main"]["bottom"] < rects["key-strip"]["y"] and rects["key-strip"]["bottom"] <= 900)
        screenshot(page, directory, report, f"shell-{width}")
    before = page.evaluate("JSON.stringify({queue:shell.store.getState().player.queue,selection:shell.store.getState().ui.selectionByStep})")
    history = page.evaluate("history.length")
    page.locator("#view-map").click()
    check(report, "shell", "List/Map preserves history selection queue", [history, before],
          [page.evaluate("history.length"), page.evaluate("JSON.stringify({queue:shell.store.getState().player.queue,selection:shell.store.getState().ui.selectionByStep})")])
    check(report, "shell", "territory slim list width", True, geometry(page)["slim-list-region"]["width"] >= 240)
    page.locator("#view-list").click()
    for mode in ("feeds", "setup", "dig"):
        page.locator("#mode-" + mode).click()
        page.wait_for_function("mode => shell.store.getState().ui.route.page === mode", arg=mode)
        check(report, "shell", "persistent reservation on " + mode, True,
              page.evaluate("reservedPlayer === document.getElementById('player')"))
    check(report, "shell", "obsolete faceplate widgets removed", 0,
          page.locator("#hud,.lcd__lights,.marquee,#lcd-clock,#lcd-daily,#lcd-weekly").count())
    page.set_viewport_size({"width": 1000, "height": 900})
    check(report, "shell", "unsupported-width notice", "cosine-digger is built for desktop windows 1100 px and wider",
          page.locator("#narrow-notice").inner_text())
    screenshot(page, directory, report, "shell-1000-notice")
    page.set_viewport_size({"width":1440,"height":900})
    original_metrics=page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
    cdp = page.context.new_cdp_session(page)
    try:
        # Chrome's settings WebUI is unavailable in installed headless Chrome.
        # Use the same explicit layout surrogate in both browser modes.
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": 720, "height": 450,
                 "deviceScaleFactor": original_metrics["dpr"] * 2,
                 "mobile": False, "screenWidth": 1440, "screenHeight": 900})
        method="CDP CSS viewport/DPR surrogate; not true browser zoom"
        rects = geometry(page)
        metrics = page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
        expected={"width":original_metrics["width"]/2,"height":original_metrics["height"]/2,"dpr":original_metrics["dpr"]*2}
        check(report, "shell", "200 percent layout surrogate", expected, metrics)
        check(report, "shell", "zoom layout video/strip visible", True,
              rects["player"]["width"] >= 200 and rects["player"]["height"] >= 200 and
              rects["player"]["right"] <= 720 and rects["key-strip"]["bottom"] <= 450)
        report["groups"]["shell"]["zoom"] = {"method": method,"before":original_metrics,
                                              "metrics": metrics, "geometry": rects}
        screenshot(page, directory, report, "shell-zoom-layout-surrogate", cdp=cdp)
        captured_metrics = page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
        check(report, "shell", "surrogate capture preserves measured viewport", metrics, captured_metrics)
        report["groups"]["shell"]["zoom"]["capture_metrics"] = captured_metrics
    finally:
        cdp.send("Emulation.clearDeviceMetricsOverride")
        cdp.detach()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("device.dataset.reducedMotion === 'true'")
    baseline = page.locator("#player-frame").screenshot()
    skin_on = page.locator("#main").screenshot()
    page.locator("#crt-toggle").click()
    check(report, "shell", "CRT leaves video paint untouched", True, baseline == page.locator("#player-frame").screenshot())
    check(report, "shell", "CRT affects stage paint", True, skin_on != page.locator("#main").screenshot())
    page.locator("#crt-toggle").click()
    overlay_checks(page,directory,report)
    for root in ("moves-region", "sheet-region", "trail-region", "step-region", "stepmap-region"):
        probe = page.evaluate("""async id => {
          const {h,clampPopover}=await import('/static/dom.js');
          const root=document.getElementById(id), wasHidden=root.hidden; root.hidden=false;
          const popup=h('div',{style:{width:'1200px',height:'900px',background:'var(--panel)',zIndex:'99999'}},
            h('button',{type:'button',style:{margin:'8px',minHeight:'36px'}},'Focus fixture'));
          root.append(popup); window.popupProbe={popup,root,wasHidden};
          const video=document.getElementById('player').getBoundingClientRect();
          clampPopover(popup,{left:video.left,top:video.top,bottom:video.bottom});
          const r=popup.getBoundingClientRect(), stage=main.getBoundingClientRect();
          return {clear:r.top >= stage.top+6 && r.bottom <= stage.bottom-6 && r.right <= stage.right-6,
            videoTop:document.elementFromPoint(video.x+100,video.y+100)?.id === 'player',
            rect:{x:r.x,y:r.y,right:r.right,bottom:r.bottom}};
        }""", root)
        check(report, "shell", "clamp utility stress fixture " + root, [True, True], [probe["clear"], probe["videoTop"]])
        check(report, "shell", "overlay video paint " + root, True, baseline == page.locator("#player-frame").screenshot())
        page.keyboard.press("Tab")
        page.evaluate("popupProbe.popup.querySelector('button').focus()")
        focus = page.evaluate("""() => {const node=document.activeElement,s=getComputedStyle(node),r=node.getBoundingClientRect();
          return {outline:s.outlineWidth,style:s.outlineStyle,color:s.outlineColor,width:r.width,height:r.height};}""")
        check(report, "shell", "focus ring and target " + root, True,
              focus["outline"] == "3px" and focus["style"] == "solid" and focus["color"] == "rgb(255, 225, 77)"
              and focus["width"] >= 24 and focus["height"] >= 24)
        measurements[root] = {"overlay": probe["rect"], "focus": focus}
        page.evaluate("popupProbe.popup.remove(); popupProbe.root.hidden=popupProbe.wasHidden")
    motion = page.evaluate("""() => ({roll:getComputedStyle(document.querySelector('.crt'),'::after').animationName,
      active:document.getAnimations().filter(a => a.playState==='running').length})""")
    check(report, "shell", "reduced motion including CRT pseudo-element", {"roll": "none", "active": 0}, motion)
    screenshot(page, directory, report, "shell-reduced-motion")
    report["groups"]["shell"].update({"geometry": measurements, "video_source": "STUB API, production mounted S14 player"})


def overlay_checks(page,directory,report):
    """Open real components over an actually playing, long-title fixture."""
    source=picks(page)
    play_pick(page,source[0])
    page.evaluate("window.overlayFrame=document.getElementById('player')")
    measurements={}
    for width in (1100,1280,1440):
        page.set_viewport_size({'width':width,'height':900})
        baseline=page.locator('#player-frame').screenshot()
        def probe(name,selector):
            popup=page.locator(selector)
            popup.wait_for(state='visible')
            measured=popup.evaluate("""el=>{
              const r=el.getBoundingClientRect(),stage=(el.matches('.toast')?document.getElementById('key-strip'):main).getBoundingClientRect(),video=document.getElementById('player').getBoundingClientRect();
              return {bounds:{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom},
                inside:r.x>=stage.x&&r.y>=stage.y&&r.right<=stage.right&&r.bottom<=stage.bottom,
                frameSame:overlayFrame===document.getElementById('player'),
                videoClear:[1,100,199].every(x=>[1,100,199].every(y=>document.elementFromPoint(video.x+x,video.y+y)?.id==='player'))};
            }""")
            check(report,'shell',f'actual {name} bounds/frame/paint {width}',[True,True,True,True],
                  [measured['inside'],measured['frameSame'],measured['videoClear'],baseline==page.locator('#player-frame').screenshot()])
            measurements[f'{name}-{width}']=measured
            screenshot(page,directory,report,f'C08-{name}-{width}')
        select_pick(page,source[0])
        page.keyboard.press('Enter')
        probe('moves','.moves-popover')
        page.keyboard.press('c')
        probe('credits','.moves-popover')
        page.keyboard.press('Escape');page.keyboard.press('Escape')
        page.keyboard.press('?')
        probe('keys','#keys-sheet')
        page.keyboard.press('Escape')
        page.locator('#trail-region .trail-row').first.focus()
        if width==1100:
            page.wait_for_function("store.getState().ui.trailRail")
            probe('expanded-rail','#trail-region .trail-panel')
        page.get_by_role('button',name='Switch dig',exact=True).click()
        probe('switcher','#trail-switcher')
        page.keyboard.press('Escape')
        page.evaluate("store.actions.showToast('S-T-ROOT')")
        probe('toast','#toast-region .toast')
        canvas=page.locator('#stepmap-canvas')
        canvas.hover(position=map_position(page,source[0]['layout']))
        probe('tooltip','.stepmap-tooltip')
        page.mouse.move(0,0)
    title=row(page,source[0]).locator('.steplist-title')
    check(report,'shell','long provider title is safely truncated with full name retained',True,
          title.evaluate("el=>el.scrollWidth>el.clientWidth&&getComputedStyle(el).textOverflow==='ellipsis'&&el.title===el.textContent"))
    report['groups']['shell']['actual_overlays']=measurements


def player_group(page, server, directory, report):
    group="player"
    root=start_fixture(page,server)
    source=picks(page,root["step_id"])
    chosen=source[6]
    select_pick(page,chosen)
    page.keyboard.press("Space")
    page.wait_for_function("store.getState().player.state==='playing'")
    check(report,group,"first playback is a real gesture",True,page.evaluate("__sliceYTStub.calls.find(c=>c.method==='playVideo').gesture"))
    check(report,group,"selected suffix queue",[p["id"] for p in source[6:]],player(page)["queue"]["pickIds"])
    select_pick(page,source[8])
    check(report,group,"selection does not follow playback",[chosen["id"],"track:"+source[8]["track_key"]],
          [player(page)["current"]["pickId"],page.evaluate("store.getState().ui.route.sel")])
    advance(page,5000)
    page.get_by_role("button",name="Previous track",exact=True).click()
    check(report,group,"previous after 3s restarts",0,player(page)["position"])
    for name,position in [("Forward 60 seconds",60),("Back 15 seconds",45),("Forward 15 seconds",60)]:
        page.get_by_role("button",name=name,exact=True).click()
        check(report,group,"transport "+name,position,player(page)["position"])
    page.get_by_role("button",name="Pause",exact=True).click()
    page.wait_for_function("store.getState().player.state==='paused'")
    check(report,group,"paused LCD glyph",True,"❚❚" in page.locator("#lcd-title").inner_text())
    screenshot(page,directory,report,"SS12-1440",group=group)
    page.get_by_role("button",name="Play",exact=True).click()
    page.wait_for_function("store.getState().player.state==='playing'")
    page.get_by_role("button",name="Next track",exact=True).click()
    check(report,group,"next advances suffix",source[7]["id"],player(page)["current"]["pickId"])
    iframe=page.evaluate("window.originalFrame=document.getElementById('player');true")
    before=player(page)["queue"]
    for mode in ("feeds","setup","dig"):
        page.locator("#mode-"+mode).click()
        page.wait_for_function("mode=>store.getState().ui.route.page===mode",arg=mode)
        check(report,group,"single playing frame on "+mode,[iframe,before,1],
              [page.evaluate("originalFrame===document.getElementById('player')"),player(page)["queue"],page.locator("#player-frame iframe").count()])
    open_step(page,root)
    for video,code,wording in [("S9GONE00100",100,"this video isn't available"),
                               ("S9REFUSE101",101,"the owner turned off embedding"),
                               ("Pso5YU_mntg",150,"the owner turned off embedding"),
                               (None,None,"No video for this track")]:
        refused=next(p for p in source if p["video_id"]==video)
        play_pick(page,refused)
        page.wait_for_function("store.getState().player.state==='refused'")
        at=page.evaluate("__sliceYTStub.clock.now()")
        note=page.locator("#lcd-note").inner_text()
        check(report,group,f"refusal {code} row and LCD",True,
              wording in note and "Skipping in 2 s" in note and
              ("can't play here" if code in (101,150) else "isn't available" if code==100 else "no video") in row(page,refused).inner_text())
        screenshot(page,directory,report,f"SS13-{code}-1440",group=group)
        advance(page,1999)
        check(report,group,f"refusal {code} not before 2000ms",refused["id"],player(page)["current"]["pickId"])
        loads=page.evaluate("__sliceYTStub.calls.filter(c=>c.method==='loadVideoById').length")
        advance(page,1)
        page.wait_for_function("id=>store.getState().player.current.pickId!==id",arg=refused["id"])
        calls=page.evaluate("__sliceYTStub.calls.filter(c=>c.method==='loadVideoById')")
        check(report,group,f"refusal {code} one skip at 2000ms",[loads+1,at+2000],[len(calls),calls[-1]["at"]])
        advance(page,10)
        check(report,group,f"refusal {code} no double skip",loads+1,page.evaluate("__sliceYTStub.calls.filter(c=>c.method==='loadVideoById').length"))
    refused=next(p for p in source if p["video_id"]=="S9REFUSE101")
    play_pick(page,refused)
    advance(page,800)
    gesture(page,"next")
    current=player(page)["current"]
    advance(page,1300)
    check(report,group,"Next cancels refusal timer",current,player(page)["current"])
    page.evaluate("""id=>{__sliceYTStub.players[0].emit('onError',100);store.actions.playPick({pickId:id});}""",chosen["id"])
    advance(page,2100)
    check(report,group,"late SDK error cannot overrule newer play",chosen["id"],player(page)["current"]["pickId"])
    # Blocked first play is driven by the SDK, not a synthetic playerEvent.
    page.evaluate("""() => {window.nativePlay=__sliceYTStub.players[0].playVideo;
      __sliceYTStub.players[0].playVideo=function(){this.setState(-1);};}""")
    play_pick_id=source[10]["id"]
    gesture(page,"playPick",{"pickId":play_pick_id})
    advance(page,1500)
    check(report,group,"blocked gesture hint",True,"Press ▶ to start playback." in page.locator("#lcd-note").inner_text())
    page.evaluate("()=>{__sliceYTStub.players[0].playVideo=nativePlay;}")
    # Fresh document resets elapsed time/refusals; two actual occurrences share heard.
    page.evaluate("shell.destroy()")
    page.reload()
    boot(page,stub=True)
    second=start_fixture(page,server,"seed-b")
    shared=source[0]
    other=next(p for p in picks(page,second["step_id"]) if p["track_key"]==shared["track_key"])
    play_pick(page,shared)
    advance(page,10000)
    gesture(page,"seek",60)
    page.evaluate("__sliceYTStub.players[0].pauseVideo()")
    advance(page,20000)
    elapsed=player(page)["heardSeconds"][shared["track_key"]]
    check(report,group,"seek/pause never count heard",10,elapsed,passed=math.isclose(elapsed,10,abs_tol=1e-8))
    page.evaluate("__sliceYTStub.players[0].setState(3)")
    advance(page,20000)
    elapsed=player(page)["heardSeconds"][shared["track_key"]]
    check(report,group,"buffer never counts heard",10,elapsed,passed=math.isclose(elapsed,10,abs_tol=1e-8))
    play_pick(page,other)
    advance(page,19900)
    check(report,group,"29.9s sends no heard POST",0,page.evaluate("requestTrace.filter(r=>r.method==='POST'&&r.path==='/api/heard').length"))
    advance(page,100)
    page.wait_for_function("key=>!!store.getState().heard.byTrack[key]",arg=shared["track_key"])
    advance(page,10000)
    marks=page.evaluate("ids=>ids.map(id=>selectors.marks(store.getState(),id).heard)",[shared["id"],other["id"]])
    check(report,group,"30s global heard provenance and single POST",[True,second["step_id"],1],
          [marks[0]==marks[1],marks[0]["first_step_id"],page.evaluate("requestTrace.filter(r=>r.method==='POST'&&r.path==='/api/heard').length")])
    record_db(report,group,server.db,"SELECT track_key,first_step_id FROM heard")
    screenshot(page,directory,report,"SS42-1440",group=group)
    select_pick(page,other)
    page.keyboard.press("Shift+p")
    check(report,group,"Shift P leaves current source and sets next queue",second["step_id"],player(page)["nextQueue"]["stepId"])
    page.evaluate("__sliceYTStub.players[0].setState(0)")
    page.wait_for_function("store.getState().player.nextQueue===null")
    check(report,group,"Shift P switches at end from the top",1,player(page)["queue"]["startRank"])
    play_pick(page,picks(page,second["step_id"])[-1])
    page.evaluate("__sliceYTStub.players[0].setState(0)")
    page.wait_for_function("store.getState().player.phase==='ended'")
    check(report,group,"source queue ends with explicit where next",["idle",3],[player(page)["state"],page.locator('[data-action="where-next"]').count()])
    screenshot(page,directory,report,"C12-1440",group=group)
    play_pick(page,other)
    page.evaluate("""() => {Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));}""")
    check(report,group,"hidden document pauses visible-player session",[2,"paused"],[page.evaluate("__sliceYTStub.players[0].getPlayerState()"),player(page)["state"]])
    page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false})")
    # Offline API failure: a second mounted document, still the same fixture app.
    report["groups"][group]["stub_calls_before_offline"]=page.evaluate("__sliceYTStub.calls")
    page.evaluate("shell.destroy()")
    route_shell(page,stub=False,offline=True)
    page.route(re.compile(r"^https://www\.youtube\.com/iframe_api$"),lambda route:route.abort())
    page.goto(server.url+"/#/dig/new")
    page.reload()  # Changing only the hash is a same-document navigation.
    boot(page,stub=False)
    page.wait_for_function("document.getElementById('player').textContent==='NO SIGNAL'")
    check(report,group,"SDK offline leaves fallback links and NO SIGNAL",True,"The player can't reach YouTube. Track links still work." in page.locator("#lcd-note").inner_text())
    screenshot(page,directory,report,"SS14-1440",group=group)


def search(page, query):
    page.locator("#seed-input").fill(query)
    advance(page,250)
    page.wait_for_function("!store.getState().ui.search.pending.library && !store.getState().ui.search.pending.cosine")


def seeds_list_group(page, server, directory, report):
    group="seeds-list"
    server.control("no-library")
    page.evaluate("store.actions.openNewDig()")
    page.reload()
    boot(page,stub=True)
    check(report,group,"first-run fixture has no saved digs",0,
          record_db(report,group,server.db,"SELECT COUNT(*) FROM digs")[0][0])
    # The first screen is the lede, the box and the link line; the library line waits for results.
    check(report,group,"fresh seed screen and no Today Daily",True,
          "Start from a track you love." in page.locator("#new-dig-region").inner_text() and
          "Scan a music folder in Setup" not in page.locator("#new-dig-region").inner_text() and
          "Today's Daily" not in page.locator("#new-dig-region").inner_text() and page.locator("#view-map").is_disabled())
    screenshot(page,directory,report,"SS01-1440",group=group)
    page.locator("#seed-input").press("Enter")
    check(report,group,"empty Enter hint",True,"Type an artist or a track first." in page.locator("#new-dig-region").inner_text())
    server.control("fixture-library")
    page.reload()
    boot(page,stub=True)
    page.evaluate("""() => {window.nativeTransport=transportFetch;transportFetch=async(...args)=>{
      const response=await nativeTransport(...args),u=new URL(args[0],location.href);
      if(u.pathname==='/api/search'&&u.searchParams.get('source')==='cosine'&&u.searchParams.get('q')==='Fixture Artist A') {
        window.oldSearchHeld=true;await new Promise(r=>window.releaseOldSearch=r);
        const read=response.text.bind(response);response.text=async()=>{const body=await read();window.oldSearchConsumed=true;return body;};}
      return response;};}""")
    page.locator("#seed-input").fill("Fixture Artist A")
    advance(page,249)
    check(report,group,"search debounce before 250ms",0,page.evaluate("requestTrace.filter(r=>r.path==='/api/search').length"))
    advance(page,1)
    page.wait_for_function("window.oldSearchHeld && store.getState().ui.search.library.length>0")
    check(report,group,"split search displays library while cosine waits",True,
          "Unindexed File" in page.locator("#seed-results").inner_text() and "~ Searching cosine.club…" in page.locator("#seed-results").inner_text())
    screenshot(page,directory,report,"SS03-1440",group=group)
    search(page,"Fixture Artist B")
    page.evaluate("releaseOldSearch()")
    page.wait_for_function("window.oldSearchConsumed")
    check(report,group,"stale search cannot replace newer query",["Fixture Artist B",True],
          [page.evaluate("store.getState().ui.search.query"),"Seed B" in page.locator("#seed-results").inner_text() and "Seed A" not in page.locator("#seed-results").inner_text()])
    page.evaluate("transportFetch=nativeTransport")
    search(page,"zzqxv")
    check(report,group,"no match names query",True,"No match on cosine.club for 'zzqxv'. Try artist and title, or paste a link." in page.locator("#seed-results").inner_text())
    search(page,"Seed A")
    page.locator("#seed-input").press("Enter")
    duplicate=page.evaluate("store.getState().ui.search.chips[0]")
    page.evaluate("p=>store.actions.addChip(p)",duplicate)
    check(report,group,"duplicate chip is refused",[1,"Already a seed."],
          [page.evaluate("store.getState().ui.search.chips.length"),page.locator(".seedbox-hint").inner_text()])
    for query in ("Seed B","Seed C"):
        search(page,query)
        page.locator("#seed-input").press("Enter")
    check(report,group,"three chips and track start wording",[3,"Dig from 3 tracks ▸"],
          [page.locator(".seedbox-chip").count(),page.locator('[data-action="start-dig"]').text_content()])
    # Temporarily fill the chip budget with genuine provider points, then restore three.
    saved=page.evaluate("store.getState().ui.search.chips")
    extras=server.control("points",["empty","no-facts","no-shared","rate-limited","retry-once","provider-failure","partial-failure","discogs-down"])
    page.evaluate("points=>points.forEach(store.actions.addChip)",extras)
    check(report,group,"ten chip cap and eleventh hint",[10,"Up to 10 seeds per dig."],
          [page.evaluate("store.getState().ui.search.chips.length"),page.locator(".seedbox-hint").inner_text()])
    page.evaluate("points=>store.actions.openNewDig({chips:points})",saved)
    server.control("block","facts")
    page.locator("#seed-input").press("Enter")
    page.wait_for_function("store.getState().ui.route.stepId!==null")
    root=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    check(report,group,"facts gate entered",True,server.control("entered","facts"))
    page.wait_for_function("id=>selectors.isPicksComplete(store.getState(),id)",arg=root["step_id"])
    source=picks(page,root["step_id"])
    select_pick(page,source[3])
    trail=page.locator(f'#trail-region [data-type="step"][data-step-id="{root["step_id"]}"] .trail-status')
    check(report,group,"O1 count replaces tilde before facts finish",["running","25",True],
          [page.evaluate("id=>store.getState().steps.byId[id].status",root["step_id"]),trail.inner_text(),
           "Loading facts from Discogs" in page.locator("#inspector-region").inner_text()])
    check(report,group,"O1 generation header ends while facts load",0,page.locator(".steplist-progress:not([hidden])").count())
    play_pick(page,source[-1])
    page.evaluate("__sliceYTStub.players[0].setState(0)")
    page.wait_for_function("store.getState().player.phase==='ended'")
    check(report,group,"O1 where-next does not wait for facts",[True,False],
          ["Where next:" in page.locator("#lcd-note").inner_text(),"Waiting for more tracks" in page.locator("#lcd-note").inner_text()])
    screenshot(page,directory,report,"O1-delayed-facts-1440",group=group)
    page.reload()
    boot(page,stub=True)
    check(report,group,"reload while facts drain keeps count and unique ranks",["25",25],
          [trail.inner_text(),len({p["rank"] for p in picks(page,root["step_id"])})])
    server.control("release","facts")
    wait_step(page,root)
    origins=snapshot(page,root)["snapshot"]["step"]["origins"]
    check(report,group,"three origins persisted in chip order",["seed-a","seed-b","seed-c"],[p["cosine_id"] for p in origins])
    check(report,group,"multi-seed why is present",True,"close to" in page.locator(".steplist-why").first.inner_text())
    screenshot(page,directory,report,"C01-1440",group=group)
    record_db(report,group,server.db,"SELECT rank,route FROM step_picks WHERE step_id=? ORDER BY rank",(root["step_id"],))
    stream_checks(page,server,root,report,directory)
    page.evaluate("store.actions.openNewDig()")
    check(report,group,"Recent digs and O3 no Daily card",[True,False],
          [page.locator(".seedbox-recent-link").count()>0,"Today's Daily" in page.locator("#new-dig-region").inner_text()])
    for link in ("https://youtu.be/S9SEEDA0001","https://www.discogs.com/label/501","https://cosine.club/track/seed-b"):
        page.locator("#seed-input").fill(link)
        page.locator("#seed-input").press("Enter")
    check(report,group,"mixed links become chips",3,page.evaluate("store.getState().ui.search.chips.length"))
    page.locator("#seed-input").press("Backspace")
    check(report,group,"Backspace drops last chip",2,page.evaluate("store.getState().ui.search.chips.length"))
    screenshot(page,directory,report,"SS02-1440",group=group)


def stream_checks(page,server,root,report,directory):
    server.control("clear-http-cache")
    server.control("block","hop1")
    first=picks(page,root["step_id"])[0]
    branch=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:true})",point_for(first))
    check(report,"seeds-list","hop1 gate entered",True,server.control("entered","hop1"))
    page.wait_for_function("id=>selectors.orderedPicks(store.getState(),id).length===25",arg=branch["step_id"])
    samples=[]
    for _ in range(3):
        rows=record_db(report,"seeds-list",server.db,"SELECT rank,track_key FROM step_picks WHERE step_id=? ORDER BY rank",(branch["step_id"],))
        samples.append({"at":time.monotonic(),"ranks":[r[0] for r in rows],"keys":[r[1] for r in rows]})
        page.wait_for_timeout(250)
    check(report,"seeds-list","near persisted before hop1 gate opens",[list(range(1,26))]*3,[s["ranks"] for s in samples])
    play_pick(page,picks(page,branch["step_id"])[-1])
    page.evaluate("__sliceYTStub.players[0].setState(0)")
    page.wait_for_function("store.getState().player.phase==='waiting'")
    check(report,"seeds-list","streaming queue honestly waits for more picks",True,"Waiting for more tracks" in page.locator("#lcd-note").inner_text())
    open_step(page,root)
    viewed=page.evaluate("store.getState().ui.route")
    server.control("release","hop1")
    wait_step(page,branch)
    check(report,"seeds-list","background finish never takes view",viewed,page.evaluate("store.getState().ui.route"))
    final=picks(page,branch["step_id"])
    check(report,"seeds-list","further append preserves prefix without duplicates",[samples[-1]["keys"],len(final),True],
          [[p["track_key"] for p in final[:25]],len({p["track_key"] for p in final}),[p["rank"] for p in final]==list(range(1,len(final)+1))])
    report["groups"]["seeds-list"]["prefix_samples_250ms"]=samples
    open_step(page,branch)
    check(report,"seeds-list","further divider",True,"One step further · sounds like the labels, artists and people of the picks above" in page.locator("#step-region").text_content())
    screenshot(page,directory,report,"PT02-further-1440",group="seeds-list")


def trail_group(page,server,directory,report):
    group="trail"
    root=start_fixture(page,server)
    original=snapshot(page,root)
    source=picks(page,root["step_id"])
    select_pick(page,source[6])
    page.locator("#step-region").evaluate("el=>{el.scrollTop=240}")
    scroll=page.locator("#step-region").evaluate("el=>el.scrollTop")
    play_pick(page,source[6])
    page.evaluate("__sliceYTStub.players[0].seekTo(33,true);__sliceYTStub.players[0].pauseVideo()")
    advance(page,100)
    queue=player(page)["queue"]
    child=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:false})",point_for(source[7]))
    wait_step(page,child)
    count_before=record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0]
    trail=page.locator(f'#trail-region [data-action="step"][data-step-id="{root["step_id"]}"]')
    trail.click()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    check(report,group,"trail return restores selected pick and scroll",["track:"+source[6]["track_key"],scroll],
          [page.evaluate("store.getState().ui.route.sel"),page.locator("#step-region").evaluate("el=>el.scrollTop")])
    check(report,group,"return says as you left it",True,"as you left it" in page.locator(".steplist-meta").inner_text())
    page.go_back()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=child["step_id"])
    page.go_forward()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    open_step(page,child)
    page.locator("#step-region").focus()
    page.keyboard.press("u")
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    page.keyboard.press("u")
    check(report,group,"root U explains boundary",True,"Step 1 is where this dig starts." in page.locator("#toast-region").inner_text())
    open_step(page,child)
    shared=next(p for p in picks(page,child["step_id"]) if p["track_key"]==source[0]["track_key"])
    select_pick(page,shared)
    also=page.locator('#inspector-region [data-action="open-step"]').filter(has_text="Step 1")
    also.first.click()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    check(report,group,"also-in link selects same track", "track:"+shared["track_key"],page.evaluate("store.getState().ui.route.sel"))
    page.locator('[data-action="kept"]').click()
    check(report,group,"empty kept list explains K",True,"Press K while a track plays to keep it" in page.locator(".trail-kept-list").inner_text())
    for pick in (source[6],source[8]):
        gesture(page,"judge",{"trackKey":pick["track_key"],"stepId":pick["step_id"],"verdict":"keep","mode":"toggle"})
    check(report,group,"kept list shows two provenanced keeps",2,page.locator(".trail-kept-row").count())
    page.locator(".trail-kept-row").first.click()
    check(report,group,"kept navigation uses track token",True,page.evaluate("store.getState().ui.route.sel.startsWith('track:')"))
    screenshot(page,directory,report,"SS40-1440",group=group)
    second=start_fixture(page,server,"seed-b")
    check(report,group,"other dig names playing source and no local playing marker",[True,0],
          ["from " in page.locator("#lcd-from").inner_text(),page.locator(".trail-playing:not([hidden])").count()])
    page.get_by_role("button",name="Switch dig",exact=True).click()
    check(report,group,"switcher new dig plus recent entries",True,"+ New dig" in page.locator("#trail-switcher").inner_text() and page.locator(".trail-switcher-row").count()>=2)
    screenshot(page,directory,report,"SS41-1440",group=group)
    page.keyboard.press("Escape")
    page.locator("#step-region").focus()
    page.keyboard.press("g")
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    check(report,group,"G navigates to source while preserving queue",["track:"+source[6]["track_key"],queue],
          [page.evaluate("store.getState().ui.route.sel"),player(page)["queue"]])
    check(report,group,"frozen original after returns and live overlays",snapshot_hash(original),snapshot_hash(snapshot(page,root)))
    # Only the deliberate new dig above created a step.
    check(report,group,"return/also-in/kept/switcher never start moves",count_before+1,record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0])
    screenshot(page,directory,report,"SS20-1440",group=group)
    page.reload()
    boot(page,stub=True)
    check(report,group,"reload returns paused source at SDK seek position",[root["step_id"],"paused",33,queue],
          [page.evaluate("store.getState().ui.route.stepId"),player(page)["state"],player(page)["position"],player(page)["queue"]])


def wait_terminal(page,identity):
    page.wait_for_function("id=>['done','failed','stopped'].includes(store.getState().steps.byId[id]?.status)",arg=identity["step_id"],timeout=60000)
    return page.evaluate("id=>store.getState().steps.byId[id].status",identity["step_id"])


def inspector_group(page,server,directory,report):
    group="inspector"
    root=start_fixture(page,server)
    original=snapshot(page,root)
    source=picks(page,root["step_id"])
    first=source[0]
    row(page,first).locator(".steplist-title").click()
    check(report,group,"inspector keeps why separate from facts",[True,True,True],
          ["Why it's here" in page.locator("#inspector-region").text_content(),
           "from Discogs" in page.locator("#inspector-region").inner_text(),page.locator(".inspector-thumb").get_attribute("width")=="56"])
    before=record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0]
    page.locator('#inspector-region [data-fact-kind="label"] [data-action="select"]').first.click()
    selected=page.evaluate("selectors.selectedPoint(store.getState())")
    check(report,group,"fact click selects provenance without move",["label","label:"+str(first["facts"]["labels"][0]["id"]),first["rank"],before],
          [selected["kind"],selected["point_id"],selected["source_rank"],record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0]])
    select_pick(page,first)
    page.keyboard.press("Enter")
    check(report,group,"Enter menu has four named near moves",4,page.locator('#moves-region .moves-choice[data-index]').count())
    screenshot(page,directory,report,"SS15-1440",group=group)
    page.keyboard.press("c")
    check(report,group,"scrolling credits picker exposes more than nine",True,page.locator('#moves-region .moves-choice[data-index]').count()>9)
    screenshot(page,directory,report,"SS16-1440",group=group)
    for _ in range(10):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
    picked=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    wait_step(page,picked)
    step=snapshot(page,picked)["snapshot"]["step"]
    check(report,group,"eleventh person reachable by arrows Enter",["person","619",root["step_id"]],[step["move"],step["origins"][0]["discogs_id"],step["parent_id"]])
    for key,move in [("t","track"),("l","label"),("a","artist"),("Shift+t","track"),("Shift+l","label"),("Shift+a","artist"),("Shift+c","person")]:
        open_step(page,root)
        select_pick(page,first)
        page.keyboard.press(key)
        if key.endswith("c"):
            page.keyboard.press("1")
        page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
        child=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
        wait_step(page,child)
        persisted=snapshot(page,child)["snapshot"]["step"]
        check(report,group,"explicit "+key+" captured source",[move,key.startswith("Shift"),root["step_id"]],
              [persisted["move"],persisted["further"],persisted["parent_id"]])
        if key=="l":
            check(report,group,"label resolution uses newest five indexed releases",[f"label-source-{i}" for i in range(5)],
                  [t["cosine_id"] for t in persisted["origins"][0]["resolved_tracks"]])
    open_step(page,root)
    select_pick(page,first)
    count=record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0]
    page.keyboard.press("t")
    page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
    check(report,group,"duplicate move opens saved step without insert",[count,True],
          [record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0],"You already made this move:" in page.locator("#toast-region").inner_text()])
    open_step(page,root)
    server.control("rerun-change")
    rerun=gesture(page,"rerun",root["step_id"])
    wait_step(page,rerun)
    saved=snapshot(page,rerun)
    check(report,group,"rerun sibling has changed/same/new/gone",[None,root["step_id"],True,True],
          [saved["snapshot"]["step"]["parent_id"],saved["snapshot"]["step"]["rerun_of"],
           "changed" in page.locator("#step-region").text_content(),"Not in this re-run" in page.locator("#step-region").text_content()])
    check(report,group,"rerun freezes original",snapshot_hash(original),snapshot_hash(snapshot(page,root)))
    screenshot(page,directory,report,"C05-1440",group=group)
    failure_checks(page,server,directory,report)


def failure_checks(page,server,directory,report):
    group="inspector"
    for name,expected in [("no-shared","No shared labels or people yet"),("no-facts","Discogs has no release for this track."),
                          ("discogs-down","Facts unavailable: Discogs isn't answering.")]:
        identity=start_fixture(page,server,name)
        items=picks(page,identity["step_id"])
        select_pick(page,items[0])
        text=page.locator("#stepmap-region").inner_text()+page.locator("#inspector-region").inner_text()
        check(report,group,name+" state copy and no fabricated hub",[True,0],[expected in text,len(snapshot(page,identity)["snapshot"]["hubs"])])
        screenshot(page,directory,report,"PT09-"+name+"-1440",group=group)
    unindexed=start_fixture(page,server,"unindexed")
    check(report,group,"unindexed file names substitute recordings",True,"Your file '" in page.locator("#step-region").inner_text() and "Using" in page.locator("#step-region").inner_text())
    screenshot(page,directory,report,"SS22-1440",group=group)
    missing=start_fixture(page,server,"missing")
    check(report,group,"checked missing root uses S-N-NONE",[0,True],[len(picks(page,missing["step_id"])),"None of your seeds are on cosine.club" in page.locator("#step-region").inner_text()])
    screenshot(page,directory,report,"SS23-1440",group=group)
    empty=start_fixture(page,server,"empty")
    origin=snapshot(page,empty)["snapshot"]["step"]["origins"][0]
    origin={k:v for k,v in origin.items() if k not in ("resolved_tracks","resolution")}
    origin.update(source_step_id=empty["step_id"],source_rank=None)
    move=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:false})",origin)
    wait_step(page,move)
    check(report,group,"empty move names alternatives and offers two controls",[True,2],
          ["Nothing came back for this move. Try" in page.locator("#step-region").inner_text(),page.locator('.steplist-body button').count()])
    screenshot(page,directory,report,"SS26-1440",group=group)
    failure=start_fixture(page,server,"provider-failure",complete=False)
    status=wait_terminal(page,failure)
    check(report,group,"cosine failure preserves truthful failed/retry",["failed",True],
          [status,"Nothing was lost." in page.locator("#step-region").inner_text()])
    screenshot(page,directory,report,"SS24-1440",group=group)
    partial_root=start_fixture(page,server,"partial-failure")
    partial_origin=server.control("points",["partial-failure"])[0]
    partial_origin.update(source_step_id=partial_root["step_id"],source_rank=None)
    partial=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:true})",partial_origin)
    status=wait_terminal(page,partial)
    partial_picks=picks(page,partial["step_id"])
    check(report,group,"partial provider failure retains the three near rows and offers rerun",["failed",3,3,1,0],
          [status,len(partial_picks),page.locator('#step-region .steplist-row').count(),
           page.locator('#step-region .steplist-body [data-action="rerun"]').count(),page.locator('#step-region .steplist-body [data-action="retry"]').count()])
    record_db(report,group,server.db,"SELECT rank,cosine_id,batch FROM step_picks WHERE step_id=? ORDER BY rank",(partial["step_id"],))
    screenshot(page,directory,report,"PT09-partial-failure-1440",group=group)
    server.control("retry-failure",True)
    limited=start_fixture(page,server,"retry-once",complete=False)
    status=wait_terminal(page,limited)
    retry=page.locator('#step-region .steplist-body [data-action="retry"]')
    check(report,group,"429 is failed with disabled countdown",["failed",True,True],[status,retry.is_disabled(),"Retry in" in retry.text_content()])
    old_job=page.evaluate("id=>store.getState().steps.byId[id].current_job_id",limited["step_id"])
    screenshot(page,directory,report,"SS25-1440",group=group)
    page.wait_for_function("id=>Date.now()>=Date.parse(store.getState().steps.byId[id].error.retry_at)",arg=limited["step_id"],timeout=5000)
    advance(page,3000)
    server.control("retry-failure",False)
    retry.click()
    page.wait_for_function("({id,old})=>store.getState().steps.byId[id].current_job_id!==old",arg={"id":limited["step_id"],"old":old_job})
    wait_step(page,limited)
    new_job=page.evaluate("id=>store.getState().steps.byId[id].current_job_id",limited["step_id"])
    check(report,group,"explicit empty retry creates new job on same step",[True,limited["step_id"]],
          [old_job!=new_job,page.evaluate("store.getState().ui.route.stepId")])
    record_db(report,group,server.db,"SELECT id,attempt,state,last_seq FROM slice_jobs WHERE step_id=? ORDER BY attempt",(limited["step_id"],))
    # A real crash and restart of our own CLI process, retaining the same temp DB.
    server.control("clear-http-cache")
    server.control("block","hop1")
    stable=start_fixture(page,server)
    pick=picks(page,stable["step_id"])[0]
    running=page.evaluate("p=>store.actions.startMove({parentStepId:p.source_step_id,origins:[p],move:'track',further:true})",point_for(pick))
    server.control("entered","hop1")
    page.wait_for_function("id=>selectors.orderedPicks(store.getState(),id).length===25",arg=running["step_id"])
    play_pick(page,picks(page,running["step_id"])[6])
    server.stop(crash=True)
    advance(page,10000)
    page.wait_for_function("store.getState().ui.server==='down'")
    check(report,group,"server loss keeps loaded track playing and banner",["playing",True],[player(page)["state"],page.locator("#server-banner").is_visible()])
    screenshot(page,directory,report,"SS39-1440",group=group)
    server.start()
    advance(page,10000)
    page.wait_for_function("store.getState().ui.server==='up'")
    page.reload()
    boot(page,stub=True)
    check(report,group,"restart recovers stopped prefix and paused queue",["stopped",25,"paused",True],
          [page.evaluate("id=>store.getState().steps.byId[id].status",running["step_id"]),len(picks(page,running["step_id"])),player(page)["state"],
           "This move stopped when the app closed." in page.locator("#step-region").inner_text()])
    screenshot(page,directory,report,"SS27-1440",group=group)
    report["groups"][group]["provider_trace"]=server.control("trace")
    server.stop()
    server.environment["DISCOGS_TOKEN"]=""
    server.start()
    page.reload()
    boot(page,stub=True)
    no_token=start_fixture(page,server)
    select_pick(page,picks(page,no_token["step_id"])[0])
    page.keyboard.press("l")
    check(report,group,"no token key hint, facts and map",[True,True,True],
          ["needs a Discogs token" in page.locator("#toast-region").inner_text(),
           "Label, year and credits come from Discogs (not connected)." in page.locator("#inspector-region").inner_text(),
           "Discogs token" not in page.locator("#stepmap-region").inner_text()])
    page.keyboard.press("Enter")
    check(report,group,"no token disables L A C",3,page.locator('#moves-region .moves-choice[aria-disabled="true"]').count())
    page.keyboard.press("Escape")
    page.locator("#mode-setup").click()
    page.wait_for_function("store.getState().ui.enrichment?.overview!=null")
    check(report,group,"no token enrichment is skipped",True,"Needs a Discogs token (Discogs, above), so this is skipped." in page.locator("#labels-status").inner_text())
    screenshot(page,directory,report,"SS31-1440",group=group)


def map_position(page,layout):
    import math
    box=page.locator("#stepmap-canvas").bounding_box()
    radius=min(box["width"],box["height"])/2-24
    return {"x":box["width"]/2+radius*layout["radius"]*math.sin(layout["angle"]),
            "y":box["height"]/2-radius*layout["radius"]*math.cos(layout["angle"])}


def map_group(page,server,directory,report):
    group="map"
    root=start_fixture(page,server,"seed-a","seed-b","seed-c")
    original=snapshot(page,root)
    hubs=original["snapshot"]["hubs"]
    check(report,group,"only exact shared Discogs facts become hubs",["label:501","person:601"],sorted(h["hub_key"] for h in hubs))
    check(report,group,"each hub has at least two verified member facts",True,
          all(len(h["pick_ranks"])>=2 and all(any(str(f["id"])==str(h["discogs_id"]) for f in
              next(p for p in original["snapshot"]["picks"] if p["rank"]==rank)["facts"]["labels" if h["kind"]=="label" else "credits"])
              for rank in h["pick_ranks"]) for h in hubs))
    source=picks(page,root["step_id"])
    canvas=page.locator("#stepmap-canvas")
    count=record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0]
    canvas.click(position=map_position(page,source[6]["layout"]))
    selected=page.evaluate("selectors.selectedPoint(store.getState())")
    check(report,group,"saved-coordinate map hit selects pick",source[6]["track_key"],selected.get("track_key"))
    canvas.dblclick(position=map_position(page,source[6]["layout"]))
    page.wait_for_function("store.getState().player.state==='playing'")
    canvas.click(position=map_position(page,source[8]["layout"]))
    check(report,group,"playing and selected dots remain distinct",[source[6]["id"],"track:"+source[8]["track_key"]],
          [player(page)["current"]["pickId"],page.evaluate("store.getState().ui.route.sel")])
    for hub in hubs:
        canvas.click(position=map_position(page,hub["layout"]))
        selected=page.evaluate("selectors.selectedPoint(store.getState())")
        check(report,group,"hub selection through store "+hub["kind"],[hub["kind"],hub["hub_key"],None],
              [selected["kind"],selected["point_id"],selected["source_rank"]])
    # Origin uses its store identity, allowing FX9's canonical origin token.
    box=canvas.bounding_box()
    import math
    angle=math.pi/len(original["snapshot"]["step"]["origins"])
    canvas.click(position={"x":box["width"]/2+18*math.sin(angle),"y":box["height"]/2-18*math.cos(angle)})
    selected=page.evaluate("selectors.selectedPoint(store.getState())")
    check(report,group,"origin provenance survives URL-token amendment",["track",original["snapshot"]["step"]["origins"][0]["point_id"],None],
          [selected["kind"],selected["point_id"],selected["source_rank"]])
    check(report,group,"content clicks and doubleclick never create moves",count,record_db(report,group,server.db,"SELECT COUNT(*) FROM steps")[0][0])
    page.mouse.move(0,0)
    select_pick(page,source[8])
    static=canvas.screenshot()
    advance(page,500)
    check(report,group,"reduced map paint is static including playing rings",True,static==canvas.screenshot())
    canvas.focus()
    baseline=page.evaluate("canvasPaint['stepmap-canvas'].filter(c=>c.name==='arc').map(c=>c.args[2])")
    page.keyboard.press("+")
    zoomed=page.evaluate("canvasPaint['stepmap-canvas'].filter(c=>c.name==='arc').map(c=>c.args[2])")
    check(report,group,"map keyboard zoom affects projected geometry",True,max(zoomed)>max(baseline))
    page.keyboard.press("0")
    check(report,group,"fit restores map camera",baseline,page.evaluate("canvasPaint['stepmap-canvas'].filter(c=>c.name==='arc').map(c=>c.args[2])"))
    check(report,group,"camera/selection leave saved layout untouched",snapshot_hash(original),snapshot_hash(snapshot(page,root)))
    report["groups"][group]["saved_geometry"]={"picks":[{"id":p["id"],"layout":p["layout"]} for p in source],"hubs":hubs}
    report["groups"][group]["canvas_paint"]=page.evaluate("canvasPaint['stepmap-canvas']")
    screenshot(page,directory,report,"PT10-stepmap-1440",group=group)
    lonely=start_fixture(page,server,"no-shared")
    check(report,group,"no singleton or via hubs",[0,True],[len(snapshot(page,lonely)["snapshot"]["hubs"]),"No shared labels or people yet" in page.locator("#stepmap-caption").inner_text()])


def territory_group(page,server,directory,report):
    group="territory"
    root=start_fixture(page,server)
    original=snapshot(page,root)
    source=picks(page,root["step_id"])
    identities=[root]
    for pick in source[:24]:
        identity=page.evaluate("p=>shell.api.request('POST','/api/steps',{parent_step_id:p.source_step_id,origins:[p],move:'track',further:false})",point_for(pick))
        identities.append(identity)
    # Real admission/exclusion rules can leave a child with fewer than fifteen
    # picks. Fill from additional children until forty distinct steps exist.
    known={identity["step_id"] for identity in identities}
    for parent in list(identities[1:]):
        if len(known)==40:
            break
        completed = wait_http_steps(page, "/api/steps/" + parent["step_id"])
        for pick in completed["snapshot"]["picks"]:
            if len(known)==40:
                break
            identity=page.evaluate("p=>shell.api.request('POST','/api/steps',{parent_step_id:p.source_step_id,origins:[p],move:'track',further:false})",point_for(pick))
            if identity["step_id"] not in known:
                identities.append(identity)
                known.add(identity["step_id"])
    if len(known)!=40:
        raise RunFailure("HTTP fixture could not create forty distinct steps")
    wait_http_steps(page, "/api/digs/" + root["dig_id"])
    open_step(page,identities[-1])
    # Hydrate the saved fixture through the normal app lifecycle, clearing
    # followers started while its forty HTTP-created jobs were still running.
    page.reload()
    boot(page,stub=True)
    page.wait_for_function("id=>{const s=store.getState(),ids=s.steps.byDig[id]||[]; return ids.length===40 && ids.every(id=>s.steps.byId[id].status==='done');}",arg=root["dig_id"],timeout=60000)
    gesture(page,"judge",{"trackKey":source[6]["track_key"],"stepId":root["step_id"],"verdict":"keep","mode":"toggle"})
    play_pick(page,source[7])
    viewed=picks(page)[6]
    select_pick(page,viewed)
    before=page.evaluate("({step:store.getState().ui.route.stepId,sel:store.getState().ui.route.sel,queue:store.getState().player.queue,history:history.length})")
    page.keyboard.press("v")
    check(report,group,"List Map shares selection source queue and history",before,page.evaluate("({step:store.getState().ui.route.stepId,sel:store.getState().ui.route.sel,queue:store.getState().player.queue,history:history.length})"))
    check(report,group,"territory renders forty saved islands",40,page.evaluate("canvasPaint['territory-canvas'].filter(c=>c.name==='fillText'&&/^\\d+ · |^▶ \\d+ · /.test(c.args[0])).length"))
    islands=record_db(report,group,server.db,"SELECT id,number,parent_id,island_x,island_y FROM steps WHERE dig_id=? ORDER BY number",(root["dig_id"],))
    check(report,group,"persisted forty island coordinates are unique",[40,40],[len(islands),len({(r[3],r[4]) for r in islands})])
    check(report,group,"territory title slim-list and persistent inspector",[True,True,True],
          ["The whole dig, drawn as a map" in page.locator("#territory-region").text_content(),page.locator("#slim-list-region").bounding_box()["width"]>=240,
           viewed["title"] in page.locator("#inspector-region").inner_text()])
    canvas=page.locator("#territory-canvas")
    page.mouse.move(0,0)
    frozen=canvas.screenshot()
    advance(page,500)
    check(report,group,"reduced territory camera and rings are static",True,frozen==canvas.screenshot())
    for width in (1100,1280,1440):
        page.set_viewport_size({"width":width,"height":900})
        screenshot(page,directory,report,f"SS32-{width}-rm",group=group)
    canvas.focus()
    page.keyboard.press("0")
    paint=page.evaluate("canvasPaint['territory-canvas']")
    report["groups"][group]["canvas_paint_fit"]=paint
    report["groups"][group]["islands"]=islands
    # Click a painted label of a different island at Fit, using the actual canvas projection.
    label=next(c for c in paint if c["name"]=="fillText" and re.match(r"^(?:▶ )?1 · ",str(c["args"][0])))
    canvas.click(position={"x":label["args"][1],"y":label["args"][2]})
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    check(report,group,"island navigation keeps Map view and frozen snapshot",["map",snapshot_hash(original)],
          [page.evaluate("store.getState().ui.route.view"),snapshot_hash(snapshot(page,root))])
    screenshot(page,directory,report,"PT10-forty-islands-1440",group=group)


def keys_group(page,server,directory,report):
    group="keys"
    ledger=[]
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("/")
    check(report,group,"slash focuses new-dig search",True,page.locator("#seed-input").evaluate("el=>el===document.activeElement"))
    page.keyboard.type("Fixture Artist B")
    advance(page,250)
    page.wait_for_function("selectors.seedResults(store.getState()).length>0")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.keyboard.press("Backspace")
    check(report,group,"keyboard add and Backspace remove seed",0,page.evaluate("store.getState().ui.search.chips.length"))
    page.keyboard.type("Fixture Artist B")
    advance(page,250)
    page.wait_for_function("selectors.seedResults(store.getState()).length>0")
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    page.wait_for_function("store.getState().ui.route.stepId!==null")
    root=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    wait_step(page,root)
    for _ in range(70):
        if page.evaluate("document.activeElement.matches('.steplist-row')"):
            break
        page.keyboard.press("Tab")
    check(report,group,"native Tab reaches list without pointer",True,page.evaluate("document.activeElement.matches('.steplist-row')"))
    page.keyboard.press("ArrowDown")
    selected=page.evaluate("selectors.selectedPoint(store.getState())")
    page.keyboard.press("p")
    page.wait_for_function("store.getState().player.state==='playing'")
    source=picks(page,root["step_id"])
    queue=player(page)["queue"]
    page.keyboard.press("t")
    page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
    child=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    wait_step(page,child)
    page.keyboard.press("Shift+p")
    check(report,group,"keyboard branch and Shift P preserve source",[queue,child["step_id"]],[player(page)["queue"],player(page)["nextQueue"]["stepId"]])
    page.keyboard.press("u")
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    check(report,group,"keyboard parent restores pick token",selected["point_id"],page.evaluate("store.getState().ui.route.sel"))
    page.go_back()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=child["step_id"])
    page.go_forward()
    page.wait_for_function("id=>store.getState().ui.route.stepId===id",arg=root["step_id"])
    ledger.append({"keys":"/ Tab Shift+Tab arrows Enter Backspace P Shift+P T U native Back/Forward","route":page.evaluate("store.getState().ui.route")})
    # Seek/previous/next keys traverse the mounted SDK's transport.
    play_pick(page,source[6])
    select_pick(page,source[6])
    for key,position in [("ArrowRight",15),("ArrowLeft",0),("Shift+ArrowRight",60),("[",0)]:
        page.keyboard.press(key)
        check(report,group,"key transport "+key,position,player(page)["position"])
        ledger.append({"key":key,"position":player(page)["position"]})
    page.keyboard.press("]")
    check(report,group,"bracket next",source[7]["id"],player(page)["current"]["pickId"])
    page.keyboard.press("g")
    page.wait_for_function("store.getState().ui.route.sel==='track:'+store.getState().player.current.trackKey")
    page.keyboard.press("Space")
    page.wait_for_function("store.getState().player.state==='paused'")
    page.keyboard.press("Space")
    page.wait_for_function("store.getState().player.state==='playing'")
    playing=source[7]
    page.keyboard.press("k")
    page.wait_for_function("key=>store.getState().judgments.byTrack[key]?.verdict==='keep'&&!store.getState().judgments.pending[key]",arg=playing["track_key"])
    page.keyboard.press("x")
    page.wait_for_function("key=>store.getState().judgments.byTrack[key]?.verdict==='pass'&&!store.getState().judgments.pending[key]",arg=playing["track_key"])
    check(report,group,"X judges source and skips it",True,player(page)["current"]["pickId"]!=playing["id"])
    page.keyboard.press("Meta+z")
    page.wait_for_function("key=>store.getState().judgments.byTrack[key]?.verdict==='keep'",arg=playing["track_key"])
    page.keyboard.press("z")
    page.wait_for_function("key=>!store.getState().judgments.byTrack[key]?.verdict",arg=playing["track_key"])
    select_pick(page,source[0])
    page.keyboard.press("Enter")
    position=player(page)["position"]
    for _ in range(3):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Shift+ArrowRight")
    check(report,group,"menu arrows open captured further credits without seeking",[True,position],
          [page.get_by_role("menu",name="Which credited person?",exact=True).is_visible(),player(page)["position"]])
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Escape")
    for key,move in [("t","track"),("l","label"),("a","artist"),("c","person"),
                     ("Shift+t","track"),("Shift+l","label"),("Shift+a","artist"),("Shift+c","person")]:
        open_step(page,root)
        select_pick(page,source[0])
        page.keyboard.press(key)
        if move=="person":
            page.keyboard.press("2")
        page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
        identity=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
        wait_step(page,identity)
        actual=snapshot(page,identity)["snapshot"]["step"]
        check(report,group,"move key "+key,[move,key.startswith("Shift"),root["step_id"]],[actual["move"],actual["further"],actual["parent_id"]])
        ledger.append({"key":key,"step_id":identity["step_id"],"origin":actual["origins"][0]})
    for number in range(1,10):
        open_step(page,root)
        select_pick(page,source[0])
        further=number%2==0
        page.keyboard.press("Shift+c" if further else "c")
        choice=page.evaluate("i=>store.getState().ui.menu.choices[i].point",number-1)
        page.keyboard.press(str(number))
        page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
        identity=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
        wait_step(page,identity)
        actual=snapshot(page,identity)["snapshot"]["step"]
        check(report,group,"picker numeric key "+str(number)+" preserves captured mode",[choice["discogs_id"],further,root["step_id"]],
              [actual["origins"][0]["discogs_id"],actual["further"],actual["parent_id"]])
        ledger.append({"key":str(number),"opener":"Shift+C" if further else "C","step_id":identity["step_id"],"origin":actual["origins"][0]})
    open_step(page,root)
    select_pick(page,source[6])
    page.keyboard.press("Enter")
    queue_before=player(page)["queue"]
    page.keyboard.press("Shift+Enter")
    page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
    identity=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
    wait_step(page,identity)
    actual=snapshot(page,identity)["snapshot"]["step"]
    check(report,group,"Shift Enter chooses captured further move without replacing queue",["track",True,source[6]["rank"],queue_before],
          [actual["move"],actual["further"],actual["origins"][0]["source_rank"],player(page)["queue"]])
    ledger.append({"key":"Shift+Enter","step_id":identity["step_id"],"origin":actual["origins"][0]})
    open_step(page,root)
    gesture(page,"judge",{"trackKey":source[6]["track_key"],"stepId":root["step_id"],"verdict":"keep","mode":"toggle"})
    for region in ("list","map","territory","trail","inspector"):
        for key in ("m","Shift+m"):
            open_step(page,root,view="map" if region=="territory" else "list")
            target={"list":"#step-region","map":"#stepmap-canvas","territory":"#territory-canvas",
                    "trail":"#trail-region [role=treeitem][tabindex='0']","inspector":"#inspector-region button"}[region]
            page.locator(target).first.focus()
            page.keyboard.press(key)
            page.wait_for_function("id=>store.getState().ui.route.stepId!==id",arg=root["step_id"])
            identity=page.evaluate("({dig_id:store.getState().ui.route.digId,step_id:store.getState().ui.route.stepId})")
            wait_step(page,identity)
            actual=snapshot(page,identity)["snapshot"]["step"]
            check(report,group,region+" "+key+" ignores selection",["these",root["step_id"],"dig" if key.startswith("Shift") else "step"],
                  [actual["move"],actual["parent_id"],actual["context"]["these_scope"]])
    open_step(page,root)
    select_pick(page,source[6])
    for canvas_id in ("stepmap-canvas","territory-canvas"):
        if canvas_id=="territory-canvas":
            page.keyboard.press("v")
        page.locator("#"+canvas_id).focus()
        for key in ("+","=","-","0","ArrowUp","ArrowDown"):
            page.keyboard.press(key)
            ledger.append({"key":key,"region":canvas_id,"selected":page.evaluate("store.getState().ui.route.sel")})
    page.keyboard.press("v")
    check(report,group,"V restores keyboard list focus",True,page.evaluate("document.activeElement.matches('.steplist-row')"))
    page.keyboard.press("?")
    sheet=page.get_by_role("dialog",name="Keys",exact=True)
    check(report,group,"complete key sheet groups",["Start","Move around","Play","Keep","Dig from…","Map"],sheet.locator(".keys-group h3").all_text_contents())
    expected=["/","?","Backspace","Tab / Shift+Tab","Cmd+[ / Cmd+]","P / Shift+P","K","X","Z / Cmd+Z","Shift+T L A C","M / Shift+M","1–9","+ (or =) / −","0"]
    check(report,group,"complete key sheet bindings",[],[key for key in expected if key not in sheet.locator("kbd").all_text_contents()])
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    check(report,group,"modal-only focus trap",True,page.locator("#keys-close").evaluate("el=>el===document.activeElement"))
    page.keyboard.press("Shift+Tab")
    check(report,group,"modal reverse Tab stays in sheet",True,sheet.evaluate("el=>el.contains(document.activeElement)"))
    screenshot(page,directory,report,"SS33-1440",group=group)
    page.keyboard.press("Escape")
    before=page.evaluate("({route:store.getState().ui.route,queue:store.getState().player.queue})")
    mutations=page.evaluate("requestTrace.filter(r=>['POST','PUT','DELETE'].includes(r.method)).length")
    page.frame_locator("#player").locator("body").click()
    for key in ("t","k","x","p","v","?","ArrowRight","Space"):
        page.keyboard.press(key)
    check(report,group,"native iframe keys are inert in app",[before,mutations],
          [page.evaluate("({route:store.getState().ui.route,queue:store.getState().player.queue})"),page.evaluate("requestTrace.filter(r=>['POST','PUT','DELETE'].includes(r.method)).length")])
    screenshot(page,directory,report,"SS38-1440",group=group)
    page.locator("#mode-setup").click()
    page.wait_for_function("store.getState().ui.route.page==='setup'")
    page.evaluate("""()=>{const el=document.createElement('div');el.id='harness-rich';el.contentEditable='true';document.getElementById('setup-region').append(el);}""")
    page.locator("#yt-connect").click()  # the login headers live in the YouTube card's form
    before=page.evaluate("({route:store.getState().ui.route,queue:store.getState().player.queue})")
    for target in ("#set-music","#yt-headers","#set-weekly-day","#harness-rich"):
        page.locator(target).focus()
        page.evaluate("""()=>{for(const key of ['t','l','a','c','m','k','x','p','v','u','z',' ','[',']','/','?','Escape','ArrowRight'])
          document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));}""")
    check(report,group,"inputs selects textarea contenteditable guard",before,page.evaluate("({route:store.getState().ui.route,queue:store.getState().player.queue})"))
    page.evaluate("document.getElementById('harness-rich').remove()")
    open_step(page,root)
    select_pick(page,source[6])
    page.keyboard.press("Tab")
    select_pick(page,source[6])
    metrics=page.evaluate("""()=>{const el=document.activeElement,r=el.getBoundingClientRect(),s=getComputedStyle(el);
      return {outline:s.outlineWidth,style:s.outlineStyle,width:r.width,height:r.height,
        visible:[5,r.height-5].every(y=>{const n=document.elementFromPoint(r.x+8,r.y+y);return n===el||el.contains(n);})};}""")
    check(report,group,"visible focus ring and target floor",True,metrics["outline"]=="3px" and metrics["style"]=="solid" and metrics["width"]>=24 and metrics["height"]>=24 and metrics["visible"])
    report["groups"][group].update(key_effects=ledger,focus=metrics)


def feeds_group(page,server,directory,report):
    group="feeds"
    server.control("feed-library")
    library = record_db(report,group,server.db,"SELECT COUNT(*) FROM tracks")
    check(report,group,"feed library is the single fixture seed",[[1]],library)
    legacy_before = legacy_hashes(server.db)
    root=start_fixture(page,server)
    play_pick(page,picks(page,root["step_id"])[6])
    queue=player(page)["queue"]
    page.locator("#mode-feeds").click()
    page.wait_for_function("!!selectors.feedState(store.getState(),'daily')")
    check(report,group,"feeds cards clock and moved schedule",[2,True,True,True],
          [page.locator('[data-feed-kind]').count(),page.locator("#feeds-clock").inner_text().startswith("LOCAL TIME "),
           page.locator("#feeds-next-daily").is_hidden(),page.locator("#key-strip").is_hidden()])  # serve --no-scheduler: no next run
    check(report,group,"feeds have no Recent digs or feed-as-dig",[False,0],
          ["Recent digs" in page.locator("#feeds-region").inner_text(),page.locator('#feeds-region [data-action="start-dig"]').count()])
    with closing(readonly(server.db)) as conn:
        tables=logical_counts(conn)
    before={name:tables.get(name) for name in ("served_ledger","sessions","session_results","result_events","session_branches")}
    for kind in ("daily","weekly"):
        card=page.locator(f'[data-feed-kind="{kind}"]')
        card.locator('[data-slot="dry"]').click()
        page.wait_for_function("kind=>!!selectors.feedState(store.getState(),kind)?.result",arg=kind,timeout=60000)
        check(report,group,kind+" real legacy dry-run adapter",True,"Dry run" in card.locator('[data-slot="result"]').text_content())
    with closing(readonly(server.db)) as conn:
        after=logical_counts(conn)
    check(report,group,"dry run writes no playlist/served ledger/CLI taste",before,{name:after.get(name) for name in before})
    legacy_after = legacy_hashes(server.db)
    check(report,group,"dry runs preserve legacy contents",legacy_before,legacy_after)
    report["groups"][group]["legacy_sha256"] = {"before":legacy_before,"after":legacy_after}
    check(report,group,"feeds preserve visible playing queue",["playing",queue],[player(page)["state"],player(page)["queue"]])
    screenshot(page,directory,report,"SS36-1440",group=group)
    # Render known legacy results through a fake legacy HTTP job, never Run now/export. RUN NOW shows
    # only once a YouTube Music login is saved (P14) and this fixture has none, so the settings read
    # says a login is saved and connected while these mocked cards render.
    def saved_login(route):
        response=route.fetch()
        route.fulfill(response=response,json={**response.json(),"youtube":{"logged_in":True,"saved":True,"checked":"connected"}})
    page.route("**/api/settings",saved_login)
    page.evaluate("store.actions.loadSettings()")
    page.wait_for_function("!document.querySelector('[data-feed-kind=daily] [data-slot=run]').hidden")
    for index,(label,export) in enumerate([("Not verified",{"verified":False,"missing":["one"],"failed":["two"],"omitted":[]}),
                         ("Written",{"verified":None,"missing":[],"failed":[],"omitted":[]})]):
        job_id=f"playthrough-card-{index}"
        page.route("**/api/playlists/daily/run",lambda route:route.fulfill(json={"job_id":job_id}))
        result={"tracks":[{"id":"fixture","artist":"Fixture","track":"Card","name":"Fixture - Card"}],"export":export}
        page.route(f"**/api/jobs/{job_id}/events",lambda route:route.abort())
        page.route(f"**/api/jobs/{job_id}",lambda route:route.fulfill(json={"id":job_id,"state":"done","result":result}))
        # The card's warning renderer reads the legacy adapter's result; dry-run flag
        # is false only in this mocked transport, with no playlist write possible.
        page.locator('[data-feed-kind="daily"] [data-slot="run"]').click()
        page.wait_for_function("label=>document.querySelector('[data-feed-kind=daily] [data-slot=result]').textContent.includes(label)",arg=label)
        check(report,group,"fake legacy export-result warning "+label,True,label in page.locator('[data-feed-kind="daily"] [data-slot="result"]').text_content())
        page.unroute("**/api/playlists/daily/run")
        page.unroute(f"**/api/jobs/{job_id}/events")
        page.unroute(f"**/api/jobs/{job_id}")
    page.unroute("**/api/settings")
    page.evaluate("store.actions.loadSettings()")
    report["groups"][group]["card_source"]="STUB legacy result cards; real provider dry runs from the temporary fixture library only, no export/login"
    record_db(report,group,server.db,"SELECT COUNT(*) FROM served_ledger")


def file_identity(folder):
    return {p.name:{"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"mtime_ns":p.stat().st_mtime_ns}
            for p in folder.iterdir() if p.is_file()}


def setup_group(page,server,directory,report):
    group="setup"
    server.control("no-library")
    folder=Path(server.control("library"))
    before=file_identity(folder)
    page.locator("#mode-setup").click()
    page.wait_for_function("store.getState().ui.enrichment?.overview!=null")
    check(report,group,"Setup help and About strings",[True,True,True],
          ["Any folder of tagged audio files works; Plex is optional." in page.locator("#setup-region").inner_text(),
           "This app embeds YouTube's player to play tracks." in page.locator("#about").inner_text(),
           "If your browser blocks YouTube's cookies" in page.locator("#about").inner_text()])
    for name,href in [("YouTube Terms of Service","https://www.youtube.com/t/terms"),("Google Privacy Policy","https://policies.google.com/privacy")]:
        check(report,group,name+" link",href,page.get_by_role("link",name=name,exact=True).get_attribute("href"))
    for key in ("discogs","exa"):  # the key screen owns the cosine.club key
        check(report,group,"masked Setup "+key,"",page.locator("#set-"+key).input_value())
    page.locator("#set-music").fill(str(folder))
    page.locator("#scan-go").click()
    page.wait_for_function("document.getElementById('scan-log').textContent.includes('Done:')",timeout=30000)
    check(report,group,"scan uses generated tagged files",3,record_db(report,group,server.db,"SELECT COUNT(*) FROM tracks")[0][0])
    check(report,group,"scan preserves file bytes and mtimes",before,file_identity(folder))
    page.wait_for_function("document.getElementById('labels-status').textContent.includes('2 of 3')")
    server.control("block","enrichment")
    page.locator("#labels-go").click()
    check(report,group,"enrichment gate entered",True,server.control("entered","enrichment"))
    page.wait_for_function("store.getState().ui.enrichment?.state==='running'")
    first=page.evaluate("store.getState().ui.enrichment.id")
    page.locator("#mode-dig").click()
    page.locator("#mode-setup").click()
    page.reload()
    boot(page,stub=True)
    page.wait_for_function("store.getState().ui.enrichment?.state==='running'")
    check(report,group,"enrichment survives navigation/reload",first,page.evaluate("store.getState().ui.enrichment.id"))
    page.locator("#labels-stop").click()
    server.control("release","enrichment")
    page.wait_for_function("store.getState().ui.enrichment?.state==='stopped'")
    check(report,group,"cancel checkpoints first lookup",True,"Stopped after 1 of 2; 1 labels filled. The next run picks up here." in page.locator("#labels-status").inner_text())
    page.locator("#labels-budget").fill("1")
    page.locator("#labels-go").click()
    page.wait_for_function("store.getState().ui.enrichment?.state==='done'")
    second=page.evaluate("store.getState().ui.enrichment")
    check(report,group,"resume gets fresh bounded request budget",[True,1],[second["id"]!=first,second["result"]["requests_used"]])
    check(report,group,"enrichment changes DB labels and never file tags",[2,before],
          [record_db(report,group,server.db,"SELECT COUNT(*) FROM tracks WHERE label='Filled Library Label'")[0][0],file_identity(folder)])
    library=page.evaluate("shell.api.request('GET','/api/search?q=File&source=library')")
    check(report,group,"source-agnostic scanned library search",True,len(library["items"])>0 and all(p["library_id"] for p in library["items"]))
    screenshot(page,directory,report,"SS37-1440",group=group)
    # A fresh missing-label checkpoint exercises stopping at a request budget.
    # Reset only the generated fixture rows, never a user's files or database.
    server.control('missing-labels')
    page.reload()
    boot(page,stub=True)
    page.locator('#labels-budget').fill('1')
    page.locator('#labels-go').click()
    page.wait_for_function("store.getState().ui.enrichment?.state==='stopped'")
    budget=page.evaluate('store.getState().ui.enrichment')
    check(report,group,'request budget stops with a resumable checkpoint',['budget',1,True],
          [budget['result']['reason'],budget['result']['requests_used'],'Lookup budget reached.' in page.locator('#labels-detail').text_content()])
    screenshot(page,directory,report,'PT13-budget-1440',group=group)
    page.locator('#labels-go').click()
    page.wait_for_function("store.getState().ui.enrichment?.state==='done'")
    resumed=page.evaluate('store.getState().ui.enrichment')
    check(report,group,'budget resume gives a fresh allowance',[True,1,2],
          [resumed['id']!=budget['id'],resumed['result']['requests_used'],resumed['result']['filled_tracks']])
    page.locator('#health-refresh').click()
    page.wait_for_function('!!store.getState().ui.setup.doctor')
    check(report,group,'O4 doctor uses a synchronous GET',True,
          page.evaluate("requestTrace.some(r=>r.path==='/api/doctor'&&r.method==='GET')&&!requestTrace.some(r=>r.path==='/api/doctor'&&r.method==='POST')"))
    check(report,group,'budget/resume still preserves generated file identities',before,file_identity(folder))
    report["groups"][group].update(generated_files=before,enrichment_jobs=[first,second["id"],budget['id'],resumed['id']],provider_trace=server.control("trace"))


def seed_live_smoke(db):
    """Only persisted smoke picks in the validated temporary copy, not recommendations."""
    from cosine_digger import storage
    from cosine_digger.api import _key
    from cosine_digger.slice_types import Point

    home=Path(os.environ["COSINE_DIGGER_HOME"])
    from cosine_digger.web.slice_testmode import require_temporary_storage
    require_temporary_storage(home,db)
    if db.resolve()!=Path(os.environ["COSINE_DIGGER_DB"]).resolve():
        raise RunFailure("Live smoke database must be the temporary copy")
    origin=Point(point_id="track:youtube-smoke-origin",kind="track",name="Persisted embed smoke",artist="YouTube smoke",title="Origin")
    with closing(storage.connect(db)) as conn,conn:
        conn.execute("BEGIN IMMEDIATE")
        dig=storage.create_dig(conn,[origin])
        identity=storage.create_step(conn,dig_id=dig,parent_id=None,origins=[origin],move="seed",further=False)
        emit=lambda name,data:storage.append_slice_event(conn,identity.job_id,name,data,redact=lambda value:value)
        emit("batch",{"hop":0,"start_rank":1,"target_count":3,"label":"near","score_floor":0.8})
        # The refusal's successor is the successful embed; no provider resolution or export.
        for rank,video,title in [(1,"jNQXAC9IVRw","Me at the zoo"),(2,"Pso5YU_mntg","Known refused embed"),(3,"jNQXAC9IVRw","Smoke successor")]:
            emit("pick",{"pick":{"rank":rank,"track_key":_key("YouTube smoke",title),"cosine_id":f"persisted-smoke-{rank}",
                                 "artist":"YouTube smoke","title":title,"video_id":video,"score":0.8,"batch":0,
                                 "route":{"hop":0,"layout_seed":origin.point_id,"source":"sandbox-only persisted smoke"},
                                 "facts":None,"facts_state":"missing"}})
        emit("picks_done",{"pick_count":3})
        emit("done",{"status":"done","pick_count":3})
        return identity.to_dict()


def route_live_observer(page):
    """Observe the SDK through the same mounted app used by the live smoke."""
    # The real SDK and iframe are fetched unchanged. This observer only
    # records notifications and method calls, forwarding the originals.
    page.route(re.compile(r"/static/slice\.js$"),lambda route:route.fulfill(content_type="text/javascript",body="""
      const sdk=document.createElement('script');sdk.src='https://www.youtube.com/iframe_api';
      await new Promise((resolve,reject)=>{sdk.onload=resolve;sdk.onerror=reject;document.head.append(sdk);});
      // iframe_api's onload precedes the widget API's definition of Player.
      // Leave onYouTubeIframeAPIReady untouched and wait before booting the app.
      const deadline=performance.now()+60000;
      while(typeof window.YT?.Player!=='function') {
        if(performance.now()>=deadline)throw new Error('YouTube Player API readiness timed out');
        await new Promise(resolve=>setTimeout(resolve,50));
      }
      window.liveEvents=[];window.liveCalls=[];window.livePlayers=[];
      const Native=YT.Player,observed=new WeakSet();
      function observe(yt){
        if(observed.has(yt))return;observed.add(yt);
        for(const name of ['loadVideoById','cueVideoById','playVideo','pauseVideo','seekTo']) {
          const original=yt[name];if(typeof original!=='function')continue;
          yt[name]=function(...args){liveCalls.push({name,args,at:performance.now()});return original.apply(this,args);};
        }
      }
      YT.Player=function(target,options){
        const events={...options.events};
        for(const name of ['onReady','onStateChange','onError','onAutoplayBlocked']) {
          const original=events[name];events[name]=e=>{
            if(name==='onReady')observe(e.target);
            liveEvents.push({name,data:typeof e.data==='number'?e.data:null,at:performance.now(),position:e.target.getCurrentTime?.()||0});
            original?.(e);
          };
        }
        const yt=new Native(target,{...options,events});livePlayers.push(yt);
        return yt;
      };
      const device=document.getElementById('device');device.id='live-observer';
      const {boot}=await import('/static/slice.js?live-observer');device.id='device';
      window.shell=await boot();window.store=shell.store;window.selectors=(await import('/static/state.js')).selectors;
    """))


def live_player(server,directory,report):
    from playwright.sync_api import sync_playwright

    result=subprocess.run([str(ROOT/".venv/bin/python"),str(Path(__file__).resolve()),"--live-smoke-db",str(server.db)],
                          env=server.environment,cwd=server.directory,capture_output=True,text=True,timeout=10)
    if result.returncode:
        raise RunFailure("Temporary persisted embed smoke could not be prepared")
    identity=json.loads(result.stdout)
    group="player"
    report["groups"][group]["pick_source"]="sandbox-only persisted smoke picks; not live recommendation evidence"
    with sync_playwright() as playwright:
        browser=launch(playwright)
        try:
            context=browser.new_context(viewport={"width":1440,"height":900},reduced_motion="reduce")
            page=context.new_page()
            page.set_default_timeout(60000)
            route_live_observer(page)
            referers=[]
            def record_request(request):
                if request.url.startswith("https://www.youtube.com/embed/"):
                    referers.append(request.header_value("referer"))
            page.on("request",record_request)
            phase="LIVE01 SDK and app readiness"
            try:
                page.goto(server.url+f"/#/dig/{identity['dig_id']}/step/{identity['step_id']}")
                boot(page,stub=False)
                page.wait_for_function("liveEvents.some(e=>e.name==='onReady')")
                source=picks(page,identity["step_id"])
                select_pick(page,source[0])
                phase="LIVE01 PLAYING with advancing currentTime"
                page.keyboard.press("Space")
                page.wait_for_function("livePlayers[0].getPlayerState()===1 && store.getState().player.state==='playing'",timeout=90000)
                first=page.evaluate("({state:livePlayers[0].getPlayerState(),time:livePlayers[0].getCurrentTime(),at:performance.now()})")
                page.wait_for_function("time=>livePlayers[0].getCurrentTime()>time+0.5",arg=first["time"],timeout=15000)
                second=page.evaluate("({state:livePlayers[0].getPlayerState(),time:livePlayers[0].getCurrentTime(),at:performance.now()})")
                check(report,group,"LIVE01 real PLAYING and advancing currentTime",True,first["state"]==second["state"]==1 and second["time"]>first["time"])
                report["groups"][group]["LIVE01"]={"first":first,"second":second,"video_id":"jNQXAC9IVRw"}
                rect=page.locator("#player").bounding_box()
                check(report,group,"LIVE01 visible frame and default Referer",True,rect["width"]>=200 and rect["height"]>=200 and any(r and r.startswith(server.url+"/") for r in referers))
                screenshot(page,directory,report,"LIVE01-1440",group=group)
                phase="LIVE02 actual error150 and row/LCD note"
                gesture(page,"playPick",{"pickId":source[1]["id"]})
                page.wait_for_function("liveEvents.some(e=>e.name==='onError'&&e.data===150) && store.getState().player.state==='refused'",timeout=90000)
                error=page.evaluate("liveEvents.findLast(e=>e.name==='onError'&&e.data===150)")
                note=page.locator("#lcd-note").inner_text()
                check(report,group,"LIVE02 actual error150 row/LCD note",True,"the owner turned off embedding" in note and "can't play here" in row(page,source[1]).inner_text())
                screenshot(page,directory,report,"LIVE02-150-1440",group=group)
                phase="LIVE02 successor load within 2.0 to 2.5s"
                page.wait_for_function("id=>store.getState().player.current.pickId===id",arg=source[2]["id"],timeout=5000)
                calls=page.evaluate("liveCalls")
                successor=next(c for c in calls if c["name"]=="loadVideoById" and c["at"]>=error["at"] and c["args"][0]["videoId"]=="jNQXAC9IVRw")
                elapsed=successor["at"]-error["at"]
                check(report,group,"LIVE02 next-load allowance 2.0 to 2.5s",True,2000<=elapsed<=2500)
                report["groups"][group]["LIVE02"]={"error":error,"successor":successor,"elapsed_ms":elapsed,"video_id":"Pso5YU_mntg"}
                report["groups"][group].update(events=page.evaluate("liveEvents"),player_calls=calls,geometry=rect,referers=referers)
                record_db(report,group,server.db,"SELECT rank,video_id,track_key FROM step_picks WHERE step_id=? ORDER BY rank",(identity["step_id"],))
                check(report,group,"live embed never substituted by stub",False,page.evaluate("!!window.__sliceYTStub"))
            except Exception as error:
                reason=str(error) if isinstance(error,RunFailure) else type(error).__name__
                check(report,group,phase,"completed",reason)
                raise RunFailure(phase+": "+reason) from None
            finally:
                if page.evaluate("!!window.shell"):
                    page.evaluate("shell.destroy()")
                context.close()
        finally:
            browser.close()
    checks=report["groups"][group]["checks"]
    report["groups"][group]["status"]="PASS" if checks and all(c["status"]=="PASS" for c in checks) else "FAIL"


def browser_groups(source, directory, selected, report):
    from playwright.sync_api import sync_playwright

    actions={"state":state_group,"shell":lambda p,s,d,r:shell_group(p,d,r),
             "player":player_group,"seeds-list":seeds_list_group,
             "trail":trail_group,"inspector":inspector_group,"map":map_group,
             "territory":territory_group,"keys":keys_group,"feeds":feeds_group,"setup":setup_group}
    with sync_playwright() as playwright:
        browser=None
        try:
            for group in selected:
                print("PLAYTHROUGH GROUP: " + group,flush=True)
                browser=launch(playwright)
                errors,external=[],[]
                try:
                    with sandbox_server(source,report) as server:
                        server.control("fixture-library")
                        if group=='shell':server.control('long-strings')
                        if group=='seeds-list':server.control('fresh-dig-home')
                        context=browser.new_context(viewport={"width":1440,"height":900},reduced_motion="reduce")
                        def local_only(route):
                            if route.request.url.startswith(server.url+"/"):
                                route.continue_()
                            else:
                                external.append("external request blocked")
                                route.abort()
                        context.route("**/*",local_only)
                        page=context.new_page()
                        page.add_init_script(CANVAS_PROBE)
                        page.set_default_timeout(15000)
                        page.on("pageerror",lambda error:errors.append(type(error).__name__))
                        route_youtube_stub(page)
                        route_shell(page)
                        try:
                            page.goto(server.url+"/#/dig/new")
                            boot(page,stub=True)
                            previous_alarm=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(RunFailure(f"Journey exceeded {JOURNEY_TIMEOUT_SECONDS}s")))
                            signal.alarm(JOURNEY_TIMEOUT_SECONDS)
                            try:
                                actions[group](page,server,directory,report)
                            finally:
                                signal.alarm(0)
                                signal.signal(signal.SIGALRM,previous_alarm)
                            forbidden=page.evaluate("""() => [...document.querySelectorAll('body *')].flatMap(el=>[
                              el.textContent,el.getAttribute('title'),el.getAttribute('aria-label')]).filter(Boolean)
                              .some(value=>/released on/i.test(value))""")
                            check(report,group,"no misleading released-on copy including accessible names",False,forbidden)
                        except Exception:
                            report["groups"][group]["failure_state"]=page.evaluate("""()=>({
                              shellState:document.getElementById('device')?.dataset.shellState,
                              route:window.store?.getState().ui.route,
                              steps:Object.values(window.store?.getState().steps.byId||{}).map(s=>({id:s.id,status:s.status})),
                              lastRequests:(window.requestTrace||[]).slice(-20)})""")
                            screenshot(page,directory,report,"FAIL-"+group+"-1440",group=group)
                            raise
                        finally:
                            report["groups"][group]["stub_calls"]=page.evaluate("window.__sliceYTStub?.calls || []")
                            report["groups"][group]["requests"]=page.evaluate("window.requestTrace || []")
                            if page.evaluate("!!window.shell"):
                                page.evaluate("shell.destroy()")
                            context.close()
                except Exception as error:
                    observed=str(error) if isinstance(error,RunFailure) else type(error).__name__
                    report["groups"][group]["failure_location"]=[{"function":frame.name,"line":frame.lineno}
                      for frame in traceback.extract_tb(error.__traceback__) if frame.filename==str(Path(__file__).resolve())]
                    check(report,group,"journey completed","completed",observed)
                check(report,group,"browser has no uncaught errors",[],errors)
                check(report,group,"stub browser has no real external calls",[],external)
                browser.close()
                check(report,None,"browser closed",False,browser.is_connected())
                checks=report["groups"][group]["checks"]
                report["groups"][group]["status"]="PASS" if checks and all(c["status"]=="PASS" for c in checks) else "FAIL"
                print("PLAYTHROUGH GROUP: " + group + " " + report["groups"][group]["status"],flush=True)
                write_evidence(directory,report)
        finally:
            if browser and browser.is_connected():
                browser.close()


def write_evidence(directory, report):
    report["journeys"]={}
    for name,group in JOURNEYS.items():
        groups=JOURNEY_GROUPS.get(name,(group,))
        selected=report["source"]=="STUB" and all(g in report["selected_groups"] for g in groups)
        statuses=[report["groups"][g]["status"] for g in groups]
        status=("FAIL" if "FAIL" in statuses else "GAP" if "GAP" in statuses else "PASS") if selected else "NOT SELECTED"
        report["journeys"][name]={"group":group,"groups":list(groups),"status":status,
          "paths":[p for g in groups for p in report["groups"][g].get("paths",[])],
          "trace":", ".join("groups."+g for g in groups)}
    failures = sum(report["groups"][name]["status"] != "PASS" for name in report["selected_groups"])
    failures += sum(item["status"] == "FAIL" for item in report["framework"]["checks"])
    report["failure_count"] = failures
    report["result"] = "ALL PASS" if failures == 0 else f"{failures} FAIL"
    (directory / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Slice playthrough", "", f"Result: {report['result']}",
             f"Source: {report['source']}; browser: {report['browser_mode']}",
             "Selected groups: " + ", ".join(report["selected_groups"]),
             "Scope: " + ("selected groups only" if report["filtered"] else "real-embed smoke" if report["source"]=="LIVE" else "all stub playthrough groups") + "; STUB and LIVE evidence are independent.", "",
             "## Stub evidence" if report["source"] == "STUB" else "## Live evidence", ""]
    lines += ["| Journey | Group | Result | Measurements / DB / event trace | Screenshots |",
              "|---|---|---|---|---|"]
    if report["source"]=="STUB":
        for name,journey in report["journeys"].items():
            if journey["group"] in report["selected_groups"]:
                paths=" ".join(f"[{path}]({path})" for path in journey["paths"])
                lines.append(f"| {name} | {', '.join(journey['groups'])} | {journey['status']} | evidence.json: {journey['trace']} | {paths} |")
    else:
        for name in ("LIVE01","LIVE02"):
            group=report["groups"]["player"]
            paths=" ".join(f"[{p}]({p})" for p in group.get("paths",[]) if p.startswith(name))
            lines.append(f"| {name} | player | {'PASS' if name in group and group['status']=='PASS' else 'FAIL'} | evidence.json: groups.player.{name} | {paths} |")
    lines += [""]
    for name in GROUPS:
        group = report["groups"][name]
        if name not in report["selected_groups"]:
            continue
        lines.append(f"- {name}: {group['status']} — {group['expected']}; observed: {group['observed']}")
        for item in group["checks"]:
            lines.append(f"  - {item['status']} {item['name']}: expected {json.dumps(item['expected'])}; observed {json.dumps(item['observed'])}")
        for path in group.get("paths", []):
            lines.append(f"  - [{path}]({path})")
    lines += ["", "## Framework", ""]
    for item in report["framework"]["checks"]:
        lines.append(f"- {item['status']} {item['name']}: expected {json.dumps(item['expected'])}; observed {json.dumps(item['observed'])}")
    lines += ["", "## Limitations", ""] + ["- " + item for item in report["limitations"]]
    lines += ["", "[Exact traces, geometry and DB hashes](evidence.json)", ""]
    (directory / "report.md").write_text("\n".join(lines))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated groups: " + ",".join(GROUPS))
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts" / "slice-ux",
                        help="Evidence directory; --live writes its independent bundle under live/")
    parser.add_argument("--db", type=Path, default=Path.home() / ".cosine-digger" / "taste.db")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    requested = list(dict.fromkeys(part.strip() for part in args.only.split(","))) if args.only is not None else (["player"] if args.live else list(GROUPS))
    selected = [name for name in GROUPS if name in requested]
    report = {"schema_version": 2, "source": "LIVE" if args.live else "STUB",
              "browser_mode": "sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome",
              "selected_groups": selected, "filtered": args.only is not None, "groups": {}, "framework": {"checks": []},
              "limitations": ["Stub player timing is deterministic API-stub evidence; real embeds require the separate --live invocation."]}
    if not args.live:
        report["limitations"].append("Both browser modes use a CDP CSS viewport/DPR surrogate, not true browser zoom; the orchestrator checks actual 200% zoom by hand in a visible installed-Chrome window.")
    commit=subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True)
    report["implementation"]={"commit":commit.stdout.strip() if commit.returncode==0 else "unavailable",
      "sha256":{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                for path in ("scripts/slice_playthrough.py","tests/test_slice_playthrough.py")}}
    for name in GROUPS:
        report["groups"][name] = {"status": "GAP", "selected": name in selected, "source": report["source"],
                                   "expected": "LIVE01/02 real embed" if args.live else "approved journey checks",
                                   "observed": "not selected" if name not in selected else "not run", "checks": []}
    try:
        directory = args.artifacts.expanduser().resolve()
        if args.live:directory=directory/'live'
        directory.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        print("Evidence output unavailable")
        print("PLAYTHROUGH: 1 FAIL")
        return 1
    try:
        if not selected or any(name not in GROUPS for name in requested):
            raise RunFailure("Unknown or empty playthrough group selection")
        if args.live and os.environ.get("SLICE_CHROMIUM_EXE"):
            raise RunFailure("Live mode refuses sandbox-shell; installed Chrome is required for H.264")
        if args.live:
            if selected != ["player"]:
                raise RunFailure("Live mode supports only the player smoke group")
            report["limitations"].append("Persisted temporary smoke picks are embed evidence, not live recommendation evidence. YouTube cookies/Premium and regional availability remain controlled by YouTube.")
            with sandbox_server(args.db.expanduser(), report, live=True, env_file=args.env_file) as server:
                live_player(server,directory,report)
        else:
            counts_at(args.db.expanduser())  # Fail safely before launching a browser.
            browser_groups(args.db.expanduser(), directory, selected, report)
    except Exception as error:
        observed = str(error) if isinstance(error, RunFailure) else type(error).__name__
        check(report, None, "runner completed", "completed", observed)
    for name in selected:
        group = report["groups"][name]
        if group["status"] != "GAP":
            group["observed"] = f"{sum(c['status'] == 'PASS' for c in group['checks'])}/{len(group['checks'])} checks passed"
        elif name in selected:
            group["status"] = "FAIL"
            group["observed"] = "runner stopped before journey group"
    try:
        failures = write_evidence(directory, report)
    except (OSError, ValueError):
        print("Evidence output unavailable")
        print("PLAYTHROUGH: 1 FAIL")
        return 1
    print("PLAYTHROUGH: " + report["result"])
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--fixture-cli":
        fixture_cli(sys.argv[2:])
    elif len(sys.argv)>1 and sys.argv[1]=="--live-smoke-db":
        print(json.dumps(seed_live_smoke(Path(sys.argv[2]))))
    else:
        raise SystemExit(main())
