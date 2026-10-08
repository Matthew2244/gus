import Foundation

/// One DCA or channel from a show file. `argument` is what goes to the CLI; `title` is shown.
public struct ShowItem: Identifiable, Hashable, Sendable {
    public let number: Int
    public let name: String
    public let title: String
    public let argument: String
    public var id: String { argument }
}

public struct ShowContents: Sendable, Equatable {
    public var sections: [ShowItem] = []
    public var channels: [ShowItem] = []
    public init(sections: [ShowItem] = [], channels: [ShowItem] = []) {
        self.sections = sections
        self.channels = channels
    }
}

/// Reads ~/Documents/WING Shows/*.snap. Lists always come from disk, so they match the files.
public struct ShowLibrary: Sendable {
    public let folder: URL
    /// DCAs that are not sections of the band.
    public static let excludedSections: Set<String> = ["FX", "PLAYBACK"]

    public init(folder: URL = ShowLibrary.defaultFolder) {
        self.folder = folder
    }

    public static var defaultFolder: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Documents/WING Shows", isDirectory: true)
    }

    public func showNames() -> [String] {
        let files = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        return files.filter { $0.lowercased().hasSuffix(".snap") }
            .map { String($0.dropLast(5)) }
            .sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    }

    public func contents(of show: String) -> ShowContents {
        let url = folder.appendingPathComponent(show + ".snap")
        guard let data = try? Data(contentsOf: url) else { return ShowContents() }
        return Self.parse(data)
    }

    public static func parse(_ data: Data) -> ShowContents {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ae = root["ae_data"] as? [String: Any] else { return ShowContents() }

        func numbered(_ key: String) -> [(Int, String)] {
            guard let dict = ae[key] as? [String: Any] else { return [] }
            return dict.compactMap { k, v -> (Int, String)? in
                guard let n = Int(k), let entry = v as? [String: Any],
                      let name = (entry["name"] as? String)?.trimmingCharacters(in: .whitespaces),
                      !name.isEmpty else { return nil }
                return (n, name)
            }.sorted { $0.0 < $1.0 }
        }

        let dcas = numbered("dca").filter { !excludedSections.contains($0.1.uppercased()) }
        let chans = numbered("ch")

        func items(_ list: [(Int, String)], titled: Bool) -> [ShowItem] {
            var counts: [String: Int] = [:]
            for (_, n) in list { counts[n.lowercased(), default: 0] += 1 }
            return list.map { num, name in
                // A repeated name would be ambiguous to the CLI, so send the number instead.
                let arg = (counts[name.lowercased()] ?? 0) > 1 ? String(num) : name
                let title = titled && name == name.uppercased() ? name.capitalized : name
                return ShowItem(number: num, name: name, title: title, argument: arg)
            }
        }
        return ShowContents(sections: items(dcas, titled: true), channels: items(chans, titled: false))
    }
}
