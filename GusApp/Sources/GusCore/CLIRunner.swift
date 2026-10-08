import Foundation

/// Runs wing-autogain asynchronously and hands back each output line the moment it is complete.
/// Ending is reported only after the process has exited AND both pipes are drained, so the last
/// line can never arrive after "finished".
public final class CLIRunner: @unchecked Sendable {
    public enum Stream: Sendable { case stdout, stderr }

    public enum Ending: Sendable, Equatable {
        case exited(Int32)
        case interrupted(Int32)   // stopped by us
        case couldNotStart(String)
    }

    private let lock = NSLock()
    private var process: Process?
    private var stopRequested = false

    public init() {}

    public var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return process?.isRunning ?? false
    }

    /// `onLine` and `onEnd` are called on a background queue; hop to the main actor yourself.
    public func start(executable: URL = GusCommand.executable,
                      arguments: [String],
                      environment: [String: String] = GusCommand.environment(),
                      onLine: @escaping @Sendable (String, Stream) -> Void,
                      onEnd: @escaping @Sendable (Ending) -> Void) {
        let fm = FileManager.default
        guard fm.fileExists(atPath: executable.path) else {
            onEnd(.couldNotStart("I can't find wing-autogain at \(executable.path). It needs to be there for me to work."))
            return
        }
        guard fm.isExecutableFile(atPath: executable.path) else {
            onEnd(.couldNotStart("wing-autogain at \(executable.path) isn't marked as runnable. In Terminal: chmod +x on it."))
            return
        }

        let p = Process()
        p.executableURL = executable
        p.arguments = arguments
        p.environment = environment
        p.currentDirectoryURL = fm.homeDirectoryForCurrentUser
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice

        let group = DispatchGroup()
        group.enter(); group.enter(); group.enter()

        func attach(_ pipe: Pipe, _ stream: Stream) {
            let buffer = LineBuffer()
            pipe.fileHandleForReading.readabilityHandler = { h in
                let data = h.availableData
                if data.isEmpty {
                    h.readabilityHandler = nil
                    for line in buffer.finish() { onLine(line, stream) }
                    group.leave()
                } else {
                    for line in buffer.append(data) { onLine(line, stream) }
                }
            }
        }
        attach(out, .stdout)
        attach(err, .stderr)

        p.terminationHandler = { _ in group.leave() }

        lock.lock()
        stopRequested = false
        process = p
        lock.unlock()

        do {
            try p.run()
        } catch {
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            lock.lock(); process = nil; lock.unlock()
            onEnd(.couldNotStart("wing-autogain wouldn't start: \(error.localizedDescription)"))
            return
        }
        // The child holds its own copies of the write ends; close ours so EOF arrives.
        try? out.fileHandleForWriting.close()
        try? err.fileHandleForWriting.close()

        // Holds the runner strongly until the run is over, so a fire-and-forget runner still reports.
        group.notify(queue: .global()) {
            self.lock.lock()
            let stopped = self.stopRequested
            self.process = nil
            self.lock.unlock()
            let code = p.terminationStatus
            onEnd(stopped ? .interrupted(code) : .exited(code))
        }
    }

    /// Like Control-C in Terminal: the CLI catches it, says what it already changed, and exits.
    /// If it hasn't gone after a few seconds, it is terminated outright.
    public func stop() {
        lock.lock()
        guard let p = process, p.isRunning else { lock.unlock(); return }
        stopRequested = true
        lock.unlock()
        p.interrupt()
        DispatchQueue.global().asyncAfter(deadline: .now() + 4) {
            if p.isRunning { p.terminate() }
        }
    }
}

/// Splits a byte stream into complete UTF-8 lines. Only touched from one handler queue at a time.
final class LineBuffer: @unchecked Sendable {
    private var pending = Data()

    func append(_ data: Data) -> [String] {
        pending.append(data)
        var lines: [String] = []
        while let nl = pending.firstIndex(of: 0x0A) {
            let lineData = pending[pending.startIndex..<nl]
            lines.append(String(decoding: lineData, as: UTF8.self))
            pending.removeSubrange(pending.startIndex...nl)
        }
        return lines
    }

    func finish() -> [String] {
        defer { pending.removeAll() }
        return pending.isEmpty ? [] : [String(decoding: pending, as: UTF8.self)]
    }
}
