import AudioToolbox
import SwiftUI
import UIKit

/// The AUv3 extension's principal class. Hosts show this view when the user
/// opens the plugin's interface.
public final class LoopGenViewController: UIViewController {
    public override func viewDidLoad() {
        super.viewDidLoad()
        let host = UIHostingController(rootView: LoopGenAUv3View())
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
    }
}

struct LoopGenAUv3View: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("LoopGen")
                .font(.largeTitle.bold())
            Text("AUv3 MIDI processor")
                .font(.headline)
                .foregroundStyle(.secondary)
            Text("Generates an in-key loop and streams the notes to the host. "
                 + "Root, Scale, Bars and Seed are exposed as parameters, so "
                 + "automate them from the host's plugin view or MIDI mapping.")
                .font(.footnote)
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
