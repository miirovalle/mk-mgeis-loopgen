"""Tests for loopgen 3. Run with: python -m unittest discover -s tests"""
import json
import os
import random
import shutil
import sys
import tempfile
import unittest

import mido

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import loopgen as L  # noqa: E402


def write_loop(path, chords, bars=4, tpb=480, root_octave=4, role_notes=None):
    """Write a 4/4 loop whose bars follow the given triads."""
    mid = mido.MidiFile(ticks_per_beat=tpb)
    mid.type = 1
    track = mido.MidiTrack()
    mid.tracks.append(track)
    events = []
    for bar, (root, mode) in enumerate(chords):
        base = root_octave * 12 + root
        for pitch in L.triads(root, mode):
            start = (bar * 4) * tpb
            for i, tick in enumerate((0, 4 * tpb)):
                events.append((start + tick, "note_on" if i == 0 else "note_off",
                               base + pitch))
    events.sort(key=lambda e: e[0])
    last = 0
    for tick, kind, pitch in events:
        track.append(mido.Message(kind, note=pitch, velocity=90 if kind == "note_on" else 0,
                                  time=tick - last))
        last = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(path)


def build_library(root):
    """A tiny library: A minor and C major (same scale) plus D minor."""
    specs = {
        "A minor": {"Amin_Bass": (9, "min"), "Amin_Chords": (9, "min"),
                    "Amin_Melody": (9, "min")},
        "C major": {"Cmaj_Chords": (0, "maj")},
        "D minor": {"Dmin_Chords": (2, "min")},
    }
    for folder, stems in specs.items():
        d = os.path.join(root, folder)
        os.makedirs(d, exist_ok=True)
        for stem, (root_pc, mode) in stems.items():
            chords = [(root_pc, mode)] * 2 + [((root_pc + 5) % 12, "maj"),
                                              ((root_pc + 7) % 12, "maj")]
            write_loop(os.path.join(d, stem + ".mid"), chords)
    return root


class ChordTests(unittest.TestCase):
    def test_estimate_major_triad(self):
        hist = [0] * 12
        for pc in (0, 4, 7):
            hist[pc] = 10
        self.assertEqual(L.estimate_chord(hist), (0, "maj"))

    def test_estimate_minor_triad(self):
        hist = [0] * 12
        for pc in (9, 0, 4):
            hist[pc] = 10
        self.assertEqual(L.estimate_chord(hist), (9, "min"))

    def test_compat_identical_and_unrelated(self):
        a = [(0, "maj")] * 4
        self.assertEqual(L.chord_compat(a, a), 1.0)
        # C major vs F# major share no triad tones
        self.assertEqual(L.chord_compat([(0, "maj")], [(6, "maj")]), 0.0)

    def test_compat_partial(self):
        # C major (C E G) vs A minor (A C E) share C and E
        self.assertAlmostEqual(L.chord_compat([(0, "maj")], [(9, "min")]), 0.7)


class KeyTests(unittest.TestCase):
    def test_scale_membership(self):
        self.assertEqual(L.key_pitch_classes("A minor"),
                         {9, 11, 0, 2, 4, 5, 7})
        self.assertEqual(L.key_pitch_classes("C major"),
                         {0, 2, 4, 5, 7, 9, 11})

    def test_relative_keys_share_scale(self):
        keys = {"A minor": {}, "C major": {}, "D minor": {}}
        self.assertEqual(sorted(L.relative_keys(keys, "A minor")),
                         ["A minor", "C major"])

    def test_scale_fit(self):
        hist = [4, 0, 4, 0, 4, 4, 0, 4, 0, 4, 0, 4]  # A minor, no out-of-scale notes
        self.assertEqual(L.scale_fit(hist, L.key_pitch_classes("A minor")), 1.0)
        self.assertLess(L.scale_fit([1] * 12, L.key_pitch_classes("A minor")), 1.0)


class PlanTests(unittest.TestCase):
    def test_auto_forms(self):
        roles = ["bass", "chords", "melody"]
        self.assertEqual(len(L.make_plan(1, roles, "auto", True)), 1)
        two = L.make_plan(2, roles, "auto", True)
        self.assertNotIn("mute", two[0])
        self.assertIn("mute", two[1])
        four = L.make_plan(4, roles, "auto", True)
        self.assertEqual(len(four), 4)
        self.assertIn("fill", four[2])
        self.assertIn("mute", four[3])

    def test_intro_drops_melody(self):
        roles = ["bass", "chords", "melody"]
        four = L.make_plan(4, roles, "auto", True)
        self.assertNotIn("melody", four[0]["roles"])

    def test_loop_form_has_no_breaks(self):
        plan = L.make_plan(4, ["bass", "chords"], "loop", True)
        self.assertTrue(all("mute" not in p for p in plan))

    def test_build_form_layers_parts(self):
        roles = ["bass", "chords", "arp", "melody"]
        plan = L.make_plan(4, roles, "build", True)
        sizes = [len(p["roles"]) for p in plan]
        self.assertEqual(sizes, sorted(sizes))


class GenerateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.lib = build_library(os.path.join(cls.tmp, "lib"))
        cls.keys, _ = L.build_index(cls.lib, os.path.join(cls.lib, "_loop_index.json"), True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _gen(self, bars, seed=1, form="auto", out=None):
        args = type("A", (), {})()
        args.min_fit = 0.85
        args.variation = "per_phrase"
        args.form = form
        args.bpm = 120
        args.seed = seed
        args.no_relative = False
        args.no_breaks = False
        args.no_fills = False
        args.no_programs = True
        args.recipe = True
        args.stems = False
        args.dry_run = False
        args.library = self.lib
        args.out = out or os.path.join(self.tmp, "out")
        return L.generate(self.keys, "A minor", bars, ["bass", "chords", "melody"],
                          args, random.Random(seed), 1)

    def test_exact_lengths(self):
        for bars in (4, 8, 16):
            path = self._gen(bars)
            mid = mido.MidiFile(path)
            span = max(sum(m.time for m in tr) for tr in mid.tracks)
            self.assertEqual(span, bars * L.BEATS_PER_BAR * mid.ticks_per_beat,
                             "bars=%d" % bars)

    def test_key_and_markers(self):
        path = self._gen(16)
        mid = mido.MidiFile(path)
        meta = [m for m in mid.tracks[0] if m.is_meta]
        self.assertTrue(any(m.type == "key_signature" for m in meta))
        markers = [m for m in meta if m.type == "marker"]
        self.assertEqual(len(markers), 4)

    def test_deterministic(self):
        a = self._gen(8, seed=7, out=os.path.join(self.tmp, "d1"))
        b = self._gen(8, seed=7, out=os.path.join(self.tmp, "d2"))
        with open(a, "rb") as f1, open(b, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_break_bar_is_silent(self):
        path = self._gen(8)
        mid = mido.MidiFile(path)
        bar = mid.ticks_per_beat * L.BEATS_PER_BAR
        window = range(7 * bar, 8 * bar)
        onsets = 0
        for tr in mid.tracks[1:]:
            t = 0
            for m in tr:
                t += m.time
                if m.type == "note_on" and m.velocity > 0 and t in window:
                    onsets += 1
        self.assertEqual(onsets, 0)

    def test_recipe_lists_sources(self):
        path = self._gen(8)
        rec = json.load(open(path[:-4] + ".recipe.json", encoding="utf-8"))
        self.assertEqual(rec["version"], L.VERSION)
        self.assertEqual(len(rec["phrases"]), 2)
        self.assertIn("parts", rec["phrases"][0])

    def test_no_notes_past_end(self):
        path = self._gen(16)
        mid = mido.MidiFile(path)
        span = max(sum(m.time for m in tr) for tr in mid.tracks)
        for tr in mid.tracks[1:]:
            t = 0
            for m in tr:
                t += m.time
                self.assertGreaterEqual(m.time, 0)
                if m.type == "note_on" and m.velocity > 0:
                    self.assertLess(t, span)


if __name__ == "__main__":
    unittest.main()
