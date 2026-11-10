"""Cutting annotated segments out of the material (ffmpeg for media, slicing for text)
and bundling them with sidecar files describing their annotations."""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .sources import HTTP_TIMEOUT


class ExtractionError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # bundled ffmpeg binary, used by the packaged app
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise ExtractionError(
            "ffmpeg not found. Install ffmpeg or `pip install imageio-ffmpeg`.") from exc


def media_path(item: dict, cache_dir: Path) -> Path:
    """A local file for this item, downloading remote files into the cache once."""
    if item["source_type"] == "local":
        p = Path(item["location"])
        if not p.exists():
            raise ExtractionError(f"File not found: {p}")
        return p
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(item.get("filename") or "").suffix or ".bin"
    target = cache_dir / f"{item['item_id']}{suffix}"
    if target.exists() and target.stat().st_size > 0:
        return target
    part = target.with_suffix(target.suffix + ".part")
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        with client.stream("GET", item["location"]) as r:
            r.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
    part.replace(target)
    return target


def cut(src: Path, start: float, end: float, out: Path, modality: str) -> None:
    dur = max(0.05, float(end) - float(start))
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{float(start):.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
    if modality == "video":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    elif out.suffix.lower() == ".m4a":
        cmd += ["-vn", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-vn"]
    cmd.append(str(out))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        raise ExtractionError(f"ffmpeg failed for {src.name}: {res.stderr.strip()[:500]}")


def clip_extension(filename: str, modality: str) -> str:
    """Video clips are MP4; audio keeps its format if browsers play it, else M4A."""
    if modality == "video":
        return ".mp4"
    from .formats import AUDIO_NATIVE
    ext = Path(filename.split("?")[0]).suffix.lower()
    return ext if ext in AUDIO_NATIVE else ".m4a"


def _fmt(t: float) -> str:
    return f"{float(t):08.2f}".replace(".", "s")


def build_zip(segments: list[dict[str, Any]], items: dict[str, dict],
              cache_dir: Path, work_dir: Path, out_zip: Path) -> Path:
    """segments: dicts with item_id, start, end, label, ratings (list of dicts).

    For text, start/end are character offsets; otherwise seconds.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    index_rows = []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, seg in enumerate(segments, 1):
            item = items[seg["item_id"]]
            modality = item["modality"]
            label = seg.get("label") or "segment"
            if modality == "text":
                stem = f"{seg['item_id']}__{label}__chars{int(seg['start'])}-{int(seg['end'])}"
                text = (item.get("text") or "")[int(seg["start"]):int(seg["end"])]
                zf.writestr(f"{stem}.txt", text)
                clip_name = f"{stem}.txt"
            else:
                ext = clip_extension(item.get("filename") or "", modality)
                stem = f"{seg['item_id']}__{label}__{_fmt(seg['start'])}-{_fmt(seg['end'])}"
                clip = work_dir / f"{n:05d}{ext}"
                cut(media_path(item, cache_dir), seg["start"], seg["end"], clip, modality)
                zf.write(clip, f"{stem}{ext}")
                clip.unlink(missing_ok=True)
                clip_name = f"{stem}{ext}"
            sidecar = {k: v for k, v in seg.items() if k != "ratings"}
            sidecar["clip_file"] = clip_name
            sidecar["ratings"] = seg.get("ratings", [])
            zf.writestr(f"{stem}.json", json.dumps(sidecar, ensure_ascii=False, indent=2))
            index_rows.append({
                "clip_file": clip_name, "item_id": seg["item_id"], "label": label,
                "start": seg["start"], "end": seg["end"],
                "variables": ";".join(sorted({r["variable"] for r in seg.get("ratings", [])})),
                "n_ratings": len(seg.get("ratings", [])),
                "annotators": ";".join(sorted({r["annotator_code"] for r in seg.get("ratings", [])})),
            })
            # one CSV per clip with its ratings (long format)
            buf = io.StringIO()
            cols = ["annotator_code", "variable", "score", "explanation", "notes",
                    "segment_start", "segment_end", "segment_id", "rating_id"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in seg.get("ratings", []):
                w.writerow(r)
            zf.writestr(f"{stem}.csv", buf.getvalue())
        buf = io.StringIO()
        cols = ["clip_file", "item_id", "label", "start", "end", "variables", "n_ratings",
                "annotators"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(index_rows)
        zf.writestr("index.csv", buf.getvalue())
    return out_zip
