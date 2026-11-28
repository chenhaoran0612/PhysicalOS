import Cocoa
let root = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
for size in [16,32,128,256,512] {
    for scale in [1,2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let p = CGFloat(pixels)
        // Issue #72: the mark of web/icons.css (N whose last stroke leaves as an arrow), on a 64 grid, y down.
        let mark: [[(Double, Double)]] = [[(18,45),(18,19),(36,45),(36,29),(46,19)],[(37,19),(46,19),(46,28)]]
        NSColor(red:0.090,green:0.169,blue:0.153,alpha:1).setFill()
        NSBezierPath(roundedRect:NSRect(x:p*0.035,y:p*0.035,width:p*0.93,height:p*0.93),xRadius:p*0.21,yRadius:p*0.21).fill()
        let unit = p*0.93/64, origin = p*0.035
        let path = NSBezierPath()
        for stroke in mark {
            for (index, point) in stroke.enumerated() {
                let at = NSPoint(x:origin+CGFloat(point.0)*unit,y:origin+CGFloat(64-point.1)*unit)
                if index == 0 { path.move(to:at) } else { path.line(to:at) }
            }
        }
        path.lineWidth=unit*5.5;path.lineCapStyle = .round;path.lineJoinStyle = .round
        NSColor(red:0.851,green:0.941,blue:0.702,alpha:1).setStroke();path.stroke()
        image.unlockFocus()
        let data=NSBitmapImageRep(data:image.tiffRepresentation!)!.representation(using:.png,properties:[:])!
        try data.write(to:root.appendingPathComponent("icon_\(size)x\(size)\(scale==2 ? "@2x" : "").png"))
    }
}
