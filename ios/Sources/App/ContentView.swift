import SwiftUI

struct ContentView: View {
    @StateObject private var transport = LoopTransport()

    @State private var rootPC = 9
    @State private var mode: ScaleMode = .minor
    @State private var bars = 16
    @State private var seed = 1.0
    @State private var exportURL: URL?
    @State private var status = ""

    private var spec: LoopSpec {
        LoopSpec(rootPC: rootPC, mode: mode, bars: bars,
                 seed: UInt64(max(0, seed)), bpm: 120)
    }

    private var chords: [String] {
        LoopGenerator.chordProgression(spec)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Key and scale") {
                    Picker("Root", selection: $rootPC) {
                        ForEach(0..<12, id: \.self) { index in
                            Text(LoopGenerator.pitchNames[index]).tag(index)
                        }
                    }
                    Picker("Scale", selection: $mode) {
                        ForEach(ScaleMode.allCases) { scale in
                            Text(scale.name).tag(scale)
                        }
                    }
                }

                Section("Length") {
                    Picker("Bars", selection: $bars) {
                        Text("4").tag(4)
                        Text("8").tag(8)
                        Text("16").tag(16)
                    }
                    .pickerStyle(.segmented)
                    Stepper("Seed  \(Int(seed))", value: $seed, in: 0...9999)
                }

                Section("Progression") {
                    Text(chords.joined(separator: "  "))
                        .font(.system(.body, design: .monospaced))
                    Text("\(spec.keyName), \(bars) bars, bass + chords + melody")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("MIDI out") {
                    Button(transport.isRunning ? "Stop" : "Play") {
                        if transport.isRunning {
                            transport.stop()
                        } else {
                            play()
                        }
                    }
                    Toggle("Send MIDI", isOn: $transport.useMIDIOut)
                    Picker("Destination", selection: $transport.selectedTargetID) {
                        ForEach(transport.targets) { target in
                            Text(target.name).tag(target.id)
                        }
                    }
                    .disabled(transport.targets.isEmpty)
                    Toggle("MIDI thru", isOn: $transport.midiThru)
                    Text("\(transport.targets.count) destination(s) found. "
                         + "MIDI thru forwards incoming MIDI to the output.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("RTP-MIDI (network)") {
                    Toggle("Enable network session", isOn: $transport.networkEnabled)
                    TextField("Session name", text: $transport.networkName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Picker("Allow", selection: $transport.networkPolicy) {
                        ForEach(MIDINetworkPolicy.allCases) { policy in
                            Text(policy.name).tag(policy)
                        }
                    }
                    Text("Connect from Audio MIDI Setup (macOS) or another MIDI "
                         + "Network session on the same network.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Export") {
                    Button("Render .mid") { exportMIDI() }
                    if let exportURL {
                        ShareLink(item: exportURL) {
                            Label("Share \(exportURL.lastPathComponent)",
                                  systemImage: "square.and.arrow.up")
                        }
                    }
                    if !status.isEmpty {
                        Text(status).font(.footnote).foregroundStyle(.secondary)
                    }
                }

                Section("AUv3 plugin") {
                    Text("LoopGen is also an AUv3 MIDI processor. In a host such as "
                         + "AUM, Drambo or GarageBand, add it as a MIDI effect and it "
                         + "streams generated notes to the track.")
                        .font(.footnote)
                }
            }
            .navigationTitle("LoopGen 3")
            .onChange(of: rootPC) { _ in syncSpec() }
            .onChange(of: mode) { _ in syncSpec() }
            .onChange(of: bars) { _ in syncSpec() }
            .onChange(of: seed) { _ in syncSpec() }
        }
    }

    private func play() {
        transport.spec = spec
        transport.start()
    }

    private func syncSpec() {
        let wasRunning = transport.isRunning
        if wasRunning { transport.stop() }
        transport.spec = spec
        if wasRunning { transport.start() }
    }

    private func exportMIDI() {
        let events = LoopGenerator.generate(spec)
        let data = LoopGenerator.standardMIDIFile(events, spec: spec)
        let name = "LoopGen_\(spec.rootPC)_"
            + "\(mode == .major ? "maj" : "min")_\(bars)bar_\(Int(seed)).mid"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do {
            try data.write(to: url)
            exportURL = url
            status = "\(events.count / 2) notes, \(data.count) bytes"
        } catch {
            status = "Export failed: \(error.localizedDescription)"
        }
    }
}

#Preview {
    ContentView()
}
