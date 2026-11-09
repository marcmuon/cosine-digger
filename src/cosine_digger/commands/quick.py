"""`cosine-digger quick`: seeds → explained picks → private YouTube Music playlist."""
from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .. import api


def register(app: typer.Typer) -> None:
    @app.command(name="quick")
    def quick_cmd(
        seeds: list[str] = typer.Argument(..., help="'Artist - Track', 'Artist - Album', or URLs"),
        limit: int = typer.Option(25, "--limit", "-l", help="How many picks"),
        playlist: str | None = typer.Option(None, "--playlist", "-p", help="YouTube Music playlist name (replaced if it exists)"),
        no_export: bool = typer.Option(False, "--no-export", help="Skip the YouTube Music playlist"),
        scorer: str = typer.Option("rrf", "--scorer", help="rrf, consensus or adventurous"),
        deep: bool = typer.Option(False, "--deep", help="Also follow labels and artists one hop (slower)"),
        json_output: bool = typer.Option(False, "--json", help="Print one JSON object to stdout"),
    ) -> None:
        """The fast path: a few favorites in, a playlist of unfamiliar picks out."""
        err = Console(stderr=True, markup=False)
        notify = None if json_output else (lambda m: err.print(api.plain(m)))
        result = api.quick(seeds, limit=limit, playlist=playlist, export_playlist=not no_export,
                           scorer=scorer, notify=notify, deep=deep)
        export = result.get("export")
        # A playlist that YouTube didn't fully accept is a failure too; the JSON keeps the detail.
        failed = bool(result.get("error") or result.get("export_error") or (export and not export["verified"]))
        if json_output:
            sys.stdout.write(json.dumps(result, default=str) + "\n")
            raise typer.Exit(code=1 if failed else 0)
        out = Console()
        if result.get("error"):
            out.print(f"[red]{escape(result['error'])}.[/] Try 'Artist - Track' text or a Discogs release URL.")
            raise typer.Exit(code=1)
        table = Table(show_lines=False, border_style="dim")
        table.add_column("#", justify="right")
        table.add_column("Artist - Track")
        table.add_column("Why")
        for i, p in enumerate(result["picks"], 1):
            table.add_row(str(i), escape(f"{p['artist']} - {p['title']}"), f"[dim]{escape(p['why'])}[/dim]")
        out.print(table)
        if result.get("playlist_url"):
            exp = result["export"]
            state = "[green]verified[/]" if exp["verified"] else "[yellow]not fully verified[/]"
            swapped = f", {len(exp['inserted'])} swapped in by YouTube" if exp.get("inserted") else ""
            out.print(f"{escape(result['playlist_name'])}: {escape(result['playlist_url'])} "
                      f"({len(exp['added'])} added, {len(exp['missing'])} missing{swapped}, "
                      f"{len(exp['failed'])} failed; {state})")
        elif result.get("export_error"):
            out.print(f"[yellow]Playlist not written:[/] {escape(result['export_error'])}")
        out.print(str(result['timings']), style="dim", markup=False)
        raise typer.Exit(code=1 if failed else 0)
