"""Coverage ("how many annotators marked this moment / word") and peak segments.

Works on plain lists of rating dicts so it can be used both inside the app
and from scripts on exported CSV files. Each rating dict needs:
  annotator, item_id, variable, score,
  start_s/end_s (video/audio) or char_start/char_end (text).

Counting rule: per bin and variable, each annotator counts at most once.
If one annotator has several overlapping ratings for the same variable
covering a bin, their scores are averaged into one score for that bin.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any, Iterable

WORD_RE = re.compile(r"\S+")


def tokenize(text: str) -> list[tuple[int, int, str]]:
    """Word tokens as (char_start, char_end, token). Must match the frontend."""
    return [(m.start(), m.end(), m.group()) for m in WORD_RE.finditer(text)]


def snap_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end) to whole-word boundaries."""
    toks = [t for t in tokenize(text) if t[0] < max(end, start + 1) and t[1] > start]
    if not toks:
        return start, end
    return toks[0][0], toks[-1][1]


def _stats(scores_by_annotator: dict[str, list[float]], midpoint: float) -> dict[str, Any]:
    per_ann = {a: sum(s) / len(s) for a, s in scores_by_annotator.items()}
    vals = list(per_ann.values())
    return {
        "n_annotators": len(vals),
        "mean_score": round(sum(vals) / len(vals), 4) if vals else None,
        "sd_score": round(statistics.stdev(vals), 4) if len(vals) > 1 else None,
        "n_low": sum(v < midpoint for v in vals),
        "n_mid": sum(v == midpoint for v in vals),
        "n_high": sum(v > midpoint for v in vals),
        "annotators": ";".join(sorted(per_ann)),
    }


def time_coverage(
    ratings: Iterable[dict], duration_s: float | None, bin_s: float, midpoint: float,
    variables: list[str] | None = None, include_empty: bool = False,
) -> list[dict[str, Any]]:
    ratings = [r for r in ratings if r.get("start_s") is not None]
    if not ratings and not include_empty:
        return []
    end_max = max([float(r["end_s"]) for r in ratings] + [float(duration_s or 0)])
    n_bins = max(1, math.ceil(end_max / bin_s - 1e-9))
    variables = variables or sorted({r["variable"] for r in ratings})
    cells: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in ratings:
        s, e = float(r["start_s"]), float(r["end_s"])
        if e <= s:  # a point marks the bin it falls in
            e = s + 1e-6
        first = int(s // bin_s)
        last = min(n_bins - 1, math.ceil(e / bin_s) - 1)
        for b in range(first, last + 1):
            cells[(r["variable"], b)][r["annotator"]].append(float(r["score"]))
    rows = []
    for var in variables:
        for b in range(n_bins):
            cell = cells.get((var, b))
            if not cell and not include_empty:
                continue
            row = {"variable": var, "bin_index": b,
                   "bin_start_s": round(b * bin_s, 4),
                   "bin_end_s": round(min((b + 1) * bin_s, end_max), 4)}
            row.update(_stats(cell or {}, midpoint))
            rows.append(row)
    return rows


def text_coverage(
    ratings: Iterable[dict], text: str, midpoint: float,
    variables: list[str] | None = None, include_empty: bool = False,
) -> list[dict[str, Any]]:
    ratings = [r for r in ratings if r.get("char_start") is not None]
    tokens = tokenize(text)
    variables = variables or sorted({r["variable"] for r in ratings})
    cells: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in ratings:
        s, e = int(r["char_start"]), int(r["char_end"])
        for i, (ts, te, _) in enumerate(tokens):
            if ts < e and te > s:
                cells[(r["variable"], i)][r["annotator"]].append(float(r["score"]))
    rows = []
    for var in variables:
        for i, (ts, te, tok) in enumerate(tokens):
            cell = cells.get((var, i))
            if not cell and not include_empty:
                continue
            row = {"variable": var, "token_index": i, "token": tok,
                   "char_start": ts, "char_end": te}
            row.update(_stats(cell or {}, midpoint))
            rows.append(row)
    return rows


def peaks(
    coverage_rows: list[dict], ratings: list[dict], threshold: int, modality: str,
    text: str | None = None,
) -> list[dict[str, Any]]:
    """Contiguous runs of bins/tokens with n_annotators >= threshold, per variable."""
    idx_key = "token_index" if modality == "text" else "bin_index"
    by_var: dict[str, list[dict]] = defaultdict(list)
    for row in coverage_rows:
        if row["n_annotators"] >= threshold:
            by_var[row["variable"]].append(row)
    out = []
    for var, rows in by_var.items():
        rows.sort(key=lambda r: r[idx_key])
        run: list[dict] = []
        for row in rows + [None]:
            if row is not None and (not run or row[idx_key] == run[-1][idx_key] + 1):
                run.append(row)
                continue
            if run:
                out.append(_summarise_run(var, run, ratings, modality, text))
            run = [row] if row is not None else []
    out.sort(key=lambda p: (p["variable"], p["start"]))
    return out


def _summarise_run(var, run, ratings, modality, text):
    if modality == "text":
        start, end = run[0]["char_start"], run[-1]["char_end"]
        overlap = [r for r in ratings if r["variable"] == var and r.get("char_start") is not None
                   and int(r["char_start"]) < end and int(r["char_end"]) > start]
    else:
        start, end = run[0]["bin_start_s"], run[-1]["bin_end_s"]
        overlap = [r for r in ratings if r["variable"] == var and r.get("start_s") is not None
                   and float(r["start_s"]) < end and max(float(r["end_s"]), float(r["start_s"]) + 1e-6) > start]
    if modality != "text" and overlap:
        # tighten bin-rounded edges to where the annotations actually start/end
        start = round(max(start, min(float(r["start_s"]) for r in overlap)), 3)
        end = round(min(end, max(float(r["end_s"]) for r in overlap)), 3)
    scores = [float(r["score"]) for r in overlap]
    peak = {
        "variable": var,
        "start": start,
        "end": end,
        "max_n_annotators": max(r["n_annotators"] for r in run),
        "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
        "n_ratings": len(overlap),
        "annotators": ";".join(sorted({r["annotator"] for r in overlap})),
    }
    if modality == "text" and text is not None:
        peak["span_text"] = text[start:end]
    return peak
