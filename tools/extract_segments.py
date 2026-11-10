"""Cut annotated segments from local copies of the material, using exported CSVs.

Use this for large batches (the admin page is limited to 2000 segments per zip)
or when the material lives on another computer.

Examples
--------
One clip per annotator segment (with all its ratings):
    python tools/extract_segments.py --csv annotations_long.csv --media-dir ./videos --out ./clips

One clip per peak segment, with the ratings that overlap it:
    python tools/extract_segments.py --csv peak_segments.csv \
        --annotations annotations_long.csv --media-dir ./videos --out ./peak_clips

Text items: pass the folder with the .txt files as --media-dir (or --text-csv for a
CSV with item_id,text columns).

Files are matched to items by file name without extension (= item_id).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from personality_annotator.config import EXTENSIONS  # noqa: E402
from personality_annotator.extract import _fmt, clip_extension, cut  # noqa: E402
from personality_annotator.formats import extract_text  # noqa: E402
from personality_annotator.sources import normalize_text, safe_id  # noqa: E402


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def index_media(folder: Path) -> dict[str, Path]:
    exts = set().union(*EXTENSIONS.values())
    found: dict[str, Path] = {}
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            found.setdefault(safe_id(p.stem), p)
            rel = p.relative_to(folder).with_suffix("")
            found.setdefault(safe_id("__".join(rel.parts)), p)
    return found


def fnum(x):
    return None if x in (None, "") else float(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="annotations_long.csv or peak_segments.csv")
    ap.add_argument("--annotations", help="annotations_long.csv (adds ratings to peak clips)")
    ap.add_argument("--media-dir", help="folder with the video/audio/text files")
    ap.add_argument("--text-csv", help="CSV with item_id,text columns (text items)")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--variable", help="only this variable")
    ap.add_argument("--min-raters", type=int, default=1, help="peaks: minimum max_n_annotators")
    args = ap.parse_args(argv)

    rows = read_csv(Path(args.csv))
    if not rows:
        sys.exit("The CSV is empty.")
    cols = rows[0].keys()
    is_peaks = "max_n_annotators" in cols
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    media = index_media(Path(args.media_dir)) if args.media_dir else {}
    texts: dict[str, str] = {}
    if args.text_csv:
        for r in read_csv(Path(args.text_csv)):
            texts[safe_id(r["item_id"])] = normalize_text(r["text"])

    def text_of(item_id):
        if item_id in texts:
            return texts[item_id]
        p = media.get(item_id)
        if p and p.suffix.lower() in EXTENSIONS["text"]:
            return normalize_text(extract_text(p.read_bytes(), p.name))
        return None

    # Build the list of segments to cut.
    segments = []
    if is_peaks:
        ann = read_csv(Path(args.annotations)) if args.annotations else []
        for r in rows:
            text_mode = r.get("start_char") not in (None, "")
            sk, ek = ("char_start", "char_end") if text_mode else ("start_s", "end_s")
            if args.variable and r["variable"] != args.variable:
                continue
            if int(float(r["max_n_annotators"])) < args.min_raters:
                continue
            s = float(r["start_char" if text_mode else "start_s"])
            e = float(r["end_char" if text_mode else "end_s"])
            rs = [a for a in ann if a["item_id"] == r["item_id"] and a["variable"] == r["variable"]
                  and a[sk] not in ("", None) and float(a[sk]) < e
                  and max(float(a[ek]), float(a[sk]) + 1e-6) > s]
            segments.append({"item_id": r["item_id"], "start": s, "end": e, "label": r["variable"],
                             "text_mode": text_mode, "kind": "peak", "peak": r, "ratings": rs})
    else:
        groups = defaultdict(list)
        for r in rows:
            if args.variable and r["variable"] != args.variable:
                continue
            groups[r["segment_id"]].append(r)
        for sid, rs in groups.items():
            r0 = rs[0]
            text_mode = r0.get("char_start") not in ("", None)
            s = fnum(r0["char_start"] if text_mode else r0["start_s"])
            e = fnum(r0["char_end"] if text_mode else r0["end_s"])
            segments.append({"item_id": r0["item_id"], "start": s, "end": e,
                             "label": f"{r0['annotator_code']}__seg{sid[:8]}", "text_mode": text_mode,
                             "kind": "annotator_segment", "ratings": rs})

    index, problems = [], []
    for n, seg in enumerate(segments, 1):
        item = seg["item_id"]
        try:
            if seg["text_mode"]:
                text = text_of(item)
                if text is None:
                    raise FileNotFoundError(f"no text found for item {item}")
                stem = f"{item}__{seg['label']}__chars{int(seg['start'])}-{int(seg['end'])}"
                clip = out / f"{stem}.txt"
                clip.write_text(text[int(seg["start"]):int(seg["end"])], encoding="utf-8")
            else:
                src = media.get(item)
                if src is None:
                    raise FileNotFoundError(f"no media file found for item {item}")
                modality = "video" if src.suffix.lower() in EXTENSIONS["video"] else "audio"
                ext = clip_extension(src.name, modality)
                stem = f"{item}__{seg['label']}__{_fmt(seg['start'])}-{_fmt(seg['end'])}"
                clip = out / f"{stem}{ext}"
                cut(src, seg["start"], seg["end"], clip, modality)
            side = {k: v for k, v in seg.items() if k != "text_mode"}
            side["clip_file"] = clip.name
            (out / f"{stem}.json").write_text(json.dumps(side, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
            index.append({"clip_file": clip.name, "item_id": item, "label": seg["label"],
                          "start": seg["start"], "end": seg["end"],
                          "n_ratings": len(seg["ratings"]),
                          "annotators": ";".join(sorted({r.get("annotator_code", "") for r in seg["ratings"]}))})
            print(f"[{n}/{len(segments)}] {clip.name}")
        except Exception as exc:
            problems.append(f"{item} {seg['start']}-{seg['end']}: {exc}")
            print(f"[{n}/{len(segments)}] SKIPPED {item}: {exc}")

    with open(out / "index.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["clip_file", "item_id", "label", "start", "end",
                                           "n_ratings", "annotators"])
        w.writeheader()
        w.writerows(index)
    print(f"\nDone: {len(index)} segment(s) written to {out}" +
          (f", {len(problems)} skipped." if problems else "."))


if __name__ == "__main__":
    main()
