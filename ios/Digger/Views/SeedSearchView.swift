import DiggerKit
import SwiftUI

/// Choose up to 10 seeds (tracks from cosine.club or your library, or a pasted link) and start a dig.
/// What you've searched and chosen lives in the workspace's `NewDigDraft`, so it survives a resize.
struct SeedSearchView: View {
    @Environment(DigSession.self) private var session
    @Environment(NewDigDraft.self) private var draft
    let onStarted: () -> Void

    @State private var searching = false
    @State private var starting = false
    @State private var searchTask: Task<Void, Never>?
    @FocusState private var typing: Bool

    private static let maxSeeds = 10

    var body: some View {
        @Bindable var draft = draft
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if session.settings?.cosineToken?.usable == false {
                    AlertBox(level: .warning) {
                        Text("Digger on your computer has no working cosine.club key, so only your library can be searched. Add one in Setup on your computer.")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                // The web client's seed box: type into the LCD, then search.
                VStack(alignment: .leading, spacing: 8) {
                    SectionLabel(title: "Seed tracks")
                    HStack(spacing: 6) {
                        TextField("Search", text: $draft.query,
                                  prompt: Text("Artist and title, or a link").foregroundStyle(Theme.lcdDim))
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .submitLabel(.search)
                            .focused($typing)
                            .onSubmit { runSearch() }
                            .lcdField()
                            .accessibilityLabel("Artist and title, or a link")
                        Button("Search") { runSearch() }
                            .buttonStyle(.key)
                            .disabled(searching)
                    }
                    Text("An artist and title, or a YouTube or cosine.club link. Up to \(Self.maxSeeds) seeds.")
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                }

                if !draft.seeds.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        PanelBar(title: "Seeds", status: "\(draft.seeds.count)/\(Self.maxSeeds)")
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(draft.seeds, id: \.pointID) { seed in
                                HStack(spacing: 8) {
                                    SeedLabel(point: seed)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    Button {
                                        draft.seeds.removeAll { $0.pointID == seed.pointID }
                                    } label: {
                                        Text(verbatim: "\u{2715}")
                                    }
                                    .buttonStyle(.key(.normal, size: .small))
                                    .accessibilityLabel("Remove \(seed.title ?? seed.name)")
                                }
                                .padding(.leading, 8)
                                .padding(.vertical, 2)
                                Rule()
                            }
                            Button {
                                Task { await start() }
                            } label: {
                                HStack(spacing: 8) {
                                    Text(draft.seeds.count == 1 ? "Start dig from this seed" : "Start dig from \(draft.seeds.count) seeds")
                                    if starting { ProgressView().tint(Color(hex: 0x03170A)) }
                                }
                            }
                            .buttonStyle(.key(.go))
                            .disabled(starting)
                            .padding(.top, 10)
                            if let startProblem = draft.startProblem {
                                AlertBox(level: .error) {
                                    Text(startProblem).fixedSize(horizontal: false, vertical: true)
                                }
                                .padding(.top, 8)
                            }
                        }
                        .padding(8)
                    }
                    .background(Theme.panel2)
                    .bevel()
                    .hardShadow()
                }

                if searching {
                    LCDBusy(text: "searching", size: 20)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lcdScreen()
                } else if let problem = draft.searchProblem {
                    AlertBox(level: .error) {
                        Text(problem)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Search again") { runSearch() }
                            .buttonStyle(.key(.normal, size: .small))
                    }
                } else if let result = draft.result {
                    VStack(alignment: .leading, spacing: 0) {
                        PanelBar(title: "Results", status: "\(result.items.count)")
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(result.notes, id: \.message) { note in
                                Text(note.message)
                                    .font(.footnote)
                                    .foregroundStyle(Theme.amber)
                                    .padding(6)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .background(Theme.panel2)
                            }
                            if result.items.isEmpty {
                                Text("Nothing found for \u{201C}\(result.query)\u{201D}. Try the artist and title, or paste a YouTube or cosine.club link.")
                                    .font(.subheadline)
                                    .foregroundStyle(Theme.muted)
                                    .padding(8)
                            }
                            ForEach(result.items, id: \.pointID) { item in
                                resultRow(item)
                                Rule()
                            }
                            if draft.seeds.count >= Self.maxSeeds {
                                Text("That's 10 seeds, the most a dig starts from. Remove one to add another.")
                                    .font(.footnote)
                                    .foregroundStyle(Theme.muted)
                                    .padding(8)
                            }
                        }
                        .padding(4)
                    }
                    .background(Theme.panel)
                    .bevel(sunken: true)
                }
            }
            .padding(10)
            .frame(maxWidth: 720, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .caseBackground()
        .skinTitle("New dig")
        .onDisappear { searchTask?.cancel() }
    }

    private func resultRow(_ item: Point) -> some View {
        let chosen = draft.seeds.contains { $0.pointID == item.pointID }
        return Button {
            toggle(item)
        } label: {
            HStack(spacing: 10) {
                SeedLabel(point: item)
                    .frame(maxWidth: .infinity, alignment: .leading)
                KeyFace(kind: chosen ? .mode(on: true) : .normal, size: .small,
                        enabled: chosen || draft.seeds.count < Self.maxSeeds) {
                    Text(chosen ? "Added" : "+ Add")
                }
            }
            .padding(.leading, 8)
            .padding(.vertical, 2)
            .background(chosen ? Theme.panel3 : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!chosen && draft.seeds.count >= Self.maxSeeds)
        .accessibilityValue(chosen ? "chosen" : "")
        .accessibilityAddTraits(chosen ? .isSelected : [])
        .accessibilityHint(chosen ? "Removes this seed." : "Adds this seed.")
    }

    private func toggle(_ item: Point) {
        if let index = draft.seeds.firstIndex(where: { $0.pointID == item.pointID }) {
            draft.seeds.remove(at: index)
        } else if draft.seeds.count < Self.maxSeeds {
            draft.seeds.append(item)
        }
    }

    private func runSearch() {
        let text = draft.query.trimmingCharacters(in: .whitespacesAndNewlines)
        // The server's limits: 2 to 200 characters.
        guard text.count >= 2 else {
            draft.searchProblem = "Type at least two characters."
            draft.result = nil
            return
        }
        guard text.count <= 200 else {
            draft.searchProblem = "That's more than 200 characters. Try just the artist and title."
            draft.result = nil
            return
        }
        searchTask?.cancel()
        searchTask = Task {
            searching = true
            draft.searchProblem = nil
            // A search replaced by a newer one leaves the spinner to it.
            defer { if !Task.isCancelled { searching = false } }
            do {
                let found = try await session.search(text)
                guard !Task.isCancelled else { return }
                draft.result = found
            } catch is CancellationError {
                return
            } catch {
                draft.searchProblem = (error as? LocalizedError)?.errorDescription ?? "Search didn't work."
            }
        }
    }

    private func start() async {
        starting = true
        draft.startProblem = nil
        // Put the keyboard away first: the new step pops this screen, and popping it while a
        // search was still being typed left iOS 26 drawing it over the step.
        typing = false
        defer { starting = false }
        do {
            try await session.startDig(seeds: draft.seeds)
            draft.reset()
            onStarted()
        } catch {
            draft.startProblem = (error as? LocalizedError)?.errorDescription ?? "Digger couldn't start the dig."
        }
    }
}

/// A seed's name and where it comes from.
struct SeedLabel: View {
    let point: Point

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(point.title ?? point.name)
                .font(.subheadline)
                .foregroundStyle(Theme.white)
                .lineLimit(2)
            HStack(spacing: 6) {
                if let artist = point.artist, !artist.isEmpty { Text(artist) }
                if point.libraryID != nil { Text("· in your library") }
                if point.indexed == false { Text("· not on cosine.club yet") }
                if point.kind != "track" { Text("· \(point.kind)") }
            }
            .font(.footnote)
            .foregroundStyle(Theme.muted)
            .lineLimit(1)
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }
}
