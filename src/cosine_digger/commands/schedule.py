"""Install an OS schedule and inspect the persisted playlist job state."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import sys

import typer

from .. import storage
from ..config import ENV_VARS, SECRET_FIELDS, AppConfig, _read_settings, load_config
from ..schedule import JOBS, last_success, next_run

app = typer.Typer(help="Install and inspect automatic playlist updates.", pretty_exceptions_show_locals=False)
LABEL = "club.cosine.digger"


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="schedule")


def _config(ctx: typer.Context) -> AppConfig:
    return ctx.obj if isinstance(ctx.obj, AppConfig) else load_config()


def _plist_path(directory: Path | None) -> Path:
    directory = directory if directory is not None else Path.home() / "Library" / "LaunchAgents"
    return directory.expanduser().resolve() / f"{LABEL}.plist"


def _program_args(program: Path | None) -> list[str]:
    if program is not None:
        return [str(program.expanduser().absolute())]
    executable = shutil.which("cosine-digger")
    if executable:
        return [str(Path(executable).absolute())]
    # Do not resolve symlinks: a venv's Python must retain its venv identity.
    return [str(Path(sys.executable).absolute()), "-m", "cosine_digger.cli"]


def _service_env(cfg: AppConfig) -> dict[str, str]:
    """What the scheduled job needs from this shell: the data paths plus any
    non-secret setting that came from the environment (secrets never go into
    a plist or crontab)."""
    env = {
        "COSINE_DIGGER_HOME": str(cfg.home.expanduser().resolve()),
        "COSINE_DIGGER_DB": str(cfg.db_path.expanduser().resolve()),
    }
    for key, var in ENV_VARS.items():
        if key in SECRET_FIELDS or key in ("home", "db_path", "host", "port") or cfg.sources.get(key) != "env":
            continue
        value = getattr(cfg, key)
        env[var] = str(value.expanduser().resolve()) if isinstance(value, Path) else str(value)
    return env


def _check_saved_keys(cfg: AppConfig) -> None:
    """The scheduled job starts outside this shell, so keys it needs must be
    saved in settings.json (the Setup page does that), not only exported."""
    saved = _read_settings(cfg.home.expanduser())
    where = cfg.home.expanduser() / "settings.json"
    if not saved.get("cosine_token"):
        typer.echo(
            "Not installed: the scheduled job can't see keys set in your shell or a .env file. "
            f"Save your cosine.club key on the app's Setup page (cosine-digger start); it is stored in {where}, "
            "readable only by you. Then run this again.", err=True)
        raise typer.Exit(2)
    for key, feature in (("discogs_token", "weekly label/artist hops"), ("exa_api_key", "fresh finds")):
        if getattr(cfg, key) and not saved.get(key):
            typer.echo(f"Note: {ENV_VARS[key]} is only in your environment, so scheduled runs skip {feature} "
                       "until you save it on the Setup page.", err=True)


@app.command("install")
def install(
    ctx: typer.Context,
    directory: Path | None = typer.Option(None, "--dir", help="LaunchAgents directory."),
    program: Path | None = typer.Option(None, "--program", help="cosine-digger executable."),
) -> None:
    """Write a launchd plist on macOS, or print a crontab entry on Linux."""
    cfg = _config(ctx)
    _check_saved_keys(cfg)
    args = [*_program_args(program), "playlist", "due"]
    service_env = _service_env(cfg)
    logs = cfg.home.expanduser().resolve() / "logs"
    if sys.platform == "darwin":
        path = _plist_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LABEL, "ProgramArguments": args,
            "StartInterval": 3600, "RunAtLoad": True,
            "StandardOutPath": str(logs / "schedule.log"),
            "StandardErrorPath": str(logs / "schedule.err.log"),
            "EnvironmentVariables": service_env,
        }
        path.write_bytes(plistlib.dumps(payload))
        typer.echo(f'launchctl bootstrap gui/$UID {shlex.quote(str(path))}')
    elif sys.platform.startswith("linux"):
        logs.mkdir(parents=True, exist_ok=True)
        env = "".join(f"{var}={shlex.quote(value)} " for var, value in service_env.items())
        # cron interprets % before the shell, even inside shell quotes.
        command = f"{env}{shlex.join(args)} >> {shlex.quote(str(logs / 'schedule.log'))} 2>&1"
        typer.echo("17 * * * * " + command.replace("%", r"\%"))
    else:
        raise typer.BadParameter("Schedule installation supports macOS and Linux.")


@app.command("uninstall")
def uninstall(
    directory: Path | None = typer.Option(None, "--dir", help="LaunchAgents directory."),
) -> None:
    """Remove the generated plist; print the command to unload its service."""
    if sys.platform == "darwin":
        _plist_path(directory).unlink(missing_ok=True)
        typer.echo(f"launchctl bootout gui/$UID/{LABEL}")
    elif sys.platform.startswith("linux"):
        typer.echo("Remove the cosine-digger playlist due entry with crontab -e.")
    else:
        raise typer.BadParameter("Schedule installation supports macOS and Linux.")


@app.command("status")
def status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable status."),
) -> None:
    """Show each job's next calendar occurrence and last successful completion."""
    cfg = _config(ctx)
    now = datetime.now()
    with closing(storage.connect(cfg.db_path)) as conn:
        storage.migrate(conn)
        jobs = []
        for job in JOBS:
            succeeded = last_success(conn, job)
            jobs.append({
                "job": job,
                "next_run": next_run(job, cfg, now).astimezone().isoformat(),
                "last_success": succeeded.isoformat() if succeeded else None,
            })
    if json_output:
        typer.echo(json.dumps({"jobs": jobs}))
    else:
        for item in jobs:
            typer.echo(f"{item['job']}: next {item['next_run']}; last success {item['last_success'] or 'never'}")
