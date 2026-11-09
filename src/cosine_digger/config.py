"""Shared configuration for the CLI, setup UI and container entry points."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Literal

from dotenv import load_dotenv

Source = Literal["default", "settings", "env", "legacy"]
ENV_VARS = {
    "home": "COSINE_DIGGER_HOME",
    "db_path": "COSINE_DIGGER_DB",
    "cosine_token": "COSINE_API_TOKEN",
    "discogs_token": "DISCOGS_TOKEN",
    "exa_api_key": "EXA_API_KEY",
    "music_dir": "COSINE_DIGGER_MUSIC_DIR",
    "ytmusic_auth_path": "COSINE_DIGGER_YTMUSIC_AUTH",
    "daily_time": "COSINE_DIGGER_DAILY_TIME",
    "weekly_day": "COSINE_DIGGER_WEEKLY_DAY",
    "weekly_time": "COSINE_DIGGER_WEEKLY_TIME",
    "daily_size": "COSINE_DIGGER_DAILY_SIZE",
    "weekly_size": "COSINE_DIGGER_WEEKLY_SIZE",
    "host": "COSINE_DIGGER_HOST",
    "port": "COSINE_DIGGER_PORT",
}
SECRET_FIELDS = frozenset({"cosine_token", "discogs_token", "exa_api_key"})
SETTINGS_KEYS = ENV_VARS.keys() - {"home"}

_CREDENTIAL = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|x-goog-authuser|x-api-key|api[_-]?key|token|sapisid\w*|\w*sid)"
    r"(\s*[:=]\s*)[^\n]*")
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)[^\s'\"]+")
_HEADER_SAFE = re.compile(r"[\x21-\x7e]+")


def scrub(text: str) -> str:
    """Blank anything shaped like a credential header, key=value or bearer token in a message."""
    text = _CREDENTIAL.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    return _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


def usable_key(value: str | None, name: str) -> str | None:
    """A key as it may go into an HTTP header: trimmed, and refused (never
    echoed) when it holds spaces, line breaks or other unusual characters,
    since the HTTP library would quote it back in its error."""
    value = (value or "").strip()
    if value and not _HEADER_SAFE.fullmatch(value):
        raise ValueError(f"{name} contains spaces, line breaks or unusual characters: paste it again")
    return value or None


@dataclass(frozen=True)
class AppConfig:
    home: Path
    db_path: Path
    cosine_token: str | None = field(default=None, repr=False)
    discogs_token: str | None = field(default=None, repr=False)
    exa_api_key: str | None = field(default=None, repr=False)
    music_dir: Path | None = None
    ytmusic_auth_path: Path = Path("~/.cosine-digger/ytmusic_auth.json")
    daily_time: str = "06:00"
    weekly_day: str = "mon"
    weekly_time: str = "06:00"
    daily_size: int = 20
    weekly_size: int = 50
    host: str = "127.0.0.1"
    port: int = 8765
    sources: Mapping[str, Source] = field(default_factory=lambda: MappingProxyType({}))


def _read_settings(home: Path) -> dict:
    try:
        settings = json.loads((home / "settings.json").read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(settings, dict):
        raise ValueError("settings.json must contain a JSON object")
    return settings


def load_env() -> None:
    """Load the current directory's .env without replacing process values.

    Never search upward from the package: in a checkout that finds the repo's
    own .env, whose keys would then beat the keys saved on the Setup page.
    The server `cosine-digger start` launches gets COSINE_DIGGER_NO_DOTENV=1:
    start has already read this folder's .env into the environment it passes on."""
    if os.environ.get("COSINE_DIGGER_NO_DOTENV") == "1":
        return
    load_dotenv(Path.cwd() / ".env", override=False)


def load_config(environ: Mapping[str, str] | None = None) -> AppConfig:
    """Load cwd's .env without replacing process values, then resolve settings.

    An explicit mapping is isolated from both os.environ and dotenv. HOME and
    COSINE_DIGGER_HOME locate settings.json. The legacy DB, when present, takes
    precedence over the saved/default DB, but never over COSINE_DIGGER_DB.
    """
    if environ is None:
        load_env()
        environ = os.environ
    user_home = Path(environ.get("HOME", str(Path.home())))

    def path(value: str | Path) -> Path:
        value = str(value)
        if value == "~":
            return user_home
        if value.startswith("~/"):
            return user_home / value[2:]
        return Path(value).expanduser()

    settings_home = path(environ.get("COSINE_DIGGER_HOME", user_home / ".cosine-digger"))
    settings = _read_settings(settings_home)
    values = {
        "home": settings_home, "cosine_token": None, "discogs_token": None,
        "exa_api_key": None, "music_dir": None, "daily_time": "06:00",
        "weekly_day": "mon", "weekly_time": "06:00", "daily_size": 20,
        "weekly_size": 50, "host": "127.0.0.1", "port": 8765,
    }
    sources: dict[str, Source] = {key: "default" for key in ENV_VARS}
    for key, env_key in ENV_VARS.items():
        if key in SETTINGS_KEYS and key in settings:
            values[key] = settings[key]
            sources[key] = "settings"
        if env_key in environ:
            values[key] = environ[env_key]
            sources[key] = "env"

    # A pasted key often carries a newline; it would make an illegal HTTP header.
    for key in SECRET_FIELDS:
        if isinstance(values[key], str):
            values[key] = values[key].strip() or None
    values["home"] = path(values["home"])
    values.setdefault("db_path", values["home"] / "taste.db")
    values.setdefault("ytmusic_auth_path", values["home"] / "ytmusic_auth.json")
    legacy_db = user_home / ".cosine-taste" / "taste.db"
    if "COSINE_DIGGER_DB" not in environ and legacy_db.exists():
        values["db_path"] = legacy_db
        sources["db_path"] = "legacy"
    for key in ("db_path", "ytmusic_auth_path"):
        values[key] = path(values[key])
    values["music_dir"] = path(values["music_dir"]) if values["music_dir"] else None
    for key in ("daily_size", "weekly_size", "port"):
        try:
            values[key] = int(values[key])
        except (ValueError, TypeError):
            raise ValueError(f"{key} must be an integer") from None
    return AppConfig(**values, sources=MappingProxyType(sources))


def save_settings(updates: dict, home: Path) -> None:
    """Merge known settings and atomically replace a private settings file."""
    if updates.keys() - SETTINGS_KEYS:
        raise ValueError("Unknown settings keys")
    home = home.expanduser()
    settings = _read_settings(home)
    settings.update(updates)
    payload = json.dumps(settings, indent=2) + "\n"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=home, delete=False) as tmp:
            temp_path = Path(tmp.name)
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        temp_path.replace(home / "settings.json")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def ensure_env(cfg: AppConfig) -> None:
    """Make saved credentials available to clients that still read the env."""
    for key in SECRET_FIELDS:
        value = getattr(cfg, key)
        if value is not None:
            os.environ.setdefault(ENV_VARS[key], value)
