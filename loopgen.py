#!/usr/bin/env python
"""loopgen 3 - arrange 4 / 8 / 16 bar MIDI loops from a key-sorted loop library.

What v3 adds over v2
  * per-bar chord estimation for every loop, and harmonic-compatible part picking
    (parts that agree chord-for-chord are preferred over random pairings)
  * form templates: --form auto|loop|build|song|dropout
  * relative-key pooling: A minor may borrow loops filed under C major (same scale)
  * section marker events and a key signature in the output
  * --version

Every part of a generated loop comes from one key (and optionally its relative),
so the result is always a single key/scale.

Examples
  python loopgen.py --list-keys
  python loopgen.py --key "A minor" --bars all --out generated
  python loopgen.py --key "A minor" --bars 16 --form song --count 3 --seed 7
  python loopgen.py --all-keys --bars 8 --variation per_phrase --recipe
"""
import argparse
import collections
import json
import math
import os
import random
import re
import statistics
import sys

import mido

VERSION = "3.0"

OUT_PPQ = 480
BEATS_PER_BAR = 4
PHRASE_BARS = 4
CHUNK_BEATS = PHRASE_BARS * BEATS_PER_BAR
DEFAULT_PARTS = "bass,chords,melody"
INDEX_NAME = "_loop_index.json"
INDEX_VERSION = 6
SLICE_SUFFIX = re.compile(r" \[\d{2}\]$")
BPM_RE = re.compile(r"(\d{2,3})\s*bpm", re.I)
PML_BPM_RE = re.compile(r"^pml[ _-](\d{2,3})[ _-]", re.I)

ROLE_WORDS = [
    ("perc", ("perc", "percs", "drums", "drum", "drm", "kick", "snare", "hat", "hats",
              "clap", "snap", "tom", "shaker", "rim", "cymbal", "crash", "tambourine")),
    ("bass", ("bass", "sub", "808", "bassline")),
    ("arp", ("arp", "arps", "arpeggio", "arpeggiator")),
    ("pad", ("pad", "pads", "string", "strings", "choir", "brass", "horn")),
    ("chords", ("chord", "chords", "stab", "stabs", "keys", "piano", "organ", "rhodes")),
    ("melody", ("melody", "mel", "lead", "vox", "vocal", "flute", "bell", "pluck",
                "synth", "guitar", "sax", "trumpet", "whistle")),
]

GM_PROGRAM = {"bass": 38, "chords": 4, "pad": 89, "arp": 81, "melody": 73, "perc": 0}

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# MIDI spells major keys with flats and minor keys with sharps; mido rejects the rest.
MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
PC_INDEX = {n: i for i, n in enumerate(PITCH_NAMES)}
PC_INDEX.update({"DB": 1, "EB": 3, "GB": 6, "AB": 8, "BB": 10, "CB": 11})

TRIAD = {"maj": (0, 4, 7), "min": (0, 3, 7)}
CHORD_WEIGHT = {0: 1.0, 4: 0.9, 7: 0.8, 3: 0.9}


# --------------------------------------------------------------------------- key

def key_pitch_classes(folder):
    """Pitch-class set implied by a folder name like 'A minor' / 'Bb major'."""
    m = re.match(r"^(\S+)\s+(major|minor)$", folder)
    if not m:
        return None
    pc = PC_INDEX.get(m.group(1).upper())
    if pc is None:
        return None
    if m.group(2) == "major":
        return {(pc + i) % 12 for i in (0, 2, 4, 5, 7, 9, 11)}
    return {(pc + i) % 12 for i in (0, 2, 3, 5, 7, 8, 10)}


def relative_keys(keys, key):
    """Key folders sharing the exact same scale, e.g. A minor and C major."""
    pcs = key_pitch_classes(key)
    if not pcs:
        return [key]
    return [key] + [k for k in sorted(keys)
                    if k != key and key_pitch_classes(k) == pcs]


def scale_fit(hist, pcs):
    """Fraction of a loop's notes that sit inside the given pitch-class set."""
    total = sum(hist)
    if not total or not pcs:
        return 0.0
    return sum(hist[i] for i in pcs) / total


# ------------------------------------------------------------------------- chords

def triads(root, mode):
    return {(root + i) % 12 for i in TRIAD[mode]}


def estimate_chord(hist):
    """Best (root, mode) triad for a pitch-class histogram."""
    total = sum(hist)
    if total <= 0:
        return (0, "maj")
    best, best_score = (0, "maj"), -1.0
    for root in range(12):
        for mode in ("maj", "min"):
            score = 0.0
            for rel, weight in CHORD_WEIGHT.items():
                if rel in TRIAD[mode]:
                    score += hist[(root + rel) % 12] * weight
            score -= 0.05 * (total - sum(hist[(root + i) % 12] for i in TRIAD[mode]))
            if score > best_score:
                best, best_score = (root, mode), score
    return best


def chord_compat(a, b):
    """0..1 agreement between two per-bar chord sequences."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    total = 0.0
    for i in range(n):
        (r1, m1), (r2, m2) = a[i], b[i]
        if r1 == r2 and m1 == m2:
            total += 1.0
        elif r1 == r2:
            total += 0.6
        else:
            shared = len(triads(r1, m1) & triads(r2, m2))
            total += {0: 0.0, 1: 0.3, 2: 0.7}.get(shared, 0.0)
    return total / n


def chord_name(chord):
    root, mode = chord
    return PITCH_NAMES[root] + ("" if mode == "maj" else "m")


# --------------------------------------------------------------------------- index

def classify(stem, avg_pitch, poly):
    """Role of a loop from its name, matched per token (not substring)."""
    toks = set(t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t)
    for role, words in ROLE_WORDS:
        if toks & set(words):
            return role
    if avg_pitch and avg_pitch < 48:
        return "bass"
    if poly and poly >= 3:
        return "chords"
    return "melody"


def detect_bpm(stem):
    m = BPM_RE.search(stem)
    if m:
        return int(m.group(1))
    m = PML_BPM_RE.match(stem.strip())
    if m and 50 <= int(m.group(1)) <= 200:
        return int(m.group(1))
    return None


def read_loop(path):
    """(beats, notes, cc_events, max_polyphony) for one file, in beat units."""
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat
    notes, ccs = [], []
    end_ticks = 0
    max_poly = 0
    for tr in mid.tracks:
        t = 0
        open_notes = collections.defaultdict(list)
        sounding = 0
        for msg in tr:
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes[msg.note].append((t, msg.velocity))
                sounding += 1
                max_poly = max(max_poly, sounding)
            elif msg.type in ("note_off", "note_on"):
                q = open_notes.get(msg.note)
                if q:
                    st, vel = q.pop(0)
                    notes.append((st / tpb, t / tpb, msg.note, vel))
                    sounding = max(0, sounding - 1)
            elif msg.type in ("control_change", "pitchwheel"):
                ccs.append((t / tpb, msg))
        end_ticks = max(end_ticks, t)
        for pitch, q in open_notes.items():
            for st, vel in q:
                notes.append((st / tpb, end_ticks / tpb, pitch, vel))
    return end_ticks / tpb, notes, ccs, max_poly


def bar_histograms(notes, beats, bars):
    """Pitch-class histogram per bar."""
    out = [[0] * 12 for _ in range(bars)]
    for st, en, pitch, vel in notes:
        i = min(int(st // BEATS_PER_BAR), bars - 1)
        weight = max(vel, 1) * max(min(en - st, BEATS_PER_BAR), 0.25)
        out[i][pitch % 12] += weight
    return out


def build_index(library, cache_path, rebuild):
    if not rebuild and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("version") == INDEX_VERSION:
            return cached["keys"], cached.get("skipped", [])

    keys, skipped = {}, []
    for entry in sorted(os.listdir(library)):
        folder = os.path.join(library, entry)
        if not os.path.isdir(folder):
            continue
        by_stem = collections.defaultdict(list)
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(".mid"):
                by_stem[SLICE_SUFFIX.sub("", f[:-4])].append(f)

        stems = {}
        for stem, files in by_stem.items():
            chunks = []
            n_notes = 0
            pitches = []
            max_poly = 0
            for f in files:
                try:
                    beats, notes, _ccs, poly = read_loop(os.path.join(folder, f))
                except Exception as exc:  # noqa: BLE001
                    skipped.append("%s/%s: %s" % (entry, f, exc))
                    continue
                if beats <= 0 or not notes:
                    continue
                if abs(beats - round(beats)) > 1e-6 or round(beats) % BEATS_PER_BAR:
                    skipped.append("%s/%s: %s beats is not whole 4/4 bars"
                                   % (entry, f, beats))
                    continue
                bars = int(round(beats)) // BEATS_PER_BAR
                chords = [estimate_chord(h) for h in bar_histograms(notes, beats, bars)]
                chunks.append({
                    "file": os.path.join(entry, f),
                    "bars": bars,
                    "chords": [[c[0], c[1]] for c in chords],
                    "notes": len(notes),
                })
                n_notes += len(notes)
                pitches.extend(n[2] for n in notes)
                max_poly = max(max_poly, poly)
            if not chunks or n_notes == 0:
                continue
            avg = sum(pitches) / len(pitches)
            hist = [0] * 12
            for p in pitches:
                hist[p % 12] += 1
            stems[stem] = {
                "folder": entry,
                "chunks": chunks,
                "role": classify(stem, avg, max_poly),
                "bars": max(c["bars"] for c in chunks),
                "notes": n_notes,
                "avg_pitch": round(avg, 1),
                "bpm": detect_bpm(stem),
                "hist": hist,
            }
        if stems:
            keys[entry] = stems

    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump({"version": INDEX_VERSION, "keys": keys, "skipped": skipped}, fh)
    return keys, skipped


# ------------------------------------------------------------------------ render

def load_chunk(path):
    """Notes and CCs for one 4-bar chunk, retiled or trimmed to exactly 16 beats."""
    beats, notes, ccs, _poly = read_loop(path)
    if beats <= 0:
        return [], []
    out_notes, out_ccs = [], []
    if beats >= CHUNK_BEATS:
        for st, en, pitch, vel in notes:
            if st < CHUNK_BEATS:
                out_notes.append((st, min(en, CHUNK_BEATS), pitch, vel))
        out_ccs = [(st, m) for st, m in ccs if st < CHUNK_BEATS]
    else:
        for rep in range(int(math.ceil(CHUNK_BEATS / beats))):
            off = rep * beats
            for st, en, pitch, vel in notes:
                s = off + st
                if s < CHUNK_BEATS:
                    out_notes.append((s, min(off + en, CHUNK_BEATS), pitch, vel))
            for st, m in ccs:
                s = off + st
                if s < CHUNK_BEATS:
                    out_ccs.append((s, m))
    return out_notes, out_ccs


def render_chunk(notes, ccs, dest_beat, channel, mute=None):
    """(tick, priority, message) events for one chunk placed at dest_beat."""
    pieces = []
    for st, en, pitch, vel in notes:
        if mute and mute[0] <= st < mute[1]:
            continue
        s = dest_beat + st
        e = dest_beat + en
        if mute and st < mute[0] < en:
            e = min(e, dest_beat + mute[0])
        if e <= s:
            e = s + 1e-6
        pieces.append((s * OUT_PPQ, 2, mido.Message(
            "note_on", channel=channel, note=pitch, velocity=vel)))
        pieces.append((e * OUT_PPQ, 3 if e <= s else 1, mido.Message(
            "note_off", channel=channel, note=pitch, velocity=0)))
    for st, msg in ccs:
        pieces.append(((dest_beat + st) * OUT_PPQ, 0, msg.copy(channel=channel)))
    return pieces


# ----------------------------------------------------------------------- arranging

FORM_LABELS = ("A", "B", "C", "D", "E", "F", "G", "H")


def make_plan(n_phrases, roles, form, breaks):
    """[{roles, label, mute?, fill?}] - what each 4-bar phrase plays."""
    intro = [r for r in roles if r in ("bass", "chords", "pad", "arp")] or list(roles)
    last_mute = (CHUNK_BEATS - BEATS_PER_BAR, CHUNK_BEATS) if breaks else None

    def phrase(rs, label, mute=None, fill=False):
        row = {"roles": list(rs), "label": label, "fill": fill}
        if mute:
            row["mute"] = mute
        return row

    if form == "loop" or n_phrases <= 1:
        return [phrase(roles, FORM_LABELS[i % len(FORM_LABELS)]) for i in range(n_phrases)]

    if form == "build":
        # add one part per phrase; every phrase is a superset of the last
        plan = []
        for i in range(n_phrases):
            k = max(1, int(round(len(roles) * (i + 1) / float(n_phrases))))
            plan.append(phrase(roles[:k], FORM_LABELS[i % len(FORM_LABELS)]))
        if last_mute:
            plan[-1]["mute"] = last_mute
        return plan

    if form == "dropout":
        plan = []
        for i in range(n_phrases):
            sparse = i % 2 == 1
            plan.append(phrase(intro if sparse else roles,
                               FORM_LABELS[i % len(FORM_LABELS)]))
        if last_mute:
            plan[-1]["mute"] = last_mute
        return plan

    if form == "song":
        if n_phrases == 2:
            return [phrase(roles, "A"), phrase(roles, "B", mute=last_mute)]
        plan = [phrase(intro, "A"), phrase(roles, "B"), phrase(roles, "C", fill=True),
                phrase(roles, "D", mute=last_mute)]
        while len(plan) < n_phrases:
            plan.insert(len(plan) - 1, phrase(roles, FORM_LABELS[len(plan) % 8]))
        return plan[:n_phrases]

    # auto: differ by length
    if n_phrases == 2:
        return [phrase(roles, "A"), phrase(roles, "B", mute=last_mute)]
    plan = [phrase(intro, "A"), phrase(roles, "B"), phrase(roles, "C", fill=True),
            phrase(roles, "D", mute=last_mute)]
    while len(plan) < n_phrases:
        plan.insert(len(plan) - 1, phrase(roles, FORM_LABELS[len(plan) % 8]))
    return plan[:n_phrases]


def arrange(roles, stems, pcs, min_fit, rng, n_phrases, variation, form, breaks,
            fills, library, candidates=40):
    """(parts_data, recipe, plan, bpms) - harmonically aware arrangement."""
    role_pool = {}
    for role in roles:
        pool = [s for s, info in stems.items()
                if info["role"] == role
                and (not pcs or scale_fit(info["hist"], pcs) >= min_fit)]
        if not pool:
            pool = [s for s, info in stems.items() if info["role"] == role]
        role_pool[role] = pool

    active = [r for r in roles if role_pool.get(r)]
    plan = make_plan(n_phrases, active, form, breaks)
    channels = [c for c in range(16) if c != 9]
    channel_of = {role: channels[i % len(channels)] for i, role in enumerate(active)}

    fixed_choice, section_choice, prev = {}, {}, {}
    used = collections.defaultdict(set)
    pieces = collections.defaultdict(list)
    recipe = []

    for p, phrase in enumerate(plan):
        offset = p * CHUNK_BEATS
        entry = {"phrase": p + 1, "section": phrase["label"],
                 "bars": "%d-%d" % (p * PHRASE_BARS + 1, (p + 1) * PHRASE_BARS),
                 "parts": {}, "chords": {}}
        chosen_here = []

        for role in phrase["roles"]:
            pool = role_pool.get(role)
            if not pool:
                continue

            pinned = None
            if variation == "fixed":
                if role not in fixed_choice:
                    fixed_choice[role] = rng.choice(pool)
                pinned = fixed_choice[role]
            elif variation == "per_section":
                slot = (role, p // 2)
                if slot not in section_choice:
                    section_choice[slot] = rng.choice(
                        [s for s in pool if s != section_choice.get((role, p // 2 - 1))]
                        or pool)
                pinned = section_choice[slot]

            if pinned:
                stem = pinned
                files = stems[stem]["chunks"]
                chunk = (p % len(files)) if variation == "fixed" else \
                    ((p % 2) % len(files))
            else:
                options = [s for s in pool if s != prev.get(role)] or pool
                sample = (rng.sample(options, min(candidates, len(options)))
                          if len(options) > candidates else options)
                scored = []
                for cand in sample:
                    chunks = stems[cand]["chunks"]
                    for ci, ch in enumerate(chunks):
                        score = statistics.fmean(
                            [chord_compat(ch["chords"], other) for other in chosen_here]) \
                            if chosen_here else 0.0
                        scored.append((score, rng.random(), cand, ci))
                scored.sort(key=lambda x: (-x[0], x[1]))
                top = scored[:max(1, min(5, len(scored)))]
                _, _, stem, chunk = rng.choice(top)
                files = stems[stem]["chunks"]
            prev[role] = stem

            chunk_info = files[chunk]
            notes, ccs = load_chunk(os.path.join(library, chunk_info["file"]))
            ch = channel_of[role]
            mute = phrase.get("mute")
            pieces[role].extend(render_chunk(notes, ccs, offset, ch, mute=mute))

            used[role].add(stem)
            entry["parts"][role] = stem
            entry["chords"][role] = [chord_name(tuple(c)) for c in chunk_info["chords"]]
            entry.setdefault("files", []).append(
                "%s <- %s" % (role, os.path.basename(chunk_info["file"])))
            chosen_here.append(chunk_info["chords"])
            if mute:
                entry["break"] = "last bar drops out"

            if phrase.get("fill") and fills and len(files) > 1 and \
                    role in ("melody", "arp", "chords"):
                alt = (chunk + 1) % len(files)
                fnotes, _ = load_chunk(os.path.join(library, files[alt]["file"]))
                bar_start = CHUNK_BEATS - BEATS_PER_BAR
                tail = [(st - bar_start, en - bar_start, pitch, vel)
                        for st, en, pitch, vel in fnotes if st >= bar_start]
                pieces[role].extend(render_chunk(tail, [], offset + bar_start, ch))
                entry["fill"] = "last bar from %s" % os.path.basename(files[alt]["file"])
        recipe.append(entry)

    parts_data, bpms = [], []
    for role in active:
        if pieces[role]:
            parts_data.append((role, " + ".join(sorted(used[role])),
                               channel_of[role], pieces[role]))
            for stem in used[role]:
                if stems[stem]["bpm"]:
                    bpms.append(stems[stem]["bpm"])
    return parts_data, recipe, plan, bpms


# ------------------------------------------------------------------------- output

def write_midi(path, parts_data, plan, total_beats, bpm, title, key_name, programs=True):
    mid = mido.MidiFile(ticks_per_beat=OUT_PPQ)
    mid.type = 1

    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name=title, time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4,
                                      clocks_per_click=24, notated_32nd_notes_per_beat=8,
                                      time=0))
    m = re.match(r"^(\S+)\s+(major|minor)$", key_name)
    if m:
        pc = PC_INDEX.get(m.group(1).upper())
        if pc is not None:
            candidate = (MAJOR_NAMES[pc] if m.group(2) == "major"
                         else PITCH_NAMES[pc] + "m")
            try:
                conductor.append(mido.MetaMessage("key_signature", key=candidate, time=0))
            except ValueError:
                pass

    for i, phrase in enumerate(plan):
        conductor.append(mido.MetaMessage(
            "marker", text="%s (bars %d-%d)" % (phrase["label"],
                                                i * PHRASE_BARS + 1,
                                                (i + 1) * PHRASE_BARS),
            time=0 if i == 0 else CHUNK_BEATS * OUT_PPQ))
    conductor.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(conductor)

    span = int(round(total_beats * OUT_PPQ))
    for role, label, channel, pieces in parts_data:
        pieces.sort(key=lambda x: (round(x[0]), x[1]))
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name="%s | %s" % (role, label), time=0))
        if programs and GM_PROGRAM.get(role):
            tr.append(mido.Message("program_change", channel=channel,
                                   program=GM_PROGRAM[role], time=0))
        last = 0
        for tick, _prio, msg in pieces:
            t = max(int(round(tick)), last)
            out = msg.copy()
            out.time = t - last
            last = t
            tr.append(out)
        tr.append(mido.MetaMessage("end_of_track", time=max(0, span - last)))
        mid.tracks.append(tr)

    mid.save(path)


def generate(keys, key, bars, parts, args, rng, index):
    allowed = relative_keys(keys, key) if not args.no_relative else [key]
    stems = {}
    for k in allowed:
        stems.update(keys[k])

    pcs = key_pitch_classes(key)
    total_beats = bars * BEATS_PER_BAR
    parts_data, recipe, plan, bpms = arrange(
        parts, stems, pcs, args.min_fit, rng, bars // PHRASE_BARS, args.variation,
        args.form, not args.no_breaks, not args.no_fills, args.library)

    bpm = args.bpm or (int(statistics.median(bpms)) if bpms else 120)
    safe_key = re.sub(r"[^A-Za-z0-9#]+", "_", key).strip("_")
    base = "%s_%dbar_%02d" % (safe_key, bars, index)
    roles = [r for r, _l, _c, _p in parts_data]
    title = "%s %d bars - %s" % (key, bars, " + ".join(roles))

    print("\n%s @ %d bpm  [%s/%s]" % (title, bpm, args.form, args.variation))
    for p in recipe:
        shown = ", ".join("%s: %s" % (r, s[:34]) for r, s in p["parts"].items())
        flags = (" (break)" if "break" in p else "") + (" (fill)" if "fill" in p else "")
        print("   %s bars %-7s %s%s" % (p["section"], p["bars"], shown[:100], flags))

    if args.dry_run:
        return None

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, base + ".mid")
    write_midi(path, parts_data, plan, total_beats, bpm, title, key,
               programs=not args.no_programs)

    if args.recipe:
        with open(os.path.join(args.out, base + ".recipe.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": VERSION, "key": key, "bars": bars, "bpm": bpm,
                       "seed": args.seed, "form": args.form,
                       "variation": args.variation, "phrases": recipe},
                      fh, indent=2, ensure_ascii=False)

    if args.stems:
        stem_dir = os.path.join(args.out, base + "_stems")
        os.makedirs(stem_dir, exist_ok=True)
        for role, label, ch, pieces in parts_data:
            name = "%s_%s.mid" % (base, re.sub(r"[^A-Za-z0-9]+", "_", role))
            write_midi(os.path.join(stem_dir, name), [(role, label, ch, list(pieces))],
                       plan, total_beats, bpm, "%s | %s" % (base, role), key,
                       programs=not args.no_programs)
    return path


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="loopgen " + VERSION)
    ap.add_argument("--library", default=os.path.join(here, "midi_4bar"))
    ap.add_argument("--out", default=os.path.join(here, "generated"))
    ap.add_argument("--key", nargs="+", default=None, help="key folder(s), e.g. 'A minor'")
    ap.add_argument("--all-keys", action="store_true")
    ap.add_argument("--bars", nargs="+", default=["all"], help="4, 8, 16 or all")
    ap.add_argument("--count", type=int, default=1, help="loops per key and length")
    ap.add_argument("--parts", default=DEFAULT_PARTS)
    ap.add_argument("--form", choices=("auto", "loop", "build", "song", "dropout"),
                    default="auto")
    ap.add_argument("--variation", choices=("per_phrase", "per_section", "fixed"),
                    default="per_phrase")
    ap.add_argument("--bpm", type=int, default=0, help="0 = take it from the loops")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-fit", type=float, default=0.85)
    ap.add_argument("--no-relative", action="store_true",
                    help="do not borrow loops from the relative major/minor folder")
    ap.add_argument("--no-breaks", action="store_true")
    ap.add_argument("--no-fills", action="store_true")
    ap.add_argument("--stems", action="store_true")
    ap.add_argument("--recipe", action="store_true")
    ap.add_argument("--no-programs", action="store_true")
    ap.add_argument("--rebuild-index", action="store_true")
    ap.add_argument("--list-keys", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.library):
        sys.exit("library not found: %s" % args.library)

    keys, skipped = build_index(args.library, os.path.join(args.library, INDEX_NAME),
                                args.rebuild_index)
    if skipped:
        print("index: skipped %d non-loop files" % len(skipped))

    if args.list_keys:
        print("%-22s %6s %6s   %s" % ("key folder", "loops", "slices", "roles"))
        for k in sorted(keys, key=lambda x: -len(keys[x])):
            stems = keys[k]
            roles = collections.Counter(v["role"] for v in stems.values())
            print("%-22s %6d %6d   %s" % (
                k, len(stems), sum(len(v["chunks"]) for v in stems.values()),
                " ".join("%s:%d" % r for r in roles.most_common())))
        return

    def is_real_key(name):
        return not name.startswith("_") and "(mode unknown)" not in name

    wanted = (sorted(k for k in keys if is_real_key(k)) if args.all_keys else (args.key or []))
    if not wanted:
        sys.exit("pass --key 'A minor' (see --list-keys) or --all-keys")
    unknown = [k for k in wanted if k not in keys]
    if unknown:
        sys.exit("unknown key folder(s): %s" % ", ".join(unknown))

    bar_opts = [4, 8, 16] if "all" in args.bars else [int(b) for b in args.bars]
    if any(b % PHRASE_BARS for b in bar_opts):
        sys.exit("bars must be a multiple of %d" % PHRASE_BARS)

    parts = [p.strip().lower() for p in args.parts.split(",") if p.strip()]
    rng_master = random.Random(args.seed)
    made = 0

    for key in wanted:
        for bars in bar_opts:
            for i in range(args.count):
                rng = random.Random(rng_master.random())
                path = generate(keys, key, bars, parts, args, rng, i + 1)
                if path:
                    print("   -> %s" % path)
                    made += 1

    if not args.dry_run:
        print("\nwrote %d loop(s) to %s" % (made, args.out))


if __name__ == "__main__":
    main()
