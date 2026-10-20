/**
 * What the page fetches the moment a job says done.
 * uses.json is in this list because a finished render always writes it.
 * A kept WAV is not: it arrives later, from Keep.
 */
export const REQUIRED_OUTPUTS = [
  "performance.flac",
  "preview.mp3",
  "record.json",
  "plan.json",
  "uses.json",
  "composition.md",
  "meta.json",
  "status.json",
] as const;

export const STORING_LINE = "Storing the Performance…";

export type ContainerStatus = {
  state?: string;
  stage?: string;
  percent?: number;
  stage_line?: string;
};

export type PageUpdate = {
  state: string;
  stage: string;
  percent: number;
  stage_line?: string;
};

/**
 * The container reports done when its own disk has the files. The page must
 * keep seeing storing until those bytes have been read back from R2.
 */
export function containerPageUpdate(last: ContainerStatus): PageUpdate {
  if (last.state === "done") {
    return { state: "storing", stage: "storing", percent: 90, stage_line: STORING_LINE };
  }
  const update: PageUpdate = {
    state: last.state || "running",
    stage: last.stage || last.state || "running",
    percent: Number(last.percent || 0),
  };
  if (typeof last.stage_line === "string" && last.stage_line.trim()) update.stage_line = last.stage_line;
  return update;
}

/** A stored object counts only when it has a size and the first byte came back. */
export function readableBytes(size: number | null | undefined, first: number | null | undefined): boolean {
  return typeof size === "number" && size > 0 && typeof first === "number" && first > 0;
}

/** Names in REQUIRED_OUTPUTS that were not read. uses.json is required; a kept WAV is not. */
export function missingOutputs(readable: readonly string[]): string[] {
  const have = new Set(readable);
  return REQUIRED_OUTPUTS.filter((name) => !have.has(name));
}
