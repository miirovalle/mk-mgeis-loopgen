import Combine
import CoreMIDI
import Foundation

/// Plays the generated loop out to MIDI, with RTP-MIDI, MIDI out and MIDI thru.
final class LoopTransport: ObservableObject {

    @Published var isRunning = false
    @Published var useMIDIOut = true
    @Published var midiThru = false
    @Published var selectedTargetID = "virtual"
    @Published private(set) var targets: [MIDIOutTarget] = []
    @Published private(set) var messageLog: [String] = []

    @Published var networkEnabled = false {
        didSet { midi.networkEnabled = networkEnabled }
    }
    @Published var networkName = "LoopGen" {
        didSet { midi.networkName = networkName }
    }
    @Published var networkPolicy: MIDINetworkPolicy = .anyone {
        didSet { midi.networkPolicy = networkPolicy.policy }
    }

    var spec = LoopSpec()

    private let midi = MIDIOutput()
    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "loopgen.transport", qos: .userInteractive)
    private var events: [MIDIEvent] = []
    private var loopBeats: Double = 16
    private var startTime = Date()
    private var nextIndex = 0
    private var completedLoops = 0

    init() {
        midi.onDestinationsChanged = { [weak self] in
            guard let self = self else { return }
            self.targets = self.midi.targets
            if !self.targets.contains(where: { $0.id == self.selectedTargetID }) {
                self.selectedTargetID = self.targets.first?.id ?? ""
            }
        }
        midi.onIncoming = { [weak self] bytes in
            self?.handleIncoming(bytes)
        }
        midi.start()
        targets = midi.targets
    }

    deinit {
        timer?.cancel()
        midi.stop()
    }

    // MARK: - Targets

    /// Everything MIDI gets sent to: the selected destination plus, when MIDI
    /// thru is on, every other destination as well.
    var activeTargets: [MIDIOutTarget] {
        guard useMIDIOut else { return [] }
        if midiThru {
            return targets
        }
        if let selected = targets.first(where: { $0.id == selectedTargetID }) {
            return [selected]
        }
        return []
    }

    // MARK: - Transport

    func start() {
        guard !isRunning else { return }
        events = LoopGenerator.generate(spec)
        loopBeats = Double(max(4, spec.bars) * 4)
        nextIndex = 0
        completedLoops = 0
        startTime = Date()
        isRunning = true

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(5))
        timer.setEventHandler { [weak self] in
            self?.tick()
        }
        self.timer = timer
        timer.resume()
    }

    func stop() {
        guard isRunning else { return }
        timer?.cancel()
        timer = nil
        isRunning = false
        let out = activeTargets
        midi.sendAllNotesOff(to: out)
    }

    private func tick() {
        let elapsed = Date().timeIntervalSince(startTime)
        let beatsPerSecond = spec.bpm / 60.0
        let totalBeats = elapsed * beatsPerSecond

        // how many complete loop passes have gone by
        let loops = Int(floor(totalBeats / loopBeats))
        if loops > completedLoops {
            completedLoops = loops
            nextIndex = 0
        }
        let beatInLoop = totalBeats - Double(completedLoops) * loopBeats

        let out = activeTargets
        guard !out.isEmpty else { return }

        var sent = 0
        while nextIndex < events.count, events[nextIndex].beat <= beatInLoop {
            let event = events[nextIndex]
            let status: UInt8 = event.isNoteOn ? 0x90 : 0x80
            let bytes: [UInt8] = [status | (event.channel & 0x0F), event.data1,
                                  event.isNoteOn ? event.data2 : 0]
            midi.send(bytes, to: out)
            nextIndex += 1
            sent += 1
        }
        if sent > 0, sent >= 6 {
            log("sent \(sent) messages")
        }
    }

    // MARK: - MIDI thru

    private func handleIncoming(_ bytes: [UInt8]) {
        guard midiThru, useMIDIOut else { return }
        let out = targets
        guard !out.isEmpty else { return }
        midi.send(bytes, to: out)
    }

    private func log(_ line: String) {
        DispatchQueue.main.async {
            self.messageLog.append(line)
            if self.messageLog.count > 40 {
                self.messageLog.removeFirst(self.messageLog.count - 40)
            }
        }
    }
}

/// UI-friendly wrapper for MIDINetworkConnectionPolicy.
enum MIDINetworkPolicy: Int, CaseIterable, Identifiable {
    case anyone = 0
    case contacts = 1
    case noOne = 2

    var id: Int { rawValue }

    var name: String {
        switch self {
        case .anyone: return "Anyone"
        case .contacts: return "Contacts only"
        case .noOne: return "No one"
        }
    }

    var policy: MIDINetworkConnectionPolicy {
        switch self {
        case .anyone: return .anyone
        case .contacts: return .hostsInContactList
        case .noOne: return .noOne
        }
    }
}
