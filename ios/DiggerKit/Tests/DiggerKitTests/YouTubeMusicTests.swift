import Foundation
import XCTest
@testable import DiggerKit

/// Sending a dig to YouTube Music and logging in from the computer's Chrome, against the stand-in
/// Digger, which answers as `serve --companion` does (the job routes as the export job reports).
extension SessionRecoveryTests {
    static let sendJob = String(repeating: "a", count: 32)

    static func json(_ object: Any) -> Data { try! JSONSerialization.data(withJSONObject: object) }

    /// The recorded dig, as it reads once it was sent to YouTube Music at `sentAt`.
    static func sentDig(at sentAt: String) throws -> Data {
        var dig = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture("dig")) as? [String: Any])
        dig["youtube_playlist_url"] = "https://music.youtube.com/playlist?list=PLdemo&authuser=1"
        dig["youtube_sent_at"] = sentAt
        return json(dig)
    }

    func testDecodingReadsTheYouTubeLoginThePlaylistAndWhenStepsFinished() throws {
        let settings = try decode(ServerSettings.self, "settings")
        XCTAssertEqual(settings.youtube, YouTubeStatus(loggedIn: false, saved: false, checked: nil, chrome: false))
        let dig = try decode(Dig.self, "dig")
        XCTAssertNil(dig.youtubePlaylistURL)
        XCTAssertEqual(dig.steps.first?.picksDoneAt, "2026-10-21T20:32:05.233581+00:00")
        let job = try JSONDecoder().decode(BackgroundJob.self, from: Self.json([
            "id": Self.sendJob, "kind": "playlist", "state": "done", "error": NSNull(), "events": 3,
            "result": ["dig_id": "d", "playlist_url": "https://music.youtube.com/playlist?list=PL1", "sent_at": "2026-10-21T20:40:00+00:00",
                       "created": true, "verified": false, "sent": 25, "added": 23, "missing": 1, "passed": 2, "no_video": 1]]))
        XCTAssertTrue(job.isFinished)
        XCTAssertEqual(job.result?.added, 23)
        XCTAssertEqual(job.result?.noVideo, 1)
        XCTAssertEqual(job.result?.verified, false)
        // Microseconds and an offset, as Digger writes them.
        let first = try XCTUnwrap(DigSession.date("2026-10-21T20:32:05.233581+00:00"))
        let later = try XCTUnwrap(DigSession.date("2026-10-21T13:32:06-07:00"))
        XCTAssertEqual(later.timeIntervalSince(first), 0.767, accuracy: 0.002)
    }

    @MainActor
    func testSendingADigFollowsItsJobToThePlaylistAndSaysWhichStepsArentInIt() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.onceRules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json(["id": Self.sendJob, "state": "running"]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json([
            "id": Self.sendJob, "state": "done",
            "result": ["playlist_url": "https://music.youtube.com/playlist?list=PLdemo&authuser=1",
                       "sent_at": "2026-10-21T20:32:40+00:00", "verified": true, "added": 23, "missing": 0, "passed": 2, "no_video": 0]]))
        var settings = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture("settings")) as? [String: Any])
        settings["youtube"] = ["logged_in": true, "saved": true, "checked": "connected", "chrome": true]
        StubDigger.rules["/api/settings"] = .init(body: Self.json(settings))
        // Sent between the two steps' picks: the label step came after.
        StubDigger.rules["/api/digs/\(digID)"] = .init(body: try Self.sentDig(at: "2026-10-21T20:32:40+00:00"))

        await session.sendToYouTubeMusic()

        guard case .sent(let result)? = session.youtubeSends[digID] else {
            return XCTFail("expected a finished send, got \(String(describing: session.youtubeSends[digID]))")
        }
        XCTAssertEqual(result.added, 23)
        XCTAssertEqual(session.dig?.youtubePlaylistURL, "https://music.youtube.com/playlist?list=PLdemo&authuser=1",
                       "the dig is read again, with its playlist")
        XCTAssertEqual(session.notice?.link?.absoluteString, "https://music.youtube.com/playlist?list=PLdemo&authuser=1")
        XCTAssertEqual(session.youtubeStepsBehind(digID: digID), 1, "the label step's picks came after the send")
        XCTAssertNil(session.youtubeLoginProblem(digID: digID))
    }

    @MainActor
    func testASendThatNeedsALoginAsksToConnectAndALoginFromChromeClearsIt() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json([
            "id": Self.sendJob, "state": "error", "error": "YouTube Music login missing"]))

        await session.sendToYouTubeMusic()

        XCTAssertEqual(session.youtubeSends[digID], .failed("YouTube Music login missing"))
        XCTAssertEqual(session.youtubeLoginProblem(digID: digID), .missing)
        XCTAssertEqual(session.notice?.message, "Connect YouTube Music to send Fixture Artist A - Seed A.")

        var settings = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture("settings")) as? [String: Any])
        settings["youtube"] = ["logged_in": true, "saved": true, "checked": "connected", "chrome": true]
        StubDigger.rules["/api/youtube/login/chrome"] = .init(body: Self.json(
            ["ok": true, "logged_in": true, "state": "connected", "settings": settings]))

        await session.logInYouTubeFromChrome()

        XCTAssertFalse(session.youtubeLoggingIn)
        XCTAssertEqual(session.settings?.youtube?.checked, "connected")
        XCTAssertEqual(session.youtubeLoginReport?.isError, false)
        XCTAssertNil(session.youtubeSends[digID], "the failed send can go again")
        XCTAssertNil(session.youtubeLoginProblem(digID: digID))
    }

    @MainActor
    func testWhileMacOSIsAlreadyAskingALoginSaysSoAndChangesNothing() async throws {
        let session = session()
        try await session.pair(connection())
        StubDigger.rules["/api/youtube/login/chrome"] = .init(status: 409, body: Self.json(
            ["error": "Already waiting for macOS. Answer its prompt about Chrome Safe Storage."]))

        await session.logInYouTubeFromChrome()

        XCTAssertEqual(session.youtubeLoginReport,
                       YouTubeLoginReport(message: "Already waiting for macOS. Answer its prompt about Chrome Safe Storage.", isError: true))
        XCTAssertEqual(session.phase, .ready)
        XCTAssertEqual(session.settings?.youtube?.saved, false)
    }

    static let doneJob: [String: Any] = [
        "id": sendJob, "state": "done",
        "result": ["playlist_url": "https://music.youtube.com/playlist?list=PLdemo", "sent_at": "2026-10-21T20:40:00+00:00",
                   "verified": true, "added": 23, "missing": 0, "passed": 2, "no_video": 0]]

    @MainActor
    func testASendGoesOnThroughAReconnectAsOnComingBackToTheApp() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json(["id": Self.sendJob, "state": "running"]))
        let sending = Task { await session.sendToYouTubeMusic() }
        try await waitFor { session.youtubeSends[digID] == .sending }
        await session.connect()  // the app came back: a new client for the same pairing
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json(Self.doneJob))
        await sending.value
        guard case .sent? = session.youtubeSends[digID] else {
            return XCTFail("the send finished, got \(String(describing: session.youtubeSends[digID]))")
        }
    }

    @MainActor
    func testDroppedAnswersWhileFollowingASendDontEndItOrTakeDiggerOffline() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(fail: true)
        let sending = Task { await session.sendToYouTubeMusic() }
        try await Task.sleep(nanoseconds: 1_500_000_000)  // two polls lost, each after the client's own retry
        XCTAssertEqual(session.youtubeSends[digID], .sending)
        XCTAssertEqual(session.phase, .ready)
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json(Self.doneJob))
        await sending.value
        guard case .sent? = session.youtubeSends[digID] else {
            return XCTFail("the send finished, got \(String(describing: session.youtubeSends[digID]))")
        }
        XCTAssertEqual(session.phase, .ready)
    }

    @MainActor
    func testASendWhoseJobDiggerNoLongerKnowsReadsTheDigForItsPlaylist() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(status: 404, body: Self.json(["error": "Job not found"]))
        StubDigger.rules["/api/digs/\(digID)"] = .init(body: try Self.sentDig(at: "2026-10-21T20:40:00+00:00"))

        await session.sendToYouTubeMusic()  // Digger restarted while sending

        XCTAssertNil(session.youtubeSends[digID], "no send left going")
        XCTAssertNotNil(session.youtubePlaylist(digID: digID), "the dig, read again, has the playlist")
        XCTAssertEqual(session.phase, .ready)
    }

    /// The settings fixture with the saved login as Digger last checked it.
    static func settings(checked: String) throws -> Data {
        var settings = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture("settings")) as? [String: Any])
        settings["youtube"] = ["logged_in": true, "saved": true, "checked": checked, "chrome": true]
        return json(settings)
    }

    @MainActor
    func testASignedOutSendAsksToLogInUntilTheLoginIsFixed() async throws {
        let session = session()
        StubDigger.rules["/api/settings"] = .init(body: try Self.settings(checked: "connected"))
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json([
            "id": Self.sendJob, "state": "error", "error": "YouTube Music says you're signed out: the saved login has expired."]))
        // Digger records the refusal as the login's check (see test_companion.py).
        StubDigger.rules["/api/settings"] = .init(body: try Self.settings(checked: "rejected"))

        await session.sendToYouTubeMusic()
        await session.refresh()
        XCTAssertEqual(session.youtubeLoginProblem(digID: digID), .rejected, "Connect, not Update")

        StubDigger.rules["/api/settings"] = .init(body: try Self.settings(checked: "connected"))  // logged in again in Setup
        await session.refresh()  // as on coming back to the app

        XCTAssertNil(session.youtubeSends[digID])
        XCTAssertNil(session.youtubeLoginProblem(digID: digID), "Send again, not Connect")
    }

    @MainActor
    func testConnectForgetsAFailedSendAsTheWebClientDoes() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let digID = StubDigger.digID
        StubDigger.rules["/api/digs/\(digID)/export"] = .init(body: Self.json(["job_id": Self.sendJob]))
        StubDigger.rules["/api/jobs/\(Self.sendJob)"] = .init(body: Self.json([
            "id": Self.sendJob, "state": "error", "error": "YouTube Music says you're signed out: the saved login has expired."]))
        await session.sendToYouTubeMusic()
        session.clearYouTubeSend(digID: digID)
        XCTAssertNil(session.youtubeSends[digID])
    }

    @MainActor
    func testALoginWithNoAnswerInTimeSaysWhatToDoAndKeepsTheDigOnScreen() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        StubDigger.rules["/api/youtube/login/chrome"] = .init(fail: true)

        await session.logInYouTubeFromChrome()

        XCTAssertEqual(session.phase, .ready, "a slow macOS prompt isn't a lost connection")
        XCTAssertEqual(session.youtubeLoginReport?.isError, true)
        XCTAssertTrue(session.youtubeLoginReport?.message.contains("click Allow") == true)
        XCTAssertFalse(session.youtubeLoggingIn)
    }
}
