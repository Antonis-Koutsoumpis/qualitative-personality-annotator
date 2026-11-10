import random
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from personality_annotator import analysis, assignment, sources
from personality_annotator.db import Database, now_iso


def setup_db(tmp_path, n_items=10, n_annotators=6):
    db = Database(tmp_path / "t.sqlite")
    with db.connect() as con:
        for i in range(n_items):
            con.execute("INSERT INTO items(item_id, modality, source_type, location, added_at) "
                        "VALUES (?, 'video', 'local', 'x', ?)", (f"item{i:02d}", now_iso()))
        for a in range(n_annotators):
            con.execute("INSERT INTO annotators(code, created_at) VALUES (?, ?)",
                        (f"R{a}", now_iso()))
    return db


def complete(db, aid):
    with db.connect() as con:
        con.execute("UPDATE assignments SET status='completed', completed_at=? WHERE id=?",
                    (now_iso(), aid))


def test_even_spread_no_repeats_and_k(tmp_path):
    k, n_items, n_ann = 3, 10, 6
    db = setup_db(tmp_path, n_items, n_ann)
    rng = random.Random(1)
    done = {f"R{a}": False for a in range(n_ann)}
    while not all(done.values()):
        for ann in done:
            if done[ann]:
                continue
            aid = assignment.current_or_next(db, ann, k, 48, rng)
            if aid is None:
                done[ann] = True
            else:
                complete(db, aid)
    with db.connect() as con:
        rows = con.execute("SELECT annotator_code, item_id FROM assignments").fetchall()
    pairs = [(r[0], r[1]) for r in rows]
    assert len(pairs) == len(set(pairs)), "an annotator got the same item twice"
    per_item = Counter(r[1] for r in rows)
    assert set(per_item.values()) == {k}, per_item  # every item exactly k times
    assert len(rows) == n_items * k


def test_evenness_during_collection(tmp_path):
    """While items are being filled, counts never differ by more than 1."""
    db = setup_db(tmp_path, 8, 4)
    rng = random.Random(3)
    for rnd in range(2):
        for ann in ("R0", "R1", "R2", "R3"):
            complete(db, assignment.current_or_next(db, ann, 4, 48, rng))
        with db.connect() as con:
            counts = assignment.item_counts(con, 48).values()
        assert max(counts) - min(counts) <= 1


def test_resume_open_assignment(tmp_path):
    db = setup_db(tmp_path, 5, 1)
    a1 = assignment.current_or_next(db, "R0", 1, 48)
    a2 = assignment.current_or_next(db, "R0", 1, 48)
    assert a1 == a2


def test_expired_reservation_released(tmp_path):
    db = setup_db(tmp_path, 1, 2)
    a0 = assignment.current_or_next(db, "R0", 1, 48)
    # R1 cannot get the only item while R0 holds it
    assert assignment.current_or_next(db, "R1", 1, 48) is None
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(timespec="seconds")
    with db.connect() as con:
        con.execute("UPDATE assignments SET last_activity_at=? WHERE id=?", (old, a0))
    a1 = assignment.current_or_next(db, "R1", 1, 48)
    assert a1 is not None
    complete(db, a1)
    # R0 comes back: item is now full, so R0's stale reservation is released
    assert assignment.current_or_next(db, "R0", 1, 48) is None
    with db.connect() as con:
        assert con.execute("SELECT status FROM assignments WHERE id=?",
                           (a0,)).fetchone()[0] == "released"


def test_concurrent_requests_never_exceed_k(tmp_path):
    db = setup_db(tmp_path, 3, 12)
    results = []

    def worker(ann):
        results.append(assignment.current_or_next(db, ann, 2, 48))

    threads = [threading.Thread(target=worker, args=(f"R{i}",)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.connect() as con:
        per_item = Counter(r[0] for r in con.execute("SELECT item_id FROM assignments"))
    assert max(per_item.values()) <= 2
    assert sum(1 for r in results if r) == 6


# ------------------------------------------------------------------ analysis

def R(ann, s, e, var="X", score=5):
    return {"annotator": ann, "start_s": s, "end_s": e, "variable": var, "score": score}


def test_time_coverage_counts_annotators_once():
    ratings = [R("a", 0.5, 2.5, score=5), R("a", 2.0, 3.0, score=3), R("b", 2.2, 2.4, score=1)]
    cov = {r["bin_index"]: r for r in analysis.time_coverage(ratings, 4, 1.0, 3)}
    assert cov[0]["n_annotators"] == 1 and cov[1]["n_annotators"] == 1
    assert cov[2]["n_annotators"] == 2       # a (two overlapping segments) counted once
    assert cov[2]["mean_score"] == 2.5       # a: mean(5,3)=4, b: 1 -> 2.5
    assert cov[2]["n_low"] == 1 and cov[2]["n_high"] == 1
    assert 3 not in cov                      # end 3.0 is exclusive


def test_time_coverage_include_empty_and_point_segments():
    cov = analysis.time_coverage([R("a", 1.5, 1.5)], 3, 1.0, 3, ["X", "Y"], include_empty=True)
    assert len(cov) == 6
    assert [r["n_annotators"] for r in cov if r["variable"] == "X"] == [0, 1, 0]


def test_peaks_merge_contiguous_bins():
    ratings = [R("a", 0, 3), R("b", 1, 5), R("c", 10, 11)]
    cov = analysis.time_coverage(ratings, 12, 1.0, 3)
    p2 = analysis.peaks(cov, ratings, 2, "video")
    assert [(p["start"], p["end"]) for p in p2] == [(1.0, 3.0)]
    p1 = analysis.peaks(cov, ratings, 1, "video")
    assert [(p["start"], p["end"]) for p in p1] == [(0.0, 5.0), (10.0, 11.0)]


def test_text_snap_and_coverage():
    text = "Hello there, dear world.\n\nSecond   paragraph here."
    s, e = analysis.snap_to_words(text, 2, 9)            # "llo the"
    assert text[s:e] == "Hello there,"
    s, e = analysis.snap_to_words(text, text.index("Second") + 1, text.index("paragraph") + 2)
    assert text[s:e] == "Second   paragraph"
    rat = [{"annotator": "a", "char_start": 0, "char_end": 12, "variable": "X", "score": 4}]
    cov = analysis.text_coverage(rat, text, 3)
    assert [r["token"] for r in cov] == ["Hello", "there,"]
    for r in cov:
        assert text[r["char_start"]:r["char_end"]] == r["token"]


def test_text_normalisation_keeps_offsets_stable():
    win = "﻿Line one\r\nLine two\rLine three"
    norm = sources.normalize_text(win)
    assert norm == "Line one\nLine two\nLine three"
    assert sources.text_hash(norm) == sources.text_hash(sources.normalize_text(norm))


# ------------------------------------------------------------------ zenodo

def test_parse_zenodo_record():
    for ref in ("https://zenodo.org/records/123456", "https://zenodo.org/record/123456",
                "10.5281/zenodo.123456", "https://doi.org/10.5281/zenodo.123456", "123456"):
        assert sources.parse_zenodo_record(ref) == "123456"
    with pytest.raises(ValueError):
        sources.parse_zenodo_record("https://example.org/foo")


def test_list_zenodo_with_mock_api():
    def handler(request: httpx.Request):
        if request.url.path == "/api/records/42":
            return httpx.Response(200, json={"files": [
                {"key": "clip one.mp4", "size": 10},
                {"key": "notes.txt", "size": 5},
                {"key": "clip2.webm", "size": 7}]})
        if request.url.path.startswith("/records/42/files/notes.txt"):
            return httpx.Response(200, content="a\r\nb".encode())
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    vids = sources.list_zenodo({"record": "https://zenodo.org/records/42"}, "video", client)
    assert [v.item_id for v in vids] == ["clip_one", "clip2"]
    assert vids[0].location == "https://zenodo.org/records/42/files/clip%20one.mp4?download=1"
    txt = sources.list_zenodo({"record": "42"}, "text", client)
    assert txt[0].text == "a\nb"
