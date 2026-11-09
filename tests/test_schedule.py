from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import plistlib
import shlex
import threading
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from cosine_digger import storage
from cosine_digger.cli import app
from cosine_digger.commands import schedule as commands
from cosine_digger.config import AppConfig, load_config, save_settings
from cosine_digger.schedule import (
    JOBS, SchedulerLoop, due_jobs, last_success, latest_occurrence, next_run, run_due,
)


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(home=tmp_path / "home", db_path=tmp_path / "taste.db")


@pytest.mark.parametrize(("job", "now", "expected"), [
    ("daily", "2024-01-01T00:00", "2023-12-31T06:00"),
    ("daily", "2024-01-01T05:59:59", "2023-12-31T06:00"),
    ("daily", "2024-01-01T06:00", "2024-01-01T06:00"),
    ("daily", "2024-01-01T23:59:59", "2024-01-01T06:00"),
    ("weekly", "2024-01-01T00:00", "2023-12-25T06:00"),
    ("weekly", "2024-01-01T06:00", "2024-01-01T06:00"),
    ("weekly", "2024-01-07T23:59:59", "2024-01-01T06:00"),
    ("weekly", "2024-01-08T05:59:59", "2024-01-01T06:00"),
])
def test_calendar_boundaries(cfg, job, now, expected):
    now, expected = datetime.fromisoformat(now), datetime.fromisoformat(expected)
    assert latest_occurrence(job, cfg, now) == expected
    following = next_run(job, cfg, now)
    assert following == expected + timedelta(days=1 if job == "daily" else 7)
    assert following > now


def test_custom_schedule_and_dst(cfg):
    cfg = replace(cfg, daily_time="00:00", weekly_day="sun", weekly_time="08:15")
    now = datetime(2024, 3, 10, 9, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert latest_occurrence("daily", cfg, now).isoformat() == "2024-03-10T00:00:00-08:00"
    assert latest_occurrence("weekly", cfg, now).isoformat() == "2024-03-10T08:15:00-07:00"
    assert next_run("daily", cfg, now).isoformat() == "2024-03-11T00:00:00-07:00"


def test_never_run_and_success_at_or_after_schedule(cfg):
    now = datetime(2024, 1, 1, 7)
    assert due_jobs(cfg, now, lambda _: None) == [
        (job, datetime(2024, 1, 1, 6)) for job in JOBS
    ]
    assert due_jobs(cfg, now, lambda _: now) == []
    assert due_jobs(cfg, now, lambda _: now.replace(hour=6)) == []
    aware_now = now.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    succeeded = aware_now.astimezone(timezone.utc)
    assert due_jobs(cfg, aware_now, lambda _: succeeded) == []
    assert due_jobs(cfg, now, lambda _: now.astimezone(timezone.utc)) == []


def test_catch_up_once_and_persist_success(cfg):
    now = datetime(2024, 1, 4, 7)
    calls = []
    with closing(storage.connect(cfg.db_path)) as conn:
        storage.migrate(conn)
        for job in JOBS:
            run_id = storage.job_start(conn, job, None)
            storage.job_finish(conn, run_id, "success", "previous run")
        with conn:
            conn.execute("UPDATE job_runs SET finished_at = ?", (datetime(2024, 1, 1, 7).astimezone().isoformat(),))

        def runner(job, scheduled_for):
            calls.append((job, scheduled_for))
            # The running record is committed before external work starts.
            with closing(storage.connect(cfg.db_path)) as observer:
                assert observer.execute("SELECT status FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "running"
            return "playlist updated"

        result = run_due(conn, cfg, runner, now=now)
        assert calls == [("daily", datetime(2024, 1, 4, 6))]
        assert result[0]["status"] == "ok"
        assert result[0]["detail"] == "playlist updated"
        assert datetime.fromisoformat(result[0]["scheduled_for"]).timestamp() == calls[0][1].timestamp()
        assert last_success(conn, "daily") is not None
        assert run_due(conn, cfg, runner, now=now) == []


def test_failure_is_recorded_and_retried_without_blocking_weekly(cfg, caplog):
    now = datetime(2024, 1, 1, 7)

    def runner(job, scheduled_for):
        if job == "daily":
            raise RuntimeError("provider said: Cookie: SID=secret-cookie-value")
        return "weekly updated"

    with closing(storage.connect(cfg.db_path)) as conn:
        storage.migrate(conn)
        results = run_due(conn, cfg, runner, now=now)
        assert [item["status"] for item in results] == ["error", "ok"]
        assert conn.execute("SELECT status, detail FROM job_runs WHERE job = 'daily'").fetchone() == (
            "error", "RuntimeError (details hidden: they can contain credentials)",
        )
        assert "Scheduled job daily failed" in caplog.text and "secret-cookie-value" not in caplog.text
        assert [job for job, _ in due_jobs(cfg, now, lambda job: last_success(conn, job))] == ["daily"]

        def failed_ago(delta):
            conn.execute("UPDATE job_runs SET finished_at = ? WHERE status = 'error'",
                         ((now - delta).astimezone().isoformat(),))

        failed_ago(timedelta(minutes=10))  # a fresh failure waits out the backoff
        assert run_due(conn, cfg, lambda *_: "recovered", now=now) == []
        failed_ago(timedelta(hours=2))
        retried = run_due(conn, cfg, lambda *_: "recovered", now=now)
        assert [(item["job"], item["status"]) for item in retried] == [("daily", "ok")]
        assert run_due(conn, cfg, runner, now=now) == []


def test_loop_recovers_from_tick_failure_and_stops(cfg, monkeypatch, caplog):
    real_connect = storage.connect
    attempts = []
    completed = threading.Event()
    calls = []

    def connect(path):
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            raise OSError("temporary database failure")
        return real_connect(path)

    def runner(job, scheduled_for):
        calls.append(job)
        if job == "weekly":
            completed.set()
        return "updated"

    monkeypatch.setattr(storage, "connect", connect)
    loop = SchedulerLoop(cfg, runner, cfg.db_path, interval_s=0.01)
    try:
        loop.start()
        loop.start()
        assert completed.wait(5), "scheduler did not recover"
    finally:
        loop.stop()
    assert calls == list(JOBS)
    assert len(attempts) >= 2
    assert all(identity != threading.get_ident() for identity in attempts)
    assert "Scheduler tick failed" in caplog.text
    assert loop._thread.daemon
    assert not loop._thread.is_alive()
    with closing(real_connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT job, status FROM job_runs ORDER BY id").fetchall() == [
            (job, "ok") for job in JOBS
        ]


@pytest.fixture
def cli_config(cfg, monkeypatch):
    import cosine_digger.cli as cli
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    save_settings({"cosine_token": "saved-token"}, cfg.home)
    return cfg


def test_install_plist_and_uninstall(cli_config, tmp_path, monkeypatch):
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    directory = tmp_path / "Launch Agents"
    program = tmp_path / "my bin" / "cosine-digger"
    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "install", "--dir", str(directory), "--program", str(program)])
    assert result.exit_code == 0, result.exception
    path = directory / "club.cosine.digger.plist"
    payload = plistlib.loads(path.read_bytes())
    assert payload["ProgramArguments"] == [str(program), "playlist", "due"]
    assert payload["StartInterval"] == 3600
    assert payload["RunAtLoad"] is True
    assert payload["Label"] == "club.cosine.digger"
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert payload[key].startswith(str(cli_config.home / "logs") + "/")
    assert (cli_config.home / "logs").is_dir()
    assert result.stdout.strip() == f"launchctl bootstrap gui/$UID {shlex.quote(str(path))}"
    result = runner.invoke(app, ["schedule", "uninstall", "--dir", str(directory)])
    assert result.exit_code == 0, result.exception
    assert not path.exists()
    assert result.stdout.strip() == "launchctl bootout gui/$UID/club.cosine.digger"


@pytest.mark.parametrize("executable", [None, "/venv/bin/cosine-digger"])
def test_install_default_program(cli_config, tmp_path, monkeypatch, executable):
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    monkeypatch.setattr(commands.shutil, "which", lambda _: executable)
    result = CliRunner().invoke(app, ["schedule", "install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    payload = plistlib.loads((tmp_path / "club.cosine.digger.plist").read_bytes())
    expected = [executable] if executable else [commands.sys.executable, "-m", "cosine_digger.cli"]
    assert payload["ProgramArguments"] == [*expected, "playlist", "due"]


def test_linux_install_only_prints_cron(cli_config, tmp_path, monkeypatch):
    monkeypatch.setattr(commands.sys, "platform", "linux")
    before = set(tmp_path.rglob("*"))
    result = CliRunner().invoke(app, ["schedule", "install", "--program", "/my bin/digger"])
    assert result.exit_code == 0, result.exception
    home, db = cli_config.home.resolve(), cli_config.db_path.resolve()
    assert result.stdout.strip() == (
        f"17 * * * * COSINE_DIGGER_HOME={home} COSINE_DIGGER_DB={db} "
        f"'/my bin/digger' playlist due >> {home}/logs/schedule.log 2>&1")
    # The log folder the redirect needs exists; nothing else was written.
    assert {p.resolve() for p in set(tmp_path.rglob("*")) - before} <= {home, home / "logs"}
    assert (home / "logs").is_dir()


def test_install_keeps_env_overrides_and_never_embeds_keys(tmp_path, monkeypatch):
    """A fresh launchd/cron environment only sees what install writes down."""
    import cosine_digger.cli as cli
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    home = tmp_path / "home"
    environ = {
        "HOME": str(tmp_path), "COSINE_DIGGER_HOME": str(home),
        "COSINE_API_TOKEN": "env-only-cosine-secret", "EXA_API_KEY": "env-only-exa-secret",
        "COSINE_DIGGER_DAILY_TIME": "18:45", "COSINE_DIGGER_YTMUSIC_AUTH": str(tmp_path / "yt.json"),
    }
    monkeypatch.setattr(cli, "load_config", lambda: load_config(environ))
    runner = CliRunner()
    args = ["schedule", "install", "--dir", str(tmp_path / "agents"), "--program", "/bin/digger"]

    refused = runner.invoke(app, args)
    assert refused.exit_code == 2
    assert "Setup page" in refused.stderr
    assert not (tmp_path / "agents").exists()

    save_settings({"cosine_token": "saved-cosine-secret"}, home)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.exception
    assert "EXA_API_KEY" in result.stderr
    plist = (tmp_path / "agents" / "club.cosine.digger.plist").read_bytes()
    env = plistlib.loads(plist)["EnvironmentVariables"]
    assert env["COSINE_DIGGER_DAILY_TIME"] == "18:45"
    assert env["COSINE_DIGGER_YTMUSIC_AUTH"] == str((tmp_path / "yt.json").resolve())
    assert env["COSINE_DIGGER_HOME"] == str(home.resolve())
    # What the scheduled job will actually load: same schedule, saved key.
    scheduled = load_config({"HOME": str(tmp_path), **env})
    assert (scheduled.daily_time, scheduled.cosine_token) == ("18:45", "saved-cosine-secret")
    for secret in (b"secret", b"COSINE_API_TOKEN", b"EXA_API_KEY"):
        assert secret not in plist


def test_a_fresh_claim_from_another_process_blocks_a_second_run(cfg):
    now = datetime(2024, 1, 1, 7)
    with closing(storage.connect(cfg.db_path)) as conn:
        storage.migrate(conn)
        # Another process (say the launchd job) claimed daily ten minutes ago.
        conn.execute("INSERT INTO job_runs (job, scheduled_for, started_at, status) VALUES ('daily', 'x', ?, 'running')",
                     ((now - timedelta(minutes=10)).astimezone().isoformat(),))
        conn.commit()
        ran = run_due(conn, cfg, lambda job, _: f"{job} ran", now=now)
        assert [item["job"] for item in ran] == ["weekly"]
        # An abandoned claim (process died) no longer blocks.
        conn.execute("UPDATE job_runs SET started_at = ? WHERE status = 'running'",
                     ((now - timedelta(hours=2)).astimezone().isoformat(),))
        conn.commit()
        assert [item["job"] for item in run_due(conn, cfg, lambda job, _: "ok", now=now)] == ["daily"]
