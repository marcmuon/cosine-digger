"""Inspect the shared taste profile."""
from contextlib import closing
import json
import sqlite3

import typer
from rich.console import Console
from rich.table import Table

from .. import storage
from ..config import load_config
from ..profile import build_profile


def register(app: typer.Typer) -> None:
    app.command(name="profile")(profile)


def profile(
    json_output: bool = typer.Option(False, "--json", help="Output the full profile as JSON"),
    top: int = typer.Option(10, "--top", min=1, help="Number of artists and labels to show"),
) -> None:
    """Show the artists and labels that shape your taste."""
    db_path = load_config().db_path
    # A first look at an empty profile should not create a database on disk.
    with closing(storage.connect(db_path) if db_path.exists() else sqlite3.connect(":memory:")) as conn:
        result = build_profile(conn)
    if json_output:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
        return
    console = Console(markup=False)
    if not result.entities:
        console.print("Run a dig or `taste-scan` your music folder to start your profile.")
        return
    for kind, title in (("artist", "Top artists"), ("label", "Top labels")):
        table = Table("Name", "Weight", "Sources", title=title)
        for entity in result.top(kind, top):
            sources = ", ".join(f"{source} {weight:+.2f}" for source, weight in entity.sources.items())
            table.add_row(entity.name, f"{entity.weight:.2f}", sources)
        console.print(table)
