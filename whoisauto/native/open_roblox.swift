import AppKit
import Foundation

guard CommandLine.arguments.count == 4 else {
    fputs("Invalid Roblox launch request\n", stderr)
    exit(2)
}

let operation = CommandLine.arguments[1]
let app = URL(fileURLWithPath: CommandLine.arguments[2]).resolvingSymlinksInPath()
let copies = URL(fileURLWithPath: CommandLine.arguments[3]).resolvingSymlinksInPath()

if operation == "stop" {
    let players = NSWorkspace.shared.runningApplications.filter { application in
        guard let bundle = application.bundleURL?.resolvingSymlinksInPath() else { return false }
        if bundle == app {
            return FileManager.default.fileExists(atPath: bundle.appendingPathComponent("Contents/Resources/libOpiumware.dylib").path)
        }
        return bundle.lastPathComponent == "Roblox.app"
            && bundle.deletingLastPathComponent().deletingLastPathComponent() == copies
            && FileManager.default.fileExists(atPath: bundle.appendingPathComponent("Contents/Resources/libOpiumware.dylib").path)
    }
    players.forEach { _ = $0.terminate() }
    let deadline = Date().addingTimeInterval(5)
    while players.contains(where: { !$0.isTerminated }) && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    players.filter { !$0.isTerminated }.forEach { _ = $0.forceTerminate() }
    let forcedDeadline = Date().addingTimeInterval(3)
    while players.contains(where: { !$0.isTerminated }) && Date() < forcedDeadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    guard players.allSatisfy({ $0.isTerminated }) else {
        fputs("Roblox Player did not stop\n", stderr)
        exit(1)
    }
    let pids = players.map { String($0.processIdentifier) }.joined(separator: ",")
    print("{\"stopped_pids\":[\(pids)]}")
    exit(0)
}

guard operation == "open" || operation == "login" else {
    fputs("Invalid Roblox launch request\n", stderr)
    exit(2)
}
var link: URL?
if operation == "open" {
    guard
      let input = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8),
      let requested = URL(string: input.trimmingCharacters(in: .whitespacesAndNewlines)),
      requested.scheme == "roblox-player" else {
        fputs("Invalid Roblox launch request\n", stderr)
        exit(2)
    }
    link = requested
}
let configuration = NSWorkspace.OpenConfiguration()
configuration.createsNewApplicationInstance = false
configuration.allowsRunningApplicationSubstitution = false
configuration.activates = true
var finished = false
var result: Int32 = 1
let completion: (NSRunningApplication?, Error?) -> Void = { application, error in
    defer { finished = true }
    guard error == nil, let application = application,
          application.bundleURL?.resolvingSymlinksInPath() == app else {
        // Do not print the error: LaunchServices can include the ticket URL.
        fputs("Could not open the requested Roblox app\n", stderr)
        return
    }
    print("{\"pid\":\(application.processIdentifier)}")
    result = 0
}
if let link = link {
    NSWorkspace.shared.open([link], withApplicationAt: app, configuration: configuration, completionHandler: completion)
} else {
    NSWorkspace.shared.openApplication(at: app, configuration: configuration, completionHandler: completion)
}
let deadline = Date().addingTimeInterval(20)
while !finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
if !finished { fputs("Roblox launch timed out\n", stderr) }
exit(result)
