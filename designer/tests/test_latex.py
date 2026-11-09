"""Tests for designer/latex.py against the scratch paper clone (never the live Overleaf clone).

Run:  PYTHONUTF8=1 python -m pytest designer/tests/test_latex.py -q     (from C:/Users/James/Desktop/paperplot)
  or  PYTHONUTF8=1 python designer/tests/test_latex.py

DESIGNER_PAPER_ROOT must point at a disposable clone; DESIGNER_STATE_DIR defaults to a fresh temp dir.
Read-only tests (scan, compile, locate, context) use the scratch clone; editing tests use a temp copy of it.
"""
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_SCRATCH = os.environ.get("DESIGNER_TEST_PAPER") or (
    "C:/Users/James/AppData/Local/Temp/claude/9119a02420dddb37/7a66152e-6919-48a8-b3a9-c4911b85a0e4/"
    "scratchpad/designer_test/paper")   # conftest.py sets DESIGNER_TEST_PAPER under pytest
os.environ.setdefault("DESIGNER_PAPER_ROOT", _SCRATCH)
os.environ.setdefault("DESIGNER_STATE_DIR", tempfile.mkdtemp(prefix="designer_state_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from designer import config, latex  # noqa: E402

LIVE = Path(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf").resolve()
assert config.PAPER_ROOT != LIVE, "refusing to test against the live Overleaf clone"
ROOT = config.PAPER_ROOT
if not (ROOT / "paper.tex").is_file():
    try:
        import pytest
        pytest.skip(f"no disposable paper clone at {ROOT} (set DESIGNER_TEST_PAPER)", allow_module_level=True)
    except ImportError:
        print(f"SKIP: no disposable paper clone at {ROOT} (set DESIGNER_TEST_PAPER)")
        sys.exit(0)
PAGE_W, PAGE_H = 612.0, 792.0          # IEEEtran letter

_COMPILED = {}


def _figs():
    return {f["fid"]: f for f in latex.scan_figures(ROOT)}


def _by_stem(stem):
    for f in latex.scan_figures(ROOT):
        if any(Path(g["path"]).stem == stem for g in f["graphics"]) and not f["commented"]:
            return f
    raise AssertionError(f"no active figure with graphics {stem}")


def _ensure_compiled():
    if "r" not in _COMPILED:
        _COMPILED["r"] = latex.compile_paper(ROOT)
    return _COMPILED["r"]


def _copy_paper():
    tmp = tempfile.mkdtemp(prefix="designer_paper_")
    atexit.register(shutil.rmtree, tmp, True)
    dst = Path(tmp) / "paper"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "*.aux", "*.log", "*.synctex.gz", "*.fls",
                                                              "*.fdb_latexmk", "*.out", "*.blg", "*.pdf.tmp"))
    return dst


# ------------------------------------------------------------------ scan

def test_scan_finds_figures():
    figs = latex.scan_figures(ROOT)
    stems = {Path(g["path"]).stem for f in figs if not f["commented"] for g in f["graphics"]}
    for s in ("mug_conditions", "difficulty_by_task", "headline", "projection"):
        assert s in stems, (s, stems)
    fids = [f["fid"] for f in figs]
    assert len(fids) == len(set(fids)), "fids must be unique"
    proj = _by_stem("projection")
    assert proj["file"] == "sections/03_method.tex"
    assert (proj["begin_line"], proj["end_line"]) == (49, 54)
    assert proj["graphics"][0]["line"] == 51
    assert proj["graphics"][0]["resolved"] == "figures/diagrams/projection.pdf"
    assert proj["graphics"][0]["options"] == "width=\\linewidth, clip"
    assert proj["fid"] == "sections/03_method.tex:figure:mug_best_bars"
    head = _by_stem("headline")
    assert head["file"] == "paper.tex" and head["env"] == "center" and head["in_twocolumn"]
    assert head["caption"] == "\\textbf{Method overview.}"
    mc = _by_stem("mug_conditions")
    assert mc["label"] == "figure:individual_conditions" and mc["env"] == "figure"
    assert mc["caption"].startswith("Success rates of pi0.5")
    # fully commented figure is reported, flagged
    com = [f for f in figs if f["commented"]]
    assert any(Path(g["path"]).stem == "mug_best_bars" for f in com for g in f["graphics"])
    assert all(f["spec_id"] is None or isinstance(f["spec_id"], str) for f in figs)


# ------------------------------------------------------------------ compile + locate

def test_full_compile():
    r = _ensure_compiled()
    print(f"\nfull compile: ok={r['ok']} {r['seconds']} s exit={r['exit_code']} errors={len(r['errors'])} "
          f"warnings={r['warnings_count']} bib_errors={r['bib_errors_count']} up_to_date={r['up_to_date']}")
    assert r["ok"], r["log_tail"]
    assert Path(r["pdf"]).exists() and (ROOT / "paper.synctex.gz").exists()
    assert isinstance(r["errors"], list)
    for e in r["errors"]:
        assert set(e) >= {"file", "line", "message"}


def _check_box(fig, box):
    assert box is not None, fig["fid"]
    assert box["method"] == "synctex-record", box
    assert box["page_w"] == PAGE_W and box["page_h"] == PAGE_H
    assert 0 <= box["x"] and box["x"] + box["w"] <= PAGE_W + 0.5
    assert 0 <= box["y"] and box["y"] + box["h"] <= PAGE_H + 0.5
    # aspect ratio of the box == aspect ratio of the image file (box covers the image, not the line)
    asp = latex._image_aspect(ROOT / fig["graphics"][0]["resolved"])
    assert abs(box["w"] / box["h"] - asp) / asp < 0.01, (box, asp)
    # click in the middle of the box maps back to the figure's source
    r = latex.reverse(box["page"], box["x"] + box["w"] / 2, box["y"] + box["h"] / 2, ROOT)
    assert r and r["file"] == fig["file"], r
    assert fig["begin_line"] <= r["line"] <= fig["end_line"] + 5, (r, fig["begin_line"], fig["end_line"])


def test_locate_boxes():
    _ensure_compiled()
    col1 = (48.96, 300.02)       # IEEEtran two-column letter: left column x range (bp)
    col2 = (311.97, 563.03)
    expect = {"headline": 1, "projection": 3, "mug_conditions": 3, "difficulty_by_task": 4}
    for stem, page in expect.items():
        fig = _by_stem(stem)
        box = latex.locate(fig, ROOT)
        print(f"{stem:20s} page {box['page']} x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
              f" float={box.get('float_box')}")
        _check_box(fig, box)
        assert box["page"] == page, (stem, box)
        if stem == "headline":       # \includegraphics[width=\textwidth] in \twocolumn[...]
            assert abs(box["x"] - col1[0]) < 0.5 and abs(box["x"] + box["w"] - col2[1]) < 0.5
        elif stem == "mug_conditions":   # scale=1.25 of a 179.33 bp wide pdf, centred in the right column
            assert abs(box["w"] - 179.3275 * 1.25) < 0.1
            assert col2[0] <= box["x"] and box["x"] + box["w"] <= col2[1]
            assert abs((box["x"] - col2[0]) - (col2[1] - box["x"] - box["w"])) < 0.5
        else:                        # width=\linewidth / \columnwidth in the left column
            assert abs(box["x"] - col1[0]) < 0.5 and abs(box["w"] - (col1[1] - col1[0])) < 0.5
        if stem != "headline":
            fb = box["float_box"]
            assert fb["y"] <= box["y"] + 0.5 and fb["y"] + fb["h"] > box["y"] + box["h"]   # includes caption


def test_locate_all_cache():
    _ensure_compiled()
    a = latex.locate_all(ROOT)
    assert (Path(config.CACHE_DIR) / "locate.json").exists()
    t = time.time()
    b = latex.locate_all(ROOT)
    assert time.time() - t < 2
    assert a.keys() == b.keys()
    for fid, f in _figs().items():
        if f["commented"]:
            assert a[fid] is None
        else:
            assert a[fid] and a[fid]["page"] == latex.locate(f, ROOT)["page"]


def test_synctex_view_box_is_not_the_image():
    """Documents why locate() parses the .synctex.gz: CLI view reports the unscaled graphic for width= images."""
    _ensure_compiled()
    fig = _by_stem("projection")
    v = latex._synctex_view(ROOT / "paper.pdf", ROOT / fig["file"], fig["graphics"][0]["line"])
    assert v and v[0]["page"] == 3
    assert v[0]["w"] > PAGE_W * 0.9     # 583 bp: the natural size of projection.pdf, not the 251 bp column


# ------------------------------------------------------------------ context

def test_compile_context_fast():
    for stem in ("mug_conditions", "difficulty_by_task", "headline"):
        fig = _by_stem(stem)
        r = latex.compile_context(fig["fid"], root=ROOT)
        print(f"context {stem}: ok={r['ok']} {r['seconds']} s errors={r['errors']} box={r['fig_box'] and {k: r['fig_box'][k] for k in ('page', 'x', 'y', 'w', 'h')}}")
        assert r["ok"], r["log_tail"]
        assert r["errors"] == [], r["errors"]
        assert r["seconds"] < 10
        assert Path(r["pdf"]).exists() and Path(r["pdf"]).parent == Path(config.CACHE_DIR) / "context"
        assert r["name"] == Path(r["pdf"]).stem
        b = r["fig_box"]
        asp = latex._image_aspect(ROOT / fig["graphics"][0]["resolved"])
        assert b and abs(b["w"] / b["h"] - asp) / asp < 0.01
        # preamble is the paper's own
        tex = Path(r["pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
        assert tex.startswith("\\documentclass[conference]{IEEEtran}")
    # nothing written into the paper root by context compiles
    assert not list(ROOT.glob("ctx_*"))


def test_compile_context_override_and_error_mapping():
    fig = _by_stem("difficulty_by_task")
    env = latex.read_env(fig["fid"], ROOT)["text"]
    lines = env.split("\n")
    # put an undefined macro on the caption line (4th line of the env)
    cap_idx = next(i for i, l in enumerate(lines) if "\\caption" in l)
    lines[cap_idx] = "    \\caption{Changed \\undefinedmacroxyz caption}"
    r = latex.compile_context(fig["fid"], text_override="\n".join(lines), root=ROOT)
    assert r["ok"]                        # nonstopmode still produces a PDF
    errs = [e for e in r["errors"] if "Undefined control sequence" in e["message"]]
    assert errs, r["errors"]
    assert errs[0]["file"] == fig["file"] and errs[0]["line"] == fig["begin_line"] + cap_idx, errs


def test_parse_log():
    log = "\n".join([
        "(./sections/04_experiments.tex",
        "./sections/04_experiments.tex:15: Undefined control sequence.",
        "l.15 \\foo",
        "",
        "! LaTeX Error: File `missing.pdf' not found.",
        "",
        "See the LaTeX manual or LaTeX Companion for explanation.",
        "l.31 ...{missing.pdf}",
        "LaTeX Warning: Reference `x' on page 2 undefined on input line 3.",
        "Package natbib Warning: Citation `y' on page 1 undefined on input line 6.",
    ])
    errs, nw = latex.parse_log(log, ROOT)
    assert errs[0] == {"file": "sections/04_experiments.tex", "line": 15, "message": "Undefined control sequence."}
    assert errs[1]["line"] == 31 and errs[1]["file"] is None and "missing.pdf" in errs[1]["message"]
    assert nw == 2


def test_full_compile_reports_errors():
    """Real latexmk run on a temp copy with an injected error: errors carry the section file and line."""
    root = _copy_paper()
    p = root / "sections" / "04_experiments.tex"
    lines = p.read_text(encoding="utf-8").split("\n")
    lines[19] = lines[19] + " \\undefinedmacroxyz"          # line 20
    p.write_text("\n".join(lines), encoding="utf-8")
    r = latex.compile_paper(root)
    print(f"\nfull compile (copy, injected error): ok={r['ok']} {r['seconds']} s errors={r['errors'][:2]}")
    assert r["ok"]                                           # -f + nonstopmode still writes a PDF
    assert any(e["file"] == "sections/04_experiments.tex" and e["line"] == 20
               and "Undefined control sequence" in e["message"] for e in r["errors"]), r["errors"]


# ------------------------------------------------------------------ editing (temp copy)

def test_read_write_env_roundtrip_and_stale():
    root = _copy_paper()
    fig = next(f for f in latex.scan_figures(root) if any("difficulty_by_task" in g["path"] for g in f["graphics"]))
    path = root / fig["file"]
    orig = path.read_bytes()
    env = latex.read_env(fig["fid"], root)
    assert env["text"].startswith("\\begin{figure}") and env["text"].rstrip().endswith("\\end{figure}")
    assert (env["begin_line"], env["end_line"]) == (fig["begin_line"], fig["end_line"])

    # identity write leaves the file byte-identical
    w0 = latex.write_env(fig["fid"], env["text"], env["sha"], root)
    assert path.read_bytes() == orig and w0["sha"] == env["sha"]

    new_text = env["text"].replace("\\caption{}", "\\caption{Success rate by difficulty.}\n    % designer")
    w = latex.write_env(fig["fid"], new_text, env["sha"], root)
    assert w["fid"] == fig["fid"] and w["end_line"] == fig["end_line"] + 1
    after = path.read_bytes().decode().splitlines()
    before = orig.decode().splitlines()
    assert after[:fig["begin_line"] - 1] == before[:fig["begin_line"] - 1]
    assert after[w["end_line"]:] == before[fig["end_line"]:]
    env2 = latex.read_env(w["fid"], root)
    assert env2["text"] == new_text and env2["sha"] == w["sha"]

    # stale sha refused, file untouched
    try:
        latex.write_env(fig["fid"], env["text"], env["sha"], root)
        raise AssertionError("expected StaleError")
    except latex.StaleError:
        pass
    assert latex.read_env(w["fid"], root)["text"] == new_text

    # external change -> StaleError
    path.write_bytes(path.read_bytes() + b"\n% edited elsewhere\n")
    try:
        latex.write_env(w["fid"], env["text"], env2["sha"], root)
        raise AssertionError("expected StaleError")
    except latex.StaleError:
        pass

    # unbalanced text refused
    env3 = latex.read_env(w["fid"], root)
    try:
        latex.write_env(w["fid"], "\\begin{figure}\n\\caption{x", env3["sha"], root)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    # label change -> new fid
    w4 = latex.write_env(w["fid"], env3["text"].replace("figure:difficulty_by_task", "figure:dbt2"), env3["sha"], root)
    assert w4["fid"] == "sections/04_experiments.tex:figure:dbt2"
    # the scratch clone itself was not touched
    assert (ROOT / fig["file"]).read_bytes() == orig


def test_crlf_preserved():
    root = _copy_paper()
    p = root / "sections" / "03_method.tex"
    p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    fig = next(f for f in latex.scan_figures(root) if f["file"] == "sections/03_method.tex")
    env = latex.read_env(fig["fid"], root)
    latex.write_env(fig["fid"], env["text"].replace("Newton iterations.", "Newton steps."), env["sha"], root)
    data = p.read_bytes()
    assert b"Newton steps." in data and data.count(b"\n") == data.count(b"\r\n")


def test_insert_figure():
    root = _copy_paper()
    n_before = len(latex.scan_figures(root))
    fid = latex.insert_figure("sections/04_experiments.tex", 36, "figures/evals/mug_best_bars.pdf",
                              "Best bars.", "figure:new_bars", root=root)
    assert fid == "sections/04_experiments.tex:figure:new_bars"
    figs = {f["fid"]: f for f in latex.scan_figures(root)}
    assert len(figs) == n_before + 1
    f = figs[fid]
    assert f["begin_line"] == 38 and f["graphics"][0]["resolved"] == "figures/evals/mug_best_bars.pdf"
    assert f["graphics"][0]["options"] == "width=\\columnwidth" and f["caption"] == "Best bars."
    # inside an existing figure -> refused
    try:
        latex.insert_figure("sections/04_experiments.tex", 30, "figures/evals/mug_best_bars.pdf", "x", "y",
                            root=root)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # preamble of the main file -> refused
    try:
        latex.insert_figure("paper.tex", 5, "figures/evals/mug_best_bars.pdf", "x", "y", root=root)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # outside the root -> refused
    try:
        latex.insert_figure("../x.tex", 1, "a.pdf", "x", "y", root=root)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def _tricky_tree() -> Path:
    """paper.tex inputs a.tex and b.tex; never.tex exists but is not part of the paper."""
    root = Path(tempfile.mkdtemp(prefix="designer_tricky_"))
    atexit.register(shutil.rmtree, str(root), True)
    (root / "sections").mkdir()
    (root / "paper.tex").write_text("\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
                                    "\\input{sections/a}\n\\input{sections/b}\n\\end{document}\n", encoding="utf-8")
    (root / "sections" / "a.tex").write_text("Para one.\n\nPara two.\n\\begin{itemize}\n\\item x\n"
                                             "\\end{itemize}\nPara three.\n", encoding="utf-8")
    (root / "sections" / "b.tex").write_text("Text.\n\\iffalse\nhidden \\ifx a b \\fi more\n\\fi\nAfter.\n",
                                             encoding="utf-8")
    (root / "sections" / "never.tex").write_text("Orphan.\n", encoding="utf-8")
    return root


def test_insert_figure_validates_before_writing():
    root = _tricky_tree()
    files = {p: p.read_bytes() for p in (root / "sections").iterdir()}

    def refused(*a, **kw):
        try:
            latex.insert_figure(*a, root=root, **kw)
        except ValueError as e:
            return str(e)
        raise AssertionError(f"expected ValueError for {a} {kw}")

    assert "not part of the paper" in refused("sections/never.tex", 1, "figs/x.pdf", "cap", "fig:n1")
    assert "break the TeX" in refused("sections/a.tex", 1, "figs/x.pdf", "cap with } brace", "fig:n2")
    assert "break the TeX" in refused("sections/a.tex", 1, "figs/x.pdf", "cap {open", "fig:n2")
    assert "break the TeX" in refused("sections/a.tex", 1, "figs/x.pdf", "50% of } it", "fig:n2")
    assert "blank line" in refused("sections/a.tex", 1, "figs/x.pdf", "a\n\nb", "fig:n2")
    assert "bad label" in refused("sections/a.tex", 1, "figs/x.pdf", "cap", "fig:a}b")
    assert "bad graphics" in refused("sections/a.tex", 1, "figs/x}.pdf", "cap", "fig:n2")
    assert "iffalse" in refused("sections/b.tex", 2, "figs/x.pdf", "cap", "fig:n3")
    assert "iffalse" in refused("sections/b.tex", 3, "figs/x.pdf", "cap", "fig:n3")
    assert "itemize" in refused("sections/a.tex", 5, "figs/x.pdf", "cap", "fig:n4")
    for p, raw in files.items():
        assert p.read_bytes() == raw, f"{p.name} was modified by a refused insert"
    # valid spots still work: after the \fi that closes \iffalse, and after \end{itemize}
    fid = latex.insert_figure("sections/b.tex", 4, "figs/x.pdf", "Balanced {nested} caption", "fig:ok", root=root)
    assert fid == "sections/b.tex:fig:ok"
    fid2 = latex.insert_figure("sections/a.tex", 6, "figs/y.pdf", "Second", "fig:ok2", root=root)
    assert fid2 == "sections/a.tex:fig:ok2"
    assert {f["fid"] for f in latex.scan_figures(root)} == {fid, fid2}


def test_boxes_stay_on_compiled_pdf_after_tex_edit():
    """Adding lines without recompiling must not move the boxes: they describe the PDF on disk."""
    root = _copy_paper()
    r = latex.compile_paper(root)
    assert r["ok"], r["log_tail"]
    before = latex.locate_all(root)
    p = root / "sections" / "04_experiments.tex"
    time.sleep(1.1)
    original = p.read_bytes()
    lines = original.decode("utf-8").split("\n")
    p.write_text("\n".join(lines[:23] + ["% padding comment"] * 8 + lines[23:]), encoding="utf-8")
    after = latex.locate_all(root)
    moved = []
    for fid, b in before.items():
        a = after.get(fid)
        if b is None:
            continue
        assert a is not None, fid
        if any(abs(a[k] - b[k]) > 0.01 for k in ("x", "y", "w", "h")) or a["page"] != b["page"]:
            moved.append((fid, b, a))
        assert a["stale"] == fid.startswith("sections/04_experiments.tex"), (fid, a["stale"])
    assert not moved, moved
    # a single figure via locate() agrees
    f = next(f for f in latex.scan_figures(root) if f["fid"].endswith("mug_ours_auxq2_levels_short"))
    one = latex.locate(f, root)
    assert one["page"] == before[f["fid"]]["page"] and abs(one["y"] - before[f["fid"]]["y"]) < 0.01
    # reverting the edit (newer mtime, same content as compiled) is not stale: latexmk would not recompile it
    time.sleep(1.1)
    p.write_bytes(original)
    assert not any(b and b["stale"] for b in latex.locate_all(root).values())


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
