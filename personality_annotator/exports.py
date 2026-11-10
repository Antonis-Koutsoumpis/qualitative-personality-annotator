"""Reading annotations out of the database: long tables, CSV/JSON exports and
importing another rater's full export (merging raters from several laptops)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any, Iterable

from . import analysis
from .config import Config
from .db import Database, now_iso

ANNOTATION_COLUMNS = [
    "annotator_code", "item_id", "modality", "assignment_id", "assignment_status",
    "segment_id", "start_s", "end_s", "char_start", "char_end", "span_text",
    "rating_id", "variable", "score", "explanation", "notes",
    "segment_created_at", "rating_created_at", "rating_updated_at",
    "assignment_completed_at", "instance_id",
]


def load_ratings(
    con: sqlite3.Connection, item_id: str | None = None,
    annotators: Iterable[str] | None = None, include_open: bool = False,
) -> list[dict[str, Any]]:
    sql = """
        SELECT a.annotator_code, a.item_id, i.modality, a.id AS assignment_id,
               a.status AS assignment_status, s.id AS segment_id,
               s.start_s, s.end_s, s.char_start, s.char_end, s.span_text,
               r.id AS rating_id, r.variable, r.score, r.explanation, r.notes,
               s.created_at AS segment_created_at, r.created_at AS rating_created_at,
               r.updated_at AS rating_updated_at, a.completed_at AS assignment_completed_at,
               a.instance_id
        FROM ratings r
        JOIN segments s ON s.id = r.segment_id AND s.deleted = 0
        JOIN assignments a ON a.id = s.assignment_id
        JOIN items i ON i.item_id = a.item_id
        WHERE 1=1
    """
    params: list[Any] = []
    if not include_open:
        sql += " AND a.status = 'completed'"
    else:
        sql += " AND a.status IN ('completed', 'active')"
    if item_id:
        sql += " AND a.item_id = ?"
        params.append(item_id)
    annotators = [a for a in (annotators or []) if a]
    if annotators:
        sql += f" AND a.annotator_code IN ({','.join('?' * len(annotators))})"
        params += annotators
    sql += " ORDER BY a.item_id, a.annotator_code, COALESCE(s.start_s, s.char_start), r.variable"
    rows = [dict(r) for r in con.execute(sql, params)]
    for r in rows:
        r["annotator"] = r["annotator_code"]
    return rows


def item_coverage(con, config: Config, item: sqlite3.Row, ratings: list[dict],
                  bin_s: float | None = None, include_empty: bool = False) -> list[dict]:
    if config.modality == "text":
        return analysis.text_coverage(ratings, item["text"] or "", config.scale_midpoint,
                                      config.variable_ids, include_empty)
    return analysis.time_coverage(ratings, item["duration_s"], bin_s or config.coverage_bin_s,
                                  config.scale_midpoint, config.variable_ids, include_empty)


def to_csv(rows: list[dict], columns: list[str] | None = None) -> str:
    buf = io.StringIO()
    columns = columns or (list(rows[0].keys()) if rows else [])
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


def annotations_csv(db: Database, include_open: bool = False) -> str:
    with db.connect() as con:
        return to_csv(load_ratings(con, include_open=include_open), ANNOTATION_COLUMNS)


def _all_items(con):
    return con.execute("SELECT * FROM items WHERE active=1 ORDER BY item_id").fetchall()


def coverage_csv(db: Database, config: Config, include_open=False, include_empty=False,
                 bin_s: float | None = None) -> str:
    rows = []
    with db.connect() as con:
        for item in _all_items(con):
            ratings = load_ratings(con, item["item_id"], include_open=include_open)
            for row in item_coverage(con, config, item, ratings, bin_s, include_empty):
                rows.append({"item_id": item["item_id"], **row})
    if config.modality == "text":
        cols = ["item_id", "variable", "token_index", "token", "char_start", "char_end"]
    else:
        cols = ["item_id", "variable", "bin_index", "bin_start_s", "bin_end_s"]
    cols += ["n_annotators", "mean_score", "sd_score", "n_low", "n_mid", "n_high", "annotators"]
    return to_csv(rows, cols)


def peaks_rows(db: Database, config: Config, threshold: int, include_open=False,
               item_id: str | None = None, annotators=None, bin_s=None) -> list[dict]:
    out = []
    with db.connect() as con:
        items = ([con.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()]
                 if item_id else _all_items(con))
        for item in items:
            if item is None:
                continue
            ratings = load_ratings(con, item["item_id"], annotators, include_open)
            cov = item_coverage(con, config, item, ratings, bin_s)
            for p in analysis.peaks(cov, ratings, threshold, config.modality, item["text"]):
                out.append({"item_id": item["item_id"], **p})
    return out


def peaks_csv(db, config, threshold, include_open=False, bin_s=None) -> str:
    rows = peaks_rows(db, config, threshold, include_open, bin_s=bin_s)
    unit = "char" if config.modality == "text" else "s"
    cols = ["item_id", "variable", "start", "end", "max_n_annotators", "mean_score",
            "n_ratings", "annotators"] + (["span_text"] if config.modality == "text" else [])
    renamed = [{**r, f"start_{unit}": r["start"], f"end_{unit}": r["end"]} for r in rows]
    cols = [f"{c}_{unit}" if c in ("start", "end") else c for c in cols]
    return to_csv(renamed, cols)


def assignments_csv(db: Database) -> str:
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT a.id AS assignment_id, a.annotator_code, a.item_id, a.status,
                      a.reserved_at, a.completed_at, a.watched_proportion, a.scrolled_to_end,
                      a.reading_time_s, a.no_info, a.no_info_explanation,
                      (SELECT COUNT(*) FROM segments s WHERE s.assignment_id=a.id AND s.deleted=0)
                        AS n_segments,
                      a.instance_id
               FROM assignments a ORDER BY a.annotator_code, a.reserved_at""")]
    return to_csv(rows, ["assignment_id", "annotator_code", "item_id", "status", "reserved_at",
                         "completed_at", "watched_proportion", "scrolled_to_end",
                         "reading_time_s", "no_info", "no_info_explanation", "n_segments",
                         "instance_id"])


def annotators_csv(db: Database) -> str:
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT code AS annotator_code, age, gender, created_at, instance_id "
            "FROM annotators ORDER BY created_at")]
    return to_csv(rows, ["annotator_code", "age", "gender", "created_at", "instance_id"])


TABLES = ["annotators", "items", "assignments", "segments", "ratings", "audit_log"]


def full_dump(db: Database, config: Config) -> dict[str, Any]:
    with db.connect() as con:
        data = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in TABLES}
    return {
        "format": "personality-annotator-dump", "version": 1,
        "instance_id": db.instance_id, "exported_at": now_iso(),
        "project_name": config.project_name, "modality": config.modality,
        "config": config.raw | {"admin_password": None},
        **data,
    }


def import_dump(db: Database, config: Config, dump: dict[str, Any]) -> dict[str, Any]:
    """Merge another installation's full export. Existing rows are never overwritten.

    Items are matched by item_id; for text, the text hash must match so that
    character offsets refer to the same text.
    """
    if dump.get("format") != "personality-annotator-dump":
        raise ValueError("Not a full export from this tool (full_export.json).")
    if dump.get("modality") != config.modality:
        raise ValueError(f"Export modality {dump.get('modality')!r} does not match "
                         f"this project ({config.modality!r}).")
    counts = {t: 0 for t in ("annotators", "items", "assignments", "segments", "ratings")}
    warnings: list[str] = []
    with db.connect(immediate=True) as con:
        for it in dump.get("items", []):
            row = con.execute("SELECT text_hash FROM items WHERE item_id=?",
                              (it["item_id"],)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO items(item_id, modality, source_type, location, filename, "
                    "duration_s, text, text_hash, metadata, active, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (it["item_id"], it["modality"], it["source_type"], it["location"],
                     it.get("filename"), it.get("duration_s"), it.get("text"),
                     it.get("text_hash"), it.get("metadata"), it.get("active", 1),
                     it.get("added_at") or now_iso()))
                counts["items"] += 1
            elif row["text_hash"] and it.get("text_hash") and row["text_hash"] != it["text_hash"]:
                warnings.append(f"Item {it['item_id']}: text differs between installations; "
                                "imported character offsets may not line up.")
        skip_assign = set()
        for a in dump.get("annotators", []):
            cur = con.execute("INSERT OR IGNORE INTO annotators(code, age, gender, created_at, "
                              "instance_id) VALUES (?,?,?,?,?)",
                              (a["code"], a.get("age"), a.get("gender"), a["created_at"],
                               a.get("instance_id") or dump.get("instance_id")))
            counts["annotators"] += cur.rowcount
        for a in dump.get("assignments", []):
            clash = con.execute("SELECT id FROM assignments WHERE annotator_code=? AND item_id=?",
                                (a["annotator_code"], a["item_id"])).fetchone()
            if clash and clash["id"] != a["id"]:
                warnings.append(f"Annotator {a['annotator_code']} already has a different "
                                f"assignment for item {a['item_id']}; skipped (same code used on "
                                "two installations?).")
                skip_assign.add(a["id"])
                continue
            cols = ["id", "annotator_code", "item_id", "status", "reserved_at",
                    "last_activity_at", "completed_at", "watched_intervals",
                    "watched_proportion", "scrolled_to_end", "reading_time_s", "no_info",
                    "no_info_explanation", "instance_id"]
            vals = [a.get(c) for c in cols]
            vals[-1] = vals[-1] or dump.get("instance_id")
            cur = con.execute(f"INSERT OR IGNORE INTO assignments({','.join(cols)}) "
                              f"VALUES ({','.join('?' * len(cols))})", vals)
            counts["assignments"] += cur.rowcount
        skip_seg = set()
        for s in dump.get("segments", []):
            if s["assignment_id"] in skip_assign:
                skip_seg.add(s["id"])
                continue
            cols = ["id", "assignment_id", "start_s", "end_s", "char_start", "char_end",
                    "span_text", "created_at", "updated_at", "deleted"]
            cur = con.execute(f"INSERT OR IGNORE INTO segments({','.join(cols)}) "
                              f"VALUES ({','.join('?' * len(cols))})", [s.get(c) for c in cols])
            counts["segments"] += cur.rowcount
        for r in dump.get("ratings", []):
            if r["segment_id"] in skip_seg:
                continue
            cols = ["id", "segment_id", "variable", "score", "explanation", "notes",
                    "created_at", "updated_at"]
            cur = con.execute(f"INSERT OR IGNORE INTO ratings({','.join(cols)}) "
                              f"VALUES ({','.join('?' * len(cols))})", [r.get(c) for c in cols])
            counts["ratings"] += cur.rowcount
        db.audit(con, "import", None, dump.get("instance_id"), {"counts": counts})
    unknown = sorted({r["variable"] for r in dump.get("ratings", [])} - set(config.variable_ids))
    if unknown:
        warnings.append(f"Imported ratings use variables not in this config: {unknown}")
    return {"imported": counts, "warnings": warnings}


def dump_json(db, config) -> str:
    return json.dumps(full_dump(db, config), ensure_ascii=False, indent=1)
