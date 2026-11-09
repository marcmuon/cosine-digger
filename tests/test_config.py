import json
import os
from pathlib import Path
import sqlite3
import stat

import httpx
import pytest
from typer.testing import CliRunner

from cosine_digger.cli import app
from cosine_digger.config import ENV_VARS, SECRET_FIELDS, ensure_env, load_config, save_settings


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in ENV_VARS.values():
        # Register restoration even when ensure_env later inserts an absent key.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return tmp_path


def test_precedence_and_legacy_override(monkeypatch, isolated_home):
    home = isolated_home / ".cosine-digger"
    save_settings({"cosine_token": "saved-secret", "daily_size": 12, "port": 9000}, home)
    saved = load_config()
    assert saved.cosine_token == "saved-secret"
    assert saved.daily_size == 12
    assert saved.sources["cosine_token"] == "settings"
    assert saved.db_path == home / "taste.db"
    assert saved.ytmusic_auth_path == home / "ytmusic_auth.json"
    assert saved.sources["db_path"] == "default"
    assert set(saved.sources) == set(ENV_VARS)

    legacy = isolated_home / ".cosine-taste" / "taste.db"
    legacy.parent.mkdir()
    legacy.touch()
    assert load_config().db_path == legacy
    assert load_config().sources["db_path"] == "legacy"

    override = isolated_home / "override.db"
    monkeypatch.setenv("COSINE_DIGGER_DB", str(override))
    monkeypatch.setenv("COSINE_API_TOKEN", "env-secret")
    monkeypatch.setenv("COSINE_DIGGER_DAILY_SIZE", "31")
    cfg = load_config()
    assert cfg.db_path == override
    assert cfg.sources["db_path"] == "env"
    assert cfg.cosine_token == "env-secret"
    assert cfg.sources["cosine_token"] == "env"
    assert cfg.daily_size == 31
    assert cfg.port == 9000


def test_save_settings_rejects_home_without_writing(isolated_home):
    home = isolated_home / "settings"
    save_settings({"daily_size": 12}, home)
    before = (home / "settings.json").read_bytes()
    with pytest.raises(ValueError, match="Unknown"):
        save_settings({"home": str(isolated_home / "stray"), "daily_size": 13}, home)
    assert (home / "settings.json").read_bytes() == before


def test_save_settings_is_private_and_rejects_unknown_keys(isolated_home):
    home = isolated_home / "settings"
    save_settings({"cosine_token": "private", "daily_size": 8}, home)
    target = home / "settings.json"
    target.chmod(0o644)
    home.chmod(0o755)
    save_settings({"daily_size": 16}, home)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert json.loads(target.read_text()) == {"cosine_token": "private", "daily_size": 16}
    before = target.read_bytes()
    with pytest.raises(ValueError, match="Unknown"):
        save_settings({"typo": "not a setting"}, home)
    assert target.read_bytes() == before
    assert list(home.iterdir()) == [target]


def test_ensure_env_preserves_existing_values(monkeypatch, isolated_home):
    save_settings({key: f"saved-{key}" for key in SECRET_FIELDS}, isolated_home / ".cosine-digger")
    cfg = load_config()
    monkeypatch.setenv("COSINE_API_TOKEN", "existing")
    monkeypatch.setenv("DISCOGS_TOKEN", "")
    ensure_env(cfg)
    assert os.environ["COSINE_API_TOKEN"] == "existing"
    assert os.environ["DISCOGS_TOKEN"] == ""
    assert os.environ["EXA_API_KEY"] == "saved-exa_api_key"


def test_doctor_offline_json_and_sources(monkeypatch, isolated_home):
    home = isolated_home / ".cosine-digger"
    secrets = {key: f"private-{key}" for key in SECRET_FIELDS}
    save_settings({**secrets, "music_dir": str(isolated_home)}, home)
    (home / "ytmusic_auth.json").write_text('{"cookie": "private-cookie"}')
    with sqlite3.connect(home / "taste.db") as conn:
        conn.execute("PRAGMA user_version=7")

    def no_network(*args, **kwargs):
        pytest.fail("offline doctor attempted network access")

    monkeypatch.setattr(httpx, "get", no_network)
    result = CliRunner().invoke(app, ["doctor", "--json", "--offline"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"checks", "config"}
    assert set(payload["checks"]) == {
        "cosine_token", "discogs_token", "exa_api_key", "youtube_login", "music_dir", "db"
    }
    for item in payload["checks"].values():
        assert set(item) == {"status", "detail", "source"}
        assert item["status"] == "ok"
        assert isinstance(item["detail"], str)
        assert item["source"] in {"default", "settings", "env", "legacy"}
    assert payload["checks"]["cosine_token"]["source"] == "settings"
    assert "user_version=7" in payload["checks"]["db"]["detail"]
    assert payload["config"]["db_path"] == str(home / "taste.db")
    assert not SECRET_FIELDS.intersection(payload["config"])
    for secret in [*secrets.values(), "private-cookie"]:
        assert secret not in result.output


def test_doctor_missing_and_corrupt_files(monkeypatch, isolated_home):
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json", "--offline"])
    assert result.exit_code == 1
    checks = json.loads(result.output)["checks"]
    # Optional items that aren't set up are "off", not "warn" (P8); a missing database still warns.
    assert {name: check["status"] for name, check in checks.items()} == {
        "cosine_token": "fail", "discogs_token": "off", "exa_api_key": "off", "youtube_login": "off",
        "music_dir": "off", "db": "warn"}
    assert not (isolated_home / ".cosine-digger").exists()

    monkeypatch.setenv("COSINE_API_TOKEN", "private-token")
    home = isolated_home / ".cosine-digger"
    home.mkdir()
    (home / "taste.db").write_text("broken database")
    (home / "ytmusic_auth.json").write_text("broken JSON")
    result = runner.invoke(app, ["doctor", "--json", "--offline"])
    assert result.exit_code == 1
    checks = json.loads(result.output)["checks"]
    assert checks["db"]["status"] == "fail"
    assert checks["youtube_login"]["status"] == "warn"
    assert (home / "taste.db").read_text() == "broken database"
    human = runner.invoke(app, ["doctor", "--offline"])
    assert human.exit_code == 1
    assert "Cosine Digger doctor" in human.output
    assert "private-token" not in human.output


@pytest.mark.parametrize("response_status", [200])
def test_doctor_online_request(monkeypatch, isolated_home, response_status):
    monkeypatch.setenv("COSINE_API_TOKEN", "private-token")
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if response_status == "timeout":
            raise httpx.ReadTimeout("private-token")
        return httpx.Response(response_status, text="private-token")

    monkeypatch.setattr(httpx, "get", request)
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == (0 if response_status == 200 else 1)
    check = json.loads(result.output)["checks"]["cosine_token"]
    assert check["status"] == ("ok" if response_status == 200 else "fail")
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://cosine.club/api/v1/search"
    assert kwargs["timeout"] == 5.0
    assert kwargs["headers"]["Authorization"] == "Bearer private-token"
    assert "private-token" not in result.output
