import AppKit
import AVFoundation

/// Says each result out loud as it arrives.
///
/// VoiceOver running: a VoiceOver announcement at high priority, so it is in his own voice and rate
/// and lands in VoiceOver's history. A high-priority announcement interrupts the one before it, and
/// the CLI often prints several lines at once (everyone measured together), so lines that arrive
/// close together are joined into one announcement, and a new one waits for the previous one's
/// estimated speaking time rather than cutting it off.
///
/// VoiceOver off: the Mac's own voice, which queues by itself.
@MainActor
final class Speaker {
    var enabled = true

    private let synth = AVSpeechSynthesizer()
    private var pending: [String] = []
    private var flushTask: Task<Void, Never>?
    private var busyUntil = Date.distantPast

    /// Gap that counts as "the same burst".
    private let gather: Duration = .milliseconds(250)
    /// Deliberately on the quick side of VoiceOver rates: a slightly early next line beats a long silence.
    private let charsPerSecond = 22.0

    func say(_ text: String) {
        guard enabled, !text.isEmpty else { return }
        if NSWorkspace.shared.isVoiceOverEnabled {
            pending.append(text)
            scheduleFlush()
        } else {
            let u = AVSpeechUtterance(string: text)
            u.prefersAssistiveTechnologySettings = true
            synth.speak(u)
        }
    }

    func stopSpeaking() {
        pending.removeAll()
        flushTask?.cancel()
        flushTask = nil
        synth.stopSpeaking(at: .word)
    }

    private func scheduleFlush() {
        flushTask?.cancel()
        flushTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: self.gather)
            let wait = self.busyUntil.timeIntervalSinceNow
            if wait > 0 { try? await Task.sleep(for: .seconds(min(wait, 20))) }
            if Task.isCancelled { return }
            self.flush()
        }
    }

    private func flush() {
        guard !pending.isEmpty else { return }
        let text = pending.joined(separator: " ")
        pending.removeAll()
        Self.announce(text)
        busyUntil = Date().addingTimeInterval(Double(text.count) / charsPerSecond)
    }

    static func announce(_ text: String) {
        let element: Any = NSApp.mainWindow ?? NSApp.windows.first ?? NSApp as Any
        NSAccessibility.post(element: element, notification: .announcementRequested, userInfo: [
            .announcement: text,
            .priority: NSAccessibilityPriorityLevel.high.rawValue,
        ])
    }
}
