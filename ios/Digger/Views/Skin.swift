import SwiftUI
import UIKit

// The web client's skin, drawn natively: beveled case metal, keys that press in, LED mode keys,
// title strips, a green LCD, and ERR/WARN alerts. Each piece follows a rule in app.css.

// MARK: Bevels and panels

/// One half of a 2-point bevel: the top and leading edges, or the bottom and trailing ones,
/// mitred at the corners like a CSS border.
struct BevelEdge: Shape {
    enum Side { case topLeading, bottomTrailing }
    let side: Side
    var width: CGFloat = 2

    func path(in rect: CGRect) -> Path {
        let w = min(width, rect.width / 2, rect.height / 2)
        var path = Path()
        switch side {
        case .topLeading:
            path.move(to: CGPoint(x: rect.minX, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.maxX - w, y: rect.minY + w))
            path.addLine(to: CGPoint(x: rect.minX + w, y: rect.minY + w))
            path.addLine(to: CGPoint(x: rect.minX + w, y: rect.maxY - w))
        case .bottomTrailing:
            path.move(to: CGPoint(x: rect.maxX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.minX + w, y: rect.maxY - w))
            path.addLine(to: CGPoint(x: rect.maxX - w, y: rect.maxY - w))
            path.addLine(to: CGPoint(x: rect.maxX - w, y: rect.minY + w))
        }
        path.closeSubpath()
        return path
    }
}

extension View {
    /// The skin's border: light on top and left, dark on the bottom and right (raised), or the
    /// other way round (sunken). It's drawn just inside the view's edge.
    func bevel(sunken: Bool = false, light: Color = Theme.caseHi, dark: Color = Theme.caseLo) -> some View {
        overlay {
            ZStack {
                BevelEdge(side: .topLeading).fill(sunken ? dark : light)
                BevelEdge(side: .bottomTrailing).fill(sunken ? light : dark)
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
    }

    /// A sunken panel, like the web client's step list, trail and inspector.
    func well(_ color: Color = Theme.panel) -> some View {
        background(color).bevel(sunken: true)
    }

    /// The skin's hard drop shadow (3 points down and right, no blur).
    func hardShadow() -> some View {
        background { Color.black.opacity(0.6).offset(x: 3, y: 3) }
    }

    /// Behind a whole screen: the player's case.
    func caseBackground() -> some View {
        background { CaseBackground() }
    }

    /// LCD text's faint green glow.
    func lcdGlow() -> some View {
        shadow(color: Theme.lcdGlow, radius: 4)
    }

    /// The green LCD: scanlines and a faint glow on near-black, set into the case.
    func lcdScreen() -> some View {
        background {
            ZStack {
                Theme.lcdBackground
                RadialGradient(colors: [Theme.lcd.opacity(0.08), .clear], center: UnitPoint(x: 0.3, y: 0),
                               startRadius: 0, endRadius: 320)
                Scanlines(color: Theme.lcd.opacity(0.035))
            }
        }
        .overlay {
            Rectangle()
                .strokeBorder(Color.black.opacity(0.85), lineWidth: 8)
                .blur(radius: 6)
                .clipped()
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
        .bevel(sunken: true)
    }
}

/// Horizontal lines every few points, for case metal and the LCD.
struct Scanlines: View {
    var color: Color
    var period: CGFloat = 3

    var body: some View {
        Canvas { context, size in
            var lines = Path()
            var y: CGFloat = 0
            while y < size.height {
                lines.addRect(CGRect(x: 0, y: y, width: size.width, height: 1))
                y += period
            }
            context.fill(lines, with: .color(color))
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// The brushed strip behind a panel's title.
struct CaseMetal: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.caseMid, Theme.case2], startPoint: .top, endPoint: .bottom)
            Scanlines(color: Color.white.opacity(0.05))
        }
    }
}

/// The player's case, behind every screen.
struct CaseBackground: View {
    var body: some View {
        ZStack(alignment: .top) {
            LinearGradient(colors: [Theme.caseMain, Theme.case2], startPoint: .top, endPoint: .bottom)
            LinearGradient(colors: [Color.white.opacity(0.05), .clear], startPoint: .top, endPoint: .bottom)
                .frame(height: 120)
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}

/// A panel's title strip: case metal, a Press Start title, and an LCD readout on the right.
struct PanelBar<Trailing: View>: View {
    let title: String
    var systemImage: String?
    var status: String?
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 10) {
            if let systemImage {
                Image(systemName: systemImage)
                    .font(.footnote.weight(.bold))
                    .foregroundStyle(Theme.amber)
                    .accessibilityHidden(true)
            }
            Text(title)
                .font(.pixel(12))
                .textCase(.uppercase)
                .foregroundStyle(Theme.white)
                .shadow(color: .black, radius: 0, x: 1, y: 1)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel(title)
                .accessibilityAddTraits(.isHeader)
            if let status {
                Text(status)
                    .font(.lcd(20))
                    .foregroundStyle(Theme.lcd)
                    .lineLimit(1)
            }
            trailing()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(minHeight: 36)
        .background { CaseMetal() }
        .overlay(alignment: .bottom) { Theme.caseLo.frame(height: 2) }
    }
}

extension PanelBar where Trailing == EmptyView {
    init(title: String, systemImage: String? = nil, status: String? = nil) {
        self.init(title: title, systemImage: systemImage, status: status, trailing: { EmptyView() })
    }
}

/// A raised panel with a title strip: a deck of the web client.
struct Deck<Content: View>: View {
    let title: String
    var status: String?
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PanelBar(title: title, status: status)
            VStack(alignment: .leading, spacing: 12) {
                content()
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.panel2)
        .bevel()
        .hardShadow()
        .accessibilityElement(children: .contain)
    }
}

/// A section's name inside a panel: Silkscreen, uppercase, like the web client's field labels.
struct SectionLabel: View {
    let title: String
    var color: Color = Theme.text

    var body: some View {
        Text(title)
            .font(.ui(14))
            .textCase(.uppercase)
            .foregroundStyle(color)
            .accessibilityLabel(title)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A 1-point rule between rows.
struct Rule: View {
    var dashed = false

    var body: some View {
        if dashed {
            Line()
                .stroke(Theme.line, style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
                .frame(height: 1)
                .accessibilityHidden(true)
        } else {
            Theme.line.frame(height: 1).accessibilityHidden(true)
        }
    }

    private struct Line: Shape {
        func path(in rect: CGRect) -> Path {
            var path = Path()
            path.move(to: CGPoint(x: rect.minX, y: rect.midY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
            return path
        }
    }
}

// MARK: Keys

/// What a key is for, which sets its colors.
enum KeyKind: Equatable {
    case normal
    /// The main action on a screen: Play step, Start dig, Pair.
    case go
    /// Keep, on a kept track.
    case kept
    /// Pass, on a passed track.
    case passed
    /// Out to YouTube.
    case youtube
    /// Forgetting a pairing.
    case danger
    /// A mode key, with its LED lit when on.
    case mode(on: Bool)
    /// Held in and lit: the playing track's play key.
    case active

    fileprivate struct Colors {
        var top: Color
        var bottom: Color
        var text: Color
        var light = Theme.caseHi
        var dark = Theme.caseLo
    }

    fileprivate func colors(enabled: Bool) -> Colors {
        guard enabled else {
            return Colors(top: Color(hex: 0x333845), bottom: Color(hex: 0x252A33), text: Color(hex: 0x8790A3))
        }
        switch self {
        case .normal:
            return Colors(top: Color(hex: 0x4D5466), bottom: Color(hex: 0x2C313C), text: Theme.text)
        case .go:
            return Colors(top: Color(hex: 0x5DFF8F), bottom: Color(hex: 0x25B857), text: Color(hex: 0x03170A),
                          light: Color(hex: 0xB5FFCC), dark: Color(hex: 0x0D4A22))
        case .kept:
            return Colors(top: Theme.lcd, bottom: Theme.lcd, text: Color(hex: 0x03170A),
                          light: Color(hex: 0xB5FFCC), dark: Color(hex: 0x0D4A22))
        case .passed:
            return Colors(top: Color(hex: 0x8A2330), bottom: Color(hex: 0x8A2330), text: .white,
                          light: Color(hex: 0xFF9AA5), dark: Color(hex: 0x3D0D12))
        case .youtube:
            return Colors(top: Color(hex: 0x7EE6FF), bottom: Color(hex: 0x2AB4DC), text: Color(hex: 0x04151C),
                          light: Color(hex: 0xC8F4FF), dark: Color(hex: 0x0B4A5D))
        case .danger:
            return Colors(top: Color(hex: 0x4D5466), bottom: Color(hex: 0x2C313C), text: Color(hex: 0xFF8080))
        case .mode(on: false):
            return Colors(top: Color(hex: 0x4A5162), bottom: Color(hex: 0x2B303B), text: Theme.text)
        case .mode(on: true), .active:
            return Colors(top: Color(hex: 0x22301F), bottom: Color(hex: 0x121A12), text: Theme.lcd)
        }
    }

    /// Mode keys that are on, and the active key, sit pressed in.
    fileprivate var heldIn: Bool {
        switch self {
        case .mode(on: true), .active: return true
        default: return false
        }
    }
}

/// How big a key is drawn. Every size answers taps over at least 44 points.
enum KeySize {
    /// 44 points: the faceplate and screen actions.
    case regular
    /// 36 points: keys on a row or beside a text.
    case small
    /// 30 points: keys in the title strip.
    case bar

    fileprivate var height: CGFloat {
        switch self {
        case .regular: return 44
        case .small: return 36
        case .bar: return 30
        }
    }

    fileprivate var fontSize: CGFloat {
        switch self {
        case .regular: return 14
        case .small: return 13
        case .bar: return 12
        }
    }

    fileprivate var padding: CGFloat {
        switch self {
        case .regular: return 14
        case .small: return 9
        case .bar: return 8
        }
    }
}

/// A key's face: case-metal gradient, bevel, Silkscreen caps, and the hard shadow under it. It
/// sinks a point while pressed, like the web client's `.btn:active`.
struct KeyFace<Label: View>: View {
    var kind: KeyKind = .normal
    var size: KeySize = .regular
    var pressed = false
    var enabled = true
    @ViewBuilder var label: () -> Label

    var body: some View {
        let colors = kind.colors(enabled: enabled)
        let sunken = pressed || kind.heldIn
        HStack(spacing: 6) {
            if case .mode(let on) = kind { LED(on: on && enabled) }
            label()
        }
        .font(.ui(size.fontSize))
        .textCase(.uppercase)
        .lineLimit(1)
        .foregroundStyle(colors.text)
        .shadow(color: kind.heldIn && enabled ? Theme.lcdGlow : .clear, radius: 3)
        .padding(.horizontal, size.padding)
        .frame(minWidth: size.height, minHeight: size.height)
        .background(LinearGradient(colors: [colors.top, colors.bottom], startPoint: .top, endPoint: .bottom))
        .bevel(sunken: sunken, light: colors.light, dark: colors.dark)
        .background { Color.black.offset(y: sunken || !enabled ? 0 : 2) }
        .offset(y: pressed ? 1 : 0)
        .padding(.vertical, (Theme.tap - size.height) / 2)
        .frame(minWidth: Theme.tap)
        .contentShape(Rectangle())
    }
}

/// A button drawn as one of the skin's keys.
struct KeyStyle: ButtonStyle {
    var kind: KeyKind = .normal
    var size: KeySize = .regular

    func makeBody(configuration: Configuration) -> some View {
        KeyButton(configuration: configuration, kind: kind, size: size)
    }
}

private struct KeyButton: View {
    let configuration: ButtonStyleConfiguration
    let kind: KeyKind
    let size: KeySize
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        KeyFace(kind: kind, size: size, pressed: configuration.isPressed, enabled: isEnabled) {
            configuration.label
        }
    }
}

extension ButtonStyle where Self == KeyStyle {
    /// A plain key.
    static var key: KeyStyle { KeyStyle() }

    static func key(_ kind: KeyKind, size: KeySize = .regular) -> KeyStyle {
        KeyStyle(kind: kind, size: size)
    }
}

/// A key's lamp: an 8-point square, lit green when on.
struct LED: View {
    var on: Bool
    var color: Color = Theme.lcd

    var body: some View {
        Rectangle()
            .fill(on ? color : Color(hex: 0x1A1F18))
            .frame(width: 8, height: 8)
            .overlay {
                if !on { Rectangle().strokeBorder(Color.black, lineWidth: 1) }
            }
            .shadow(color: on ? color.opacity(0.45) : .clear, radius: on ? 3 : 0)
            .accessibilityHidden(true)
    }
}

/// A link inside a panel: LCD green, underlined, as the web inspector's links.
struct PanelLink: View {
    let title: String
    let destination: URL

    var body: some View {
        Link(destination: destination) {
            Text(title)
                .font(.subheadline)
                .underline()
                .foregroundStyle(Theme.lcdSoft)
                .multilineTextAlignment(.leading)
                .tapTarget(alignment: .leading)
        }
    }
}

// MARK: Alerts and progress

/// A notice in the skin's alert box: a colored frame with a thick left edge and an "ERR",
/// "WARN" or "OK" tag.
struct AlertBox<Content: View>: View {
    enum Level {
        case error, warning, ok

        var color: Color {
            switch self {
            case .error: return Theme.red
            case .warning: return Theme.amber
            case .ok: return Theme.lcdSoft
            }
        }

        var text: Color {
            switch self {
            case .error: return Color(hex: 0xFFD9D9)
            case .warning: return Color(hex: 0xFFE9BF)
            case .ok: return Theme.text
            }
        }

        var tag: String {
            switch self {
            case .error: return "ERR"
            case .warning: return "WARN"
            case .ok: return "OK"
            }
        }
    }

    let level: Level
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(verbatim: level.tag)
                .font(.ui(13))
                .foregroundStyle(level.color)
                .accessibilityHidden(true)
            content()
        }
        .font(.footnote)
        .foregroundStyle(level.text)
        .padding(.leading, 16)
        .padding(.trailing, 6)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(level.color.opacity(level == .warning ? 0.08 : 0.1))
        .background(Theme.panel2)
        .overlay { Rectangle().strokeBorder(level.color, lineWidth: 2) }
        .overlay(alignment: .leading) { level.color.frame(width: 6) }
    }
}

/// What's under way, as the web client's log prints it ("> reading your digs▌"), with the
/// cursor blinking unless Reduce Motion is on.
struct LCDBusy: View {
    let text: String
    var size: CGFloat = 22
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var cursor = true

    var body: some View {
        (Text(verbatim: "> ").foregroundStyle(Theme.lcdDim)
            + Text(text)
            + Text(verbatim: "\u{258C}").foregroundStyle(cursor || reduceMotion ? Theme.lcd : Color.clear))
            .font(.lcd(size))
            .foregroundStyle(Theme.lcd)
            .lcdGlow()
            .task {
                guard !reduceMotion else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 550_000_000)
                    cursor.toggle()
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(text)
    }
}

/// A whole screen waiting on something: the LCD with what's under way.
struct BusyScreen: View {
    let text: String

    var body: some View {
        LCDBusy(text: text)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: 480, alignment: .leading)
            .lcdScreen()
            .padding(16)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: Title strip

/// The four-bar spectrum mark beside the name.
struct BrandMark: View {
    var body: some View {
        Canvas { context, size in
            context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Theme.lcdBackground))
            let unit = size.width / 16
            let bars: [(x: CGFloat, y: CGFloat, height: CGFloat, color: Color)] = [
                (1, 9, 6, Theme.lcd), (5, 5, 10, Theme.lcd), (9, 2, 13, Theme.amber), (13, 7, 8, Theme.lcd),
            ]
            for bar in bars {
                context.fill(Path(CGRect(x: bar.x * unit, y: bar.y * unit, width: 3 * unit, height: bar.height * unit)),
                             with: .color(bar.color))
            }
        }
        .frame(width: 16, height: 16)
        .padding(1)
        .background(Theme.caseLo)
        .accessibilityHidden(true)
    }
}

/// "COSINE · DIGGER", as the web client's title strip shows it.
struct Brand: View {
    var size: CGFloat = 12

    var body: some View {
        // Where the bar is crowded (a phone with the trail and back keys), just the mark.
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                BrandMark()
                (Text(verbatim: "COSINE") + Text(verbatim: " \u{00B7} ").foregroundStyle(Theme.amber) + Text(verbatim: "DIGGER"))
                    .font(.pixel(size))
                    .foregroundStyle(Theme.white)
                    .shadow(color: .black, radius: 0, x: 2, y: 2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
            BrandMark()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Cosine Digger")
        .accessibilityAddTraits(.isHeader)
    }
}

/// A screen's name in the title strip, in the pixel face.
struct TitleStripText: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.pixel(12))
            .textCase(.uppercase)
            .foregroundStyle(Theme.white)
            .shadow(color: .black, radius: 0, x: 1, y: 1)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .accessibilityLabel(title)
            .accessibilityAddTraits(.isHeader)
    }
}

extension View {
    /// The screen's name in the title strip. The plain title stays underneath, for VoiceOver,
    /// the back button and UI tests.
    func skinTitle(_ title: String) -> some View {
        navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { TitleStripText(title: title) }
            }
    }

    /// The title strip's name, or none when a screen around this one names it.
    func skinTitle(_ title: String?) -> some View {
        modifier(OptionalTitle(title: title))
    }

    /// The brand in the title strip, for the screen the app opens on.
    func brandTitle(_ title: String) -> some View {
        navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { Brand() }
            }
    }

    /// A text field set into the LCD, like the web client's seed box.
    func lcdField() -> some View {
        textFieldStyle(.plain)
            .font(.lcd(22))
            .foregroundStyle(Theme.lcd)
            .tint(Theme.lcd)
            .padding(.horizontal, 10)
            .frame(minHeight: Theme.tap)
            .background(Theme.lcdBackground)
            .bevel(sunken: true)
    }

    /// A plain text field, like the web client's setup inputs.
    func wellField() -> some View {
        textFieldStyle(.plain)
            .font(.subheadline)
            .foregroundStyle(Theme.text)
            .tint(Theme.lcd)
            .padding(.horizontal, 10)
            .frame(minHeight: Theme.tap)
            .background(Theme.well)
            .bevel(sunken: true)
    }
}

private struct OptionalTitle: ViewModifier {
    let title: String?

    func body(content: Content) -> some View {
        if let title {
            content.skinTitle(title)
        } else {
            content
        }
    }
}

/// The title strip's case color and faces, for every navigation bar. SwiftUI can't set a bar's
/// fonts, so this goes through UIKit's appearance once, at launch.
@MainActor
enum SkinChrome {
    static func install() {
        let bar = UINavigationBarAppearance()
        bar.configureWithOpaqueBackground()
        bar.backgroundColor = UIColor(Theme.caseMain)
        bar.shadowColor = UIColor(Theme.caseLo)
        var title: [NSAttributedString.Key: Any] = [.foregroundColor: UIColor(Theme.white)]
        if let pixel = UIFont(name: "PressStart2P-Regular", size: 12) {
            title[.font] = UIFontMetrics(forTextStyle: .headline).scaledFont(for: pixel)
        }
        bar.titleTextAttributes = title
        let button = UIBarButtonItemAppearance(style: .plain)
        var buttonText: [NSAttributedString.Key: Any] = [.foregroundColor: UIColor(Theme.lcdSoft)]
        if let face = UIFont(name: "Silkscreen-Bold", size: 14) {
            buttonText[.font] = UIFontMetrics(forTextStyle: .body).scaledFont(for: face)
        }
        button.normal.titleTextAttributes = buttonText
        bar.buttonAppearance = button
        bar.backButtonAppearance = button

        let appearance = UINavigationBar.appearance()
        appearance.standardAppearance = bar
        appearance.compactAppearance = bar
        appearance.scrollEdgeAppearance = bar
        appearance.compactScrollEdgeAppearance = bar
        appearance.tintColor = UIColor(Theme.lcdSoft)
    }
}
