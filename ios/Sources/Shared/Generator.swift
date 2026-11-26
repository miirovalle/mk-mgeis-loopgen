import Foundation

/// One MIDI message positioned at a musical beat.
public struct MIDIEvent {
    public var beat: Double
    public var channel: UInt8
    public var data1: UInt8      // note number
    public var data2: UInt8      // velocity, 0 for note-off
    public var isNoteOn: Bool
}

public enum ScaleMode: Int, CaseIterable, Identifiable {
    case minor = 0
    case major = 1

    public var id: Int { rawValue }
    public var name: String { self == .minor ? "Minor" : "Major" }

    /// Semitone offsets of the natural scale from its tonic.
    public var intervals: [Int] {
        self == .major ? [0, 2, 4, 5, 7, 9, 11] : [0, 2, 3, 5, 7, 8, 10]
    }
}

public struct LoopSpec {
    public var rootPC: Int
    public var mode: ScaleMode
    public var bars: Int
    public var seed: UInt64
    public var bpm: Double

    public init(rootPC: Int = 9, mode: ScaleMode = .minor, bars: Int = 16,
                seed: UInt64 = 1, bpm: Double = 120) {
        self.rootPC = rootPC
        self.mode = mode
        self.bars = bars
        self.seed = seed
        self.bpm = bpm
    }

    public var keyName: String {
        LoopGenerator.pitchNames[rootPC] + (mode == .major ? " major" : " minor")
    }
}

/// Deterministic RNG so a seed always reproduces the same loop.
public struct SplitMix64: RandomNumberGenerator {
    private var state: UInt64

    public init(seed: UInt64) {
        state = seed &+ 0x9E37_79B9_7F4A_7C15
    }

    public mutating func next() -> UInt64 {
        state = state &+ 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    public mutating func int(_ range: ClosedRange<Int>) -> Int {
        let span = UInt64(range.upperBound - range.lowerBound + 1)
        return range.lowerBound + Int(next() % span)
    }

    public mutating func chance(_ probability: Double) -> Bool {
        Double(next() % 1000) / 1000.0 < probability
    }
}

/// Generates in-key 4/8/16-bar MIDI loops (bass, chords, melody) arranged in
/// 4-bar phrases with an intro, a fill and a closing break.
public enum LoopGenerator {
    public static let pitchNames = ["C", "C#", "D", "D#", "E", "F", "F#",
                                    "G", "G#", "A", "A#", "B"]

    public struct Chord {
        public let degree: Int
        public let name: String
        public let tones: [Int]
    }

    static let roman = ["I", "II", "III", "IV", "V", "VI", "VII"]

    /// i - VI - III - VII in minor, I - V - vi - IV in major.
    public static func progression(for mode: ScaleMode) -> [Int] {
        mode == .minor ? [0, 5, 2, 6] : [0, 4, 5, 3]
    }

    public static func chord(_ degree: Int) -> Chord {
        let d = ((degree % 7) + 7) % 7
        return Chord(degree: d, name: roman[d], tones: [d, (d + 2) % 7, (d + 4) % 7])
    }

    /// Absolute MIDI note for a scale degree; octave 5 sits around middle C.
    public static func note(rootPC: Int, mode: ScaleMode, degree: Int,
                            octave: Int) -> UInt8 {
        let wrapped = ((degree % 7) + 7) % 7
        let octaveShift = Int(floor(Double(degree) / 7.0))
        let value = rootPC + mode.intervals[wrapped] + 12 * (octave + octaveShift)
        return UInt8(min(127, max(0, value)))
    }

    public static func chordProgression(_ spec: LoopSpec) -> [String] {
        let prog = progression(for: spec.mode)
        return (0..<spec.bars).map { chord(prog[$0 % prog.count]).name }
    }

    public static func generate(_ spec: LoopSpec) -> [MIDIEvent] {
        var rng = SplitMix64(seed: spec.seed)
        var events: [MIDIEvent] = []
        let bars = max(4, spec.bars)
        let phrases = bars / 4
        let prog = progression(for: spec.mode)

        func add(_ beat: Double, _ note: UInt8, velocity: UInt8, duration: Double,
                 channel: UInt8) {
            events.append(MIDIEvent(beat: beat, channel: channel, data1: note,
                                    data2: velocity, isNoteOn: true))
            events.append(MIDIEvent(beat: beat + duration, channel: channel,
                                    data1: note, data2: 0, isNoteOn: false))
        }

        for bar in 0..<bars {
            let phrase = bar / 4
            let barInPhrase = bar % 4
            let isLastPhrase = phrase == phrases - 1
            let isBreakBar = isLastPhrase && barInPhrase == 3 && phrases > 1
            if isBreakBar { continue }

            let harmony = chord(prog[bar % prog.count])
            let barStart = Double(bar) * 4.0
            let playMelody = phrase > 0 || phrases == 1

            // bass: root on 1, fifth or root on 3, occasional pick-up
            let root = note(rootPC: spec.rootPC, mode: spec.mode,
                            degree: harmony.degree, octave: 2)
            let fifth = note(rootPC: spec.rootPC, mode: spec.mode,
                             degree: harmony.degree + 4, octave: 2)
            add(barStart, root, velocity: 96, duration: 1.5, channel: 0)
            add(barStart + 2.0, rng.chance(0.6) ? fifth : root,
                velocity: 88, duration: 1.0, channel: 0)
            if rng.chance(0.5) {
                add(barStart + 3.5, root, velocity: 72, duration: 0.4, channel: 0)
            }

            // chords: block triads on 1 and 3
            for offset in stride(from: 0.0, to: 4.0, by: 2.0) {
                for tone in harmony.tones {
                    let pitch = note(rootPC: spec.rootPC, mode: spec.mode,
                                     degree: tone, octave: 4)
                    add(barStart + offset, pitch, velocity: 80, duration: 1.6,
                        channel: 1)
                }
            }

            // melody: chord-tone arpeggio over 8ths, with rests
            if playMelody {
                let step = rng.chance(0.5) ? 0.5 : 1.0
                var t = 0.0
                while t < 4.0 {
                    if rng.chance(0.75) {
                        let tone = harmony.tones[rng.int(0...2)]
                        let degree = tone + (rng.chance(0.25) ? 7 : 0)
                        let pitch = note(rootPC: spec.rootPC, mode: spec.mode,
                                         degree: degree, octave: 5)
                        add(barStart + t, pitch,
                            velocity: UInt8(rng.int(70...110)),
                            duration: step * 0.9, channel: 2)
                    }
                    t += step
                    if rng.chance(0.15) { t += step }
                }
            }

            // fill in the last bar of the phrase before the closing one
            if barInPhrase == 3 && phrase == phrases - 2 && phrases > 1 {
                var t = 3.0
                while t < 4.0 {
                    let pitch = note(rootPC: spec.rootPC, mode: spec.mode,
                                     degree: harmony.tones[rng.int(0...2)],
                                     octave: 5)
                    add(barStart + t, pitch, velocity: UInt8(rng.int(80...118)),
                        duration: 0.2, channel: 2)
                    t += 0.25
                }
            }
        }

        return events.sorted { lhs, rhs in
            if lhs.beat != rhs.beat { return lhs.beat < rhs.beat }
            return lhs.isNoteOn && !rhs.isNoteOn
        }
    }

    // MARK: - Standard MIDI file

    public static func standardMIDIFile(_ events: [MIDIEvent], spec: LoopSpec) -> Data {
        let ppq = 480
        var track = Data()

        func variableLength(_ value: Int) -> [UInt8] {
            var bytes = [UInt8(value & 0x7F)]
            var v = value >> 7
            while v > 0 {
                bytes.insert(UInt8((v & 0x7F) | 0x80), at: 0)
                v >>= 7
            }
            return bytes
        }

        func append(delta: Int, bytes: [UInt8]) {
            track.append(contentsOf: variableLength(max(0, delta)))
            track.append(contentsOf: bytes)
        }

        let name = Array("LoopGen \(spec.keyName)".utf8)
        append(delta: 0, bytes: [0xFF, 0x03] + variableLength(name.count) + name)
        let micros = Int(60_000_000.0 / max(20.0, spec.bpm))
        append(delta: 0, bytes: [0xFF, 0x51, 0x03,
                                 UInt8((micros >> 16) & 0xFF),
                                 UInt8((micros >> 8) & 0xFF),
                                 UInt8(micros & 0xFF)])
        append(delta: 0, bytes: [0xFF, 0x58, 0x04, 4, 2, 24, 8])
        append(delta: 0, bytes: [0xFF, 0x59, 0x02,
                                 UInt8(bitPattern: Int8(accidentals(spec))),
                                 spec.mode == .major ? 0 : 1])

        let programs: [(UInt8, UInt8)] = [(0, 38), (1, 4), (2, 73)]
        for (channel, program) in programs {
            append(delta: 0, bytes: [0xC0 | channel, program])
        }

        var lastTick = 0
        for event in events {
            let tick = Int((event.beat * Double(ppq)).rounded())
            let delta = tick - lastTick
            if event.isNoteOn {
                append(delta: delta,
                       bytes: [0x90 | event.channel, event.data1, event.data2])
            } else {
                append(delta: delta,
                       bytes: [0x80 | event.channel, event.data1, 0])
            }
            lastTick = tick
        }
        append(delta: 0, bytes: [0xFF, 0x2F, 0x00])

        var file = Data()
        file.append(contentsOf: Array("MThd".utf8))
        file.append(contentsOf: [0, 0, 0, 6, 0, 0, 0, 1,
                                 UInt8((ppq >> 8) & 0xFF), UInt8(ppq & 0xFF)])
        file.append(contentsOf: Array("MTrk".utf8))
        let length = track.count
        file.append(contentsOf: [UInt8((length >> 24) & 0xFF),
                                 UInt8((length >> 16) & 0xFF),
                                 UInt8((length >> 8) & 0xFF),
                                 UInt8(length & 0xFF)])
        file.append(track)
        return file
    }

    /// Sharps (positive) or flats (negative) for the key, counted from its
    /// relative major, which is how the MIDI key-signature meta event encodes it.
    static func accidentals(_ spec: LoopSpec) -> Int {
        let relativeMajor = spec.mode == .major ? spec.rootPC : (spec.rootPC + 3) % 12
        let table: [Int: Int] = [0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6,
                                 1: -5, 8: -4, 3: -3, 10: -2, 5: -1]
        return table[relativeMajor] ?? 0
    }
}
