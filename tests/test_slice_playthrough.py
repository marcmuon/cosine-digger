"""Complete playthroughs through the PEP 723 CLI/server/SQLite and mounted stub."""

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from cosine_digger import api, storage
from scripts import slice_playthrough as runner


def seed_synthetic_library(conn, track_count):
    api.import_library(conn, [{"id": f"synthetic-{index}",
                              "artist": f"Synthetic Artist {index:05}",
                              "title": f"Synthetic Title {index:05}",
                              "added_at": "2026-09-01T00:00:00+00:00"}
                             for index in range(track_count)], source="playthrough-synthetic")
    storage.record_served(conn, "synthetic-daily", [
        {"track_id": f"synthetic-served-{index}", "artist": f"Served Artist {index:05}",
         "title": f"Served Title {index:05}"} for index in range(146)])


def invoke(tmp_path, source, *arguments):
    artifacts = tmp_path / "evidence"
    # uv's own cache (UV_CACHE_DIR when set): a fixed macOS-only path with --offline failed on a fresh
    # machine or Linux, where nothing had filled it, and after a reboot cleared /private/tmp.
    environment = dict(os.environ)
    result = subprocess.run(["uv", "run", "scripts/slice_playthrough.py",
                             "--db", str(source), "--artifacts", str(artifacts), *arguments],
                            cwd=runner.ROOT, env=environment, capture_output=True, text=True,
                            timeout=runner.PLAYTHROUGH_TIMEOUT_SECONDS)
    if '--live' in arguments:artifacts=artifacts/'live'
    return result, json.loads((artifacts / "evidence.json").read_text()), artifacts


@pytest.mark.parametrize("filtered", [False])
def test_pep723_real_cli_backup_browser_groups_and_cleanup(tmp_path, monkeypatch, filtered):
    # Keep committed content in WAL while the runner backs it up. A file copy
    # would miss this row. Settings/auth next to it must never reach the sandbox.
    source = tmp_path / "source" / "taste.db"
    sentinel = "PRIVATE-S13A-SENTINEL"
    source.parent.mkdir()
    (source.parent / "settings.json").write_text(json.dumps({"cosine_token": sentinel}))
    (source.parent / "auth.json").write_text(sentinel)
    monkeypatch.setenv("COSINE_API_TOKEN", sentinel)
    monkeypatch.setenv("DISCOGS_TOKEN", sentinel)
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(source.parent / "auth.json"))
    monkeypatch.setenv("COSINE_DIGGER_MUSIC_DIR", str(source.parent / "forbidden-library"))
    with closing(storage.connect(source)) as conn:
        storage.migrate(conn)
        if not filtered:
            seed_synthetic_library(conn, 10808)
        conn.execute("CREATE TABLE backup_probe (value TEXT)")
        conn.execute("INSERT INTO backup_probe VALUES ('committed in WAL')")
        if not filtered:
            # A real source DB can already hold saved digs. The forty-island
            # journey must measure its own dig after ordinary app hydration.
            from cosine_digger.slice_types import Point

            origin=Point(point_id='track:earlier-seed',kind='track',name='Earlier Artist - Earlier Seed',
                         artist='Earlier Artist',title='Earlier Seed')
            dig=storage.create_dig(conn,[origin])
            saved=storage.create_step(conn,dig_id=dig,parent_id=None,origins=[origin],move='seed',further=False)
            storage.append_slice_event(conn,saved.job_id,'picks_done',{'pick_count':0},redact=lambda value:value)
            storage.append_slice_event(conn,saved.job_id,'done',{'status':'done','pick_count':0},redact=lambda value:value)
        conn.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        before = runner.counts_at(source)
        legacy_before = runner.legacy_hashes(source)
        if not filtered:
            assert before["tracks"] == 10808 and before["served_ledger"] == 146
        arguments = ("--only", "state,shell") if filtered else ()
        result, report, artifacts = invoke(tmp_path, source, *arguments)
        # Name each failed check: pytest cuts the whole report short, and CI deletes evidence.json.
        # A framework check belongs to the group whose "browser closed" check comes next.
        failed, closed = [], 0
        for item in report["framework"]["checks"]:
            if item["status"] == "FAIL":
                during = report["selected_groups"][closed] if closed < len(report["selected_groups"]) else "the end"
                failed.append(f"framework ({during}): {item['name']}: "
                              f"expected {item['expected']!r}, observed {item['observed']!r}")
            closed += item["name"] == "browser closed"
        failed += [f"{name}: {item['name']}: expected {item['expected']!r}, observed {item['observed']!r}"
                   for name, group in report["groups"].items() for item in group["checks"] if item["status"] == "FAIL"]
        assert result.returncode == 0, "\n".join(failed) or result.stdout
        assert result.stdout.splitlines()[-1] == "PLAYTHROUGH: ALL PASS"
        assert runner.counts_at(source) == before
        assert runner.legacy_hashes(source) == legacy_before
        assert report["framework"]["source_counts_before"] == report["framework"]["source_counts_after"] == before
        assert report["groups"]["state"]["status"] == report["groups"]["shell"]["status"] == "PASS"
        assert all(item["status"] == "PASS" for item in report["framework"]["checks"])
        assert report["browser_mode"] == ("sandbox-shell" if os.environ.get("SLICE_CHROMIUM_EXE") else "installed-chrome")
        assert report["source"] == "STUB" and report["framework"]["fixture_gate"]
        assert report["framework"]["stub_gate"] and report["framework"]["scheduler"] is False
        assert report["framework"]["backup_method"] == "sqlite3.Connection.backup"
        assert report["framework"]["source_counts_before"]["backup_probe"] == 1
        assert report["groups"]["state"]["ranks"] == list(range(1, 26))
        actions = report["groups"]["state"]["judgment_actions"]
        assert {event["action"] for event in actions} >= {"row Keep", "row Pass", "X"}
        assert all(event["trusted"] for event in actions)
        row_ids = {event["pick_id"] for event in actions if event["action"].startswith("row ")}
        skipped = next(event for event in actions if event["action"] == "X")
        assert skipped["pick_id"] not in row_ids
        state_checks = {item["name"]: item for item in report["groups"]["state"]["checks"]}
        assert state_checks["PT06 X passes playing source and skips it"]["status"] == "PASS"
        restored_capture = report["groups"]["state"]["captures"]["C14-1440"]
        assert restored_capture["step_number"] == 1
        assert restored_capture["step_id"] != report["groups"]["state"]["intent_trace"]["viewed"]
        for group in runner.GROUPS[2:]:
            assert report["groups"][group]["status"] == ("GAP" if filtered else "PASS")
            assert report["groups"][group]["selected"] is (not filtered)
        if not filtered:
            assert set(report["journeys"]) == {f"PT{number:02}" for number in range(1,14)}
            assert all(journey["status"] == "PASS" for journey in report["journeys"].values())
            assert all(journey["paths"] for journey in report["journeys"].values())
            assert report["groups"]["state"]["intent_trace"]["latest"]["step_id"] == report["groups"]["state"]["intent_trace"]["viewed"]
            assert len(report["groups"]["territory"]["islands"]) == 40
            feeds = {item["name"]:item for item in report["groups"]["feeds"]["checks"]}
            assert feeds["feed library is the single fixture seed"]["observed"] == [[1]]
            assert feeds["dry run writes no playlist/served ledger/CLI taste"]["observed"]["served_ledger"] == 146
            assert feeds["dry runs preserve legacy contents"]["observed"] == legacy_before
            delayed={c["name"]:c for c in report["groups"]["seeds-list"]["checks"]}
            assert delayed["O1 count replaces tilde before facts finish"]["observed"] == ["running","25",True]
            assert delayed["O1 where-next does not wait for facts"]["observed"] == [True,False]
            assert report["groups"]["setup"]["generated_files"]
        assert "Selected groups: " + ", ".join(report["selected_groups"]) in (artifacts / "report.md").read_text()
        zoom = report["groups"]["shell"]["zoom"]
        assert zoom["method"] == "CDP CSS viewport/DPR surrogate; not true browser zoom"
        assert zoom["metrics"] == {"width":720,"height":450,"dpr":zoom["before"]["dpr"]*2}
        assert zoom["capture_metrics"] == zoom["metrics"]
        assert "not true browser zoom" in (artifacts / "report.md").read_text()
        assert report["groups"]["shell"]["paths"]
        assert all((artifacts / path).is_file() for path in report["groups"]["shell"]["paths"])
        contents = result.stdout + result.stderr + (artifacts / "evidence.json").read_text() + (artifacts / "report.md").read_text()
        assert sentinel not in contents
        assert str(source.parent) not in contents
        assert not (source.parent / "forbidden-library").exists()
        with socket.socket() as sock:
            assert report["framework"]["port"] != 8777
            assert sock.connect_ex(("127.0.0.1", report["framework"]["port"])) != 0
