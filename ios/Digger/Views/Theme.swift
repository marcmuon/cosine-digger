import DiggerKit
import SwiftUI

/// Darwin's MacTypes.h also declares a QuickDraw `Point`, which Foundation brings in; without
/// this, `Point` is ambiguous in every file of the app. Here it is always DiggerKit's.
typealias Point = DiggerKit.Point

/// The web client's skin (src/cosine_digger/web/static/app.css): a dark media-player case, a
/// green LCD, and signal colors. Every color here is one of its tokens, so a pick reads the same
/// on every screen: green for playing, magenta for kept, yellow for selected, grey for passed.
enum Theme {
    // The case.
    static let void = Color(hex: 0x06070B)
    static let caseHi = Color(hex: 0x7C8497)
    static let caseMid = Color(hex: 0x3A404D)
    static let caseMain = Color(hex: 0x2A2F3A)
    static let case2 = Color(hex: 0x1E222B)
    static let caseLo = Color(hex: 0x0A0B0F)

    // Its insides.
    static let panel = Color(hex: 0x10131A)
    static let panel2 = Color(hex: 0x161A23)
    static let panel3 = Color(hex: 0x1C2130)
    static let line = Color(hex: 0x2B3242)
    static let well = Color(hex: 0x07090D)

    // The LCD.
    static let lcdBackground = Color(hex: 0x020D06)
    static let lcd = Color(hex: 0x3DFF7A)
    static let lcdSoft = Color(hex: 0x2FD865)
    static let lcdDim = Color(hex: 0x1C5A33)
    static let lcdGlow = Color(hex: 0x3DFF7A, opacity: 0.45)

    // Signal colors.
    static let amber = Color(hex: 0xFFB627)
    static let cyan = Color(hex: 0x3FD8FF)
    static let magenta = Color(hex: 0xFF5BD8)
    static let red = Color(hex: 0xFF5A5A)
    static let white = Color(hex: 0xF4F6FB)
    static let text = Color(hex: 0xE6E9F1)
    static let muted = Color(hex: 0xA7AFC0)
    static let focus = Color(hex: 0xFFE14D)

    // The marks, as the web client names them.
    static let playing = lcd
    static let kept = magenta
    static let selected = focus
    static let passed = muted
    static let label = amber
    static let nearness = cyan

    /// The system's Paste key: the skin's green, dark enough for the white label iOS gives it.
    static let pasteGreen = Color(hex: 0x187A42)

    /// The smallest touch target for any primary action.
    static let tap: CGFloat = 44
}

extension Color {
    /// A color from the web skin's hex value.
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255, opacity: opacity)
    }
}

/// The web client's three faces, bundled in Resources/Fonts. Each grows with Dynamic Type
/// along with the text style it stands in for.
extension Font {
    /// Press Start 2P: the title strip and panel titles. Uppercase only.
    static func pixel(_ size: CGFloat, relativeTo style: Font.TextStyle = .headline) -> Font {
        .custom("PressStart2P-Regular", size: size, relativeTo: style)
    }

    /// VT323: the LCD, ranks, step numbers and counters.
    static func lcd(_ size: CGFloat, relativeTo style: Font.TextStyle = .body) -> Font {
        .custom("VT323-Regular", size: size, relativeTo: style)
    }

    /// Silkscreen: keys, labels and section names (bold on keys).
    static func ui(_ size: CGFloat, bold: Bool = true, relativeTo style: Font.TextStyle = .subheadline) -> Font {
        .custom(bold ? "Silkscreen-Bold" : "Silkscreen-Regular", size: size, relativeTo: style)
    }
}

extension View {
    /// At least 44 by 44 points that answer a tap. Put it on a button's label: a frame outside
    /// the button makes it bigger without making more of it tappable.
    func tapTarget(alignment: Alignment = .center) -> some View {
        frame(minWidth: Theme.tap, minHeight: Theme.tap, alignment: alignment)
            .contentShape(Rectangle())
    }
}

extension DigSession {
    /// The words VoiceOver reads for a pick, with every mark it carries.
    func spokenMarks(for pick: Pick, selected: Bool) -> String {
        var marks: [String] = []
        if playback.isCurrent(pick.id) { marks.append(playback.markWord) }
        switch verdict(for: pick.trackKey) {
        case .keep?: marks.append("kept")
        case .pass?: marks.append("passed")
        case nil: break
        }
        if isHeard(pick.trackKey) { marks.append("heard") }
        if pick.videoID == nil { marks.append("no video") }
        if selected { marks.append("selected") }
        return marks.joined(separator: ", ")
    }
}

extension Playback {
    /// How the track in the player is marked on its row: "playing", "paused" or "in the player".
    var markWord: String {
        switch status {
        case .playing, .loading: return "playing"
        case .paused: return "paused"
        default: return "in the player"
        }
    }
}

extension Step {
    /// "Step 2", for titles and buttons.
    var title: String { "Step \(number)" }
}

/// A full-width notice for a whole screen: nothing loaded yet, nothing found, or a problem. It's
/// one of the skin's panels: a title strip, then what happened and the ways on.
struct StateMessage<Actions: View>: View {
    let title: String
    let systemImage: String
    let message: String
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        VStack(spacing: 0) {
            PanelBar(title: title, systemImage: systemImage)
            VStack(alignment: .leading, spacing: 14) {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(Theme.text)
                    .fixedSize(horizontal: false, vertical: true)
                VStack(alignment: .leading, spacing: 10) {
                    actions()
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.panel2)
        .bevel()
        .hardShadow()
        .frame(maxWidth: 480)
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .contain)
    }
}

extension StateMessage where Actions == EmptyView {
    init(title: String, systemImage: String, message: String) {
        self.init(title: title, systemImage: systemImage, message: message, actions: { EmptyView() })
    }
}
