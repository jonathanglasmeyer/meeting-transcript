// micwatch — emits RUNNING / STOPPED on stdout when a real microphone starts
// or stops being captured by *any* process (Notion/Granola-style detection).
//
// Mechanism: Core Audio property listener on
// kAudioDevicePropertyDeviceIsRunningSomewhere for every input device.
// This reflects device IO state only — it does NOT read audio data, so it
// needs no microphone TCC permission.
//
// Virtual loopbacks (BlackHole) and Multi-Output devices are excluded so that
// merely playing system audio through a Multi-Output device does not look like
// a meeting. Aggregate devices (e.g. "Hollyland (Aggregate)") ARE watched —
// they are real microphones.
//
// Debounced: a mic must stay running for `onDelay` before RUNNING is emitted,
// and stay idle for `offDelay` before STOPPED. Filters out dictation/Siri blips.
//
// Build:  swiftc -O micwatch.swift -o micwatch
// Tune:   MICWATCH_IGNORE="BlackHole,Multi-Output,Loopback" ./micwatch

import CoreAudio
import Foundation

// Unbuffered line output so the Python controller reads events immediately.
func emit(_ s: String) {
    FileHandle.standardOutput.write(Data((s + "\n").utf8))
}
func logErr(_ s: String) {
    FileHandle.standardError.write(Data((s + "\n").utf8))
}

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

final class Watcher {
    let q = DispatchQueue(label: "micwatch.events")
    let ignoreSubstrings: [String]
    let onDelay: TimeInterval = 1.2
    let offDelay: TimeInterval = 2.0

    var watched = Set<AudioDeviceID>()   // input devices we listen on
    var emittedRunning = false
    var pending: DispatchWorkItem?

    init() {
        let env = ProcessInfo.processInfo.environment["MICWATCH_IGNORE"] ?? "BlackHole,Multi-Output"
        ignoreSubstrings = env.split(separator: ",").map {
            $0.trimmingCharacters(in: .whitespaces).lowercased()
        }.filter { !$0.isEmpty }
    }

    // A device counts as a watchable microphone if it has input channels, is not
    // a virtual loopback, and its name is not on the ignore list.
    func isWatchableMic(_ dev: AudioDeviceID) -> Bool {
        guard inputChannelCount(dev) > 0 else { return false }
        if transportType(dev) == kVirtual { return false }
        let name = deviceName(dev).lowercased()
        for sub in ignoreSubstrings where name.contains(sub) { return false }
        return true
    }

    func refreshWatched() {  // call on q
        for dev in allDeviceIDs() where isWatchableMic(dev) && !watched.contains(dev) {
            addRunningListener(dev)
            watched.insert(dev)
            logErr("  watching: \(deviceName(dev)) [id \(dev)]")
        }
    }

    func anyMicRunning() -> Bool {
        for dev in watched where isRunningSomewhere(dev) { return true }
        return false
    }

    func addRunningListener(_ dev: AudioDeviceID) {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        AudioObjectAddPropertyListenerBlock(dev, &addr, q) { [weak self] _, _ in
            self?.recompute()   // already on q
        }
    }

    func recompute() {  // call on q
        let running = anyMicRunning()
        if running == emittedRunning {
            pending?.cancel(); pending = nil   // transition reverted before debounce fired
            return
        }
        pending?.cancel()
        let delay = running ? onDelay : offDelay
        let item = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            let now = self.anyMicRunning()
            if now != self.emittedRunning {
                self.emittedRunning = now
                emit(now ? "RUNNING" : "STOPPED")
            }
            self.pending = nil
        }
        pending = item
        q.asyncAfter(deadline: .now() + delay, execute: item)
    }

    func start() {
        q.sync {
            logErr("micwatch: scanning microphones (ignoring: \(ignoreSubstrings.joined(separator: ", ")))")
            refreshWatched()
            if watched.isEmpty { logErr("micwatch: WARNING — no watchable microphones found") }

            // Re-scan when the device list changes (mic plugged/unplugged).
            var devAddr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDevices,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            AudioObjectAddPropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject), &devAddr, q) { [weak self] _, _ in
                self?.refreshWatched()
                self?.recompute()
            }

            // Emit initial state if a mic is already live at launch.
            emittedRunning = anyMicRunning()
            logErr("micwatch: ready (initial state: \(emittedRunning ? "RUNNING" : "idle"))")
            if emittedRunning { emit("RUNNING") }
        }
    }
}

let watcher = Watcher()
watcher.start()
RunLoop.current.run()
