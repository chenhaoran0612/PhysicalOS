import Foundation

/// Where the app keeps its data: `~/Library/Application Support/<name>/` (issue #55).
///
/// The folder was renamed together with the product. On the first start after the rename the former
/// folder is moved to the new name, and a link is left at the former place: paths saved inside the
/// data, an older copy of the app and scripts that still use the former path keep reaching the same
/// data. When the move cannot be made the app goes on with the former folder; it never starts on an
/// empty folder while the data sits next to it. Checked by tests/test_data_folder.py.
enum DataFolder {
    static let name = "NeuxMind Physical OS"
    static let formerName = "NeuxMind Robot OS"

    enum Outcome: Equatable {
        /// Neither folder exists yet. The caller creates the returned one.
        case fresh
        /// The folder with the current name is there and is used as it is.
        case current
        /// The former folder was moved to the current name just now.
        case moved
        /// The former folder is still in use, and why it could not be moved.
        case keptFormer(String)
    }

    static func resolve(in support: URL, fileManager: FileManager = .default) -> (URL, Outcome) {
        let current = support.appendingPathComponent(name, isDirectory: true)
        let former = support.appendingPathComponent(formerName, isDirectory: true)

        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: current.path, isDirectory: &isDirectory), isDirectory.boolValue {
            return (current, .current)
        }
        guard kind(of: former, fileManager) == .typeDirectory else { return (current, .fresh) }
        // The former folder holds the data. Something that is not a usable folder may be in the way.
        if kind(of: current, fileManager) != nil {
            return (former, .keptFormer("\(current.path) exists and is not a folder"))
        }
        do {
            try fileManager.moveItem(at: former, to: current)
        } catch {
            return (former, .keptFormer(error.localizedDescription))
        }
        // A relative link, so it stays right when the home folder is restored or copied elsewhere.
        // The data is already in place: a link that cannot be made is not a reason to stop.
        try? fileManager.createSymbolicLink(atPath: former.path, withDestinationPath: name)
        return (current, .moved)
    }

    /// What is at this path itself (a link is reported as a link, not as what it points to); nil when nothing is.
    private static func kind(of url: URL, _ fileManager: FileManager) -> FileAttributeType? {
        (try? fileManager.attributesOfItem(atPath: url.path))?[.type] as? FileAttributeType
    }
}
