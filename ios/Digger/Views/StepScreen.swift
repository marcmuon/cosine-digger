import DiggerKit
import SwiftUI

/// One step: where it came from, then its picks in Digger's order. Each step keeps its own
/// scroll position and selection, so going back to it puts you where you were.
struct StepScreen: View {
    @Environment(DigSession.self) private var session
    let stepID: String
    /// Set on a phone: tapping a pick also opens its details. On iPad the details sit alongside.
    let openDetail: ((Pick) -> Void)?
    @State private var restored = false
    /// The pick at the top of the list. It goes to the session only once the list is back where
    /// you left it: a list that has just appeared reports its own top first, and that would
    /// overwrite the place it's about to go back to.
    @State private var topPick: String?

    var body: some View {
        let picks = session.picks(in: stepID)
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    StepHeader(stepID: stepID)
                        .padding(.horizontal, 10)
                        .padding(.top, 10)
                        .padding(.bottom, 8)
                    StepProblem(stepID: stepID)
                    Rule()
                    if picks.isEmpty {
                        StepPlaceholder(stepID: stepID)
                            .padding(.top, 8)
                    } else {
                        ForEach(picks) { pick in
                            VStack(alignment: .leading, spacing: 0) {
                                if let label = batchLabel(before: pick) {
                                    BatchDivider(label: label)
                                }
                                PickRow(pick: pick, openDetail: openDetail)
                                Rule()
                            }
                            .id(pick.id)
                        }
                        if session.step(stepID)?.isRunning == true {
                            LCDBusy(text: "finding more picks", size: 20)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .lcdScreen()
                                .padding(8)
                        }
                    }
                }
                // On the stack itself, so each pick is a target and the position can be kept:
                // a padding between them makes the padded stack the only target.
                .scrollTargetLayout()
                .padding(4)
            }
            .scrollPosition(id: $topPick, anchor: .top)
            .onChange(of: topPick) { _, top in
                if restored, let top { session.scrollAnchorByStep[stepID] = top }
            }
            .onAppear { restore(proxy, picks: picks) }
            .onChange(of: picks.count) { restore(proxy, picks: picks) }
            .onChange(of: session.reveal) { show(session.reveal, proxy, picks: session.picks(in: stepID)) }
        }
        .refreshable { await session.resume(stepID) }
        .well()
        .padding(6)
        .caseBackground()
        .brandTitle(session.step(stepID)?.title ?? "Step")
    }

    /// Once the picks are here: the pick a move started from (going back), else where you
    /// left this step, else its selection.
    private func restore(_ proxy: ScrollViewProxy, picks: [Pick]) {
        guard !restored, !picks.isEmpty else { return }
        let left = session.scrollAnchorByStep[stepID]
        defer { restored = true }
        if show(session.reveal, proxy, picks: picks) { return }
        if let left, picks.contains(where: { $0.id == left }) {
            // At the first pick, stay at the top so the step's header shows. A list that has
            // just appeared hasn't laid out its picks yet and drops a scroll asked for now, so
            // it's asked again once it has.
            if left != picks.first?.id {
                topPick = left
                DispatchQueue.main.async { proxy.scrollTo(left, anchor: .top) }
            }
        } else if let selected = session.selectionByStep[stepID], picks.contains(where: { $0.id == selected }) {
            proxy.scrollTo(selected, anchor: .center)
        }
    }

    @discardableResult
    private func show(_ reveal: Reveal?, _ proxy: ScrollViewProxy, picks: [Pick]) -> Bool {
        guard let reveal, reveal.stepID == stepID, picks.contains(where: { $0.id == reveal.pickID }) else { return false }
        withAnimation { proxy.scrollTo(reveal.pickID, anchor: .center) }
        session.revealed(reveal)
        return true
    }

    /// "One step further: …" and other batch headings, before the batch's first pick.
    private func batchLabel(before pick: Pick) -> String? {
        let batches = session.envelopes[stepID]?.snapshot.batches ?? []
        guard batches.count > 1 else { return nil }
        return batches.first { $0.startRank == pick.rank }?.label
    }
}

/// A batch heading inside a step ("One step further: …"), between dotted rules.
struct BatchDivider: View {
    let label: String

    var body: some View {
        VStack(spacing: 0) {
            Rule(dashed: true)
            Text(label)
                .font(.ui(11, bold: false))
                .textCase(.uppercase)
                .foregroundStyle(Theme.muted)
                .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
                .padding(.horizontal, 6)
                .accessibilityLabel(label)
                .accessibilityAddTraits(.isHeader)
            Rule(dashed: true)
        }
        .padding(.top, 10)
    }
}

/// Which step this is (its number in amber, its sentence in Silkscreen), how it was made, the
/// way back, and Play step.
struct StepHeader: View {
    @Environment(DigSession.self) private var session
    let stepID: String

    var body: some View {
        if let step = session.step(stepID) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(step.number)")
                        .font(.lcd(26))
                        .foregroundStyle(Theme.amber)
                        .accessibilityHidden(true)
                    Text(step.header)
                        .font(.ui(14, bold: false))
                        .textCase(.uppercase)
                        .foregroundStyle(Theme.white)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel(step.header)
                        .accessibilityAddTraits(.isHeader)
                }
                let counts = session.counts(for: stepID)
                Text(summary(picks: session.picks(in: stepID).count, kept: counts.kept, passed: counts.passed))
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
                if let parentID = step.parentID, let parent = session.step(parentID) {
                    let back = "Back to \(parent.title): \(source(of: step, in: parentID) ?? parent.header)"
                    Button {
                        session.openParent()
                    } label: {
                        (Text(verbatim: "\u{25C0}\u{FE0E} ") + Text(back).underline())
                            .font(.footnote)
                            .foregroundStyle(Theme.lcdSoft)
                            .multilineTextAlignment(.leading)
                            .tapTarget(alignment: .leading)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(back)
                    .accessibilityHint("Shows the step this one was made from, with that pick selected.")
                }
                Button {
                    session.playStep(stepID)
                } label: {
                    Text("\u{25B6}\u{FE0E} Play step")
                }
                .buttonStyle(.key)
                .disabled(session.picks(in: stepID).allSatisfy { $0.videoID == nil })
                .accessibilityLabel("Play step")
                .accessibilityHint("Plays this step's picks from the top, skipping the ones you passed.")
                ForEach(Array(step.warnings.enumerated()), id: \.offset) { _, note in
                    if let message = note.message {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(Theme.amber)
                            .padding(6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Theme.panel2)
                    }
                }
            }
        }
    }

    private func source(of step: Step, in parentID: String) -> String? {
        let picks = session.picks(in: parentID)
        guard let id = Moves.sourcePickID(selectionID: step.context.sourceSelectionID, parentStepID: parentID, picks: picks)
        else { return nil }
        return picks.first { $0.id == id }?.title
    }

    private func summary(picks: Int, kept: Int, passed: Int) -> String {
        var parts = [picks == 1 ? "1 pick" : "\(picks) picks"]
        if kept > 0 { parts.append("\(kept) kept") }
        if passed > 0 { parts.append("\(passed) passed") }
        return parts.joined(separator: " · ")
    }
}

/// A step that has picks but stopped part way, or that couldn't be read again: what happened and
/// the way on. An empty step says so in its placeholder instead.
struct StepProblem: View {
    @Environment(DigSession.self) private var session
    let stepID: String
    @State private var retrying = false

    var body: some View {
        let hasPicks = !session.picks(in: stepID).isEmpty
        if hasPicks, let step = session.step(stepID), step.isFailed {
            banner(icon: "exclamationmark.triangle.fill",
                   message: "This step stopped before it finished. \(step.error?.message ?? "") Its picks so far stay; running it again makes a new step beside it.") {
                Button {
                    Task { await session.rerun(step) }
                } label: {
                    if session.isRerunning { ProgressView().tint(Theme.lcd) } else { Text("Run again") }
                }
                .buttonStyle(.key(.normal, size: .small))
                .disabled(session.submitting != nil)
            }
        } else if hasPicks, let error = session.stepErrors[stepID] {
            banner(icon: "arrow.clockwise", message: "Couldn't read this step again. \(error)") {
                Button {
                    Task {
                        retrying = true
                        await session.resume(stepID)
                        retrying = false
                    }
                } label: {
                    if retrying { ProgressView().tint(Theme.lcd) } else { Text("Try again") }
                }
                .buttonStyle(.key(.normal, size: .small))
                .disabled(retrying)
            }
        }
    }

    private func banner<Action: View>(icon: String, message: String, @ViewBuilder action: () -> Action) -> some View {
        // AlertBox keeps its content closure, so it can't capture the non-escaping `action`.
        let action = action()
        return AlertBox(level: .warning) {
            Text(message)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            action
        }
        .padding(.horizontal, 6)
        .padding(.bottom, 8)
        .accessibilityElement(children: .contain)
    }
}

/// A step with no picks on screen: loading, still running, failed, or empty.
struct StepPlaceholder: View {
    @Environment(DigSession.self) private var session
    let stepID: String
    @State private var retrying = false

    var body: some View {
        let step = session.step(stepID)
        if let error = session.stepErrors[stepID], session.envelopes[stepID] == nil {
            StateMessage(title: "Couldn't load this step", systemImage: "exclamationmark.triangle", message: error) {
                retryButton("Try again") { await session.resume(stepID) }
            }
        } else if session.envelopes[stepID] == nil {
            BusyScreen(text: "loading step")
        } else if let step, step.isRunning {
            BusyScreen(text: "Digger is finding picks. They appear here as they arrive.")
        } else if let step, step.isFailed {
            StateMessage(title: "This step didn't finish", systemImage: "exclamationmark.triangle",
                         message: step.error?.message ?? "Digger stopped before it found any picks.") {
                retryButton("Run it again") { await session.retry(step) }
            }
        } else {
            StateMessage(title: "No picks", systemImage: "tray",
                         message: step?.parentID == nil
                            ? "cosine.club found nothing close to these seeds. Try other seeds."
                            : "This move found nothing. Go back and try another move from that pick.") {
                if step?.parentID != nil {
                    Button("Back") { session.openParent() }
                        .buttonStyle(.key(.go))
                } else {
                    NavigationLink(value: Screen.newDig) {
                        Text("Choose other seeds")
                    }
                    .buttonStyle(.key(.go))
                }
            }
        }
    }

    private func retryButton(_ title: String, _ work: @escaping () async -> Void) -> some View {
        Button {
            Task {
                retrying = true
                await work()
                retrying = false
            }
        } label: {
            if retrying { ProgressView().tint(Theme.lcd) } else { Text(title) }
        }
        .buttonStyle(.key(.go))
        .disabled(retrying)
    }
}
