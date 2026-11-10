"""Daily and weekly playlists from the taste profile."""
from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Build the daily and weekly playlists from your taste profile.", pretty_exceptions_show_locals=False)


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="playlist")


def _run(kind: str, dry_run: bool, json_output: bool, name: str | None) -> None:
    from ..client import CosineClientError
    from ..playlists import PlaylistError, run_playlist

    err = Console(stderr=True, markup=False)
    notify = None if json_output else (lambda msg: err.print(msg))
    try:
        result = run_playlist(kind, name=name, dry_run=dry_run, notify=notify, log_run=True)
    except (PlaylistError, CosineClientError) as exc:  # the second: no key, a refused key, no answer
        if json_output:
            typer.echo(json.dumps({"kind": kind, "error": str(exc), "tracks": []}))
        else:
            err.print(str(exc))
        raise typer.Exit(1) from exc
    if result.get("playlist_url"):
        from .. import youtube

        # A private playlist opens only in the account that owns it.
        result["playlist_url"] = youtube.account_link(
            result["playlist_url"], youtube.login_account(youtube.auth_path()))
    failed = bool(result.get("export_error")) or (
        result.get("export") is not None and not result["export"]["verified"])
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        _print(result, dry_run)
    if failed or not result["tracks"]:
        raise typer.Exit(1)


def _print(result: dict, dry_run: bool) -> None:
    console = Console(markup=False)
    table = Table("#", "Track", "Why", title=f"{result['name']} · {result['date']}")
    for i, track in enumerate(result["tracks"], 1):
        table.add_row(str(i), f"{track['artist']} - {track['title']}", track["why"])
    console.print(table)
    bad = {k: v for k, v in result["checks"].items() if v}
    console.print(f"Checks: {'all clear' if not bad else bad} · sources {result['sources']}")
    for warning in result["warnings"]:
        console.print(f"Warning: {warning}")
    if dry_run:
        console.print("Dry run: YouTube Music not touched.")
    elif result.get("export_error"):
        console.print(f"Export failed: {result['export_error']}")
    elif result.get("export"):
        report = result["export"]
        state = "verified" if report["verified"] else (
            f"NOT verified: {len(report['missing'])} missing, {len(report['failed'])} failed")
        console.print(f"{result['playlist_url']} ({len(report['added'])} tracks, {state})")


@app.command()
def daily(
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; leave YouTube Music alone"),
    json_output: bool = typer.Option(False, "--json", help="Print one JSON object to stdout"),
    name: str | None = typer.Option(None, "--name", help='Playlist name (default "Digger Daily")'),
) -> None:
    """About 20 new tracks near your taste; replaces the "Digger Daily" playlist."""
    _run("daily", dry_run, json_output, name)


@app.command()
def weekly(
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; leave YouTube Music alone"),
    json_output: bool = typer.Option(False, "--json", help="Print one JSON object to stdout"),
    name: str | None = typer.Option(None, "--name", help='Playlist name (default "Digger Weekly")'),
) -> None:
    """About 50 tracks, one hop further through labels and artists; replaces "Digger Weekly"."""
    _run("weekly", dry_run, json_output, name)


@app.command()
def due(
    json_output: bool = typer.Option(False, "--json", help="Print one JSON object to stdout"),
) -> None:
    """Run whichever of daily/weekly is due now (what the installed schedule calls)."""
    from contextlib import closing

    from .. import api
    from ..config import load_config
    from ..playlists import make_runner
    from ..schedule import run_due

    cfg = load_config()
    with closing(api.open_db(cfg.db_path)) as conn:
        results = run_due(conn, cfg, make_runner(cfg))
    if json_output:
        typer.echo(json.dumps({"runs": results}, ensure_ascii=False))
    else:
        for run in results or [{"job": "-", "status": "nothing due", "detail": ""}]:
            print(f"{run['job']}: {run['status']} {run['detail'] or ''}".rstrip(), file=sys.stdout)
    if any(run["status"] != "ok" for run in results):
        raise typer.Exit(1)
