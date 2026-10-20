import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AFTER_SIGNING,
  KEPT_TAIL_S,
  KEPT_WAV_MAX_BYTES,
  TAKE_AUDIO_MAX_BYTES,
  TAKE_REQUEST_MAX_BYTES,
  takeAudioKind,
  takeTooLargeMessage,
  KeepReject,
  dialsLine,
  inspectWav,
  jobLengthS,
  keptFiles,
  parseDials,
  parseMultipart,
  tooLargeMessage,
} from "./keep.ts";

const DIALS = {
  cutoff: 40,
  cutoff_hz: 1200,
  delay_timing: "1/8 dotted",
  delay_beats: 0.75,
  delay_s: 0.4,
  feedback: 0.35,
  wet: 0.5,
  loop: false,
};

function pcmWav(opts: { rate?: number; channels?: number; bits?: number; frames?: number; data?: Uint8Array; audioFormat?: number } = {}) {
  const rate = opts.rate ?? 48000;
  const channels = opts.channels ?? 2;
  const bits = opts.bits ?? 16;
  const block = channels * (bits / 8);
  const data = opts.data ?? new Uint8Array((opts.frames ?? 480) * block);
  const fmt = new Uint8Array(16);
  const view = new DataView(fmt.buffer);
  view.setUint16(0, opts.audioFormat ?? 1, true);
  view.setUint16(2, channels, true);
  view.setUint32(4, rate, true);
  view.setUint32(8, rate * block, true);
  view.setUint16(12, block, true);
  view.setUint16(14, bits, true);
  const fmtChunk = concat([ascii("fmt "), u32(fmt.length), fmt]);
  const dataChunk = concat([ascii("data"), u32(data.length), data]);
  const payload = concat([ascii("WAVE"), fmtChunk, dataChunk]);
  return concat([ascii("RIFF"), u32(payload.length), payload]);
}

function multipart(fields: Array<{ name: string; value: Uint8Array; filename?: string; type?: string }>, boundary = "----nc-keep") {
  const chunks: Uint8Array[] = [];
  for (const field of fields) {
    let head = `--${boundary}\r\nContent-Disposition: form-data; name="${field.name}"`;
    if (field.filename) head += `; filename="${field.filename}"`;
    head += "\r\n";
    if (field.type) head += `Content-Type: ${field.type}\r\n`;
    head += "\r\n";
    chunks.push(ascii(head), field.value, ascii("\r\n"));
  }
  chunks.push(ascii(`--${boundary}--\r\n`));
  return { body: concat(chunks), contentType: `multipart/form-data; boundary=${boundary}` };
}

test("the dialed WAV cap is 40 MiB and the tail allowance is 30 s", () => {
  assert.equal(KEPT_WAV_MAX_BYTES, 40 * 1024 * 1024);
  assert.equal(KEPT_TAIL_S, 30);
  assert.match(tooLargeMessage(1), /41943040 bytes \(40 MiB\)/);
});

test("PCM 44.1 and 48 kHz pass, and the job length comes from the record", () => {
  const wide = inspectWav(pcmWav({ frames: 48000 }), 31);
  assert.equal(wide.sampleRate, 48000);
  assert.equal(wide.durationS, 1);
  const cd = inspectWav(pcmWav({ rate: 44100, channels: 1, frames: 44100 }), 31);
  assert.equal(cd.sampleRate, 44100);
  assert.equal(cd.channels, 1);
  assert.equal(jobLengthS({ performance: { music_ends_at_s: 8, outputs: [{ duration_s: 12 }] } }), 8);
  assert.equal(jobLengthS({ performance: { outputs: [{ duration_s: 12 }] } }), 12);
  assert.equal(jobLengthS(null), 180);
  inspectWav(pcmWav({ frames: 48000 }), 1);
  inspectWav(pcmWav({ frames: 48000 }), 0.96);
  assert.throws(() => inspectWav(pcmWav({ frames: 48000 }), 0.9), /at most 0\.9s/);
});

test("a bad header, the wrong rate, and a truncated file are refused", () => {
  assert.throws(() => inspectWav(ascii("not a wav"), 30), (err: unknown) => {
    assert.ok(err instanceof KeepReject);
    assert.match(err.message, /PCM WAV/);
    return true;
  });
  assert.throws(() => inspectWav(pcmWav({ rate: 22050, frames: 22050 }), 30), /44100 or 48000/);
  const cut = pcmWav();
  assert.throws(() => inspectWav(cut.subarray(0, cut.length - 8), 30), /truncated/);
  assert.throws(() => inspectWav(pcmWav({ bits: 8, frames: 48 }), 30), /16-bit or 24-bit/);
  assert.throws(() => inspectWav(pcmWav({ audioFormat: 3, frames: 48 }), 30), /format 1/);
  const wide = inspectWav(pcmWav({ bits: 24, frames: 48 }), 30);
  assert.equal(wide.bits, 24);
});

test("dials match the create page, and the multipart WAV is not trimmed", () => {
  const dials = parseDials(JSON.stringify(DIALS));
  assert.equal(dials.delay_timing, "1/8 dotted");
  assert.equal(dials.cutoff_hz, 1200);
  assert.equal(dials.loop, false);
  assert.equal(parseDials(JSON.stringify({ ...DIALS, cutoff: 0, cutoff_hz: null, wet: 0 })).cutoff_hz, null);
  assert.throws(() => parseDials(JSON.stringify({ ...DIALS, feedback: 0.96 })), /feedback/);
  assert.throws(() => parseDials(JSON.stringify({ ...DIALS, loop: "yes" })), /loop/);
  assert.match(dialsLine(dials), /1\/8 dotted/);
  assert.match(dialsLine(dials), /1200 Hz/);

  const data = new Uint8Array(480 * 4);
  data[data.length - 2] = 0x0d;
  data[data.length - 1] = 0x0a;
  data[10] = 0xff;
  const wav = pcmWav({ data });
  const form = multipart([
    { name: "claim_token", value: ascii("token") },
    { name: "dials", value: ascii(JSON.stringify(DIALS)) },
    { name: "audio", value: wav, filename: "h-23456789abcd.live.wav", type: "audio/wav" },
  ]);
  const parts = parseMultipart(form.body, form.contentType);
  assert.deepEqual(parts.audio, wav);
  assert.equal(new TextDecoder().decode(parts.claim_token), "token");
  assert.equal(JSON.parse(new TextDecoder().decode(parts.dials)).wet, 0.5);

  const pub = keptFiles({ live_dials: dials, live_audio: { mp3: null } }, "https://netcomposer.art", "h-23456789abcd");
  assert.equal(pub?.files.kept_wav, "https://netcomposer.art/api/jobs/h-23456789abcd/files/kept.wav");
  assert.equal(pub?.files.kept_mp3, undefined);
  assert.equal(pub?.kept_note, AFTER_SIGNING);
  assert.equal(keptFiles(null, "https://netcomposer.art", "h-1"), null);
});

test("a saved take is wav or flac and at most 40 MiB", () => {
  assert.equal(TAKE_AUDIO_MAX_BYTES, 40 * 1024 * 1024);
  assert.equal(TAKE_REQUEST_MAX_BYTES, TAKE_AUDIO_MAX_BYTES + 64 * 1024 + 16 * 1024);
  assert.equal(takeAudioKind(new Uint8Array([0x66, 0x4c, 0x61, 0x43, 0, 0, 0, 0])), "flac");
  const wav = new Uint8Array(12);
  wav.set([0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x41, 0x56, 0x45]);
  assert.equal(takeAudioKind(wav), "wav");
  assert.equal(takeAudioKind(new Uint8Array([0xff, 0xfb, 0x90, 0x00])), null);
  assert.match(takeTooLargeMessage(50 * 1024 * 1024), /40 MiB/);
});

function ascii(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function u32(n: number): Uint8Array {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, n, true);
  return out;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const len = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(len);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}
