// swift-tools-version: 6.0
// Gus: a native macOS front end for ~/bin/wing-autogain.
// Build the app with ./build.sh (wraps the executable into /Applications/Gus.app).
import PackageDescription

let package = Package(
    name: "Gus",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Gus", targets: ["Gus"]),
    ],
    targets: [
        // Everything testable without a window: line parsing, show files, command lines, the process runner.
        .target(name: "GusCore"),
        .executableTarget(name: "Gus", dependencies: ["GusCore"]),
        .testTarget(name: "GusCoreTests", dependencies: ["GusCore"]),
    ]
)
