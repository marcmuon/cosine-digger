import DiggerKit
import SwiftUI

/// A pick's details, laid out like the web client's inspector: why it's here, the moves that go
/// deeper from it, what Discogs knows, and where else it appeared. On a phone this is pushed; on
/// iPad it sits beside the step.
struct PickDetail: View {
    @Environment(DigSession.self) private var session
    let pickID: String
    var showsTitle = true

    var body: some View {
        if let pick = session.pick(id: pickID), let step = session.step(pick.stepID) {
            let verdict = session.verdict(for: pick.trackKey)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(pick.title)
                            .font(.headline)
                            .strikethrough(verdict == .pass)
                            .foregroundStyle(Theme.white)
                        Text(pick.artist)
                            .font(.subheadline)
                            .foregroundStyle(Theme.muted)
                        PickMarks(pick: pick)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityAddTraits(.isHeader)

                    HStack(spacing: 6) {
                        DetailPlayButton(pick: pick)
                        Button {
                            Task { await session.judge(pick, .keep) }
                        } label: {
                            Text(verdict == .keep ? "\u{2605} Kept" : "\u{2605} Keep")
                        }
                        .buttonStyle(.key(verdict == .keep ? .kept : .normal, size: .small))
                        .accessibilityLabel(verdict == .keep ? "Remove keep from \(pick.title)" : "Keep \(pick.title)")
                        Button {
                            Task { await session.judge(pick, .pass) }
                        } label: {
                            Text(verdict == .pass ? "\u{2715} Passed" : "\u{2715} Pass")
                        }
                        .buttonStyle(.key(verdict == .pass ? .passed : .normal, size: .small))
                        .accessibilityLabel(verdict == .pass ? "Remove pass from \(pick.title)" : "Pass \(pick.title)")
                    }
                    .padding(.top, 8)

                    InspectorSection(title: "Why it's here") {
                        Text(Explain.why(pick, in: step, pickCount: session.picks(in: pick.stepID).count))
                            .font(.subheadline)
                            .foregroundStyle(Theme.text)
                        Text(Explain.scoreLine(pick))
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }

                    InspectorSection(title: "Go deeper") {
                        MoveButtons(pick: pick)
                        Text("Each move makes a new step from this pick. You can always come back here.")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }

                    if let facts = pick.factsState == "ready" ? pick.facts : nil {
                        FactsSection(facts: facts, pick: pick)
                    } else if pick.factsState == "loading" {
                        InspectorSection(title: "Discogs") {
                            LCDBusy(text: "looking up this record on Discogs", size: 19)
                        }
                    } else if let note = Moves.factsNote(pick, discogsUsable: session.discogsUsable) {
                        InspectorSection(title: "Discogs") {
                            Text(note)
                                .font(.footnote)
                                .foregroundStyle(Theme.muted)
                        }
                    }

                    let appearances = (session.envelopes[pick.stepID]?.live.alsoIn[pick.trackKey] ?? [])
                    if !appearances.isEmpty {
                        InspectorSection(title: "Also in") {
                            ForEach(appearances, id: \.stepID) { place in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(place.digName)
                                        .font(.subheadline)
                                        .foregroundStyle(Theme.label)
                                    Text("Step \(place.number) \u{00B7} rank \(place.rank) \u{00B7} \(place.header)")
                                        .font(.footnote)
                                        .foregroundStyle(Theme.muted)
                                }
                                .accessibilityElement(children: .combine)
                            }
                        }
                    }

                    if let url = Explain.youTubeMusicURL(pick) {
                        InspectorSection(title: "Listen elsewhere") {
                            PanelLink(title: pick.videoID == nil ? "Search YouTube Music" : "Open in YouTube Music",
                                      destination: url)
                        }
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .well()
            .padding(6)
            .caseBackground()
            .skinTitle(showsTitle ? pick.title : nil)
        } else {
            StateMessage(title: "Pick not loaded", systemImage: "questionmark.circle",
                         message: "This pick's step isn't loaded. Go back and open the step again.")
                .caseBackground()
        }
    }
}

/// Play or pause this pick, spelled out, for the details.
private struct DetailPlayButton: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        let current = session.playback.isCurrent(pick.id)
        let playing = current && session.playback.isPlaying
        Button {
            PlayButton.press(pick, in: session)
        } label: {
            Text(pick.videoID == nil ? "No video" : playing ? "\u{275A}\u{275A} Pause" : "\u{25B6}\u{FE0E} Play")
        }
        .buttonStyle(.key(current ? .active : .normal, size: .small))
        .disabled(pick.videoID == nil)
        .accessibilityLabel(pick.videoID == nil ? "No video for \(pick.title)" : playing ? "Pause \(pick.title)" : "Play \(pick.title)")
    }
}

/// One of the inspector's sections: a rule, a Silkscreen name, then its content.
struct InspectorSection<Content: View>: View {
    let title: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(title: title)
            content()
        }
        .padding(.top, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { Rule() }
        .padding(.top, 16)
    }
}

/// One button per origin a move can start from, with progress while Digger makes the step. They
/// look like the web client's moves: a framed row with the move's mark, its name and the people's roles.
struct MoveButtons: View {
    @Environment(DigSession.self) private var session
    let pick: Pick
    @State private var showAllPeople = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(session.choices(for: pick), id: \.move) { choice in
                if choice.isAvailable {
                    let points = choice.move == .person && !showAllPeople ? Array(choice.candidates.prefix(3)) : choice.candidates
                    ForEach(points, id: \.pointID) { point in
                        moveButton(choice.move, point)
                    }
                    if choice.move == .person, choice.candidates.count > 3, !showAllPeople {
                        Button("Show all \(choice.candidates.count) credited people") { showAllPeople = true }
                            .buttonStyle(.key(.normal, size: .small))
                    }
                } else {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(choice.move.title)
                            .font(.subheadline)
                            .foregroundStyle(Theme.muted)
                        Text(choice.unavailableReason ?? "Not available")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay { Rectangle().strokeBorder(Theme.line, style: StrokeStyle(lineWidth: 1, dash: [3, 2])) }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(choice.move.title) unavailable. \(choice.unavailableReason ?? "")")
                }
            }
        }
    }

    private func moveButton(_ move: MoveKind, _ point: Point) -> some View {
        let roles = move == .person ? Moves.roles(of: FactEntity(id: point.discogsID, name: point.name), in: pick) : []
        let submitting = session.isSubmitting(pick, move)
        return Button {
            Task { await session.branch(from: pick, move: move, origin: point) }
        } label: {
            HStack(spacing: 10) {
                Self.mark(move)
                    .frame(width: 24)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(Explain.moveTitle(move, point: point))
                        .font(.subheadline)
                        .foregroundStyle(Theme.text)
                    if !roles.isEmpty {
                        Text(roles.joined(separator: ", "))
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }
                }
                Spacer(minLength: 0)
                if submitting {
                    ProgressView().tint(Theme.lcd)
                } else {
                    Text(verbatim: "\u{25B8}")
                        .font(.lcd(22))
                        .foregroundStyle(Theme.lcdSoft)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, minHeight: Theme.tap, alignment: .leading)
            .background(Theme.panel)
            .overlay { Rectangle().strokeBorder(submitting ? Theme.lcdSoft : Theme.line, lineWidth: 1) }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(session.submitting != nil)
        .accessibilityLabel(Explain.moveTitle(move, point: point))
        .accessibilityValue(submitting ? "making the step" : roles.joined(separator: ", "))
        .accessibilityHint("Makes a new step from this pick.")
    }

    /// The map's mark for each kind of origin: a cyan dot for a track, an amber square for a
    /// label, a green ring for a person.
    @ViewBuilder
    static func mark(_ move: MoveKind) -> some View {
        switch move {
        case .track: Circle().fill(Theme.nearness).frame(width: 10, height: 10)
        case .label: Rectangle().fill(Theme.label).frame(width: 10, height: 10)
        case .artist: Circle().fill(Theme.white).frame(width: 10, height: 10)
        case .person: Circle().strokeBorder(Theme.lcdSoft, lineWidth: 2).frame(width: 12, height: 12)
        }
    }
}

/// What Discogs says about the record: label, year, styles, artists and credits.
struct FactsSection: View {
    let facts: Facts
    let pick: Pick
    @State private var showCredits = false

    var body: some View {
        InspectorSection(title: "Discogs") {
            if !facts.labels.isEmpty {
                fact("Label", facts.labels.map(\.name).joined(separator: ", "))
            }
            if let year = facts.year {
                fact("Year", String(year))
            }
            if !facts.styles.isEmpty {
                fact("Styles", facts.styles.joined(separator: ", "))
            }
            if !facts.artists.isEmpty {
                fact("Artists", facts.artists.map(\.name).joined(separator: ", "))
            }
            if !facts.credits.isEmpty {
                DisclosureGroup(isExpanded: $showCredits) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(facts.credits.enumerated()), id: \.offset) { _, credit in
                            fact(credit.name, credit.roles.joined(separator: ", "))
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    Text("Credits (\(facts.credits.count))")
                        .font(.subheadline)
                        .foregroundStyle(Theme.lcdSoft)
                        .frame(minHeight: Theme.tap, alignment: .leading)
                }
                .tint(Theme.lcdSoft)
            }
            if let link = facts.releaseURL, let url = URL(string: link) {
                PanelLink(title: "Open on Discogs", destination: url)
            }
        }
    }

    private func fact(_ name: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(name)
                .foregroundStyle(Theme.muted)
                .frame(width: 72, alignment: .leading)
            Text(value)
                .foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.subheadline)
        .accessibilityElement(children: .combine)
    }
}
