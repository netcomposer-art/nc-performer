"""Keep a plan inside the limits the hosted validator enforces.

The planner calls this before it returns, and the runner calls it again
before check_plan. A plan that would have been refused (too many layers,
sources, chords, seconds, or bytes) is merged or trimmed, and the reason
is appended to plan["notes"] so the record says what was left out.

Packing is idempotent: a second call on an already-fitted plan changes
nothing and adds no note.
"""
from __future__ import annotations

import copy
import json
from collections import Counter


def fit_plan(
    plan: dict,
    *,
    max_layers: int = 16,
    max_sources: int = 24,
    max_chords: int = 40,
    max_length_s: float = 180,
    max_plan_bytes: int = 64 * 1024,
) -> list[str]:
    """Mutate ``plan`` until it satisfies the caps. Return the notes added."""
    notes: list[str] = []
    if not isinstance(plan, dict):
        return notes
    plan.setdefault("layers", [])
    plan.setdefault("sources", {})
    plan.setdefault("banks", {})
    plan.setdefault("chords", [])

    before = len(plan["layers"])
    merged = _merge_chord_notes(plan)
    if merged > 1 and before > max_layers:
        notes.append(
            f"{merged} chord-note layers were packed into one layer so the Performance "
            f"stays within {max_layers} layers. Each chord still uses its own samples."
        )

    dropped: list[str] = []
    merged_strikes = False
    merged_texture = False
    while len(plan["layers"]) > max_layers:
        if _merge_clips(plan, min_rank=3):
            merged_strikes = True
            continue
        if _merge_clips(plan, min_rank=0, max_rank=0):
            merged_texture = True
            continue
        label = _pop_least_important(plan)
        if label is None:
            break
        dropped.append(label)
    if merged_strikes:
        notes.append(
            f"Strike clips were combined into one layer so the Performance stays within {max_layers} layers."
        )
    if merged_texture:
        notes.append(
            f"Texture clips were combined into one layer so the Performance stays within {max_layers} layers."
        )
    if dropped:
        shown = ", ".join(dropped[:8])
        extra = f" and {len(dropped) - 8} more" if len(dropped) > 8 else ""
        notes.append(
            f"Left out {shown}{extra} so the Performance stays within {max_layers} layers."
        )

    chords = plan.get("chords") or []
    if len(chords) > max_chords:
        plan["chords"] = chords[:max_chords]
        _clip_to_chords(plan)
        notes.append(
            f"The piece had {len(chords)} chords; it was shortened to {max_chords} "
            "so the Performance stays within the chord limit."
        )

    _clamp_length(plan, max_length_s, notes)
    _prune(plan)
    _fit_sources(plan, max_sources, notes)
    _fit_bytes(plan, max_plan_bytes, notes)

    if notes:
        existing = plan.setdefault("notes", [])
        for note in notes:
            if note not in existing:
                existing.append(note)
        text = " ".join(notes)
        prior = plan.get("trimmed")
        if not prior:
            plan["trimmed"] = text
        elif text not in str(prior):
            plan["trimmed"] = str(prior).rstrip() + " " + text
    return notes


def _rank(ly: dict) -> int:
    """Higher stays. Texture goes first; the chord strikes stay."""
    typ = ly.get("type") or ""
    role = (ly.get("role") or "").lower()
    ident = (ly.get("id") or "").lower()
    bus = ly.get("bus") or ""
    if ident == "bed" or "texture" in role or (typ == "clips" and bus == "pad"):
        return 0
    if typ in ("chord_pad", "chord_under", "drone"):
        return 1
    if typ == "notes":
        return 2
    if typ == "clips":
        return 3
    if typ == "chord_notes":
        return 4
    return 2


def _merge_bank(plan: dict, dest: str, src: str) -> None:
    if not dest or not src or dest == src:
        return
    banks = plan.setdefault("banks", {})
    if src not in banks:
        return
    target = banks.setdefault(dest, {"notes": {}})
    notes = target.setdefault("notes", {})
    for midi, sids in (banks[src].get("notes") or {}).items():
        bucket = notes.setdefault(str(midi), [])
        for sid in sids:
            if sid not in bucket:
                bucket.append(sid)


def _merge_chord_notes(plan: dict) -> int:
    """One chord_notes layer. Several strikes share it; each keeps its bank.

    Layers that strike the same chord (one layer per note, or per source)
    are folded into that one strike and their samples share a bank.
    """
    layers = plan.get("layers") or []
    chosen = [ly for ly in layers if ly.get("type") == "chord_notes"]
    if not chosen:
        return 0
    keep = chosen[0]
    grouped: list[dict] = []
    by_chord: dict = {}
    for ly in chosen:
        layer_bank = ly.get("bank")
        for pc in ly.get("per_chord") or []:
            row = dict(pc)
            bank = row.get("bank") or layer_bank
            if bank:
                row["bank"] = bank
            key = row.get("chord")
            prev = by_chord.get(key)
            if prev is None:
                by_chord[key] = row
                grouped.append(row)
                continue
            dest = prev.get("bank")
            src = row.get("bank")
            if dest and src and dest != src:
                _merge_bank(plan, dest, src)
            elif src and not dest:
                prev["bank"] = src
            strikes = list(prev.get("strikes_at") or [0.0])
            for t in row.get("strikes_at") or []:
                if t not in strikes:
                    strikes.append(t)
            prev["strikes_at"] = strikes
    keep["per_chord"] = grouped
    if grouped and grouped[0].get("bank"):
        keep["bank"] = grouped[0]["bank"]
    if len(chosen) > 1:
        drop = {id(ly) for ly in chosen[1:]}
        plan["layers"] = [ly for ly in layers if id(ly) not in drop]
    return len(chosen)


def _merge_clips(plan: dict, *, min_rank: int, max_rank: int | None = None) -> bool:
    ceiling = 99 if max_rank is None else max_rank
    chosen = [
        ly for ly in plan.get("layers") or []
        if ly.get("type") == "clips" and min_rank <= _rank(ly) <= ceiling
    ]
    if len(chosen) < 2:
        return False
    keep = chosen[0]
    events = list(keep.get("events") or [])
    for ly in chosen[1:]:
        events.extend(ly.get("events") or [])
    keep["events"] = events
    drop = {id(ly) for ly in chosen[1:]}
    plan["layers"] = [ly for ly in plan["layers"] if id(ly) not in drop]
    return True


def _pop_least_important(plan: dict) -> str | None:
    layers = plan.get("layers") or []
    if len(layers) <= 1:
        return None
    n_notes = sum(1 for ly in layers if ly.get("type") == "chord_notes")
    n_strike = sum(1 for ly in layers if ly.get("type") == "clips" and _rank(ly) >= 3)
    order = sorted(range(len(layers)), key=lambda i: (_rank(layers[i]), i))
    for i in order:
        ly = layers[i]
        if ly.get("type") == "chord_notes" and n_notes <= 1:
            continue
        if ly.get("type") == "clips" and _rank(ly) >= 3 and n_strike <= 1 and n_notes == 0:
            continue
        layers.pop(i)
        return str(ly.get("id") or ly.get("type") or "a layer")
    return None


def _clip_to_chords(plan: dict) -> None:
    chords = plan.get("chords") or []
    keep = len(chords)
    if not chords:
        return
    end = float(chords[-1].get("t") or 0) + float(chords[-1].get("dur") or 0)
    plan["length_s"] = round(min(float(plan.get("length_s") or end), end), 3)
    for ly in plan.get("layers") or []:
        if ly.get("per_chord"):
            ly["per_chord"] = [pc for pc in ly["per_chord"] if int(pc.get("chord") or 0) < keep]
        if isinstance(ly.get("notes_by_chord"), dict):
            ly["notes_by_chord"] = {
                k: v for k, v in ly["notes_by_chord"].items() if int(k) < keep
            }
        if isinstance(ly.get("chords"), list):
            ly["chords"] = [i for i in ly["chords"] if int(i) < keep]
        if ly.get("events"):
            ly["events"] = [
                ev for ev in ly["events"]
                if float(ev.get("t") or 0) < float(plan["length_s"]) + 0.05
            ]
        if ly.get("spans"):
            ly["spans"] = [
                sp for sp in ly["spans"]
                if float(sp.get("start_s") or 0) < float(plan["length_s"])
            ]
    for sec in plan.get("sections") or []:
        if "end_s" in sec:
            sec["end_s"] = round(min(float(sec["end_s"]), float(plan["length_s"])), 3)


def _clamp_length(plan: dict, max_length_s: float, notes: list[str]) -> None:
    tail = float(plan.get("tail_s") or 0)
    length = float(plan.get("length_s") or 0)
    if length <= 0 or length + tail <= max_length_s:
        return
    new_len = max(5.0, max_length_s - tail)
    if new_len + tail > max_length_s:
        tail = max(0.0, max_length_s - new_len)
        plan["tail_s"] = round(tail, 3)
    scale = new_len / length
    plan["length_s"] = round(new_len, 3)
    for chord in plan.get("chords") or []:
        chord["t"] = round(float(chord.get("t") or 0) * scale, 3)
        chord["dur"] = round(max(0.1, float(chord.get("dur") or 0) * scale), 3)
    for sec in plan.get("sections") or []:
        if "start_s" in sec:
            sec["start_s"] = round(float(sec["start_s"]) * scale, 3)
        if "end_s" in sec:
            sec["end_s"] = round(min(float(sec["end_s"]) * scale, new_len), 3)
    for ly in plan.get("layers") or []:
        for ev in ly.get("events") or []:
            if "t" in ev:
                ev["t"] = round(float(ev["t"]) * scale, 3)
        for sp in ly.get("spans") or []:
            if "start_s" in sp:
                sp["start_s"] = round(float(sp["start_s"]) * scale, 3)
            if "dur_s" in sp:
                sp["dur_s"] = round(max(0.1, float(sp["dur_s"]) * scale), 3)
    notes.append(
        f"The music was {length:.0f}s plus a {tail:.0f}s tail; it was shortened to "
        f"{plan['length_s']:.0f}s so the Performance stays within {max_length_s:.0f}s."
    )


def _used_banks(plan: dict) -> set[str]:
    used: set[str] = set()
    for ly in plan.get("layers") or []:
        if ly.get("bank"):
            used.add(ly["bank"])
        for bank in ly.get("fallback_banks") or []:
            used.add(bank)
        for pc in ly.get("per_chord") or []:
            if pc.get("bank"):
                used.add(pc["bank"])
        for ev in ly.get("events") or []:
            for timbre in ev.get("timbres") or []:
                if isinstance(timbre, dict) and timbre.get("bank"):
                    used.add(timbre["bank"])
    return used


def _prune(plan: dict) -> None:
    used_b = _used_banks(plan)
    banks = plan.get("banks") or {}
    plan["banks"] = {name: bank for name, bank in banks.items() if name in used_b}
    used_s: set[str] = set()
    for name in used_b:
        for sids in ((plan["banks"].get(name) or {}).get("notes") or {}).values():
            used_s.update(sids)
    for ly in plan.get("layers") or []:
        for ev in ly.get("events") or []:
            if ev.get("source"):
                used_s.add(ev["source"])
    for clip in plan.get("clips") or []:
        if isinstance(clip, dict):
            used_s.update(str(sid) for sid in (clip.get("sids") or []) if sid)
    sources = plan.get("sources") or {}
    plan["sources"] = {sid: src for sid, src in sources.items() if sid in used_s}


def _drop_beds(plan: dict) -> list[str]:
    layers = plan.get("layers") or []
    beds = [ly for ly in layers if _rank(ly) == 0]
    if not beds or len(beds) == len(layers):
        return []
    drop = {id(ly) for ly in beds}
    plan["layers"] = [ly for ly in layers if id(ly) not in drop]
    return [str(ly.get("id") or "texture") for ly in beds]


def _trim_candidates(plan: dict) -> int:
    removed = 0
    for bank in (plan.get("banks") or {}).values():
        notes = bank.get("notes") or {}
        for midi, sids in list(notes.items()):
            if len(sids) > 1:
                removed += len(sids) - 1
                notes[midi] = sids[:1]
    return removed


def _drop_one_strike(plan: dict) -> str | None:
    layers = plan.get("layers") or []
    for ly in reversed(layers):
        if ly.get("type") != "chord_notes":
            continue
        pcs = ly.get("per_chord") or []
        others = any(item is not ly for item in layers)
        if not pcs or (len(pcs) == 1 and not others):
            continue
        gone = pcs.pop()
        if not pcs and others:
            plan["layers"] = [item for item in plan["layers"] if item is not ly]
        idx = gone.get("chord")
        chords = plan.get("chords") or []
        sym = ""
        if isinstance(idx, int) and 0 <= idx < len(chords):
            sym = str(chords[idx].get("symbol") or "")
        label = f"chord {idx}" + (f" ({sym})" if sym else "")
        return label
    for ly in reversed(layers):
        if ly.get("type") != "clips" or _rank(ly) == 0:
            continue
        events = ly.get("events") or []
        others = any(item is not ly for item in layers)
        if len(events) <= 1 and not others:
            continue
        if not events:
            continue
        events.pop()
        if not events and others:
            plan["layers"] = [item for item in plan["layers"] if item is not ly]
        return "a whole-chord strike"
    return None


def _drop_one_tone(plan: dict) -> bool:
    banks = plan.get("banks") or {}
    best = None
    best_n = 0
    for name, bank in banks.items():
        count = sum(len(sids) for sids in (bank.get("notes") or {}).values())
        if count > best_n:
            best_n = count
            best = name
    if not best or best_n < 1:
        return False
    notes = banks[best].setdefault("notes", {})
    if not notes:
        return False
    midi = max(notes, key=lambda m: (len(notes[m]), int(m) if str(m).isdigit() else 0))
    sids = notes.get(midi) or []
    if sids:
        sids.pop()
    if not sids:
        notes.pop(midi, None)
        if str(midi).isdigit():
            midi_i = int(midi)
            for ly in plan.get("layers") or []:
                for pc in ly.get("per_chord") or []:
                    bank = pc.get("bank") or ly.get("bank")
                    if bank != best:
                        continue
                    idx = pc.get("chord")
                    chords = plan.get("chords") or []
                    if not isinstance(idx, int) or not (0 <= idx < len(chords)):
                        continue
                    voicing = list(chords[idx].get("voicing") or [])
                    if midi_i in voicing and len(voicing) > 1:
                        chords[idx]["voicing"] = [n for n in voicing if n != midi_i]
    return True


def _sync_mix(plan: dict) -> None:
    mix = plan.get("source_mix")
    if not isinstance(mix, dict):
        return
    chords = plan.get("chords") or []
    rows = mix.get("per_chord")
    if isinstance(rows, list) and chords and len(rows) == len(chords):
        heard: set[int] = set()
        for ly in plan.get("layers") or []:
            for pc in ly.get("per_chord") or []:
                if isinstance(pc.get("chord"), int):
                    heard.add(pc["chord"])
            if ly.get("type") != "clips" or _rank(ly) == 0:
                continue
            for ev in ly.get("events") or []:
                t = float(ev.get("t") or 0)
                for i, chord in enumerate(chords):
                    start = float(chord.get("t") or 0)
                    dur = float(chord.get("dur") or 0)
                    if start - 0.05 <= t < start + dur + 0.5:
                        heard.add(i)
                        break
        mix["per_chord"] = [row for i, row in enumerate(rows) if i in heard]
        rows = mix["per_chord"]
    if isinstance(rows, list):
        counts = Counter(
            row.get("origin") for row in rows if isinstance(row, dict) and row.get("origin")
        )
        total = sum(counts.values()) or 1
        mix["by_origin"] = dict(counts)
        mix["share"] = {k: round(v / total, 2) for k, v in counts.items()}
        mix["strike_share"] = {k: round(v / total, 2) for k, v in counts.items()}
    if not any(_rank(ly) == 0 for ly in plan.get("layers") or []):
        if mix.get("texture"):
            mix["texture"] = None


def _snapshot(plan: dict) -> dict:
    return {
        "layers": copy.deepcopy(plan.get("layers")),
        "banks": copy.deepcopy(plan.get("banks")),
        "sources": copy.deepcopy(plan.get("sources")),
        "chords": copy.deepcopy(plan.get("chords")),
    }


def _restore(plan: dict, saved: dict) -> None:
    plan["layers"] = saved["layers"]
    plan["banks"] = saved["banks"]
    plan["sources"] = saved["sources"]
    plan["chords"] = saved["chords"]


def _detach_one_source(plan: dict) -> bool:
    """Drop one source from every bank and event. Always shrinks the plan."""
    sources = plan.get("sources") or {}
    if not sources:
        return False
    sid = next(reversed(sources))
    for bank in (plan.get("banks") or {}).values():
        notes = bank.get("notes") or {}
        for midi, sids in list(notes.items()):
            kept = [item for item in sids if item != sid]
            if kept:
                notes[midi] = kept
            else:
                notes.pop(midi, None)
    for ly in plan.get("layers") or []:
        if ly.get("events"):
            ly["events"] = [ev for ev in ly["events"] if ev.get("source") != sid]
    _prune(plan)
    return sid not in (plan.get("sources") or {})


def _fit_sources(plan: dict, max_sources: int, notes: list[str]) -> None:
    if len(plan.get("sources") or {}) <= max_sources:
        return
    changed = False
    beds = _drop_beds(plan)
    _prune(plan)
    if beds:
        changed = True
        notes.append(
            "The texture bed was left out so the Performance stays within "
            f"{max_sources} sources."
        )
    if len(plan.get("sources") or {}) > max_sources and _trim_candidates(plan):
        before = len(plan["sources"])
        _prune(plan)
        if len(plan["sources"]) < before:
            changed = True
            notes.append(
                "Extra note candidates were left out so the Performance stays within "
                f"{max_sources} sources. The first sample for each pitch was kept."
            )
    strike_labels: list[str] = []
    tone_noted = False
    detached = False
    while len(plan.get("sources") or {}) > max_sources:
        before = len(plan["sources"])
        saved = _snapshot(plan)
        label = _drop_one_strike(plan)
        _prune(plan)
        if label and len(plan["sources"]) < before:
            changed = True
            strike_labels.append(label)
            continue
        _restore(plan, saved)
        saved = _snapshot(plan)
        if _drop_one_tone(plan):
            _prune(plan)
            if len(plan["sources"]) < before:
                changed = True
                if not tone_noted:
                    notes.append(
                        "Some chord tones were left out so the Performance stays within "
                        f"{max_sources} sources. Nothing was pitch-shifted to replace them."
                    )
                    tone_noted = True
                continue
        _restore(plan, saved)
        if not _detach_one_source(plan):
            break
        changed = True
        detached = True
    if detached:
        notes.append(
            "Some samples were left out so the Performance stays within "
            f"{max_sources} sources. Nothing was pitch-shifted to replace them."
        )
    if strike_labels:
        shown = ", ".join(strike_labels[:6])
        extra = f" and {len(strike_labels) - 6} more" if len(strike_labels) > 6 else ""
        notes.append(
            f"Left out {shown}{extra} so the Performance stays within {max_sources} sources."
        )
    if changed:
        _sync_mix(plan)


def _nbytes(plan: dict) -> int:
    return len(json.dumps(plan, ensure_ascii=False).encode())


def _fit_bytes(plan: dict, limit: int, notes: list[str]) -> None:
    if _nbytes(plan) <= limit:
        return
    mix = plan.get("source_mix")
    if isinstance(mix, dict):
        for row in mix.get("per_chord") or []:
            if isinstance(row, dict):
                row.pop("tones", None)
    if _nbytes(plan) > limit and isinstance(mix, dict):
        plan["source_mix"] = {
            "trimmed": True,
            "by_origin": mix.get("by_origin"),
            "strike_share": mix.get("strike_share"),
            "per_chord": [
                {
                    k: row.get(k)
                    for k in ("symbol", "origin", "instrument", "built", "license")
                    if isinstance(row, dict) and row.get(k)
                }
                for row in (mix.get("per_chord") or [])
                if isinstance(row, dict)
            ],
        }
    if _nbytes(plan) > limit:
        plan.pop("source_mix", None)
    guard = 0
    while _nbytes(plan) > limit and guard < 80:
        guard += 1
        before = _nbytes(plan)
        if _drop_one_strike(plan) is None and not _drop_one_tone(plan):
            break
        _prune(plan)
        if _nbytes(plan) >= before:
            break
    notes.append("The plan was shortened so it stays within the size limit.")
