import Foundation

/// What kind of sentence wing-autogain just said. Drives the icon, the colour, the badge word
/// and how the line is spoken. Colour is never the only signal: every kind has its own icon,
/// and warnings and problems carry a written badge too.
public enum LineKind: String, Sendable, CaseIterable {
    case progress   // "Listening to 4 inputs…", "Next: Matt Vox…"
    case raised     // gain went (or would go) up
    case lowered    // gain went (or would go) down
    case result     // anything else per input: left alone, status readback, found a WING
    case notice     // Skipped, Stopped, Note, Nothing to do
    case warning    // any line containing WARNING
    case problem    // errors: stderr, "Problem: …", the CLI missing
    case summary    // "Done: …", "Dry run: …", "Undo done: …"

    /// The word that leads the spoken sentence and labels the visual badge, if any.
    public var badge: String? {
        switch self {
        case .warning: return "Warning"
        case .problem: return "Problem"
        default: return nil
        }
    }
}

public struct ResultLine: Identifiable, Sendable, Equatable {
    public let id: UUID
    /// What the row shows.
    public let text: String
    public let kind: LineKind
    /// The single sentence VoiceOver reads for the row, and what gets announced.
    public let spoken: String

    /// What the row shows: for warnings and problems the badge already says "Warning", so the
    /// sentence drops its shouted WARNING and reads like the spoken version.
    public var display: String {
        if let badge = kind.badge, spoken.hasPrefix(badge + ". ") {
            let rest = spoken.dropFirst(badge.count + 2)
            return rest.prefix(1).uppercased() + rest.dropFirst()
        }
        return text
    }

    public init(id: UUID = UUID(), text: String, kind: LineKind, spoken: String) {
        self.id = id
        self.text = text
        self.kind = kind
        self.spoken = spoken
    }
}

public enum LineParser {
    /// Turn one line of wing-autogain output into a row. Returns nil for blank lines.
    /// `fromStderr` lines are problems unless they say they are only a warning.
    public static func parse(_ raw: String, fromStderr: Bool = false) -> ResultLine? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        text = appify(text)

        let kind = classify(text, fromStderr: fromStderr)
        return ResultLine(text: text, kind: kind, spoken: spokenForm(text, kind: kind))
    }

    /// The CLI talks about itself as a command; in the app the same thing is a button.
    static func appify(_ text: String) -> String {
        var t = text
        t = t.replacingOccurrences(of: "wing-autogain --undo puts them back", with: "Undo last run puts them back")
        t = t.replacingOccurrences(of: "wing-autogain --undo puts it back", with: "Undo last run puts it back")
        if t.hasPrefix("Which WING? Give --host") {
            t = "Which WING? Type its address under Your WING and press Save, or press Find my WING. Practice mode works without one."
        }
        // "Last run 2026-10-08T05:04:01, 4 inputs:" reads badly aloud; say it like a person would.
        if t.hasPrefix("Last run "), let r = t.range(of: #"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d"#, options: .regularExpression) {
            let iso = DateFormatter()
            iso.locale = Locale(identifier: "en_US_POSIX")
            iso.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            if let date = iso.date(from: String(t[r])) {
                t.replaceSubrange(r, with: friendlyTime(date))
                t = t.replacingOccurrences(of: "Last run ", with: "Last run, ", options: .anchored)
                if t.hasSuffix(":") { t = String(t.dropLast()) + "." }
            }
        }
        return t
    }

    static func friendlyTime(_ date: Date, now: Date = Date()) -> String {
        let time = DateFormatter()
        time.locale = Locale(identifier: "en_US")
        time.dateFormat = "h:mm a"
        let cal = Calendar.current
        if cal.isDate(date, inSameDayAs: now) { return "today at " + time.string(from: date) }
        if let y = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(date, inSameDayAs: y) {
            return "yesterday at " + time.string(from: date)
        }
        let day = DateFormatter()
        day.locale = Locale(identifier: "en_US")
        day.dateFormat = "EEEE d MMMM"
        return day.string(from: date) + " at " + time.string(from: date)
    }

    public static func classify(_ text: String, fromStderr: Bool = false) -> LineKind {
        let lower = text.lowercased()
        if fromStderr {
            return lower.hasPrefix("warning") ? .warning : .problem
        }
        if text.hasPrefix("Problem:") || text.hasPrefix("Traceback") { return .problem }
        if text.contains("WARNING") { return .warning }
        if text.hasPrefix("No WING answered") { return .warning }
        if text.hasPrefix("Done:") || text == "Done." || text.hasPrefix("Dry run:")
            || text.hasPrefix("Undo done:") {
            return .summary
        }
        if text.hasPrefix("Listening to ") || text.hasPrefix("Next: ") { return .progress }
        if text.hasPrefix("Skipped:") || text.hasPrefix("Stopped.") || text.hasPrefix("Note:")
            || text.hasPrefix("Nothing to do") || text.hasPrefix("Nothing to undo")
            || text.hasPrefix("No runs logged") || text.hasPrefix("Last run")
            || lower.contains("someone changed it since") {
            return .notice
        }
        if text.contains("Raised gain") || text.contains("Would raise gain") { return .raised }
        if text.contains("Lowered gain") || text.contains("Would lower gain") { return .lowered }
        return .result
    }

    /// The sentence to speak. Warnings and problems lead with their badge word, so the first thing
    /// heard is that something needs attention; shouted capitals are lowered so speech reads them
    /// as words and never spells them out.
    public static func spokenForm(_ text: String, kind: LineKind) -> String {
        var t = text
        switch kind {
        case .warning:
            // "Kick In: WARNING, CLIPPING, peaks hit 0." -> "Warning. Kick In: clipping, peaks hit 0."
            if let r = t.range(of: ": WARNING, ") {
                t.replaceSubrange(r, with: ": ")
            } else if t.hasPrefix("WARNING, ") {
                t.removeFirst("WARNING, ".count)
            } else if t.lowercased().hasPrefix("warning: ") {
                t.removeFirst("warning: ".count)
            }
            t = lowerShouting(t)
            return "Warning. " + t
        case .problem:
            if t.hasPrefix("Problem: ") { t.removeFirst("Problem: ".count) }
            return "Problem. " + lowerShouting(t)
        default:
            return lowerShouting(t)
        }
    }

    static func lowerShouting(_ t: String) -> String {
        t.replacingOccurrences(of: "WARNING", with: "Warning")
            .replacingOccurrences(of: "CLIPPING", with: "clipping")
    }

    /// "Found WING-Matt at 192.168.68.50, model …" -> "192.168.68.50"
    public static func discoveredAddress(in text: String) -> String? {
        guard text.hasPrefix("Found ") else { return nil }
        let pattern = #" at (\d{1,3}(?:\.\d{1,3}){3})"#
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let r = Range(m.range(at: 1), in: text) else { return nil }
        return String(text[r])
    }

    /// A plausible IPv4 address or host name, for enabling the Save button.
    public static func isPlausibleHost(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !t.contains(" ") else { return false }
        let parts = t.split(separator: ".", omittingEmptySubsequences: false)
        if parts.count == 4, parts.allSatisfy({ Int($0) != nil }) {
            return parts.allSatisfy { (Int($0) ?? 999) <= 255 }
        }
        return t.range(of: #"^[A-Za-z0-9][A-Za-z0-9.-]*$"#, options: .regularExpression) != nil
            && !t.allSatisfy({ $0.isNumber || $0 == "." })
    }
}
