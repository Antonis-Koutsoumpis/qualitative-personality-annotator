"""Which file formats are supported, how each is shown to annotators, and the
conversions needed for formats browsers cannot play or display directly.

- "native" media play directly in the browser.
- "convert" media are converted once with ffmpeg (video -> MP4/H.264,
  audio -> M4A/AAC) and the converted copy is cached in data_dir/converted.
- text documents are read as plain text when the items are scanned; the
  extracted text is stored, and character positions refer to it.
"""
from __future__ import annotations

import io
import re
import subprocess
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

VIDEO_NATIVE = {".mp4", ".m4v", ".webm", ".ogv"}
VIDEO_CONVERT = {".avi", ".mov", ".mkv", ".wmv", ".mpg", ".mpeg", ".flv", ".3gp", ".mts",
                 ".m2ts", ".ts", ".vob"}
AUDIO_NATIVE = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac"}
AUDIO_CONVERT = {".wma", ".aif", ".aiff", ".amr", ".ac3", ".mka"}
TEXT_PLAIN = {".txt", ".md"}
TEXT_EXTRACT = {".docx", ".pdf", ".odt", ".rtf", ".html", ".htm"}

BY_MODALITY = {
    "video": VIDEO_NATIVE | VIDEO_CONVERT,
    "audio": AUDIO_NATIVE | AUDIO_CONVERT,
    "text": TEXT_PLAIN | TEXT_EXTRACT,
}
ALL_EXTENSIONS = set().union(*BY_MODALITY.values())


def modality_of(name: str) -> str | None:
    ext = Path(name.split("?")[0]).suffix.lower()
    for mod, exts in BY_MODALITY.items():
        if ext in exts:
            return mod
    return None


def needs_conversion(name: str) -> bool:
    ext = Path(name.split("?")[0]).suffix.lower()
    return ext in VIDEO_CONVERT or ext in AUDIO_CONVERT


# ------------------------------------------------------------------ text

def _docx_text(data: bytes) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{ns}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{ns}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{ns}tab":
                parts.append("\t")
            elif el.tag in (f"{ns}br", f"{ns}cr"):
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras)


def _odt_text(data: bytes) -> str:
    text_ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("content.xml"))

    def walk(el) -> str:
        out = [el.text or ""]
        for ch in el:
            if ch.tag == f"{text_ns}s":
                out.append(" " * int(ch.get(f"{text_ns}c", "1")))
            elif ch.tag == f"{text_ns}tab":
                out.append("\t")
            elif ch.tag == f"{text_ns}line-break":
                out.append("\n")
            else:
                out.append(walk(ch))
            out.append(ch.tail or "")
        return "".join(out)

    return "\n".join(walk(p) for p in root.iter() if p.tag in (f"{text_ns}p", f"{text_ns}h"))


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Reading PDF files needs the 'pypdf' package "
                           "(pip install -r requirements.txt).") from exc
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        raise ValueError("no text found in the PDF (is it a scanned image?)")
    return text


def _rtf_text(data: bytes) -> str:
    """Basic RTF to text: good enough for transcripts, not for complex layouts."""
    s = data.decode("latin-1")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    s = re.sub(r"\{\\\*[^{}]*\}", "", s)
    # drop header groups (fonts, colours, styles, document info), allowing one nesting level
    s = re.sub(r"\{\\(fonttbl|colortbl|stylesheet|info|pict|header|footer)"
               r"(?:[^{}]|\{[^{}]*\})*\}", "", s)
    s = re.sub(r"\\(par|line)\b ?", "\n", s)
    s = re.sub(r"\\tab\b ?", "\t", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    s = s.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    s = re.sub(r"[{}]", "", s)
    return s.strip()


def _html_text(data: bytes) -> str:
    from html import unescape
    s = data.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_text(data: bytes, name: str, encoding: str = "utf-8") -> str:
    ext = Path(name.split("?")[0]).suffix.lower()
    if ext in TEXT_PLAIN:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            return data.decode("cp1252", "replace")
    if ext == ".docx":
        return _docx_text(data)
    if ext == ".odt":
        return _odt_text(data)
    if ext == ".pdf":
        return _pdf_text(data)
    if ext == ".rtf":
        return _rtf_text(data)
    if ext in (".html", ".htm"):
        return _html_text(data)
    raise ValueError(f"Unsupported text format: {ext}")


# ------------------------------------------------------------------ media conversion

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def converted_path(item_id: str, modality: str, out_dir: Path) -> Path:
    return out_dir / f"{item_id}{'.mp4' if modality == 'video' else '.m4a'}"


def convert_for_browser(src: Path, item_id: str, modality: str, out_dir: Path) -> Path:
    """Convert once to a browser-playable file; later calls return the cached copy."""
    from .extract import ffmpeg_exe

    target = converted_path(item_id, modality, out_dir)
    with _locks_guard:
        lock = _locks.setdefault(item_id, threading.Lock())
    with lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.stem + ".part" + target.suffix)
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
        if modality == "video":
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart"]
        else:
            cmd += ["-vn", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
        cmd.append(str(tmp))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Could not convert {src.name}: {res.stderr.strip()[:400]}")
        tmp.replace(target)
        return target


def probe_duration(path: Path) -> float | None:
    """Duration in seconds, read from ffmpeg's file information (no ffprobe needed)."""
    from .extract import ffmpeg_exe
    try:
        res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)
