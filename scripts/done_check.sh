#!/bin/sh
# Completion check. PASS / FAIL / PENDING apply to the named scope; SKIP
# explicitly identifies evidence outside it. Offline/local success exits 0.
# --live also runs checks that call cosine.club, Exa and YouTube Music. Live
# checks use a SQLite backup of the taste DB and write only to
# playlists named "Digger Test ...", never to "Digger Daily"/"Digger Weekly".
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 2
export UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/cosine-digger-uv-cache}"
export PYTHONDONTWRITEBYTECODE=1

# The private second invocation receives credentials only in its environment,
# never through shell output, a settings/auth copy or a temporary script.
if [ "${1:-}" != --sandbox ]; then
  LIVE=0
  case "$#:${1:-}" in
    0:) ;;
    1:--live) LIVE=1 ;;
    *) echo 'Usage: sh scripts/done_check.sh [--live]' >&2; exit 2 ;;
  esac
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/done-check.XXXXXX") || exit 2
  WORK=$(cd "$WORK" && pwd -P)
  trap 'rm -rf "$WORK"' 0
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  uv run python - "$ROOT" "$WORK" "$LIVE" <<'PY'
from contextlib import closing
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

from cosine_digger.config import ENV_VARS, load_config

root, work, live = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
try:
    cfg = load_config()  # Reads the original settings; never writes them.
    home = work / "home"
    home.mkdir(mode=0o700)
    (home / "generated-library").mkdir()
    (work / "tmp").mkdir()
    db = home / "taste.db"
    source = cfg.db_path.expanduser().resolve()
    with closing(sqlite3.connect(db)) as destination:
        if source.is_file():
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as original:
                original.backup(destination)
    environment = dict(os.environ)
    environment.update({"COSINE_DIGGER_HOME": str(home), "COSINE_DIGGER_DB": str(db),
                        "COSINE_DIGGER_MUSIC_DIR": str(home / "generated-library"),
                        "COSINE_DIGGER_YTMUSIC_AUTH": str(cfg.ytmusic_auth_path if live else home / "unused-auth.json"),
                        "TMPDIR": str(work / "tmp"),
                        "COSINE_DIGGER_HOST": "127.0.0.1"})
    for key in ("cosine_token", "discogs_token", "exa_api_key"):
        environment[ENV_VARS[key]] = (getattr(cfg, key) or "") if live else ""
    for key in ("daily_time", "weekly_day", "weekly_time", "daily_size", "weekly_size"):
        environment[ENV_VARS[key]] = str(getattr(cfg, key))
    for key in ("COSINE_DIGGER_TEST_MODE", "COSINE_DIGGER_FIXTURE", "COSINE_DIGGER_ALLOWED_HOSTS"):
        environment.pop(key, None)
    environment["PYTEST_ADDOPTS"] = (environment.get("PYTEST_ADDOPTS", "") +
        " -o " + shlex.quote("cache_dir=" + str(work / "pytest-cache")) +
        " --basetemp=" + shlex.quote(str(work / "tmp" / "pytest-temp")))
except (OSError, ValueError, sqlite3.Error) as error:
    print(f"DONE_CHECK: FAIL sandbox setup ({type(error).__name__})", file=sys.stderr)
    raise SystemExit(1) from None
raise SystemExit(subprocess.call(["sh", str(root / "scripts/done_check.sh"), "--sandbox", str(work),
                                 "1" if live else "0"], cwd=root, env=environment))
PY
  exit $?
fi
# Internal arguments must refer to the app home and DB just allocated above.
[ "$#" -eq 3 ] || exit 2
WORK=$2
LIVE=$3
[ "${COSINE_DIGGER_HOME:-}" = "$WORK/home" ] && [ "${COSINE_DIGGER_DB:-}" = "$WORK/home/taste.db" ] || exit 2
case "$LIVE" in 0) SCOPE=offline/local;; 1) SCOPE=live;; *) exit 2;; esac
SPID=
cleanup() {
  if [ -n "$SPID" ]; then kill "$SPID" 2>/dev/null || :; wait "$SPID" 2>/dev/null || :; fi
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

PASS=0; FAIL=0; PEND=0; SKIP=0
report() {  # status name detail
  printf '%-8s %-28s %s\n' "$1" "$2" "$3"
  case "$1" in PASS) PASS=$((PASS+1));; FAIL) FAIL=$((FAIL+1));; SKIP) SKIP=$((SKIP+1));; *) PEND=$((PEND+1));; esac
}
has_cmd() { uv run cosine-digger "$@" --help >/dev/null 2>&1; }
cd_json() { uv run cosine-digger "$@" 2>"$WORK/stderr"; }
why() {  # why a command failed: what its JSON says, else its last stderr line
  r=$(printf '%s' "$1" | python3 -c '
import json, sys
d = json.load(sys.stdin); e = d.get("export") or {}
n = lambda k: len(e.get(k) or [])
print(d.get("error") or d.get("export_error") or "verified=%s added=%d/%d missing=%d failed=%d" % (
    e.get("verified"), n("added"), n("requested"), n("missing"), n("failed")))' 2>/dev/null)
  printf '%s' "${r:-$(tail -1 "$WORK/stderr")}"
}

# Fixtures supply their own credentials/config; retain only the sandbox home/DB.
# The surrounding CLI/live checks keep their configuration after this subshell.
run_tests() (
  unset COSINE_DIGGER_YTMUSIC_AUTH COSINE_DIGGER_MUSIC_DIR \
    COSINE_DIGGER_DAILY_TIME COSINE_DIGGER_WEEKLY_DAY COSINE_DIGGER_WEEKLY_TIME \
    COSINE_DIGGER_DAILY_SIZE COSINE_DIGGER_WEEKLY_SIZE COSINE_DIGGER_HOST COSINE_DIGGER_PORT
  # A fixture clears the DB override; also hide any real legacy user-home DB.
  HOME="$WORK/test-user-home" COSINE_API_TOKEN= DISCOGS_TOKEN= EXA_API_KEY= \
    "$@"
)

# 1. Test suite
mkdir -p "$WORK/test-user-home"
if out=$(run_tests uv run --with playwright pytest -q 2>&1); then
  report PASS tests "$(printf '%s\n' "$out" | tail -1)"
else
  report FAIL tests "$(printf '%s\n' "$out" | tail -1)"
  printf '%s\n' "$out" | sed -n '/^FAILED /p'
fi

# 2. Obsidian / Apple Music removed
# Strip only the approved raw MP4 label atoms, not the lines or tag module.
# Other integration references on the same line still fail this check.
hits=$(grep -rniE --exclude-dir=__pycache__ 'obsidian|apple music|itunes|music\.apple|NOTES_DIR' src skills scripts README.md .claude/skills 2>/dev/null |
  grep -v 'scripts/done_check.sh:' | grep -v '/publish-report/' |
  sed -E 's/----:com\.apple\.iTunes:(LABEL|Label|label|publisher)([^[:alnum:]_.:]|$)/\2/g' |
  grep -iE 'obsidian|apple music|itunes|music\.apple|NOTES_DIR' | cut -d: -f1 | sort -u)
if [ -z "$hits" ]; then report PASS removed-obsidian-apple "no integration references (MP4 label atoms allowed)"
else report FAIL removed-obsidian-apple "$(printf '%s' "$hits" | tr '\n' ' ')"; fi

# 3. Plex moved out
hits=$(grep -rni --exclude-dir=__pycache__ 'plex' src tests scripts skills 2>/dev/null |
  grep -v 'scripts/done_check.sh:' |
  sed 's/Any folder of tagged audio files works; Plex is optional\.//g' |
  grep -i 'plex' | cut -d: -f1 | sort -u)
if [ -z "$hits" ]; then report PASS plex-moved-out "no integration references (approved optional-importer help allowed)"
else report FAIL plex-moved-out "$(printf '%s' "$hits" | tr '\n' ' ')"; fi

# 4. plex-shelves repo tests (the optional Plex importer lives in its own repo).
PLEX=${DONE_CHECK_EXTERNAL_TEST_DIR:-$HOME/Projects/plex-shelves}
if [ -f "$PLEX/pyproject.toml" ]; then
  if out=$(cd "$PLEX" && run_tests uv run pytest -q 2>&1); then report PASS plex-shelves-tests "$(printf '%s\n' "$out" | tail -1)"
  else report FAIL plex-shelves-tests "$(printf '%s\n' "$out" | tail -1)"; fi
else report SKIP plex-shelves-tests "external repository not found at $PLEX; separate evidence required"; fi

# 5. doctor: required checks ok; optional ones (Exa key, music folder) may warn, or be off when not set up
if has_cmd doctor; then
  doctor_status=0
  if [ "$LIVE" -eq 1 ]; then out=$(cd_json doctor --json) || doctor_status=$?
  else out=$(cd_json doctor --json --offline) || doctor_status=$?; fi
  if [ "$doctor_status" -le 1 ] && bad=$(printf '%s' "$out" | python3 -c '
import json, sys
optional = {"exa_api_key", "music_dir", "discogs_token"}
d = json.load(sys.stdin)
checks = d["checks"] if sys.argv[1] == "1" else {"db": d["checks"]["db"]}
print(",".join(k for k, v in checks.items()
               if v["status"] != "ok" and not (k in optional and v["status"] in ("warn", "off"))))' "$LIVE"); then
    if [ -z "$bad" ]; then report PASS doctor "scope=$SCOPE (offline checks DB; credentials/login require --live)"
    else report FAIL doctor "not ok: $bad"; fi
  else report FAIL doctor "$(tail -1 "$WORK/stderr")"; fi
else report PENDING doctor "command missing"; fi

# 6-7. Playlist dry-run invariants (count range, nothing owned or served)
plan_check() {  # kind min max
  if ! has_cmd playlist "$1"; then report PENDING "playlist-$1-dry-run" "command missing"; return; fi
  if [ $LIVE -eq 0 ]; then report SKIP "playlist-$1-dry-run" "needs --live (cosine.club); count goal $2-$3 not checked"; return; fi
  if out=$(cd_json playlist "$1" --dry-run --json); then
    verdict=$(printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin); lo, hi = int(sys.argv[1]), int(sys.argv[2])
tracks = d["tracks"]; checks = d.get("checks", {})
problems = []
if not lo <= len(tracks) <= hi: problems.append(f"count={len(tracks)} expected={lo}-{hi}; keep a track or scan a music folder; browsing has zero feed weight")
for k in ("owned", "served", "muted", "known", "missing_why", "no_video", "duplicates"):
    if checks.get(k, 1): problems.append(f"{k}={checks.get(k)}")
print("ok count=%d" % len(tracks) if not problems else " ".join(problems))' "$2" "$3")
    case "$verdict" in ok*) report PASS "playlist-$1-dry-run" "$verdict";; *) report FAIL "playlist-$1-dry-run" "$verdict";; esac
  else report FAIL "playlist-$1-dry-run" "$(why "$out")"; fi
}
plan_check daily 15 25
plan_check weekly 40 60

# 8. Live export to test playlists, verified by re-read
if has_cmd playlist daily && [ $LIVE -eq 1 ]; then
  for kind in daily weekly; do
    if out=$(cd_json playlist "$kind" --json --name "Digger Test $kind"); then
      v=$(printf '%s' "$out" | python3 -c 'import json,sys; r=json.load(sys.stdin)["export"]; print("ok" if r["verified"] and not r["failed"] else "verified=%s failed=%d" % (r["verified"], len(r["failed"])))')
      [ "$v" = ok ] && report PASS "live-export-$kind" "verified in YouTube Music" || report FAIL "live-export-$kind" "$v"
    else report FAIL "live-export-$kind" "$(why "$out")"; fi
  done
elif [ "$LIVE" -eq 0 ]; then report SKIP live-export "Daily/Weekly Digger Test exports require --live; no YouTube Music writes"
else report PENDING live-export "playlist command missing"; fi

# 9. Skill path: seeds -> playlist URL in <= 120 s
if has_cmd quick && [ $LIVE -eq 1 ]; then
  # Daily/Weekly may have spent this minute's cosine.club allowance. The
  # override permits fast offline script tests; the live default is 65 s.
  cooldown=${DONE_CHECK_COSINE_COOLDOWN_SECONDS:-65}
  if sleep "$cooldown"; then
    detail="after $cooldown s cosine.club cool-down"
    start=$(date +%s)
    if out=$(cd_json quick "Vladislav Delay - Ranta" "Conoco - Koski" "Humbros - Ressac" --playlist "Digger Test quick" --json); then
      secs=$(( $(date +%s) - start ))
      url=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["playlist_url"] or "")')
      if [ -n "$url" ] && [ "$secs" -le 120 ]; then report PASS skill-path "${secs}s $url ($detail)"
      else report FAIL skill-path "${secs}s url=${url:-none} ($detail)"; fi
    else report FAIL skill-path "$(why "$out") ($detail)"; fi
  else report FAIL skill-path "cosine.club cool-down failed"; fi
elif [ "$LIVE" -eq 0 ]; then report SKIP skill-path "live quick/playlist URL requires --live"
else report PENDING skill-path "quick command missing"; fi

# 10. The current app serves the shell and a digs list, including an empty list.
if has_cmd serve; then
  PORT=$(uv run python - <<'PY'
import socket

while True:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port != 8777:
        print(port)
        break
PY
)
  uv run cosine-digger serve --host 127.0.0.1 --port "$PORT" --no-scheduler >"$WORK/serve.log" 2>&1 &
  SPID=$!
  if out=$(uv run python - "$PORT" "$ROOT/src/cosine_digger/web/static/index.html" <<'PY'
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = "http://127.0.0.1:" + sys.argv[1]
deadline = time.monotonic() + 20
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit("Server did not answer /api/health within 20 seconds")
    try:
        with opener.open(base + "/api/health", timeout=min(1, remaining)) as response:
            assert json.load(response)["ok"] is True
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
with opener.open(base + "/api/digs", timeout=5) as response:
    digs = json.load(response)["digs"]
    assert isinstance(digs, list), "Missing digs list"
with opener.open(base + "/", timeout=5) as response:
    page = response.read()
assert page == Path(sys.argv[2]).read_bytes(), "Served shell differs from current source"

class Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.ids.add(attrs.get("id"))
        if tag == "script":
            self.scripts.append((attrs.get("type"), attrs.get("src")))

shell = Shell()
shell.feed(page.decode("utf-8"))
assert {"player-region", "player-frame", "player", "new-dig-region", "key-strip"} <= shell.ids, "Missing slice shell/player reservation"
assert shell.scripts == [("module", "/static/slice.js")], "Wrong app entry"
print(f"current shell/player reservation; digs={len(digs)}")
PY
  ); then report PASS app-shell "$out"
  else report FAIL app-shell "health, digs list or current shell check failed"; fi
  kill "$SPID" 2>/dev/null || :; wait "$SPID" 2>/dev/null || :
  SPID=
else report PENDING app-shell "serve command missing"; fi

# 11. Fresh desktop evidence from the stub playthrough, never stale phone/map shots.
# Only these groups produce the screenshots below; S25 owns full/live UX proof.
SHOTS="$WORK/slice-ux"
if out=$(uv run scripts/slice_playthrough.py --db "$COSINE_DIGGER_DB" --artifacts "$SHOTS" \
    --only shell,seeds-list,feeds,setup 2>&1); then
  if evidence=$(uv run python - "$SHOTS" <<'PY'
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
report = json.loads((directory / "evidence.json").read_text())
assert report["source"] == "STUB", "Desktop evidence must identify stub calls"
for group in ("shell", "seeds-list", "feeds", "setup"):
    assert report["groups"][group]["status"] == "PASS", f"Missing passing {group} evidence"
required = [directory / (name + ".png") for name in ("C01-1440", "SS36-1440", "SS37-1440", "shell-zoom-layout-surrogate")]
for width in (1100, 1280, 1440):
    shots = list(directory.glob(f"C08-*-{width}.png"))
    assert shots, f"Missing C08 desktop overlays at {width}"
    required.extend(shots)
for path in required:
    assert path.is_file() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), f"Missing screenshot {path.name}"
zoom = report["groups"]["shell"]["zoom"]
assert zoom["capture_metrics"] == zoom["metrics"], "Zoom capture changed measured viewport"
assert zoom["geometry"]["player"]["width"] >= 200 and zoom["geometry"]["player"]["height"] >= 200
assert zoom["geometry"]["key-strip"]["bottom"] <= zoom["metrics"]["height"]
print(f"STUB; browser={report['browser_mode']}; C01/C08/SS36/SS37 desktop PNGs; zoom={zoom['method']}")
PY
  ); then report PASS app-screenshots "$evidence"
  else report FAIL app-screenshots "required desktop PNGs/zoom measurements missing or invalid"; fi
else report FAIL app-screenshots "$(printf '%s\n' "$out" | tail -1)"; fi

# 12. Wheel installs into a clean venv
if [ -f scripts/smoke_wheel.sh ]; then
  if out=$(sh scripts/smoke_wheel.sh 2>&1) && printf '%s' "$out" | grep -q 'SMOKE OK'; then report PASS wheel-smoke "SMOKE OK"
  else report FAIL wheel-smoke "$(printf '%s\n' "$out" | tail -1)"; fi
else report PENDING wheel-smoke "scripts/smoke_wheel.sh missing"; fi

# 13. Docker image builds and serves
if [ -f scripts/smoke_docker.sh ]; then
  if out=$(sh scripts/smoke_docker.sh 2>&1) && printf '%s' "$out" | grep -q 'SMOKE OK'; then report PASS docker-smoke "SMOKE OK"
  else report FAIL docker-smoke "$(printf '%s\n' "$out" | tail -1)"; fi
else report PENDING docker-smoke "scripts/smoke_docker.sh missing"; fi

echo
echo 'Browser evidence above is STUB; real embeds and actual 200% browser zoom require the separate S25 evidence.'
if [ "$LIVE" -eq 0 ]; then
  echo 'OFFLINE SCOPE: live doctor validation, Daily/Weekly count goals, Digger Test exports and live quick URL were skipped.'
fi
if [ $FAIL -eq 0 ] && [ $PEND -eq 0 ]; then
  echo "DONE_CHECK: ALL PASS ($PASS checks, scope=$SCOPE, $SKIP SKIPPED)"
  exit 0
fi
echo "DONE_CHECK: $FAIL FAIL, $PEND PENDING, $PASS PASS (scope=$SCOPE, $SKIP SKIPPED)"
exit 1
