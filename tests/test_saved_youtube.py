import sqlite3
from contextlib import closing

import typer

import cosine_digger.cli as cli
from cosine_digger.models import Track
from tests.test_youtube import FakeYTMusic


def _added(youtube):
    return [vid for call in youtube.calls if call[0] == "add" for vid in call[2]]


def _dig_export(monkeypatch, tmp_path, youtube):
    from io import StringIO
    from rich.console import Console
    from cosine_digger.fusion import RankedTrack

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output))
    monkeypatch.setattr("cosine_digger.youtube._authenticated_ytm", lambda: youtube)
    tracks = [
        Track(id="a", name="A", artist="Artist A", track="A", video_id="video-a"),
        Track(id="b", name="B", artist="Artist B", track="B"),
    ]
    ranked = [RankedTrack(track=track, final_score=0.8, match_count=1, avg_score=0.8, seed_scores={}) for track in tracks]
    try:
        cli._finish_dig(client=object(), ranked=ranked, mode="centroid", seeds=["seed"],
                        resolved_seeds=[], limit=2, youtube=True, skip_table=True, playlist_name="Dig Test")
    except typer.Exit as exc:
        return output.getvalue(), exc.exit_code
    return output.getvalue(), 0


def test_dig_export_goes_through_the_ledger_and_replaces_by_name(monkeypatch, tmp_path):
    youtube = FakeYTMusic()
    text, code = _dig_export(monkeypatch, tmp_path, youtube)
    assert code == 0 and _added(youtube) == ["video-a"]
    assert "1 result(s) have no YouTube video" in text and "verified" in text
    text, code = _dig_export(monkeypatch, tmp_path, youtube)
    assert code == 0
    assert [c[0] for c in youtube.calls].count("create") == 1       # same name: replaced, not duplicated
    with closing(sqlite3.connect(tmp_path / "taste.db")) as db:
        assert {r[0] for r in db.execute("SELECT track_id FROM served_ledger")} == {"a"}


def test_dig_export_failure_hides_provider_text_and_exits_nonzero(monkeypatch, tmp_path):
    class Leaky(FakeYTMusic):
        def add_playlist_items(self, pid, ids, duplicates):
            raise ValueError("Illegal header value b'SAPISIDHASH synthetic-cookie-secret'")

    text, code = _dig_export(monkeypatch, tmp_path, Leaky())
    assert code == 1
    assert "synthetic-cookie-secret" not in text and "SAPISID" not in text
    assert "NOT verified" in text
