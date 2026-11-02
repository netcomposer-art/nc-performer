"""Library harvester, qualify step: measures match musicmatter.art, gates reject with reasons, roles and tags."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from nc_perform import library as L  # noqa: E402
from nc_perform import library_measures as M  # noqa: E402
from nc_perform.config import Settings  # noqa: E402

SR = 44100
# sha256 of musicmatter.art tools/library/measures.py. The two files are one module in two repos: if this fails,
# copy the changed file across (both ways match) and update this constant and MEASURES_SHA256 in
# musicmatter.art tools/library/test_measures.py together.
MEASURES_SHA256 = "ba4d537d497023933b310980cd12c5157ee7fda04efeb7e40c3bd61cbe2a996c"
# measure() on golden_signal(); the same literal is in musicmatter.art tools/library/test_measures.py.
GOLDEN = {"length_s": 0.602, "lufs": -17.3, "peak_dbfs": -2.1, "attack_ms": 2.0, "decay_s": 0.48,
          "centroid_hz": 60, "low_share": 1.0, "pitch_hz": 56.7}


def golden_signal() -> np.ndarray:
    """A deterministic 808-style kick at 48 kHz, stereo: 50 ms of silence, then a 160->55 Hz sweep with a click."""
    sr = M.SR
    t = np.arange(int(0.6 * sr)) / sr
    freq = 55 + 105 * np.exp(-t * 30)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    body = np.sin(phase) * np.exp(-t * 7)
    click = np.exp(-t * 900) * 0.5
    mono = (0.7 * (body + click)).astype(np.float32)
    mono = np.concatenate([np.zeros(int(0.05 * sr), np.float32), mono])
    return np.stack([mono, mono * 0.9], axis=1)


def wav_bytes(data: np.ndarray, sr: int = SR, fmt: str = "WAV", subtype: str = "PCM_16") -> bytes:
    buf = io.BytesIO()
    sf.write(buf, data, sr, format=fmt, subtype=subtype)
    return buf.getvalue()


def kick(sr=SR, length=0.6):
    t = np.arange(int(length * sr)) / sr
    phase = 2 * np.pi * np.cumsum(50 + 110 * np.exp(-t * 35)) / sr
    return (0.8 * np.sin(phase) * np.exp(-t * 6)).astype(np.float32)[:, None]


def noise_hit(sr=SR, length=0.15, decay=60.0, seed=1, hp=True):
    rng = np.random.default_rng(seed)
    n = int(length * sr)
    x = rng.normal(0, 1, n)
    if hp:
        x = np.diff(np.concatenate([[0], x]))  # crude high-pass: bright like a hat
    t = np.arange(n) / sr
    y = x * np.exp(-t * decay)
    return (0.7 * y / np.abs(y).max()).astype(np.float32)[:, None]


def mp3_bytes(data: np.ndarray, sr: int = SR) -> bytes:
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "x.wav")
        sf.write(src, data, sr)
        return subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-b:a", "128k", "-f", "mp3", "-"],
                              check=True, capture_output=True).stdout


class Measures(unittest.TestCase):
    def test_same_file_as_musicmatter(self):
        path = os.path.join(ROOT, "service", "nc_perform", "library_measures.py")
        with open(path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), MEASURES_SHA256)

    def test_golden_measures(self):
        data, _ = M.trim(golden_signal())
        self.assertEqual(M.measure(data, None), GOLDEN)

    def test_playback_gain_never_passes_ceiling(self):
        self.assertEqual(M.playback_gain(-20.0, -6.0), (5.0, True))
        self.assertEqual(M.playback_gain(-20.0, -12.0), (6.0, False))


class Qualify(unittest.TestCase):
    def test_808_kick_original(self):
        r = L.qualify(wav_bytes(kick()), {"name": "TR-808 Kick_long.wav", "tags": ["808", "analog"],
                                          "query": "808 kick one shot", "role": "kick", "quality": "original",
                                          "ext": "wav"})
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["role"], "kick")
        self.assertEqual(r["role_basis"], "name")
        self.assertIn("808", r["tags"])
        self.assertIn("electronic", r["tags"])
        self.assertIn("analog", r["tags"])
        self.assertEqual(r["format"]["sample_rate"], 48000)
        self.assertTrue(r["format"]["lossless"])
        self.assertEqual(r["sha256"], hashlib.sha256(r["flac"]).hexdigest())
        decoded, sr = sf.read(io.BytesIO(r["flac"]))
        self.assertEqual(sr, 48000)
        self.assertEqual(len(decoded), r["format"]["frames"])
        self.assertGreater(len(r["preview"]), 1000)
        self.assertEqual(r["title"], "TR-808 Kick long")
        self.assertIn("hit_dbfs", r["measures"])
        self.assertEqual(len(r["wave"]), 48)

    def test_preview_quality_is_marked_lossy(self):
        r = L.qualify(mp3_bytes(noise_hit(length=0.12, decay=70)), {"name": "909 closed hat", "tags": [],
                                                                      "role": "hat", "quality": "preview", "ext": "mp3"})
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["role"], "hat")
        self.assertIn("closed", r["tags"])
        self.assertIn("909", r["tags"])
        self.assertFalse(r["format"]["lossless"])
        self.assertTrue(r["processing"][0].startswith("decoded from the source's HQ MP3 preview"))

    def test_open_hat_by_name_and_by_decay(self):
        r = L.qualify(wav_bytes(noise_hit(length=1.2, decay=4)), {"name": "OH 808", "role": "hat", "ext": "wav"})
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["role"], "hat")
        self.assertIn("open", r["tags"])
        r2 = L.qualify(wav_bytes(noise_hit(length=1.2, decay=4)), {"name": "hihat 3", "role": "hat", "ext": "wav"})
        self.assertIn("open", r2["tags"])

    def test_name_beats_query(self):
        r = L.qualify(wav_bytes(noise_hit(hp=False, decay=25, length=0.3)),
                      {"name": "909 snare tight", "role": "kick", "query": "909 kick", "ext": "wav"})
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["role"], "snare")

    def test_rimshot_is_perc_with_rim_tag(self):
        r = L.qualify(wav_bytes(noise_hit(length=0.08, decay=90)), {"name": "808 Rimshot", "role": "perc", "ext": "wav"})
        self.assertEqual((r["role"], "rim" in r["tags"]), ("perc", True))

    def test_query_role_when_name_says_nothing(self):
        r = L.qualify(wav_bytes(noise_hit(hp=False, length=0.4, decay=20)), {"name": "TR8 sample 12", "role": "clap", "ext": "wav"})
        self.assertEqual((r["role"], r["role_basis"]), ("clap", "query"))

    def test_rejections_carry_reasons(self):
        cases = [
            (wav_bytes(np.zeros((SR, 1), np.float32)), {"name": "silence"}, "silent"),
            (wav_bytes(kick()), {"name": "808 kick loop 120 bpm"}, "loop"),
            (wav_bytes(kick(length=0.01)), {"name": "blip"}, "short"),
            (wav_bytes(np.tile(np.sign(np.sin(np.arange(SR // 2) / 20)), (1, 1)).T.astype(np.float32) * 1.0),
             {"name": "square kick"}, "clipped"),
            (wav_bytes((np.sin(np.arange(5 * SR) / 30) * 0.5).astype(np.float32)[:, None]), {"name": "long kick", "role": "kick"},
             "longer than 3 s"),
            (b"not audio at all", {"name": "x", "ext": "wav"}, "could not decode"),
            (wav_bytes(kick()), {"name": "x", "ext": "exe"}, "unsupported"),
            (wav_bytes(kick() * 0.001), {"name": "quiet kick"}, "too quiet"),
        ]
        for raw, meta, word in cases:
            meta = {"ext": "wav", **meta}
            r = L.qualify(raw, meta)
            self.assertFalse(r["ok"], meta)
            self.assertIn(word, r["reason"], (meta, r["reason"]))
            self.assertEqual(len(r["source_sha256"]), 64)

    def test_full_scale_once_is_not_clipping(self):
        x = kick()
        x = x / np.abs(x).max()
        self.assertFalse(L.run_clipped(x))

    def test_meta_header_roundtrip(self):
        doc = {"id": "fs-1-kick", "tags": ["808"]}
        enc = base64.urlsafe_b64encode(json.dumps(doc).encode()).decode().rstrip("=")
        self.assertEqual(L.read_meta(enc), doc)
        self.assertEqual(L.read_meta("%%%"), {})


class QualifyEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-lib-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = Settings()
        s.storage = "file://" + os.path.join(self.tmp, "files")
        s.data_dir = os.path.join(self.tmp, "data")
        s.bind = "127.0.0.1:0"
        s.queue_autostart = False
        s.signing_key = ""
        s.internal_token = "inside"
        from nc_perform.api import build_app, serve
        self.app = build_app(s)
        self.httpd = serve(self.app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        host, port = self.httpd.server_address[:2]
        self.api = f"http://{host}:{port}"

    def post(self, body: bytes, meta: dict, token="inside"):
        enc = base64.urlsafe_b64encode(json.dumps(meta).encode()).decode().rstrip("=")
        req = urllib.request.Request(self.api + "/internal/library/qualify", data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "X-NC-Library-Meta": enc, "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_qualify_over_http(self):
        status, doc = self.post(wav_bytes(kick()), {"name": "808 kick", "role": "kick", "ext": "wav", "quality": "original"})
        self.assertEqual(status, 200)
        self.assertTrue(doc["ok"])
        flac = base64.b64decode(doc["flac_b64"])
        self.assertEqual(hashlib.sha256(flac).hexdigest(), doc["sha256"])
        self.assertTrue(base64.b64decode(doc["preview_b64"]))
        self.assertNotIn("flac", doc)

    def test_rejection_is_200_with_reason(self):
        status, doc = self.post(wav_bytes(np.zeros((SR, 1), np.float32)), {"name": "x", "ext": "wav"})
        self.assertEqual((status, doc["ok"]), (200, False))
        self.assertIn("silent", doc["reason"])

    def test_needs_internal_token(self):
        status, _ = self.post(wav_bytes(kick()), {"name": "x"}, token="wrong")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
