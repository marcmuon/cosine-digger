"""Read-only checks of the shared application configuration."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .. import storage
from ..client import CosineRequestError, api_base, proxies_allowed
from ..config import AppConfig, ENV_VARS, SECRET_FIELDS, load_config, usable_key
from ..web.connections import classify
from ..youtube import has_login


def register(app: typer.Typer) -> None:
    app.command(name="doctor")(doctor)


def run_checks(cfg: AppConfig, *, offline: bool, fixture_mode: bool | None = None) -> dict:
    """Return the same checks used by the command and local web app."""
    if fixture_mode is None:
        fixture_mode = (os.environ.get("COSINE_DIGGER_TEST_MODE") == "1"
                        and bool(os.environ.get("COSINE_DIGGER_FIXTURE", "").strip()))
    checks = {}

    def check(name: str, status: str, detail: str, field: str | None = None) -> None:
        checks[name] = {"status": status, "detail": detail, "source": cfg.sources.get(field or name, "default")}

    if not cfg.cosine_token:
        check("cosine_token", "fail", "Missing COSINE_API_TOKEN")
    elif not _sendable(cfg.cosine_token):
        check("cosine_token", "fail", "set, but it has a space or line break in it (not sent)")
    elif offline:
        check("cosine_token", "ok", "set (offline; API not checked)")
    elif fixture_mode:
        check("cosine_token", "warn", "set (test fixture; API not checked)")
    else:
        try:
            base = api_base()
            response = httpx.get(
                f"{base}/search",
                params={"q": "test", "limit": 1},
                headers={"Authorization": f"Bearer {cfg.cosine_token}", "Accept": "application/json"},
                timeout=5.0,
                trust_env=proxies_allowed(base),
            )
            if response.is_success:
                check("cosine_token", "ok", "set; API reachable")
            else:
                check("cosine_token", "fail", f"set; API returned HTTP {response.status_code}")
            # The answer as the key screen words it (connected, rejected, busy, server_error), so
            # Setup never reads it from the text above; the web app records connected or rejected
            # as the verdict on the key this call sent.
            answer = classify(response)
            checks["cosine_token"].update(state=answer.state, retry_after_s=answer.retry_after_s)
        except (httpx.HTTPError, ValueError, CosineRequestError):
            # Exception text and response bodies can contain credentials.
            check("cosine_token", "fail", "set; API request failed")
            checks["cosine_token"].update(state="unreachable", retry_after_s=None)

    # Optional items that aren't set up are "off", never "warn": warn means set up but broken.
    for key in ("discogs_token", "exa_api_key"):
        value = getattr(cfg, key)
        if not value:
            check(key, "off", "Not set (optional)")
        elif not _sendable(value):
            check(key, "warn", "set, but it has a space or line break in it (not used)")
        else:
            check(key, "ok", "set")

    try:
        json.loads(cfg.ytmusic_auth_path.read_text())
    except FileNotFoundError:
        check("youtube_login", "off", "Not logged in (optional)", "ytmusic_auth_path")
    except (OSError, ValueError):
        check("youtube_login", "warn", "Auth file unreadable or invalid JSON", "ytmusic_auth_path")
    else:
        if has_login(cfg.ytmusic_auth_path):
            check("youtube_login", "ok", "Auth file holds a saved login (not checked here)", "ytmusic_auth_path")
        else:  # an empty {} file isn't a login (P17)
            check("youtube_login", "off", "Not logged in (optional): the auth file holds no login", "ytmusic_auth_path")

    if cfg.music_dir and cfg.music_dir.is_dir() and os.access(cfg.music_dir, os.R_OK | os.X_OK):
        check("music_dir", "ok", str(cfg.music_dir))
    elif not cfg.music_dir and (library := _library_summary(cfg.db_path)):
        # A library imported from elsewhere (Plex) needs no folder.
        check("music_dir", "ok", f"Not set; your library has {library}")
    elif not cfg.music_dir:
        check("music_dir", "off", "Not set (optional)")
    else:
        check("music_dir", "warn", "Not a readable directory")

    if not cfg.db_path.exists():
        check("db", "warn", f"{cfg.db_path}; exists=false; user_version=unavailable", "db_path")
    else:
        try:
            uri = cfg.db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            check("db", "ok", f"{cfg.db_path}; exists=true; user_version={version}", "db_path")
        except (sqlite3.Error, OSError):
            check("db", "fail", f"{cfg.db_path}; exists=true; user_version=unavailable (cannot read database)", "db_path")

    public_config = {
        key: str(value) if isinstance(value, Path) else value
        for key in ENV_VARS if key not in SECRET_FIELDS
        for value in (getattr(cfg, key),)
    }
    return {"checks": checks, "config": public_config}


def _sendable(value: str) -> bool:
    try:
        return bool(usable_key(value, "key"))
    except ValueError:
        return False


def _library_summary(db_path: Path) -> str | None:
    """"10,808 tracks from Plex" when the library has rows; read-only, None otherwise."""
    if not db_path.exists():
        return None
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            sources = storage.library_sources(conn)
    except (sqlite3.Error, OSError):
        return None  # No tracks table yet, or the database is unreadable (the db check says which).
    total = sum(row["count"] for row in sources)
    if not total:
        return None
    parts = [f"from {'your music folder' if row['source'] == 'file' else row['source'].title()}" for row in sources]
    tracks = f"{total:,} track{'' if total == 1 else 's'}"
    if len(sources) == 1:
        return f"{tracks} {parts[0]}"
    counts = ", ".join(f"{row['count']:,} {part}" for row, part in zip(sources, parts))
    return f"{tracks} ({counts})"


def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable checks"),
    offline: bool = typer.Option(False, "--offline", help="Skip the Cosine API request"),
) -> None:
    """Check credentials, music access, YouTube login and the taste database."""
    cfg = ctx.obj if isinstance(ctx.obj, AppConfig) else load_config()
    result = run_checks(cfg, offline=offline)
    checks = result["checks"]
    if json_output:
        typer.echo(json.dumps(result))
    else:
        table = Table("Check", "Status", "Detail", "Source", title="Cosine Digger doctor")
        colors = {"ok": "green", "off": "dim", "warn": "yellow", "fail": "red"}
        for name, result in checks.items():
            table.add_row(name, result["status"], result["detail"], result["source"], style=colors[result["status"]])
        Console(markup=False).print(table)
    if any(result["status"] == "fail" for result in checks.values()):
        raise typer.Exit(code=1)
