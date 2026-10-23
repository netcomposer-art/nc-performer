/**
 * Copies into R2 without holding the file in Worker memory.
 *
 * A Worker isolate has 128 MB. Gather copies eight files at once, and a
 * University of Iowa note file can be one 50+ MB AIFF of a whole chromatic
 * run (guitar Em7 from 4 notes was about 160 MB of parts). Buffering each copy
 * with arrayBuffer() put several hundred MB in memory, the isolate was
 * stopped, and the Gather never left "Gathering… 99%".
 */

export type Body = ReadableStream<Uint8Array> | ArrayBuffer;
/** In the Worker: (n) => new FixedLengthStream(n), so R2 knows the length of a streamed body. */
export type Sized = (length: number) => TransformStream<Uint8Array, Uint8Array>;

/** A positive whole byte count, or null. */
export function byteLength(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Store one HTTP response. With a Content-Length the body is streamed through
 * sized(length) into put. Without one it falls back to the old buffered copy.
 * false (nothing stored) for a non-OK or empty response.
 */
export async function storeResponse(res: Response, put: (body: Body) => Promise<unknown>, sized: Sized): Promise<boolean> {
  if (!res.ok) {
    await res.body?.cancel().catch(() => {});
    return false;
  }
  const length = byteLength(res.headers.get("content-length"));
  if (res.body && length) {
    await put(res.body.pipeThrough(sized(length)));
    return true;
  }
  if (res.headers.get("content-length") === "0") {
    await res.body?.cancel().catch(() => {});
    return false;
  }
  const bytes = await res.arrayBuffer();
  if (!bytes.byteLength) return false;
  await put(bytes);
  return true;
}

/** Copy one stored object (R2 to R2) as a stream of its known size. */
export async function storeObject(
  obj: { body: ReadableStream<Uint8Array>; size: number } | null,
  put: (body: Body) => Promise<unknown>,
  sized: Sized,
): Promise<boolean> {
  if (!obj) return false;
  const length = byteLength(obj.size);
  if (!length) {
    await obj.body.cancel().catch(() => {});
    return false;
  }
  await put(obj.body.pipeThrough(sized(length)));
  return true;
}
