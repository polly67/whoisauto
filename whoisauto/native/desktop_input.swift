import AppKit
import ApplicationServices
import Foundation

guard AXIsProcessTrusted() else {
    fputs("Accessibility permission is required\n", stderr)
    exit(3)
}
let source = CGEventSource(stateID: .hidSystemState)
let bounds = CGDisplayBounds(CGMainDisplayID())
let keys: [String: CGKeyCode] = [
    "KeyA":0,"KeyS":1,"KeyD":2,"KeyF":3,"KeyH":4,"KeyG":5,"KeyZ":6,"KeyX":7,"KeyC":8,"KeyV":9,
    "KeyB":11,"KeyQ":12,"KeyW":13,"KeyE":14,"KeyR":15,"KeyY":16,"KeyT":17,"Digit1":18,"Digit2":19,
    "Digit3":20,"Digit4":21,"Digit6":22,"Digit5":23,"Equal":24,"Digit9":25,"Digit7":26,"Minus":27,
    "Digit8":28,"Digit0":29,"BracketRight":30,"KeyO":31,"KeyU":32,"BracketLeft":33,"KeyI":34,
    "KeyP":35,"Enter":36,"KeyL":37,"KeyJ":38,"Quote":39,"KeyK":40,"Semicolon":41,"Backslash":42,
    "Comma":43,"Slash":44,"KeyN":45,"KeyM":46,"Period":47,"Tab":48,"Space":49,"Backquote":50,
    "Backspace":51,"Escape":53,"MetaLeft":55,"ShiftLeft":56,"CapsLock":57,"AltLeft":58,
    "ControlLeft":59,"ShiftRight":60,"AltRight":61,"ControlRight":62,"F1":122,"F2":120,
    "F3":99,"F4":118,"F5":96,"F6":97,"F7":98,"F8":100,"F9":101,"F10":109,"F11":103,"F12":111,
    "ArrowLeft":123,"ArrowRight":124,"ArrowDown":125,"ArrowUp":126,"Delete":117,"Home":115,"End":119
]
var heldKeys = Set<CGKeyCode>()
var heldButtons = Set<Int>()
var point = CGPoint(x: bounds.midX, y: bounds.midY)

func releaseAll() {
    for key in heldKeys {
        CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false)?.post(tap: .cghidEventTap)
    }
    for button in heldButtons {
        let type: CGEventType = button == 2 ? .rightMouseUp : button == 1 ? .otherMouseUp : .leftMouseUp
        let mouse: CGMouseButton = button == 2 ? .right : button == 1 ? .center : .left
        CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: mouse)?.post(tap: .cghidEventTap)
    }
    heldKeys.removeAll()
    heldButtons.removeAll()
}

print("READY")
fflush(stdout)
while let line = readLine() {
    guard let data = line.data(using: .utf8),
          let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let type = event["type"] as? String else { continue }
    if let x = event["x"] as? Double, let y = event["y"] as? Double,
       x.isFinite, y.isFinite, (0...1).contains(x), (0...1).contains(y) {
        point = CGPoint(x: bounds.minX + min(x * bounds.width, bounds.width - 1),
                        y: bounds.minY + min(y * bounds.height, bounds.height - 1))
    }
    switch type {
    case "move":
        let move: CGEventType = heldButtons.contains(0) ? .leftMouseDragged : heldButtons.contains(2) ? .rightMouseDragged : .mouseMoved
        CGEvent(mouseEventSource: source, mouseType: move, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    case "button":
        guard let button = event["button"] as? Int, let down = event["down"] as? Bool, (0...2).contains(button) else { continue }
        let mouse: CGMouseButton = button == 2 ? .right : button == 1 ? .center : .left
        let kind: CGEventType = button == 2 ? (down ? .rightMouseDown : .rightMouseUp)
            : button == 1 ? (down ? .otherMouseDown : .otherMouseUp) : (down ? .leftMouseDown : .leftMouseUp)
        if down { heldButtons.insert(button) } else { heldButtons.remove(button) }
        CGEvent(mouseEventSource: source, mouseType: kind, mouseCursorPosition: point, mouseButton: mouse)?.post(tap: .cghidEventTap)
    case "key":
        guard let code = event["code"] as? String, let key = keys[code], let down = event["down"] as? Bool else { continue }
        if down { heldKeys.insert(key) } else { heldKeys.remove(key) }
        CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: down)?.post(tap: .cghidEventTap)
    case "text":
        guard let text = event["text"] as? String, text.count <= 256 else { continue }
        let characters = Array(text.utf16)
        for down in [true, false] {
            guard let input = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { continue }
            input.keyboardSetUnicodeString(stringLength: characters.count, unicodeString: characters)
            input.post(tap: .cghidEventTap)
        }
    case "scroll":
        guard let delta = event["dy"] as? Double, delta.isFinite, abs(delta) <= 1000 else { continue }
        CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 1, wheel1: Int32(-delta), wheel2: 0, wheel3: 0)?.post(tap: .cghidEventTap)
    case "release": releaseAll()
    default: break
    }
}
releaseAll()
