import csv
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from conftest import SAMPLES
from personality_annotator import formats, sources
from personality_annotator.extract import ffmpeg_exe


# ------------------------------------------------------------------ text extraction

def make_docx(paragraphs):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def make_odt(paragraphs):
    ns = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" ' \
         'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", f"<office:document-content {ns}><office:body><office:text>"
                                  f"{body}</office:text></office:body></office:document-content>")
    return buf.getvalue()


def make_pdf(text):
    """A minimal one-page PDF with a text layer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>stream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_extract_text_formats():
    assert formats.extract_text(make_docx(["Hello there.", "Second line"]), "a.docx") == \
        "Hello there.\nSecond line"
    assert formats.extract_text(make_odt(["Eén", "twee"]), "a.odt") == "Eén\ntwee"
    assert "I enjoy parties" in formats.extract_text(make_pdf("I enjoy parties"), "a.pdf")
    rtf = rb"{\rtf1\ansi{\fonttbl\f0 Arial;}\f0 Caf\'e9 talk\par Next}"
    assert formats.extract_text(rtf, "a.rtf") == "Café talk\nNext"
    assert formats.extract_text(b"<p>A &amp; B</p><p>C</p>", "a.html") == "A & B\nC"
    assert formats.extract_text("café".encode("utf-8"), "a.txt") == "café"
    assert formats.extract_text("café".encode("cp1252"), "a.txt") == "café"


def test_modality_detection():
    assert formats.modality_of("x.AVI") == "video" and formats.needs_conversion("x.avi")
    assert formats.modality_of("x.mp4?download=1") == "video" and not formats.needs_conversion("x.mp4")
    assert formats.modality_of("x.wma") == "audio" and formats.needs_conversion("x.wma")
    assert formats.modality_of("x.docx") == "text"
    assert formats.modality_of("x.xyz") is None


# ------------------------------------------------------------------ conversion

@pytest.fixture
def avi_file(tmp_path):
    p = tmp_path / "clip.avi"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=3", "-f", "lavfi", "-i",
                    "sine=duration=3", "-c:v", "mpeg4", "-c:a", "mp3", "-shortest", str(p)],
                   check=True)
    return p


def test_convert_avi_to_mp4(avi_file, tmp_path):
    out = formats.convert_for_browser(avi_file, "clip", "video", tmp_path / "conv")
    assert out.suffix == ".mp4" and out.stat().st_size > 0
    again = formats.convert_for_browser(avi_file, "clip", "video", tmp_path / "conv")
    assert again == out  # cached


# ------------------------------------------------------------------ sources

def mixed_folder(tmp_path, with_avi=None):
    d = tmp_path / "mixed"
    d.mkdir()
    shutil.copy(SAMPLES / "videos" / "sample_video_01.mp4", d)
    shutil.copy(SAMPLES / "audio" / "sample_audio_01.mp3", d)
    (d / "notes.docx").write_bytes(make_docx(["I organise everything.", "Every week."]))
    (d / "ignore.xyz").write_text("x")
    (d / "~$notes.docx").write_text("office lock file")
    sub = d / "sub"
    sub.mkdir()
    (sub / "deep.txt").write_text("Deep text.")
    if with_avi:
        shutil.copy(with_avi, d)
    return d


def test_list_local_mixed(tmp_path):
    d = mixed_folder(tmp_path)
    errs = []
    specs = sources.list_local({"path": str(d)}, ("video", "audio", "text"), errs)
    assert sorted((s.item_id, s.modality) for s in specs) == [
        ("notes", "text"), ("sample_audio_01", "audio"), ("sample_video_01", "video")]
    assert specs[0].text if specs[0].modality == "text" else True
    rec = sources.list_local({"path": str(d), "recursive": True}, ("text",), errs)
    assert sorted(s.item_id for s in rec) == ["notes", "sub__deep"]
    one = sources.list_local({"path": str(d / "notes.docx")}, ("text",), errs)
    assert one[0].text == "I organise everything.\nEvery week."


def test_normalise_new_source(tmp_path):
    assert sources.normalise_new_source({"type": "url", "url": "https://zenodo.org/records/123"}) == \
        {"type": "zenodo", "record": "https://zenodo.org/records/123"}
    assert sources.normalise_new_source({"type": "url", "url": "https://x.org/a/b.mp4"}) == \
        {"type": "url", "url": "https://x.org/a/b.mp4"}
    spec = sources.normalise_new_source({"type": "local", "path": f'"{tmp_path}"', "recursive": True})
    assert spec["path"] == str(tmp_path.resolve()) and spec["recursive"]
    with pytest.raises(ValueError):
        sources.normalise_new_source({"type": "local", "path": str(tmp_path / "nope")})
    with pytest.raises(ValueError):
        sources.normalise_new_source({"type": "url", "url": "ftp://x"})


def test_list_url_with_mock():
    def handler(request):
        return httpx.Response(200, content=make_docx(["Remote words"]))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    errs = []
    v = sources.list_url({"url": "https://x.org/media/My%20Talk.mp4"}, ("video",), client, errs)
    assert v[0].item_id == "My_Talk" and v[0].modality == "video"
    t = sources.list_url({"url": "https://x.org/t/doc.docx"}, ("text",), client, errs)
    assert t[0].text == "Remote words"
    with pytest.raises(ValueError, match="file type"):
        sources.list_url({"url": "https://x.org/watch?v=1"}, ("video",), client, errs)
    with pytest.raises(ValueError, match="only uses"):
        sources.list_url({"url": "https://x.org/a.mp3"}, ("video",), client, errs)


# ------------------------------------------------------------------ API: choose mode + add files

def login(c, code):
    c.post("/api/login", json={"code": code, "age": 30, "gender": "Woman"})


def test_choose_mode_add_files_and_mixed_exports(make_client, tmp_path, avi_file):
    d = mixed_folder(tmp_path, with_avi=avi_file)
    c = make_client("any", k=1, sources=[])
    login(c, "R01")
    assert c.get("/api/items").json()["items"] == []
    r = c.post("/api/sources", json={"type": "local", "path": str(d)})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["added"] == 4      # mp4, mp3, docx, avi
    # adding the same folder again adds nothing new
    assert c.post("/api/sources", json={"type": "local", "path": str(d)}).json()["report"]["added"] == 0
    bad = c.post("/api/sources", json={"type": "local", "path": str(tmp_path / "missing")})
    assert bad.status_code == 400

    items = {i["item_id"]: i for i in c.get("/api/items").json()["items"]}
    assert {i["modality"] for i in items.values()} == {"video", "audio", "text"}
    assert all(i["status"] == "new" for i in items.values())

    # the AVI is converted on request
    r = c.get("/media/clip")
    assert r.status_code == 200 and r.headers["content-type"].startswith("video/mp4")

    # open + annotate the text item
    p = c.post("/api/items/notes/open", json={}).json()
    assert p["item"]["modality"] == "text" and "I organise" in p["item"]["text"]
    aid = p["assignment"]["id"]
    c.post(f"/api/assignments/{aid}/progress", json={"scrolled_to_end": True, "reading_time_s": 3})
    c.post(f"/api/assignments/{aid}/segments", json={"char_start": 0, "char_end": 10, "ratings": [
        {"variable": "HEX_C", "score": 5, "explanation": "Organises"}]})
    assert c.post(f"/api/assignments/{aid}/submit", json={}).status_code == 200
    # and the video
    p = c.post("/api/items/sample_video_01/open", json={}).json()
    vid = p["assignment"]["id"]
    c.post(f"/api/assignments/{vid}/progress", json={"watched_intervals": [[0, 30]], "duration_s": 30})
    c.post(f"/api/assignments/{vid}/segments", json={"start_s": 1, "end_s": 3, "ratings": [
        {"variable": "HEX_X", "score": 4, "explanation": "Lively"}]})
    assert c.post(f"/api/assignments/{vid}/submit", json={}).status_code == 200

    items = {i["item_id"]: i for i in c.get("/api/items").json()["items"]}
    assert items["notes"]["status"] == "completed" and items["sample_audio_01"]["status"] == "new"
    assert c.post("/api/items/notes/open", json={}).status_code == 409

    # second annotator: k=1, so the completed items are full
    c2 = c.__class__(c.app)
    login(c2, "R02")
    items2 = {i["item_id"]: i for i in c2.get("/api/items").json()["items"]}
    assert items2["notes"]["status"] == "full"
    assert c2.post("/api/items/notes/open", json={}).status_code == 409

    # mixed exports
    cov = list(csv.DictReader(io.StringIO(c.get("/api/admin/export/coverage_long.csv").text)))
    assert {r["modality"] for r in cov} == {"text", "video"}
    assert "bin_start_s" in cov[0] and "token" in cov[0]
    peaks = list(csv.DictReader(io.StringIO(c.get("/api/admin/export/peak_segments.csv").text)))
    by_mod = {r["modality"]: r for r in peaks}
    assert by_mod["text"]["start_char"] == "0" and by_mod["video"]["start_s"] == "1.0"
    z = c.post("/api/admin/extract", json={"mode": "peaks", "threshold": 1})
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert any(n.endswith(".txt") for n in names) and any(n.endswith(".mp4") for n in names)

    # remove the added source: its items disappear from the list, data stays
    src_id = c.get("/api/sources").json()["sources"][0]["id"]
    assert c.delete(f"/api/sources/{src_id}").status_code == 200
    assert c.get("/api/items").json()["items"] == []
    rows = list(csv.DictReader(io.StringIO(c.get("/api/admin/export/annotations_long.csv").text)))
    assert len(rows) == 2


def test_add_files_can_be_switched_off(make_client):
    c = make_client("video", annotators_can_add_files=False)
    login(c, "R01")
    r = c.post("/api/sources", json={"type": "url", "url": "https://x.org/a.mp4"})
    assert r.status_code == 403


def test_pick_dialog_reports_problem(make_client, monkeypatch):
    c = make_client("video")
    login(c, "R01")
    from personality_annotator import picker

    def boom(kind):
        raise RuntimeError("no display")
    monkeypatch.setattr(picker, "pick", boom)
    r = c.post("/api/pick", json={"kind": "folder"})
    assert r.status_code == 500 and "paste the path" in r.json()["detail"]
