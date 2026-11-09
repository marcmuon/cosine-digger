"""Browser-header login and authentication status for YouTube Music."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import typer

from cosine_digger import youtube

app = typer.Typer(help="Configure YouTube Music browser authentication.", pretty_exceptions_show_locals=False)


@contextmanager
def _hidden_stdin():
    # Suppress terminal echo while accepting multiline pasted headers until EOF.
    if not sys.stdin.isatty():
        yield
        return
    import termios

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    hidden = original.copy()
    hidden[3] &= ~(termios.ECHO | termios.ECHONL)
    try:
        termios.tcsetattr(fd, termios.TCSANOW, hidden)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)


def _live_check() -> bool:
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = youtube._authenticated_ytm().get_library_playlists(limit=1)
        return youtube.signed_in(result)
    except Exception:
        # Provider exception messages can contain request headers.
        return False


@app.command("login")
def login_command(
    file: Path | None = typer.Option(None, "--file", help="File containing browser request headers."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip the live authentication check."),
    from_chrome: bool = typer.Option(False, "--from-chrome",
                                     help="Read the login from Chrome on this Mac (macOS asks first: click Allow)."),
    chrome_profile: str | None = typer.Option(None, "--chrome-profile",
                                              help="Chrome profile folder, such as Default or 'Profile 1'."),
):
    """Read browser request headers from stdin until EOF, from --file, or from Chrome."""
    if from_chrome:
        from .. import chrome_login

        typer.echo("Reading your YouTube Music login from Chrome. If macOS asks to use "
                   "\u201cChrome Safe Storage\u201d, click Allow (not Always Allow).", err=True)
        try:
            headers = chrome_login.youtube_headers(chrome_profile)
        except chrome_login.ChromeLoginError as exc:  # our own wording, never cookie values
            typer.echo(f"fail: {exc}", err=True)
            raise typer.Exit(1) from None
    try:
        if not from_chrome and file is not None:
            headers = file.read_text()
        elif not from_chrome:
            typer.echo("Paste YouTube Music request headers, then send EOF (Ctrl-D).", err=True)
            with _hidden_stdin():
                headers = sys.stdin.read()
        youtube.login(headers, youtube.auth_path())
    except Exception:
        typer.echo("fail: could not save login; check the headers and file permissions.", err=True)
        raise typer.Exit(1) from None
    if no_verify:
        typer.echo("ok: login saved; live verification skipped.")
    elif _live_check():
        typer.echo("ok: login saved and verified.")
    else:
        fresh = ("Open music.youtube.com in Chrome, check you're signed in, then run this again." if from_chrome
                 else "Copy fresh browser headers.")
        typer.echo(f"fail: login saved, but live verification failed. {fresh}", err=True)
        raise typer.Exit(1)


@app.command("status")
def status_command(
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
    offline: bool = typer.Option(False, "--offline", help="Only check for the auth file."),
):
    """Check for credentials and, by default, verify access to the library."""
    present = youtube.auth_path().is_file()
    verified = _live_check() if present and not offline else None
    payload = {"auth_present": present, "verified": verified, "offline": offline}
    if json_output:
        typer.echo(json.dumps(payload))
    elif not present:
        typer.echo("fail: auth file missing. Run cosine-digger youtube login.")
    elif offline:
        typer.echo("ok: auth file present; live verification skipped.")
    elif verified:
        typer.echo("ok: YouTube Music authentication verified.")
    else:
        typer.echo("fail: YouTube Music doesn't accept the saved login (expired or signed out). "
                   "Run cosine-digger youtube login with fresh browser headers.")
    if not present or verified is False:
        raise typer.Exit(1)


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="youtube")
