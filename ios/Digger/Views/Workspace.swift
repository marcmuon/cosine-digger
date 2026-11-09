import DiggerKit
import SwiftUI

/// Screens pushed onto a navigation stack.
enum Screen: Hashable {
    case pick(String)
    case trail
    case digs
    case newDig
    case connection

    /// The wide composition shows these in a column of their own instead of pushing them.
    var hasColumn: Bool {
        switch self {
        case .pick, .trail: return true
        case .digs, .newDig, .connection: return false
        }
    }
}

/// A new dig being put together. It lives above both compositions, so resizing the window while
/// choosing seeds keeps the search, its results and the seeds chosen so far.
@Observable
@MainActor
final class NewDigDraft {
    var query = ""
    var result: SearchResult?
    var seeds: [Point] = []
    var searchProblem: String?
    var startProblem: String?

    func reset() {
        query = ""
        result = nil
        seeds = []
        searchProblem = nil
        startProblem = nil
    }
}

/// One dig, in the composition that fits the width. Both compositions read the same session and
/// share one navigation path, so going from one to the other (rotating an iPad mini, Split View,
/// Slide Over, Stage Manager) keeps the current step, the selection in every step, each step's
/// scroll position, the playing track, and a new dig half chosen.
struct Workspace: View {
    @Environment(DigSession.self) private var session
    @Environment(\.horizontalSizeClass) private var sizeClass
    /// "narrow" or "wide" forces a composition, for trying the resize on an iPad (Connection
    /// screen, or launch with `-layoutOverride narrow`); anything else follows the width.
    @AppStorage("layoutOverride") private var layoutOverride = "automatic"
    @State private var path: [Screen] = []
    @State private var draft = NewDigDraft()

    /// Narrower than this, side-by-side columns leave the player and a pick's details too cramped.
    static let narrowest: CGFloat = 640

    var body: some View {
        GeometryReader { geometry in
            let compact = isCompact(width: geometry.size.width)
            Group {
                if compact {
                    CompactWorkspace(path: $path)
                } else {
                    WideWorkspace(path: $path, width: geometry.size.width)
                }
            }
            .onChange(of: compact) { _, nowCompact in
                // A pick or the trail pushed on a phone-width window has its own column when wide.
                if !nowCompact { path.removeAll { $0.hasColumn } }
            }
        }
        .environment(draft)
        .safeAreaInset(edge: .bottom, spacing: 0) { NoticeBanner() }
        .background { UndoShortcut(enabled: path.last != .newDig) }
        .onChange(of: session.currentStepID) {
            // A move, a step chosen in the trail, or a new dig: show that step.
            path.removeAll()
        }
    }

    private func isCompact(width: CGFloat) -> Bool {
        switch layoutOverride {
        case "narrow": return true
        case "wide": return false
        default: return sizeClass == .compact || width < Self.narrowest
        }
    }
}

/// iPhone, and iPad at narrow widths: the player stays on top while you move through the step,
/// a pick's details and the trail below it. Nothing covers the player while it plays.
struct CompactWorkspace: View {
    @Environment(DigSession.self) private var session
    @Binding var path: [Screen]

    var body: some View {
        VStack(spacing: 0) {
            if session.playback.isActive {
                PlayerPanel(onGoToPlaying: { path.removeAll() })
            } else if session.dig != nil {
                IdlePlayerPanel()
            }
            NavigationStack(path: $path) {
                CurrentStep(openDetail: { pick in path.append(.pick(pick.id)) })
                    .toolbar {
                        ToolbarItemGroup(placement: .topBarLeading) {
                            if session.dig != nil {
                                Button {
                                    path.append(.trail)
                                } label: {
                                    Text("Trail")
                                }
                                .buttonStyle(.key(.normal, size: .bar))
                                .accessibilityLabel("Trail")
                            }
                            BackToParentButton()
                        }
                        ToolbarItem(placement: .topBarTrailing) { DigMenu(push: { path.append($0) }) }
                    }
                    .navigationDestination(for: Screen.self) { screen in
                        Destination(screen: screen, path: $path)
                    }
            }
            .safeAreaInset(edge: .top, spacing: 0) { StatusBanners() }
        }
        .caseBackground()
    }
}

/// What a pushed screen shows. A screen that changes the step clears the stack.
struct Destination: View {
    let screen: Screen
    @Binding var path: [Screen]

    var body: some View {
        switch screen {
        case .pick(let id): PickDetail(pickID: id)
        case .trail: TrailList(onOpen: { path.removeAll() }, connect: { path.append(.connection) })
        case .digs: DigsList(onOpen: { path.removeAll() })
        case .newDig: SeedSearchView(onStarted: { path.removeAll() })
        case .connection: ConnectionView()
        }
    }
}

/// iPad: the trail, the step, and the player above the selected pick's details, side by side.
struct WideWorkspace: View {
    @Environment(DigSession.self) private var session
    @Binding var path: [Screen]
    let width: CGFloat
    @State private var columns: NavigationSplitViewVisibility = .automatic

    /// Below this the trail tucks away, so the step and the player keep their room.
    static let roomForThree: CGFloat = 1000

    var body: some View {
        NavigationSplitView(columnVisibility: $columns) {
            // Choosing a step or a keep shows it, even when it's the step already open.
            TrailSidebar(push: { path.append($0) }, onOpen: { path.removeAll() })
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 320)
        } content: {
            NavigationStack(path: $path) {
                CurrentStep(openDetail: nil)
                    .toolbar {
                        ToolbarItem(placement: .topBarLeading) { BackToParentButton() }
                        ToolbarItem(placement: .topBarTrailing) { DigMenu(push: { path.append($0) }) }
                    }
                    .navigationDestination(for: Screen.self) { screen in
                        Destination(screen: screen, path: $path)
                    }
            }
            .safeAreaInset(edge: .top, spacing: 0) { StatusBanners() }
            .navigationSplitViewColumnWidth(min: 320, ideal: 400, max: 520)
        } detail: {
            NavigationStack {
                DetailColumn(onGoToPlaying: { path.removeAll() })
            }
        }
        .navigationSplitViewStyle(.balanced)
        .onChange(of: width < Self.roomForThree, initial: true) { _, tight in
            columns = tight ? .doubleColumn : .all
        }
    }
}

/// The step on screen, or the list of digs before one is open.
struct CurrentStep: View {
    @Environment(DigSession.self) private var session
    let openDetail: ((Pick) -> Void)?

    var body: some View {
        if let stepID = session.currentStepID {
            StepScreen(stepID: stepID, openDetail: openDetail)
                .id(stepID)
        } else {
            HomeView()
        }
    }
}

/// Back to the step this one was made from. It sits in the toolbar so it's there however far
/// down the step you've scrolled.
struct BackToParentButton: View {
    @Environment(DigSession.self) private var session

    var body: some View {
        if let step = session.currentStep, let parentID = step.parentID, let parent = session.step(parentID) {
            Button {
                session.openParent()
            } label: {
                Text("\u{25C0}\u{FE0E} \(parent.title)")
            }
            .buttonStyle(.key(.normal, size: .bar))
            .accessibilityLabel("Back to \(parent.title)")
            .accessibilityHint("Shows the step this one was made from, with that pick selected.")
        }
    }
}

/// Undo, new dig, other digs, and the connection.
struct DigMenu: View {
    @Environment(DigSession.self) private var session
    @Environment(\.openURL) private var openURL
    let push: (Screen) -> Void

    var body: some View {
        Menu {
            if let summary = session.undoSummary {
                Button {
                    Task { await session.undo() }
                } label: {
                    Label("Undo \u{201C}\(summary)\u{201D}", systemImage: "arrow.uturn.backward")
                }
                Divider()
            }
            if let dig = session.dig {
                // The trail's YouTube Music keys, reachable from the step too.
                if session.youtubeLoginProblem(digID: dig.id) != nil {
                    Button {
                        session.clearYouTubeSend(digID: dig.id)
                        push(.connection)
                    } label: { Label("Connect YT Music", systemImage: "music.note.list") }
                } else {
                    Button {
                        Task { await session.sendToYouTubeMusic() }
                    } label: {
                        Label(session.youtubePlaylist(digID: dig.id) == nil ? "Send to YT Music" : "Update YT Music playlist",
                              systemImage: "music.note.list")
                    }
                    .disabled(session.youtubeSends[dig.id] == .sending)
                }
                if let playlist = session.youtubePlaylist(digID: dig.id) {
                    Button { openURL(playlist) } label: { Label("Open in YT Music", systemImage: "arrow.up.forward.app") }
                }
                Divider()
            }
            Button { push(.newDig) } label: { Label("New dig", systemImage: "plus") }
            Button { push(.digs) } label: { Label("Your digs", systemImage: "square.stack") }
            Divider()
            Button { push(.connection) } label: { Label("Connection", systemImage: "desktopcomputer") }
        } label: {
            KeyFace(size: .bar) {
                Text("Dig \u{25BE}")
            }
        }
        .accessibilityLabel("Dig menu")
    }
}

/// ⌘Z with a keyboard on iPad: undo the latest keep or pass made here. Off while a text field
/// that has its own undo is on screen.
struct UndoShortcut: View {
    @Environment(DigSession.self) private var session
    let enabled: Bool

    var body: some View {
        Button("Undo") { Task { await session.undo() } }
            .keyboardShortcut("z", modifiers: .command)
            .disabled(!enabled || !session.canUndo)
            .opacity(0)
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
    }
}

/// The selected pick, below the player.
struct DetailColumn: View {
    @Environment(DigSession.self) private var session
    let onGoToPlaying: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            if session.playback.isActive {
                PlayerPanel(onGoToPlaying: onGoToPlaying)
                    .padding([.horizontal, .top], 6)
            } else {
                IdlePlayerPanel()
                    .padding([.horizontal, .top], 6)
            }
            if let pick = session.selectedPick {
                // A new pick is a new screen: its scroll position and "show all people" start over.
                PickDetail(pickID: pick.id, showsTitle: false)
                    .id(pick.id)
            } else if session.currentStepID != nil {
                StateMessage(title: "Choose a pick", systemImage: "hand.point.left",
                             message: "Select a pick to see why it's here and where you can go from it. Play starts only when you press play.")
            } else {
                StateMessage(title: "No dig open", systemImage: "square.stack.3d.down.right",
                             message: "Start a dig from a seed, or open one of your digs.")
            }
        }
        .caseBackground()
        .skinTitle(session.selectedPick?.title ?? "Details")
    }
}

/// Above the step: Digger can't be reached, or a move is being made.
struct StatusBanners: View {
    var body: some View {
        VStack(spacing: 0) {
            OfflineBanner()
            WorkingBanner()
        }
    }
}

/// Shown while Digger can't be reached but a dig is still on screen.
struct OfflineBanner: View {
    @Environment(DigSession.self) private var session
    @State private var retrying = false

    var body: some View {
        if case .offline(let message) = session.phase {
            AlertBox(level: .warning) {
                Text(message)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Task {
                        retrying = true
                        await session.connect()
                        retrying = false
                    }
                } label: {
                    if retrying { ProgressView().tint(Theme.lcd) } else { Text("Try again") }
                }
                .buttonStyle(.key(.normal, size: .small))
                .disabled(retrying)
                .accessibilityLabel("Try again")
            }
            .padding(.horizontal, 6)
            .padding(.top, 6)
            .accessibilityElement(children: .contain)
        }
    }
}

/// While a move or a rerun is being sent: what's being made. The new step opens when it's made.
struct WorkingBanner: View {
    @Environment(DigSession.self) private var session

    var body: some View {
        if let description = session.workingDescription {
            LCDBusy(text: description, size: 20)
                .lineLimit(2)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .lcdScreen()
                .padding(.horizontal, 6)
                .padding(.top, 6)
            .onAppear { AccessibilityNotification.Announcement(description).post() }
        }
    }
}
