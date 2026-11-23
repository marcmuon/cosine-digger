"""Send to YT Music in the dig header, through the served app, real store and the fixture playlist sink."""

import json
import re
import time

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright

from cosine_digger import api
from cosine_digger.web.slice_testmode import SliceFixtures
from tests.slice_browser import launch, route_youtube_stub, served_app


def finished(http, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = http.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_dig_header_sends_the_dig_and_keeps_its_playlist_link(tmp_path, monkeypatch):
    # A saved login (the fixture sink never calls YouTube), so the header offers Send.
    (tmp_path / "auth.json").write_text(json.dumps({"cookie": "fixture"}))
    providers = SliceFixtures()
    with served_app(tmp_path, monkeypatch, providers=providers,
                    sink_factory=lambda conn: providers.youtube_sink(api.DbIdStore(conn))) as app, \
            httpx.Client(base_url=app.url) as http, sync_playwright() as playwright:
        dig = http.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]}).json()
        assert finished(http, dig["job_id"])["state"] == "done"
        picks = http.get(f"/api/steps/{dig['step_id']}").json()["snapshot"]["picks"]
        browser = launch(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        route_youtube_stub(page)
        page.goto(f"{app.url}/#/dig/{dig['dig_id']}/step/{dig['step_id']}")
        send, open_link, status = page.locator(".trail-yt-send"), page.locator(".trail-yt-open"), page.locator(".trail-yt-status")
        expect(send).to_have_text("▶ Send to YouTube")
        expect(open_link).to_be_hidden()
        playlist_url = providers.youtube_sink().playlist_url
        assert playlist_url.startswith("https://www.youtube.com/playlist?list=")
        # The first send opens the playlist in a new tab; YouTube is a local stand-in here.
        page.context.route(re.compile(r"^https://www\.youtube\.com/"),
                           lambda route: route.fulfill(content_type="text/html", body="<title>YouTube</title>"))
        with page.context.expect_page() as opened:
            send.click()
        opened.value.wait_for_url(playlist_url)
        page.bring_to_front()
        expect(open_link).to_be_visible()
        expect(open_link).to_have_text("▶ Open in YouTube")
        expect(open_link).to_have_attribute("href", playlist_url)
        expect(open_link).to_have_attribute("target", "_blank")
        expect(status).to_have_text(f"{sum(bool(p['video_id']) for p in picks)} tracks in the playlist · 1 with no video")
        expect(send).to_have_text("Update")
        expect(page.locator("#toast-region")).to_contain_text("is in YouTube")
        assert providers._playlists[api.dig_playlist_key(dig["dig_id"])] == [p["video_id"] for p in picks if p["video_id"]]

        # Inspector links point to www.youtube.com (/watch and /results?search_query=).
        page.locator('#step-region [data-rank="1"]').click()
        ins_link = page.locator(".inspector-actions a.inspector-link")
        expect(ins_link).to_have_text("Open in YouTube ↗")
        expect(ins_link).to_have_attribute("href", re.compile(r"^https://www\.youtube\.com/watch\?v="))
        no_video = next(p for p in picks if not p["video_id"])
        page.locator(f'#step-region [data-rank="{no_video["rank"]}"]').click()
        expect(ins_link).to_have_text("Search YouTube ↗")
        expect(ins_link).to_have_attribute("href", re.compile(r"^https://www\.youtube\.com/results\?search_query="))

        page.reload()  # the link and when it was sent come back from the server
        expect(open_link).to_be_visible()
        expect(status).to_have_text(re.compile(r"^Sent \d\d:\d\d$"))

        (tmp_path / "auth.json").unlink()  # no login: the header sends you to Setup instead
        page.reload()
        expect(send).to_have_text("Connect YouTube")
        send.click()
        page.wait_for_function("location.hash === '#setup'")
        expect(page.locator("#card-youtube")).to_be_in_viewport()
        expect(page.locator("#card-youtube-title")).to_have_text("YouTube")
        browser.close()


def test_youtube_music_target_switches_links_and_ui(tmp_path, monkeypatch):
    from cosine_digger import youtube
    from cosine_digger.commands.doctor import run_checks
    from cosine_digger.config import AppConfig

    (tmp_path / "auth.json").write_text(json.dumps({"cookie": "fixture"}))
    monkeypatch.setattr("cosine_digger.web.app.run_checks",
                        lambda config, **kwargs: run_checks(config, offline=True))
    providers = SliceFixtures()
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", ytmusic_auth_path=tmp_path / "auth.json",
                    cosine_token="fixture-cosine-key", discogs_token="fixture-discogs-key",
                    daily_size=10, weekly_size=15, youtube_target="youtube_music")
    with served_app(tmp_path, monkeypatch, providers=providers, cfg=cfg,
                    sink_factory=lambda conn: providers.youtube_sink(api.DbIdStore(conn), target="youtube_music")) as app, \
            httpx.Client(base_url=app.url) as http, sync_playwright() as playwright:
        assert http.get("/api/settings").json()["youtube"]["target"] == "youtube_music"
        dig = http.post("/api/digs", json={"seeds": [providers.point("seed-a").to_dict()]}).json()
        assert finished(http, dig["job_id"])["state"] == "done"
        picks = http.get(f"/api/steps/{dig['step_id']}").json()["snapshot"]["picks"]
        browser = launch(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        route_youtube_stub(page)
        page.goto(f"{app.url}/#/dig/{dig['dig_id']}/step/{dig['step_id']}")
        send, open_link = page.locator(".trail-yt-send"), page.locator(".trail-yt-open")
        expect(send).to_have_text("▶ Send to YT Music")
        playlist_url = providers.youtube_sink(target="youtube_music").playlist_url
        assert playlist_url.startswith("https://music.youtube.com/playlist?list=")
        page.context.route(re.compile(r"^https://music\.youtube\.com/"),
                           lambda route: route.fulfill(content_type="text/html", body="<title>YouTube Music</title>"))
        with page.context.expect_page() as opened:
            send.click()
        opened.value.wait_for_url(playlist_url)
        page.bring_to_front()
        expect(open_link).to_have_text("▶ Open in YT Music")
        expect(open_link).to_have_attribute("href", playlist_url)
        expect(page.locator("#toast-region")).to_contain_text("is in YouTube Music")

        # Both old ("YouTube Music") and new ("YouTube") server messages trigger missing/rejected login flows.
        login_checks = page.evaluate("""async ([ytSignedOut, ytmSignedOut]) => {
          const {youtubeLoginProblem} = await import('/static/state.js');
          return {
            ytMissing: youtubeLoginProblem('YouTube login missing: run `cosine-digger youtube login`'),
            ytmMissing: youtubeLoginProblem('YouTube Music login missing: run `cosine-digger youtube login`'),
            ytRejected: youtubeLoginProblem(ytSignedOut),
            ytmRejected: youtubeLoginProblem(ytmSignedOut),
            other: youtubeLoginProblem('Network timeout'),
          };
        }""", [youtube.signed_out_message("youtube"), youtube.signed_out_message("youtube_music")])
        assert login_checks == {
            "ytMissing": "missing", "ytmMissing": "missing",
            "ytRejected": "rejected", "ytmRejected": "rejected", "other": None,
        }

        # Keep one pick so the feed profile isn't empty, then verify Feeds lede and dry-run track links.
        page.locator('#step-region [data-rank="1"]').click()
        ins_link = page.locator(".inspector-actions a.inspector-link")
        expect(ins_link).to_have_text("Open in YouTube Music ↗")
        expect(ins_link).to_have_attribute("href", re.compile(r"^https://music\.youtube\.com/watch\?v="))
        page.locator(".inspector-actions [data-action='keep']").click()
        no_video = next(p for p in picks if not p["video_id"])
        page.locator(f'#step-region [data-rank="{no_video["rank"]}"]').click()
        expect(ins_link).to_have_text("Search YouTube Music ↗")
        expect(ins_link).to_have_attribute("href", re.compile(r"^https://music\.youtube\.com/search\?q="))

        page.goto(f"{app.url}/#setup")
        expect(page.locator("#card-youtube-title")).to_have_text("YouTube Music")
        expect(page.locator("#yt-desc")).to_contain_text("YouTube Music")
        expect(page.locator('#health-list [data-check="youtube_login"] .check__name')).to_have_text("YouTube Music login")

        page.goto(f"{app.url}/#feeds")
        expect(page.locator("#feeds-lede")).to_contain_text("YouTube Music")
        daily = page.locator('[data-feed-kind="daily"]')
        daily.locator('[data-action="dry-run-feed"]').click()
        first_track_link = daily.locator('.plcard__tracks a.btn--yt').first
        expect(first_track_link).to_have_text("▶ YT Music")
        expect(first_track_link).to_have_attribute("href", re.compile(r"^https://music\.youtube\.com/watch\?v="))
        browser.close()
