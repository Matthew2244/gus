import SwiftUI
import AppKit
import GusCore

@main
enum Entry {
    static func main() {
        let args = CommandLine.arguments
        if args.contains("--self-test") {
            exit(SelfTest.run(verbose: !args.contains("--brief")))
        }
        if let i = args.firstIndex(of: "--render-icon"), i + 1 < args.count {
            exit(MainActor.assumeIsolated { IconRenderer.render(to: args[i + 1]) })
        }
        GusApp.main()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct GusApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model = GusModel()

    var body: some Scene {
        Window("Gus", id: "main") {
            ContentView(model: model)
                .frame(minWidth: 860, minHeight: 640)
        }
        .defaultSize(width: 980, height: 760)
        .commands {
            // Command-Z is "Undo last run" here: the one undo that matters in Gus.
            CommandGroup(replacing: .undoRedo) {
                Button("Undo Last Run…") { model.askToUndo() }
                    .keyboardShortcut("z", modifiers: .command)
                    .disabled(model.isRunning)
            }
            CommandMenu("Gains") {
                Button(model.dryRun ? "Listen and Tell Me" : "Listen and Set Gains") { model.listenAndSet() }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(!model.canRun)
                Button("Stop") { model.stop() }
                    .keyboardShortcut(".", modifiers: .command)
                    .disabled(!model.isRunning)
                Divider()
                Button("What Are the Gains Now?") { model.status() }
                    .keyboardShortcut("g", modifiers: .command)
                    .disabled(model.isRunning)
                Button("Find My WING") { model.discover() }
                    .disabled(model.isRunning)
                Divider()
                Button("Copy Results") { model.copyResults() }
                    .keyboardShortcut("c", modifiers: [.command, .shift])
                    .disabled(model.lines.isEmpty)
            }
            CommandGroup(after: .toolbar) {
                Button("Make Text Bigger") { model.textScale = min(1.6, model.textScale + 0.1) }
                    .keyboardShortcut("+", modifiers: .command)
                Button("Make Text Smaller") { model.textScale = max(0.85, model.textScale - 0.1) }
                    .keyboardShortcut("-", modifiers: .command)
                Button("Actual Size") { model.textScale = 1.0 }
                    .keyboardShortcut("0", modifiers: .command)
                Divider()
                Button("Show the Welcome Again") { model.welcomed = false }
            }
        }
    }
}

@MainActor
enum IconRenderer {
    static func render(to path: String) -> Int32 {
        let view = GusMark(size: 824).padding(100).frame(width: 1024, height: 1024)
        let r = ImageRenderer(content: view)
        r.scale = 1
        guard let cg = r.cgImage else { return 1 }
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
        do { try png.write(to: URL(fileURLWithPath: path)); return 0 } catch { return 1 }
    }
}
