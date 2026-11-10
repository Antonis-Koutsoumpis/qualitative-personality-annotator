"""Discovering items from sources and syncing them into the database.

Sources come from config.yaml and from the "Add files" dialog (stored in the
database). Supported source types:
  local     - a folder or a single file on this computer
  zenodo    - a public Zenodo record (link, DOI or numeric id)
  url       - a direct link to one file
  text_csv  - a CSV with one text per row (local path or URL)

Each file's modality (video/audio/text) follows from its extension; see
formats.py. New storage back-ends (e.g. WebDAV for Yoda) can be added by
writing another `list_*` function that returns `ItemSpec`s.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from .config import Config
from .db import Database, new_id, now_iso
from .formats import extract_text, modality_of

ZENODO_API = "https://zenodo.org/api/records/{id}"
ZENODO_FILE_BASE = "https://zenodo.org/records/{id}/files"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


@dataclass
class ItemSpec:
    item_id: str
    modality: str             # video | audio | text
    source_type: str          # local | zenodo | url | text_csv
    location: str             # file path or URL
    filename: str
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    """Normalise so character offsets are stable across platforms."""
    text = text.lstrip("﻿")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_id(s: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", s.strip(), flags=re.UNICODE)
    return s.strip("_") or "item"


def parse_zenodo_record(ref: str | int) -> str:
    """Accepts https://zenodo.org/records/123, .../record/123, 10.5281/zenodo.123, 123."""
    s = str(ref).strip()
    for pat in (r"zenodo\.org/records?/(\d+)", r"zenodo\.(\d+)", r"^(\d+)$"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    raise ValueError(f"Could not find a Zenodo record id in {ref!r}")


def is_zenodo_record_link(url: str) -> bool:
    return bool(re.search(r"zenodo\.org/records?/\d+/?(\?.*)?$", url.strip())
                or re.search(r"(doi\.org/)?10\.5281/zenodo\.\d+$", url.strip()))


def http_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)


def source_label(src: dict) -> str:
    return str(src.get("path") or src.get("record") or src.get("url") or "")


def _allowed(name: str, allowed: tuple[str, ...]) -> str | None:
    mod = modality_of(name)
    return mod if mod in allowed else None


def _text_or_error(data: bytes, name: str, encoding: str, errors: list[str]) -> str | None:
    try:
        return normalize_text(extract_text(data, name, encoding))
    except Exception as exc:
        errors.append(f"{name}: could not read text ({exc})")
        return None


def list_local(source: dict, allowed: tuple[str, ...], errors: list[str]) -> list[ItemSpec]:
    root = Path(source["path"])
    enc = source.get("encoding", "utf-8")
    if root.is_file():
        files, base = [root], root.parent
    elif root.is_dir():
        pattern = source.get("pattern", "*")
        it = root.rglob("*") if source.get("recursive") else root.glob("*")
        files = [p for p in it if p.is_file() and fnmatch.fnmatch(p.name.lower(), pattern.lower())]
        base = root
    else:
        raise FileNotFoundError(f"Folder or file not found: {root}")
    specs = []
    for p in sorted(files):
        if p.name.startswith((".", "~$")):
            continue
        mod = _allowed(p.name, allowed)
        if not mod:
            continue
        rel = p.relative_to(base).with_suffix("")
        spec = ItemSpec(safe_id("__".join(rel.parts)), mod, "local", str(p), p.name)
        if mod == "text":
            spec.text = _text_or_error(p.read_bytes(), p.name, enc, errors)
            if spec.text is None:
                continue
        specs.append(spec)
    return specs


def list_zenodo(source: dict, allowed: tuple[str, ...], client: httpx.Client,
                errors: list[str]) -> list[ItemSpec]:
    rec_id = parse_zenodo_record(source["record"])
    resp = client.get(ZENODO_API.format(id=rec_id))
    resp.raise_for_status()
    files = resp.json().get("files") or []
    if isinstance(files, dict):  # some API versions nest entries
        files = files.get("entries") or []
        if isinstance(files, dict):
            files = list(files.values())
    if not files:  # fall back to the dedicated files endpoint
        r = client.get(ZENODO_API.format(id=rec_id) + "/files")
        if r.status_code == 200:
            files = r.json().get("entries") or []
    if not files:
        raise ValueError(f"Zenodo record {rec_id} has no (public) files.")
    pattern = source.get("pattern", "*")
    base = ZENODO_FILE_BASE.format(id=rec_id)
    specs = []
    for f in files:
        key = f.get("key") or f.get("filename")
        if not key or not fnmatch.fnmatch(key.lower(), pattern.lower()):
            continue
        mod = _allowed(key, allowed)
        if not mod:
            continue
        # The public download link is the most stable form across API versions.
        url = f"{base}/{quote(key)}?download=1"
        spec = ItemSpec(safe_id(Path(key).stem), mod, "zenodo", url, key,
                        metadata={"zenodo_record": rec_id, "size": f.get("size"),
                                  "checksum": f.get("checksum")})
        if mod == "text":
            r = client.get(url)
            r.raise_for_status()
            spec.text = _text_or_error(r.content, key, source.get("encoding", "utf-8"), errors)
            if spec.text is None:
                continue
        specs.append(spec)
    return specs


def list_url(source: dict, allowed: tuple[str, ...], client: httpx.Client,
             errors: list[str]) -> list[ItemSpec]:
    url = source["url"].strip()
    name = unquote(Path(urlparse(url).path).name) or "file"
    if source.get("filename"):
        name = source["filename"]
    mod = modality_of(name)
    if mod is None:
        raise ValueError(f"Can't tell the file type of {url}: the link should end in a file "
                         "name with an extension such as .mp4, .mp3 or .txt.")
    if mod not in allowed:
        raise ValueError(f"{name} is a {mod} file, but this project only uses "
                         f"{' / '.join(allowed)}.")
    spec = ItemSpec(safe_id(Path(name).stem), mod, "url", url, name)
    if mod == "text":
        r = client.get(url)
        r.raise_for_status()
        spec.text = _text_or_error(r.content, name, source.get("encoding", "utf-8"), errors)
        if spec.text is None:
            return []
    return [spec]


def list_text_csv(source: dict, client: httpx.Client) -> list[ItemSpec]:
    id_col = source.get("id_column", "item_id")
    text_col = source.get("text_column", "text")
    if "path" in source:
        raw = Path(source["path"]).read_text(encoding=source.get("encoding", "utf-8"))
        location = source["path"]
    else:
        r = client.get(source["url"])
        r.raise_for_status()
        raw = r.content.decode(source.get("encoding", "utf-8"))
        location = source["url"]
    reader = csv.DictReader(io.StringIO(raw.lstrip("﻿")))
    if not reader.fieldnames or id_col not in reader.fieldnames or text_col not in reader.fieldnames:
        raise ValueError(
            f"CSV {location} needs columns {id_col!r} and {text_col!r}; found {reader.fieldnames}"
        )
    specs = []
    for row in reader:
        meta = {k: v for k, v in row.items() if k not in (id_col, text_col)}
        specs.append(ItemSpec(safe_id(str(row[id_col])), "text", "text_csv",
                              f"{location}#{row[id_col]}", Path(location).name,
                              text=normalize_text(row[text_col] or ""), metadata=meta))
    return specs


def list_source(src: dict, allowed: tuple[str, ...], client: httpx.Client,
                errors: list[str]) -> list[ItemSpec]:
    t = src["type"]
    if t == "local":
        return list_local(src, allowed, errors)
    if t == "zenodo":
        return list_zenodo(src, allowed, client, errors)
    if t == "url":
        return list_url(src, allowed, client, errors)
    if t == "text_csv":
        return list_text_csv(src, client) if "text" in allowed else []
    raise ValueError(f"Unknown source type {t!r}")


def stored_sources(db: Database) -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT id, spec, added_at, added_by FROM sources "
                           "ORDER BY added_at").fetchall()
    out = []
    for r in rows:
        spec = json.loads(r["spec"])
        spec.update(_id=r["id"], _added_at=r["added_at"], _added_by=r["added_by"])
        out.append(spec)
    return out


def all_sources(db: Database, config: Config) -> list[dict]:
    return [dict(s, _origin="config") for s in config.sources] + \
           [dict(s, _origin="added") for s in stored_sources(db)]


def normalise_new_source(body: dict) -> dict:
    """Turn what the Add-files dialog sends into a source spec (raises ValueError)."""
    kind = str(body.get("type", "")).lower()
    if kind == "local":
        p = str(body.get("path", "")).strip().strip('"').strip("'")
        if not p:
            raise ValueError("Enter or choose a folder or file.")
        path = Path(p).expanduser()
        if not path.exists():
            raise ValueError(f"Not found on this computer: {p}")
        spec = {"type": "local", "path": str(path.resolve())}
        if path.is_dir() and body.get("recursive"):
            spec["recursive"] = True
        return spec
    if kind == "url":
        url = str(body.get("url", "")).strip()
        if not re.match(r"https?://", url) and not re.match(r"10\.5281/zenodo\.\d+$", url):
            raise ValueError("Enter a link starting with http:// or https://")
        if is_zenodo_record_link(url):
            return {"type": "zenodo", "record": url}
        return {"type": "url", "url": url}
    raise ValueError("Unknown source type.")


def add_source(db: Database, config: Config, spec: dict, added_by: str | None) -> dict:
    """Check a new source (it must yield at least one usable file), store it, sync."""
    errors: list[str] = []
    with http_client() as client:
        specs = list_source(spec, config.modalities, client, errors)
    if not specs:
        kinds = " / ".join(config.modalities)
        raise ValueError("No supported files found" + (f" ({'; '.join(errors)})" if errors else
                         f". This project accepts {kinds} files; see the README for formats."))
    sid = new_id()
    with db.connect(immediate=True) as con:
        same = [r["id"] for r in con.execute("SELECT id, spec FROM sources")
                if {k: v for k, v in json.loads(r["spec"]).items()} == spec]
        if not same:
            con.execute("INSERT INTO sources(id, spec, added_at, added_by) VALUES (?,?,?,?)",
                        (sid, json.dumps(spec), now_iso(), added_by))
            db.audit(con, "source_add", added_by, sid, spec)
    report = sync_items(db, config)
    report["source_files"] = len(specs)
    report["source_file_errors"] = errors
    return report


def remove_source(db: Database, config: Config, source_id: str, by: str | None) -> dict:
    with db.connect(immediate=True) as con:
        cur = con.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if not cur.rowcount:
            raise KeyError(source_id)
        db.audit(con, "source_remove", by, source_id)
    return sync_items(db, config)


def discover(db: Database, config: Config) -> tuple[list[ItemSpec], list[str]]:
    """All items found in all sources, plus error messages (unreachable sources)."""
    specs: list[ItemSpec] = []
    errors: list[str] = []
    file_errors: list[str] = []
    with http_client() as client:
        for src in all_sources(db, config):
            try:
                specs += list_source(src, config.modalities, client, file_errors)
            except Exception as exc:  # report, keep going with other sources
                errors.append(f"{src['type']} source {source_label(src)}: {exc}")
    return specs, errors + file_errors


def sync_items(db: Database, config: Config) -> dict[str, Any]:
    """Add newly found items; mark missing ones inactive. Never deletes data.

    Items that already have annotations keep their stored text even if the
    source text changed, so character offsets stay valid; this is reported.
    """
    specs, errors = discover(db, config)
    added, updated, kept_text = 0, 0, []
    seen: dict[str, str] = {}
    with db.connect(immediate=True) as con:
        for spec in specs:
            # Same file listed twice (e.g. a folder and one of its files): keep one.
            if spec.location in seen.values():
                continue
            # Make item ids unique when different files share a name.
            base, n = spec.item_id, 2
            while spec.item_id in seen and seen[spec.item_id] != spec.location:
                spec.item_id = f"{base}_{n}"
                n += 1
            seen[spec.item_id] = spec.location
            th = text_hash(spec.text) if spec.text is not None else None
            row = con.execute("SELECT * FROM items WHERE item_id=?", (spec.item_id,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO items(item_id, modality, source_type, location, filename, "
                    "text, text_hash, metadata, active, added_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (spec.item_id, spec.modality, spec.source_type, spec.location,
                     spec.filename, spec.text, th, json.dumps(spec.metadata), now_iso()),
                )
                added += 1
                continue
            text, th_new = spec.text, th
            if row["text_hash"] and th and row["text_hash"] != th:
                has_segments = con.execute(
                    "SELECT 1 FROM segments s JOIN assignments a ON s.assignment_id=a.id "
                    "WHERE a.item_id=? LIMIT 1", (spec.item_id,)).fetchone()
                if has_segments:
                    text, th_new = row["text"], row["text_hash"]
                    kept_text.append(spec.item_id)
            duration = row["duration_s"] if row["location"] == spec.location else None
            con.execute(
                "UPDATE items SET modality=?, source_type=?, location=?, filename=?, text=?, "
                "text_hash=?, metadata=?, duration_s=?, active=1 WHERE item_id=?",
                (spec.modality, spec.source_type, spec.location, spec.filename, text, th_new,
                 json.dumps(spec.metadata), duration, spec.item_id),
            )
            updated += 1
        found = set(seen)
        deactivated = 0
        if not errors:  # only retire items when every source was reachable
            for row in con.execute("SELECT item_id FROM items WHERE active=1").fetchall():
                if row["item_id"] not in found:
                    con.execute("UPDATE items SET active=0 WHERE item_id=?", (row["item_id"],))
                    deactivated += 1
        total = con.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
    return {
        "at": now_iso(), "found": len(specs), "added": added, "updated": updated,
        "deactivated": deactivated, "active_items": total,
        "text_kept_because_annotated": kept_text, "errors": errors,
    }
