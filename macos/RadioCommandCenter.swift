import AppKit
import Darwin
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler, NSTableViewDataSource, NSTableViewDelegate {
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
    private var sourcePanel: NSPanel?
    private var folderPathField: NSTextField!
    private var folderStatusField: NSTextField!
    private var folderTable: NSTableView!
    private var folderUseButton: NSButton!
    private var folderSavedButton: NSButton!
    private var folderEntries: [URL] = []
    private var folderCurrentURL: URL?
    private var folderLoadToken = UUID()
    private var timer: Timer?
    private var dashboardURL: URL?
    private var dashboardLoadInProgress = false
    private var dashboardLoaded = false
    private var statusRefreshInFlight = false
    private var updateCheckInFlight = false
    private var automaticUpdateChecked = false
    private var transcriptionEnabled = true
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
        window.minSize = NSSize(width: 1040, height: 650)
        window.center()
        window.contentView = content

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

    @objc private func checkForUpdates() {
        performUpdateCheck(manual: true)
    }

    private func performUpdateCheck(manual: Bool) {
        guard !updateCheckInFlight else { return }
        updateCheckInFlight = true
        updateButton.isEnabled = false
        updateButton.title = "Checking…"
        statusLabel.stringValue = "Checking GitHub for updates…"

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
            let releaseNotes = (value["release_notes"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let notes = releaseNotes?.isEmpty == false
                ? "\n\n\(releaseNotes!.prefix(1800))"
                : ""
            alert.informativeText =
                "The update will be verified before installation. Recordings, transcripts, profiles, credentials, and settings remain in Application Support and will not be replaced.\(notes)"
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
                self.sourceField.stringValue = snapshot.sourcePath
                self.folderSavedButton?.isEnabled = !snapshot.sourcePath.isEmpty
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

    private func loadDashboardIfNeeded(force: Bool = false) {
        guard let dashboardURL else { return }
        if !force && (dashboardLoaded || dashboardLoadInProgress) { return }
        dashboardLoadInProgress = true
        webView.load(URLRequest(url: dashboardURL, cachePolicy: .reloadIgnoringLocalCacheData))
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
        guard sourcePanel == nil else {
            sourcePanel?.makeKeyAndOrderFront(nil)
            return
        }

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 540),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        panel.title = "Choose Recording Folder"
        panel.isReleasedWhenClosed = false

        let content = NSView(frame: panel.contentView?.bounds ?? .zero)
        content.autoresizingMask = [.width, .height]
        panel.contentView = content

        let title = label("Choose the folder that contains the radio recordings", size: 17, bold: true)
        title.frame = NSRect(x: 20, y: 497, width: 680, height: 24)
        content.addSubview(title)

        let pathLabel = label("Folder path", size: 10, bold: true)
        pathLabel.textColor = .secondaryLabelColor
        pathLabel.frame = NSRect(x: 20, y: 467, width: 90, height: 18)
        content.addSubview(pathLabel)

        folderPathField = NSTextField(frame: NSRect(x: 20, y: 432, width: 620, height: 28))
        folderPathField.placeholderString = "Enter or paste a folder path"
        folderPathField.target = self
        folderPathField.action = #selector(goToTypedFolder)
        content.addSubview(folderPathField)

        let goButton = button("Go", action: #selector(goToTypedFolder))
        goButton.frame = NSRect(x: 648, y: 431, width: 54, height: 30)
        content.addSubview(goButton)

        let upButton = button("Up", action: #selector(goToParentFolder))
        upButton.frame = NSRect(x: 20, y: 394, width: 58, height: 30)
        content.addSubview(upButton)

        let homeButton = button("Home", action: #selector(goToHomeFolder))
        homeButton.frame = NSRect(x: 84, y: 394, width: 66, height: 30)
        content.addSubview(homeButton)

        let volumesButton = button("Drives", action: #selector(goToVolumesFolder))
        volumesButton.frame = NSRect(x: 156, y: 394, width: 72, height: 30)
        content.addSubview(volumesButton)

        folderSavedButton = button("Saved Folder", action: #selector(goToSavedFolder))
        folderSavedButton.frame = NSRect(x: 234, y: 394, width: 104, height: 30)
        folderSavedButton.isEnabled = !sourceField.stringValue.isEmpty
        content.addSubview(folderSavedButton)

        let refreshButton = button("Refresh", action: #selector(refreshFolderList))
        refreshButton.frame = NSRect(x: 344, y: 394, width: 78, height: 30)
        content.addSubview(refreshButton)

        let openButton = button("Open Selected", action: #selector(openSelectedFolder))
        openButton.frame = NSRect(x: 564, y: 394, width: 138, height: 30)
        content.addSubview(openButton)

        folderTable = NSTableView(frame: NSRect(x: 0, y: 0, width: 680, height: 308))
        let folderColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("folder"))
        folderColumn.title = "Folders"
        folderColumn.width = 660
        folderTable.addTableColumn(folderColumn)
        folderTable.headerView = nil
        folderTable.rowHeight = 27
        folderTable.dataSource = self
        folderTable.delegate = self
        folderTable.target = self
        folderTable.doubleAction = #selector(openSelectedFolder)

        let scrollView = NSScrollView(frame: NSRect(x: 20, y: 78, width: 682, height: 308))
        scrollView.borderType = .bezelBorder
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.documentView = folderTable
        content.addSubview(scrollView)

        folderStatusField = label("Loading folders…", size: 11)
        folderStatusField.textColor = .secondaryLabelColor
        folderStatusField.lineBreakMode = .byTruncatingMiddle
        folderStatusField.frame = NSRect(x: 22, y: 50, width: 678, height: 18)
        content.addSubview(folderStatusField)

        let cancelButton = button("Cancel", action: #selector(cancelFolderSelection))
        cancelButton.frame = NSRect(x: 492, y: 12, width: 96, height: 32)
        cancelButton.keyEquivalent = "\u{1b}"
        content.addSubview(cancelButton)

        folderUseButton = button("Use This Folder", action: #selector(useFolderSelection))
        folderUseButton.frame = NSRect(x: 594, y: 12, width: 108, height: 32)
        folderUseButton.keyEquivalent = "\r"
        folderUseButton.isEnabled = false
        content.addSubview(folderUseButton)

        sourcePanel = panel
        chooseSourceButton.isEnabled = false
        window.beginSheet(panel)
        loadFolder(FileManager.default.homeDirectoryForCurrentUser)
    }

    private func loadFolder(_ url: URL) {
        guard sourcePanel != nil else { return }

        let requestedURL = url.standardizedFileURL
        let token = UUID()
        folderLoadToken = token
        folderCurrentURL = requestedURL
        folderPathField.stringValue = requestedURL.path
        folderStatusField.stringValue = "Loading folders…"
        folderStatusField.textColor = .secondaryLabelColor
        folderUseButton.isEnabled = false
        folderUseButton.title = "Use This Folder"
        folderEntries = []
        folderTable.reloadData()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result: Result<[URL], Error>
            do {
                let values: [URLResourceKey] = [.isDirectoryKey, .isHiddenKey]
                let children = try FileManager.default.contentsOfDirectory(
                    at: requestedURL,
                    includingPropertiesForKeys: values,
                    options: [.skipsHiddenFiles]
                )
                // The entries directly under /Volumes are mount points. Asking
                // each one for resource values can block on a disconnected
                // network share before the user has even chosen it.
                let directories = (requestedURL.path == "/Volumes" ? children : children.filter { child in
                    (try? child.resourceValues(forKeys: Set(values)).isDirectory) == true
                }).sorted {
                    $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending
                }
                result = .success(directories)
            } catch {
                result = .failure(error)
            }

            DispatchQueue.main.async { [weak self] in
                guard let self, self.sourcePanel != nil, self.folderLoadToken == token else { return }
                switch result {
                case .success(let directories):
                    self.folderEntries = directories
                    self.folderTable.reloadData()
                    self.folderUseButton.isEnabled = true
                    self.folderStatusField.stringValue = directories.isEmpty
                        ? "This folder has no subfolders. You can still use it."
                        : "\(directories.count) folders — double-click one to open it."
                    self.folderStatusField.textColor = .secondaryLabelColor
                case .failure(let error):
                    self.folderEntries = []
                    self.folderTable.reloadData()
                    self.folderStatusField.stringValue = "Could not open this folder: \(error.localizedDescription)"
                    self.folderStatusField.textColor = .systemRed
                }
            }
        }
    }

    private func selectedFolder() -> URL? {
        let row = folderTable.selectedRow
        guard row >= 0, row < folderEntries.count else { return nil }
        return folderEntries[row]
    }

    private func closeFolderSheet(using folder: URL?) {
        guard let panel = sourcePanel else { return }
        folderLoadToken = UUID()
        window.endSheet(panel)
        panel.orderOut(nil)
        sourcePanel = nil
        chooseSourceButton.isEnabled = true

        guard let folder else { return }
        sourceField.stringValue = folder.path
        statusLabel.stringValue = "Saving recording folder…"
        runControl(["configure", "--source", folder.path]) { [weak self] value in
            guard value != nil else { return }
            self?.statusLabel.stringValue = "Folder saved — restart to apply"
        }
    }

    @objc private func goToTypedFolder() {
        let expandedPath = NSString(string: folderPathField.stringValue)
            .expandingTildeInPath
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !expandedPath.isEmpty else { return }
        loadFolder(URL(fileURLWithPath: expandedPath, isDirectory: true))
    }

    @objc private func goToParentFolder() {
        guard let current = folderCurrentURL else { return }
        let parent = current.deletingLastPathComponent()
        guard parent.path != current.path else { return }
        loadFolder(parent)
    }

    @objc private func goToHomeFolder() {
        loadFolder(FileManager.default.homeDirectoryForCurrentUser)
    }

    @objc private func goToVolumesFolder() {
        loadFolder(URL(fileURLWithPath: "/Volumes", isDirectory: true))
    }

    @objc private func goToSavedFolder() {
        let savedPath = sourceField.stringValue
        guard !savedPath.isEmpty else { return }
        loadFolder(URL(fileURLWithPath: savedPath, isDirectory: true))
    }

    @objc private func refreshFolderList() {
        guard let current = folderCurrentURL else { return }
        loadFolder(current)
    }

    @objc private func openSelectedFolder() {
        guard let selected = selectedFolder() else { return }
        loadFolder(selected)
    }

    @objc private func cancelFolderSelection() {
        closeFolderSheet(using: nil)
    }

    @objc private func useFolderSelection() {
        guard folderUseButton.isEnabled else { return }
        closeFolderSheet(using: selectedFolder() ?? folderCurrentURL)
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        folderEntries.count
    }

    func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        let identifier = NSUserInterfaceItemIdentifier("FolderCell")
        let field: NSTextField
        if let reusable = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTextField {
            field = reusable
        } else {
            field = NSTextField(labelWithString: "")
            field.identifier = identifier
            field.lineBreakMode = .byTruncatingMiddle
            field.font = .systemFont(ofSize: 13)
        }
        field.stringValue = "📁  \(folderEntries[row].lastPathComponent)"
        return field
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let selected = selectedFolder() else {
            folderUseButton.title = "Use This Folder"
            return
        }
        folderUseButton.title = "Use Selected"
        folderStatusField.textColor = .secondaryLabelColor
        folderStatusField.stringValue = "Selected: \(selected.path)"
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
        statusLabel.stringValue = "Restarting services…"
        dashboardLoaded = false
        dashboardLoadInProgress = false
        runControl(["restart"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func stopServices() {
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
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
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
        if let requestURL = navigationAction.request.url {
            webView.load(URLRequest(url: requestURL))
        }
        return nil
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.activate(ignoringOtherApps: true)
application.run()
