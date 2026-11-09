"""Serve the local UI and its JSON API."""
from __future__ import annotations

import logging.config
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn
from uvicorn.config import LOGGING_CONFIG

from .. import companion as pairing
from .. import runtime
from ..config import AppConfig, load_config
from ..web.app import _fixture_providers, _version, create_app


def register(app: typer.Typer) -> None:
    app.command(name="serve")(serve)


def open_browser(url: str) -> bool:
    """Open ``url`` in a browser. When BROWSER is set, only its commands are
    tried: a failing choice never falls back silently to another browser."""
    choices = [choice for choice in os.environ.get("BROWSER", "").split(os.pathsep) if choice.strip()]
    try:
        if choices:
            return any(webbrowser.get(choice).open(url) for choice in choices)
        return bool(webbrowser.open(url))
    except Exception:  # webbrowser.Error, OSError
        return False


class _Server(uvicorn.Server):
    def __init__(self, config, on_exit):
        super().__init__(config)
        self._on_exit = on_exit

    def handle_exit(self, sig, frame):
        # The runtime record says "stopping" at once and stays until every job has drained.
        threading.Thread(target=self._on_exit, name="digger-stopping", daemon=True).start()
        super().handle_exit(sig, frame)


def run_server(app, *, host: str, port: int, log_level: str, log_config=LOGGING_CONFIG, on_exit=lambda: None) -> bool:
    """uvicorn.run for one app object; True once the server had started."""
    server = _Server(uvicorn.Config(app, host=host, port=port, log_level=log_level, log_config=log_config), on_exit)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return server.started


def serve(
    ctx: typer.Context,
    host: str | None = typer.Option(None, "--host", help="Bind host (default: configured host)"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535, help="Bind port (default: configured port)"),
    no_scheduler: bool = typer.Option(False, "--no-scheduler", help="Disable daily and weekly scheduling"),
    open_flag: bool = typer.Option(False, "--open", help="Open the local app in your browser"),
    reload: bool = typer.Option(False, "--reload",
                                help="Restart when the code changes (development; the scheduler stays off)"),
    owned_by_start: str | None = typer.Option(None, "--owned-by-start", hidden=True),
    companion: bool = typer.Option(False, "--companion",
                                   help="Let the paired iPhone and iPad app connect over your network: listens on "
                                        "every interface unless --host is given, and needs the pairing token "
                                        "(see cosine-digger companion)"),
) -> None:
    """Run the local web app and daily/weekly scheduler."""
    from dataclasses import replace

    cfg = ctx.obj if isinstance(ctx.obj, AppConfig) else load_config()
    background = owned_by_start is not None
    if background and (reload or not runtime.INSTANCE.fullmatch(owned_by_start)):
        raise typer.BadParameter("is only for the server cosine-digger start launches", param_hint="--owned-by-start")
    if background and companion:
        raise typer.BadParameter("runs in a Terminal: cosine-digger serve --companion", param_hint="--companion")
    # The server start launches stays on loopback, whatever the environment says.
    companion = companion or (pairing.enabled() and not background)
    default_host = "0.0.0.0" if companion else cfg.host  # a phone can't reach this computer's loopback
    cfg = replace(cfg, host=host if host is not None else default_host,
                  port=port if port is not None else cfg.port)
    paired = pairing.Companion.at(cfg.home) if companion else None
    _fixture_providers(cfg, scheduler=not (no_scheduler or reload))
    log_config = LOGGING_CONFIG
    if background:
        # Started by `start`: in its own session, with no Terminal to write to.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        logging.config.dictConfig(runtime.log_config(runtime.log_dir(cfg.home) / runtime.SERVER_LOG))
        runtime.detach_output()
        log_config = None  # configured once, above
    identity = runtime.Identity(home=cfg.home, db_path=cfg.db_path, launcher="start" if background else "serve",
                                instance=owned_by_start, version=_version())
    url = f"http://{runtime.url_host(runtime.probe_host(cfg.host))}:{cfg.port}"
    stopped = threading.Event()
    if open_flag:
        def open_when_ready():
            # Only this server's own answer counts: another program on the
            # port, or another Digger, must never be opened in its place.
            pid = None if reload else identity.pid  # reload serves from a worker process
            if runtime.wait_for_self(cfg.host, cfg.port, instance=identity.instance, pid=pid, stop=stopped,
                                     timeout=30) and not open_browser(url):
                print(f"Digger is running at {url}\nCouldn't open your browser. Open that address yourself.",
                      flush=True)

        threading.Thread(target=open_when_ready, name="digger-browser", daemon=True).start()
    if companion:
        print("\n".join(["Companion mode is on: the paired Digger app can reach this Digger.",
                         *pairing.pairing_lines(cfg.host, cfg.port, pairing.load_token(cfg.home)), ""]), flush=True)
    try:
        if reload:  # uvicorn re-imports the app on each change, so it needs the factory by name
            bind = {"COSINE_DIGGER_HOST": cfg.host, "COSINE_DIGGER_PORT": str(cfg.port),
                    "COSINE_DIGGER_INSTANCE": identity.instance, pairing.ENV: "1" if companion else "0"}
            previous = {key: os.environ.get(key) for key in bind}
            os.environ.update(bind)
            try:
                uvicorn.run("cosine_digger.web.app:create_app", factory=True, reload=True,
                            reload_dirs=[str(Path(__file__).resolve().parents[1])],
                            host=cfg.host, port=cfg.port, log_level="info")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        else:
            app = create_app(cfg, scheduler=not no_scheduler, identity=identity, companion=paired,
                             announce=(cfg.host, cfg.port, runtime.silence_startup_output if background else None))
            if not run_server(app, host=cfg.host, port=cfg.port, log_level="info", log_config=log_config,
                              on_exit=identity.stopping):
                sys.exit(3)  # as uvicorn.run does when startup fails
    finally:
        stopped.set()
