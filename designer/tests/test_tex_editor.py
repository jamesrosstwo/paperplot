"""Tests for the section editor (whole .tex files) and synctex forward search.

Backend: latex.tex_sections / read_tex_file / write_tex_file / forward and the endpoints GET /api/tex/sections,
GET/PUT /api/tex/file, POST /api/synctex/forward. Frontend wiring: static checks on index.html / app.js.

Writes happen only in a synthetic paper made in a temp dir (config.PAPER_ROOT is monkeypatched to it); the scratch
clone is used read-only for synctex forward (needs its compiled paper.pdf + paper.synctex.gz; skipped otherwise).

Run:  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe -m pytest designer/tests/test_tex_editor.py -q
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PAPERPLOT_ROOT = HERE.parent.parent
if str(PAPERPLOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PAPERPLOT_ROOT))

from designer import config, latex, server  # noqa: E402

LIVE = Path(config.LIVE_PAPER_WINDOWS).resolve()
assert config.PAPER_ROOT.resolve() != LIVE, "refusing to test against the live Overleaf clone"
STATIC = PAPERPLOT_ROOT / "designer" / "static"
HOST = "127.0.0.1:8768"

MAIN = (b"\\documentclass{article}\r\n\\title{A {Test} Paper}\r\n\\begin{document}\r\n\\maketitle\r\n"
        b"\\input{sections/00_abstract}\r\n\\input{sections/01_intro}\r\n% \\input{sections/commented_out}\r\n"
        b"\\include{sections/02_method}\r\n\\end{document}\r\n")
ABSTRACT = b"\\begin{abstract}\nShort.\n\\end{abstract}\n"
INTRO = "\\section{Intro\\-duction}\r\nCaf\u00e9 text.\r\n\\input{sections/01a_sub}\r\n".encode("utf-8")
SUB = b"\\subsection{Sub part}\nnested\n"
METHOD = b"\xef\xbb\xbf\\section[short]{Method \\& More}\nno newline at end"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def paper(tmp_path, monkeypatch):
    root = (tmp_path / "paper").resolve()
    (root / "sections").mkdir(parents=True)
    (root / "paper.tex").write_bytes(MAIN)
    (root / "sections" / "00_abstract.tex").write_bytes(ABSTRACT)
    (root / "sections" / "01_intro.tex").write_bytes(INTRO)
    (root / "sections" / "01a_sub.tex").write_bytes(SUB)
    (root / "sections" / "02_method.tex").write_bytes(METHOD)
    (root / "sections" / "commented_out.tex").write_bytes(b"\\section{Hidden}\n")
    (root / "sections" / "notes.txt").write_bytes(b"not tex\n")
    (root / "orphan.tex").write_bytes(b"\\section{Orphan}\n")
    monkeypatch.setattr(config, "PAPER_ROOT", root)
    monkeypatch.setattr(config, "TOKEN", None)
    monkeypatch.setattr(config, "READONLY", False)
    return root


@pytest.fixture()
def client(paper):
    app = server.create_app()
    app.testing = True
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = HOST
    c.environ_base["HTTP_X_DESIGNER"] = "1"
    return c


# ============================================================================================ latex module
def test_sections_in_document_order_with_headings(paper):
    secs = latex.tex_sections()
    assert [s["path"] for s in secs] == ["paper.tex", "sections/00_abstract.tex", "sections/01_intro.tex",
                                         "sections/01a_sub.tex", "sections/02_method.tex"]
    by = {s["path"]: s for s in secs}
    assert by["paper.tex"]["main"] is True and by["paper.tex"]["depth"] == 0
    assert by["paper.tex"]["title"] == "A Test Paper"
    assert by["sections/00_abstract.tex"]["heading"] == "Abstract"
    assert by["sections/01_intro.tex"]["title"] == "Introduction"
    assert by["sections/01a_sub.tex"]["depth"] == 2 and by["sections/01a_sub.tex"]["title"] == "Sub part"
    assert by["sections/02_method.tex"]["title"] == "Method & More"   # \& -> &, [short] skipped
    assert by["sections/01_intro.tex"]["lines"] == 3
    assert by["sections/01_intro.tex"]["hash"] == sha(INTRO)


def test_read_keeps_text_exact(paper):
    r = latex.read_tex_file("sections/01_intro.tex")
    assert r["text"] == "\\section{Intro\\-duction}\nCaf\u00e9 text.\n\\input{sections/01a_sub}\n"
    assert r["newline"] == "\r\n" and r["editable"] is True and r["hash"] == sha(INTRO)
    m = latex.read_tex_file("sections\\02_method.tex")         # backslashes accepted, canonical path back
    assert m["path"] == "sections/02_method.tex" and m["bom"] is True and not m["text"].startswith("\ufeff")


@pytest.mark.parametrize("bad", ["../paper.tex", "sections/../../x.tex", "/etc/passwd", "C:/Windows/win.ini",
                                 "C:\\Windows\\win.ini", "sections/notes.txt", "orphan.tex",
                                 "sections/commented_out.tex", "sections/nope.tex", "paper.tex\x00.tex"])
def test_paths_outside_the_paper_are_refused(paper, bad):
    with pytest.raises(PermissionError):
        latex.read_tex_file(bad)
    with pytest.raises(PermissionError):
        latex.write_tex_file(bad, "x", "0" * 64)


def test_symlink_out_of_root_refused(paper, tmp_path):
    outside = tmp_path / "outside.tex"
    outside.write_bytes(b"\\section{Outside}\n")
    link = paper / "sections" / "linked.tex"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    (paper / "paper.tex").write_bytes(MAIN.replace(b"\\end{document}", b"\\input{sections/linked}\r\n\\end{document}"))
    assert "sections/linked.tex" not in [s["path"] for s in latex.tex_sections()]
    with pytest.raises(PermissionError):
        latex.read_tex_file("sections/linked.tex")


def test_write_round_trip_keeps_crlf_and_bom(paper):
    r = latex.read_tex_file("sections/01_intro.tex")
    new = r["text"].replace("Caf\u00e9 text.", "Caf\u00e9 text, edited.\nSecond line.")
    w = latex.write_tex_file("sections/01_intro.tex", new, r["hash"])
    raw = (paper / "sections" / "01_intro.tex").read_bytes()
    assert raw == "\\section{Intro\\-duction}\r\nCaf\u00e9 text, edited.\r\nSecond line.\r\n\\input{sections/01a_sub}\r\n".encode()
    assert w["hash"] == sha(raw) and w["changed"] is True and w["lines"] == 4
    m = latex.read_tex_file("sections/02_method.tex")
    latex.write_tex_file("sections/02_method.tex", m["text"] + " more", m["hash"])
    raw = (paper / "sections" / "02_method.tex").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf\\section") and raw.endswith(b"no newline at end more") and b"\r" not in raw
    # no temp files left behind
    assert sorted(p.name for p in (paper / "sections").iterdir() if p.name.startswith(".")) == []


def test_unchanged_write_is_a_no_op(paper):
    r = latex.read_tex_file("sections/01_intro.tex")
    before = (paper / "sections" / "01_intro.tex").stat().st_mtime_ns
    w = latex.write_tex_file("sections/01_intro.tex", r["text"], r["hash"])
    assert w["changed"] is False and w["hash"] == r["hash"]
    assert (paper / "sections" / "01_intro.tex").stat().st_mtime_ns == before


def test_stale_write_refused(paper):
    r = latex.read_tex_file("sections/01_intro.tex")
    p = paper / "sections" / "01_intro.tex"
    p.write_bytes(p.read_bytes() + b"% from Overleaf\r\n")
    with pytest.raises(latex.StaleError):
        latex.write_tex_file("sections/01_intro.tex", "mine\n", r["hash"])
    assert p.read_bytes().endswith(b"% from Overleaf\r\n")      # untouched
    with pytest.raises(ValueError):
        latex.write_tex_file("sections/01_intro.tex", "mine\n", "")


def test_non_utf8_file_is_read_only(paper):
    p = paper / "sections" / "00_abstract.tex"
    p.write_bytes(b"\\begin{abstract}\nlatin-1 \xe9\n\\end{abstract}\n")
    r = latex.read_tex_file("sections/00_abstract.tex")
    assert r["editable"] is False and "UTF-8" in r["warning"]
    with pytest.raises(ValueError):
        latex.write_tex_file("sections/00_abstract.tex", r["text"], r["hash"])


# ============================================================================================ endpoints
def test_sections_endpoint(client):
    r = client.get("/api/tex/sections")
    assert r.status_code == 200
    body = r.get_json()
    assert body["main_tex"] == "paper.tex" and body["sections"][0]["path"] == "paper.tex"


def test_file_get_put_and_conflict(client, paper):
    r = client.get("/api/tex/file?path=sections/01_intro.tex")
    assert r.status_code == 200
    f = r.get_json()
    r = client.put("/api/tex/file", json={"path": f["path"], "text": f["text"] + "% added\n", "base_hash": f["hash"]})
    assert r.status_code == 200, r.data
    ok = r.get_json()
    assert ok["ok"] is True and ok["hash"] == sha((paper / "sections" / "01_intro.tex").read_bytes())
    # a second save with the old hash: 409 with the current content, file unchanged
    before = (paper / "sections" / "01_intro.tex").read_bytes()
    r = client.put("/api/tex/file", json={"path": f["path"], "text": "clobber\n", "base_hash": f["hash"]})
    assert r.status_code == 409
    j = r.get_json()
    assert j["error"] == "stale" and j["current"]["hash"] == ok["hash"] and j["current"]["text"].endswith("% added\n")
    assert (paper / "sections" / "01_intro.tex").read_bytes() == before
    # "Keep mine" = an explicit save against the hash the conflict reported
    r = client.put("/api/tex/file", json={"path": f["path"], "text": "mine\n", "base_hash": j["current"]["hash"]})
    assert r.status_code == 200 and (paper / "sections" / "01_intro.tex").read_bytes() == b"mine\r\n"


@pytest.mark.parametrize("body,status", [
    ({"path": "sections/01_intro.tex", "text": "x"}, 400),                     # no base_hash
    ({"path": "sections/01_intro.tex", "base_hash": "ab"}, 400),               # no text
    ({"text": "x", "base_hash": "ab"}, 400),                                   # no path
    ({"path": "../x.tex", "text": "x", "base_hash": "ab"}, 403),
    ({"path": "orphan.tex", "text": "x", "base_hash": "ab"}, 403),
    ({"path": "sections/notes.txt", "text": "x", "base_hash": "ab"}, 403),
])
def test_put_validation(client, body, status):
    assert client.put("/api/tex/file", json=body).status_code == status


def test_get_validation(client):
    assert client.get("/api/tex/file").status_code == 400
    assert client.get("/api/tex/file?path=../../secret.tex").status_code == 403
    assert client.get("/api/tex/file?path=orphan.tex").status_code == 403


def test_put_readonly_403(client, paper, monkeypatch):
    monkeypatch.setattr(config, "READONLY", True)
    f = client.get("/api/tex/file?path=sections/01_intro.tex").get_json()       # reading still works
    r = client.put("/api/tex/file", json={"path": f["path"], "text": "x\n", "base_hash": f["hash"]})
    assert r.status_code == 403 and r.get_json()["error"] == "read-only"
    assert (paper / "sections" / "01_intro.tex").read_bytes() == INTRO
    # forward search is allowed read-only (it writes nothing)
    assert client.post("/api/synctex/forward", json={"file": "sections/01_intro.tex", "line": 1}).status_code == 200


def test_put_guards_origin_and_token(paper, monkeypatch):
    app = server.create_app()
    app.testing = True
    c = app.test_client()
    f = latex.read_tex_file("sections/01_intro.tex")
    body = {"path": f["path"], "text": "x\n", "base_hash": f["hash"]}
    r = c.put("/api/tex/file", json=body, headers={"Host": HOST, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c.put("/api/tex/file", data="{}", headers={"Host": HOST, "Content-Type": "text/plain"})
    assert r.status_code == 403                                # simple (CSRF-able) request refused
    monkeypatch.setattr(config, "TOKEN", "tok-" + "x9")
    assert c.get("/api/tex/sections", headers={"Host": HOST}).status_code == 401
    assert c.put("/api/tex/file", json=body, headers={"Host": HOST}).status_code == 401
    assert c.get("/api/tex/sections", headers={"Host": HOST, "Authorization": "Bearer tok-x9"}).status_code == 200
    assert (paper / "sections" / "01_intro.tex").read_bytes() == INTRO


def test_forward_without_pdf_is_null(client):
    r = client.post("/api/synctex/forward", json={"file": "sections/01_intro.tex", "line": 2})
    assert r.status_code == 200 and r.get_json()["page"] is None
    assert client.post("/api/synctex/forward", json={"file": "sections/01_intro.tex"}).status_code == 400
    assert client.post("/api/synctex/forward", json={"file": "../x.tex", "line": 1}).status_code == 403


def _scratch_with_synctex():
    root = Path(os.environ.get("DESIGNER_TEST_PAPER", ""))
    if not root.is_dir() or root.resolve() == LIVE or not (root / "paper.synctex.gz").is_file():
        pytest.skip("no compiled scratch paper with synctex")
    if not (root / "sections" / "03_method.tex").is_file():
        pytest.skip("scratch paper has no sections/03_method.tex")
    return root.resolve()


def test_forward_on_compiled_scratch_paper(monkeypatch):
    root = _scratch_with_synctex()
    monkeypatch.setattr(config, "PAPER_ROOT", root)
    r = latex.forward("sections/03_method.tex", 1)            # the \section line
    assert r is not None and r["page"] >= 1 and r["w"] > 0 and r["h"] > 0 and r["file"] == "sections/03_method.tex"
    assert 0 <= r["x"] <= (r["page_w"] or 1000) and 0 <= r["y"] <= (r["page_h"] or 1000)
    # a blank line falls back to a nearby line that has output
    r2 = latex.forward("sections/03_method.tex", 2)
    assert r2 is not None and abs(r2["line"] - 2) <= 40
    app = server.create_app()
    app.testing = True
    j = app.test_client().post("/api/synctex/forward", json={"file": "sections/03_method.tex", "line": 1},
                               headers={"Host": HOST}).get_json()
    assert j["page"] == r["page"] and set(j["box"]) == {"x", "y", "w", "h"}


# ============================================================================================ frontend wiring
def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_layout_left_editor_and_hamburger():
    html = _read("index.html")
    assert 'id="editor-panel"' not in html, "the bottom TeX panel was moved to the left"
    left = html[html.index('<aside id="left"'):html.index("</aside>", html.index('<aside id="left"'))]
    for i in ("editor-host", "tex-save", "tex-recompile", "tex-show", "tex-find", "tex-wrap", "editor-dirty"):
        assert f'id="{i}"' in left, i
    drawer = html[html.index('<nav id="nav-drawer"'):html.index("</nav>")]
    for i in ("section-list", "thumbs", "fig-list"):
        assert f'id="{i}"' in drawer, i
    assert html.index('id="btn-menu"') < html.index('class="brand"')       # hamburger at the top left


def test_editor_keys_and_conflict_flow():
    app = _read("app.js")
    for k in ("'Ctrl-S'", "'Ctrl-Enter'", "'Ctrl-J'", "'Ctrl-F': 'findPersistent'"):
        assert k in app, k
    assert "cm.on('mousedown'" in app and "e.ctrlKey || e.metaKey" in app     # Ctrl+click -> show on page
    body = app[app.index("async function resolveConflict"):app.index("async function saveTexAndRecompile")]
    # "Keep mine" overwrites only after an explicit confirm, and still as compare-and-swap on the disk hash
    assert body.index("Overwrite the file on disk?") < body.index("putSection(path, mine, current.hash")
    assert "if (!ok) return false;" in body
    assert "confirmLeaveSection" in app and "beforeunload" in app
    assert "e.defaultPrevented" in app        # CodeMirror-handled keys are not handled twice (one save, one compile)


def test_default_zoom_and_no_auto_zoom_kept():
    pv = _read("pdfview.js")
    assert "DEFAULT_SCALE = 1.5 * 96 / 72" in pv
    sel = _read("app.js")
    sel = sel[sel.index("async function selectFigure"):sel.index("async function selectSpec")]
    assert "zoom: true" not in sel and "setScale" not in sel and "fitWidth" not in sel
