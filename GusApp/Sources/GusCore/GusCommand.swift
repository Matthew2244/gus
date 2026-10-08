import Foundation

public enum GainTarget: Equatable, Sendable {
    case everything
    case section(String)   // DCA argument
    case channel(String)   // channel argument

    var arguments: [String] {
        switch self {
        case .everything: return ["--all"]
        case .section(let s): return ["--dca", s]
        case .channel(let c): return ["--channel", c]
        }
    }
}

public struct RunOptions: Equatable, Sendable {
    public var target: GainTarget
    public var show: String?
    public var practice: Bool
    public var dryRun: Bool
    public var oneAtATime: Bool
    public var listenSeconds: Int

    public init(target: GainTarget = .everything, show: String? = nil, practice: Bool = false,
                dryRun: Bool = false, oneAtATime: Bool = false, listenSeconds: Int = 8) {
        self.target = target
        self.show = show
        self.practice = practice
        self.dryRun = dryRun
        self.oneAtATime = oneAtATime
        self.listenSeconds = listenSeconds
    }
}

/// Builds the exact argument lists for wing-autogain. Always --quiet: the app does the talking,
/// so nothing is ever spoken twice.
public enum GusCommand {
    public static var executable: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("bin/wing-autogain")
    }

    /// A Finder-launched app gets /usr/bin:/bin:/usr/sbin:/sbin. Give the CLI the PATH a Terminal has.
    public static func environment(base: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
        var env = base
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let wanted = ["\(home)/bin", "\(home)/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                      "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let existing = (base["PATH"] ?? "").split(separator: ":").map(String.init)
        var path: [String] = []
        for p in wanted + existing where !p.isEmpty && !path.contains(p) { path.append(p) }
        env["PATH"] = path.joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        if env["HOME"] == nil { env["HOME"] = home }
        return env
    }

    private static func common(_ o: RunOptions) -> [String] {
        var a: [String] = []
        if let s = o.show, !s.isEmpty { a += ["--show", s] }
        if o.practice { a.append("--simulate") }
        return a
    }

    public static func run(_ o: RunOptions) -> [String] {
        var a = o.target.arguments + common(o)
        if o.dryRun { a.append("--dry-run") }
        if o.oneAtATime { a.append("--each") }
        a += ["--listen", String(max(1, o.listenSeconds)), "--quiet"]
        return a
    }

    public static func status(_ o: RunOptions) -> [String] {
        o.target.arguments + common(o) + ["--status", "--quiet"]
    }

    public static func undo(_ o: RunOptions) -> [String] {
        common(o) + ["--undo", "--quiet"]
    }

    public static func discover() -> [String] { ["--discover", "--quiet"] }

    public static func setHost(_ ip: String) -> [String] {
        ["--set-host", ip.trimmingCharacters(in: .whitespaces), "--quiet"]
    }

    /// The WING address saved by --set-host, if any.
    public static func savedHost() -> String {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/wing-autogain/config.json")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return "" }
        return (obj["host"] as? String) ?? ""
    }
}
