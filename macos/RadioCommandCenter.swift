import AppKit
import Darwin
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler, NSToolbarDelegate {
    private struct ServiceSnapshot {
        let running: Bool
        let transcriptionEnabled: Bool
        let transcriptionRunning: Bool
        let sourcePath: String
        let dashboardURL: URL?
    }

    private var window: NSWindow!
    private var appHeader: NSVisualEffectView!
    private var appHeaderToggleButton: NSButton!
    private var appHeaderExpandedViews: [NSView] = []
    private var appHeaderExpanded = false
    private var statusLabel: NSTextField!
    private var sourceField: NSTextField!
    private var webView: WKWebView!
    private var chooseSourceButton: NSButton!
    private var transcriptionButton: NSButton!
    private var updateButton: NSButton!
    private var timer: Timer?
    private var dashboardURL: URL?
    private var dashboardLoadInProgress = false
    private var dashboardLoaded = false
    private var statusRefreshInFlight = false
    private var updateCheckInFlight = false
    private var automaticUpdateChecked = false
    private var transcriptionEnabled = true
    private var settingsRequested = false
    private var updateProgressPanel: NSPanel?
    private var menuStatusItem: NSStatusItem?
    private let collapsedHeaderHeight: CGFloat = 46
    private let expandedHeaderHeight: CGFloat = 104

    private var bundledRuntimeRoot: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let runtime = resources.appendingPathComponent("runtime", isDirectory: true)
        return FileManager.default.fileExists(
            atPath: runtime.appendingPathComponent("backend/server.py").path
        ) ? runtime : nil
    }

    private var projectRoot: URL {
        if let bundledRuntimeRoot {
            return bundledRuntimeRoot
        }
        return Bundle.main.bundleURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private var pythonURL: URL {
        if let resources = Bundle.main.resourceURL {
            let bundledPython = resources.appendingPathComponent(
                "python/Python.framework/Versions/3.12/bin/python3.12"
            )
            if FileManager.default.isExecutableFile(atPath: bundledPython.path) {
                return bundledPython
            }
        }
        return projectRoot.appendingPathComponent("venv/bin/python")
    }

    private var applicationDataURL: URL {
        FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0].appendingPathComponent("Radio Command Center", isDirectory: true)
    }

    private var applicationVersion: String {
        Bundle.main.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "0.0.0"
    }

    private var updateRepository: String {
        Bundle.main.object(
            forInfoDictionaryKey: "RCCGitHubRepository"
        ) as? String ?? "SequoyahGeber/Radio-Recording-Transcription"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 1220, height: 780))
        window = NSWindow(
            contentRect: content.bounds,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Radio Command Center"
        window.minSize = NSSize(width: 760, height: 560)
        window.center()
        window.contentView = content
        configureNativeToolbar()
        configureStatusItem()

        appHeader = NSVisualEffectView(
            frame: NSRect(
                x: 0,
                y: content.bounds.height - collapsedHeaderHeight,
                width: content.bounds.width,
                height: collapsedHeaderHeight
            )
        )
        appHeader.autoresizingMask = [.width, .minYMargin]
        appHeader.material = .headerView
        appHeader.blendingMode = .withinWindow
        content.addSubview(appHeader)

        let title = label("Radio Command Center", size: 16, bold: true)
        title.frame = NSRect(x: 20, y: 12, width: 210, height: 23)
        title.autoresizingMask = [.minYMargin]
        appHeader.addSubview(title)

        statusLabel = label("Checking services…", size: 11, bold: true)
        statusLabel.frame = NSRect(x: 238, y: 13, width: 220, height: 20)
        statusLabel.autoresizingMask = [.minYMargin]
        appHeader.addSubview(statusLabel)

        let sourceTitle = label("Recording folder", size: 10, bold: true)
        sourceTitle.textColor = .secondaryLabelColor
        sourceTitle.frame = NSRect(x: 22, y: 42, width: 120, height: 18)
        sourceTitle.autoresizingMask = [.maxYMargin]
        appHeader.addSubview(sourceTitle)

        sourceField = NSTextField(frame: NSRect(x: 22, y: 9, width: 590, height: 27))
        sourceField.autoresizingMask = [.width]
        sourceField.isEditable = false
        sourceField.placeholderString = "Choose a local or mounted network folder"
        sourceField.lineBreakMode = .byTruncatingMiddle
        appHeader.addSubview(sourceField)

        chooseSourceButton = button("Choose…", action: #selector(chooseSource))
        chooseSourceButton.frame = NSRect(x: 620, y: 8, width: 82, height: 30)
        chooseSourceButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(chooseSourceButton)

        let startButton = button("Launch", action: #selector(startServices))
        startButton.frame = NSRect(x: 724, y: 8, width: 78, height: 30)
        startButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(startButton)

        let restartButton = button("Restart", action: #selector(restartServices))
        restartButton.frame = NSRect(x: 808, y: 8, width: 78, height: 30)
        restartButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(restartButton)

        let stopButton = button("Stop", action: #selector(stopServices))
        stopButton.frame = NSRect(x: 892, y: 8, width: 70, height: 30)
        stopButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(stopButton)

        let reloadButton = button("Reload", action: #selector(reloadDashboard))
        reloadButton.frame = NSRect(x: 968, y: 8, width: 82, height: 30)
        reloadButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(reloadButton)

        updateButton = button("Updates", action: #selector(checkForUpdates))
        updateButton.frame = NSRect(x: 1056, y: 8, width: 92, height: 30)
        updateButton.autoresizingMask = [.minXMargin]
        appHeader.addSubview(updateButton)

        transcriptionButton = button(
            "Stop Transcription",
            action: #selector(toggleTranscription)
        )
        transcriptionButton.frame = NSRect(x: 850, y: 8, width: 190, height: 30)
        transcriptionButton.autoresizingMask = [.minXMargin, .minYMargin]
        appHeader.addSubview(transcriptionButton)

        appHeaderToggleButton = button("App Controls", action: #selector(toggleAppHeader))
        appHeaderToggleButton.frame = NSRect(x: 1068, y: 8, width: 132, height: 30)
        appHeaderToggleButton.autoresizingMask = [.minXMargin, .minYMargin]
        appHeaderToggleButton.isHidden = true
        appHeader.addSubview(appHeaderToggleButton)

        appHeaderExpandedViews = [
            sourceTitle,
            sourceField,
            chooseSourceButton,
            transcriptionButton,
            startButton,
            restartButton,
            stopButton,
            reloadButton,
            updateButton,
        ]
        appHeaderExpandedViews.forEach { $0.isHidden = true }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "profileAccess")
        configuration.userContentController.add(self, name: "nativeAction")
        webView = WKWebView(
            frame: NSRect(
                x: 0,
                y: 0,
                width: content.bounds.width,
                height: content.bounds.height - collapsedHeaderHeight
            ),
            configuration: configuration
        )
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        content.addSubview(webView, positioned: .below, relativeTo: appHeader)

        window.makeKeyAndOrderFront(nil)
        startServices()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
    }

    @objc private func toggleAppHeader() {
        setAppHeaderExpanded(!appHeaderExpanded, animated: true)
    }

    private func setAppHeaderExpanded(_ expanded: Bool, animated: Bool) {
        guard let content = window.contentView else { return }
        appHeaderExpanded = expanded
        appHeaderExpandedViews.forEach { $0.isHidden = !expanded }
        appHeaderToggleButton.title = expanded ? "Hide App Controls" : "App Controls"

        let height = expanded ? expandedHeaderHeight : collapsedHeaderHeight
        let headerFrame = NSRect(
            x: 0,
            y: content.bounds.height - height,
            width: content.bounds.width,
            height: height
        )
        let webFrame = NSRect(
            x: 0,
            y: 0,
            width: content.bounds.width,
            height: content.bounds.height - height
        )
        let topRowY = height - 34

        let changes = {
            self.appHeader.frame = headerFrame
            self.webView.frame = webFrame
            self.appHeader.subviews.first {
                ($0 as? NSTextField)?.stringValue == "Radio Command Center"
            }?.frame.origin.y = topRowY
            self.statusLabel.frame.origin.y = topRowY + 1
        }

        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.18
                context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                changes()
            }
        } else {
            changes()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }

    private func label(_ text: String, size: CGFloat, bold: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
        return field
    }

    private func button(_ title: String, action: Selector) -> NSButton {
        let control = NSButton(title: title, target: self, action: action)
        control.bezelStyle = .rounded
        return control
    }

    private func configureNativeToolbar() {
        let toolbar = NSToolbar(identifier: "RadioCommandCenterToolbar")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = true
        toolbar.autosavesConfiguration = true
        window.toolbar = toolbar
        window.toolbarStyle = .unified
    }

    private func configureStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Radio ○"
        item.button?.toolTip = "Radio Command Center status"
        let menu = NSMenu()
        menu.addItem(withTitle: "Show Radio Command Center", action: #selector(showMainWindow), keyEquivalent: "")
        menu.addItem(withTitle: "Copy Dashboard Address", action: #selector(copyDashboardAddress), keyEquivalent: "")
        menu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        menuStatusItem = item
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.toggleSidebar, .flexibleSpace, .init("status"), .init("source"), .init("restart"),
         .init("settings"), .init("copyAddress"), .init("reload"), .init("updates")]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.toggleSidebar, .init("status"), .flexibleSpace, .init("source"), .init("restart"),
         .init("settings"), .init("copyAddress"), .init("reload"), .init("updates")]
    }

    func toolbar(
        _ toolbar: NSToolbar,
        itemForItemIdentifier identifier: NSToolbarItem.Identifier,
        willBeInsertedIntoToolbar flag: Bool
    ) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: identifier)
        switch identifier.rawValue {
        case NSToolbarItem.Identifier.toggleSidebar.rawValue:
            item.label = "App Controls"
            item.toolTip = "Show or hide app controls"
            item.image = NSImage(systemSymbolName: "slider.horizontal.3", accessibilityDescription: "App controls")
            item.target = self
            item.action = #selector(toggleAppHeader)
        case "status":
            item.label = "Status"
            item.toolTip = "Open system health"
            item.image = NSImage(systemSymbolName: "waveform.path.ecg", accessibilityDescription: "System status")
            item.target = self
            item.action = #selector(openHealthCenter)
        case "source":
            item.label = "Recording Source"
            item.toolTip = "Choose the recording source folder"
            item.image = NSImage(systemSymbolName: "externaldrive", accessibilityDescription: "Choose recording source")
            item.target = self
            item.action = #selector(chooseSource)
        case "restart":
            item.label = "Restart"
            item.toolTip = "Restart Radio Command Center services"
            item.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Restart services")
            item.target = self
            item.action = #selector(restartServices)
        case "settings":
            item.label = "Settings"
            item.toolTip = "Open Radio Command Center settings"
            item.image = NSImage(systemSymbolName: "gearshape", accessibilityDescription: "Settings")
            item.target = self
            item.action = #selector(showSettings)
        case "copyAddress":
            item.label = "Copy Address"
            item.toolTip = "Copy the secure dashboard address"
            item.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: "Copy dashboard address")
            item.target = self
            item.action = #selector(copyDashboardAddress)
        case "reload":
            item.label = "Reload"
            item.toolTip = "Reload the dashboard"
            item.image = NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: "Reload dashboard")
            item.target = self
            item.action = #selector(reloadDashboard)
        case "updates":
            item.label = "Updates"
            item.toolTip = "Check for Radio Command Center updates"
            item.image = NSImage(systemSymbolName: "arrow.down.circle", accessibilityDescription: "Check for updates")
            item.target = self
            item.action = #selector(checkForUpdates)
        default:
            return nil
        }
        return item
    }

    @objc private func showMainWindow() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func copyDashboardAddress() {
        guard let value = dashboardURL?.absoluteString else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(value, forType: .string)
        statusLabel.stringValue = "Dashboard address copied"
    }

    @objc private func openHealthCenter() {
        guard let base = dashboardURL else { return }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)
        components?.fragment = "health-center"
        guard let healthURL = components?.url else { return }
        webView.load(URLRequest(url: healthURL))
        webView.evaluateJavaScript("document.getElementById('health-center-button')?.click()")
    }

    /// Settings live in the dashboard itself so the app, a browser on this Mac,
    /// and a browser on another device all edit exactly the same values.
    @objc private func showSettings() {
        showMainWindow()
        guard dashboardLoaded else {
            settingsRequested = true
            statusLabel.stringValue = "Starting services to open settings…"
            startServices()
            return
        }
        settingsRequested = false
        webView.evaluateJavaScript("window.radioOpenSettings && window.radioOpenSettings()") {
            [weak self] _, error in
            guard let self, error != nil else { return }
            self.statusLabel.stringValue = "Settings are available once the dashboard loads"
        }
    }

    private func chooseSourceFolderForDashboard() {
        let chooser = NSOpenPanel()
        chooser.title = "Choose Recording Source"
        chooser.message = "Choose the local folder or mounted share that contains radio recordings."
        chooser.prompt = "Use Recording Folder"
        chooser.canChooseFiles = false
        chooser.canChooseDirectories = true
        chooser.allowsMultipleSelection = false
        chooser.canCreateDirectories = false
        guard chooser.runModal() == .OK, let folder = chooser.url else { return }
        let encoded = String(
            data: (try? JSONSerialization.data(withJSONObject: [folder.path]))
                ?? Data("[\"\"]".utf8),
            encoding: .utf8
        ) ?? "[\"\"]"
        webView.evaluateJavaScript(
            "window.radioApplySourceFolder && window.radioApplySourceFolder(\(encoded)[0])"
        )
    }

    private func runControl(_ arguments: [String], completion: @escaping ([String: Any]?) -> Void) {
        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            statusLabel.stringValue = "Not installed yet — run the installer first"
            completion(nil)
            return
        }
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = [
            projectRoot.appendingPathComponent("scripts/service_control.py").path
        ] + arguments
        process.currentDirectoryURL = projectRoot
        var environment = ProcessInfo.processInfo.environment
        environment["RADIO_DATA_DIR"] = applicationDataURL.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if let resources = Bundle.main.resourceURL, bundledRuntimeRoot != nil {
            let pythonHome = resources.appendingPathComponent(
                "python/Python.framework/Versions/3.12"
            )
            let sitePackages = resources.appendingPathComponent(
                "runtime/site-packages"
            )
            environment["PYTHONHOME"] = pythonHome.path
            environment["PYTHONPATH"] = sitePackages.path
            environment["RADIO_MODEL_DIR"] =
                applicationDataURL.appendingPathComponent("models").path
            environment["HF_HOME"] =
                applicationDataURL.appendingPathComponent("models/hf-mlx").path
        }
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self] task in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            DispatchQueue.main.async {
                if task.terminationStatus != 0 {
                    self?.statusLabel.stringValue =
                        (value?["error"] as? String) ?? "Command failed"
                }
                completion(value)
            }
        }
        do {
            try process.run()
        } catch {
            statusLabel.stringValue = error.localizedDescription
            completion(nil)
        }
    }

    private func runUpdater(
        _ arguments: [String],
        completion: @escaping ([String: Any]?) -> Void
    ) {
        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            statusLabel.stringValue = "The bundled update runtime is unavailable"
            completion(nil)
            return
        }
        let updaterURL = projectRoot.appendingPathComponent("scripts/app_updater.py")
        guard FileManager.default.fileExists(atPath: updaterURL.path) else {
            statusLabel.stringValue = "The app updater is unavailable"
            completion(nil)
            return
        }

        let process = Process()
        process.executableURL = pythonURL
        process.arguments = [updaterURL.path] + arguments
        process.currentDirectoryURL = projectRoot
        var environment = ProcessInfo.processInfo.environment
        environment["RADIO_DATA_DIR"] = applicationDataURL.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if let resources = Bundle.main.resourceURL, bundledRuntimeRoot != nil {
            let pythonHome = resources.appendingPathComponent(
                "python/Python.framework/Versions/3.12"
            )
            let sitePackages = resources.appendingPathComponent(
                "runtime/site-packages"
            )
            environment["PYTHONHOME"] = pythonHome.path
            environment["PYTHONPATH"] = sitePackages.path
        }
        process.environment = environment

        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self] task in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            DispatchQueue.main.async {
                if task.terminationStatus != 0 {
                    self?.statusLabel.stringValue =
                        (value?["error"] as? String) ?? "Update command failed"
                }
                completion(value)
            }
        }
        do {
            try process.run()
        } catch {
            statusLabel.stringValue = error.localizedDescription
            completion(nil)
        }
    }

    private func showUpdateError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Radio Command Center could not update"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    /// A small window shown while the update downloads.
    ///
    /// The only previous feedback was the status line inside App Controls,
    /// which is collapsed by default: pressing Download & Install appeared to
    /// do nothing at all until the install prompt arrived a minute later.
    private func showUpdateProgress(_ message: String) {
        if updateProgressPanel == nil {
            let panel = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 420, height: 118),
                styleMask: [.titled, .hudWindow, .utilityWindow],
                backing: .buffered,
                defer: false
            )
            panel.title = "Software Update"
            panel.isReleasedWhenClosed = false
            panel.hidesOnDeactivate = false

            let spinner = NSProgressIndicator(
                frame: NSRect(x: 24, y: 46, width: 24, height: 24)
            )
            spinner.style = .spinning
            spinner.isIndeterminate = true
            spinner.startAnimation(nil)
            panel.contentView?.addSubview(spinner)

            let text = label("", size: 12)
            text.frame = NSRect(x: 60, y: 34, width: 336, height: 48)
            text.maximumNumberOfLines = 3
            text.lineBreakMode = .byWordWrapping
            text.identifier = NSUserInterfaceItemIdentifier("updateProgressText")
            panel.contentView?.addSubview(text)

            panel.center()
            updateProgressPanel = panel
        }
        (updateProgressPanel?.contentView?.subviews.first {
            $0.identifier?.rawValue == "updateProgressText"
        } as? NSTextField)?.stringValue = message
        updateProgressPanel?.makeKeyAndOrderFront(nil)
    }

    private func hideUpdateProgress() {
        updateProgressPanel?.orderOut(nil)
    }

    /// A fixed-size scrolling box for release notes, with the plainest of
    /// Markdown removed so headings and emphasis read as text rather than
    /// punctuation.
    private func releaseNotesView(_ notes: String) -> NSView {
        var lines: [String] = []
        for rawLine in notes.components(separatedBy: .newlines) {
            var line = rawLine
            while line.hasPrefix("#") { line.removeFirst() }
            line = line.replacingOccurrences(of: "**", with: "")
            line = line.replacingOccurrences(of: "`", with: "")
            lines.append(line.trimmingCharacters(in: .whitespaces))
        }
        let cleaned = lines.joined(separator: "\n")
            .replacingOccurrences(of: "\n\n\n", with: "\n\n")

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 420, height: 220))
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = false

        let textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: 6, height: 6)
        textView.font = .systemFont(ofSize: 11)
        textView.string = cleaned
        textView.textColor = .labelColor
        scroll.documentView = textView
        return scroll
    }

    @objc private func checkForUpdates() {
        performUpdateCheck(manual: true)
    }

    private func performUpdateCheck(manual: Bool) {
        guard !updateCheckInFlight else { return }
        updateCheckInFlight = true
        updateButton.isEnabled = false
        updateButton.title = "Checking…"
        statusLabel.stringValue = "Checking GitHub for updates…"
        if manual {
            showUpdateProgress("Checking for a newer version…")
        }

        runUpdater(
            [
                "check",
                "--current-version", applicationVersion,
                "--repository", updateRepository,
            ]
        ) { [weak self] value in
            guard let self else { return }
            self.updateCheckInFlight = false
            self.updateButton.isEnabled = true
            self.updateButton.title = "Updates"
            self.hideUpdateProgress()

            guard let value else {
                if manual {
                    self.showUpdateError("The update check could not be completed.")
                }
                self.refreshStatus()
                return
            }
            if let error = value["error"] as? String {
                if manual {
                    self.showUpdateError(error)
                }
                self.refreshStatus()
                return
            }

            let latestVersion = value["latest_version"] as? String ?? "unknown"
            guard value["status"] as? String == "available" else {
                self.statusLabel.stringValue =
                    "Radio Command Center \(self.applicationVersion) is current"
                if manual {
                    let alert = NSAlert()
                    alert.messageText = "You’re up to date"
                    alert.informativeText =
                        "Radio Command Center \(self.applicationVersion) is the newest installed version. The latest published GitHub release is \(latestVersion)."
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
                self.refreshStatus()
                return
            }

            let alert = NSAlert()
            alert.messageText = "Radio Command Center \(latestVersion) is available"
            alert.informativeText =
                "The update will be verified before installation. Recordings, transcripts, profiles, credentials, and settings remain in Application Support and will not be replaced."
            // Release notes go in a scrolling box of fixed height. Appending them
            // to the informative text grew the alert with the length of the
            // notes, and a long set pushed these buttons off the bottom of the
            // screen where they could not be reached or resized into view.
            let releaseNotes = (value["release_notes"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !releaseNotes.isEmpty {
                alert.accessoryView = self.releaseNotesView(releaseNotes)
            }
            alert.addButton(withTitle: "Download & Install")
            alert.addButton(withTitle: "Later")
            if alert.runModal() == .alertFirstButtonReturn {
                self.prepareUpdate()
            } else {
                self.refreshStatus()
            }
        }
    }

    private func prepareUpdate() {
        updateCheckInFlight = true
        updateButton.isEnabled = false
        updateButton.title = "Downloading…"
        statusLabel.stringValue = "Downloading and verifying the update…"
        showUpdateProgress(
            "Downloading Radio Command Center and checking it is genuine.\n"
                + "This takes a minute or two on a typical connection."
        )

        runUpdater(
            [
                "prepare",
                "--current-version", applicationVersion,
                "--repository", updateRepository,
                "--app-path", Bundle.main.bundleURL.path,
                "--data-dir", applicationDataURL.path,
            ]
        ) { [weak self] value in
            guard let self else { return }
            self.updateCheckInFlight = false
            self.updateButton.isEnabled = true
            self.updateButton.title = "Updates"
            self.hideUpdateProgress()

            guard
                let value,
                value["error"] == nil,
                let manifestPath = value["manifest_path"] as? String,
                let latestVersion = value["latest_version"] as? String
            else {
                self.showUpdateError(
                    (value?["error"] as? String)
                        ?? "The update could not be downloaded and verified."
                )
                self.refreshStatus()
                return
            }

            let alert = NSAlert()
            alert.messageText = "Ready to install \(latestVersion)"
            alert.informativeText =
                "Radio Command Center will briefly stop its services, save a recovery copy of the database, security profiles, and settings, replace only the app, and reopen automatically."
            alert.addButton(withTitle: "Restart & Install")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else {
                self.refreshStatus()
                return
            }
            self.installPreparedUpdate(manifestPath)
        }
    }

    private func installPreparedUpdate(_ manifestPath: String) {
        updateButton.isEnabled = false
        statusLabel.stringValue = "Stopping services for the update…"
        runControl(["stop"]) { [weak self] value in
            guard let self else { return }
            guard value != nil, value?["error"] == nil else {
                self.updateButton.isEnabled = true
                self.showUpdateError(
                    (value?["error"] as? String)
                        ?? "The services could not be stopped safely."
                )
                self.refreshStatus()
                return
            }
            self.statusLabel.stringValue = "Installing update and reopening…"
            self.runUpdater(
                [
                    "launch",
                    "--manifest", manifestPath,
                    "--current-pid", String(ProcessInfo.processInfo.processIdentifier),
                ]
            ) { [weak self] launchValue in
                guard let self else { return }
                guard launchValue?["status"] as? String == "installing" else {
                    self.updateButton.isEnabled = true
                    self.showUpdateError(
                        (launchValue?["error"] as? String)
                            ?? "The prepared update could not be started."
                    )
                    self.startServices()
                    return
                }
                NSApp.terminate(nil)
            }
        }
    }

    private func refreshStatus() {
        guard !statusRefreshInFlight else { return }
        statusRefreshInFlight = true

        let dataRoot = applicationDataURL
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let settingsURL = dataRoot.appendingPathComponent("settings.json")
            let settings: [String: Any]
            if
                let data = try? Data(contentsOf: settingsURL),
                let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            {
                settings = value
            } else {
                settings = [:]
            }

            let runtimePath = (settings["runtime_dir"] as? String)
                ?? dataRoot.appendingPathComponent("runtime").path
            let pidURL = URL(fileURLWithPath: runtimePath, isDirectory: true)
                .appendingPathComponent("supervisor.pid")
            let pid = (try? String(contentsOf: pidURL, encoding: .utf8))
                .flatMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
            let running: Bool
            if let pid, pid > 0 {
                running = Darwin.kill(pid_t(pid), 0) == 0 || errno == EPERM
            } else {
                running = false
            }

            let host = settings["host"] as? String ?? "127.0.0.1"
            let port = settings["port"] as? Int ?? 8000
            let dashboardHost = ["0.0.0.0", "::"].contains(host) ? "127.0.0.1" : host
            let snapshot = ServiceSnapshot(
                running: running,
                transcriptionEnabled: settings["transcription_enabled"] as? Bool ?? true,
                transcriptionRunning: {
                    guard running, let supervisorPID = pid else { return false }
                    let statusURL = URL(fileURLWithPath: runtimePath, isDirectory: true)
                        .appendingPathComponent("service-status.json")
                    guard
                        let data = try? Data(contentsOf: statusURL),
                        let value = try? JSONSerialization.jsonObject(with: data)
                            as? [String: Any],
                        (value["supervisor_pid"] as? Int) == Int(supervisorPID),
                        let processes = value["processes"] as? [String: Any],
                        let worker = processes["worker"] as? [String: Any]
                    else { return false }
                    return worker["running"] as? Bool ?? false
                }(),
                sourcePath: settings["source_dir"] as? String ?? "/Volumes/Active Recording",
                dashboardURL: URL(string: "https://\(dashboardHost):\(port)")
            )

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.statusRefreshInFlight = false
                self.transcriptionEnabled = snapshot.transcriptionEnabled
                self.transcriptionButton.title = snapshot.transcriptionEnabled
                    ? "Stop Transcription"
                    : "Start Transcription"
                self.statusLabel.stringValue = snapshot.running
                    ? (
                        snapshot.transcriptionRunning
                            ? "● Dashboard on · Transcribing"
                            : "● Dashboard on · Transcription off"
                    )
                    : "○ Services stopped"
                self.statusLabel.textColor = snapshot.running ? .systemGreen : .secondaryLabelColor
                self.menuStatusItem?.button?.title = snapshot.running
                    ? (snapshot.transcriptionRunning ? "Radio ●" : "Radio ◐")
                    : "Radio ○"
                self.menuStatusItem?.button?.toolTip = self.statusLabel.stringValue
                self.sourceField.stringValue = snapshot.sourcePath
                self.dashboardURL = snapshot.dashboardURL

                if snapshot.running {
                    self.loadDashboardIfNeeded()
                } else {
                    self.dashboardLoaded = false
                    self.dashboardLoadInProgress = false
                    self.showServicePlaceholder()
                }
            }
        }
    }

    /// The app's private sign-in key, stored beside the dashboard's own
    /// credentials. Only this macOS account can read the file, which is what
    /// lets the app skip the login screen while browsers still sign in.
    private func applicationSignInKey() -> String? {
        let settingsURL = applicationDataURL.appendingPathComponent("settings.json")
        var securityDirectory = applicationDataURL.appendingPathComponent(
            "security",
            isDirectory: true
        )
        if
            let data = try? Data(contentsOf: settingsURL),
            let settings = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let configured = settings["security_dir"] as? String,
            !configured.isEmpty
        {
            securityDirectory = URL(fileURLWithPath: configured, isDirectory: true)
        }
        let securityURL = securityDirectory.appendingPathComponent("security.json")
        guard
            let data = try? Data(contentsOf: securityURL),
            let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let key = value["app_key"] as? String,
            !key.isEmpty
        else { return nil }
        return key
    }

    private func installSignInKey(for url: URL, completion: @escaping () -> Void) {
        guard let host = url.host, let key = applicationSignInKey() else {
            completion()
            return
        }
        var properties: [HTTPCookiePropertyKey: Any] = [
            .domain: host,
            .path: "/",
            .name: "radio_app_key",
            .value: key,
            .secure: "TRUE",
            .expires: Date(timeIntervalSinceNow: 60 * 60 * 24 * 3650),
        ]
        if #available(macOS 10.15, *) {
            properties[.sameSitePolicy] = HTTPCookieStringPolicy.sameSiteStrict
        }
        guard let cookie = HTTPCookie(properties: properties) else {
            completion()
            return
        }
        webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
            completion()
        }
    }

    private func loadDashboardIfNeeded(force: Bool = false) {
        guard let dashboardURL else { return }
        if !force && (dashboardLoaded || dashboardLoadInProgress) { return }
        dashboardLoadInProgress = true
        installSignInKey(for: dashboardURL) { [weak self] in
            guard let self else { return }
            self.webView.load(
                URLRequest(url: dashboardURL, cachePolicy: .reloadIgnoringLocalCacheData)
            )
        }
    }

    private func showServicePlaceholder() {
        let html = """
        <!doctype html>
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            body { margin: 0; min-height: 100vh; display: grid; place-items: center;
                   background: #090a0f; color: #94a3b8;
                   font: 15px -apple-system, BlinkMacSystemFont, sans-serif; }
            div { text-align: center; }
            strong { color: #f8fafc; display: block; font-size: 18px; margin-bottom: 8px; }
          </style>
        </head>
        <body><div><strong>Dashboard is waiting</strong>Launch the services to connect.</div></body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    @objc private func chooseSource() {
        let chooser = NSOpenPanel()
        chooser.title = "Choose Recording Source"
        chooser.message = "Choose the local folder or mounted share that contains radio recordings."
        chooser.prompt = "Use Recording Folder"
        chooser.canChooseFiles = false
        chooser.canChooseDirectories = true
        chooser.allowsMultipleSelection = false
        chooser.canCreateDirectories = false
        if chooser.runModal() == .OK, let folder = chooser.url {
            guard FileManager.default.isReadableFile(atPath: folder.path) else {
                let alert = NSAlert()
                alert.alertStyle = .warning
                alert.messageText = "Recording folder is not readable"
                alert.informativeText = "Reconnect the drive or choose a folder this Mac can read."
                alert.runModal()
                return
            }
            sourceField.stringValue = folder.path
            statusLabel.stringValue = "Validating and saving recording folder…"
            runControl(["configure", "--source", folder.path]) { [weak self] value in
                self?.statusLabel.stringValue = value == nil
                    ? "Recording folder could not be saved"
                    : "Folder validated — restart to apply"
            }
        }
    }

    @objc private func startServices() {
        statusLabel.stringValue = "Starting services…"
        runControl(["start"]) { [weak self] value in
            guard let self else { return }
            guard value != nil else {
                // Recovery controls remain available if automatic startup
                // fails before an administrator can sign in.
                self.appHeaderToggleButton.isHidden = false
                return
            }
            self.dashboardLoaded = false
            self.dashboardLoadInProgress = false
            self.refreshStatus()
        }
    }

    @objc private func restartServices() {
        if transcriptionEnabled {
            let alert = NSAlert()
            alert.messageText = "Restart Radio Command Center?"
            alert.informativeText = "Active transcription will pause briefly. Durable queued work resumes automatically."
            alert.addButton(withTitle: "Restart")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        statusLabel.stringValue = "Restarting services…"
        dashboardLoaded = false
        dashboardLoadInProgress = false
        runControl(["restart"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func stopServices() {
        if transcriptionEnabled {
            let alert = NSAlert()
            alert.messageText = "Stop Radio Command Center?"
            alert.informativeText = "Transcription and sync will stop. Queued jobs remain saved for the next launch."
            alert.addButton(withTitle: "Stop")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        statusLabel.stringValue = "Stopping services…"
        runControl(["stop"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func toggleTranscription() {
        let enable = !transcriptionEnabled
        transcriptionButton.isEnabled = false
        statusLabel.stringValue = enable
            ? "Preparing Medium model… first use downloads it once"
            : "Stopping transcription…"
        let action = enable ? "transcription-start" : "transcription-stop"
        runControl([action]) { [weak self] value in
            guard let self else { return }
            self.transcriptionButton.isEnabled = true
            if value != nil {
                self.transcriptionEnabled = enable
            }
            self.refreshStatus()
        }
    }

    @objc private func reloadDashboard() {
        dashboardLoaded = false
        dashboardLoadInProgress = false
        loadDashboardIfNeeded(force: true)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        dashboardLoadInProgress = false
        dashboardLoaded = webView.url?.host == dashboardURL?.host
        if webView.url?.path == "/login" {
            appHeaderToggleButton.isHidden = true
            setAppHeaderExpanded(false, animated: false)
        }
        if settingsRequested && dashboardLoaded && webView.url?.path == "/" {
            showSettings()
        }
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        if message.name == "nativeAction" {
            guard
                message.frameInfo.isMainFrame,
                let payload = message.body as? [String: Any],
                payload["action"] as? String == "chooseSourceFolder"
            else { return }
            chooseSourceFolderForDashboard()
            return
        }
        guard
            message.name == "profileAccess",
            message.frameInfo.isMainFrame,
            let payload = message.body as? [String: Any],
            let role = payload["role"] as? String
        else { return }

        let isAdministrator = role == "admin"
        appHeaderToggleButton.isHidden = !isAdministrator
        if !isAdministrator {
            setAppHeaderExpanded(false, animated: false)
        } else if !automaticUpdateChecked {
            automaticUpdateChecked = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.performUpdateCheck(manual: false)
            }
        }
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        dashboardLoadInProgress = false
        dashboardLoaded = false
        statusLabel.stringValue = "Services are starting — dashboard will retry"
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        dashboardLoadInProgress = false
        dashboardLoaded = false
        statusLabel.stringValue = "Waiting for the secure dashboard…"
    }

    func webView(
        _ webView: WKWebView,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let protectionSpace = challenge.protectionSpace
        if protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           protectionSpace.host == dashboardURL?.host,
           let trust = protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
            return
        }
        completionHandler(.performDefaultHandling, nil)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        // Anything outside the local dashboard opens in the user's browser, so
        // the window holding the app's sign-in key only ever shows the dashboard.
        if let requestURL = navigationAction.request.url {
            if requestURL.host == dashboardURL?.host {
                webView.load(URLRequest(url: requestURL))
            } else {
                NSWorkspace.shared.open(requestURL)
            }
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let requestURL = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        let scheme = requestURL.scheme?.lowercased() ?? ""
        if scheme == "about" || requestURL.host == dashboardURL?.host {
            decisionHandler(.allow)
            return
        }
        if scheme == "http" || scheme == "https" {
            NSWorkspace.shared.open(requestURL)
        }
        decisionHandler(.cancel)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.activate(ignoringOtherApps: true)
application.run()
