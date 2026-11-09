import DiggerKit
import SwiftUI

/// The app's screens with the demo Digger's recorded answers: Seed A's first step, and the
/// step made from the label Label A.
private struct DemoHost<Content: View>: View {
    @State private var session: DigSession
    @State private var player = YouTubePlayer()
    let content: () -> Content

    init(mode: DemoServer.Mode = .normal, @ViewBuilder content: @escaping () -> Content) {
        _session = State(initialValue: DemoServer.session(mode: mode))
        self.content = content
    }

    var body: some View {
        content()
            .environment(session)
            .environment(player)
            .preferredColorScheme(.dark)
            .tint(Theme.lcdSoft)
            .task {
                session.playback.engine = player
                player.onSignal = { [weak session] signal in session?.playback.handle(signal) }
                await session.connect()
            }
    }
}

#Preview("Dig") {
    DemoHost { RootView() }
}

#Preview("Step list") {
    DemoHost {
        NavigationStack { CurrentStep(openDetail: { _ in }) }
    }
}

#Preview("No digs yet") {
    DemoHost(mode: .empty) { RootView() }
}

#Preview("Can't reach Digger") {
    DemoHost(mode: .offline) { RootView() }
}

#Preview("Pairing") {
    PairingView(problem: .constant(nil), connecting: false, pair: { _ in }, pairByHand: { _, _ in })
}
