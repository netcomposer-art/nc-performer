"""Slicer and sampler: manifest, cut points, pitch left alone, gaps named and not filled.

Run: python3 tests/test_slicer.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ncperf import sampler, slicer  # noqa: E402
from ncperf.slicer import (  # noqa: E402
    ATTACK_FADE_S,
    PRE_ROLL_S,
    RING_CAP_S,
    SLICE_FADE_S,
    accept_native,
    assign_recoveries,
    build_chord_manifest,
    build_note_manifest,
    classify_gaps,
    encode_commands,
    identify_region,
    make_chord,
    make_note,
    note_name,
    parse_sfz_regions,
    slice_note,
    strict_regions,
    validate_manifest,
)


def _tone(sr, seconds, freq, amp=0.4):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _peak_hz(audio, sr):
    mono = audio.mean(axis=1) if np.asarray(audio).ndim == 2 else np.asarray(audio)
    # Skip the short attack fade and the end fade so the window is the steady tone.
    a = int(0.02 * sr)
    b = max(a + 16, len(mono) - int(0.05 * sr))
    frame = mono[a:b].astype(np.float64)
    frame = frame - frame.mean()
    n = 1 << 16
    spec = np.abs(np.fft.rfft(frame * np.hanning(len(frame)), n))
    idx = int(np.argmax(spec[1:]) + 1)
    if 0 < idx < len(spec) - 1:
        left, mid, right = spec[idx - 1], spec[idx], spec[idx + 1]
        denom = left - 2 * mid + right
        delta = 0.5 * (left - right) / denom if denom else 0.0
    else:
        delta = 0.0
    return (idx + delta) * sr / n


def _spec(**over):
    spec = {
        "id": "uiowa-guitar",
        "instrument": "guitar",
        "pluck": True,
        "license": "PD-equivalent",
        "page": "https://theremin.music.uiowa.edu/MISguitar.html",
        "author": "University of Iowa Electronic Music Studios (Lawrence Fritts, director)",
        "lo": 40,
        "hi": 88,
        "velocity": ["mf"],
    }
    spec.update(over)
    return spec


def _hand_note(midi=64, slug="guitar"):
    return {
        "midi": midi,
        "name": note_name(midi),
        "file": f"notes/{slug}/{midi}.opus",
        "master": f"notes/{slug}/{midi}.flac",
        "cents": 2.0,
        "cents_method": "fft",
        "dur_s": 1.5,
        "lufs": -32.0,
        "peak_db": -6.0,
        "sr": 44100,
        "channels": 2,
        "source": {
            "url": "https://theremin.music.uiowa.edu/sound%20files/MIS/Piano_Other/guitar/Guitar.mf.sulG.C4B4.stereo.aif",
            "t0": 1.0,
            "t1": 2.5,
            "license": "PD-equivalent",
        },
    }


class SliceBoundsTest(unittest.TestCase):
    def test_preroll_cap_and_fade(self):
        sr = 48000
        onset = 1.0
        # A tone that never falls 60 dB, so the cap is what ends it.
        tone = _tone(sr, 12.0, 440.0)
        audio = np.zeros((int(onset * sr), 1), np.float32)
        audio = np.concatenate([audio, tone[:, None]])
        seg, t0, t1 = slice_note(audio, sr, onset)
        self.assertAlmostEqual(t0, onset - PRE_ROLL_S, places=4)
        self.assertAlmostEqual(t1 - t0, RING_CAP_S, delta=1.0 / sr)
        self.assertEqual(seg.shape[0], int(round(RING_CAP_S * sr)))
        self.assertLess(abs(float(seg[-1, 0])), 1e-4)
        fade = int(round(SLICE_FADE_S * sr))
        # Halfway through the fade the linear ramp is near a half.
        self.assertAlmostEqual(abs(float(seg[-fade // 2, 0])) / 0.4, 0.5, delta=0.08)
        # The attack fade is audible when the tone itself starts at the cut.
        immediate, _, _ = slice_note(tone[:, None], sr, 0.0, region_end_s=0.5)
        attack = int(round(ATTACK_FADE_S * sr))
        self.assertLess(abs(float(immediate[0, 0])), abs(float(immediate[attack + 8, 0])))
        # The source array is not faded in place. A 440 Hz sine is zero at whole seconds.
        body = audio[int((onset + 0.25) * sr):int((onset + 0.30) * sr), 0]
        self.assertGreater(float(np.max(np.abs(body))), 0.2)

    def test_ring_stops_at_minus_60_db(self):
        sr = 48000
        seconds = 6.0
        t = np.arange(int(seconds * sr)) / sr
        # exp decay reaches -60 dB at t = 6.907 * tau.
        tau = 0.4
        env = np.exp(-t / tau).astype(np.float32)
        audio = (env * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[:, None]
        expected = 6.907755 * tau
        seg, t0, t1 = slice_note(audio, sr, 0.0)
        self.assertAlmostEqual(t0, 0.0, places=4)
        self.assertAlmostEqual(t1, expected, delta=0.06)
        self.assertLess(t1, RING_CAP_S)

    def test_region_end_blocks_the_next_note(self):
        sr = 44100
        audio = np.ones((int(5 * sr), 1), np.float32)
        seg, t0, t1 = slice_note(audio, sr, 0.2, region_end_s=1.2)
        self.assertAlmostEqual(t0, 0.2 - PRE_ROLL_S, places=4)
        self.assertLessEqual(t1, 1.2 + 1.0 / sr)
        self.assertGreater(t1, 1.1)

    def test_sample_rate_is_unchanged(self):
        sr = 44100
        audio = _tone(sr, 1.0, 440.0)[:, None]
        seg, _, _ = slice_note(audio, sr, 0.0, region_end_s=1.0)
        hz = _peak_hz(seg, sr)
        cents = 1200 * np.log2(hz / 440.0)
        self.assertLess(abs(cents), 8.0)


class PitchPolicyTest(unittest.TestCase):
    def test_encode_commands_do_not_resample_or_shift(self):
        flac, opus = encode_commands("in.wav", "out.flac", "out.opus")
        for cmd in (flac, opus):
            text = " ".join(cmd)
            self.assertNotIn("-ar", cmd)
            self.assertNotIn("atempo", text)
            self.assertNotIn("asetrate", text)
            self.assertNotIn("rubberband", text)
            self.assertNotIn("pitch", text)
        self.assertIn("96k", opus)
        self.assertIn("flac", flac)

    def test_round_trip_keeps_440(self):
        sr = 44100
        audio = _tone(sr, 1.2, 440.0)[:, None]
        seg, _, _ = slice_note(audio, sr, 0.0, region_end_s=1.2)
        tmp = tempfile.mkdtemp(prefix="nc-slice-")
        self.addCleanup(shutil.rmtree, tmp, True)
        flac = os.path.join(tmp, "n.flac")
        opus = os.path.join(tmp, "n.opus")
        slicer.encode_pair(seg, sr, flac, opus)
        decoded = sampler.load_opus(opus)
        hz = _peak_hz(decoded, sampler.MIX_SR)
        cents = 1200 * np.log2(hz / 440.0)
        self.assertLess(abs(float(cents)), 15.0)
        import soundfile as sf
        master, master_sr = sf.read(flac, always_2d=True)
        self.assertEqual(master_sr, sr)
        hz = _peak_hz(master, master_sr)
        self.assertLess(abs(1200 * np.log2(hz / 440.0)), 8.0)

    def test_module_does_not_upload(self):
        text = open(os.path.join(ROOT, "ncperf", "slicer.py"), encoding="utf-8").read()
        self.assertNotIn("boto3", text)
        self.assertNotIn("put_object", text)
        self.assertNotIn("def upload", text)

    def test_pyin_within_15_cents_does_not_need_the_fft_inside_15(self):
        from ncperf.slicer import absent_reason, strict_accept
        # Guitar E3 on sul A: pYIN is in tune, the FFT peak sits on a partial.
        ok = {"midi": 52, "cents": 2.0, "fft_cents": -23.4, "spread_cents": 37.9}
        self.assertTrue(strict_accept(ok, 40, 88))
        flat = {"midi": 40, "cents": -38.0, "fft_cents": -38.0, "spread_cents": 10.0}
        self.assertFalse(strict_accept(flat, 40, 88))
        fft_only = {"midi": 52, "cents": None, "fft_cents": 2.0, "spread_cents": 10.0}
        self.assertFalse(strict_accept(fft_only, 40, 88))
        wide = {"midi": 52, "cents": -10.0, "fft_cents": -23.0, "spread_cents": 47.8}
        self.assertFalse(strict_accept(wide, 40, 88))
        far_fft = {"midi": 52, "cents": 2.0, "fft_cents": -51.0, "spread_cents": 10.0}
        self.assertFalse(strict_accept(far_fft, 40, 88))
        outside = {"midi": 30, "cents": 1.0, "fft_cents": 1.0, "spread_cents": 5.0}
        self.assertFalse(strict_accept(outside, 40, 88))
        self.assertIn("15 cent gate", absent_reason(40, flat))
        self.assertIn("spread", absent_reason(52, wide))
        self.assertNotIn("15 cent gate", absent_reason(52, wide))
        self.assertIn("FFT peak", absent_reason(52, far_fft))

    def test_identify_does_not_snap_to_the_search_centre(self):
        sr = 44100
        # E3, while the file span is centred on F3. The result stays E3.
        audio = _tone(sr, 1.5, 164.8138)[:, None]
        found = identify_region(audio, sr, 48, 59)
        self.assertIsNotNone(found)
        self.assertEqual(found["midi"], 52)
        self.assertTrue(found["accepted"])
        self.assertLess(abs(found["cents"]), 15)

    def test_neighbour_is_not_moved_into_a_hole(self):
        holes = {52}
        measurements = [
            {"midi": 53, "cents": 1.0, "file": "run.aif"},
            {"midi": 52, "cents": 40.0, "file": "nope.aif"},
            {"midi": 52, "cents": -4.0, "file": "run.aif"},
        ]
        # 40 cents would have failed the strict gate; assign still keeps the
        # closer reading of the same midi and never rewrites 53 as 52.
        chosen = assign_recoveries(measurements, holes)
        self.assertEqual(set(chosen), {52})
        self.assertEqual(chosen[52]["file"], "run.aif")
        self.assertNotIn(53, chosen)
        untouched = assign_recoveries([{"midi": 53, "cents": 1.0}], holes)
        self.assertEqual(untouched, {})
        flat = assign_recoveries([{"midi": 52, "cents": -38.0, "accepted": False}], holes)
        self.assertEqual(flat, {})

    def test_segment_count_picks_the_split_that_matches_the_filename(self):
        from ncperf.slicer import choose_segments
        glued = [(0.0, 8.0)]
        right = [(i * 1.0, i * 1.0 + 0.8) for i in range(8)]
        extra = right + [(9.0, 9.2)]  # the short tail is dropped
        chosen, drop = choose_segments([(40, glued), (50, right), (55, extra)], 8)
        self.assertEqual(drop, 50.0)
        self.assertEqual(len(chosen), 8)


class GapReportTest(unittest.TestCase):
    def test_absent_versus_not_in_the_sources(self):
        # Spec E2–E6. The page covers E2–B5. We opened E2–B2 only, and sliced E2.
        absent, not_in, unscanned = classify_gaps(40, 88, set(range(40, 84)), set(range(40, 48)), {40})
        self.assertIn(41, absent)
        self.assertNotIn(40, absent)
        self.assertEqual(not_in, list(range(84, 89)))
        self.assertEqual(unscanned[0], 48)
        self.assertNotIn(84, unscanned)

    def test_remaining_holes_do_not_raise(self):
        notes = {64: _hand_note(64)}
        gaps = [{
            "midi": 52, "name": "E3", "status": "absent",
            "reason": "Scanned the Iowa file that covers this pitch. The strict onset and pitch check did not accept it, and a neighbour was not moved into the hole.",
            "files": ["Guitar.mf.sulE.C3B3.stereo.aif"],
        }]
        doc = build_note_manifest(_spec(), notes, gaps, [])
        self.assertEqual(validate_manifest(doc), [])
        self.assertEqual(doc["gaps"][0]["name"], "E3")
        self.assertEqual(doc["playback"]["octave_substitution"], False)
        self.assertEqual(doc["playback"]["missing"], "omit")


class OnsetTest(unittest.TestCase):
    def test_short_gap_stays_two_notes(self):
        from ncperf import pitch
        sr = 48000
        gap = np.zeros(int(0.20 * sr), np.float32)
        y = np.concatenate([_tone(sr, 0.60, 440), gap, _tone(sr, 0.60, 523.25)])
        merged = pitch.segment_run(y, sr)
        regions = strict_regions(y, sr)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(regions), 2)

    def test_quiet_note_after_a_loud_one_is_kept(self):
        from ncperf import pitch
        sr = 48000
        silence = np.zeros(int(0.50 * sr), np.float32)
        quiet = _tone(sr, 0.60, 523.25, amp=0.8 * 10 ** (-50 / 20))
        y = np.concatenate([_tone(sr, 0.60, 440, amp=0.8), silence, quiet])
        self.assertEqual(len(pitch.segment_run(y, sr)), 1)
        self.assertEqual(len(strict_regions(y, sr)), 2)

    def test_note_at_the_start_is_a_region(self):
        sr = 22050
        y = _tone(sr, 0.80, 330)
        regions = strict_regions(y, sr)
        self.assertGreaterEqual(len(regions), 1)
        self.assertLess(regions[0][0], 0.05)


class ManifestTest(unittest.TestCase):
    def test_note_manifest_and_chord_manifest(self):
        doc = build_note_manifest(_spec(), {64: _hand_note()}, [], [])
        self.assertEqual(doc["format"], "nc-sampler/1")
        self.assertEqual(doc["license_out"], "CC-BY-SA-4.0")
        self.assertEqual(doc["sustain"], "plucked")
        self.assertEqual(doc["velocity"], ["mf"])
        self.assertIn("University of Iowa", doc["credit"])
        self.assertEqual(validate_manifest(doc), [])
        audio = np.zeros((48000, 2), np.float32)
        audio[:24000] = 0.2
        chord = make_chord("cello", "Cm", audio, 48000, {
            "url": "https://example.test/cello-cm.flac",
            "t0": 0.0,
            "t1": 1.0,
            "license": "PD-equivalent",
        })
        chords = build_chord_manifest(_spec(id="cello-chords", instrument="cello", pluck=False, lo=36, hi=72), {"Cm": chord})
        self.assertEqual(chords["kind"], "chords")
        self.assertEqual(chords["chords"]["Cm"]["file"], "notes/cello/chords/Cm.opus")
        self.assertEqual(validate_manifest(chords), [])

    def test_rejects_nc_license_pitch_shift_and_octave_swap(self):
        with self.assertRaises(ValueError):
            build_note_manifest(_spec(license="CC-BY-NC-4.0"), {64: _hand_note()}, [], [])
        doc = build_note_manifest(_spec(), {64: _hand_note()}, [], [])
        doc["playback"]["octave_substitution"] = True
        self.assertTrue(validate_manifest(doc))
        doc = build_note_manifest(_spec(), {64: _hand_note()}, [], [])
        doc["notes"]["64"]["playback_rate"] = 1.05
        self.assertTrue(any("playback_rate" in err for err in validate_manifest(doc)))
        doc = build_note_manifest(_spec(), {64: _hand_note()}, [], [])
        doc["license_in"] = "CC-BY-NC-4.0"
        self.assertTrue(any("license_in" in err for err in validate_manifest(doc)))

    def test_made_note_records_cents_and_stays_under_the_cap(self):
        sr = 48000
        audio = _tone(sr, 1.0, 440.0)[:, None]
        note = make_note("piano", 69, audio, sr, {
            "url": "https://theremin.music.uiowa.edu/Piano.mf.A4.aiff",
            "t0": 0.0, "t1": 1.0, "license": "PD-equivalent",
        }, cents=1.5, method="fft")
        self.assertEqual(note["cents"], 1.5)
        self.assertLessEqual(note["dur_s"], RING_CAP_S)
        self.assertTrue(note["file"].endswith("/69.opus"))
        self.assertIsNotNone(note["lufs"])
        self.assertLess(note["peak_db"], 0)


class SamplerTest(unittest.TestCase):
    def test_missing_note_is_named_and_not_replaced(self):
        manifest = build_note_manifest(_spec(), {64: _hand_note(64), 67: _hand_note(67)}, [], [])
        # E3 is written. E4 is on the instrument and is not written, so it must not play.
        sr = sampler.MIX_SR
        called = []

        def load(midi):
            called.append(midi)
            amp = 0.2 if midi == 64 else 0.05
            return np.ones((sr, 2), np.float32) * amp

        audio, messages = sampler.render(
            manifest,
            [("Em7", [52, 64], 2)],
            96,
            load,
            spread=0.0,
        )
        self.assertEqual(called, [64])
        self.assertEqual(messages, ["guitar has no E3; Em7 plays E4"])
        self.assertGreater(float(np.max(np.abs(audio))), 0.01)
        called.clear()
        _, only_missing = sampler.render(manifest, [("Em7", [52], 2)], 96, load)
        self.assertEqual(called, [])
        self.assertIn("plays without", only_missing[0])
        self.assertNotIn("E4", only_missing[0])

    def test_chord_change_fades_120_ms(self):
        manifest = {"instrument": "piano", "notes": {"69": {"lufs": -20.0, "midi": 69}}}
        sr = sampler.MIX_SR
        tone = np.ones((int(3 * sr), 2), np.float32) * 0.5

        def load(midi):
            return tone

        audio, messages = sampler.render(manifest, [("A", [69], 1)], 60, load, spread=0.0)
        self.assertEqual(messages, [])
        # 1 beat at 60 BPM, then 120 ms of fade, then the short tail the mix keeps.
        self.assertAlmostEqual(len(audio) / sr, 1.2, delta=0.03)
        full = abs(float(audio[int(0.5 * sr), 0]))
        mid_fade = abs(float(audio[int(1.06 * sr), 0]))
        after = abs(float(audio[int(1.18 * sr), 0]))
        self.assertGreater(full, mid_fade * 2)
        self.assertGreater(mid_fade, after * 2)

    def test_checks_use_the_written_octaves(self):
        guitar = dict(sampler.CHECKS[0])
        self.assertEqual(guitar["events"][0][1], [64, 67, 71, 74])
        self.assertNotIn(52, guitar["events"][0][1])
        cello = sampler.CHECKS[2]
        # Fm root writes G#3. There is no other G# in that voicing to swap in.
        self.assertEqual(cello["events"][1][1], [53, 56, 60])
        self.assertEqual(cello["bpm"], 96)


class NativeFileTest(unittest.TestCase):
    def test_leading_silence_is_not_the_whole_slice(self):
        from ncperf.slicer import _onset_s
        sr = 44100
        silence = np.zeros((int(0.25 * sr), 1), np.float32)
        audio = np.concatenate([silence, _tone(sr, 1.2, 440.0)[:, None]])
        onset = _onset_s(audio, sr)
        self.assertGreater(onset, 0.2)
        seg, _t0, _t1 = slice_note(audio, sr, onset, None)
        self.assertGreater(len(seg), int(0.5 * sr))

    def test_a_native_file_is_kept_only_when_it_is_that_note(self):
        sr = 44100
        # A4 is MIDI 69, 440 Hz.
        ok = accept_native(_tone(sr, 1.5, 440.0)[:, None], sr, 69)
        self.assertIsNotNone(ok)
        self.assertEqual(ok["midi"], 69)
        self.assertLess(abs(ok["cents"]), 15)
        # A semitone sharp is not stored as A4, and it is not moved.
        self.assertIsNone(accept_native(_tone(sr, 1.5, 466.16)[:, None], sr, 69))

    def test_sfz_keeps_the_first_native_round_robin_only(self):
        text = """
        <region>
        sample=..\\Samples\\bass_e2_mf_rr2.wav
        lokey=40
        hikey=40
        seq_position=2
        pitch_keycenter=40
        <region>
        sample=..\\Samples\\bass_e2_mf_rr1.wav
        lokey=40
        hikey=40
        pitch_keycenter=40
        <region>
        sample=..\\Samples\\bass_f2_stretched.wav
        lokey=41
        hikey=41
        pitch_keycenter=40
        <region>
        sample=..\\Samples\\bass_span.wav
        lokey=40
        hikey=42
        pitch_keycenter=41
        """
        rows = parse_sfz_regions(text, "https://example.test/Programs/")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lo"], 40)
        self.assertEqual(rows[0]["hi"], 40)
        self.assertTrue(rows[0]["url"].endswith("/Samples/bass_e2_mf_rr1.wav"))

    def test_new_libraries_are_native_and_do_not_shift(self):
        from ncperf.notemap import _specs
        by_id = {spec["id"]: spec for spec in _specs()}
        for gone in ("uiowa-cello-arco", "uiowa-vibraphone-dampen", "vcsl-kalimba"):
            self.assertNotIn(gone, by_id)
        spec = by_id["karoryfer-darkblack"]
        self.assertTrue(spec.get("native"))
        self.assertEqual((spec["lo"], spec["hi"], spec["license"], spec["instrument"]), (35, 76, "CC0-1.0", "bass"))
        self.assertNotIn("pitch_shift", spec)
        vibes = by_id["uiowa-marimba-cord-2012"]
        self.assertTrue(vibes.get("native"))
        self.assertEqual((vibes["lo"], vibes["hi"], vibes["license"], vibes["instrument"]), (36, 96, "PD-equivalent", "vibes"))
        self.assertNotIn("pitch_shift", vibes)
        self.assertIn("Marimba\\.cord\\.ff\\.", vibes["file_re"])
        strings = by_id["uiowa-violin-arco-2012"]
        self.assertTrue(strings.get("native"))
        self.assertEqual((strings["lo"], strings["hi"], strings["license"], strings["instrument"]), (55, 100, "PD-equivalent", "strings"))
        self.assertNotIn("pitch_shift", strings)
        self.assertIn("Violin\\.arco\\.ff\\.", strings["file_re"])


class CommandTest(unittest.TestCase):
    def test_help_lists_slice(self):
        env = os.environ.copy()
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "nc"), "slice", "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not upload", (proc.stdout + proc.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=1)
