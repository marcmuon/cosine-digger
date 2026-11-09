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
        expect(send).to_have_text("▶ Send to YT Music")
        expect(open_link).to_be_hidden()
        playlist_url = providers.youtube_sink().playlist_url
        # The first send opens the playlist in a new tab; YouTube Music is a local stand-in here.
        page.context.route(re.compile(r"^https://music\.youtube\.com/"),
                           lambda route: route.fulfill(content_type="text/html", body="<title>YouTube Music</title>"))
        with page.context.expect_page() as opened:
            send.click()
        opened.value.wait_for_url(playlist_url)
        page.bring_to_front()
        expect(open_link).to_be_visible()
        expect(open_link).to_have_attribute("href", playlist_url)
        expect(open_link).to_have_attribute("target", "_blank")
        expect(status).to_have_text(f"{sum(bool(p['video_id']) for p in picks)} tracks in the playlist · 1 with no video")
        expect(send).to_have_text("Update")
        expect(page.locator("#toast-region")).to_contain_text("is in YouTube Music")
        assert providers._playlists[api.dig_playlist_key(dig["dig_id"])] == [p["video_id"] for p in picks if p["video_id"]]

        page.reload()  # the link and when it was sent come back from the server
        expect(open_link).to_be_visible()
        expect(status).to_have_text(re.compile(r"^Sent \d\d:\d\d$"))

        (tmp_path / "auth.json").unlink()  # no login: the header sends you to Setup instead
        page.reload()
        expect(send).to_have_text("Connect YT Music")
        send.click()
        page.wait_for_function("location.hash === '#setup'")
        expect(page.locator("#card-youtube")).to_be_in_viewport()
        browser.close()
