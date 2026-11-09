import XCTest
@testable import DiggerKit

final class MovesTests: XCTestCase {
    func testPointsMatchTheWebClientAndServerProvenance() throws {
        let envelope = try decode(StepEnvelope.self, "step1")
        let pick = envelope.picks[0]
        let track = Moves.pickPoint(pick)
        XCTAssertEqual(track.pointID, "track:\(pick.trackKey)")
        XCTAssertEqual(track.sourceRank, 1)
        XCTAssertEqual(track.name, "Discovery Artist 0 - Discovery 0")
        XCTAssertEqual(Moves.selectionID(for: track), track.pointID)
        let label = Moves.factPoint(kind: "label", entity: FactEntity(id: "501", name: "Label A"), stepID: pick.stepID, rank: 1)
        XCTAssertEqual(label.pointID, "label:501")
        XCTAssertEqual(Moves.selectionID(for: label), "fact:1:label:501")
        XCTAssertEqual(Moves.factID(kind: "artist", entity: FactEntity(id: nil, name: "  Basic  Channel (2) ")),
                       "artist:name:basic channel")
    }

    func testReturningFindsThePickAMoveStartedFrom() throws {
        let parent = try decode(StepEnvelope.self, "step1")
        let child = try decode(StepEnvelope.self, "step2")
        let source = Moves.sourcePickID(selectionID: child.step.context.sourceSelectionID,
                                        parentStepID: parent.step.id, picks: parent.picks)
        XCTAssertEqual(source, "\(parent.step.id):1")
        let byTrack = Moves.sourcePickID(selectionID: "track:\(parent.picks[4].trackKey)",
                                         parentStepID: parent.step.id, picks: parent.picks)
        XCTAssertEqual(byTrack, parent.picks[4].id)
        XCTAssertNil(Moves.sourcePickID(selectionID: "origin:track:x", parentStepID: parent.step.id, picks: parent.picks))
    }

    func testChoicesSayWhyAMoveCantStart() throws {
        let envelope = try decode(StepEnvelope.self, "step1")
        let ready = Moves.choices(for: envelope.picks[0], discogsUsable: true)
        XCTAssertEqual(ready.map(\.move), [.track, .label, .artist, .person])
        XCTAssertTrue(ready.allSatisfy(\.isAvailable))
        XCTAssertEqual(ready[1].candidates.map(\.name), ["Label A"])
        XCTAssertEqual(ready[3].candidates.count, 12)
        let missing = try XCTUnwrap(envelope.picks.first { $0.factsState == "missing" })
        let noFacts = Moves.choices(for: missing, discogsUsable: true)
        XCTAssertTrue(noFacts[0].isAvailable)
        XCTAssertFalse(noFacts[1].isAvailable)
        XCTAssertTrue(noFacts[2].isAvailable, "the recording's own artist, by name")
        XCTAssertEqual(noFacts[2].candidates.first?.pointID, "artist:name:\(Moves.normalizeName(missing.artist))")
        let noToken = Moves.choices(for: envelope.picks[0], discogsUsable: false)
        XCTAssertEqual(noToken[1].unavailableReason, "Needs a Discogs token. Add one in Setup on your computer.")
        XCTAssertTrue(noToken[0].isAvailable)
    }

    func testWhyItsHereReadsLikeTheWebInspector() throws {
        let envelope = try decode(StepEnvelope.self, "step1")
        XCTAssertEqual(Explain.why(envelope.picks[1], in: envelope.step, pickCount: 25),
                       "Sounds like Fixture Artist A - Seed A. Rank 2 of 25.")
        XCTAssertEqual(Explain.short(envelope.picks[1]), "Close to Fixture Artist A - Seed A")
        XCTAssertEqual(Explain.moveTitle(.label, point: Moves.factPoint(kind: "label", entity: FactEntity(id: "1", name: "Label A"),
                                                                    stepID: "s", rank: 1)),
                       "Sounds like the label Label A")
        XCTAssertEqual(Explain.youTubeMusicURL(envelope.picks[0])?.absoluteString, "https://music.youtube.com/watch?v=jNQXAC9IVRw")
    }
}

final class PairingTests: XCTestCase {
    func testPairingLinkWithEveryAddress() throws {
        let token = String(repeating: "a", count: 43)
        let link = "cosinedigger://pair?url=http%3A%2F%2Fstudio.local%3A8765&url=http%3A%2F%2F192.168.1.20%3A8765&token=\(token)&name=studio"
        let connection = try Pairing.parse(link: "Pair with this: \(link) \n")
        XCTAssertEqual(connection.urls.map(\.absoluteString), ["http://studio.local:8765", "http://192.168.1.20:8765"])
        XCTAssertEqual(connection.token, token)
        XCTAssertEqual(connection.displayName, "studio")
    }

    func testTypedAddressGetsSchemeAndPort() throws {
        let token = String(repeating: "b", count: 43)
        XCTAssertEqual(try Pairing.connection(address: "192.168.1.20", token: token).urls.first?.absoluteString,
                       "http://192.168.1.20:8765")
        XCTAssertEqual(try Pairing.connection(address: "http://127.0.0.1:9000/", token: token).urls.first?.absoluteString,
                       "http://127.0.0.1:9000")
        XCTAssertThrowsError(try Pairing.connection(address: "ftp://x", token: token))
        XCTAssertThrowsError(try Pairing.connection(address: "http://x:8765/api", token: token))
        XCTAssertThrowsError(try Pairing.connection(address: "192.168.1.20", token: "short"))
        XCTAssertThrowsError(try Pairing.parse(link: "https://example.com"))
    }
}
