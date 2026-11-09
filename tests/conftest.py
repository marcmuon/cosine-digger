import os
from pathlib import Path

import pytest

from cosine_digger import client, config, discogs


def pytest_terminal_summary(terminalreporter):
    skipped = terminalreporter.stats.get("skipped", [])
    if os.environ.get("DIGGER_NO_SKIPS") == "1" and skipped:
        terminalreporter.write_line(f"DIGGER_NO_SKIPS=1: {len(skipped)} skipped test(s) fail this run", red=True)


def pytest_sessionfinish(session, exitstatus):
    """CI and done_check set DIGGER_NO_SKIPS=1: a skipped test (missing Chrome,
    Playwright, uv or Docker) fails the run instead of passing quietly."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if os.environ.get("DIGGER_NO_SKIPS") == "1" and skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _no_checkout_dotenv(monkeypatch):
    """Every caller, including imported aliases, must ignore the checkout's .env."""
    real_load_env = config.load_env
    for module in (config, client, discogs):
        monkeypatch.setattr(module, "load_env", lambda: None)
    return real_load_env


@pytest.fixture
def allow_tmp_dotenv(monkeypatch, tmp_path, _no_checkout_dotenv):
    """Opt in to the real loader only within this test's temporary directory."""
    monkeypatch.chdir(tmp_path)

    def load_temporary_env():
        assert Path.cwd().resolve().is_relative_to(tmp_path.resolve()), "dotenv requires a temporary cwd"
        _no_checkout_dotenv()

    for module in (config, client, discogs):
        monkeypatch.setattr(module, "load_env", load_temporary_env)


@pytest.fixture(autouse=True)
def _no_discogs_album_search(monkeypatch):
    """Album-seed fallback asks Discogs over the network; tests opt in explicitly."""
    monkeypatch.setattr("cosine_digger.seeds.discogs_find_release", lambda artist, album, **_: None)


@pytest.fixture(autouse=True)
def _no_real_chrome(monkeypatch, tmp_path):
    """Log in from Chrome reads the real profile folder; tests point it at an empty one."""
    monkeypatch.setattr("cosine_digger.chrome_login.CHROME_DIR", tmp_path / "no-chrome")


@pytest.fixture(autouse=True)
def _no_youtube_read_waits(monkeypatch):
    """The sink re-reads a playlist for up to ~20 s while YouTube catches up; tests don't wait."""
    monkeypatch.setattr("cosine_digger.youtube.YTMusicSink.READ_WAITS", (0, 0, 0))


@pytest.fixture(autouse=True)
def _fresh_discogs_pacer(monkeypatch):
    """The Discogs pacing gate is process-wide; each test starts with a free slot."""
    monkeypatch.setattr("cosine_digger.discogs._next_slot", 0.0)


@pytest.fixture(autouse=True)
def _no_outside_db(monkeypatch):
    """done_check exports COSINE_DIGGER_DB (a copy of the real taste DB) before
    running the suite; a test that loads config must never write into it."""
    monkeypatch.delenv("COSINE_DIGGER_DB", raising=False)
