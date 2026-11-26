import CoreMIDI
import Foundation

/// One place MIDI can be sent to.
struct MIDIOutTarget: Identifiable {
    enum Kind {
        case virtualSource
        case external
        case network
    }

    let id: String
    let name: String
    let kind: Kind
    let endpoint: MIDIEndpointRef
}

/// CoreMIDI plumbing: a virtual output source, external destinations, an
/// RTP-MIDI (AppleMIDI) network session, and an input port used for MIDI thru.
final class MIDIOutput {

    private var client = MIDIClientRef()
    private var outputPort = MIDIPortRef()
    private var inputPort = MIDIPortRef()
    private var virtualSource = MIDIEndpointRef()
    private var connectedSources = Set<MIDIEndpointRef>()

    /// Called on the main queue when the set of destinations changes.
    var onDestinationsChanged: (() -> Void)?
    /// Called with raw bytes for every incoming MIDI message (main queue).
    var onIncoming: (([UInt8]) -> Void)?

    private(set) var isStarted = false

    // RTP-MIDI settings
    var networkEnabled = false {
        didSet { applyNetworkSettings() }
    }
    var networkPolicy: MIDINetworkConnectionPolicy = .anyone {
        didSet { applyNetworkSettings() }
    }

    /// The name and port iOS advertises for the network session. Both are
    /// read-only in MIDINetworkSession, so they are shown rather than set.
    var sessionName: String { MIDINetworkSession.default().networkName }
    var sessionPort: Int { MIDINetworkSession.default().networkPort }

    deinit {
        stop()
    }

    // MARK: - Lifecycle

    func start() {
        guard !isStarted else { return }
        let notify: MIDINotifyBlock = { [weak self] _ in
            DispatchQueue.main.async { self?.onDestinationsChanged?() }
        }
        guard MIDIClientCreateWithBlock("LoopGen" as CFString, &client, notify) == noErr else {
            return
        }
        guard MIDIOutputPortCreate(client, "LoopGen Out" as CFString, &outputPort) == noErr else {
            return
        }
        if MIDISourceCreate(client, "LoopGen Out" as CFString, &virtualSource) != noErr {
            virtualSource = 0
        }

        let readBlock: MIDIReadBlock = { [weak self] packetList, _ in
            guard let self = self else { return }
            for bytes in MIDIOutput.messages(in: packetList) {
                DispatchQueue.main.async { self.onIncoming?(bytes) }
            }
        }
        if MIDIInputPortCreateWithBlock(client, "LoopGen In" as CFString,
                                        &inputPort, readBlock) == noErr {
            connectAllSources()
        }
        isStarted = true
        applyNetworkSettings()
        onDestinationsChanged?()
    }

    func stop() {
        guard isStarted else { return }
        MIDINetworkSession.default().isEnabled = false
        if client != 0 {
            MIDIClientDispose(client)
        }
        client = 0
        outputPort = 0
        inputPort = 0
        virtualSource = 0
        connectedSources.removeAll()
        isStarted = false
        onDestinationsChanged?()
    }

    // MARK: - RTP-MIDI

    private func applyNetworkSettings() {
        let session = MIDINetworkSession.default()
        session.connectionPolicy = networkPolicy
        session.isEnabled = networkEnabled && isStarted
        onDestinationsChanged?()
    }

    // MARK: - Discovery

    var targets: [MIDIOutTarget] {
        var list: [MIDIOutTarget] = []

        if virtualSource != 0 {
            list.append(MIDIOutTarget(id: "virtual", name: "LoopGen Out (virtual)",
                                      kind: .virtualSource, endpoint: virtualSource))
        }

        let networkDestination = MIDINetworkSession.default().destinationEndpoint()
        if networkEnabled, networkDestination != 0 {
            list.append(MIDIOutTarget(id: "network",
                                      name: "RTP-MIDI: \(sessionName) (\(sessionPort))",
                                      kind: .network, endpoint: networkDestination))
        }

        for index in 0..<MIDIGetNumberOfDestinations() {
            let endpoint = MIDIGetDestination(index)
            guard endpoint != 0 else { continue }
            list.append(MIDIOutTarget(id: "dest-\(endpoint)",
                                      name: MIDIOutput.displayName(of: endpoint),
                                      kind: .external, endpoint: endpoint))
        }
        return list
    }

    static func displayName(of endpoint: MIDIEndpointRef) -> String {
        var value: Unmanaged<CFString>?
        if MIDIObjectGetStringProperty(endpoint, kMIDIPropertyDisplayName, &value) == noErr,
           let name = value?.takeRetainedValue() {
            return name as String
        }
        return "MIDI destination"
    }

    private func connectAllSources() {
        for index in 0..<MIDIGetNumberOfSources() {
            let source = MIDIGetSource(index)
            guard source != 0, !connectedSources.contains(source) else { continue }
            if MIDIPortConnectSource(inputPort, source, nil) == noErr {
                connectedSources.insert(source)
            }
        }
    }

    // MARK: - Sending

    /// Sends one message to every enabled target.
    func send(_ bytes: [UInt8], to targets: [MIDIOutTarget]) {
        guard !bytes.isEmpty, isStarted, !targets.isEmpty else { return }
        guard let packetList = MIDIOutput.packetList(for: [bytes]) else { return }
        defer { MIDIOutput.deallocate(packetList) }

        for target in targets {
            if target.kind == .virtualSource {
                MIDIReceived(target.endpoint, UnsafePointer(packetList))
            } else {
                MIDISend(outputPort, target.endpoint, UnsafePointer(packetList))
            }
        }
    }

    func sendAllNotesOff(to targets: [MIDIOutTarget]) {
        for channel in UInt8(0)...UInt8(15) {
            send([0xB0 | channel, 123, 0], to: targets)
        }
    }

    // MARK: - Packet helpers

    private static let packetBufferSize = 1024

    /// Builds a MIDIPacketList holding the given messages. Caller deallocates.
    static func packetList(for messages: [[UInt8]]) -> UnsafeMutablePointer<MIDIPacketList>? {
        guard let first = messages.first else { return nil }
        let capacity = packetBufferSize
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: capacity,
            alignment: MemoryLayout<MIDIPacketList>.alignment)
        let list = raw.bindMemory(to: MIDIPacketList.self, capacity: 1)
        var packet = MIDIPacketListInit(list)

        for bytes in messages {
            guard !bytes.isEmpty else { continue }
            let next = MIDIPacketListAdd(list, capacity, packet, 0, bytes.count, bytes)
            if next == nil { break }
            packet = next
        }
        return list
    }

    /// Flattens a packet list into individual messages.
    static func messages(in packetList: UnsafePointer<MIDIPacketList>) -> [[UInt8]] {
        var out: [[UInt8]] = []
        var packet = packetList.pointee.packet
        for _ in 0..<packetList.pointee.numPackets {
            let length = Int(packet.length)
            let bytes = withUnsafeBytes(of: packet.data) { raw in
                Array(raw.prefix(length))
            }
            if !bytes.isEmpty {
                out.append(bytes)
            }
            packet = MIDIPacketNext(&packet).pointee
        }
        return out
    }

    static func deallocate(_ list: UnsafeMutablePointer<MIDIPacketList>) {
        UnsafeMutableRawPointer(list).deallocate()
    }
}
