import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var localURL: URL?
    var serviceURL: URL?
    var outputBuffer = ""
    var closing = false
    var ready = false
    var loading: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        // Native folder picker for "从文件夹安装" (web/sim.js posts to nxPickFolder).
        configuration.userContentController.add(self, name: "nxPickFolder")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1390, height: 930), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "NeuxMind Robot OS"
        window.minSize = NSSize(width: 900, height: 640)
        window.center()
        window.contentView = webView
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.96, green: 0.97, blue: 0.95, alpha: 1)
        loading = NSTextField(labelWithString: "正在启动 NeuxMind Robot OS…")
        loading.textColor = .secondaryLabelColor
        loading.font = .systemFont(ofSize: 17)
        loading.translatesAutoresizingMaskIntoConstraints = false
        webView.addSubview(loading)
        NSLayoutConstraint.activate([loading.centerXAnchor.constraint(equalTo: webView.centerXAnchor), loading.centerYAnchor.constraint(equalTo: webView.centerYAnchor)])
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        startServer()
    }

    func buildMenu() {
        let bar = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 NeuxMind Robot OS", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "连接平台服务…", action: #selector(configurePlatform), keyEquivalent: ",")
        appMenu.addItem(withTitle: "使用本机工作空间", action: #selector(useLocal), keyEquivalent: "")
        appMenu.addItem(withTitle: "开源许可…", action: #selector(openLicenses), keyEquivalent: "")
        appMenu.addItem(withTitle: "打开数据文件夹", action: #selector(openData), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 NeuxMind Robot OS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        bar.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        for (title, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        editItem.submenu = edit; bar.addItem(editItem)
        let viewItem = NSMenuItem()
        let view = NSMenu(title: "视图")
        view.addItem(withTitle: "刷新", action: #selector(reload), keyEquivalent: "r")
        view.addItem(withTitle: "在浏览器中打开", action: #selector(openBrowser), keyEquivalent: "")
        viewItem.submenu = view; bar.addItem(viewItem)
        NSApplication.shared.mainMenu = bar
        for item in appMenu.items where item.action != #selector(NSApplication.terminate(_:)) { item.target = self }
        for item in view.items { item.target = self }
    }

    @objc func openLicenses() {
        if let url = Bundle.main.resourceURL?.appendingPathComponent("licenses") { NSWorkspace.shared.open(url) }
    }

    var dataDirectory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("NeuxMind Robot OS", isDirectory: true)
    }

    func startServer() {
        guard let resources = Bundle.main.resourceURL else { showError("无法读取应用资源。"); return }
        do {
            try FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
            let process = Process()
            process.executableURL = resources.appendingPathComponent("backend/brainos-server")
            process.arguments = ["--port", "0", "--state-dir", dataDirectory.appendingPathComponent("state").path]
            let pipe = Pipe()
            process.standardOutput = pipe
            let logURL = dataDirectory.appendingPathComponent("runtime.log")
            if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
            let log = try FileHandle(forWritingTo: logURL)
            try log.seekToEnd()
            process.standardError = log
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.outputBuffer += text
                    if !self.ready, let range = self.outputBuffer.range(of: "http://127.0.0.1:"), let raw = self.outputBuffer[range.lowerBound...].split(whereSeparator: { $0.isWhitespace }).first, let url = URL(string: String(raw)) {
                        self.ready = true; self.localURL = url
                        let saved = UserDefaults.standard.string(forKey: "platformURL")
                        self.serviceURL = saved.flatMap(URL.init(string:)) ?? url
                        self.loading.removeFromSuperview()
                        self.webView.load(URLRequest(url: self.serviceURL!))
                    }
                }
            }
            process.terminationHandler = { [weak self] process in
                DispatchQueue.main.async {
                    guard let self = self, !self.closing else { return }
                    self.showError("本地服务已退出（\(process.terminationStatus)）。请重新打开应用。日志保存在应用数据文件夹。")
                }
            }
            self.server = process
            try process.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
                guard let self = self, !self.ready, !self.closing else { return }
                self.showError("本地服务尚未就绪。请检查 runtime.log 后重新打开应用。")
            }
        } catch { showError("启动失败：\(error.localizedDescription)") }
    }

    func showError(_ message: String) {
        let alert = NSAlert(); alert.messageText = "NeuxMind Robot OS"; alert.informativeText = message
        alert.addButton(withTitle: "确定"); alert.runModal()
    }
    @objc func reload() { webView.reload() }
    @objc func openBrowser() { if let url = serviceURL { NSWorkspace.shared.open(url) } }
    @objc func openData() { NSWorkspace.shared.open(dataDirectory) }
    @objc func showAbout() {
        let alert = NSAlert(); alert.messageText = "NeuxMind Robot OS 0.16.0"
        alert.informativeText = "机器人开发与交付平台\n模型 · Agent · 训练 · 仿真 · 部署 · 用量\n\nmacOS Apple Silicon 开发预览版\n平台资源和生产模型由运营端配置。"
        alert.runModal()
    }
    @objc func useLocal() {
        UserDefaults.standard.removeObject(forKey: "platformURL")
        if let url = localURL { serviceURL = url; webView.load(URLRequest(url: url)) }
    }
    @objc func configurePlatform() {
        let alert = NSAlert(); alert.messageText = "连接 NeuxMind 平台服务"
        alert.informativeText = "填写运营方提供的 HTTPS 工作台地址。账号、托管模型、资源与账单由该平台服务管理。本机工作空间保留。"
        let text = NSTextField(frame: NSRect(x: 0, y: 0, width: 380, height: 26))
        text.placeholderString = "https://platform.example.com"
        text.stringValue = UserDefaults.standard.string(forKey: "platformURL") ?? ""
        alert.accessoryView = text; alert.addButton(withTitle: "连接"); alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            guard let url = URL(string: text.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)), url.scheme == "https", url.host != nil, url.user == nil, url.password == nil else { showError("请输入有效的 HTTPS 平台地址。"); return }
            UserDefaults.standard.set(url.absoluteString, forKey: "platformURL")
            serviceURL = url; webView.load(URLRequest(url: url))
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        closing = true
        if let server = server, server.isRunning { server.terminate() }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "blob" || (url.host == serviceURL?.host && url.port == serviceURL?.port) { decisionHandler(.allow) }
        else if url.scheme == "https" || url.scheme == "http" { NSWorkspace.shared.open(url); decisionHandler(.cancel) }
        else { decisionHandler(.cancel) }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        // Only the local console may open the picker; the chosen path goes back to that page only.
        guard message.name == "nxPickFolder", let local = localURL else { return }
        let origin = message.frameInfo.securityOrigin
        guard origin.host == local.host, origin.port == (local.port ?? 0) else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "选择"
        panel.message = "选择包含 skill.json 的技能文件夹"
        panel.beginSheetModal(for: window) { [weak self] result in
            guard result == .OK, let url = panel.url, let self = self else { return }
            guard let data = try? JSONSerialization.data(withJSONObject: [url.path]), let text = String(data: data, encoding: .utf8) else { return }
            self.webView.evaluateJavaScript("window.nxFolderPicked && window.nxFolderPicked(\(text)[0])", completionHandler: nil)
        }
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = suggestedFilename
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.url : nil) }
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = AppDelegate()
application.delegate = delegate
application.run()
