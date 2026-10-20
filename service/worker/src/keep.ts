/**
 * Processed Keep: a browser-rendered WAV with the Live dials.
 *
 * A JSON {claim_token} keep still stores nothing new. A multipart keep checks
 * this WAV and stores it as kept.wav. The signed record is never rewritten.
 *
 * Size cap: 40 MiB (41943040 bytes). 180 s of music plus the 30 s tail is
 * 210 s. Stereo 16-bit PCM at 48 kHz is 192000 bytes/s, so that file is
 * 40320000 bytes plus a 44-byte header and fits. The create page sends that
 * encoding. Stereo 24-bit at the same length is about 60.5 MB and does not
 * fit; a short 24-bit file under the cap is still accepted. The Worker has
 * no ffmpeg, so it stores the WAV only.
 */

export const KEPT_WAV = "kept.wav";
export const KEPT_MP3 = "kept.mp3";
export const EXPIRE_AUDIO = ["performance.flac", "preview.mp3", KEPT_WAV, KEPT_MP3] as const;
export const KEPT_TAIL_S = 30;
/** 40 MiB. Confirm with Petrus before raising it. */
export const KEPT_WAV_MAX_BYTES = 40 * 1024 * 1024;
export const KEPT_FORM_OVERHEAD = 64 * 1024;
/** Same 40 MiB cap as a dialed WAV. The take is WAV or FLAC. */
export const TAKE_AUDIO_MAX_BYTES = KEPT_WAV_MAX_BYTES;
/** take_state is at most the 64 KiB plan cap, plus the other form fields. */
export const TAKE_REQUEST_MAX_BYTES = TAKE_AUDIO_MAX_BYTES + 64 * 1024 + 16 * 1024;
export const DEFAULT_JOB_LENGTH_S = 180;
const DURATION_EPS_S = 0.05;

export const AFTER_SIGNING =
  "Live dials were applied in the browser after the record was signed. " +
  "The signed record is the original render.";

export class KeepReject extends Error {
  constructor(message: string) {
    super(message);
    this.name = "KeepReject";
  }
}

export type WavInfo = {
  sampleRate: number;
  channels: number;
  bits: number;
  frames: number;
  durationS: number;
};

export type LiveDials = {
  cutoff: number;
  cutoff_hz: number | null;
  delay_timing: string;
  delay_beats: number;
  delay_s: number;
  feedback: number;
  wet: number;
  loop: boolean;
};

export function takeAudioKind(audio: Uint8Array): "wav" | "flac" | null {
  if (
    audio.length >= 12 &&
    audio[0] === 0x52 && audio[1] === 0x49 && audio[2] === 0x46 && audio[3] === 0x46 &&
    audio[8] === 0x57 && audio[9] === 0x41 && audio[10] === 0x56 && audio[11] === 0x45
  ) return "wav";
  if (audio.length >= 4 && audio[0] === 0x66 && audio[1] === 0x4c && audio[2] === 0x61 && audio[3] === 0x43) return "flac";
  return null;
}

export function takeTooLargeMessage(n: number): string {
  return `The take is too large (${n} bytes). The limit is ${TAKE_AUDIO_MAX_BYTES} bytes (40 MiB).`;
}

export function tooLargeMessage(n: number): string {
  const cap = KEPT_WAV_MAX_BYTES;
  const label = cap % (1024 * 1024) === 0 ? `${cap / (1024 * 1024)} MiB` : `${cap} bytes`;
  return `The dialed audio is too large (${n} bytes). The limit is ${cap} bytes (${label}).`;
}

export function jobLengthS(record: Record<string, unknown> | null): number {
  const perf = (record?.performance as Record<string, unknown> | undefined) || {};
  const music = perf.music_ends_at_s;
  if (typeof music === "number" && Number.isFinite(music) && music > 0) return music;
  const outputs = perf.outputs;
  if (Array.isArray(outputs)) {
    for (const output of outputs) {
      const duration = (output as Record<string, unknown> | null)?.duration_s;
      if (typeof duration === "number" && Number.isFinite(duration) && duration > 0) return duration;
    }
  }
  return DEFAULT_JOB_LENGTH_S;
}

export function inspectWav(blob: Uint8Array, maxDurationS: number): WavInfo {
  if (blob.length < 12 || ascii(blob, 0, 4) !== "RIFF" || ascii(blob, 8, 4) !== "WAVE") {
    throw new KeepReject("audio is not a PCM WAV.");
  }
  let offset = 12;
  let fmt: Uint8Array | null = null;
  let dataSize: number | null = null;
  const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
  while (offset + 8 <= blob.length) {
    const size = view.getUint32(offset + 4, true);
    const start = offset + 8;
    const end = start + size;
    if (end > blob.length) throw new KeepReject("WAV data is truncated.");
    const id = ascii(blob, offset, 4);
    if (id === "fmt " && !fmt) fmt = blob.subarray(start, end);
    else if (id === "data" && dataSize === null) dataSize = size;
    offset = end + (size & 1);
    if (offset > blob.length) break;
  }
  if (!fmt || fmt.length < 16 || dataSize === null) throw new KeepReject("audio is not a PCM WAV.");
  const fmtView = new DataView(fmt.buffer, fmt.byteOffset, fmt.byteLength);
  const audioFormat = fmtView.getUint16(0, true);
  const channels = fmtView.getUint16(2, true);
  const rate = fmtView.getUint32(4, true);
  const byteRate = fmtView.getUint32(8, true);
  const blockAlign = fmtView.getUint16(12, true);
  const bits = fmtView.getUint16(14, true);
  if (audioFormat !== 1) throw new KeepReject("WAV must be PCM (format 1).");
  if (channels !== 1 && channels !== 2) throw new KeepReject("WAV must be mono or stereo.");
  if (bits !== 16 && bits !== 24) throw new KeepReject("WAV must be 16-bit or 24-bit PCM.");
  if (rate !== 44100 && rate !== 48000) throw new KeepReject("WAV sample rate must be 44100 or 48000 Hz.");
  const expectBlock = channels * (bits / 8);
  if (blockAlign !== expectBlock || byteRate !== rate * expectBlock) throw new KeepReject("WAV header is inconsistent.");
  if (dataSize % blockAlign !== 0) throw new KeepReject("WAV data is truncated.");
  const frames = dataSize / blockAlign;
  if (frames < 1) throw new KeepReject("WAV has no audio frames.");
  const duration = frames / rate;
  if (duration > maxDurationS + DURATION_EPS_S) {
    throw new KeepReject(
      `The dialed audio is ${duration.toFixed(1)}s. ` +
        `This Performance allows at most ${maxDurationS.toFixed(1)}s ` +
        `(the job length plus ${KEPT_TAIL_S}s of tail).`,
    );
  }
  return { sampleRate: rate, channels, bits, frames, durationS: duration };
}

export function parseDials(raw: string): LiveDials {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    throw new KeepReject("dials must be JSON.");
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new KeepReject("dials must be a JSON object.");
  const rec = obj as Record<string, unknown>;
  const num = (name: string, lo: number, hi: number): number => {
    if (!(name in rec)) throw new KeepReject(`dials.${name} is required.`);
    const value = rec[name];
    if (typeof value !== "number" || !Number.isFinite(value)) throw new KeepReject(`dials.${name} must be a number.`);
    if (value < lo || value > hi) throw new KeepReject(`dials.${name} must be between ${lo} and ${hi}.`);
    return value;
  };
  let cutoffHz: number | null = null;
  if ("cutoff_hz" in rec && rec.cutoff_hz !== null) {
    if (typeof rec.cutoff_hz !== "number" || !Number.isFinite(rec.cutoff_hz)) {
      throw new KeepReject("dials.cutoff_hz must be a number or null.");
    }
    if (rec.cutoff_hz < 20 || rec.cutoff_hz > 24000) {
      throw new KeepReject("dials.cutoff_hz must be between 20 and 24000, or null when the filter is open.");
    }
    cutoffHz = rec.cutoff_hz;
  }
  const timing = rec.delay_timing;
  if (typeof timing !== "string" || !timing.trim() || timing.length > 40) {
    throw new KeepReject("dials.delay_timing must be a short string.");
  }
  if (typeof rec.loop !== "boolean") throw new KeepReject("dials.loop must be true or false.");
  return {
    cutoff: num("cutoff", 0, 100),
    cutoff_hz: cutoffHz,
    delay_timing: timing,
    delay_beats: num("delay_beats", 0, 8),
    delay_s: num("delay_s", 0, 5),
    feedback: num("feedback", 0, 0.95),
    wet: num("wet", 0, 1),
    loop: rec.loop,
  };
}

export function liveAudioMeta(info: WavInfo, hasMp3: boolean) {
  return {
    wav: KEPT_WAV,
    mp3: hasMp3 ? KEPT_MP3 : null,
    sample_rate: info.sampleRate,
    channels: info.channels,
    bits_per_sample: info.bits,
    duration_s: Math.round(info.durationS * 1000) / 1000,
    note: AFTER_SIGNING,
  };
}

export function dialsLine(dials: LiveDials): string {
  const hz = dials.cutoff_hz === null ? "filter open" : `${dials.cutoff_hz} Hz`;
  const loop = dials.loop ? "on" : "off";
  return (
    `cutoff ${dials.cutoff} (${hz}), ` +
    `delay ${dials.delay_timing} (${dials.delay_beats} beats, ${dials.delay_s} s), ` +
    `feedback ${dials.feedback}, wet ${dials.wet}, loop ${loop}`
  );
}

export type KeptPublic = {
  files: { kept_wav: string; kept_mp3?: string };
  live_dials: LiveDials;
  kept_note: string;
};

export function keptFiles(
  meta: { live_dials?: LiveDials | null; live_audio?: { mp3?: string | null } | null } | null,
  base: string,
  id: string,
): KeptPublic | null {
  if (!meta?.live_dials) return null;
  const files: { kept_wav: string; kept_mp3?: string } = {
    kept_wav: `${base}/api/jobs/${id}/files/${KEPT_WAV}`,
  };
  if (meta.live_audio?.mp3) files.kept_mp3 = `${base}/api/jobs/${id}/files/${KEPT_MP3}`;
  return { files, live_dials: meta.live_dials, kept_note: AFTER_SIGNING };
}

export function parseMultipart(body: Uint8Array, contentType: string): Record<string, Uint8Array> {
  const found = /boundary=([^;]+)/i.exec(contentType);
  if (!found) throw new KeepReject("Keep the dialed audio as multipart/form-data.");
  const boundary = found[1].trim().replace(/^"|"$/g, "");
  const marker = new TextEncoder().encode(`--${boundary}`);
  const positions: number[] = [];
  let from = 0;
  while (from < body.length) {
    const at = indexOfBytes(body, marker, from);
    if (at < 0) break;
    positions.push(at);
    from = at + marker.length;
  }
  const parts: Record<string, Uint8Array> = {};
  for (let i = 0; i < positions.length; i++) {
    const chunkStart = positions[i] + marker.length;
    const chunkEnd = i + 1 < positions.length ? positions[i + 1] : body.length;
    const chunk = body.subarray(chunkStart, chunkEnd);
    const split = splitHead(chunk);
    if (!split) continue;
    const head = new TextDecoder().decode(split.head);
    if (!head.includes("Content-Disposition")) continue;
    const name = /name="([^"]+)"/.exec(head);
    if (!name) continue;
    parts[name[1]] = split.data;
  }
  return parts;
}

function splitHead(chunk: Uint8Array): { head: Uint8Array; data: Uint8Array } | null {
  // The boundary marker is followed by one CRLF, then the headers.
  let body = chunk;
  if (body.length >= 2 && body[0] === 13 && body[1] === 10) body = body.subarray(2);
  else if (body.length >= 1 && body[0] === 10) body = body.subarray(1);
  const crlf = indexOfBytes(body, new Uint8Array([13, 10, 13, 10]), 0);
  const lf = indexOfBytes(body, new Uint8Array([10, 10]), 0);
  let sep = -1;
  let width = 4;
  if (crlf >= 0 && (lf < 0 || crlf <= lf)) sep = crlf;
  else if (lf >= 0) {
    sep = lf;
    width = 2;
  }
  if (sep < 0) return null;
  // One CRLF before the next boundary belongs to the delimiter, not the file.
  return { head: body.subarray(0, sep), data: stripOneCrlf(body.subarray(sep + width)) };
}

function stripOneCrlf(data: Uint8Array): Uint8Array {
  if (data.length >= 2 && data[data.length - 2] === 13 && data[data.length - 1] === 10) {
    return data.subarray(0, data.length - 2);
  }
  if (data.length >= 1 && data[data.length - 1] === 10) return data.subarray(0, data.length - 1);
  return data;
}

function indexOfBytes(hay: Uint8Array, needle: Uint8Array, from: number): number {
  if (!needle.length) return from;
  const last = hay.length - needle.length;
  for (let i = from; i <= last; i++) {
    let ok = true;
    for (let j = 0; j < needle.length; j++) {
      if (hay[i + j] !== needle[j]) {
        ok = false;
        break;
      }
    }
    if (ok) return i;
  }
  return -1;
}

function ascii(bytes: Uint8Array, offset: number, length: number): string {
  let out = "";
  for (let i = 0; i < length; i++) out += String.fromCharCode(bytes[offset + i] || 0);
  return out;
}
