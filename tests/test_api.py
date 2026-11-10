import csv
import io
import json
import zipfile

from fastapi.testclient import TestClient


def login(c, code="R01", age=30, gender="Woman"):
    r = c.post("/api/login", json={"code": code})
    if r.json().get("needs_demographics"):
        r = c.post("/api/login", json={"code": code, "age": age, "gender": gender})
    assert r.status_code == 200, r.text


def rating(var="HEX_X", score=5, expl="Talks enthusiastically to everyone"):
    return {"variable": var, "score": score, "explanation": expl, "notes": ""}


def test_video_flow_and_exports(make_client):
    c = make_client("video")
    login(c)
    cur = c.get("/api/current").json()
    assert not cur["done"]
    aid, item = cur["assignment"]["id"], cur["item"]
    # media served with range support
    r = c.get(item["media_url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100

    # cannot submit before watching
    assert c.post(f"/api/assignments/{aid}/submit", json={}).status_code == 400
    c.post(f"/api/assignments/{aid}/progress",
           json={"watched_intervals": [[0, 29.9]], "duration_s": 30})
    # cannot submit without segments or no-info explanation
    assert c.post(f"/api/assignments/{aid}/submit", json={}).status_code == 400

    # two ratings in one segment; explanation required
    bad = c.post(f"/api/assignments/{aid}/segments",
                 json={"start_s": 2, "end_s": 5, "ratings": [rating(expl=" ")]})
    assert bad.status_code == 400
    p = c.post(f"/api/assignments/{aid}/segments",
               json={"start_s": 10.2, "end_s": 18.0,
                     "ratings": [rating(), rating("HEX_H", 1, "Brags about status")]}).json()
    assert len(p["segments"]) == 1 and len(p["segments"][0]["ratings"]) == 2
    # overlapping segment allowed
    p = c.post(f"/api/assignments/{aid}/segments",
               json={"start_s": 15, "end_s": 20, "ratings": [rating(score=4)]}).json()
    assert len(p["segments"]) == 2
    assert c.post(f"/api/assignments/{aid}/submit", json={}).status_code == 200

    # second item, no-info path
    cur = c.get("/api/current").json()
    aid2 = cur["assignment"]["id"]
    c.post(f"/api/assignments/{aid2}/progress",
           json={"watched_intervals": [[0, 30]], "duration_s": 30})
    assert c.post(f"/api/assignments/{aid2}/submit",
                  json={"no_info": True, "no_info_explanation": ""}).status_code == 400
    assert c.post(f"/api/assignments/{aid2}/submit",
                  json={"no_info": True,
                        "no_info_explanation": "Only test patterns"}).status_code == 200
    assert c.get("/api/current").json()["done"] is True

    # exports
    rows = list(csv.DictReader(io.StringIO(c.get("/api/admin/export/annotations_long.csv").text)))
    assert len(rows) == 3 and {r["annotator_code"] for r in rows} == {"R01"}
    cov = list(csv.DictReader(io.StringIO(c.get("/api/admin/export/coverage_long.csv").text)))
    x = [r for r in cov if r["variable"] == "HEX_X"]
    # HEX_X covers 10.2-20 -> bins 10..19
    assert [int(r["bin_index"]) for r in x] == list(range(10, 20))
    b16 = next(r for r in x if r["bin_index"] == "16")
    assert b16["n_annotators"] == "1" and float(b16["mean_score"]) == 4.5
    peaks = list(csv.DictReader(io.StringIO(
        c.get("/api/admin/export/peak_segments.csv?threshold=1").text)))
    assert {p["variable"] for p in peaks} == {"HEX_X", "HEX_H"}
    assigns = c.get("/api/admin/export/assignments.csv").text
    assert "Only test patterns" in assigns
    ov = c.get("/api/admin/overview").json()
    assert ov["totals"]["items_complete"] == 2

    # extraction: a peak region -> zip with mp4 + json + csv
    r = c.post("/api/admin/extract", json={"mode": "peaks", "threshold": 1,
                                           "variable": "HEX_X"})
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "index.csv" in names and any(n.endswith(".mp4") for n in names)
    side = json.loads(z.read(next(n for n in names if n.endswith(".json"))))
    assert side["variable"] == "HEX_X" and len(side["ratings"]) == 2


def test_two_raters_merge(make_client, tmp_path):
    """Rater A and B on separate installations; B's export is imported into A."""
    a = make_client("text")
    b = make_client("text")
    for client, code, score in ((a, "A01", 5), (b, "B01", 2)):
        login(client, code)
        while True:
            cur = client.get("/api/current").json()
            if cur["done"]:
                break
            aid, text = cur["assignment"]["id"], cur["item"]["text"]
            client.post(f"/api/assignments/{aid}/progress",
                        json={"scrolled_to_end": True, "reading_time_s": 5})
            # select from the middle of a word: must snap to whole words
            client.post(f"/api/assignments/{aid}/segments",
                        json={"char_start": 2, "char_end": 20,
                              "ratings": [rating(score=score)]})
            assert client.post(f"/api/assignments/{aid}/submit", json={}).status_code == 200
    dump = b.get("/api/admin/export/full_export.json").content
    res = a.post("/api/admin/import", files={"file": ("full.json", dump, "application/json")})
    assert res.status_code == 200, res.text
    assert res.json()["imported"]["ratings"] == 2
    # importing twice adds nothing
    assert a.post("/api/admin/import", files={"file": ("f.json", dump)}).json()[
        "imported"]["ratings"] == 0
    rows = list(csv.DictReader(io.StringIO(a.get("/api/admin/export/annotations_long.csv").text)))
    assert {r["annotator_code"] for r in rows} == {"A01", "B01"}
    assert len({r["instance_id"] for r in rows}) == 2
    for r in rows:
        assert r["span_text"] and not r["span_text"][0].isspace()
    cov = list(csv.DictReader(io.StringIO(a.get("/api/admin/export/coverage_long.csv").text)))
    first = next(r for r in cov if r["token_index"] == "0")
    assert first["n_annotators"] == "2" and float(first["mean_score"]) == 3.5
    assert first["n_low"] == "1" and first["n_high"] == "1"
    item = rows[0]["item_id"]
    res = a.get(f"/api/admin/items/{item}/results?annotators=B01").json()
    assert {r["annotator_code"] for r in res["ratings"]} == {"B01"}
