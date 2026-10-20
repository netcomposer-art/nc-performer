"""Processed Keep: JSON still keeps the original, multipart stores the dialed WAV."""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from nc_perform.config import Settings
from nc_perform.ids import sha256_text
from nc_perform.keep import (
    AFTER_SIGNING,
    KEPT_FORM_OVERHEAD,
    KEPT_TAIL_S,
    KEPT_WAV_MAX_BYTES,
    inspect_wav,
    job_length_s,
    parse_dials,
    parse_multipart,
)
from nc_perform.limits import Reject


DIALS = {
    "cutoff": 40,
    "cutoff_hz": 1200,
    "delay_timing": "1/8 dotted",
    "delay_beats": 0.75,
    "delay_s": 0.4,
    "feedback": 0.35,
    "wet": 0.5,
    "loop": False,
}


def pcm_wav(rate=48000, channels=2, bits=16, frames=480, data=None, audio_format=1):
    block = channels * (bits // 8)
    if data is None:
        data = bytes(frames * block)
    fmt = struct.pack("<HHIIHH", audio_format, channels, rate, rate * block, block, bits)
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    data_chunk = b"data" + struct.pack("<I", len(data)) + data
    payload = b"WAVE" + fmt_chunk + data_chunk
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def multipart(fields, boundary="----nc-keep"):
    chunks = []
    for name, value, filename, ctype in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if filename:
            head += f'; filename="{filename}"'
        head += "\r\n"
        if ctype:
            head += f"Content-Type: {ctype}\r\n"
        head += "\r\n"
        chunks.append(head.encode() + value + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    return body, f"multipart/form-data; boundary={boundary}"


def keep_body(token, wav, dials=None):
    dials = DIALS if dials is None else dials
    return multipart([
        ("claim_token", token.encode(), None, None),
        ("dials", json.dumps(dials).encode(), None, None),
        ("audio", wav, "h-23456789abcd.live.wav", "audio/wav"),
    ])


class KeepRules(unittest.TestCase):
    def test_cap_is_40_mib(self):
        self.assertEqual(KEPT_WAV_MAX_BYTES, 40 * 1024 * 1024)
        self.assertEqual(KEPT_TAIL_S, 30)

    def test_wav_rates_and_job_length(self):
        info = inspect_wav(pcm_wav(rate=48000, frames=48000), 31)
        self.assertEqual(info.sample_rate, 48000)
        self.assertAlmostEqual(info.duration_s, 1.0, places=3)
        info = inspect_wav(pcm_wav(rate=44100, frames=44100, channels=1), 31)
        self.assertEqual(info.sample_rate, 44100)
        self.assertEqual(info.channels, 1)
        rec = {"performance": {"music_ends_at_s": 8, "outputs": [{"duration_s": 12}]}}
        self.assertEqual(job_length_s(rec), 8)
        self.assertEqual(job_length_s({"performance": {"outputs": [{"duration_s": 12}]}}), 12)
        self.assertEqual(job_length_s({}), 180)
        # Exactly the limit, and a hair inside the rounding window, is kept.
        inspect_wav(pcm_wav(frames=48000), 1.0)
        inspect_wav(pcm_wav(frames=48000), 0.96)
        with self.assertRaises(Reject) as cm:
            inspect_wav(pcm_wav(frames=48000), 0.9)
        self.assertIn("at most 0.9s", str(cm.exception))

    def test_wav_rejects_bad_files(self):
        with self.assertRaises(Reject) as cm:
            inspect_wav(b"not a wav", 30)
        self.assertIn("PCM WAV", str(cm.exception))
        with self.assertRaises(Reject) as cm:
            inspect_wav(pcm_wav(rate=22050, frames=22050), 30)
        self.assertIn("44100 or 48000", str(cm.exception))
        with self.assertRaises(Reject) as cm:
            inspect_wav(pcm_wav()[:-8], 30)
        self.assertIn("truncated", str(cm.exception))
        with self.assertRaises(Reject):
            inspect_wav(pcm_wav(bits=8, frames=48), 30)
        with self.assertRaises(Reject):
            inspect_wav(pcm_wav(audio_format=3, frames=48), 30)
        with self.assertRaises(Reject):
            inspect_wav(pcm_wav(channels=3, frames=48), 30)
        # 24-bit is allowed when the file is short enough for the byte cap.
        info = inspect_wav(pcm_wav(bits=24, frames=48), 30)
        self.assertEqual(info.bits, 24)

    def test_dials(self):
        got = parse_dials(json.dumps(DIALS))
        self.assertEqual(got["delay_timing"], "1/8 dotted")
        self.assertEqual(got["cutoff_hz"], 1200)
        self.assertFalse(got["loop"])
        open_filter = dict(DIALS, cutoff=0, cutoff_hz=None, wet=0)
        self.assertIsNone(parse_dials(open_filter)["cutoff_hz"])
        with self.assertRaises(Reject):
            parse_dials(json.dumps(dict(DIALS, feedback=0.96)))
        with self.assertRaises(Reject):
            parse_dials(json.dumps(dict(DIALS, loop="yes")))
        with self.assertRaises(Reject):
            parse_dials(b"\xff")

    def test_multipart_keeps_trailing_wav_bytes(self):
        wav = pcm_wav(frames=4)
        wav = wav[:-2] + b"\r\n"
        # Rebuild is messy if I splice the header. Put the marker bytes in the
        # data by constructing data that ends with CRLF.
        data = bytes(480 * 4 - 2) + b"\r\n"
        wav = pcm_wav(frames=0, data=data)
        self.assertTrue(wav.endswith(b"\r\n"))
        body, ctype = keep_body("token", wav)
        parts = parse_multipart(body, ctype)
        self.assertEqual(parts["audio"], wav)
        self.assertEqual(parts["claim_token"], b"token")
        dials = json.loads(parts["dials"])
        self.assertEqual(dials["delay_beats"], 0.75)


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class ProcessedKeepApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-keep-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = Settings()
        s.storage = "file://" + os.path.join(self.tmp, "files")
        s.data_dir = os.path.join(self.tmp, "data")
        s.bind = "127.0.0.1:0"
        s.queue_autostart = False
        s.signing_key = ""
        from nc_perform.api import build_app, serve
        self.app = build_app(s)
        self.httpd = serve(self.app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        host, port = self.httpd.server_address[:2]
        self.api = f"http://{host}:{port}"
        s.public_base_url = self.api
        self.token = "claim-secret-token"
        self.job = "h-23456789abcd"
        self.record = {
            "record_type": "netcomposer.performance_record",
            "record_hash": {"value": "ab" * 32},
            "signature": {"status": "SIGNED", "algorithm": "Ed25519", "value": "signed-bytes"},
            "performance": {
                "title": "Glass",
                "music_ends_at_s": 8,
                "outputs": [{"file": "performance.flac", "duration_s": 12}],
            },
        }
        self.record_bytes = json.dumps(self.record).encode()
        self.flac = b"FLAC-ORIGINAL"
        self.mp3 = b"MP3-ORIGINAL"
        self._store(self.job, public=True)

    def _store(self, job_id, public=False, music_ends=8, tombstone=None):
        base = f"performances/{job_id}/"
        rec = json.loads(self.record_bytes)
        rec["performance"]["music_ends_at_s"] = music_ends
        raw = json.dumps(rec).encode()
        st = self.app.storage
        st.put(base + "record.json", raw, "application/json")
        st.put(base + "performance.flac", self.flac, "audio/flac")
        st.put(base + "preview.mp3", self.mp3, "audio/mpeg")
        meta = {
            "id": job_id,
            "visibility": "public" if public else "unlisted",
            "retention": "days",
            "keep": False,
            "owner": None,
            "claim_token_sha256": sha256_text(self.token),
            "created_at": "2026-01-01T00:00:00Z",
            "audio_expires_at": "2026-04-01T00:00:00Z",
            "public_after": "2020-01-01T00:00:00Z" if public else None,
            "hidden": False,
            "tombstone": tombstone,
            "origin": "hosted",
        }
        st.put(base + "meta.json", json.dumps(meta).encode(), "application/json")
        return raw

    def test_json_keep_is_unchanged(self):
        job = "h-23456789abce"
        raw = self._store(job)
        status, body = _post(
            self.api + f"/api/jobs/{job}/keep",
            json.dumps({"claim_token": self.token, "dials": DIALS}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        kept = json.loads(body)
        self.assertEqual(set(kept), {"id", "visibility", "keep", "audio_expires_at"})
        self.assertTrue(kept["keep"])
        self.assertIsNone(kept["audio_expires_at"])
        self.assertNotIn("processed", kept)
        self.assertIsNone(self.app.storage.get(f"performances/{job}/kept.wav"))
        self.assertEqual(self.app.storage.get(f"performances/{job}/record.json"), raw)
        self.assertEqual(self.app.storage.get(f"performances/{job}/performance.flac"), self.flac)
        meta = json.loads(self.app.storage.get(f"performances/{job}/meta.json"))
        self.assertNotIn("live_dials", meta)

    def test_processed_keep_stores_wav_and_leaves_the_record(self):
        wav = pcm_wav(rate=48000, frames=48000)
        # A byte the old 8000-byte read would have cut off, and a trailing CRLF
        # that a blanket rstrip would eat.
        wav = wav[:-2] + b"\r\n"
        self.assertGreater(len(wav), 8000)
        body, ctype = keep_body(self.token, wav)
        before = self.app.storage.get(f"performances/{self.job}/record.json")
        status, raw = _post(self.api + f"/api/jobs/{self.job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 200, raw)
        kept = json.loads(raw)
        self.assertTrue(kept["ok"])
        self.assertTrue(kept["processed"])
        self.assertEqual(kept["dials"]["delay_timing"], "1/8 dotted")
        self.assertEqual(kept["audio_url"], f"{self.api}/api/jobs/{self.job}/files/kept.wav")
        self.assertTrue(kept["keep"])
        self.assertIsNone(kept["audio_expires_at"])
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/kept.wav"), wav)
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/record.json"), before)
        self.assertNotIn(b"live_dials", before)
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/performance.flac"), self.flac)
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/preview.mp3"), self.mp3)
        meta = json.loads(self.app.storage.get(f"performances/{self.job}/meta.json"))
        self.assertEqual(meta["live_dials"]["cutoff"], 40)
        self.assertEqual(meta["live_audio"]["wav"], "kept.wav")
        self.assertEqual(meta["live_audio"]["sample_rate"], 48000)
        self.assertIn("after the record was signed", meta["live_audio"]["note"])
        self.assertNotIn("live_dials", json.loads(before))
        if shutil.which("ffmpeg"):
            mp3 = self.app.storage.get(f"performances/{self.job}/kept.mp3")
            self.assertTrue(mp3 and (mp3[:3] == b"ID3" or mp3[0] == 0xFF))
            self.assertEqual(meta["live_audio"]["mp3"], "kept.mp3")
        else:
            self.assertIsNone(meta["live_audio"]["mp3"])

        code, headers, file_bytes = _get(self.api + f"/api/jobs/{self.job}/files/kept.wav")
        self.assertEqual(code, 200)
        self.assertEqual(file_bytes, wav)
        self.assertIn("audio/wav", headers.get("Content-Type", ""))

        code, _, job = _get(self.api + f"/api/jobs/{self.job}")
        self.assertEqual(code, 200)
        job = json.loads(job)
        self.assertTrue(job["files"]["flac"].endswith("/performance.flac"))
        self.assertTrue(job["files"]["mp3"].endswith("/preview.mp3"))
        self.assertTrue(job["files"]["kept_wav"].endswith("/kept.wav"))
        self.assertEqual(job["live_dials"]["wet"], 0.5)
        self.assertEqual(job["kept_note"], AFTER_SIGNING)
        if shutil.which("ffmpeg"):
            self.assertTrue(job["files"]["kept_mp3"].endswith("/kept.mp3"))

        code, _, page = _get(self.api + f"/records/{self.job}")
        self.assertEqual(code, 200)
        html = page.decode()
        self.assertIn("This is the kept sound with the Live dials.", html)
        self.assertIn("after the record was signed", html)
        self.assertIn("Download dialed WAV", html)
        self.assertIn("signed render", html)
        self.assertIn("1/8 dotted", html)
        self.assertIn("1200 Hz", html)
        if shutil.which("ffmpeg"):
            self.assertIn("/files/kept.mp3", html)
        else:
            self.assertIn("/files/kept.wav", html)

        code, _, feed = _get(self.api + "/api/feed")
        item = next(p for p in json.loads(feed)["performances"] if p["id"] == self.job)
        self.assertEqual(item["live_dials"]["cutoff"], 40)
        self.assertTrue(item["kept_audio"].endswith("/kept.wav"))
        self.assertIn("after the record was signed", item["note"])
        self.assertTrue(item["record"].endswith(f"/records/{self.job}.json"))

        # A later JSON keep does not drop the dialed file or rewrite the record.
        status, raw = _post(
            self.api + f"/api/jobs/{self.job}/keep",
            json.dumps({"claim_token": self.token}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        again = json.loads(raw)
        self.assertNotIn("processed", again)
        self.assertTrue(again["keep"])
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/kept.wav"), wav)
        self.assertEqual(self.app.storage.get(f"performances/{self.job}/record.json"), before)
        meta = json.loads(self.app.storage.get(f"performances/{self.job}/meta.json"))
        self.assertEqual(meta["live_dials"]["delay_timing"], "1/8 dotted")

    def test_bad_keep_is_rejected(self):
        job = self.job
        wav = pcm_wav(frames=480)
        body, ctype = keep_body("wrong-token", wav)
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 403, raw)
        self.assertIn("claim_token", json.loads(raw)["error"])
        self.assertIsNone(self.app.storage.get(f"performances/{job}/kept.wav"))

        body, ctype = keep_body(self.token, b"RIFF----not-wave")
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 400, raw)
        self.assertIn("PCM WAV", json.loads(raw)["error"])

        body, ctype = keep_body(self.token, pcm_wav(rate=22050, frames=2205))
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 400, raw)
        self.assertIn("44100 or 48000", json.loads(raw)["error"])

        body, ctype = multipart([
            ("claim_token", self.token.encode(), None, None),
            ("dials", json.dumps(DIALS).encode(), None, None),
        ])
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 400, raw)
        self.assertIn("audio", json.loads(raw)["error"])

        # Longer than the job plus 30 s. Mono 44.1 kHz keeps the fixture smaller.
        long = pcm_wav(rate=44100, channels=1, bits=16, frames=44100 * 32)
        self._store(job, public=True, music_ends=1)
        body, ctype = keep_body(self.token, long)
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 400, raw)
        self.assertIn("30s of tail", json.loads(raw)["error"])
        self.assertIsNone(self.app.storage.get(f"performances/{job}/kept.wav"))

        import nc_perform.keep as K
        old = K.KEPT_WAV_MAX_BYTES
        K.KEPT_WAV_MAX_BYTES = 50
        try:
            body, ctype = keep_body(self.token, pcm_wav(frames=48))
            self.assertLess(len(body), 50 + KEPT_FORM_OVERHEAD)
            status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        finally:
            K.KEPT_WAV_MAX_BYTES = old
        self.assertEqual(status, 400, raw)
        self.assertIn("50 bytes", json.loads(raw)["error"])

        self._store(job, tombstone={"reason": "taken down"})
        body, ctype = keep_body(self.token, wav)
        status, raw = _post(self.api + f"/api/jobs/{job}/keep", body, {"Content-Type": ctype})
        self.assertEqual(status, 410, raw)

        # A multipart body used to be read as 8000 bytes of JSON and then crash.
        status, raw = _post(
            self.api + f"/api/jobs/{job}/keep",
            b"not-json-and-not-a-form",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 400, raw)
        self.assertIn("JSON", json.loads(raw)["error"])


if __name__ == "__main__":
    unittest.main()
