import AppKit
import GusCore
import Observation

enum TargetKind: String, CaseIterable, Identifiable {
    case everything, section, channel
    var id: String { rawValue }
    var title: String {
        switch self {
        case .everything: return "Everything"
        case .section: return "A section"
        case .channel: return "One channel"
        }
    }
}

enum JobKind {
    case run, status, undo, discover, saveHost

    var busyLabel: String {
        switch self {
        case .run: return "Gus is listening…"
        case .status: return "Gus is reading the gains…"
        case .undo: return "Gus is putting the gains back…"
        case .discover: return "Gus is looking for your WING…"
        case .saveHost: return "Saving the address…"
        }
    }
}

@MainActor
@Observable
final class GusModel {
    // MARK: remembered settings
    private let defaults = UserDefaults.standard

    var targetKind: TargetKind { didSet { defaults.set(targetKind.rawValue, forKey: "targetKind") } }
    var show: String { didSet { defaults.set(show, forKey: "show"); reloadShow() } }
    var sectionID: String { didSet { defaults.set(sectionID, forKey: "section") } }
    var channelID: String { didSet { defaults.set(channelID, forKey: "channel") } }
    var practice: Bool { didSet { defaults.set(practice, forKey: "practice") } }
    var dryRun: Bool { didSet { defaults.set(dryRun, forKey: "dryRun") } }
    var oneAtATime: Bool { didSet { defaults.set(oneAtATime, forKey: "each") } }
    var listenSeconds: Int { didSet { defaults.set(listenSeconds, forKey: "listen") } }
    var speakResults: Bool { didSet { defaults.set(speakResults, forKey: "speak"); speaker.enabled = speakResults; if !speakResults { speaker.stopSpeaking() } } }
    var welcomed: Bool { didSet { defaults.set(welcomed, forKey: "welcomed") } }
    var textScale: Double { didSet { defaults.set(textScale, forKey: "textScale") } }

    // MARK: live state
    var showNames: [String] = []
    var contents = ShowContents()
    var host: String = ""
    var savedHost: String = ""
    var lines: [ResultLine] = []
    var heading: String = ""
    var job: JobKind?
    var isRunning: Bool { job != nil }
    var confirmingUndo = false

    private let runner = CLIRunner()
    private let speaker = Speaker()
    private let library = ShowLibrary()

    init() {
        let d = UserDefaults.standard
        d.register(defaults: ["practice": false, "dryRun": false, "each": false, "listen": 8,
                              "speak": true, "welcomed": false, "textScale": 1.0,
                              "targetKind": TargetKind.everything.rawValue, "show": "The Woodshed"])
        targetKind = TargetKind(rawValue: d.string(forKey: "targetKind") ?? "") ?? .everything
        show = d.string(forKey: "show") ?? "The Woodshed"
        sectionID = d.string(forKey: "section") ?? ""
        channelID = d.string(forKey: "channel") ?? ""
        practice = d.bool(forKey: "practice")
        dryRun = d.bool(forKey: "dryRun")
        oneAtATime = d.bool(forKey: "each")
        listenSeconds = min(60, max(2, d.integer(forKey: "listen")))
        speakResults = d.bool(forKey: "speak")
        welcomed = d.bool(forKey: "welcomed")
        textScale = min(1.6, max(0.85, d.double(forKey: "textScale")))
        speaker.enabled = speakResults

        savedHost = GusCommand.savedHost()
        host = savedHost
        // Reading ~/Documents can sit behind the macOS "access your Documents folder" question
        // on first launch, so it never happens on the main thread: the window comes up at once.
        loadShows()
    }

    var showsLoaded = false

    func loadShows() {
        let lib = library
        let wanted = show
        Task.detached(priority: .userInitiated) {
            let names = lib.showNames()
            let pick = names.contains(wanted) ? wanted : (names.first ?? wanted)
            let contents = lib.contents(of: pick)
            await MainActor.run {
                self.showNames = names
                self.loadingShow = true
                if self.show != pick { self.show = pick }
                self.loadingShow = false
                self.apply(contents)
                self.showsLoaded = true
            }
        }
    }

    private var loadingShow = false

    func reloadShow() {
        guard !loadingShow else { return }
        let lib = library
        let name = show
        Task.detached(priority: .userInitiated) {
            let c = lib.contents(of: name)
            await MainActor.run { if self.show == name { self.apply(c) } }
        }
    }

    private func apply(_ c: ShowContents) {
        contents = c
        if !contents.sections.contains(where: { $0.id == sectionID }) {
            sectionID = contents.sections.first(where: { $0.name.uppercased() == "VOCALS" })?.id
                ?? contents.sections.first?.id ?? ""
        }
        if !contents.channels.contains(where: { $0.id == channelID }) {
            channelID = contents.channels.first?.id ?? ""
        }
    }

    var selectedSection: ShowItem? { contents.sections.first { $0.id == sectionID } }
    var selectedChannel: ShowItem? { contents.channels.first { $0.id == channelID } }

    var target: GainTarget? {
        switch targetKind {
        case .everything: return .everything
        case .section: return selectedSection.map { .section($0.argument) }
        case .channel: return selectedChannel.map { .channel($0.argument) }
        }
    }

    var targetDescription: String {
        switch targetKind {
        case .everything: return "everything"
        case .section: return selectedSection?.title ?? "a section"
        case .channel: return selectedChannel?.title ?? "a channel"
        }
    }

    var options: RunOptions {
        RunOptions(target: target ?? .everything, show: show.isEmpty ? nil : show, practice: practice,
                   dryRun: dryRun, oneAtATime: oneAtATime, listenSeconds: listenSeconds)
    }

    var canRun: Bool { !isRunning && target != nil }
    var canSaveHost: Bool {
        !isRunning && LineParser.isPlausibleHost(host)
            && host.trimmingCharacters(in: .whitespaces) != savedHost
    }

    // MARK: actions

    func listenAndSet() {
        guard canRun else { return }
        var head = "Setting gains for \(targetDescription)"
        if dryRun { head = "Measuring \(targetDescription), changing nothing" }
        if practice { head += ", on the pretend WING" }
        var args = GusCommand.run(options)
        // Developer hook for testing the window, e.g. GUS_EXTRA_ARGS="--sim-signal A1=clip".
        if let extra = ProcessInfo.processInfo.environment["GUS_EXTRA_ARGS"], !extra.isEmpty {
            args += extra.split(separator: " ").map(String.init)
        }
        start(.run, heading: head, arguments: args)
    }

    func status() {
        guard !isRunning, target != nil else { return }
        start(.status, heading: "The gains right now, \(targetDescription)\(practice ? ", pretend WING" : "")",
              arguments: GusCommand.status(options))
    }

    func askToUndo() {
        guard !isRunning else { return }
        confirmingUndo = true
    }

    func undo() {
        guard !isRunning else { return }
        start(.undo, heading: practice ? "Undoing the last run on the pretend WING" : "Undoing the last run",
              arguments: GusCommand.undo(options))
    }

    func discover() {
        guard !isRunning else { return }
        start(.discover, heading: "Looking for a WING on the network", arguments: GusCommand.discover())
    }

    func saveHost() {
        guard canSaveHost else { return }
        let ip = host.trimmingCharacters(in: .whitespaces)
        start(.saveHost, heading: "Remembering the WING address", arguments: GusCommand.setHost(ip))
    }

    func stop() {
        guard isRunning else { return }
        runner.stop()
        speaker.say("Stopping.")
    }

    func copyResults() {
        let text = ([heading] + lines.map(\.display)).joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        speaker.say("Copied \(lines.count) line\(lines.count == 1 ? "" : "s").")
    }

    // MARK: running

    private func start(_ kind: JobKind, heading: String, arguments: [String]) {
        speaker.stopSpeaking()
        lines.removeAll()
        self.heading = heading
        job = kind
        if kind == .run {
            speaker.say(oneAtATime ? "Listening. I'll name each one first." : "Listening. Play like it's the show.")
        }
        runner.start(arguments: arguments,
                     onLine: { [weak self] text, stream in
                         Task { @MainActor in self?.receive(text, stderr: stream == .stderr) }
                     },
                     onEnd: { [weak self] ending in
                         Task { @MainActor in self?.finish(ending) }
                     })
    }

    private func receive(_ text: String, stderr: Bool) {
        guard let line = LineParser.parse(text, fromStderr: stderr) else { return }
        lines.append(line)
        speaker.say(line.spoken)
        if job == .discover, !lines.dropLast().contains(where: { $0.text.hasPrefix("Found ") }), let ip = LineParser.discoveredAddress(in: line.text) {
            host = ip
        }
    }

    private func finish(_ ending: CLIRunner.Ending) {
        let kind = job
        job = nil
        switch ending {
        case .couldNotStart(let message):
            add(LineParser.parse(message, fromStderr: true)!)
        case .exited(let code):
            // Exit 1 after a run means "there were warnings", already said line by line.
            if code != 0 && !lines.contains(where: { $0.kind == .summary || $0.kind == .problem
                                                    || $0.kind == .warning }) {
                add(ResultLine(text: "Gus stopped without finishing (code \(code)). Nothing more to report.",
                               kind: .problem,
                               spoken: "Problem. Gus stopped without finishing, code \(code)."))
            } else if lines.isEmpty {
                add(ResultLine(text: "Done, nothing to report.", kind: .summary, spoken: "Done, nothing to report."))
            }
        case .interrupted:
            if !lines.contains(where: { $0.text.hasPrefix("Stopped.") }) {
                add(ResultLine(text: "Stopped. Anything already changed is logged, and Undo last run puts it back.",
                               kind: .notice,
                               spoken: "Stopped. Anything already changed is logged, and Undo last run puts it back."))
            }
        }
        if kind == .saveHost { savedHost = GusCommand.savedHost(); if !savedHost.isEmpty { host = savedHost } }
    }

    private func add(_ line: ResultLine) {
        lines.append(line)
        speaker.say(line.spoken)
    }
}
