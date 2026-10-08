import XCTest
@testable import GusCore

final class LineParserTests: XCTestCase {
    func testRaisedResult() {
        let l = LineParser.parse("Matt Vox: peaks were minus 22. Raised gain 8 dB to 32. Now peaking around minus 10. Nice and healthy.")!
        XCTAssertEqual(l.kind, .raised)
        XCTAssertEqual(l.spoken, l.text)
    }

    func testLoweredAndDryRun() {
        XCTAssertEqual(LineParser.classify("Hi-Hat: peaks were minus 2. Lowered gain 8 dB to 4. Now peaking around minus 10."), .lowered)
        XCTAssertEqual(LineParser.classify("Vox 2: peaks were minus 37. Would raise gain 12 dB, from 0 to 12."), .raised)
        XCTAssertEqual(LineParser.classify("Vox 2: peaks were minus 7. Would lower gain 3 dB, from 20 to 17."), .lowered)
    }

    func testClippingWarningSpokenWarningFirst() {
        let l = LineParser.parse("Snare 1 Top: WARNING, CLIPPING, peaks hit 0. Lowered gain 6 dB to 26. Now peaking around minus 11.")!
        XCTAssertEqual(l.kind, .warning)
        XCTAssertEqual(l.spoken, "Warning. Snare 1 Top: clipping, peaks hit 0. Lowered gain 6 dB to 26. Now peaking around minus 11.")
        XCTAssertEqual(l.text, "Snare 1 Top: WARNING, CLIPPING, peaks hit 0. Lowered gain 6 dB to 26. Now peaking around minus 11.")
        XCTAssertEqual(l.display, "Snare 1 Top: clipping, peaks hit 0. Lowered gain 6 dB to 26. Now peaking around minus 11.")
    }

    func testSecondWarningInLineIsLowered() {
        let l = LineParser.parse("Kick In: WARNING, CLIPPING, peaks hit 0. Lowered gain 3 dB to minus 3. WARNING, still clipping at minimum gain, minus 3.")!
        XCTAssertEqual(l.kind, .warning)
        XCTAssertTrue(l.spoken.hasPrefix("Warning. Kick In: clipping"))
        XCTAssertFalse(l.spoken.contains("WARNING"))
        XCTAssertTrue(l.spoken.contains("Warning, still clipping"))
    }

    func testSilentWarning() {
        let l = LineParser.parse("Vox 4: WARNING, no signal heard. Gain left at 30. Check the cable and the mute.")!
        XCTAssertEqual(l.kind, .warning)
        XCTAssertEqual(l.spoken, "Warning. Vox 4: no signal heard. Gain left at 30. Check the cable and the mute.")
    }

    func testSummaries() {
        XCTAssertEqual(LineParser.classify("Done: 4 raised. Undo last run puts them back."), .summary)
        XCTAssertEqual(LineParser.classify("Dry run: 4 of 4 would change."), .summary)
        XCTAssertEqual(LineParser.classify("Undo done: 1 restored."), .summary)
        XCTAssertEqual(LineParser.classify("Done."), .summary)
    }

    func testSummaryMentionsTheButtonNotTheCommand() {
        let l = LineParser.parse("Done: 4 raised. wing-autogain --undo puts them back. That's a wrap on gains.")!
        XCTAssertEqual(l.text, "Done: 4 raised. Undo last run puts them back. That's a wrap on gains.")
    }

    func testProgressAndNotices() {
        XCTAssertEqual(LineParser.classify("Listening to 4 inputs for 8 seconds. Whenever you're ready."), .progress)
        XCTAssertEqual(LineParser.classify("Next: Matt Vox. Listening for 8 seconds."), .progress)
        XCTAssertEqual(LineParser.classify("Skipped: Click has no preamp."), .notice)
        XCTAssertEqual(LineParser.classify("Stopped. Any gain I already changed is in the log."), .notice)
        XCTAssertEqual(LineParser.classify("Nothing to undo for the simulator."), .notice)
        XCTAssertEqual(LineParser.classify("Vox 2: gain is 20 now, not the 24 I set, so someone changed it since. Left alone."), .notice)
    }

    func testProblems() {
        let l = LineParser.parse("Problem: the console didn't answer")!
        XCTAssertEqual(l.kind, .problem)
        XCTAssertEqual(l.spoken, "Problem. the console didn't answer")
        let e = LineParser.parse("No DCA called 'NOPE'.", fromStderr: true)!
        XCTAssertEqual(e.kind, .problem)
        XCTAssertEqual(e.spoken, "Problem. No DCA called 'NOPE'.")
        XCTAssertEqual(e.display, "No DCA called 'NOPE'.")
        XCTAssertEqual(LineParser.parse("Warning: couldn't write the log", fromStderr: true)!.kind, .warning)
    }

    func testStatusLines() {
        XCTAssertEqual(LineParser.classify("Vox 2, B23, channel 33: gain 24, phantom off, mono, target minus 12."), .result)
        let indented = LineParser.parse("  Matt Vox: peaks were minus 41. Raised gain 29 dB to 29.")!
        XCTAssertEqual(indented.text, "Matt Vox: peaks were minus 41. Raised gain 29 dB to 29.")
    }

    func testFriendlyWording() {
        let w = LineParser.parse("Which WING? Give --host 192.168.x.x, or --set-host once, or --discover to find it. --simulate tries it on a pretend one.", fromStderr: true)!
        XCTAssertEqual(w.kind, .problem)
        XCTAssertFalse(w.text.contains("--"))
        let l = LineParser.parse("Last run 2020-01-02T17:04:01, 4 inputs:")!
        XCTAssertEqual(l.kind, .notice)
        XCTAssertEqual(l.text, "Last run, Thursday 2 January at 5:04 PM, 4 inputs.")
    }

    func testBlankLinesDropped() {
        XCTAssertNil(LineParser.parse(""))
        XCTAssertNil(LineParser.parse("   "))
    }

    func testDiscovery() {
        XCTAssertEqual(LineParser.discoveredAddress(in: "Found WING-Matt at 192.168.68.50, model WING, firmware 3.1."), "192.168.68.50")
        XCTAssertNil(LineParser.discoveredAddress(in: "No WING answered on this network."))
        XCTAssertEqual(LineParser.classify("No WING answered on this network."), .warning)
    }

    func testHostValidation() {
        XCTAssertTrue(LineParser.isPlausibleHost("192.168.68.50"))
        XCTAssertTrue(LineParser.isPlausibleHost("wing.local"))
        XCTAssertFalse(LineParser.isPlausibleHost(""))
        XCTAssertFalse(LineParser.isPlausibleHost("192.168.68"))
        XCTAssertFalse(LineParser.isPlausibleHost("300.1.1.1"))
        XCTAssertFalse(LineParser.isPlausibleHost("my wing"))
    }

    func testLineBufferSplitsAcrossChunks() {
        let b = LineBuffer()
        XCTAssertEqual(b.append(Data("Matt Vox: pe".utf8)), [])
        XCTAssertEqual(b.append(Data("aks\nVox 2: ok\nDon".utf8)), ["Matt Vox: peaks", "Vox 2: ok"])
        XCTAssertEqual(b.finish(), ["Don"])
        XCTAssertEqual(b.finish(), [])
    }
}

final class CommandTests: XCTestCase {
    func testRunArguments() {
        let o = RunOptions(target: .section("VOCALS"), show: "The Woodshed", practice: true,
                           dryRun: true, oneAtATime: true, listenSeconds: 8)
        XCTAssertEqual(GusCommand.run(o), ["--dca", "VOCALS", "--show", "The Woodshed", "--simulate",
                                           "--dry-run", "--each", "--listen", "8", "--quiet"])
    }

    func testRealRunEverything() {
        let o = RunOptions(target: .everything, show: "Swing Shift")
        XCTAssertEqual(GusCommand.run(o), ["--all", "--show", "Swing Shift", "--listen", "8", "--quiet"])
    }

    func testOtherJobsAreQuiet() {
        let o = RunOptions(target: .channel("Matt Vox"), show: "The Lab", practice: true)
        XCTAssertEqual(GusCommand.status(o), ["--channel", "Matt Vox", "--show", "The Lab", "--simulate", "--status", "--quiet"])
        XCTAssertEqual(GusCommand.undo(o), ["--show", "The Lab", "--simulate", "--undo", "--quiet"])
        XCTAssertEqual(GusCommand.setHost(" 10.0.0.5 "), ["--set-host", "10.0.0.5", "--quiet"])
        XCTAssertEqual(GusCommand.discover(), ["--discover", "--quiet"])
    }

    func testEnvironmentFixesFinderPath() {
        let env = GusCommand.environment(base: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        let path = env["PATH"]!.split(separator: ":").map(String.init)
        XCTAssertTrue(path.contains("/opt/homebrew/bin"))
        XCTAssertTrue(path.contains("/usr/bin"))
        XCTAssertEqual(Set(path).count, path.count, "no duplicates")
        XCTAssertEqual(env["PYTHONUNBUFFERED"], "1")
    }
}

final class ShowLibraryTests: XCTestCase {
    func testParsesSectionsAndChannels() throws {
        let json = """
        {"ae_data": {
          "dca": {"1": {"name": "DRUMS"}, "2": {"name": "MATT VOX"}, "10": {"name": "FX"},
                  "11": {"name": "PLAYBACK"}, "12": {"name": ""}},
          "ch": {"2": {"name": "Kick Out"}, "1": {"name": "Kick In"}, "3": {"name": ""},
                 "4": {"name": "Tom"}, "5": {"name": "tom"}}
        }}
        """
        let c = ShowLibrary.parse(Data(json.utf8))
        XCTAssertEqual(c.sections.map(\.title), ["Drums", "Matt Vox"])
        XCTAssertEqual(c.sections.map(\.argument), ["DRUMS", "MATT VOX"])
        XCTAssertEqual(c.channels.map(\.name), ["Kick In", "Kick Out", "Tom", "tom"])
        // Duplicate names go to the CLI by number so they can't be ambiguous.
        XCTAssertEqual(c.channels.map(\.argument), ["Kick In", "Kick Out", "4", "5"])
    }

    func testBadDataIsEmpty() {
        XCTAssertEqual(ShowLibrary.parse(Data("nope".utf8)), ShowContents())
    }
    // The real show files live in ~/Documents, which the test runner has no permission to read;
    // `Gus --self-test` covers them instead.
}

final class RunnerTests: XCTestCase {
    func testStreamsLinesAndExitCode() {
        let done = expectation(description: "ended")
        let got = Collector()
        CLIRunner().start(executable: URL(fileURLWithPath: "/bin/sh"),
                          arguments: ["-c", "echo one; echo oops >&2; printf 'two'; exit 3"],
                          onLine: { l, s in got.add("\(s == .stderr ? "E" : "O"):\(l)") },
                          onEnd: { e in got.ending = e; done.fulfill() })
        wait(for: [done], timeout: 10)
        XCTAssertEqual(Set(got.items), ["O:one", "E:oops", "O:two"])
        XCTAssertEqual(got.ending, .exited(3))
    }

    func testStopSendsInterruptLikeControlC() {
        let started = expectation(description: "started")
        let done = expectation(description: "ended")
        let got = Collector()
        let r = CLIRunner()
        r.start(executable: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", "trap 'echo Stopped.; exit 130' INT; echo ready; while :; do sleep 0.1; done"],
                onLine: { l, _ in got.add(l); if l == "ready" { started.fulfill() } },
                onEnd: { e in got.ending = e; done.fulfill() })
        wait(for: [started], timeout: 10)
        r.stop()
        wait(for: [done], timeout: 10)
        XCTAssertTrue(got.items.contains("Stopped."))
        XCTAssertEqual(got.ending, .interrupted(130))
    }

    func testMissingExecutable() {
        let done = expectation(description: "ended")
        let got = Collector()
        CLIRunner().start(executable: URL(fileURLWithPath: "/nope/wing-autogain"), arguments: [],
                          onLine: { _, _ in }, onEnd: { e in got.ending = e; done.fulfill() })
        wait(for: [done], timeout: 5)
        guard case .couldNotStart(let m) = got.ending else { return XCTFail("expected couldNotStart") }
        XCTAssertTrue(m.contains("can't find wing-autogain"))
    }
}

final class Collector: @unchecked Sendable {
    private let lock = NSLock()
    private var _items: [String] = []
    private var _ending: CLIRunner.Ending?
    func add(_ s: String) { lock.lock(); _items.append(s); lock.unlock() }
    var items: [String] { lock.lock(); defer { lock.unlock() }; return _items }
    var ending: CLIRunner.Ending? {
        get { lock.lock(); defer { lock.unlock() }; return _ending }
        set { lock.lock(); _ending = newValue; lock.unlock() }
    }
}
