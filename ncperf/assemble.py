"""Raw assembly: one clip per chord, placed in harmonic order.

A Gather finds an openly licensed recording that is that chord. Perform trims
each clip to its slot, fades the edges, matches loudness, and guards the peak.
Nothing else runs: no layers, no reverb, no room tone, no mastering, no
time-stretch, and no pitch-shift.

A written slash bass prefers the voicing whose bass matches. Otherwise the
chord recording and one indexed bass note are sounded together. If that note
is not in the index, the chord is kept on its own. A bass never drops the chord.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextvars

import numpy as np
import soundfile as sf

from . import chords as CH
from . import deadline
from . import discover
from . import fetch
from . import interpret
from . import licenses as LIC
from . import notemap
from . import progress
from . import take
from .parallel import run_all
from .util import SR, decode

# A few milliseconds, long enough to hide a click and short enough that the
# chords still meet. The same window is the fade and the crossfade.
FADE_S = 0.012
CLIP_LUFS = -20.0
PEAK = 0.98
# The whole Gather stops here. A supplier that is still out does not hold the list.
GATHER_DEADLINE_S = 8.0
# Fast suppliers get this long to join the pool. A slow one does not move the first chord.
GATHER_SETTLE_S = 1.0


def plan_raw(
    comp: dict,
    seed: int = 1,
    instrument: str | None = None,
    host_ok_fn=None,
    searchers=None,
    registry_url=None,
    verify_live: bool = True,
    note_maps=None,
    progress_cb=None,
    origin_budget_s: float = 4.0,
    avoid_keys=None,
    avoid_sample_ids=None,
    gather_deadline_s: float = GATHER_DEADLINE_S,
    settle_s: float = GATHER_SETTLE_S,
) -> dict:
    """One clip per chord. Whole-chord files first; indexed notes only as a stack.

    ``instrument`` is ignored. The words choose the ensemble when they name
    one. Otherwise each family that can play the chords is equally likely.
    """
    del instrument
    comp_text = comp.get("composition") or ""
    found = take.composition_chords(comp_text) or CH.find_full(
        f"{comp_text} {comp.get('performance') or ''}"
    )
    if not found:
        raise ValueError('no chord symbols found in the Composition (e.g. "Cm, Am, F")')
    words = f"{comp.get('composition') or ''} {comp.get('performance') or ''}"
    syms = [sym for sym, _bass in found]
    hint = None
    previous_exclude = discover._EXCLUDE
    discover._EXCLUDE = discover.excluded_terms(words)
    try:
        return _plan(
            comp, found, syms, words, comp_text, seed, hint, host_ok_fn, searchers,
            registry_url, verify_live, note_maps, progress_cb, origin_budget_s,
            avoid_keys, avoid_sample_ids, gather_deadline_s, settle_s,
        )
    finally:
        discover._EXCLUDE = previous_exclude


def preview_clip(plan: dict, clip: dict) -> bytes:
    """One chord as a wav. A stack is summed here; a single file is that file's audio."""
    import io

    sources = plan.get("sources") or {}
    parts = []
    for sid in clip.get("sids") or []:
        src = sources.get(sid)
        if not isinstance(src, dict):
            continue
        got = fetch.resolve(dict(src))
        parts.append(_slice(decode(got["path"]), src))
    slot_n = max(1, int(round(float(clip.get("dur") or 1) * SR)))
    audible, stacked = _stack(parts, slot_n)
    matched, _gain = _match_level(stacked, audible)
    shaped = _fade_audible(matched, audible, min(max(1, int(round(FADE_S * SR))), audible))
    buf = io.BytesIO()
    sf.write(buf, shaped, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def render(plan: dict, out_dir: str, name: str = "performance", log=print):
    """Place the gathered clips in order and write FLAC, MP3, and uses.json."""
    from . import util

    os.makedirs(out_dir, exist_ok=True)
    util.Q.update(resampler="soxr", stretch_engine="auto")
    util.USED["resampler"].clear()
    util.USED["stretch"].clear()
    sources = plan.get("sources") or {}
    clips = list(plan.get("clips") or [])
    if not clips:
        raise RuntimeError("raw assembly has no clips")
    resolved: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def load(sid: str) -> None:
        src = sources.get(sid)
        if not isinstance(src, dict):
            errors[sid] = "missing"
            return
        try:
            got = fetch.resolve(dict(src))
            got["y"] = _slice(decode(got["path"]), src)
            resolved[sid] = got
            title = str(got.get("title") or "")[:60]
            log(f"  source {sid}: {got['license']['name']} · {got['author']} · {title}")
        except Exception as exc:
            errors[sid] = str(exc)[:180]
            log(f"  source {sid}: {exc}")

    sids = []
    for clip in clips:
        for sid in clip.get("sids") or []:
            if sid not in sids:
                sids.append(sid)
    if sids:
        run_all([lambda sid=sid: load(sid) for sid in sids], workers=min(8, len(sids)))
    missing = [sid for sid in sids if sid not in resolved]
    if missing:
        detail = "; ".join(f"{sid}: {errors.get(sid, 'missing')}" for sid in missing[:6])
        raise RuntimeError(f"could not fetch {', '.join(missing[:8])}. {detail}")

    length = float(plan["length_s"])
    total = max(1, int(round(length * SR)))
    mix = np.zeros((total, 2))
    uses = []
    fade_n = max(1, int(round(FADE_S * SR)))
    chords = plan.get("chords") or []
    for clip in clips:
        index = int(clip.get("chord") or 0)
        chord = chords[index] if 0 <= index < len(chords) else {}
        slot_n = max(1, int(round(float(chord.get("dur") or clip.get("dur") or 0.1) * SR)))
        parts = []
        for sid in clip.get("sids") or []:
            y = resolved[sid].get("y")
            if y is None or len(y) == 0:
                continue
            parts.append(np.asarray(y, dtype=np.float64))
        audible, stacked = _stack(parts, slot_n)
        matched, gain_db = _match_level(stacked, audible)
        shaped = _fade_audible(matched, audible, min(fade_n, audible))
        start = int(round(float(chord.get("t") or clip.get("t") or 0) * SR))
        if index:
            start = max(0, start - fade_n)
        end = min(total, start + len(shaped))
        if end > start:
            mix[start:end] += shaped[: end - start]
        at = round(start / SR, 3)
        for sid in clip.get("sids") or []:
            uses.append({
                "key": sid,
                "layer": "chords",
                "role": "raw chord",
                "at_s": at,
                "len_s": round(audible / SR, 3),
                "gain_db": gain_db,
                "pitch_shift": False,
                "time_stretch": 1.0,
                "chord_index": index,
            })
    guarded, before = _peak_guard(mix, PEAK)
    flac = os.path.join(out_dir, name + ".flac")
    mp3 = os.path.join(out_dir, name + ".mp3")
    sf.write(flac, guarded, SR, subtype="PCM_24")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", flac, "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k", mp3],
        check=True,
    )
    logged = {}
    for sid, got in resolved.items():
        logged[sid] = {key: value for key, value in got.items() if key != "y"}
    body = {
        "seed": plan.get("seed"),
        "uses": uses,
        "selections": {},
        "measurements": [],
        "rejected": [],
        "reverb": None,
        "ir_inputs": None,
        "master": {
            "method": "peak guard on the sum",
            "mode_actual": "peak" if before > PEAK else "unity",
            "ceiling": PEAK,
            "peak_before": round(float(before), 4),
        },
        "tone": None,
        "quality": {
            "resampler": sorted(util.USED["resampler"]),
            "stretch": sorted(util.USED["stretch"]),
        },
        "sources": logged,
    }
    with open(os.path.join(out_dir, "uses.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1, default=float)
    return flac, mp3, body


def _plan(
    comp, found, syms, words, comp_text, seed, hint, host_ok_fn, searchers,
    registry_url, verify_live, note_maps, progress_cb, origin_budget_s,
    avoid_keys, avoid_sample_ids, gather_deadline_s, settle_s,
) -> dict:
    query = discover.terms_for(comp, hint, seed)
    named = discover._named_origin(words)
    order = list(discover.ORIGINS)
    rot = int(seed) % len(order)
    order = order[rot:] + order[:rot]
    if named and named in order:
        order = [named] + [origin for origin in order if origin != named]
    fns = searchers or discover.SEARCHERS
    heard = set()

    def tell(line=None, sound=None):
        if not progress_cb:
            return
        if sound:
            row = progress.clip_row(sound)
            if not row:
                sound = None
            else:
                key = ("chord", row["chord"]) if "chord" in row else (row["title"], row["supplier"], row["license"])
                # A chord can be replaced when a later supplier wins it. Search hits stay unique.
                if key in heard and "chord" not in row:
                    sound = None
                else:
                    heard.add(key)
                    sound = row
        if line or sound:
            progress_cb(line, sound)

    started = time.monotonic()
    deadline_at = started + max(0.2, float(gather_deadline_s))
    indexed = _indexed_hits(query, syms, seed, host_ok_fn, registry_url)
    live_hits: list = []
    errors: dict = {}
    pending: set[str] = set()
    live_lock = threading.Lock()
    note_box: dict = {}

    def remember(origin, hits, err):
        with live_lock:
            if err:
                errors[origin] = err
            for hit in hits or []:
                if hit:
                    live_hits.append(hit)
            pending.discard(origin)

    def query_origin(fn, origin_seed, budget):
        try:
            with deadline.limit(max(0.05, budget)):
                hits = fn(query, hint, syms, origin_seed, host_ok_fn, registry_url) or []
        except deadline.SearchTimeout:
            return [], "timed out"
        except Exception as exc:
            return [], str(exc)[:180]
        return [hit for hit in hits if hit], None

    def load_index():
        if note_maps is not None:
            note_box["maps"] = note_maps
            return
        try:
            maps, _status = notemap.ensure(syms, False, None)
        except Exception as exc:
            note_box["error"] = str(exc)[:180]
            maps = []
        note_box["maps"] = maps

    threads = []
    for origin in order:
        fn = fns.get(origin)
        if not fn:
            continue
        tell(progress.finding_line(origin, query))
        pending.add(origin)
        budget = min(float(origin_budget_s), max(0.05, deadline_at - time.monotonic()))

        def run(origin=origin, fn=fn, budget=budget):
            hits, err = query_origin(fn, seed, budget)
            remember(origin, hits, err)

        thread = threading.Thread(target=run, name=f"nc-gather-{origin}", daemon=True)
        thread.start()
        threads.append(thread)
    index_thread = threading.Thread(target=load_index, name="nc-gather-notes", daemon=True)
    index_thread.start()

    seq, slots, section_spec, slice_note = _clock(comp_text, words, found, seed)
    if not seq:
        raise ValueError('no chord symbols found in the Composition (e.g. "Cm, Am, F")')
    blocked = {str(item) for item in (avoid_keys or []) if item}
    blocked |= {str(item) for item in (avoid_sample_ids or []) if item}
    settle_at = started + max(0.0, float(settle_s))

    def snapshot():
        with live_lock:
            arrived = list(live_hits)
            still = set(pending)
        return _annotate([*indexed, *arrived], blocked, syms), still

    pool, still = snapshot()
    while still and time.monotonic() < deadline_at:
        pool, still = snapshot()
        if _covers(pool, seq) and time.monotonic() >= settle_at:
            break
        time.sleep(0.02)
        pool, still = snapshot()
    index_thread.join(timeout=max(0.0, deadline_at - time.monotonic()))
    pool, _still = snapshot()
    maps = discover._drop_avoided_notes(note_box.get("maps") or [], blocked)
    assembly = notemap.assemblies_for(syms, maps, discover._EXCLUDE)
    counts: dict[str, int] = {}
    for cand in pool:
        counts[cand.get("origin") or "?"] = counts.get(cand.get("origin") or "?", 0) + 1
    print(json.dumps({
        "event": "assemble",
        "terms": query,
        "order": order,
        "counts": counts,
        "errors": errors,
        "note_libraries": len(maps or []),
        "indexed": len(indexed),
        "deadline_s": round(float(gather_deadline_s), 3),
    }, ensure_ascii=False), flush=True)

    option_groups = _option_groups(seq, pool, assembly, hint, seed, named)
    prefer = take.prefer_from(words)

    def on_pick(index, opt):
        sym = seq[index][0]
        title = opt.get("title")
        license_id = opt.get("license")
        origin = opt.get("origin")
        if opt.get("accepted"):
            got = opt["accepted"]
            title = got.get("title") or title
            license_id = got.get("license") or license_id
            origin = (got.get("source") or {}).get("kind") or origin
        tell(None, {
            "chord": index,
            "id": f"c{index}",
            "title": title,
            "supplier": progress.origin_label(origin),
            "license": license_id,
            "symbol": sym,
        })

    chosen = _choose_files(option_groups, seq, seed, prefer, verify_live, on_pick=on_pick)
    if any(row is None for row in chosen):
        missing = [seq[i][0] for i, row in enumerate(chosen) if row is None]
        raise ValueError(
            "No exact chord recording or native-pitch notes for: "
            + ", ".join(dict.fromkeys(missing))
            + ". A pitched clip in another key was not used."
        )
    return _pack(comp, seq, slots, section_spec, chosen, seed, slice_note, prefer, maps)


def _clock(comp_text, words, found, seed):
    length = interpret._length(words)
    written = take.written_durations(comp_text, found)
    halves = None if written or take.bar_chords(comp_text) else take.half_sections(comp_text)
    slice_bits = []
    if written:
        seq = list(found)
        if len(seq) > take.CHORD_LIMIT:
            slice_bits.append(
                f"The piece had {len(seq)} chords; it was shortened to {take.CHORD_LIMIT} "
                "so the Performance stays within the chord limit."
            )
            seq = seq[: take.CHORD_LIMIT]
            written = written[: take.CHORD_LIMIT]
        length = float(sum(written))
        section_spec = [(len(seq), length)]
        slots = take.slots_from_durations(written)
    elif halves:
        left, right = halves
        half_len = float(length) / 2.0
        seq = []
        section_spec = []
        for group, seconds in ((left, half_len), (right, half_len)):
            part, part_note = take.expand_sequence(group, seconds)
            seq.extend(part)
            section_spec.append((len(part), seconds))
            if part_note:
                slice_bits.append(part_note)
        pulse = take.stated_pulse(words)
        broaden = take.broaden_bars(words) if pulse else 0
        slots = take.chord_slots(section_spec, int(seed), pulse, broaden)
    else:
        seq, one_note = take.expand_sequence(found, length)
        section_spec = [(len(seq), float(length))]
        if one_note:
            slice_bits.append(one_note)
        pulse = take.stated_pulse(words)
        broaden = take.broaden_bars(words) if pulse else 0
        slots = take.chord_slots(section_spec, int(seed), pulse, broaden)
    if seq and not written and len(slots) != len(seq):
        slots = take.chord_slots([(len(seq), float(length))], int(seed), take.stated_pulse(words), 0)
        section_spec = [(len(seq), float(length))]
    return seq, slots, section_spec, " ".join(slice_bits) or None


def _whole_opt(cand: dict) -> dict:
    return {
        "built": "whole-chord file",
        "origin": cand.get("origin"),
        "instrument": cand.get("instrument"),
        "pluck": False,
        "tier": discover._tier(cand),
        "cand": cand,
        "title": cand.get("title"),
        "author": cand.get("author"),
        "license": cand.get("license"),
        "page": cand.get("page"),
    }


def _note_opt(row: dict) -> dict:
    return {
        "built": "assembled notes",
        "origin": row.get("origin"),
        "instrument": row.get("instrument"),
        "pluck": bool(row.get("pluck")),
        "tier": row.get("tier", 0),
        "voicing": row.get("voicing"),
        "samples": row.get("samples"),
        "title": row.get("title"),
        "author": row.get("author"),
        "license": row.get("license"),
        "page": row.get("page"),
    }


def _kept_out(cand: dict, blocked: set[str]) -> bool:
    src = cand.get("source") or {}
    values = (
        cand.get("key"), src.get("url"), src.get("audio_url"), src.get("id"),
        src.get("manifest"), src.get("page"), cand.get("title"),
    )
    return any(str(value) in blocked for value in values if value not in (None, ""))


def _accept(opt: dict, sym: str, bass, verify_live: bool):
    """Credit the file. A slash bass is resolved later and does not drop the chord."""
    del sym, bass
    if opt.get("built") != "whole-chord file":
        return opt
    if opt.get("accepted"):
        return opt
    cand = opt["cand"]
    got = discover._ensure_credit(cand, verify_live, bass=None)
    if not got:
        return None
    if verify_live and (not discover._declared_on_page(got) or not discover._audio_reachable(got)):
        return None
    src = {key: value for key, value in got["source"].items() if not str(key).startswith("_")}
    got["source"] = src
    opt["accepted"] = got
    opt["title"] = got.get("title")
    opt["author"] = got.get("author")
    opt["license"] = got.get("license")
    opt["page"] = got.get("page")
    return opt


def _indexed_hits(terms, syms, seed, host_ok_fn, registry_url) -> list:
    """Whole-chord files already on disk. No network. This is the deadline fallback."""
    url = registry_url or "https://netcomposer.art/suppliers.json"
    disk = discover._read_index_disk(url)
    if not disk:
        return []
    with discover._MM_LOCK:
        discover._MM_CACHE.setdefault(url, disk)
    try:
        return discover.search_musicmatter(terms, None, syms, seed, host_ok_fn, registry_url) or []
    except Exception:
        return []


def _annotate(cands, blocked, syms) -> list:
    pool = [cand for cand in cands if not (blocked and _kept_out(cand, blocked))]
    for cand in pool:
        if cand.get("chord_symbol"):
            spelled = CH.spell(str(cand["chord_symbol"])) or cand["chord_symbol"]
            cand["chord_symbol"] = spelled
            if spelled not in syms:
                cand["_wrong_chord"] = True
        elif discover._declared_chord(cand.get("title") or "", syms):
            declared = discover._declared_chord(cand.get("title") or "", syms)
            cand["chord_symbol"] = declared
            cand["voicing_kind"] = cand.get("voicing_kind") or "exact"
            cand["pitched"] = True
        if cand.get("origin") != "musicmatter" and not discover._rights_ok(discover._blob(cand)):
            cand["_blocked"] = True
    return pool


def _covers(pool, seq) -> bool:
    have = {
        cand.get("chord_symbol")
        for cand in pool
        if cand.get("chord_symbol") and not cand.get("_wrong_chord") and not cand.get("_blocked") and cand.get("origin") != "uiowa"
    }
    return all(sym in have for sym, _bass in seq)


def _option_groups(seq, pool, assembly, hint, seed, named) -> list:
    option_groups = []
    for index, (sym, _bass) in enumerate(seq):
        whole = []
        usable = [
            cand for cand in pool
            if cand.get("chord_symbol") == sym
            and not cand.get("_wrong_chord")
            and not cand.get("_blocked")
            and cand.get("origin") != "uiowa"
        ]
        for cand in discover._strike_order(usable, hint, seed, index, named):
            whole.append(_whole_opt(cand))
        notes = []
        for row in assembly.get(sym) or []:
            if any(discover._hint_ok(row.get("instrument") or "", word) for word in discover._EXCLUDE):
                continue
            notes.append(_note_opt(row))
        # A whole-chord recording is the clip. Notes are only the fallback
        # for a chord no recording covers, so a note library is not a second
        # piano entry beside the piano files.
        option_groups.append(whole or notes)
    if named and option_groups and all(any(row.get("origin") == named for row in group) for group in option_groups):
        option_groups = [[row for row in group if row.get("origin") == named] for group in option_groups]
    return option_groups


def _heard_bass(opt) -> int | None:
    if not opt or opt.get("built") != "whole-chord file":
        return None
    accepted = opt.get("accepted") or {}
    cand = opt.get("cand") or {}
    value = accepted.get("bass_pc", cand.get("bass_pc"))
    if value is None:
        return None
    try:
        return int(value) % 12
    except (TypeError, ValueError):
        return None


def _place_bass(chosen, groups, seq, verify_live):
    """Prefer a matching bass in the chosen instrument. Else mark a bass note.

    The ensemble stays the one already chosen. A bass miss does not empty the chord.
    """
    for index, opt in enumerate(chosen):
        if not opt or opt.get("built") != "whole-chord file":
            continue
        _sym, bass = seq[index]
        if bass is None:
            continue
        bass = int(bass) % 12
        if _heard_bass(opt) == bass:
            continue
        voice = (opt.get("origin"), opt.get("instrument"))
        replacement = None
        for row in groups[index]:
            if row is opt or row.get("built") != "whole-chord file":
                continue
            if (row.get("origin"), row.get("instrument")) != voice:
                continue
            hinted = (row.get("cand") or {}).get("bass_pc")
            if hinted is None or int(hinted) % 12 != bass:
                continue
            got = row if row.get("accepted") else _accept(row, _sym, None, verify_live)
            if got and _heard_bass(got) == bass:
                replacement = got
                break
        if replacement is not None:
            chosen[index] = replacement
        else:
            opt["bass_note_pc"] = bass
    return chosen


def _choose_files(groups, seq, seed, prefer, verify_live, on_pick=None):
    dead = [set() for _ in groups]
    for salt in range(24):
        current = []
        for index, group in enumerate(groups):
            current.append([
                row for row in group
                if not (row.get("built") == "whole-chord file" and id(row.get("cand")) in dead[index])
            ])
        if not any(current):
            return [None] * len(groups)
        chosen, _primary = take.choose(current, int(seed) + salt, prefer, None, False)
        if any(row is None for row in chosen):
            return chosen
        pending = [
            index for index, opt in enumerate(chosen)
            if opt and opt.get("built") == "whole-chord file" and not opt.get("accepted")
        ]
        # Notes and files that are already accepted need no network. List them now.
        if on_pick:
            for index, opt in enumerate(chosen):
                if not opt or index in pending:
                    continue
                on_pick(index, opt)

        def one(index):
            sym, bass = seq[index]
            return index, _accept(chosen[index], sym, bass, verify_live)

        failed = []
        if pending:
            contexts = [contextvars.copy_context() for _ in pending]
            width = max(1, min(8, len(pending)))
            with ThreadPoolExecutor(max_workers=width, thread_name_prefix="nc-accept") as pool:
                futs = [pool.submit(ctx.run, one, index) for ctx, index in zip(contexts, pending)]
                for fut in as_completed(futs):
                    index, got = fut.result()
                    if got is None:
                        failed.append(index)
                        continue
                    chosen[index] = got
                    if on_pick:
                        on_pick(index, got)
        if not failed:
            before = list(chosen)
            chosen = _place_bass(chosen, groups, seq, verify_live)
            if on_pick:
                for index, opt in enumerate(chosen):
                    if opt is not before[index]:
                        on_pick(index, opt)
            return chosen
        for index in failed:
            dead[index].add(id(chosen[index]["cand"]))
    return [None] * len(groups)


def _credit(title, author, page, license_id) -> str:
    name = LIC.from_id(str(license_id or ""))[1] or license_id
    return f"“{title}” by {author} ({page}), {name}"


def _indexed_bass(maps, pc: int) -> dict | None:
    """Lowest native-pitch note of this pitch class. UIowa piano, then other UIowa, then the rest."""
    pc = int(pc) % 12
    best = None
    for mp in maps or []:
        origin = str(mp.get("origin") or "").lower()
        ident = str(mp.get("id") or "").lower()
        inst = str(mp.get("instrument") or "").lower()
        uiowa = origin == "uiowa" or ident.startswith("uiowa")
        if uiowa and "piano" in inst:
            rank = 0
        elif uiowa:
            rank = 1
        else:
            rank = 2
        for key, sample in (mp.get("notes") or {}).items():
            if not isinstance(sample, dict):
                continue
            try:
                midi = int(sample.get("midi") if sample.get("midi") is not None else key)
            except (TypeError, ValueError):
                continue
            if midi % 12 != pc:
                continue
            if sample.get("kind") not in ("freesound", "uiowa_mis", "commons", "supplier"):
                continue
            if not all(str(sample.get(field) or "").strip() for field in ("title", "author", "license", "page")):
                continue
            expect = sample.get("expect_midi")
            if expect is not None and int(expect) != midi:
                continue
            rank_key = (rank, midi)
            if best is None or rank_key < best[0]:
                best = (rank_key, sample)
    return dict(best[1]) if best else None


def _bass_source(sample: dict) -> dict:
    """The note as recorded. No cents, so nothing downstream pitch-shifts it."""
    keep = {}
    for field in ("kind", "url", "id", "audio_url", "page", "title", "author", "license", "expect_midi", "onset_s", "length_s"):
        if sample.get(field) not in (None, ""):
            keep[field] = sample[field]
    keep["pitched"] = True
    return keep


def _pack(comp, seq, slots, section_spec, chosen, seed, slice_note, prefer, maps=None) -> dict:
    sources = {}
    clips = []
    events = []
    picks = []
    chords = []
    section_of: list[str] = []
    for idx, (count, _seconds) in enumerate(section_spec):
        label = "A" if idx == 0 else ("B" if idx == 1 else chr(ord("A") + idx))
        section_of.extend([label] * count)
    anchor = next(opt for opt in chosen if opt)
    families = [take.family_of(opt.get("instrument") or "") for opt in chosen if opt]
    family = max(set(families), key=families.count) if families else ""
    anchor = next((opt for opt in chosen if opt and take.family_of(opt.get("instrument") or "") == family), anchor)
    t_end = 0.0
    for index, opt in enumerate(chosen):
        sym, _bass = seq[index]
        slot = slots[index]
        start = float(slot["t"])
        dur = max(0.1, float(slot["dur"]))
        t_end = round(start + dur, 3)
        if opt.get("built") == "assembled notes":
            sids, titles, authors, pages, licenses = _add_samples(sources, picks, opt, index)
            built = "assembled notes"
            title = opt.get("title") or f"{opt.get('instrument')} {sym}"
            author = opt.get("author") or ""
            page = opt.get("page") or ""
            license_id = opt.get("license") or (licenses[0] if licenses else "")
            credit = "; ".join(
                _credit(titles[i], authors[i] if i < len(authors) else author, pages[i] if i < len(pages) else page, licenses[i] if i < len(licenses) else license_id)
                for i in range(len(sids))
            )
            voicing = [int(n) for n in (opt.get("voicing") or discover._voicing(sym))]
            if not sids:
                raise ValueError(
                    "No exact chord recording or native-pitch notes for: "
                    + sym
                    + ". A pitched clip in another key was not used."
                )
        else:
            got = opt["accepted"]
            sid = f"c{index}"
            sources[sid] = got["source"]
            sids = [sid]
            if got.get("key"):
                picks.append(str(got["key"]))
            for field in ("url", "audio_url", "id", "manifest"):
                value = (got.get("source") or {}).get(field)
                if value:
                    picks.append(str(value))
            built = "whole-chord file"
            title = got.get("title") or opt.get("title") or sym
            author = got.get("author") or ""
            page = got.get("page") or ""
            license_id = got.get("license") or ""
            credit = _credit(title, author, page, license_id)
            voicing = [int(n) for n in discover._voicing(sym)]
            sample = _indexed_bass(maps, opt["bass_note_pc"]) if opt.get("bass_note_pc") is not None else None
            if sample:
                sid = f"c{index}b"
                sources[sid] = _bass_source(sample)
                sids.append(sid)
                built = "chord and bass note"
                credit = credit + "; " + _credit(
                    sample.get("title") or "",
                    sample.get("author") or "",
                    sample.get("page") or "",
                    sample.get("license") or "",
                )
                if sample.get("id"):
                    picks.append(str(sample["id"]))
                if sample.get("url"):
                    picks.append(str(sample["url"]))
        voicing = [n for n in voicing if 21 <= int(n) <= 108] or [60]
        chords.append({
            "t": round(start, 3),
            "dur": round(dur, 3),
            "symbol": sym,
            "voicing": voicing,
            "section": section_of[index] if index < len(section_of) else "A",
        })
        clips.append({
            "id": f"c{index}",
            "chord": index,
            "symbol": sym,
            "sids": sids,
            "title": title,
            "author": author,
            "supplier": progress.origin_label(opt.get("origin")),
            "license": str(license_id),
            "credit": credit,
            "page": page,
            "built": built,
            "instrument": opt.get("instrument") or "",
            "family": take.family_of(opt.get("instrument") or ""),
            "t": round(start, 3),
            "dur": round(dur, 3),
        })
        events.append({
            "t": round(start, 3),
            "source": sids[0],
            "dur_s": round(dur, 3),
            "fade_in_s": FADE_S,
            "fade_out_s": FADE_S,
            "gain": 1.0,
            "role": "raw chord",
            "chord_index": index,
        })
    length = max(5.0, round(t_end, 3))
    notes = [
        "Raw assembly. One clip per chord, in order. No reverb, no room tone, no mastering, no time-stretch, no pitch-shift.",
    ]
    if slice_note:
        notes.append(slice_note)
    if any(opt.get("bass_note_pc") is not None for opt in chosen if opt):
        notes.append(
            "A written bass with no matching voicing is the chord plus one indexed bass note, "
            "sounded together, or the chord alone when that note is not indexed."
        )
    if prefer:
        notes.append("The words asked for " + ", ".join(prefer) + ".")
    title = comp.get("title") or "Untitled"
    plan = {
        "nc_plan_version": "0.1",
        "assembly": "raw",
        "id": "raw",
        "title": title,
        "composition": {
            "title": title,
            "composer": comp.get("composer") or "",
            "words_verbatim": {
                "composition": comp.get("composition") or "",
                "performance": comp.get("performance") or "",
            },
        },
        "interpreter": {"kind": "discover", "note": "raw assembly"},
        "seed": int(seed),
        "length_s": length,
        "tail_s": 0,
        "chords": chords,
        "sources": sources,
        "banks": {},
        "layers": [{
            "id": "chords",
            "type": "clips",
            "role": "one recording per chord, in order",
            "events": events,
        }],
        "master": {"buses": {"dry": {}}},
        "clips": clips,
        "ensemble": {
            "family": family,
            "instrument": anchor.get("instrument") or "",
            "origin": anchor.get("origin") or "",
            "built": anchor.get("built") or "",
        },
        "notes": notes,
        "gather_picks": list(dict.fromkeys(picks)),
        "from_gather": True,
    }
    sections = []
    cursor = 0.0
    for idx, (count, seconds) in enumerate(section_spec):
        label = "A" if idx == 0 else ("B" if idx == 1 else chr(ord("A") + idx))
        sections.append({"id": label, "start_s": round(cursor, 3), "end_s": round(cursor + float(seconds), 3)})
        cursor += float(seconds)
    if sections:
        plan["sections"] = sections
    return plan


def _add_samples(sources, picks, opt, index):
    sids = []
    titles = []
    authors = []
    pages = []
    licenses = []
    for nth, sample in enumerate(opt.get("samples") or []):
        kind = sample.get("kind")
        if kind not in ("freesound", "uiowa_mis", "commons", "supplier"):
            continue
        sid = f"c{index}n{nth}"
        keep = {}
        for field in ("kind", "url", "id", "audio_url", "page", "title", "author", "license", "pitched", "expect_midi", "segment", "onset_s", "length_s", "credit_checked", "manifest"):
            if sample.get(field) not in (None, ""):
                keep[field] = sample[field]
        if sample.get("cents") is not None:
            keep["cents"] = sample["cents"]
            keep["pitch_checked"] = True
        if not keep.get("title") or not keep.get("author") or not keep.get("license") or not keep.get("page"):
            continue
        sources[sid] = keep
        sids.append(sid)
        titles.append(str(keep.get("title")))
        authors.append(str(keep.get("author")))
        pages.append(str(keep.get("page")))
        licenses.append(str(keep.get("license")))
        if sample.get("id"):
            picks.append(str(sample["id"]))
        if sample.get("url"):
            picks.append(str(sample["url"]))
    return sids, titles, authors, pages, licenses


def _slice(y, src: dict):
    seg = src.get("segment") or {}
    if seg.get("t0") is None or seg.get("t1") is None:
        return y
    a = max(0, int(float(seg["t0"]) * SR))
    b = max(a, int(float(seg["t1"]) * SR))
    return y[a:b]


def _stack(parts: list, slot_n: int):
    acc = np.zeros((slot_n, 2))
    audible = 0
    for part in parts:
        y = part
        if y.ndim == 1:
            y = np.column_stack([y, y])
        n = min(len(y), slot_n)
        if n <= 0:
            continue
        acc[:n] += y[:n]
        audible = max(audible, n)
    if audible <= 0:
        return 1, acc
    return audible, acc


def _match_level(y: np.ndarray, audible: int):
    """Bring this clip to one loudness. A short or silent clip keeps a plain RMS match."""
    heard = y[: max(1, audible)]
    if not np.any(np.isfinite(heard)) or float(np.max(np.abs(heard))) < 1e-8:
        return y, 0.0
    gain_db = 0.0
    matched = False
    try:
        if audible >= int(0.4 * SR):
            import pyloudnorm as pyln
            meter = pyln.Meter(SR)
            loud = float(meter.integrated_loudness(np.nan_to_num(heard)))
            if np.isfinite(loud) and loud > -70:
                gain_db = CLIP_LUFS - loud
                matched = True
    except Exception:
        matched = False
    if not matched:
        rms = float(np.sqrt(np.mean(np.square(heard))))
        if rms > 1e-8:
            target = 10 ** (CLIP_LUFS / 20.0)
            gain_db = 20.0 * np.log10(target / rms)
    gain = 10 ** (gain_db / 20.0)
    out = np.array(y, copy=True)
    out[:audible] *= gain
    return out, round(float(gain_db), 2)


def _fade_audible(y: np.ndarray, audible: int, fade_n: int):
    z = np.array(y, copy=True)
    a = min(fade_n, audible, len(z))
    b = min(fade_n, audible, len(z))
    if a:
        z[:a] *= (np.sin(np.linspace(0, np.pi / 2, a)) ** 2)[:, None]
    if b and audible >= b:
        z[audible - b:audible] *= (np.cos(np.linspace(0, np.pi / 2, b)) ** 2)[:, None]
    return z


def _peak_guard(y: np.ndarray, ceiling: float):
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if not np.isfinite(peak) or peak <= ceiling or peak <= 0:
        return y, peak
    return y * (ceiling / peak), peak
