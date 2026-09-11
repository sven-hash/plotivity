import AppKit
// Draws a runner emoji on a rounded orange-to-red square and writes icon_1024.png
let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let rect = NSRect(x: 0, y: 0, width: size, height: size)
let path = NSBezierPath(roundedRect: rect.insetBy(dx: 60, dy: 60), xRadius: 220, yRadius: 220)
let grad = NSGradient(starting: NSColor(red: 1.0, green: 0.55, blue: 0.2, alpha: 1), ending: NSColor(red: 0.75, green: 0.12, blue: 0.1, alpha: 1))!
grad.draw(in: path, angle: -70)
let text = "🏃" as NSString
let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 640)]
let ts = text.size(withAttributes: attrs)
text.draw(at: NSPoint(x: (size - ts.width) / 2, y: (size - ts.height) / 2 + 20), withAttributes: attrs)
img.unlockFocus()
let tiff = img.tiffRepresentation!
let png = NSBitmapImageRep(data: tiff)!.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
