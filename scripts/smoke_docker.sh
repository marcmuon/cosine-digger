#!/bin/sh
set -eu

if ! docker info >/dev/null 2>&1; then
    echo 'SMOKE SKIP: docker unavailable'
    exit 1
fi

CONTAINER=digger-smoke-$$
VOLUME=digger-smoke-$$
IMAGE=cosine-digger:smoke-$$
WORK=
STEP=setup
cleanup() {
    result=$?
    trap - 0 HUP INT TERM
    if [ "$result" -ne 0 ]; then
        printf 'SMOKE FAIL: %s\n' "$STEP" >&2
        docker logs --tail 30 "$CONTAINER" >&2 || true
    fi
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
    exit "$result"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/digger-smoke.XXXXXX")
# Buildx writes activity/state during builds; keep it inside the cleanup tree.
export BUILDX_CONFIG="$WORK/buildx"
command -v curl >/dev/null
command -v python3 >/dev/null
PORT=$(python3 - "${SMOKE_PORT:-0}" <<'PY'
import socket
import sys

port = int(sys.argv[1])
assert port != 8777, "Port 8777 is reserved"
while True:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        selected = sock.getsockname()[1]
    if selected != 8777:
        print(selected)
        break
PY
)
BASE_URL=http://127.0.0.1:$PORT

STEP=build
docker build -t "$IMAGE" .
echo 'SMOKE PASS: build'

STEP=start
docker run -d --name "$CONTAINER" -p "127.0.0.1:$PORT:8765" \
    -v "$VOLUME:/data" "$IMAGE" \
    cosine-digger serve --host 0.0.0.0 --port 8765 --no-scheduler >/dev/null
echo 'SMOKE PASS: start'

STEP=health
deadline=$(( $(date +%s) + 60 ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl --noproxy '*' -fsS --max-time 1 "$BASE_URL/api/health" >"$WORK/health.json" 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
[ "$ready" -eq 1 ]
python3 -c 'import json, sys; assert json.load(sys.stdin)["ok"] is True' <"$WORK/health.json"
echo 'SMOKE PASS: health'

STEP=digs
curl --noproxy '*' -fsS --max-time 10 "$BASE_URL/api/digs" >"$WORK/digs.json"
python3 -c 'import json, sys; digs = json.load(sys.stdin)["digs"]; assert isinstance(digs, list) and digs == []' <"$WORK/digs.json"
echo 'SMOKE PASS: empty digs'

STEP=shell-assets
curl --noproxy '*' -fsS --max-time 10 "$BASE_URL/" >"$WORK/page.html"
# Read the installed package, not files bind-mounted from the checkout.
docker exec "$CONTAINER" python -c '
import hashlib, json
from importlib.resources import files
from pathlib import Path

root = Path(str(files("cosine_digger.web").joinpath("static")))
print(json.dumps({path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in root.rglob("*") if path.is_file()}))
' >"$WORK/assets.json"
python3 - "$BASE_URL" "$WORK" "$ROOT/src/cosine_digger/web/static" <<'PY'
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import urllib.request

base, work, source = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
installed = json.loads((work / "assets.json").read_text())
served = (work / "page.html").read_bytes()
assert hashlib.sha256((source / "index.html").read_bytes()).hexdigest() == installed["index.html"], "Installed index differs from source"
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
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for name in sorted(assets):
    expected = (source / name).read_bytes()
    assert hashlib.sha256(expected).hexdigest() == installed[name], f"Installed {name} differs from source"
    with opener.open(base + "/static/" + name, timeout=10) as response:
        assert response.read() == expected, f"Served {name} differs from installed asset"
        if name.endswith(".js"):
            assert response.headers.get_content_type() in {"text/javascript", "application/javascript"}, f"Bad module MIME type: {name}"
        elif name.endswith(".css"):
            assert response.headers.get_content_type() == "text/css", f"Bad stylesheet MIME type: {name}"
for name in styles:
    assert f'"/static/{name}?v={version(name)}"'.encode() in served, f"Stylesheet {name} isn't named by its content"
for name in modules[1:]:  # slice.js is the page's own script; the import map names every other module
    assert versions.get("/static/" + name) == f"/static/{name}?v={version(name)}", f"Import map misses {name} at its content version"
    with opener.open(base + versions["/static/" + name], timeout=10) as response:
        assert response.read() == (source / name).read_bytes(), f"Versioned {name} differs from source"
print(f"SMOKE PASS: exact index, {len(modules)} ES modules, {len(styles)} stylesheets, content-named, player/new-dig shell")
PY

STEP=non-root
uid=$(docker exec "$CONTAINER" id -u)
[ "$uid" -gt 0 ]
echo 'SMOKE PASS: non-root'

STEP=doctor
# A fresh install has no API token, so doctor can exit 1 with valid checks.
doctor_status=0
docker exec "$CONTAINER" cosine-digger doctor --json --offline >"$WORK/doctor.json" || doctor_status=$?
[ "$doctor_status" -le 1 ]
python3 -c 'import json, sys; assert json.load(sys.stdin)["checks"]["db"]["status"] == "ok"' <"$WORK/doctor.json"
echo 'SMOKE PASS: doctor'

echo 'SMOKE OK'
