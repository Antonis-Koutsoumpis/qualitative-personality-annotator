"""Which item an annotator gets next.

Rules:
- An annotator keeps working on their open (active) assignment until submitted.
- A new item is chosen among active items the annotator has never been
  assigned and that have fewer than k assignments counting completed ones and
  active ones still within the reservation timeout.
- Annotations are spread evenly: random choice among eligible items with the
  lowest current count.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import Database, new_id, now_iso


def _cutoff(timeout_h: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=timeout_h)).isoformat(timespec="seconds")


def item_counts(con: sqlite3.Connection, timeout_h: float) -> dict[str, int]:
    """Completed + still-reserved assignments per active item."""
    rows = con.execute(
        """
        SELECT i.item_id,
               COALESCE(SUM(CASE WHEN a.status='completed' THEN 1
                                 WHEN a.status='active' AND a.last_activity_at >= ? THEN 1
                                 ELSE 0 END), 0) AS n
        FROM items i LEFT JOIN assignments a ON a.item_id = i.item_id
        WHERE i.active = 1
        GROUP BY i.item_id
        """,
        (_cutoff(timeout_h),),
    ).fetchall()
    return {r["item_id"]: r["n"] for r in rows}


def eligible_items(con: sqlite3.Connection, annotator: str, k: int, timeout_h: float) -> dict[str, int]:
    counts = item_counts(con, timeout_h)
    mine = {r["item_id"] for r in con.execute(
        "SELECT item_id FROM assignments WHERE annotator_code=?", (annotator,))}
    return {i: n for i, n in counts.items() if i not in mine and n < k}


def current_or_next(db: Database, annotator: str, k: int, timeout_h: float,
                    rng: random.Random | None = None) -> str | None:
    """Return the id of the annotator's open assignment, creating one if needed."""
    rng = rng or random.Random()
    with db.connect(immediate=True) as con:
        row = con.execute(
            "SELECT a.id, a.item_id, a.last_activity_at FROM assignments a "
            "JOIN items i ON i.item_id=a.item_id "
            "WHERE a.annotator_code=? AND a.status='active' AND i.active=1 "
            "ORDER BY a.reserved_at LIMIT 1", (annotator,)).fetchone()
        if row and row["last_activity_at"] < _cutoff(timeout_h):
            # Reservation expired; if others have since filled the item, release it.
            if item_counts(con, timeout_h).get(row["item_id"], 0) >= k:
                con.execute("UPDATE assignments SET status='released' WHERE id=?", (row["id"],))
                db.audit(con, "release_expired", annotator, row["id"])
                row = None
        if row:
            con.execute("UPDATE assignments SET last_activity_at=? WHERE id=?",
                        (now_iso(), row["id"]))
            return row["id"]
        eligible = eligible_items(con, annotator, k, timeout_h)
        if not eligible:
            return None
        lowest = min(eligible.values())
        choice = rng.choice(sorted(i for i, n in eligible.items() if n == lowest))
        aid = new_id()
        ts = now_iso()
        con.execute(
            "INSERT INTO assignments(id, annotator_code, item_id, status, reserved_at, "
            "last_activity_at, instance_id) VALUES (?,?,?,?,?,?,?)",
            (aid, annotator, choice, "active", ts, ts, db.instance_id),
        )
        db.audit(con, "assign", annotator, aid, {"item_id": choice})
        return aid


def annotator_progress(con: sqlite3.Connection, annotator: str, k: int, timeout_h: float) -> dict:
    done = con.execute(
        "SELECT COUNT(*) FROM assignments WHERE annotator_code=? AND status='completed'",
        (annotator,)).fetchone()[0]
    remaining = len(eligible_items(con, annotator, k, timeout_h))
    open_ = con.execute(
        "SELECT COUNT(*) FROM assignments WHERE annotator_code=? AND status='active'",
        (annotator,)).fetchone()[0]
    return {"completed": done, "open": open_, "remaining": remaining + open_}
