import XCTest

/// The journey on the demo Digger (recorded fixture answers, no server needed), driven the way
/// VoiceOver and Voice Control see the app: by accessibility labels. Runs on iPhone and iPad.
final class JourneyUITests: XCTestCase {
    private let firstStep = "Sounds like Fixture Artist A - Seed A"
    private let labelStep = "Sounds like the label Label A"

    override func setUp() {
        continueAfterFailure = false
    }

    private func launch(_ arguments: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-DiggerDemo"] + arguments
        app.launch()
        return app
    }

    func testSeedKeepBranchIntoTheLabelAndReturn() {
        let app = launch()
        XCTAssertTrue(app.staticTexts[firstStep].waitForExistence(timeout: 15))

        // Inspect a candidate: selecting it opens its details and plays nothing.
        let row = app.buttons["Discovery 0, Discovery Artist 0"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()
        XCTAssertFalse(app.buttons["Close player"].exists, "selecting a pick never starts playback")

        // It was kept on the computer earlier: clear the keep, then keep it again. On a wide iPad
        // the row and the details beside it both have these keys, so take the first.
        let kept = app.buttons["Remove keep from Discovery 0"].firstMatch
        XCTAssertTrue(kept.waitForExistence(timeout: 5))
        kept.tap()
        let keep = app.buttons["Keep Discovery 0"].firstMatch
        XCTAssertTrue(keep.waitForExistence(timeout: 5))
        keep.tap()
        XCTAssertTrue(app.buttons["Remove keep from Discovery 0"].waitForExistence(timeout: 5))

        // Branch into its label.
        let move = app.buttons[labelStep]
        XCTAssertTrue(move.waitForExistence(timeout: 5))
        move.tap()
        XCTAssertTrue(app.staticTexts[labelStep].waitForExistence(timeout: 10))

        // Return: the first step, with the pick the move started from still selected.
        let back = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Back to Step 1")).firstMatch
        XCTAssertTrue(back.waitForExistence(timeout: 5))
        back.tap()
        XCTAssertTrue(app.staticTexts[firstStep].waitForExistence(timeout: 5))
        let returned = app.buttons["Discovery 0, Discovery Artist 0"]
        XCTAssertTrue(returned.waitForExistence(timeout: 5))
        let marks = returned.value as? String ?? ""
        XCTAssertTrue(marks.contains("selected"), "marks were: \(marks)")
        XCTAssertTrue(marks.contains("kept"), "marks were: \(marks)")
    }

    func testPlayingShowsTheVisiblePlayerAndClosingStopsIt() {
        let app = launch()
        let row = app.buttons["Discovery 0, Discovery Artist 0"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        let play = app.buttons["Play Discovery 0"].firstMatch  // the row's or, on a wide iPad, the details'
        XCTAssertTrue(play.waitForExistence(timeout: 5))
        play.tap()
        let close = app.buttons["Close player"]
        XCTAssertTrue(close.waitForExistence(timeout: 5))
        XCTAssertTrue(app.otherElements["YouTube video"].exists || app.webViews.firstMatch.exists)
        close.tap()
        XCTAssertTrue(close.waitForNonExistence(timeout: 2))
    }

    func testNoDigsYetOffersASeed() {
        let app = launch(["-DiggerDemoEmpty"])
        XCTAssertTrue(app.staticTexts["No digs yet"].waitForExistence(timeout: 15))
        app.buttons["Choose a seed"].tap()
        XCTAssertTrue(app.navigationBars["New dig"].waitForExistence(timeout: 5))
    }

    func testCantReachDiggerOffersTryAgain() {
        let app = launch(["-DiggerDemoOffline"])
        XCTAssertTrue(app.staticTexts["Can't reach Digger"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["Try again"].exists)
    }

    /// The trail: pushed on a phone, the sidebar on a wide iPad (hidden in portrait until shown).
    private func openTrail(_ app: XCUIApplication) {
        XCTAssertTrue(app.staticTexts[firstStep].waitForExistence(timeout: 15))
        if app.buttons["Trail"].exists {
            app.buttons["Trail"].tap()
        } else if app.buttons["Show Sidebar"].exists {
            app.buttons["Show Sidebar"].tap()
        }
    }

    func testSendingTheDigToYouTubeMusicFromItsTrail() {
        let app = launch()
        openTrail(app)
        let send = app.buttons["Send to YouTube Music"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        XCTAssertTrue(app.buttons["Open in YouTube Music"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Update the YouTube Music playlist"].exists)
        XCTAssertTrue(app.staticTexts["35 tracks in the playlist \u{00B7} 2 passed left out \u{00B7} 1 with no video"].exists)
    }

    func testSendingTheDigToYouTubeMusicFromTheDigMenu() {
        let app = launch()
        XCTAssertTrue(app.staticTexts[firstStep].waitForExistence(timeout: 15))
        app.buttons["Dig menu"].tap()
        let send = app.buttons["Send to YT Music"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        // The notice says when it's done, with the playlist to open.
        XCTAssertTrue(app.staticTexts["Fixture Artist A - Seed A is in YouTube Music: 35 tracks."].waitForExistence(timeout: 10))
        app.buttons["Dig menu"].tap()
        XCTAssertTrue(app.buttons["Open in YT Music"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Update YT Music playlist"].exists)
    }

    func testConnectingYouTubeMusicFromChromeOnTheMac() {
        let app = launch(["-DiggerDemoYTDisconnected"])
        openTrail(app)
        let connect = app.buttons["Connect YouTube Music"]
        XCTAssertTrue(connect.waitForExistence(timeout: 5))
        connect.tap()
        let login = app.buttons["Log in from Chrome on your Mac"]
        XCTAssertTrue(login.waitForExistence(timeout: 5))
        login.tap()
        // While the computer waits on macOS, the app says what to do there.
        XCTAssertTrue(app.staticTexts["Click Allow on your Mac when macOS asks to use \u{201C}Chrome Safe Storage\u{201D}."]
            .waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["Logged in from Chrome. Digger can send digs to YouTube Music."].waitForExistence(timeout: 10))
        if !app.buttons["Send to YouTube Music"].exists { app.navigationBars.buttons.element(boundBy: 0).tap() }
        XCTAssertTrue(app.buttons["Send to YouTube Music"].waitForExistence(timeout: 5))
    }
}
