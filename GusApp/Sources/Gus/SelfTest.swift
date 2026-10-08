import Foundation
import GusCore

/// `Gus --self-test`: the app's real CLI path end to end, headless, on the pretend WING.
/// Same runner, same command builder, same parser the window uses. Prints every parsed line
/// with its kind and spoken form, and exits non-zero if anything didn't come back as expected.
enum SelfTest {
    static func run(verbose: Bool) -> Int32 {
        var failures: [String] = []
        func check(_ ok: Bool, _ what: String) {
            print(ok ? "  ok   \(what)" : "  FAIL \(what)")
            if !ok { failures.append(what) }
        }

        let env = GusCommand.environment()
        print("Gus self-test")
        print("  CLI:  \(GusCommand.executable.path)")
        print("  PATH: \(env["PATH"] ?? "")")
        check(FileManager.default.isExecutableFile(atPath: GusCommand.executable.path), "wing-autogain is there and runnable")

        let lib = ShowLibrary()
        let shows = lib.showNames()
        check(!shows.isEmpty, "found \(shows.count) show files")
        let show = shows.contains("The Woodshed") ? "The Woodshed" : (shows.first ?? "")
        let contents = lib.contents(of: show)
        print("  \(show): sections \(contents.sections.map(\.title).joined(separator: ", "))")
        print("  \(show): \(contents.channels.count) channels")
        check(!contents.sections.contains { ["FX", "PLAYBACK", "TRACKS"].contains($0.name.uppercased()) }, "FX, PLAYBACK and TRACKS left out of sections")

        let vocals = contents.sections.first { $0.name.uppercased() == "VOCALS" } ?? contents.sections.first
        let drums = contents.sections.first { $0.name.uppercased() == "DRUMS" } ?? contents.sections.first
        let channel = contents.channels.first

        func step(_ title: String, _ args: [String]) -> (lines: [ResultLine], ending: CLIRunner.Ending) {
            print("\n== \(title)\n   wing-autogain \(args.map { $0.contains(" ") ? "\"\($0)\"" : $0 }.joined(separator: " "))")
            let done = DispatchSemaphore(value: 0)
            let box = Box()
            CLIRunner().start(arguments: args, onLine: { text, stream in
                if let l = LineParser.parse(text, fromStderr: stream == .stderr) { box.add(l) }
            }, onEnd: { e in box.ending = e; done.signal() })
            if done.wait(timeout: .now() + 120) == .timedOut { box.ending = .couldNotStart("timed out") }
            let lines = box.lines
            if verbose {
                for l in lines {
                    print("   [\(l.kind.rawValue)] \(l.text)")
                    if l.spoken != l.text { print("        spoken: \(l.spoken)") }
                }
            }
            print("   ending: \(box.ending.map { "\($0)" } ?? "none")")
            return (lines, box.ending ?? .couldNotStart("no ending"))
        }

        var base = RunOptions(show: show, practice: true, listenSeconds: 1)

        if let vocals {
            base.target = .section(vocals.argument)
            base.dryRun = true
            let r = step("Dry run, section \(vocals.title)", GusCommand.run(base))
            check(r.lines.contains { $0.kind == .progress }, "progress line arrived")
            check(r.lines.last?.kind == .summary && r.lines.last!.text.hasPrefix("Dry run:"), "dry run ends with a summary")
            base.dryRun = false
        }
        if let drums {
            base.target = .section(drums.argument)
            let args = GusCommand.run(base) + ["--sim-reset", "--sim-signal", "A1=clip,A2=silent"]
            let r = step("Real run, section \(drums.title), with a clipping and a silent input", args)
            let warnings = r.lines.filter { $0.kind == .warning }
            check(warnings.count >= 2, "warnings classified (\(warnings.count))")
            check(warnings.allSatisfy { $0.spoken.hasPrefix("Warning. ") && !$0.spoken.contains("WARNING") }, "warnings spoken with Warning first")
            check(r.lines.contains { $0.kind == .raised }, "raised lines classified")
            check(r.lines.last?.kind == .summary, "ends with a summary")
            check(r.lines.last?.text.contains("Undo last run puts them back") == true, "summary points at the Undo button")
        }
        if let channel {
            base.target = .channel(channel.argument)
            base.oneAtATime = true
            let r = step("One at a time, channel \(channel.title)", GusCommand.run(base))
            check(r.lines.first?.text.hasPrefix("Next: ") == true, "one-at-a-time names the input first")
            base.oneAtATime = false
            let s = step("What are the gains now?", GusCommand.status(base))
            check(s.lines.contains { $0.text.contains("gain") }, "status read back a gain")
        }
        let u = step("Undo last run", GusCommand.undo(base))
        check(u.lines.contains { $0.kind == .summary || $0.kind == .notice }, "undo reported")

        base.target = .section("NO SUCH SECTION")
        let bad = step("A section that doesn't exist (error path)", GusCommand.run(base))
        check(bad.lines.contains { $0.kind == .problem }, "CLI error shown as a problem")
        if case .exited(let code) = bad.ending { check(code != 0, "non-zero exit seen") }

        let missing = DispatchSemaphore(value: 0)
        let box = Box()
        CLIRunner().start(executable: URL(fileURLWithPath: "/nonexistent/wing-autogain"), arguments: [],
                          onLine: { _, _ in }, onEnd: { e in box.ending = e; missing.signal() })
        missing.wait()
        if case .couldNotStart(let m) = box.ending { print("\n== Missing CLI\n   \(m)"); check(true, "missing CLI reported") }
        else { check(false, "missing CLI reported") }

        print(failures.isEmpty ? "\nSelf-test passed." : "\nSelf-test FAILED: \(failures.joined(separator: "; "))")
        return failures.isEmpty ? 0 : 1
    }

    final class Box: @unchecked Sendable {
        private let lock = NSLock()
        private var _lines: [ResultLine] = []
        private var _ending: CLIRunner.Ending?
        func add(_ l: ResultLine) { lock.lock(); _lines.append(l); lock.unlock() }
        var lines: [ResultLine] { lock.lock(); defer { lock.unlock() }; return _lines }
        var ending: CLIRunner.Ending? {
            get { lock.lock(); defer { lock.unlock() }; return _ending }
            set { lock.lock(); _ending = newValue; lock.unlock() }
        }
    }
}
