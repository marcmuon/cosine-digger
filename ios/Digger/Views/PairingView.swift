import DiggerKit
import SwiftUI

/// Pair with the Digger running on your computer: paste (or open) the link that
/// `cosine-digger companion` prints, or type its address and token.
struct PairingView: View {
    @Binding var problem: String?
    /// A pairing is under way, from here or from a link opened in another app.
    let connecting: Bool
    let pair: (String) async -> Void
    let pairByHand: (String, String) async -> Void

    @State private var address = ""
    @State private var token = ""
    @State private var typing = false
    @State private var working = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Brand(size: 16)
                        .padding(.vertical, 6)
                        .frame(maxWidth: .infinity)

                    Deck(title: "On your computer") {
                        Text("Dig from your iPhone or iPad with the Digger running on your computer.")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.white)
                        Text("On your computer, start Digger for this device, then copy its pairing link:")
                            .font(.subheadline)
                            .foregroundStyle(Theme.text)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: "> cosine-digger serve --companion")
                            Text(verbatim: "> cosine-digger companion --copy")
                        }
                        .font(.lcd(20))
                        .foregroundStyle(Theme.lcd)
                        .lcdGlow()
                        .textSelection(.enabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lcdScreen()
                        Text("Then press Paste here. Or AirDrop the link to this device and open it.")
                            .font(.subheadline)
                            .foregroundStyle(Theme.muted)
                        // The system's paste key reads the clipboard without asking. iOS draws it
                        // (and refuses taps on one it can't read), so it takes a green dark enough
                        // for its white label, and no bevel over it.
                        PasteButton(payloadType: String.self) { strings in
                            guard let text = strings.first else { return }
                            Task { await run { await pair(text) } }
                        }
                        .labelStyle(.titleAndIcon)
                        .buttonBorderShape(.roundedRectangle(radius: 0))
                        .tint(Theme.pasteGreen)
                        .controlSize(.large)
                        .frame(maxWidth: .infinity)
                        .disabled(busy)
                        Text("The phone and the computer need to be on the same Wi-Fi, or the same Tailscale tailnet.")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }

                    Deck(title: "Or type it") {
                        DisclosureGroup(isExpanded: $typing) {
                            VStack(alignment: .leading, spacing: 10) {
                                SectionLabel(title: "Address")
                                TextField("Address, such as 192.168.1.20:8765", text: $address)
                                    .keyboardType(.URL)
                                    .textContentType(.URL)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .wellField()
                                SectionLabel(title: "Token")
                                SecureField("Token", text: $token)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .wellField()
                                Button("Pair") {
                                    Task { await run { await pairByHand(address, token) } }
                                }
                                .buttonStyle(.key(.go))
                                .disabled(busy || address.isEmpty || token.isEmpty)
                            }
                            .padding(.top, 10)
                        } label: {
                            Text("Type the address and token")
                                .font(.subheadline)
                                .foregroundStyle(Theme.lcdSoft)
                                .frame(minHeight: Theme.tap, alignment: .leading)
                        }
                        .tint(Theme.lcdSoft)
                    }

                    if busy {
                        LCDBusy(text: "connecting", size: 20)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .lcdScreen()
                    } else if let problem {
                        AlertBox(level: .error) {
                            Text(problem).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(12)
                .frame(maxWidth: 560)
                .frame(maxWidth: .infinity)
            }
            .caseBackground()
            .skinTitle("Pair with Digger")
        }
    }

    private var busy: Bool { working || connecting }

    private func run(_ work: () async -> Void) async {
        working = true
        await work()
        working = false
    }
}
