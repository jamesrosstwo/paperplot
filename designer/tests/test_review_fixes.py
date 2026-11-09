"""Fixes from the 2026-11-09 review: spec compare-and-swap saves, one server per port, line-end preservation in the
section editor, synctex forward bounds, seeding of pre-marker state dirs, and frontend wiring (static checks).

Writes only into temp dirs (the per-run state dir from conftest, tmp_path papers); the scratch clone is read-only.

Run:  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe -m pytest designer/tests/test_review_fixes.py -q
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PAPERPLOT_ROOT = HERE.parent.parent
if str(PAPERPLOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PAPERPLOT_ROOT))

from designer import config, latex, server, spec  # noqa: E402

from designer.live_papers import is_live  # noqa: E402
assert not is_live(config.PAPER_ROOT), "refusing to test against the live Overleaf clone"
STATIC = PAPERPLOT_ROOT / "designer" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _client():
    app = server.create_app()
    app.testing = True
    c = app.test_client()
    c.environ_base["HTTP_X_DESIGNER"] = "1"
    return c


@pytest.fixture()
def tmp_spec():
    sid = "review_cas_tmp"
    try:
        spec.delete(sid)
    except FileNotFoundError:
        pass
    spec.create(sid)
    yield sid
    try:
        spec.delete(sid)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ spec compare-and-swap
def test_spec_save_is_compare_and_swap(tmp_spec):
    p = config.SPECS_DIR / f"{tmp_spec}.yaml"
    h0 = spec.file_hash(tmp_spec)
    assert h0 == hashlib.sha256(p.read_bytes()).hexdigest()
    s = spec.load(tmp_spec)
    s["style"]["width_pt"] = 300
    h1 = spec.save(s, base_hash=h0)
    assert h1 == spec.file_hash(tmp_spec) and h1 != h0
    # a hand edit on disk, then a save from the stale version: refused, nothing written
    p.write_text(p.read_text(encoding="utf-8").replace("width_pt: 300", "width_pt: 310"), encoding="utf-8")
    edited = p.read_bytes()
    s["style"]["width_pt"] = 320
    with pytest.raises(spec.StaleError) as ei:
        spec.save(s, base_hash=h1)
    assert ei.value.current_hash == hashlib.sha256(edited).hexdigest()
    assert p.read_bytes() == edited
    # an unconditional save (scripts, old clients) still works; an unchanged save leaves the file alone
    h2 = spec.save(s)
    mtime = p.stat().st_mtime_ns
    assert spec.save(s, base_hash=h2) == h2 and p.stat().st_mtime_ns == mtime


def test_spec_put_409_with_current_spec(tmp_spec):
    c = _client()
    r = c.get(f"/api/specs/{tmp_spec}")
    assert r.status_code == 200
    h0 = r.headers.get("X-Spec-Hash")
    assert h0 and re.fullmatch(r"[0-9a-f]{64}", h0)
    s = r.get_json()
    s["style"]["width_pt"] = 290
    r = c.put(f"/api/specs/{tmp_spec}", json=s, headers={"X-Spec-Base-Hash": h0})
    assert r.status_code == 200
    h1 = r.get_json()["hash"]
    assert h1 == spec.file_hash(tmp_spec) and r.headers.get("X-Spec-Hash") == h1
    # the tab that still holds h0 cannot overwrite the newer file
    s["style"]["width_pt"] = 200
    r = c.put(f"/api/specs/{tmp_spec}", json=s, headers={"X-Spec-Base-Hash": h0})
    assert r.status_code == 409
    j = r.get_json()
    assert j["error"] == "stale" and j["hash"] == h1 and j["current"]["style"]["width_pt"] == 290
    assert spec.load(tmp_spec)["style"]["width_pt"] == 290
    # keep mine = retry against the hash just reported
    r = c.put(f"/api/specs/{tmp_spec}", json=s, headers={"X-Spec-Base-Hash": j["hash"]})
    assert r.status_code == 200 and spec.load(tmp_spec)["style"]["width_pt"] == 200
    assert c.put(f"/api/specs/{tmp_spec}", json=s, headers={"X-Spec-Base-Hash": "nothex"}).status_code == 400
    assert c.put(f"/api/specs/{tmp_spec}", json=s).status_code == 200          # no header: unconditional


def test_render_with_posted_spec_respects_base_hash(tmp_spec):
    c = _client()
    s = spec.load(tmp_spec)
    r = c.post(f"/api/specs/{tmp_spec}/render", json=s, headers={"X-Spec-Base-Hash": "0" * 64})
    assert r.status_code == 409          # refused before anything is rendered or saved


# ------------------------------------------------------------------ seeding
def test_ensure_seeds_marks_pre_marker_dir_without_adding(tmp_path, monkeypatch):
    seeds = sorted(spec.SEED_DIR.glob("*.yaml"))
    assert len(seeds) >= 2
    old = tmp_path / "old_specs"
    old.mkdir()
    (old / seeds[0].name).write_bytes(seeds[0].read_bytes())        # seeded once, before .seeded existed
    monkeypatch.setattr(config, "SPECS_DIR", old)
    spec.ensure_seeds()
    assert (old / ".seeded").exists()
    assert sorted(p.name for p in old.glob("*.yaml")) == [seeds[0].name]
    fresh = tmp_path / "fresh_specs"
    (fresh).mkdir()
    (fresh / "my_own.yaml").write_text("id: my_own\n", encoding="utf-8")   # a user's spec, no seed names
    monkeypatch.setattr(config, "SPECS_DIR", fresh)
    spec.ensure_seeds()
    assert {p.name for p in seeds} <= {p.name for p in fresh.glob("*.yaml")}


def test_seed_yaml_has_no_anchors():
    for f in spec.SEED_DIR.glob("*.yaml"):
        t = f.read_text(encoding="utf-8")
        assert not re.search(r"[&*]id\d+", t), f"{f.name} has YAML anchors/aliases"


# ------------------------------------------------------------------ one server per port
def test_check_port_free_refuses_a_served_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        with pytest.raises(SystemExit) as ei:
            server.check_port_free("127.0.0.1", port)
        assert "already in use" in str(ei.value)
    server.check_port_free("127.0.0.1", port)       # closed again: no error


def test_exclusive_server_cannot_share_a_listening_port():
    """Windows lets a SO_REUSEADDR socket bind a port another socket is LISTENING on; ours must not."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # like werkzeug's default server socket
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        cls = server.exclusive_server_class()
        assert cls.allow_reuse_address is (os.name != "nt")
        with pytest.raises(SystemExit):           # werkzeug reports the bind error and exits
            srv = cls("127.0.0.1", port, lambda env, sr: [])
            srv.server_close()


# ------------------------------------------------------------------ section editor line ends
@pytest.mark.parametrize("old,new,want", [
    ("a\r\nb\nc\nd\n", "a\nb\nc\nd\n", "a\r\nb\nc\nd\n"),             # mixed, unchanged: byte-identical
    ("a\rb\nc\n", "a\nb\nc\n", "a\rb\nc\n"),                           # lone CR kept
    ("x\r\ny\r\n", "x\ny\n", "x\r\ny\r\n"),
    ("a\r\nb\nc\nd\n", "a\nB\nc\nd\nnew\n", "a\r\nB\nc\nd\nnew\n"),     # edit keeps that line's end; new = majority
    ("p\r\nq\r\nr\n", "p\nq2\nr\nadd", "p\r\nq2\r\nr\nadd"),
    ("only", "only\n", "only\n"),
    ("", "", ""),
])
def test_restore_line_ends(old, new, want):
    assert latex._restore_line_ends(old, new) == want


def test_write_tex_file_keeps_mixed_endings(tmp_path, monkeypatch):
    root = (tmp_path / "paper").resolve()
    (root / "sections").mkdir(parents=True)
    (root / "paper.tex").write_bytes(b"\\documentclass{article}\n\\begin{document}\n\\input{sections/mixed}\n"
                                     b"\\end{document}\n")
    raw = b"\\section{Mixed}\r\nb\nc\rd\n"
    (root / "sections" / "mixed.tex").write_bytes(raw)
    monkeypatch.setattr(config, "PAPER_ROOT", root)
    r = latex.read_tex_file("sections/mixed.tex")
    w = latex.write_tex_file("sections/mixed.tex", r["text"].replace("\r", "\n"), r["hash"])
    assert w["changed"] is False and (root / "sections" / "mixed.tex").read_bytes() == raw
    r = latex.read_tex_file("sections/mixed.tex")
    text = r["text"].replace("\r", "\n").replace("\nb\n", "\nB\n")
    latex.write_tex_file("sections/mixed.tex", text, r["hash"])
    assert (root / "sections" / "mixed.tex").read_bytes() == b"\\section{Mixed}\r\nB\nc\rd\n"


def test_reachable_files_cache_sees_new_inputs(tmp_path, monkeypatch):
    root = (tmp_path / "paper").resolve()
    (root / "sections").mkdir(parents=True)
    (root / "paper.tex").write_bytes(b"\\begin{document}\n\\input{sections/a}\n\\input{sections/b}\n\\end{document}\n")
    (root / "sections" / "a.tex").write_bytes(b"\\section{A}\n")
    monkeypatch.setattr(config, "PAPER_ROOT", root)
    assert latex._reachable_files(root) == ["paper.tex", "sections/a.tex"]
    (root / "sections" / "b.tex").write_bytes(b"\\section{B}\n")          # appears later; paper.tex unchanged
    assert latex._reachable_files(root) == ["paper.tex", "sections/a.tex", "sections/b.tex"]
    assert [s["title"] for s in latex.tex_sections(root)] == ["paper", "A", "B"]
    (root / "sections" / "a.tex").write_bytes(b"\\section{A2}\n")         # size change invalidates the cache
    assert [s["title"] for s in latex.tex_sections(root)][1] == "A2"


# ------------------------------------------------------------------ synctex forward bounds (scratch clone, read-only)
def _compiled():
    root = config.PAPER_ROOT
    return (root / "paper.pdf").exists() and latex._synctex_path(root / "paper.pdf") is not None \
        and (root / "sections" / "04_experiments.tex").exists()


@pytest.mark.skipif(not _compiled(), reason="needs the compiled scratch paper")
def test_forward_bounds_line_and_box():
    root = config.PAPER_ROOT
    n = latex._count_lines(latex._decode_tex((root / "sections" / "04_experiments.tex").read_bytes())[0])
    for big in (n + 20, 10 ** 30):
        r = latex.forward("sections/04_experiments.tex", big)
        assert r is None or r["line"] <= n
    for f, ln in (("paper.tex", 1), ("paper.tex", 66), ("paper.tex", 73), ("sections/04_experiments.tex", 63)):
        r = latex.forward(f, ln)
        if r is None:
            continue
        assert 0 <= r["x"] and r["x"] + r["w"] <= r["page_w"] + 0.01, (f, ln, r)
        assert 0 <= r["y"] and r["y"] + r["h"] <= r["page_h"] + 0.01, (f, ln, r)


# ------------------------------------------------------------------ frontend wiring (static)
def test_frontend_spec_saves_are_versioned():
    api = _read("api.js")
    app = _read("app.js")
    assert "X-Spec-Base-Hash" in api and "getSpecVersioned" in api and "X-Spec-Hash" in api
    assert "api.getSpecVersioned(id)" in app and "api.saveSpec(snap, S.specHash)" in app
    assert "resolveSpecConflict" in app and "checkSpecDiskChange" in app
    # the beforeunload keepalive PUT carries the base hash (it can only fail safely)
    m = re.search(r"beforeunload[\s\S]{0,900}?keepalive[^\n]*", app)
    assert m and "X-Spec-Base-Hash" in m.group(0)


def test_frontend_boot_does_not_wait_for_the_pdf():
    app = _read("app.js")
    boot = app[app.index("async function boot()"):app.index("// ───────────────────────────── read-only mode")]
    awaited = re.search(r"await Promise\.all\(\[([^\]]*)\]\)", boot).group(1)
    assert "loadPaper" not in awaited and "loadPaper();" in boot
    assert "Promise.race([img.decode()" in app


def test_frontend_reload_keeps_keys_typed_during_fetch():
    app = _read("app.js")
    i = app.index("async function openSection(")
    body = app[i:app.index("\nasync function ", i + 10)] if "\nasync function " in app[i + 10:] else app[i:]
    assert "typedGuard" in body and "ed.get() !== typedGuard" in body


def test_frontend_layout_keeps_page_largest():
    css = _read("styles.css")
    assert "--tex-w: min(580px, 34vw)" in css
    assert re.search(r"@media \(max-width: 1000px\) \{\s*#right \{ position: absolute;", css)
    assert re.search(r"#center \{ flex: 1 1 0; min-width: min\(560px, 38vw\); \}", css)
    assert re.search(r"#center-toolbar \{ flex-wrap: nowrap; overflow-x: auto;[^}]*position: relative; \}", css)
    assert 'id="right-close"' in _read("index.html")
    pv = _read("pdfview.js")
    assert "bw + 2 * pad <= r.width" in pv       # a box wider than the stage starts at its left edge
