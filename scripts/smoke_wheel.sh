#!/bin/sh
# Exercise only the installed wheel, from outside the checkout.
set -eu

STEP=setup
TMP=
SERVER_PID=
CLI=
cleanup() {
    status=$?
    trap - 0 HUP INT TERM
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || :
        wait "$SERVER_PID" 2>/dev/null || :
    fi
    if [ -n "$CLI" ] && [ -x "$CLI" ]; then
        "$CLI" stop >"$TMP/cleanup.log" 2>&1 || :
    fi
    if [ "$status" -ne 0 ]; then
        if [ -n "$TMP" ]; then
            cat "$TMP/step.log" "$TMP/serve.log" "$TMP/cleanup.log" \
                "$TMP/home/logs/start.log" "$TMP/home/logs/digger.log" >&2 2>/dev/null || :
        fi
        printf 'SMOKE FAIL: %s\n' "$STEP" >&2
        status=1
    fi
    if [ -n "$TMP" ]; then rm -rf "$TMP"; fi
    exit "$status"
}
trap cleanup 0
trap 'exit 1' HUP INT TERM
step() {
    STEP=$1
    printf 'SMOKE: %s\n' "$STEP"
    : >"$TMP/step.log"
}

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/cosine-wheel.XXXXXX")
# Keep build caches, temporary files, app state and logs in the cleanup tree.
TMP=$(cd "$TMP" && pwd)
export TMPDIR="$TMP/tmp" UV_CACHE_DIR="$TMP/cache" PYTHONDONTWRITEBYTECODE=1
export HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config"
export XDG_DATA_HOME="$TMP/data" XDG_CACHE_HOME="$TMP/cache"
mkdir -p "$TMPDIR" "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"

cd "$ROOT"
if [ -n "${SMOKE_WHEEL:-}" ]; then
    # The release workflow passes the wheel it publishes, so that exact file is tested.
    step 'copy release wheel'
    mkdir -p "$TMP/dist"
    cp "$SMOKE_WHEEL" "$TMP/dist/"
else
    step 'build wheel'
    uv build --wheel --no-sources --out-dir "$TMP/dist" >"$TMP/step.log" 2>&1
fi

step 'create clean virtualenv'
uv venv "$TMP/venv" >"$TMP/step.log" 2>&1

step 'install wheel'
uv pip install --python "$TMP/venv/bin/python" "$TMP"/dist/*.whl >"$TMP/step.log" 2>&1

cd "$TMP"
unset PYTHONPATH
export COSINE_DIGGER_HOME="$TMP/home" COSINE_DIGGER_DB="$TMP/home/taste.db"
export COSINE_DIGGER_YTMUSIC_AUTH="$TMP/home/unused-auth.json"
export COSINE_DIGGER_MUSIC_DIR="$TMP/home/generated-library"
export COSINE_API_TOKEN= DISCOGS_TOKEN= EXA_API_KEY=
unset COSINE_DIGGER_TEST_MODE COSINE_DIGGER_FIXTURE COSINE_DIGGER_ALLOWED_HOSTS \
  COSINE_DIGGER_DAILY_TIME COSINE_DIGGER_WEEKLY_DAY COSINE_DIGGER_WEEKLY_TIME \
  COSINE_DIGGER_DAILY_SIZE COSINE_DIGGER_WEEKLY_SIZE COSINE_DIGGER_HOST COSINE_DIGGER_PORT
PYTHON="$TMP/venv/bin/python"
CLI="$TMP/venv/bin/cosine-digger"
PORT=$("$PYTHON" - "${DIGGER_TEST_PORTS-}" <<'PY'
import socket
import sys

if sys.argv[1]:
    first, last = map(int, sys.argv[1].split("-"))
    assert 1 <= first <= last <= 65535, "Invalid DIGGER_TEST_PORTS range"
    for port in range(first, last + 1):
        if port == 8777:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        print(port)
        raise SystemExit(0)
    raise SystemExit("No free port in DIGGER_TEST_PORTS (8777 is reserved)")
while True:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        selected = sock.getsockname()[1]
    if selected != 8777:
        print(selected)
        break
PY
)

step 'CLI help'
"$CLI" --help >"$TMP/step.log" 2>&1

step 'offline doctor JSON'
# Missing credentials are expected in a fresh app home (doctor exits 1).
doctor_status=0
"$CLI" doctor --json --offline >"$TMP/doctor.json" 2>"$TMP/step.log" || doctor_status=$?
[ "$doctor_status" -le 1 ]
"$PYTHON" -c 'import json, sys; assert isinstance(json.load(open(sys.argv[1]))["checks"], dict)' "$TMP/doctor.json"

step 'start local app'
# Fail on an occupied port rather than accidentally testing another server.
"$PYTHON" -c 'import socket, sys; s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$PORT"
"$CLI" serve --host 127.0.0.1 --port "$PORT" --no-scheduler >"$TMP/serve.log" 2>&1 &
SERVER_PID=$!

step 'health JSON (up to 20 seconds)'
"$PYTHON" - "$PORT" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 20
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit("App did not become healthy within 20 seconds")
    try:
        with opener.open(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=min(1, remaining)) as response:
            assert json.load(response)["ok"] is True
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
PY
kill -0 "$SERVER_PID"

step 'empty digs and exact installed shell/modules/styles'
"$PYTHON" - "$PORT" "$ROOT/src/cosine_digger/web/static" <<'PY'
import hashlib
from html.parser import HTMLParser
from importlib.resources import files
import json
from pathlib import Path
import re
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f"http://127.0.0.1:{sys.argv[1]}"
with opener.open(base + "/api/digs", timeout=5) as response:
    digs = json.load(response)["digs"]
    assert isinstance(digs, list) and digs == [], "Fresh home must list zero digs"
with opener.open(base + "/", timeout=5) as response:
    served = response.read()
source = Path(sys.argv[2])
installed = files("cosine_digger.web").joinpath("static")
assert installed.joinpath("index.html").is_file(), "Wheel is missing static/index.html"
assert installed.joinpath("index.html").read_bytes() == (source / "index.html").read_bytes(), "Installed index differs from source"
# The served page names each code file by its content: an import map before slice.js and
# ?v=<content hash> on stylesheets. Without those it is the source index, byte for byte.
importmap = re.search(rb'<script type="importmap">(.*?)</script>\n  ', served)
assert importmap, "Served index has no import map"
versions = json.loads(importmap[1])["imports"]
page = re.sub(rb'\?v=[0-9a-f]{12}"', b'"', served[:importmap.start()] + served[importmap.end():])
assert page == (source / "index.html").read_bytes(), "Served index differs from source beyond versioned names"
version = lambda name: hashlib.sha256((source / name).read_bytes()).hexdigest()[:12]

class Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.assets = set()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "script":
            self.scripts.append((attrs.get("type"), attrs.get("src")))
        if tag == "link":
            self.assets.add(attrs.get("href") or attrs.get("data-href"))
        if tag == "a":
            self.links.add(attrs.get("href"))

shell = Shell()
shell.feed(page.decode("utf-8"))
assert {"player-region", "player-frame", "player", "new-dig-region", "dig-region", "key-strip", "feeds-region", "setup-region"} <= shell.ids, "Missing slice shell/player reservation"
assert "#/dig/new" in shell.links, "Missing new-dig route"
assert shell.scripts == [("module", "/static/slice.js")], "Shell must load only the slice module entry"
components = "player seedbox steplist trail inspector moves stepmap territory keys feeds setup".split()
modules = [name + ".js" for name in ["slice", "state", "api", "dom", *components]]
styles = [name + ".css" for name in ["app", *components]]
assert {"/static/" + name for name in styles} <= shell.assets, "Missing declared stylesheets"
assets = set(modules + styles) | {url.removeprefix("/static/") for url in shell.assets if url and url.startswith("/static/")}
for name in sorted(assets):
    asset = installed.joinpath(name)
    assert asset.is_file(), f"Wheel is missing {name}"
    expected = (source / name).read_bytes()
    assert asset.read_bytes() == expected, f"Installed {name} differs from source"
    with opener.open(base + "/static/" + name, timeout=5) as response:
        assert response.read() == expected, f"Served {name} differs from installed asset"
        if name.endswith(".js"):
            assert response.headers.get_content_type() in {"text/javascript", "application/javascript"}, f"Bad module MIME type: {name}"
        elif name.endswith(".css"):
            assert response.headers.get_content_type() == "text/css", f"Bad stylesheet MIME type: {name}"
for name in styles:
    assert f'"/static/{name}?v={version(name)}"'.encode() in served, f"Stylesheet {name} isn't named by its content"
for name in modules[1:]:  # slice.js is the page's own script; the import map names every other module
    assert versions.get("/static/" + name) == f"/static/{name}?v={version(name)}", f"Import map misses {name} at its content version"
    with opener.open(base + versions["/static/" + name], timeout=5) as response:
        assert response.read() == (source / name).read_bytes(), f"Versioned {name} differs from source"
print(f"SMOKE PASS: exact index, {len(modules)} ES modules, {len(styles)} stylesheets, content-named, player/new-dig shell, empty digs")
PY

step 'stop foreground serve'
kill "$SERVER_PID"
wait "$SERVER_PID" || :
SERVER_PID=

step 'installed start opens the browser URL'
cat >"$TMP/browser" <<'SH'
#!/bin/sh
printf '%s\n' "$1" >>"$BROWSER_URLS"
exit 0
SH
chmod +x "$TMP/browser"
export BROWSER="\"$TMP/browser\" %s" BROWSER_URLS="$TMP/browser-urls"
URL="http://127.0.0.1:$PORT"
"$CLI" start --port "$PORT" >"$TMP/step.log" 2>&1
[ "$(sed -n '1p' "$TMP/step.log")" = "Digger is running at $URL" ]
[ "$(cat "$BROWSER_URLS")" = "$URL" ]

step 'installed status reports running'
"$CLI" status >"$TMP/step.log" 2>&1
case "$(sed -n '1p' "$TMP/step.log")" in
    'Digger is running at '*) ;;
    *) exit 1 ;;
esac

step 'installed stop preserves digs and settings'
"$CLI" stop >"$TMP/step.log" 2>&1
[ "$(tail -n 1 "$TMP/step.log")" = 'Digger stopped. Your digs and settings are saved.' ]

step 'installed status exits 3 after stop'
status_code=0
"$CLI" status >"$TMP/step.log" 2>&1 || status_code=$?
[ "$status_code" -eq 3 ]

step 'nothing listens after stop'
"$PYTHON" - "$PORT" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(1)
    assert sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0, "Port is still listening"
PY

printf 'SMOKE OK\n'
