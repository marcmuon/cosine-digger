import DiggerKit
import SwiftUI

/// One step in the trail, as the web client's trail draws it: its number in amber, its move, how
/// many picks and keeps, and whether it's running or playing.
struct TrailRow: View {
    @Environment(DigSession.self) private var session
    let node: TrailNode

    var body: some View {
        let step = node.step
        let counts = session.counts(for: step.id)
        let playingHere = session.playback.current?.stepID == step.id
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("\(step.number)")
                .font(.lcd(20))
                .foregroundStyle(Theme.amber)
                .frame(minWidth: 18, alignment: .trailing)
            VStack(alignment: .leading, spacing: 1) {
                Text(step.header)
                    .font(.footnote)
                    .foregroundStyle(Theme.text)
                    .lineLimit(2)
                HStack(spacing: 8) {
                    if step.isRunning {
                        Text("running").foregroundStyle(Theme.muted)
                    } else if step.isFailed {
                        Text("didn't finish").foregroundStyle(Theme.red)
                    } else {
                        Text("\(step.counts?.picks ?? session.picks(in: step.id).count) picks").foregroundStyle(Theme.muted)
                    }
                    if counts.kept > 0 {
                        Text("\u{2605}\(counts.kept)").foregroundStyle(Theme.kept)
                    }
                    if playingHere {
                        Text(verbatim: "\u{25B6}\u{FE0E}").foregroundStyle(Theme.playing)
                    }
                }
                .font(.caption)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, CGFloat(min(node.depth, 6)) * 12)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(step.number), \(step.header)")
        .accessibilityValue(spokenState(step: step, kept: counts.kept, playing: playingHere))
        .accessibilityAddTraits(session.currentStepID == step.id ? .isSelected : [])
    }

    private func spokenState(step: Step, kept: Int, playing: Bool) -> String {
        var parts: [String] = []
        if let parentID = step.parentID, let parent = session.step(parentID) {
            parts.append("made from step \(parent.number)")
        }
        if kept > 0 { parts.append("\(kept) kept") }
        if step.isRunning { parts.append("running") }
        if step.isFailed { parts.append("didn't finish") }
        if playing { parts.append("playing") }
        if session.currentStepID == step.id { parts.append("on screen") }
        return parts.joined(separator: ", ")
    }
}

/// The dig's trail in a sunken panel: its steps (the one on screen marked in yellow), its keeps,
/// and, in iPad's sidebar, the way to a new dig or another one.
struct TrailPanel: View {
    @Environment(DigSession.self) private var session
    /// Set in the sidebar, which has no dig menu of its own beside it.
    let push: ((Screen) -> Void)?
    let onOpen: () -> Void
    /// Shows the Connection screen, where the computer can log in to YouTube Music.
    let connect: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let dig = session.dig {
                    Text(dig.name)
                        .font(.ui(14, bold: false))
                        .textCase(.uppercase)
                        .foregroundStyle(Theme.white)
                        .lineLimit(2)
                        .accessibilityLabel(dig.name)
                        .accessibilityAddTraits(.isHeader)
                    Text(meta(steps: session.trail.count, kept: session.keptInDig.count))
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                        .padding(.top, 2)
                        .padding(.bottom, 10)
                    YouTubeMusicPanel(dig: dig, connect: connect)
                        .padding(.bottom, 12)
                    ForEach(session.trail) { node in
                        let current = session.currentStepID == node.id
                        Button {
                            session.open(stepID: node.id)
                            onOpen()
                        } label: {
                            TrailRow(node: node)
                                .padding(.leading, 8)
                                .padding(.trailing, 4)
                                .padding(.vertical, 6)
                                .frame(maxWidth: .infinity, minHeight: Theme.tap, alignment: .leading)
                                .background(current ? Theme.panel3 : Color.clear)
                                .overlay(alignment: .leading) {
                                    if current { Theme.selected.frame(width: 4) }
                                }
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Rule()
                    }
                    let kept = session.keptInDig
                    if !kept.isEmpty {
                        SectionLabel(title: "\u{2605} Kept in this dig (\(kept.count))")
                            .padding(.top, 18)
                            .padding(.bottom, 4)
                        ForEach(kept) { pick in
                            Button {
                                session.open(stepID: pick.stepID, select: pick.id)
                                onOpen()
                            } label: {
                                HStack(alignment: .firstTextBaseline, spacing: 6) {
                                    Text(verbatim: "\u{2605}").foregroundStyle(Theme.kept)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(pick.title)
                                            .foregroundStyle(Theme.text)
                                            .lineLimit(1)
                                        Text(pick.artist)
                                            .font(.caption)
                                            .foregroundStyle(Theme.muted)
                                            .lineLimit(1)
                                    }
                                }
                                .font(.footnote)
                                .padding(.horizontal, 4)
                                .frame(maxWidth: .infinity, minHeight: Theme.tap, alignment: .leading)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    Text("Each step was made from a pick in the step above it.")
                        .font(.caption)
                        .foregroundStyle(Theme.muted)
                        .padding(.top, 12)
                }
                if let push {
                    HStack(spacing: 6) {
                        Button { push(.newDig) } label: { Text("+ New dig") }
                            .buttonStyle(.key(.normal, size: .small))
                        Button { push(.digs) } label: { Text("Your digs") }
                            .buttonStyle(.key(.normal, size: .small))
                    }
                    .padding(.top, 14)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .well()
        .padding(6)
        .caseBackground()
    }

    private func meta(steps: Int, kept: Int) -> String {
        let count = steps == 1 ? "1 step" : "\(steps) steps"
        return kept > 0 ? "\(count) \u{00B7} \(kept) kept" : count
    }
}

/// iPad's sidebar: the dig's trail, its keeps, and your other digs.
struct TrailSidebar: View {
    let push: (Screen) -> Void
    let onOpen: () -> Void

    var body: some View {
        TrailPanel(push: push, onOpen: onOpen, connect: { push(.connection) })
            .skinTitle("Trail")
    }
}

/// The trail on a phone, pushed: choosing a step shows it.
struct TrailList: View {
    let onOpen: () -> Void
    let connect: () -> Void

    var body: some View {
        TrailPanel(push: nil, onOpen: onOpen, connect: connect)
            .skinTitle("Trail")
    }
}

/// The dig's YouTube Music keys, at the head of its trail as on the web client: send the dig to
/// its own private playlist, open the playlist, bring it up to date, or connect first.
struct YouTubeMusicPanel: View {
    @Environment(DigSession.self) private var session
    @Environment(\.openURL) private var openURL
    let dig: Dig
    let connect: () -> Void

    var body: some View {
        let send = session.youtubeSends[dig.id]
        let sending = send == .sending
        let problem = session.youtubeLoginProblem(digID: dig.id)
        let playlist = session.youtubePlaylist(digID: dig.id)
        VStack(alignment: .leading, spacing: 6) {
            // Side by side when they fit; stacked in a narrow iPad sidebar.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) { keys(sending: sending, problem: problem, playlist: playlist) }
                VStack(alignment: .leading, spacing: 6) { keys(sending: sending, problem: problem, playlist: playlist) }
            }
            if let status = status(send: send, problem: problem, sent: playlist != nil) {
                Text(status.text)
                    .font(.caption)
                    .foregroundStyle(status.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func keys(sending: Bool, problem: YouTubeLoginProblem?, playlist: URL?) -> some View {
        if let playlist {
            Button { openURL(playlist) } label: { Text("\u{25B6}\u{FE0E} Open in YT Music") }
                .buttonStyle(.key(.youtube, size: .small))
                .accessibilityLabel("Open in YouTube Music")
                .accessibilityHint("Opens this dig's playlist in YouTube Music. Digger's player pauses.")
        }
        HStack(spacing: 6) {
            Button {
                if problem != nil {
                    session.clearYouTubeSend(digID: dig.id)
                    connect()
                } else {
                    Task { await session.sendToYouTubeMusic() }
                }
            } label: {
                Text(sending ? "Sending\u{2026}" : problem != nil ? "Connect YT Music"
                     : playlist != nil ? "Update" : "\u{25B6}\u{FE0E} Send to YT Music")
            }
            .buttonStyle(.key(playlist == nil && problem == nil ? .youtube : .normal, size: .small))
            .disabled(sending)
            .accessibilityLabel(problem != nil ? "Connect YouTube Music" : playlist != nil
                                ? "Update the YouTube Music playlist" : "Send to YouTube Music")
            .accessibilityHint(problem != nil ? "Shows how the computer can log in to YouTube Music."
                               : playlist != nil ? "Its tracks become this dig as it is now."
                               : "Makes a private YouTube Music playlist of this dig: every step's tracks in order, passes left out.")
            if sending { ProgressView().tint(Theme.lcd) }
        }
    }

    /// What the last send did, else when the dig was last sent, and which steps aren't in it yet.
    private func status(send: YouTubeSend?, problem: YouTubeLoginProblem?, sent: Bool) -> (text: String, color: Color)? {
        if send == .sending {
            return (sent ? "Updating the playlist\u{2026}" : "Making a private playlist of this dig\u{2026}", Theme.muted)
        }
        if let problem {
            return (problem == .rejected ? "YouTube Music signed Digger out." : "YouTube Music isn't connected.", Theme.amber)
        }
        if case .failed(let message)? = send { return (message, Theme.red) }
        var parts: [String] = []
        var warn = false
        if case .sent(let result)? = send {
            parts.append(result.added == 1 ? "1 track in the playlist" : "\(result.added) tracks in the playlist")
            if result.passed > 0 { parts.append("\(result.passed) passed left out") }
            if result.noVideo > 0 { parts.append("\(result.noVideo) with no video") }
            if result.missing > 0 { parts.append("\(result.missing) YouTube Music didn't take") }
            if !result.verified {
                parts.append("YouTube Music hasn't shown every track yet. Open it to check.")
                warn = true
            }
        } else if let sentAt = dig.youtubeSentAt.flatMap(DigSession.date) {
            parts.append("Sent \(sentAt.formatted(date: .abbreviated, time: .shortened))")
        }
        let behind = session.youtubeStepsBehind(digID: dig.id)
        if behind > 0 {
            parts.append(behind == 1 ? "1 step isn't in it yet" : "\(behind) steps aren't in it yet")
            warn = true
        }
        if parts.isEmpty {
            return sent ? nil : ("A private playlist: every step's tracks in order, passes left out.", Theme.muted)
        }
        return (parts.joined(separator: " \u{00B7} "), warn ? Theme.amber : Theme.muted)
    }
}

/// Before a dig is open: your recent digs, or a way to start the first one.
struct HomeView: View {
    @Environment(DigSession.self) private var session
    @State private var retrying = false

    var body: some View {
        Group {
            if session.loadingDig {
                BusyScreen(text: "opening your dig")
            } else if session.recentDigs.isEmpty, session.loadingHome {
                BusyScreen(text: "reading your digs")
            } else if session.recentDigs.isEmpty, let error = session.homeError {
                StateMessage(title: "Couldn't read your digs", systemImage: "exclamationmark.triangle", message: error) {
                    Button {
                        Task {
                            retrying = true
                            await session.loadHome()
                            retrying = false
                        }
                    } label: {
                        if retrying { ProgressView().tint(Theme.lcd) } else { Text("Try again") }
                    }
                    .buttonStyle(.key(.go))
                    .disabled(retrying)
                    .accessibilityLabel("Try again")
                }
            } else if session.recentDigs.isEmpty {
                StateMessage(title: "No digs yet", systemImage: "square.stack.3d.down.right",
                             message: "Choose a seed track and Digger finds what sounds like it. Keep what you like, then follow a pick's label or people to go deeper.") {
                    NavigationLink(value: Screen.newDig) {
                        Text("Choose a seed")
                    }
                    .buttonStyle(.key(.go))
                    .accessibilityLabel("Choose a seed")
                }
            } else {
                DigsList(onOpen: {}, titled: false)
            }
        }
        .caseBackground()
        .brandTitle(session.serverName.map { "Digger on \($0)" } ?? "Digger")
    }
}

/// Your recent digs, newest first. Choosing one opens it; one that won't open says why and the
/// dig on screen stays.
struct DigsList: View {
    @Environment(DigSession.self) private var session
    let onOpen: () -> Void
    /// Off on the home screen, which names itself.
    var titled = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                NavigationLink(value: Screen.newDig) {
                    Text("+ New dig")
                }
                .buttonStyle(.key(.go))

                VStack(alignment: .leading, spacing: 0) {
                    PanelBar(title: "Your digs", status: "\(session.recentDigs.count)")
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(session.recentDigs) { dig in
                            row(dig)
                            Rule()
                        }
                    }
                    .padding(4)
                    if let error = session.homeError {
                        AlertBox(level: .warning) {
                            Text("Couldn't read the list again. \(error)")
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(8)
                    }
                }
                .background(Theme.panel)
                .bevel()
                .hardShadow()
            }
            .padding(10)
            .frame(maxWidth: 720, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .refreshable { await session.reloadRecentDigs() }
        .caseBackground()
        .skinTitle(titled ? "Your digs" : nil)
    }

    private func row(_ dig: DigSummary) -> some View {
        let open = session.dig?.id == dig.id
        return Button {
            Task {
                if await session.openDig(dig.id) { onOpen() }
            }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(dig.name)
                        .font(.subheadline)
                        .foregroundStyle(Theme.white)
                    HStack(spacing: 6) {
                        Text(dig.stepCount == 1 ? "1 step" : "\(dig.stepCount) steps")
                            .foregroundStyle(Theme.muted)
                        if dig.keptCount > 0 {
                            Text("\u{2605} \(dig.keptCount) kept").foregroundStyle(Theme.kept)
                        }
                    }
                    .font(.footnote)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if session.openingDigID == dig.id { ProgressView().tint(Theme.lcd) }
            }
            .padding(.leading, 10)
            .padding(.trailing, 6)
            .padding(.vertical, 8)
            .frame(minHeight: Theme.tap)
            .background(open ? Theme.panel3 : Color.clear)
            .overlay(alignment: .leading) {
                if open { Theme.selected.frame(width: 4) }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(session.openingDigID != nil)
        .accessibilityValue(session.openingDigID == dig.id ? "opening" : open ? "open" : "")
        .accessibilityAddTraits(open ? .isSelected : [])
    }
}
