from typer.testing import CliRunner

from cosine_digger import api
from cosine_digger.cli import app
from cosine_digger.models import SimilarTracksData, Track


def test_quick_preserves_brackets_in_tracks_and_progress(tmp_path, monkeypatch):
    seed_title, pick_title = "[untitled]", "Track [Extended Mix]"
    seed = Track(id="seed", name=seed_title, artist="SnPLO", track=seed_title)
    pick = Track(id="pick", name=pick_title, artist="Other", track=pick_title,
                 video_id="video", score=0.9)

    class Client:
        def search(self, query, limit=20):
            return [seed]

        def similar(self, track_id, limit=50, **filters):
            return SimilarTracksData(source_track=seed, similar_tracks=[pick])

        def close(self):
            pass

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setattr(api, "make_client", lambda: Client())
    result = CliRunner().invoke(app, ["quick", f"SnPLO - {seed_title}", "--no-export"])

    assert result.exit_code == 0, result.output
    assert f"Other - {pick_title}" in result.stdout
    assert f"SnPLO - {seed_title}" in result.stdout  # the pick's explanation
    assert f"SnPLO - {seed_title}" in result.stderr
    assert "[dim]" not in result.stderr
    assert "[/dim]" not in result.stderr
