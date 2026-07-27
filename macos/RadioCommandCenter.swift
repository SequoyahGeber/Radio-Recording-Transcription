import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var statusLabel: NSTextField!
    private var sourceField: NSTextField!
    private var timer: Timer?

    private var projectRoot: URL {
        Bundle.main.bundleURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private var pythonURL: URL {
        projectRoot.appendingPathComponent("venv/bin/python")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 620, height: 360))
        window = NSWindow(
            contentRect: content.bounds,
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Radio Command Center"
        window.center()
        window.contentView = content

        let title = label("Radio Command Center", size: 26, bold: true)
        title.frame = NSRect(x: 28, y: 298, width: 560, height: 34)
        content.addSubview(title)

        let subtitle = label("Live transcription services and recording source", size: 13)
        subtitle.textColor = .secondaryLabelColor
        subtitle.frame = NSRect(x: 30, y: 274, width: 560, height: 20)
        content.addSubview(subtitle)

        statusLabel = label("Checking services…", size: 14, bold: true)
        statusLabel.frame = NSRect(x: 30, y: 225, width: 560, height: 24)
        content.addSubview(statusLabel)

        let sourceTitle = label("Recording folder", size: 12, bold: true)
        sourceTitle.frame = NSRect(x: 30, y: 178, width: 160, height: 20)
        content.addSubview(sourceTitle)

        sourceField = NSTextField(frame: NSRect(x: 30, y: 143, width: 455, height: 28))
        sourceField.isEditable = false
        sourceField.placeholderString = "Choose a local or mounted network folder"
        content.addSubview(sourceField)

        let chooseButton = button("Choose…", action: #selector(chooseSource))
        chooseButton.frame = NSRect(x: 495, y: 142, width: 95, height: 30)
        content.addSubview(chooseButton)

        let startButton = button("Launch", action: #selector(startServices))
        startButton.frame = NSRect(x: 30, y: 78, width: 110, height: 36)
        content.addSubview(startButton)

        let restartButton = button("Restart", action: #selector(restartServices))
        restartButton.frame = NSRect(x: 150, y: 78, width: 110, height: 36)
        content.addSubview(restartButton)

        let stopButton = button("Stop", action: #selector(stopServices))
        stopButton.frame = NSRect(x: 270, y: 78, width: 110, height: 36)
        content.addSubview(stopButton)

        let openButton = button("Open Dashboard", action: #selector(openDashboard))
        openButton.frame = NSRect(x: 405, y: 78, width: 185, height: 36)
        content.addSubview(openButton)

        let note = label("The selected folder may be on a mounted SMB/network share.", size: 11)
        note.textColor = .tertiaryLabelColor
        note.frame = NSRect(x: 30, y: 31, width: 560, height: 20)
        content.addSubview(note)

        window.makeKeyAndOrderFront(nil)
        refreshStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
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
        process.arguments = [projectRoot.appendingPathComponent("scripts/service_control.py").path] + arguments
        process.currentDirectoryURL = projectRoot
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self] task in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            DispatchQueue.main.async {
                if task.terminationStatus != 0 {
                    self?.statusLabel.stringValue = (value?["error"] as? String) ?? "Command failed"
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

    private func refreshStatus() {
        runControl(["status"]) { [weak self] value in
            guard let self, let value else { return }
            let running = value["running"] as? Bool ?? false
            self.statusLabel.stringValue = running ? "● Services running" : "○ Services stopped"
            self.statusLabel.textColor = running ? .systemGreen : .secondaryLabelColor
            self.loadSelectedSource()
        }
    }

    private func loadSelectedSource() {
        let settingsURL = projectRoot.appendingPathComponent("data/settings.json")
        guard
            let data = try? Data(contentsOf: settingsURL),
            let settings = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        sourceField.stringValue = settings["source_dir"] as? String ?? ""
    }

    @objc private func chooseSource() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Recording Folder"
        if panel.runModal() == .OK, let folder = panel.url {
            sourceField.stringValue = folder.path
            statusLabel.stringValue = "Saving recording folder…"
            runControl(["configure", "--source", folder.path]) { [weak self] value in
                guard value != nil else { return }
                self?.statusLabel.stringValue = "Folder saved — restart to apply"
            }
        }
    }

    @objc private func startServices() {
        statusLabel.stringValue = "Starting services…"
        runControl(["start"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func restartServices() {
        statusLabel.stringValue = "Restarting services…"
        runControl(["restart"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func stopServices() {
        statusLabel.stringValue = "Stopping services…"
        runControl(["stop"]) { [weak self] _ in self?.refreshStatus() }
    }

    @objc private func openDashboard() {
        runControl(["status"]) { value in
            guard
                let address = value?["dashboard"] as? String,
                let url = URL(string: address)
            else { return }
            NSWorkspace.shared.open(url)
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.activate(ignoringOtherApps: true)
application.run()
