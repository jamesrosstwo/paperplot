"""Tests for designer/render.py. Run: python -m pytest designer/tests/test_render.py  (or python designer/tests/test_render.py).

Uses a fresh temporary state dir and temporary output dirs; never touches the live Overleaf clone.
If designer.datastore is missing, a stand-in with the same load_table(name) reads data/difficulty_eval.tsv with pandas."""
import copy
import io
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # Desktop/paperplot
sys.path.insert(0, str(ROOT))
LIVE = Path(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf").resolve()
SCRATCH_PAPER = os.environ.get("DESIGNER_TEST_PAPER") or tempfile.mkdtemp(prefix="designer_paper_")  # see conftest.py
if "designer.config" not in sys.modules:
    os.environ.setdefault("DESIGNER_PAPER_ROOT", SCRATCH_PAPER)
    os.environ["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="designer_render_test_")

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from designer import config, render, spec  # noqa: E402

assert config.PAPER_ROOT != LIVE, "tests must not run against the live Overleaf clone"
SEED = ROOT / "designer" / "seeds" / "mug_ours_auxq2_levels_short.yaml"
SEED2 = ROOT / "designer" / "seeds" / "mug_difficulty_dataset.yaml"
# The contract aims at < 0.5 s per preview (typically ~0.2 s); the assert allows 1 s so a busy machine (other jobs,
# a browser, Docker) does not fail the suite. Tighten with DESIGNER_TEST_PREVIEW_LIMIT=0.5.
PREVIEW_LIMIT_S = float(os.environ.get("DESIGNER_TEST_PREVIEW_LIMIT", "1.0"))
REFERENCE_PNG = ROOT / "figures" / "out" / "mug_ours_auxq2_levels_short.png"
TSV = ROOT / "data" / "difficulty_eval.tsv"
OUT = Path(tempfile.mkdtemp(prefix="designer_render_out_"))
DATASTORE_STANDIN = False


def _install_dataset():
    """Register difficulty_eval in the (temporary) data store by reference, or stand in for a missing datastore."""
    global DATASTORE_STANDIN
    try:
        from designer import datastore
    except ImportError:
        import pandas as pd
        DATASTORE_STANDIN = True
        mod = types.ModuleType("designer.datastore")
        mod.load_table = lambda name, sheet=None: pd.read_csv({"difficulty_eval": TSV}[name], sep="\t")
        sys.modules["designer.datastore"] = mod
        import designer
        designer.datastore = mod
        return
    try:
        datastore.get("difficulty_eval")
    except Exception:
        datastore.add("difficulty_eval", str(TSV), notes="render test", make_link=False)


_install_dataset()


def seed():
    return yaml.safe_load(SEED.read_text(encoding="utf-8"))


def seed2():
    return yaml.safe_load(SEED2.read_text(encoding="utf-8"))


def _img(data):
    return np.asarray(Image.open(io.BytesIO(data) if isinstance(data, bytes) else data).convert("RGBA"))


def test_seed_pixel_fidelity():
    r = render.render(seed(), OUT / "short.pdf")
    a, b = _img(r["png"]), _img(REFERENCE_PNG)
    assert a.shape == b.shape, (a.shape, b.shape)
    frac = float((a != b).any(-1).mean())
    strong = float((np.abs(a.astype(int) - b.astype(int)) > 32).any(-1).mean())
    print(f"  seed vs figures/out PNG: {a.shape[1]}x{a.shape[0]} px, differing pixels {frac:.6f} (>32: {strong:.6f})")
    from matplotlib import ft2font
    if ft2font.__freetype_version__ == "2.12.1":     # the FreeType that rendered the reference (Windows Anaconda)
        assert frac < 0.001, frac
    else:   # pip wheels bundle FreeType 2.6.1: glyph-edge anti-aliasing differs, layout and shapes must not
        assert frac < 0.005 and strong < 0.0005, (frac, strong, ft2font.__freetype_version__)
    # PDF page is exactly 252 x 69 TeX pt
    import re
    m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]", Path(r["pdf"]).read_bytes())
    assert abs(float(m.group(1)) - 252 / 72.27 * 72) < 0.01 and abs(float(m.group(2)) - 69 / 72.27 * 72) < 0.01
    assert r["width_pt"] == 252 and r["height_pt"] == 69
    assert any("partial" in w for w in r["warnings"])
    st = {(s["level"], s["arm"]): s for s in r["stats"]}
    assert len(st) == 11
    s = st[("Nominal", "Classical")]
    assert (s["k"], s["n"]) == (3, 5) and abs(s["lo"] - 0.2307) < 1e-3 and abs(s["hi"] - 0.8824) < 1e-3
    sys.path.insert(0, str(ROOT / "figures"))
    import mug_best_bars as best
    for v in st.values():
        lo, hi = best.wilson(v["k"], v["n"])
        assert abs(lo - v["lo"]) < 1e-12 and abs(hi - v["hi"]) < 1e-12


def test_default_output_path_and_png():
    tmp_root = Path(tempfile.mkdtemp(prefix="designer_paper_"))
    old = config.PAPER_ROOT
    config.PAPER_ROOT = tmp_root
    try:
        r = render.render(seed())
    finally:
        config.PAPER_ROOT = old
    pdf = tmp_root / "figures" / "evals" / "mug_ours_auxq2_levels_short.pdf"
    assert Path(r["pdf"]) == pdf and pdf.exists() and pdf.with_suffix(".png").exists()
    assert Image.open(pdf.with_suffix(".png")).size == (2092, 572)   # 600 dpi, same size as the PDF page


def test_preview_fast_and_writes_nothing():
    tmp_root = Path(tempfile.mkdtemp(prefix="designer_paper_"))
    old = config.PAPER_ROOT
    config.PAPER_ROOT = tmp_root
    try:
        render.preview_png(seed())          # warm-up (font cache, imports)
        times = []
        for _ in range(5):
            t = time.perf_counter()
            png = render.preview_png(seed())
            times.append(time.perf_counter() - t)
    finally:
        config.PAPER_ROOT = old
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(io.BytesIO(png)).size == (int(252 / 72.27 * 200), int(69 / 72.27 * 200))
    assert list(tmp_root.iterdir()) == [], "preview must not write into the paper repo"
    print(f"  preview_png (inline seed, 200 dpi): median {sorted(times)[2] * 1000:.0f} ms, max {max(times) * 1000:.0f} ms")
    assert sorted(times)[2] < PREVIEW_LIMIT_S


# one changed value per style key; each must change the picture
CHANGES = {
    "theme": "acme_tnr", "font_family": "DejaVu Serif", "width_pt": 300, "height_pt": 100, "group_width": 0.9,
    "bar_fill": 0.9, "edge_width": 1.2, "na_label": "--", "legend_loc": "right", "legend_cols": 2,
    "legend_fontsize": 8, "tick_fontsize": 8, "xtick_fontsize": 10, "ylabel": "Rate", "ylabel_fontsize": 10,
    "y_mode": "percent", "out_of": 10, "ytick_step": 20, "divider_before": 1, "ci": "bayes", "ci_level": 0.8,
    "ci_color": "#0000FF", "ci_linewidth": 1.5, "ci_capsize": 3.5, "partial_below": None, "partial_ci_color": "#00FF00",
    "palette": "categorical", "fill_darken": 1.0, "edge_darken": 0.3, "transparent": True,
    "arm_order": {"fixed": ["Ours"], "rest": "as_listed"}, "level_sort": "desc", "legend_align": "right",
    "legend_box_fontsize": 8, "legend_pad": 0.5, "xtick_rotation": 30, "level_colors": {"Easy": "#BBBBBB"},
}
# keys that only show together with another setting: {key: (settings that make it visible, changed value)}
COMBOS = {
    "level_pin_first": ({"level_sort": "desc"}, 2),
    "divider_style": ({"divider_before": 1}, "theme"),
    "dup_ci_color": ({"_dup": True}, "#00AA00"),
}
# data.panels keys: tested on the panel seed in test_kinds.py
PANEL_KEYS = {"panel_gap", "share_x_labels", "panel_title_loc", "panel_title_fontsize", "panel_ylabel"}


def test_every_style_key_is_honoured():
    assert set(CHANGES) | set(COMBOS) | PANEL_KEYS == set(spec.defaults()),         set(spec.defaults()) ^ (set(CHANGES) | set(COMBOS) | PANEL_KEYS)
    base = render.preview_png(seed(), dpi=100)
    for key, value in CHANGES.items():
        s = seed()
        s["style"][key] = value
        assert spec.validate(s) == [], (key, spec.validate(s))
        png = render.preview_png(s, dpi=100)
        assert png != base, f"style.{key} = {value!r} did not change the image"
    for key, (setup, value) in COMBOS.items():
        s = seed()
        for k, v in setup.items():
            if k == "_dup":
                s["data"]["arms"][0]["counts"][0] = [16, 20, {"dup": True}]
            else:
                s["style"][k] = v
        ref = render.preview_png(s, dpi=100)
        s["style"][key] = value
        assert spec.validate(s) == [], (key, spec.validate(s))
        assert render.preview_png(s, dpi=100) != ref, f"style.{key} = {value!r} did not change the image"
    s = seed()
    s["style"]["legend_loc"] = "none"
    assert render.preview_png(s, dpi=100) != base
    s = seed()
    s["style"]["ci"] = "none"
    r = render.preview(s, dpi=100)
    assert r["png"] != base and all(x["lo"] is None for x in r["stats"])


def test_specific_effects():
    s = seed()
    s["style"]["transparent"] = True
    a = _img(render.preview_png(s, dpi=50))
    assert a[0, 0, 3] == 0, "transparent background"
    assert _img(render.preview_png(seed(), dpi=50))[0, 0, 3] == 255
    s = seed()
    s["style"]["ci"] = "bayes"
    s["style"]["ci_level"] = 0.9
    r = render.preview(s, dpi=50)
    import paperplot as pp
    lo, hi = pp.stats.rate_interval(16, 20, level=0.9)
    assert abs(r["stats"][0]["lo"] - lo) < 1e-12 and abs(r["stats"][0]["hi"] - hi) < 1e-12
    s = seed()
    s["style"]["ci_level"] = 0.99
    assert render.preview(s, dpi=50)["stats"][0]["lo"] < render.wilson(16, 20, 0.95)[0]


def test_edge_cases():
    s = seed()
    s["data"]["arms"][1]["counts"] = [[0, 20], [0, 20], [0, 20]]          # zero-height bars
    s["data"]["arms"][2]["counts"] = [None, None, None]                  # no data at all
    s["data"]["arms"][3]["hatch"] = "..."
    r = render.preview(s, dpi=50)
    assert any("no data" in w for w in r["warnings"])
    s["style"]["divider_before"] = 7
    assert any("divider_before" in w for w in render.preview(s, dpi=50)["warnings"])
    s = seed()
    s["data"]["levels"] = ["Nominal"]
    for a in s["data"]["arms"]:
        a["counts"] = [[5, 20]]
    s["style"]["palette"] = "sequential"
    assert render.preview_png(s, dpi=50)[:4] == b"\x89PNG"
    s = seed()
    for i in range(6):                                                  # more arms than the categorical palette
        s["data"]["arms"].append({"name": f"extra {i}", "color": "#336699", "counts": [[i, 10], [i, 10], None]})
    s["style"]["palette"] = "categorical"
    assert any("repeat" in w for w in render.preview(s, dpi=50)["warnings"])
    bad = seed()
    bad["style"]["width_pt"] = 5
    try:
        render.preview_png(bad)
        raise AssertionError("invalid spec must raise")
    except spec.SpecError as e:
        assert "width_pt" in e.errors[0]


def test_dataset_seed():
    s = seed2()
    d = render.resolve_data(s)
    assert d["levels"] == ["Nominal", "Easy", "Medium"]
    by = {a["name"]: a["counts"] for a in d["arms"]}
    assert by["Classical"] == [[3, 5], [3, 10], None]
    assert by["ZeroNVS"] == [None, [5, 20], None]
    assert by[r"$\mathrm{\pi}_{0.5}$ (base policy)"] == [[16, 20], [12, 20], [9, 20]]
    assert by["Ours"] == [[17, 20], [15, 20], None]
    render.preview_png(s)
    dt = float("inf")
    for _ in range(3):               # best of 3: one slow run on a busy machine is not a regression
        t = time.perf_counter()
        png = render.preview_png(s)
        dt = min(dt, time.perf_counter() - t)
    print(f"  preview_png (dataset seed): {dt * 1000:.0f} ms{' [datastore stand-in]' if DATASTORE_STANDIN else ''}")
    assert dt < PREVIEW_LIMIT_S and png[:4] == b"\x89PNG"
    r = render.render(s, OUT / "dataset.pdf")
    assert Path(r["png"]).exists() and len(r["stats"]) == 10
    # inline equivalent of the dataset spec renders identically
    inline = copy.deepcopy(s)
    inline["data"] = {"source": "inline", "levels": d["levels"],
                      "arms": [{k: a[k] for k in ("name", "color", "counts")} for a in d["arms"]]}
    assert render.preview_png(inline) == png
    # default query: every arm value in order of appearance, theme colors
    s2 = seed2()
    s2["data"]["query"] = {"level": "condition", "arm": "policy", "successes": "successes", "trials": "trials",
                           "filter": {"task": "rope"}}
    d2 = render.resolve_data(s2)
    assert d2["levels"] == ["Nominal", "Easy", "Hard"] and [a["name"] for a in d2["arms"]] == [
        "base", "ours-small"]
    s2["data"]["query"]["level"] = "nope"
    try:
        render.resolve_data(s2)
        raise AssertionError("missing column must raise")
    except ValueError as e:
        assert "nope" in str(e)


def test_layout_overflow_warnings():
    def warns(**st):
        s = seed()
        s["style"].update(st)
        return [w for w in render.preview(s)["warnings"] if "partial" not in w and "drawn at its rate" not in w]
    assert warns() == []                                    # the seed fits
    assert warns(legend_loc="right", legend_cols=1) == []   # a 1-column legend on the right fits too
    assert any("squeezed" in w for w in warns(legend_loc="right"))
    w12 = warns(legend_fontsize=12)
    assert any("legend" in w for w in w12), w12
    assert any("y label" in w for w in warns(ylabel_fontsize=14))


def test_colliding_na_marks_merge_per_group():
    from matplotlib.text import Annotation
    s = seed()
    for a in s["data"]["arms"][1:4]:
        a["counts"][2] = None            # three neighbouring arms not evaluated at the last level
    s["style"]["group_width"] = 0.365
    sp = render._prepare(s)
    with render._LOCK:
        fig, _, _ = render._draw(sp)
        try:
            marks = [t for t in fig.axes[0].texts if isinstance(t, Annotation)]
        finally:
            render.plt.close(fig)
    by_level = {}
    for t in marks:
        by_level.setdefault(round(t.xy[0]), []).append(t)
    assert len(by_level[2]) == 1, "neighbouring n/a marks of one group should merge into one"
    assert len(by_level[0]) == 1      # the seed's single n/a (ZeroNVS @ Nominal) is untouched


def test_theme_without_the_specs_swatches():
    s = seed()
    s["style"]["theme"] = "default"
    a = _img(render.preview_png(s, dpi=100))
    assert a.shape[0] > 10            # renders instead of failing validation on 'tol-blue'


def test_threads_serialized():
    s = seed()
    ref = render.preview_png(s, dpi=80)
    other = seed()
    other["style"]["theme"] = "default"
    for a, c in zip(other["data"]["arms"], ("navy", "gold", "rust", "sky", "orange")):
        a["color"] = c
    results, errors = [], []

    def work(sp, keep):
        try:
            png = render.preview_png(sp, dpi=80)
            if keep:
                results.append(png)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=work, args=(s if i % 2 == 0 else other, i % 2 == 0)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert results and all(r == ref for r in results), "interleaved renders must not leak theme state"


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
