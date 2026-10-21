/**
 * Gather progress for the page, finer than the fixed stage percent.
 *
 * The container reports 0..CONTAINER_END while it searches, picks, downloads,
 * copies or mixes note stacks, and stores (ncperf/progress.py GatherMeter).
 * The Worker's copy of each file to R2 takes it from there to SAVE_END. 100
 * is only for a finished Gather. The number never goes down.
 */
export const CONTAINER_END = 85;
export const SAVE_END = 99;

export type GatherProgress = {
  progress?: number;
  progress_stage?: string;
  progress_label?: string;
  progress_done?: number;
  progress_total?: number;
};

export const PROGRESS_KEYS = ["progress", "progress_stage", "progress_label", "progress_done", "progress_total"] as const;

/** The progress fields of a container payload, or {} when it has none. */
export function progressFields(src: unknown): GatherProgress {
  if (!src || typeof src !== "object") return {};
  const row = src as Record<string, unknown>;
  if (typeof row.progress !== "number" || !Number.isFinite(row.progress)) return {};
  const out: GatherProgress = { progress: Math.max(0, Math.min(SAVE_END, Math.floor(row.progress))) };
  if (typeof row.progress_stage === "string") out.progress_stage = row.progress_stage;
  if (typeof row.progress_label === "string") out.progress_label = row.progress_label;
  if (typeof row.progress_done === "number") out.progress_done = row.progress_done;
  if (typeof row.progress_total === "number") out.progress_total = row.progress_total;
  return out;
}

/** Drop a progress patch that would move the number back. */
export function keepForward<T extends GatherProgress>(current: GatherProgress | null | undefined, fields: T): T {
  if (typeof fields.progress !== "number") return fields;
  const was = typeof current?.progress === "number" ? current.progress : -1;
  if (fields.progress >= was) return fields;
  const out = { ...fields } as Record<string, unknown>;
  for (const key of PROGRESS_KEYS) delete out[key];
  return out as T;
}

/** The R2 copy step: one unit per file, then one per note stack mixed again. */
export class SaveMeter {
  private done = 0;
  private total: number;
  private from: number;
  private shown: number;

  constructor(total: number, from = CONTAINER_END) {
    this.total = Math.max(0, total);
    this.from = Math.max(0, Math.min(CONTAINER_END, from));
    this.shown = this.from;
  }

  /** More units became known, such as a note stack that has to be mixed again. */
  grow(n: number): void {
    if (n > 0) this.total += n;
  }

  tick(): GatherProgress {
    if (this.done < this.total) this.done += 1;
    return this.fields();
  }

  fields(): GatherProgress {
    const frac = this.total ? this.done / this.total : 0;
    this.shown = Math.max(this.shown, Math.floor(this.from + (SAVE_END - this.from) * frac));
    return {
      progress: Math.min(SAVE_END, this.shown),
      progress_stage: "saving",
      progress_label: "Saving the recordings",
      progress_done: this.done,
      progress_total: this.total,
    };
  }
}

/** A finished Gather. */
export function doneProgress(total?: number): GatherProgress {
  return {
    progress: 100,
    progress_stage: "done",
    progress_label: "Gathered",
    ...(typeof total === "number" ? { progress_done: total, progress_total: total } : {}),
  };
}
