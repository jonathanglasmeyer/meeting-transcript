// MeetingBar — menu-bar app that auto-detects meetings (Core Audio mic-in-use)
// and offers to start the `meeting` recorder. Notion/Granola-style:
//   • 🎙 in the menu bar; turns red while recording
//   • mic goes live + a meeting app is around  ->  native alert (real mic icon)
//   • "Aufnehmen"  ->  recording starts, managed by this app
//   • click the menu bar item -> "Stoppen"  ->  newline to recorder stdin ->
//     recorder stops and transcribes (no Terminal involved)
//
// Detection reads only Core Audio device IO state — no microphone permission.
// Build:  swiftc -O MeetingBar.swift -o meetingbar   (see install.sh)

import AppKit
import CoreAudio
import Foundation

// =============================================================================
// Core Audio — "is any real microphone being captured?"
// =============================================================================

let kVirtual: UInt32 = 0x76697274  // 'virt' — kAudioDeviceTransportTypeVirtual

func allDeviceIDs() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioDeviceID>.size
    var ids = [AudioDeviceID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func deviceName(_ dev: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var name: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &name) == noErr,
          let cf = name?.takeRetainedValue() else { return "?" }
    return cf as String
}

func transportType(_ dev: AudioDeviceID) -> UInt32 {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyTransportType,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var t: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &t)
    return t
}

func inputChannelCount(_ dev: AudioDeviceID) -> Int {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(
        byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    var channels = 0
    for b in abl { channels += Int(b.mNumberChannels) }
    return channels
}

func isRunningSomewhere(_ dev: AudioDeviceID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var val: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &val) == noErr else { return false }
    return val != 0
}

final class MicWatcher {
    private let q = DispatchQueue(label: "meetingbar.micwatch")
    private let ignore: [String]
    private let onDelay: TimeInterval = 1.2
    private let offDelay: TimeInterval = 2.0
    private var watched = Set<AudioDeviceID>()
    private var emitted = false
    private var pending: DispatchWorkItem?
    private let onChange: (Bool) -> Void   // called on main queue

    init(ignore: [String], onChange: @escaping (Bool) -> Void) {
        self.ignore = ignore.map { $0.lowercased() }
        self.onChange = onChange
    }

    private func isWatchableMic(_ dev: AudioDeviceID) -> Bool {
        guard inputChannelCount(dev) > 0 else { return false }
        if transportType(dev) == kVirtual { return false }
        let name = deviceName(dev).lowercased()
        for sub in ignore where !sub.isEmpty && name.contains(sub) { return false }
        return true
    }

    private func refreshWatched() {
        for dev in allDeviceIDs() where isWatchableMic(dev) && !watched.contains(dev) {
            var addr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            AudioObjectAddPropertyListenerBlock(dev, &addr, q) { [weak self] _, _ in
                self?.recompute()
            }
            watched.insert(dev)
            NSLog("MeetingBar: watching \(deviceName(dev))")
        }
    }

    private func anyRunning() -> Bool {
        for dev in watched where isRunningSomewhere(dev) { return true }
        return false
    }

    private func recompute() {
        let running = anyRunning()
        if running == emitted { pending?.cancel(); pending = nil; return }
        pending?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            let now = self.anyRunning()
            if now != self.emitted {
                self.emitted = now
                DispatchQueue.main.async { self.onChange(now) }
            }
            self.pending = nil
        }
        pending = item
        q.asyncAfter(deadline: .now() + (running ? onDelay : offDelay), execute: item)
    }

    func start() {
        q.async {
            self.refreshWatched()
            var devAddr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDevices,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            AudioObjectAddPropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject), &devAddr, self.q) { [weak self] _, _ in
                self?.refreshWatched(); self?.recompute()
            }
            self.emitted = self.anyRunning()   // adopt current state silently; only edges fire onChange
        }
    }
}

// =============================================================================
// Config
// =============================================================================

struct Config {
    var meetingBundleIDs: [String]
    var meetingCommand: String
    var recordOnly: Bool
    var whisperModel: String?
    var cooldown: TimeInterval
    var requireMeetingApp: Bool
    var ignoreDeviceSubstrings: [String]

    static func load(from dir: URL) -> Config {
        var c = Config(
            meetingBundleIDs: ["com.microsoft.teams2", "com.microsoft.teams",
                               "us.zoom.xos", "com.tinyspeck.slackmacgap",
                               "com.cisco.webexmeetingsapp"],
            meetingCommand: "/opt/homebrew/bin/uv run --project " +
                            "/Users/jonathan.glasmeyer/Projects/meeting-transcript meeting",
            recordOnly: false, whisperModel: "large-v3-turbo", cooldown: 180,
            requireMeetingApp: true,
            ignoreDeviceSubstrings: ["blackhole", "multi-output"])
        let url = dir.appendingPathComponent("config.json")
        guard let data = try? Data(contentsOf: url),
              let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return c }
        if let v = j["meeting_bundle_ids"] as? [String] { c.meetingBundleIDs = v }
        if let v = j["meeting_command"] as? String { c.meetingCommand = v }
        if let v = j["record_only"] as? Bool { c.recordOnly = v }
        if let v = j["whisper_model"] as? String { c.whisperModel = v.isEmpty ? nil : v }
        if let v = j["cooldown_seconds"] as? Double { c.cooldown = v }
        if let v = j["require_meeting_app"] as? Bool { c.requireMeetingApp = v }
        if let v = j["ignore_device_substrings"] as? [String] { c.ignoreDeviceSubstrings = v }
        return c
    }
}

let FRIENDLY: [String: String] = [
    "com.microsoft.teams2": "Microsoft Teams", "com.microsoft.teams": "Microsoft Teams",
    "us.zoom.xos": "Zoom", "com.tinyspeck.slackmacgap": "Slack",
    "com.cisco.webexmeetingsapp": "Webex", "com.webex.meetingmanager": "Webex",
]

// =============================================================================
// Recorder — owns the `meeting` subprocess; stop = newline on stdin
// =============================================================================

final class Recorder {
    private var process: Process?
    private var stdinHandle: FileHandle?
    let logURL: URL
    var onFinish: ((Int32) -> Void)?

    init(logURL: URL) { self.logURL = logURL }
    var isRunning: Bool { process?.isRunning ?? false }

    func start(command: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", command]   // login shell → PATH for uv/ffmpeg/sox
        let inPipe = Pipe()
        p.standardInput = inPipe
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let lh = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = lh
            p.standardError = lh
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.onFinish?(proc.terminationStatus) }
        }
        do {
            try p.run()
            self.process = p
            self.stdinHandle = inPipe.fileHandleForWriting
            NSLog("MeetingBar: recorder started")
        } catch {
            NSLog("MeetingBar: recorder failed to start: \(error)")
            DispatchQueue.main.async { self.onFinish?(-1) }
        }
    }

    func stop() {
        guard isRunning else { return }
        try? stdinHandle?.write(contentsOf: Data("\n".utf8))   // release input()
    }

    // Best-effort: last "✅ <path>" line the meeting tool printed.
    func lastResultPath() -> String? {
        guard let txt = try? String(contentsOf: logURL, encoding: .utf8) else { return nil }
        for line in txt.split(separator: "\n").reversed() where line.contains("✅") {
            if let r = line.range(of: "/") { return String(line[r.lowerBound...]).trimmingCharacters(in: .whitespaces) }
        }
        return nil
    }
}

// =============================================================================
// App
// =============================================================================

enum State { case idle, recording, transcribing }

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var watcher: MicWatcher!
    private var recorder: Recorder!
    private var cfg: Config!
    private var cfgDir: URL!
    private var state: State = .idle
    private var lastTrigger: Date = .distantPast
    private var recordingStart: Date?
    private var elapsedTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)   // menu-bar only, no Dock icon

        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        cfgDir = exe.deletingLastPathComponent()
        cfg = Config.load(from: cfgDir)

        let state = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".meeting-autostart")
        try? FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        recorder = Recorder(logURL: state.appendingPathComponent("recording.log"))
        recorder.onFinish = { [weak self] status in self?.recordingFinished(status) }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        render()

        watcher = MicWatcher(ignore: cfg.ignoreDeviceSubstrings) { [weak self] running in
            self?.micChanged(running)
        }
        watcher.start()
        NSLog("MeetingBar: ready")
    }

    // ---- detection → policy ----

    private func micChanged(_ running: Bool) {
        guard running else { return }              // only rising edge matters
        guard state == .idle else { return }       // already recording/transcribing
        cfg = Config.load(from: cfgDir)            // pick up live edits
        if Date().timeIntervalSince(lastTrigger) < cfg.cooldown { return }
        if anotherRecorderRunning() { NSLog("MeetingBar: another recorder active, ignoring"); return }
        let context = meetingAppPresent()
        if cfg.requireMeetingApp && context == nil { NSLog("MeetingBar: no meeting app, ignoring"); return }
        lastTrigger = Date()
        promptToRecord(context: context)
    }

    private func meetingAppPresent() -> String? {
        for app in NSWorkspace.shared.runningApplications {
            if let bid = app.bundleIdentifier, cfg.meetingBundleIDs.contains(bid) {
                return FRIENDLY[bid] ?? bid
            }
        }
        return nil
    }

    private func anotherRecorderRunning() -> Bool {
        for pat in ["ffmpeg.*avfoundation", "sox.*coreaudio"] {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
            p.arguments = ["-f", pat]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try? p.run(); p.waitUntilExit()
            if p.terminationStatus == 0 { return true }
        }
        return false
    }

    private func micIcon(filled: Bool) -> NSImage? {
        let name = filled ? "mic.fill" : "mic"
        return NSImage(systemSymbolName: name, accessibilityDescription: "Meeting")
    }

    private func promptToRecord(context: String?) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = context.map { "Meeting erkannt — \($0)" } ?? "Meeting erkannt"
        alert.informativeText = "Aufnahme starten? Stoppen über das 🎙-Symbol in der Menüleiste."
        alert.icon = NSImage(systemSymbolName: "mic.circle.fill", accessibilityDescription: nil)
        alert.addButton(withTitle: "Aufnehmen")   // first = default (Return)
        alert.addButton(withTitle: "Ignorieren")
        if alert.runModal() == .alertFirstButtonReturn {
            startRecording()
        }
    }

    // ---- recording lifecycle ----

    private func startRecording() {
        var cmd = cfg.meetingCommand
        if cfg.recordOnly { cmd += " -r" }
        // Language is auto-detected by the transcriber (no -l). Model pinned via env.
        if let m = cfg.whisperModel, !m.isEmpty { cmd = "WHISPER_MODEL=\(m) " + cmd }
        recorder.start(command: cmd)
        state = .recording
        recordingStart = Date()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.render() }
        notify(title: "🎙 Meeting wird aufgenommen", body: "Klick aufs Menüleisten-Symbol zum Stoppen.")
        render()
    }

    @objc private func stopRecording() {
        guard state == .recording else { return }
        recorder.stop()
        state = cfg.recordOnly ? .idle : .transcribing
        elapsedTimer?.invalidate(); elapsedTimer = nil
        render()
    }

    private func recordingFinished(_ status: Int32) {
        elapsedTimer?.invalidate(); elapsedTimer = nil
        state = .idle
        recordingStart = nil
        if status == 0 {
            if let path = recorder.lastResultPath() {
                notify(title: "✅ Transkript fertig", body: path)
            } else {
                notify(title: "✅ Aufnahme fertig", body: "Gespeichert unter ~/Meetings")
            }
        } else {
            notify(title: "⚠️ Aufnahme beendet", body: "Recorder exit \(status) — siehe recording.log")
        }
        render()
    }

    // ---- menu bar rendering ----

    private func render() {
        guard let button = statusItem.button else { return }
        let menu = NSMenu()
        switch state {
        case .idle:
            button.image = micIcon(filled: false)
            button.image?.isTemplate = true
            button.title = ""
            button.contentTintColor = nil
            menu.addItem(NSMenuItem(title: "Bereit — wartet auf Meeting", action: nil, keyEquivalent: ""))
        case .recording:
            button.image = micIcon(filled: true)
            button.image?.isTemplate = false
            button.contentTintColor = .systemRed
            button.title = " " + elapsedString()
            let stop = NSMenuItem(title: "⏹ Aufnahme stoppen (\(elapsedString()))",
                                  action: #selector(stopRecording), keyEquivalent: "")
            stop.target = self
            menu.addItem(stop)
        case .transcribing:
            button.image = micIcon(filled: true)
            button.image?.isTemplate = false
            button.contentTintColor = .systemOrange
            button.title = " …"
            menu.addItem(NSMenuItem(title: "Transkribiere…", action: nil, keyEquivalent: ""))
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Beenden", action: #selector(NSApp.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        statusItem.menu = menu
    }

    private func elapsedString() -> String {
        guard let s = recordingStart else { return "0:00" }
        let t = Int(Date().timeIntervalSince(s))
        return String(format: "%d:%02d", t / 60, t % 60)
    }

    private func notify(title: String, body: String) {
        let n = NSUserNotification()
        n.title = title
        n.informativeText = body
        NSUserNotificationCenter.default.deliver(n)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
