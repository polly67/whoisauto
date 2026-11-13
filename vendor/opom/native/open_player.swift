import AppKit
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 4, let expectedPID = Int32(arguments[2]), let link = URL(string: arguments[3]) else {
    fputs("usage: open-player <app> <pid> <url>\n", stderr)
    exit(2)
}
let app = URL(fileURLWithPath: arguments[1]).resolvingSymlinksInPath()
let configuration = NSWorkspace.OpenConfiguration()
configuration.createsNewApplicationInstance = false
configuration.allowsRunningApplicationSubstitution = false
configuration.activates = true
var finished = false
var code: Int32 = 1
NSWorkspace.shared.open([link], withApplicationAt: app, configuration: configuration) { application, error in
    defer { finished = true }
    if let error = error {
        fputs("\(error.localizedDescription)\n", stderr)
        return
    }
    guard let application = application,
          application.processIdentifier == expectedPID,
          application.bundleURL?.resolvingSymlinksInPath() == app else {
        fputs("LaunchServices did not return the requested Player copy\n", stderr)
        return
    }
    print("{\"ok\":true,\"pid\":\(application.processIdentifier)}")
    code = 0
}
let deadline = Date().addingTimeInterval(20)
while !finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
if !finished { fputs("Timed out delivering the place URL\n", stderr) }
exit(code)
