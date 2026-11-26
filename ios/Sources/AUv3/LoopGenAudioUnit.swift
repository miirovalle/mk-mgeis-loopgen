import AudioToolbox
import AVFoundation
import Foundation

/// AUv3 MIDI processor ('aumi'): generates an in-key loop and streams the notes
/// out to the host as MIDI, aligned to the host's beat position.
public final class LoopGenAudioUnit: AUAudioUnit {

    private enum Address {
        static let root: AUParameterAddress = 0
        static let mode: AUParameterAddress = 1
        static let bars: AUParameterAddress = 2
        static let seed: AUParameterAddress = 3
    }

    private final class RenderState {
        var events: [MIDIEvent] = []
        var loopBeats: Double = 16
        var lastBucket = Int.min
        var internalFrames: Double = 0
        var needsRebuild = true
        var spec = LoopSpec()
        var tempo: Double = 120
    }

    private let state = RenderState()
    private let lock = NSLock()
    private var _parameterTree: AUParameterTree?
    private var _inputBusses: AUAudioUnitBusArray!
    private var _outputBusses: AUAudioUnitBusArray!

    public override init(componentDescription: AudioComponentDescription,
                         options: AudioComponentInstantiationOptions = []) throws {
        try super.init(componentDescription: componentDescription,
                       options: options)
        _inputBusses = AUAudioUnitBusArray(audioUnit: self, count: 0, busType: .input)
        _outputBusses = AUAudioUnitBusArray(audioUnit: self, count: 0, busType: .output)
        maximumFramesToRender = 4096
        _parameterTree = makeParameterTree()
    }

    // MARK: - Parameters

    private func makeParameterTree() -> AUParameterTree {
        let root = AUParameterTree.createParameter(
            withIdentifier: "root", name: "Root", address: Address.root,
            min: 0, max: 11, unit: .indexed, unitName: nil,
            flags: [.flagIsWritable], valueStrings: LoopGenerator.pitchNames,
            dependentParameters: nil)
        let mode = AUParameterTree.createParameter(
            withIdentifier: "mode", name: "Scale", address: Address.mode,
            min: 0, max: 1, unit: .indexed, unitName: nil,
            flags: [.flagIsWritable], valueStrings: ["Minor", "Major"],
            dependentParameters: nil)
        let bars = AUParameterTree.createParameter(
            withIdentifier: "bars", name: "Bars", address: Address.bars,
            min: 4, max: 16, unit: .indexed, unitName: nil,
            flags: [.flagIsWritable], valueStrings: ["4", "8", "16"],
            dependentParameters: nil)
        let seed = AUParameterTree.createParameter(
            withIdentifier: "seed", name: "Seed", address: Address.seed,
            min: 0, max: 9999, unit: .indexed, unitName: nil,
            flags: [.flagIsWritable], valueStrings: nil, dependentParameters: nil)

        seed.value = 1
        bars.value = 16

        let tree = AUParameterTree.createTree(withChildren: [root, mode, bars, seed])
        tree.implementorValueObserver = { [weak self] parameter, value in
            self?.applyParameter(address: parameter.address, value: value)
        }
        tree.implementorValueProvider = { [weak self] parameter in
            self?.parameterValue(for: parameter.address) ?? 0
        }
        return tree
    }

    public override var parameterTree: AUParameterTree? {
        get { return _parameterTree }
        set { _parameterTree = newValue }
    }

    private func applyParameter(address: AUParameterAddress, value: AUValue) {
        lock.lock()
        switch address {
        case Address.root: state.spec.rootPC = Int(value.rounded())
        case Address.mode: state.spec.mode = ScaleMode(rawValue: Int(value.rounded())) ?? .minor
        case Address.bars: state.spec.bars = max(4, min(16, Int(value.rounded())))
        case Address.seed: state.spec.seed = UInt64(max(0, value.rounded()))
        default: break
        }
        state.needsRebuild = true
        lock.unlock()
    }

    private func parameterValue(for address: AUParameterAddress) -> AUValue {
        switch address {
        case Address.root: return AUValue(state.spec.rootPC)
        case Address.mode: return AUValue(state.spec.mode.rawValue)
        case Address.bars: return AUValue(state.spec.bars)
        case Address.seed: return AUValue(state.spec.seed)
        default: return 0
        }
    }

    // MARK: - Buses

    public override var inputBusses: AUAudioUnitBusArray { return _inputBusses }
    public override var outputBusses: AUAudioUnitBusArray { return _outputBusses }

    // MARK: - Render resources

    public override func allocateRenderResources() throws {
        try super.allocateRenderResources()
        lock.lock()
        state.needsRebuild = true
        state.lastBucket = Int.min
        lock.unlock()
    }

    public override func deallocateRenderResources() {
        lock.lock()
        state.events = []
        lock.unlock()
        super.deallocateRenderResources()
    }

    // MARK: - Render

    public override var internalRenderBlock: AUInternalRenderBlock {
        let midiOut = self.midiOutputEventBlock
        let musicalContext = self.musicalContextBlock
        let state = self.state
        let lock = self.lock

        return { _, timestamp, frameCount, _, _, _, _ -> OSStatus in
            var tempo: Double = 120
            var beat: Double = -1

            if let musicalContext = musicalContext {
                _ = musicalContext(&tempo, nil, nil, &beat, nil, nil)
            }
            state.tempo = tempo

            if beat < 0 {
                state.internalFrames += Double(frameCount)
                beat = state.internalFrames / 48000.0 * tempo / 60.0
            }
            _ = timestamp

            guard let midiOut = midiOut else { return noErr }

            lock.lock()
            if state.needsRebuild {
                state.events = LoopGenerator.generate(state.spec)
                state.loopBeats = Double(max(4, state.spec.bars) * 4)
                state.needsRebuild = false
            }
            let events = state.events
            let loopBeats = state.loopBeats
            lock.unlock()

            guard !events.isEmpty, loopBeats > 0 else { return noErr }

            let step = 0.25
            let bucket = Int((beat / step).rounded(.down))
            if bucket == state.lastBucket { return noErr }
            let first = state.lastBucket == Int.min
                ? bucket
                : state.lastBucket + 1
            state.lastBucket = bucket
            if first > bucket { return noErr }

            let stepsPerLoop = Int((loopBeats / step).rounded())
            guard stepsPerLoop > 0 else { return noErr }

            for current in first...bucket {
                let loopStep = ((current % stepsPerLoop) + stepsPerLoop) % stepsPerLoop
                let start = Double(loopStep) * step
                let end = start + step
                for event in events where event.beat >= start && event.beat < end {
                    var bytes: [UInt8] = [
                        (event.isNoteOn ? 0x90 : 0x80) | (event.channel & 0x0F),
                        event.data1,
                        event.isNoteOn ? event.data2 : 0,
                    ]
                    _ = midiOut(AUEventSampleTimeImmediate, 0, 3, &bytes)
                }
            }
            return noErr
        }
    }
}

/// Creates the audio unit for the host.
public final class LoopGenAudioUnitFactory: NSObject, AUAudioUnitFactory {
    public func createAudioUnit(with componentDescription: AudioComponentDescription)
        throws -> AUAudioUnit {
        return try LoopGenAudioUnit(componentDescription: componentDescription)
    }
}
