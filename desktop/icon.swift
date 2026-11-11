import Cocoa
let root = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
for size in [16,32,128,256,512] {
    for scale in [1,2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let p = CGFloat(pixels)
        NSColor(red:0.09,green:0.19,blue:0.15,alpha:1).setFill()
        NSBezierPath(roundedRect:NSRect(x:p*0.035,y:p*0.035,width:p*0.93,height:p*0.93),xRadius:p*0.2,yRadius:p*0.2).fill()
        let path = NSBezierPath()
        path.move(to:NSPoint(x:p*0.25,y:p*0.28));path.line(to:NSPoint(x:p*0.25,y:p*0.65))
        path.line(to:NSPoint(x:p*0.48,y:p*0.65));path.line(to:NSPoint(x:p*0.48,y:p*0.28))
        path.move(to:NSPoint(x:p*0.5,y:p*0.54));path.line(to:NSPoint(x:p*0.75,y:p*0.79))
        path.move(to:NSPoint(x:p*0.57,y:p*0.79));path.line(to:NSPoint(x:p*0.75,y:p*0.79));path.line(to:NSPoint(x:p*0.75,y:p*0.61))
        path.lineWidth=p*0.065;path.lineCapStyle = .square;path.lineJoinStyle = .miter
        NSColor(red:0.8,green:0.92,blue:0.64,alpha:1).setStroke();path.stroke()
        image.unlockFocus()
        let data=NSBitmapImageRep(data:image.tiffRepresentation!)!.representation(using:.png,properties:[:])!
        try data.write(to:root.appendingPathComponent("icon_\(size)x\(size)\(scale==2 ? "@2x" : "").png"))
    }
}
