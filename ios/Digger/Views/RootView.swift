import DiggerKit
import SwiftUI
import UIKit

/// Pairing, connecting, a lost connection, or the dig itself.
struct RootView: View {
    @Environment(DigSession.self) private var session
    @Environment(\.scenePhase) private var scenePhase
    @State private var wasAway = false
    @State private var pairingProblem: String?
    /// The problem screen a pasted link was tried from: it stays while the link is tried.
    @State private var pairingFrom: Showing?
    /// A link another app opened while this device is paired, waiting to be confirmed.
    @State private var openedLink: Connection?

    /// What fills the window.
    private enum Showing: Equatable {
        case pairing
        case connecting
        case offline(String)
        case unauthorized(String)
        case workspace
    }

    private var showing: Showing {
        if session.phase == .connecting, let pairingFrom { return pairingFrom }
        switch session.phase {
        case .unpaired:
            return .pairing
        // Pairing for the first time: the pairing screen shows its own progress and problems.
        case .connecting where session.connection == nil:
            return .pairing
        case .connecting where session.dig == nil:
            return .connecting
        case .unauthorized(let message):
            return .unauthorized(message)
        case .offline(let message) where session.dig == nil:
            return .offline(message)
        default:
            return .workspace
        }
    }

    var body: some View {
        content
            .task { await session.connect() }
            .onOpenURL { url in opened(url) }
            .confirmationDialog(openedLinkTitle, isPresented: Binding(get: { openedLink != nil }, set: { if !$0 { openedLink = nil } }),
                                titleVisibility: .visible, presenting: openedLink) { link in
                Button("Pair with \(link.displayName)") { Task { await attempt { link } } }
                Button("Cancel", role: .cancel) {}
            } message: { link in
                Text(openedLinkMessage(link))
            }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .active:
                    // Coming back: read Digger again, so a step that ran meanwhile is whole.
                    if wasAway { Task { await session.connect() } }
                    wasAway = false
                case .inactive, .background:
                    // The player can't be seen: no hidden or background playback.
                    session.playback.pauseForApp()
                    if phase == .background { wasAway = true }
                @unknown default:
                    session.playback.pauseForApp()
                }
            }
            .onChange(of: showing == .workspace) { _, visible in
                // The player goes with the workspace; nothing keeps playing behind another screen.
                if !visible { session.playback.pauseForApp() }
            }
    }

    @ViewBuilder
    private var content: some View {
        switch showing {
        case .pairing:
            PairingView(problem: $pairingProblem, connecting: session.phase == .connecting,
                        pair: pair(link:), pairByHand: pair(address:token:))
        case .connecting:
            ConnectingView(name: session.connection?.displayName)
        case .unauthorized(let message):
            ConnectionProblemView(kind: .unauthorized, message: message, pairingProblem: pairingProblem, pair: pair(link:))
        case .offline(let message):
            ConnectionProblemView(kind: .offline, message: message, pairingProblem: pairingProblem, pair: pair(link:))
        case .workspace:
            Workspace()
        }
    }

    /// A pairing link, pasted or opened. While a dig is on screen a link that doesn't work says
    /// so in a notice and the pairing that works stays.
    private func pair(link: String) async {
        await attempt { try Pairing.parse(link: link) }
    }

    /// A link opened from AirDrop, Messages or Safari. The first pairing goes ahead; one that would
    /// replace this device's pairing asks first, since any app or web page can open a link.
    private func opened(_ url: URL) {
        guard let current = session.connection, let link = try? Pairing.parse(link: url.absoluteString),
              link.urls != current.urls || link.token != current.token else {
            Task { await pair(link: url.absoluteString) }
            return
        }
        openedLink = link
    }

    private func sameComputer(_ link: Connection) -> Bool {
        guard let current = session.connection else { return false }
        return !Set(link.urls).isDisjoint(with: current.urls)
    }

    private var openedLinkTitle: String {
        guard let link = openedLink else { return "" }
        return sameComputer(link) ? "Use this new pairing link?" : "Pair with a different Digger?"
    }

    private func openedLinkMessage(_ link: Connection) -> String {
        let current = session.connection?.displayName ?? "your computer"
        if sameComputer(link) {
            return "It's for Digger on \(current), which this device is paired with. Use it if you made a new token "
                + "there with cosine-digger companion --rotate."
        }
        return "This device is paired with Digger on \(current). The link pairs it with \(link.displayName) instead, "
            + "and shows that computer's digs. Use links only from cosine-digger companion on your own computer."
    }

    private func pair(address: String, token: String) async {
        await attempt { try Pairing.connection(address: address, token: token) }
    }

    private func attempt(_ make: () throws -> Connection) async {
        let from = showing
        let wasWorking = from == .workspace
        if from != .workspace && from != .pairing { pairingFrom = from }
        defer { pairingFrom = nil }
        do {
            let connection = try make()
            pairingProblem = nil
            try await session.pair(connection)
            if wasWorking {
                session.notice = Notice("Paired with Digger on \(session.serverName ?? connection.displayName).")
            }
        } catch is CancellationError {
            // A newer link took over.
        } catch {
            let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
            if showing == .workspace {
                session.notice = Notice("That pairing link didn't work. \(message)", isError: true)
            } else {
                pairingProblem = message
            }
        }
    }
}

struct ConnectingView: View {
    @Environment(DigSession.self) private var session
    let name: String?
    @State private var slow = false
    @State private var confirmingUnpair = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Brand(size: 16)
                .frame(maxWidth: .infinity)
            LCDBusy(text: name.map { "connecting to Digger on \($0)" } ?? "connecting to Digger")
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .lcdScreen()
            if slow {
                Text("Still trying. Check that Digger is running on your computer with cosine-digger serve --companion, and that this device is on the same Wi-Fi or tailnet.")
                    .font(.subheadline)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Pair with a different Digger") { confirmingUnpair = true }
                    .buttonStyle(.key)
            }
        }
        .padding(16)
        .frame(maxWidth: 480)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .caseBackground()
        .task {
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            withAnimation { slow = true }
        }
        .forgetDialog(isPresented: $confirmingUnpair)
    }
}

/// Digger can't be reached, or no longer accepts this device. Both say what to do next.
struct ConnectionProblemView: View {
    enum Kind { case offline, unauthorized }

    @Environment(DigSession.self) private var session
    let kind: Kind
    let message: String
    /// Why the last pasted link didn't work.
    let pairingProblem: String?
    let pair: (String) async -> Void
    @State private var working = false
    @State private var confirmingUnpair = false

    var body: some View {
        StateMessage(title: kind == .offline ? "Can't reach Digger" : "Pair again",
                     systemImage: kind == .offline ? "wifi.exclamationmark" : "key.slash",
                     message: kind == .offline ? message : "\(message) On your computer, run cosine-digger companion --copy, then paste the new link here.") {
            VStack(spacing: 12) {
                if kind == .unauthorized {
                    PasteButton(payloadType: String.self) { strings in
                        guard let text = strings.first else { return }
                        Task { await run { await pair(text) } }
                    }
                    .labelStyle(.titleAndIcon)
                    .buttonBorderShape(.roundedRectangle(radius: 0))
                    .tint(Theme.pasteGreen)
                    .controlSize(.large)
                    .disabled(working)
                }
                // Why the last link didn't work (pasted here, or opened from another app).
                if let pairingProblem {
                    AlertBox(level: .error) {
                        Text(pairingProblem).fixedSize(horizontal: false, vertical: true)
                    }
                }
                tryAgain.buttonStyle(.key(kind == .offline ? .go : .normal))
                if kind == .offline {
                    // iOS asks once for Local Network access; turned off, Digger can't be reached at all.
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                    .buttonStyle(.key)
                }
                Button(kind == .offline ? "Pair with a different Digger" : "Unpair this device", role: .destructive) {
                    confirmingUnpair = true
                }
                .buttonStyle(.key(.danger))
            }
        }
        .caseBackground()
        .forgetDialog(isPresented: $confirmingUnpair)
    }

    private var tryAgain: some View {
        Button {
            Task { await run { await session.connect() } }
        } label: {
            if working { ProgressView().tint(Theme.lcd) } else { Text("Try again") }
        }
        .disabled(working)
        .accessibilityLabel("Try again")
    }

    private func run(_ work: () async -> Void) async {
        working = true
        await work()
        working = false
    }
}

extension View {
    /// "Forget this Digger?", before unpairing.
    func forgetDialog(isPresented: Binding<Bool>) -> some View {
        modifier(ForgetDialog(isPresented: isPresented))
    }
}

private struct ForgetDialog: ViewModifier {
    @Environment(DigSession.self) private var session
    @Binding var isPresented: Bool

    func body(content: Content) -> some View {
        content.confirmationDialog("Forget this Digger?", isPresented: $isPresented, titleVisibility: .visible) {
            Button("Forget and pair again", role: .destructive) { session.forget() }
        } message: {
            Text("Your digs, keeps and passes stay on your computer. You'll need a new pairing link from cosine-digger companion.")
        }
    }
}
