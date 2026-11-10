"""FastAPI application: annotator API, admin API, media serving and static pages."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import analysis, assignment, exports, extract
from .config import Config, load_config
from .db import Database, new_id, now_iso
from .sources import HTTP_TIMEOUT, sync_items

STATIC = Path(__file__).parent / "static"
ANN_COOKIE = "pa_annotator"
ADMIN_COOKIE = "pa_admin"


# ---------------------------------------------------------------- helpers

def merge_intervals(intervals: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if out and s <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in out]


def covered(intervals: list[list[float]], duration: float) -> float:
    return sum(max(0.0, min(e, duration) - max(s, 0.0)) for s, e in intervals)


def admin_token(password: str) -> str:
    return hashlib.sha256(("pa-admin:" + password).encode()).hexdigest()


def create_app(config_path: str | os.PathLike) -> FastAPI:
    config: Config = load_config(config_path)
    db = Database(config.data_dir / "annotations.sqlite")
    cache_dir = config.data_dir / "media_cache"
    work_dir = config.data_dir / "tmp"
    admin_password = os.environ.get("ANNOTATOR_ADMIN_PASSWORD", "")
    state: dict[str, Any] = {"sync": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["sync"] = {"at": now_iso(), **sync_items(db, config)}
        state["client"] = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True)
        yield
        await state["client"].aclose()

    app = FastAPI(title="Personality Annotator", lifespan=lifespan)
    app.state.config, app.state.db = config, db
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------ auth helpers

    def annotator_of(request: Request) -> str:
        code = request.cookies.get(ANN_COOKIE)
        if not code:
            raise HTTPException(401, "Not logged in")
        with db.connect() as con:
            if not con.execute("SELECT 1 FROM annotators WHERE code=?", (code,)).fetchone():
                raise HTTPException(401, "Unknown participant code")
        return code

    def require_admin(request: Request) -> None:
        if not admin_password:
            return
        tok = request.cookies.get(ADMIN_COOKIE, "")
        if not hmac.compare_digest(tok, admin_token(admin_password)):
            raise HTTPException(401, "Admin login required")

    def own_assignment(con, aid: str, code: str, must_be_active: bool = True):
        a = con.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        if a is None or a["annotator_code"] != code:
            raise HTTPException(404, "Assignment not found")
        if must_be_active and a["status"] != "active":
            raise HTTPException(409, "This item has already been submitted")
        return a

    # ------------------------------------------------------------ pages

    def page(name: str) -> HTMLResponse:
        return HTMLResponse((STATIC / name).read_text(encoding="utf-8"))

    @app.get("/", include_in_schema=False)
    def index():
        return page("index.html")

    @app.get("/annotate", include_in_schema=False)
    def annotate_page():
        return page("annotate.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return page("admin.html")

    # ------------------------------------------------------------ public API

    @app.get("/api/config")
    def get_config():
        return config.public_dict()

    @app.post("/api/login")
    def login(body: dict = Body(...)):
        code = str(body.get("code", "")).strip()
        if not code or len(code) > 64:
            raise HTTPException(400, "Please enter a participant code (max. 64 characters).")
        with db.connect(immediate=True) as con:
            row = con.execute("SELECT * FROM annotators WHERE code=?", (code,)).fetchone()
            if row is None:
                age, gender = body.get("age"), body.get("gender")
                if age in (None, "") or not gender:
                    return JSONResponse({"needs_demographics": True})
                try:
                    age = int(age)
                except (TypeError, ValueError):
                    raise HTTPException(400, "Age must be a whole number.")
                if not (config.min_age <= age <= config.max_age):
                    raise HTTPException(400, f"Age must be between {config.min_age} and "
                                             f"{config.max_age}.")
                if gender not in config.gender_options:
                    raise HTTPException(400, "Please choose a gender option.")
                con.execute("INSERT INTO annotators(code, age, gender, created_at, instance_id) "
                            "VALUES (?,?,?,?,?)", (code, age, gender, now_iso(), db.instance_id))
                db.audit(con, "register", code)
            else:
                db.audit(con, "login", code)
        resp = JSONResponse({"ok": True, "code": code})
        resp.set_cookie(ANN_COOKIE, code, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(ANN_COOKIE)
        return resp

    def assignment_payload(con, aid: str) -> dict:
        a = con.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        item = con.execute("SELECT * FROM items WHERE item_id=?", (a["item_id"],)).fetchone()
        segs = []
        for s in con.execute("SELECT * FROM segments WHERE assignment_id=? AND deleted=0 "
                             "ORDER BY COALESCE(start_s, char_start), created_at", (aid,)):
            ratings = [dict(r) for r in con.execute(
                "SELECT id, variable, score, explanation, notes FROM ratings "
                "WHERE segment_id=? ORDER BY created_at", (s["id"],))]
            segs.append({**dict(s), "ratings": ratings})
        return {
            "assignment": {
                "id": a["id"], "status": a["status"],
                "watched_intervals": json.loads(a["watched_intervals"] or "[]"),
                "watched_proportion": a["watched_proportion"] or 0,
                "scrolled_to_end": bool(a["scrolled_to_end"]),
                "reading_time_s": a["reading_time_s"] or 0,
                "no_info": bool(a["no_info"]),
                "no_info_explanation": a["no_info_explanation"] or "",
            },
            "item": {
                "item_id": item["item_id"], "modality": item["modality"],
                "filename": item["filename"], "duration_s": item["duration_s"],
                "media_url": None if item["modality"] == "text" else f"/media/{item['item_id']}",
                "text": item["text"] if item["modality"] == "text" else None,
            },
            "segments": segs,
        }

    @app.get("/api/me")
    def me(request: Request):
        code = annotator_of(request)
        with db.connect() as con:
            prog = assignment.annotator_progress(con, code, config.annotators_per_item,
                                                 config.reservation_timeout_h)
        return {"code": code, "progress": prog}

    @app.get("/api/current")
    def current(request: Request):
        code = annotator_of(request)
        aid = assignment.current_or_next(db, code, config.annotators_per_item,
                                         config.reservation_timeout_h)
        with db.connect() as con:
            prog = assignment.annotator_progress(con, code, config.annotators_per_item,
                                                 config.reservation_timeout_h)
            if aid is None:
                return {"done": True, "progress": prog}
            return {"done": False, "progress": prog, **assignment_payload(con, aid)}

    @app.post("/api/assignments/{aid}/progress")
    def progress(aid: str, request: Request, body: dict = Body(...)):
        code = annotator_of(request)
        with db.connect(immediate=True) as con:
            a = own_assignment(con, aid, code)
            item = con.execute("SELECT * FROM items WHERE item_id=?", (a["item_id"],)).fetchone()
            updates: dict[str, Any] = {"last_activity_at": now_iso()}
            dur = body.get("duration_s")
            if dur and float(dur) > 0 and not item["duration_s"]:
                con.execute("UPDATE items SET duration_s=? WHERE item_id=?",
                            (float(dur), item["item_id"]))
            duration = float(item["duration_s"] or dur or 0)
            if item["modality"] != "text":
                old = json.loads(a["watched_intervals"] or "[]")
                new = [[float(s), float(e)] for s, e in body.get("watched_intervals", [])]
                merged = merge_intervals(old + new)
                updates["watched_intervals"] = json.dumps(merged)
                if duration > 0:
                    updates["watched_proportion"] = round(min(1.0, covered(merged, duration)
                                                              / duration), 4)
            else:
                if body.get("scrolled_to_end"):
                    updates["scrolled_to_end"] = 1
                if body.get("reading_time_s") is not None:
                    updates["reading_time_s"] = max(float(a["reading_time_s"] or 0),
                                                    float(body["reading_time_s"]))
            sets = ", ".join(f"{k}=?" for k in updates)
            con.execute(f"UPDATE assignments SET {sets} WHERE id=?", (*updates.values(), aid))
            a = con.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        return {"watched_proportion": a["watched_proportion"] or 0,
                "watched_intervals": json.loads(a["watched_intervals"] or "[]"),
                "scrolled_to_end": bool(a["scrolled_to_end"]),
                "reading_time_s": a["reading_time_s"] or 0}

    def validate_ratings(ratings: list) -> list[dict]:
        if not ratings:
            raise HTTPException(400, "Add at least one rating to the segment.")
        clean, seen = [], set()
        for r in ratings:
            var = str(r.get("variable", ""))
            if var not in config.variable_ids:
                raise HTTPException(400, f"Unknown variable {var!r}.")
            if var in seen:
                raise HTTPException(400, f"Variable {var!r} appears twice in this segment.")
            seen.add(var)
            try:
                score = float(r.get("score"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Choose a score for {var}.")
            if not (config.scale_min <= score <= config.scale_max):
                raise HTTPException(400, f"Score for {var} is outside the scale.")
            expl = str(r.get("explanation", "")).strip()
            if not expl:
                raise HTTPException(400, f"Please explain your rating for {var}.")
            clean.append({"variable": var, "score": score, "explanation": expl,
                          "notes": str(r.get("notes", "") or "").strip()})
        return clean

    def segment_bounds(con, a, body: dict) -> dict:
        item = con.execute("SELECT * FROM items WHERE item_id=?", (a["item_id"],)).fetchone()
        if item["modality"] == "text":
            text = item["text"] or ""
            try:
                cs, ce = int(body["char_start"]), int(body["char_end"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "char_start and char_end are required.")
            cs, ce = max(0, cs), min(len(text), ce)
            if ce <= cs:
                raise HTTPException(400, "Select some text first.")
            cs, ce = analysis.snap_to_words(text, cs, ce)
            return {"start_s": None, "end_s": None, "char_start": cs, "char_end": ce,
                    "span_text": text[cs:ce]}
        try:
            s, e = float(body["start_s"]), float(body["end_s"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "start_s and end_s are required.")
        dur = item["duration_s"]
        if s < 0 or e <= s or (dur and e > dur + 0.5):
            raise HTTPException(400, "The segment end must come after its start, within "
                                     "the item's duration.")
        return {"start_s": round(s, 3), "end_s": round(e, 3), "char_start": None,
                "char_end": None, "span_text": None}

    def write_ratings(con, seg_id: str, ratings: list[dict]) -> None:
        ts = now_iso()
        con.execute("DELETE FROM ratings WHERE segment_id=?", (seg_id,))
        for r in ratings:
            con.execute("INSERT INTO ratings(id, segment_id, variable, score, explanation, "
                        "notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (new_id(), seg_id, r["variable"], r["score"], r["explanation"],
                         r["notes"], ts, ts))


    @app.post("/api/assignments/{aid}/segments")
    def add_segment(aid: str, request: Request, body: dict = Body(...)):
        code = annotator_of(request)
        ratings = validate_ratings(body.get("ratings") or [])
        with db.connect(immediate=True) as con:
            a = own_assignment(con, aid, code)
            b = segment_bounds(con, a, body)
            sid, ts = new_id(), now_iso()
            con.execute("INSERT INTO segments(id, assignment_id, start_s, end_s, char_start, "
                        "char_end, span_text, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (sid, aid, b["start_s"], b["end_s"], b["char_start"], b["char_end"],
                         b["span_text"], ts, ts))
            write_ratings(con, sid, ratings)
            con.execute("UPDATE assignments SET last_activity_at=?, no_info=0 WHERE id=?",
                        (ts, aid))
            db.audit(con, "segment_create", code, sid, {**b, "ratings": ratings})
            return assignment_payload(con, aid)

    @app.put("/api/segments/{sid}")
    def update_segment(sid: str, request: Request, body: dict = Body(...)):
        code = annotator_of(request)
        ratings = validate_ratings(body.get("ratings") or [])
        with db.connect(immediate=True) as con:
            s = con.execute("SELECT * FROM segments WHERE id=? AND deleted=0", (sid,)).fetchone()
            if s is None:
                raise HTTPException(404, "Segment not found")
            a = own_assignment(con, s["assignment_id"], code)
            b = segment_bounds(con, a, body)
            ts = now_iso()
            con.execute("UPDATE segments SET start_s=?, end_s=?, char_start=?, char_end=?, "
                        "span_text=?, updated_at=? WHERE id=?",
                        (b["start_s"], b["end_s"], b["char_start"], b["char_end"],
                         b["span_text"], ts, sid))
            write_ratings(con, sid, ratings)
            db.audit(con, "segment_update", code, sid, {**b, "ratings": ratings})
            return assignment_payload(con, a["id"])

    @app.delete("/api/segments/{sid}")
    def delete_segment(sid: str, request: Request):
        code = annotator_of(request)
        with db.connect(immediate=True) as con:
            s = con.execute("SELECT * FROM segments WHERE id=? AND deleted=0", (sid,)).fetchone()
            if s is None:
                raise HTTPException(404, "Segment not found")
            a = own_assignment(con, s["assignment_id"], code)
            con.execute("UPDATE segments SET deleted=1, updated_at=? WHERE id=?",
                        (now_iso(), sid))
            db.audit(con, "segment_delete", code, sid)
            return assignment_payload(con, a["id"])

    @app.post("/api/assignments/{aid}/submit")
    def submit(aid: str, request: Request, body: dict = Body(default={})):
        code = annotator_of(request)
        with db.connect(immediate=True) as con:
            a = own_assignment(con, aid, code)
            item = con.execute("SELECT * FROM items WHERE item_id=?", (a["item_id"],)).fetchone()
            if item["modality"] == "text":
                if config.require_scroll_to_end and not a["scrolled_to_end"]:
                    raise HTTPException(400, "Please read the whole text (scroll to the end) "
                                             "before submitting.")
                if (a["reading_time_s"] or 0) < config.min_reading_time_s:
                    raise HTTPException(400, "Please take a bit more time to read the text.")
            elif (a["watched_proportion"] or 0) + 1e-9 < config.min_watched_proportion:
                raise HTTPException(400, f"Please play at least "
                                         f"{config.min_watched_proportion:.0%} of the item "
                                         "before submitting.")
            n_seg = con.execute(
                "SELECT COUNT(*) FROM segments s WHERE s.assignment_id=? AND s.deleted=0 AND "
                "EXISTS (SELECT 1 FROM ratings r WHERE r.segment_id=s.id)", (aid,)).fetchone()[0]
            no_info = bool(body.get("no_info"))
            expl = str(body.get("no_info_explanation", "") or "").strip()
            if n_seg == 0 and not (no_info and expl):
                raise HTTPException(400, "Mark at least one segment, or tick 'No personality "
                                         "information in this item' and explain why.")
            if n_seg > 0 and no_info:
                raise HTTPException(400, "You marked segments, so untick 'No personality "
                                         "information'.")
            ts = now_iso()
            con.execute("UPDATE assignments SET status='completed', completed_at=?, "
                        "last_activity_at=?, no_info=?, no_info_explanation=? WHERE id=?",
                        (ts, ts, int(no_info), expl if no_info else None, aid))
            db.audit(con, "submit", code, aid, {"n_segments": n_seg, "no_info": no_info})
        return {"ok": True}

    # ------------------------------------------------------------ media

    def _item(item_id: str):
        with db.connect() as con:
            item = con.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if item is None or item["modality"] == "text":
            raise HTTPException(404, "No media for this item")
        return dict(item)

    @app.get("/media/{item_id}")
    async def media(item_id: str, request: Request):
        if not (request.cookies.get(ANN_COOKIE) or request.cookies.get(ADMIN_COOKIE)
                or not admin_password):
            raise HTTPException(401, "Login required")
        item = _item(item_id)
        if item["source_type"] == "local":
            p = Path(item["location"])
            if not p.exists():
                raise HTTPException(404, f"File missing: {p.name}")
            return FileResponse(p)
        cached = cache_dir / f"{item_id}{Path(item['filename'] or '').suffix}"
        if cached.exists():
            return FileResponse(cached)
        if config.cache_media:
            import anyio
            path = await anyio.to_thread.run_sync(extract.media_path, item, cache_dir)
            return FileResponse(path)
        # Proxy the remote file, passing Range through so seeking works.
        client: httpx.AsyncClient = state["client"]
        headers = {}
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]
        upstream = await client.send(client.build_request("GET", item["location"],
                                                          headers=headers), stream=True)
        if upstream.status_code >= 400:
            await upstream.aclose()
            raise HTTPException(502, f"Remote source returned {upstream.status_code}")
        passthrough = {k: v for k, v in upstream.headers.items()
                       if k.lower() in ("content-type", "content-length", "content-range",
                                        "accept-ranges", "last-modified", "etag")}
        if "content-type" not in {k.lower() for k in passthrough} or \
                passthrough.get("content-type", "").startswith("application/octet"):
            import mimetypes
            passthrough["content-type"] = mimetypes.guess_type(item["filename"] or "")[0] \
                or "application/octet-stream"
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=passthrough, background=BackgroundTask(upstream.aclose))

    # ------------------------------------------------------------ admin API

    @app.post("/api/admin/login")
    def admin_login(body: dict = Body(...)):
        if not admin_password:
            return {"ok": True, "password_required": False}
        if not hmac.compare_digest(str(body.get("password", "")), admin_password):
            raise HTTPException(401, "Wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(ADMIN_COOKIE, admin_token(admin_password), httponly=True,
                        samesite="lax", max_age=60 * 60 * 12)
        return resp

    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        require_admin(request)
        k = config.annotators_per_item
        with db.connect() as con:
            items = [dict(r) for r in con.execute(
                """SELECT i.item_id, i.filename, i.source_type, i.duration_s, i.active,
                          SUM(a.status='completed') AS n_completed,
                          SUM(a.status='active') AS n_open,
                          SUM(a.status='completed' AND a.no_info=1) AS n_no_info
                   FROM items i LEFT JOIN assignments a ON a.item_id=i.item_id
                   GROUP BY i.item_id ORDER BY i.item_id""")]
            for it in items:
                for key in ("n_completed", "n_open", "n_no_info"):
                    it[key] = it[key] or 0
            annotators = []
            for r in con.execute("SELECT * FROM annotators ORDER BY created_at"):
                prog = assignment.annotator_progress(con, r["code"], k,
                                                     config.reservation_timeout_h)
                n_ratings = con.execute(
                    "SELECT COUNT(*) FROM ratings r JOIN segments s ON s.id=r.segment_id "
                    "JOIN assignments a ON a.id=s.assignment_id WHERE a.annotator_code=? "
                    "AND s.deleted=0", (r["code"],)).fetchone()[0]
                annotators.append({"code": r["code"], "age": r["age"], "gender": r["gender"],
                                   "created_at": r["created_at"], "instance_id": r["instance_id"],
                                   "n_ratings": n_ratings, **prog})
            no_info = [dict(r) for r in con.execute(
                "SELECT annotator_code, item_id, no_info_explanation FROM assignments "
                "WHERE no_info=1 AND status='completed'")]
        active = [i for i in items if i["active"]]
        return {
            "project_name": config.project_name, "modality": config.modality,
            "k": k, "instance_id": db.instance_id, "config_path": str(config.path),
            "data_dir": str(config.data_dir),
            "password_protected": bool(admin_password),
            "sources": config.sources, "sync": state["sync"],
            "totals": {
                "items": len(active),
                "target_annotations": len(active) * k,
                "completed": sum(min(i["n_completed"], k) for i in active),
                "items_complete": sum(i["n_completed"] >= k for i in active),
            },
            "items": items, "annotators": annotators, "no_info": no_info,
            "variables": [v.__dict__ for v in config.variables],
            "scale": config.public_dict()["scale"],
        }

    @app.post("/api/admin/sync")
    def admin_sync(request: Request):
        require_admin(request)
        state["sync"] = {"at": now_iso(), **sync_items(db, config)}
        return state["sync"]

    def _annot_list(s: str | None) -> list[str]:
        return [x for x in (s or "").split(",") if x]

    @app.get("/api/admin/items/{item_id}/results")
    def item_results(item_id: str, request: Request, annotators: str | None = None,
                     include_open: bool = False, bin_s: float | None = None):
        require_admin(request)
        with db.connect() as con:
            item = con.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            if item is None:
                raise HTTPException(404, "Item not found")
            ratings = exports.load_ratings(con, item_id, _annot_list(annotators), include_open)
            cov = exports.item_coverage(con, config, item, ratings, bin_s)
            raters = [r["annotator_code"] for r in con.execute(
                "SELECT DISTINCT annotator_code FROM assignments WHERE item_id=? AND status IN "
                "('completed','active') ORDER BY annotator_code", (item_id,))]
            no_info = [dict(r) for r in con.execute(
                "SELECT annotator_code, no_info_explanation FROM assignments WHERE item_id=? "
                "AND no_info=1 AND status='completed'", (item_id,))]
        return {
            "item": {"item_id": item["item_id"], "modality": item["modality"],
                     "filename": item["filename"], "duration_s": item["duration_s"],
                     "text": item["text"],
                     "media_url": None if item["modality"] == "text" else f"/media/{item_id}"},
            "ratings": ratings, "coverage": cov, "raters": raters, "no_info": no_info,
            "bin_s": bin_s or config.coverage_bin_s,
        }

    @app.get("/api/admin/items/{item_id}/peaks")
    def item_peaks(item_id: str, request: Request, threshold: int = 1,
                   annotators: str | None = None, include_open: bool = False,
                   bin_s: float | None = None):
        require_admin(request)
        return exports.peaks_rows(db, config, threshold, include_open, item_id,
                                  _annot_list(annotators), bin_s)

    @app.get("/api/admin/export/{name}")
    def export(name: str, request: Request, include_open: bool = False,
               include_empty: bool = False, threshold: int = 1, bin_s: float | None = None):
        require_admin(request)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if name == "full_export.json":
            body, media = exports.dump_json(db, config), "application/json"
        elif name == "annotations_long.csv":
            body, media = exports.annotations_csv(db, include_open), "text/csv"
        elif name == "coverage_long.csv":
            body, media = exports.coverage_csv(db, config, include_open, include_empty,
                                               bin_s), "text/csv"
        elif name == "peak_segments.csv":
            body, media = exports.peaks_csv(db, config, threshold, include_open,
                                            bin_s), "text/csv"
        elif name == "assignments.csv":
            body, media = exports.assignments_csv(db), "text/csv"
        elif name == "annotators.csv":
            body, media = exports.annotators_csv(db), "text/csv"
        else:
            raise HTTPException(404, "Unknown export")
        stem, ext = name.rsplit(".", 1)
        return Response(body.encode("utf-8"), media_type=media + "; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{stem}_{stamp}.{ext}"'})

    @app.post("/api/admin/import")
    async def import_export(request: Request, file: UploadFile = File(...)):
        require_admin(request)
        try:
            dump = json.loads((await file.read()).decode("utf-8"))
            return exports.import_dump(db, config, dump)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/admin/extract")
    def extract_zip(request: Request, body: dict = Body(...)):
        """body.mode: 'segments' (list of segment ids), 'region' (item_id, start, end,
        variable) or 'peaks' (threshold, optional item_id / variable)."""
        require_admin(request)
        mode = body.get("mode")
        include_open = bool(body.get("include_open"))
        annots = body.get("annotators") or []
        with db.connect() as con:
            all_ratings = exports.load_ratings(con, body.get("item_id"), annots, True)
            items = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM items")}
        if not include_open:
            all_ratings = [r for r in all_ratings if r["assignment_status"] == "completed"]
        text_mode = config.modality == "text"
        sk, ek = ("char_start", "char_end") if text_mode else ("start_s", "end_s")

        def rating_out(r):
            return {"annotator_code": r["annotator_code"], "variable": r["variable"],
                    "score": r["score"], "explanation": r["explanation"], "notes": r["notes"],
                    "segment_start": r[sk], "segment_end": r[ek],
                    "segment_id": r["segment_id"], "rating_id": r["rating_id"],
                    "span_text": r["span_text"]}

        segs: list[dict] = []
        if mode == "segments":
            ids = set(body.get("segment_ids") or [])
            with db.connect() as con:
                rows = exports.load_ratings(con, None, None, True)
            by_seg: dict[str, list] = {}
            for r in rows:
                if r["segment_id"] in ids:
                    by_seg.setdefault(r["segment_id"], []).append(r)
            for sid, rs in by_seg.items():
                r0 = rs[0]
                segs.append({"item_id": r0["item_id"], "start": r0[sk], "end": r0[ek],
                             "label": f"{r0['annotator_code']}__seg{sid[:8]}",
                             "kind": "annotator_segment", "segment_id": sid,
                             "annotator_code": r0["annotator_code"],
                             "ratings": [rating_out(r) for r in rs]})
        elif mode in ("region", "peaks"):
            if mode == "region":
                regions = [{"item_id": body["item_id"], "variable": body.get("variable"),
                            "start": float(body["start"]), "end": float(body["end"])}]
            else:
                regions = exports.peaks_rows(db, config, int(body.get("threshold", 1)),
                                             include_open, body.get("item_id"), annots,
                                             body.get("bin_s"))
                if body.get("variable"):
                    regions = [p for p in regions if p["variable"] == body["variable"]]
            for reg in regions:
                s, e = float(reg["start"]), float(reg["end"])
                rs = [r for r in all_ratings if r["item_id"] == reg["item_id"]
                      and (not reg.get("variable") or r["variable"] == reg["variable"])
                      and r[sk] is not None and float(r[sk]) < e
                      and max(float(r[ek]), float(r[sk]) + 1e-6) > s]
                segs.append({"item_id": reg["item_id"],
                             "start": int(s) if text_mode else s,
                             "end": int(e) if text_mode else e,
                             "label": reg.get("variable") or "region",
                             "kind": "peak" if mode == "peaks" else "region",
                             "variable": reg.get("variable"),
                             "max_n_annotators": reg.get("max_n_annotators"),
                             "mean_score": reg.get("mean_score"),
                             "ratings": [rating_out(r) for r in rs]})
        else:
            raise HTTPException(400, "mode must be segments, region or peaks")
        if not segs:
            raise HTTPException(400, "Nothing to extract with these settings.")
        if len(segs) > 2000:
            raise HTTPException(400, "More than 2000 segments; use the command-line "
                                     "extraction script instead.")
        work_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=work_dir)
        os.close(fd)
        try:
            extract.build_zip(segs, items, config.modality, cache_dir, work_dir, Path(tmp))
        except extract.ExtractionError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise HTTPException(500, str(exc))
        name = f"segments_{mode}_{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return FileResponse(tmp, filename=name, media_type="application/zip",
                            background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))

    @app.get("/api/health")
    def health():
        return PlainTextResponse("ok")

    return app
