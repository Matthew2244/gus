import SwiftUI
import GusCore

struct ResultsPanel: View {
    @Bindable var model: GusModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Results")
                        .gusFont(20, .bold, design: .rounded)
                        .accessibilityAddTraits(.isHeader)
                    if !model.heading.isEmpty {
                        Text(model.heading)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button { model.copyResults() } label: {
                    Label("Copy", systemImage: "doc.on.doc")
                }
                .disabled(model.lines.isEmpty)
                .accessibilityLabel("Copy results")
                .accessibilityHint("Copies every line, to paste somewhere else.")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)

            Divider()

            if model.lines.isEmpty && !model.isRunning {
                EmptyResults(practice: model.practice)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(model.lines) { line in
                                ResultRow(line: line).id(line.id)
                            }
                            if model.isRunning {
                                WaitingRow(label: model.job?.busyLabel ?? "Working…")
                                    .id("waiting")
                            }
                        }
                        .padding(16)
                    }
                    .onChange(of: model.lines.count) {
                        let target: AnyHashable = model.isRunning ? AnyHashable("waiting") : AnyHashable(model.lines.last?.id)
                        if reduceMotion {
                            proxy.scrollTo(target, anchor: .bottom)
                        } else {
                            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(target, anchor: .bottom) }
                        }
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Results")
            }
        }
        .background(.background.secondary.opacity(0.5))
    }
}

struct ResultRow: View {
    let line: ResultLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: line.kind.symbol)
                .foregroundStyle(line.kind.tint)
                .gusFont(15, .semibold)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                if let badge = line.kind.badge {
                    Text(badge.uppercased())
                        .gusFont(10, .heavy)
                        .tracking(0.8)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .foregroundStyle(line.kind == .warning ? Color.black : Color.white)
                        .background(line.kind.tint, in: Capsule())
                }
                Text(line.display)
                    .gusFont(line.kind == .summary ? 14 : 13, line.kind == .summary ? .semibold : .regular)
                    .foregroundStyle(line.kind == .progress ? .secondary : .primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(background, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(alignment: .leading) {
            if line.kind == .warning || line.kind == .problem {
                // A thick edge as well as the tint, so a warning reads at a glance even in greyscale.
                UnevenRoundedRectangle(topLeadingRadius: 10, bottomLeadingRadius: 10)
                    .fill(line.kind.tint)
                    .frame(width: 4)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(line.spoken)
        .accessibilityAddTraits(line.kind == .summary ? [.isStaticText, .isHeader] : .isStaticText)
    }

    private var background: AnyShapeStyle {
        switch line.kind {
        case .warning: return AnyShapeStyle(Color.orange.opacity(0.14))
        case .problem: return AnyShapeStyle(Color.red.opacity(0.14))
        case .summary: return AnyShapeStyle(GusColors.brandB.opacity(0.12))
        case .progress: return AnyShapeStyle(Color.clear)
        default: return AnyShapeStyle(.background)
        }
    }
}

struct WaitingRow: View {
    let label: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        HStack(spacing: 10) {
            if reduceMotion {
                Image(systemName: "ellipsis").foregroundStyle(.secondary)
            } else {
                ProgressView().controlSize(.small)
            }
            Text(label).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityAddTraits(.isStaticText)
    }
}

struct EmptyResults: View {
    let practice: Bool
    var body: some View {
        VStack(spacing: 14) {
            Spacer()
            Image(systemName: "waveform.and.mic")
                .font(.system(size: 46, weight: .light))
                .foregroundStyle(GusColors.brand)
                .accessibilityHidden(true)
            Text("All ears, nothing yet")
                .gusFont(17, .semibold)
            Text(practice
                 ? "Practice mode is on, so this is a pretend WING. Press Listen and set gains and I'll write down everything I hear, right here."
                 : "Pick what to set and press Listen and set gains. I'll write down everything I hear, right here.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 340)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .accessibilityElement(children: .combine)
    }
}
