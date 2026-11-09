"""cosine-digger companion: pair the Digger iPhone and iPad app with this computer."""
from __future__ import annotations

import os

import typer

from .. import companion as pairing
from ..config import AppConfig, load_config


def register(app: typer.Typer) -> None:
    app.command(name="companion")(companion)


def companion(
    ctx: typer.Context,
    port: int | None = typer.Option(None, "--port", min=1, max=65535,
                                    help="The port serve --companion uses (default: the configured port)"),
    rotate: bool = typer.Option(False, "--rotate",
                                help="Make a new pairing token. Paired devices stop working until they pair again"),
    copy: bool = typer.Option(False, "--copy",
                              help="Copy the pairing link (macOS), so the phone can paste it with Universal Clipboard"),
) -> None:
    """Show the link and token that pair the Digger app on an iPhone or iPad."""
    cfg = ctx.obj if isinstance(ctx.obj, AppConfig) else load_config(os.environ)
    token = pairing.load_token(cfg.home, rotate=rotate)
    port = port or cfg.port
    if rotate:
        typer.echo("Made a new pairing token. A Digger already running uses it from now on, "
                   "so devices paired before need this new link.")
    typer.echo("Run Digger for the phone with:  cosine-digger serve --companion"
               + (f" --port {port}" if port != 8765 else ""))
    typer.echo("(Stop a Digger you started with cosine-digger start first: cosine-digger stop.)\n")
    for line in pairing.pairing_lines("0.0.0.0", port, token):
        typer.echo(line)
    typer.echo("\nThe iOS Simulator on this computer can use http://127.0.0.1:" + str(port) + " with the same token.")
    typer.echo("Anyone with the token on your network can dig, keep and pass as you. "
               "cosine-digger companion --rotate replaces it.")
    if copy:
        link = pairing.pairing_link(pairing.addresses("0.0.0.0", port), token, pairing.display_name())
        typer.echo("\nCopied the pairing link." if pairing.copy_to_clipboard(link)
                   else "\nCouldn't copy the link here (copying needs macOS). Copy it from above.")
