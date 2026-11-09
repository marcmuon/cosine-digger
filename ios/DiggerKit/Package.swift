// swift-tools-version:5.9
import PackageDescription

// DiggerKit is the app's platform-neutral core: the wire models of Digger's JSON API, the
// client that talks to it, and the dig and playback state the SwiftUI views render. It uses
// only Foundation and Observation, so `swift test` runs on macOS and on Linux.
let package = Package(
    name: "DiggerKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "DiggerKit", targets: ["DiggerKit"])],
    targets: [
        .target(name: "DiggerKit"),
        .testTarget(name: "DiggerKitTests", dependencies: ["DiggerKit"], resources: [.copy("Fixtures")]),
    ]
)
