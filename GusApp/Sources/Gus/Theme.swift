import SwiftUI
import GusCore

/// Text scale (View menu: bigger / smaller / actual size). macOS has no system Dynamic Type for
/// SwiftUI apps, so Gus carries its own, applied to every font through this environment value.
private struct GusScaleKey: EnvironmentKey { static let defaultValue: Double = 1.0 }

extension EnvironmentValues {
    var gusScale: Double {
        get { self[GusScaleKey.self] }
        set { self[GusScaleKey.self] = newValue }
    }
}

struct ScaledFont: ViewModifier {
    @Environment(\.gusScale) private var scale
    let size: CGFloat
    let weight: Font.Weight
    let design: Font.Design
    func body(content: Content) -> some View {
        content.font(.system(size: size * scale, weight: weight, design: design))
    }
}

extension View {
    func gusFont(_ size: CGFloat, _ weight: Font.Weight = .regular, design: Font.Design = .default) -> some View {
        modifier(ScaledFont(size: size, weight: weight, design: design))
    }
}

enum GusColors {
    static let brandA = Color(red: 0.16, green: 0.50, blue: 0.96)
    static let brandB = Color(red: 0.42, green: 0.27, blue: 0.93)
    static var brand: LinearGradient {
        LinearGradient(colors: [brandA, brandB], startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

extension LineKind {
    var symbol: String {
        switch self {
        case .progress: return "ear"
        case .raised: return "arrow.up.circle.fill"
        case .lowered: return "arrow.down.circle.fill"
        case .result: return "checkmark.circle.fill"
        case .notice: return "info.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .problem: return "xmark.octagon.fill"
        case .summary: return "flag.checkered"
        }
    }

    var tint: Color {
        switch self {
        case .progress: return .secondary
        case .raised: return .green
        case .lowered: return .blue
        case .result: return .teal
        case .notice: return .gray
        case .warning: return .orange
        case .problem: return .red
        case .summary: return GusColors.brandB
        }
    }
}

/// A rounded panel used for every group of controls.
struct Card<Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: symbol)
                .labelStyle(.titleAndIcon)
                .gusFont(13, .semibold)
                .foregroundStyle(.secondary)
                .accessibilityAddTraits(.isHeader)
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator.opacity(0.6)))
    }
}

/// Gus's face: used in the header and, rendered at 1024 points, as the app icon.
struct GusMark: View {
    var size: CGFloat = 44
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
                .fill(GusColors.brand)
            RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
                .strokeBorder(.white.opacity(0.18), lineWidth: max(1, size * 0.02))
            Image(systemName: "slider.vertical.3")
                .font(.system(size: size * 0.5, weight: .semibold))
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.25), radius: size * 0.03, y: size * 0.02)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
