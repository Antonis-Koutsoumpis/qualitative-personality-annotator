"""Discovering items from the configured sources and syncing them into the database.

Supported source types (see config.example.yaml):
  local     - a folder on this computer
  zenodo    - a public Zenodo record (link, DOI or numeric id)
  text_csv  - a CSV with one text per row (local path or URL), text modality only

New storage back-ends (e.g. WebDAV for Yoda) can be added by writing another
`list_*` function that returns `ItemSpec`s and adding a branch in `discover`.
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
from urllib.parse import quote

import httpx

from .config import EXTENSIONS, Config
from .db import Database, now_iso

ZENODO_API = "https://zenodo.org/api/records/{id}"
ZENODO_FILE_BASE = "https://zenodo.org/records/{id}/files"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


@dataclass
class ItemSpec:
    item_id: str
    source_type: str          # local | zenodo | text_csv
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


def http_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)


def list_local(source: dict, modality: str) -> list[ItemSpec]:
    root = Path(source["path"])
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")
    pattern = source.get("pattern", "*")
    recursive = bool(source.get("recursive", False))
    exts = EXTENSIONS[modality]
    files = root.rglob("*") if recursive else root.glob("*")
    specs = []
    for p in sorted(files):
        if not p.is_file() or p.suffix.lower() not in exts or p.name.startswith("."):
            continue
        if not fnmatch.fnmatch(p.name, pattern):
            continue
        rel = p.relative_to(root).with_suffix("")
        item_id = safe_id("__".join(rel.parts))
        spec = ItemSpec(item_id, "local", str(p), p.name)
        if modality == "text":
            spec.text = normalize_text(p.read_text(encoding=source.get("encoding", "utf-8")))
        specs.append(spec)
    return specs


def list_zenodo(source: dict, modality: str, client: httpx.Client) -> list[ItemSpec]:
    rec_id = parse_zenodo_record(source["record"])
    resp = client.get(ZENODO_API.format(id=rec_id))
    resp.raise_for_status()
    data = resp.json()
    files = data.get("files") or []
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
    exts = EXTENSIONS[modality]
    specs = []
    base = ZENODO_FILE_BASE.format(id=rec_id)
    for f in files:
        key = f.get("key") or f.get("filename")
        if not key or Path(key).suffix.lower() not in exts or not fnmatch.fnmatch(key, pattern):
            continue
        # The public download link is the most stable form across API versions.
        url = f"{base}/{quote(key)}?download=1"
        item_id = safe_id(Path(key).stem)
        spec = ItemSpec(
            item_id,
            "zenodo",
            url,
            key,
            metadata={"zenodo_record": rec_id, "size": f.get("size"),
                      "checksum": f.get("checksum")},
        )
        if modality == "text":
            r = client.get(url)
            r.raise_for_status()
            spec.text = normalize_text(r.content.decode(source.get("encoding", "utf-8")))
        specs.append(spec)
    return specs


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
        item_id = safe_id(str(row[id_col]))
        meta = {k: v for k, v in row.items() if k not in (id_col, text_col)}
        specs.append(
            ItemSpec(item_id, "text_csv", f"{location}#{row[id_col]}", Path(location).name,
                     text=normalize_text(row[text_col] or ""), metadata=meta)
        )
    return specs


def discover(config: Config) -> tuple[list[ItemSpec], list[str]]:
    """Return all items found in all sources, plus a list of error messages."""
    specs: list[ItemSpec] = []
    errors: list[str] = []
    with http_client() as client:
        for src in config.sources:
            label = src.get("path") or src.get("record") or src.get("url")
            try:
                if src["type"] == "local":
                    specs += list_local(src, config.modality)
                elif src["type"] == "zenodo":
                    specs += list_zenodo(src, config.modality, client)
                elif src["type"] == "text_csv":
                    specs += list_text_csv(src, client)
            except Exception as exc:  # report, keep going with other sources
                errors.append(f"{src['type']} source {label}: {exc}")
    return specs, errors


def sync_items(db: Database, config: Config) -> dict[str, Any]:
    """Add newly found items; mark missing ones inactive. Never deletes data.

    Items that already have annotations keep their stored text even if the
    source text changed, so character offsets stay valid; this is reported.
    """
    specs, errors = discover(config)
    added, updated, kept_text = 0, 0, []
    seen: dict[str, str] = {}
    with db.connect(immediate=True) as con:
        for spec in specs:
            # Make item ids unique when two sources contain the same file name.
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
                    (spec.item_id, config.modality, spec.source_type, spec.location,
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
            con.execute(
                "UPDATE items SET source_type=?, location=?, filename=?, text=?, text_hash=?, "
                "metadata=?, active=1 WHERE item_id=?",
                (spec.source_type, spec.location, spec.filename, text, th_new,
                 json.dumps(spec.metadata), spec.item_id),
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
        "found": len(specs), "added": added, "updated": updated,
        "deactivated": deactivated, "active_items": total,
        "text_kept_because_annotated": kept_text, "errors": errors,
    }
