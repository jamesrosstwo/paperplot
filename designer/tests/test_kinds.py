"""Tests for the 2026-11-09 renderer features: stacked panels, global arm order, duplicate-flagged counts, the
`lines` kind (Fig. 4) and the conditions-chart options (Fig. mug_conditions).
Run: python -m pytest designer/tests/test_kinds.py  (or python designer/tests/test_kinds.py).

Uses a fresh temporary state dir and temporary output dirs; never touches the live Overleaf clone."""
import copy
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # Desktop/paperplot
sys.path.insert(0, str(ROOT))
from designer.live_papers import is_live  # noqa: E402
SCRATCH_PAPER = os.environ.get("DESIGNER_TEST_PAPER") or tempfile.mkdtemp(prefix="designer_paper_")  # see conftest.py
if "designer.config" not in sys.modules:
    os.environ.setdefault("DESIGNER_PAPER_ROOT", SCRATCH_PAPER)
    os.environ["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="designer_kinds_test_")

import numpy as np  # noqa: E402
import pytest
import yaml  # noqa: E402
from matplotlib.colors import to_hex  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.text import Annotation  # noqa: E402
from PIL import Image  # noqa: E402

from designer import config, render, spec  # noqa: E402

assert not is_live(config.PAPER_ROOT), "tests must not run against the live Overleaf clone"
SEEDS = ROOT / "designer" / "seeds"
OUT = Path(tempfile.mkdtemp(prefix="designer_kinds_out_"))
PI = r"$\mathrm{\pi}_{0.5}$"
BASE = PI + " (base policy)"


def load_seed(name):
    return yaml.safe_load((SEEDS / f"{name}.yaml").read_text(encoding="utf-8"))


def _img(data):
    return np.asarray(Image.open(io.BytesIO(data) if isinstance(data, bytes) else data).convert("RGBA"))


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _maybe_update_reference(r, ref):
    """designer/docker/test.sh --update-references: this render becomes the reference (PNG and PDF)."""
    if os.environ.get("DESIGNER_UPDATE_REFERENCES") == "1":
        ref.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(r["png"], ref)
        shutil.copyfile(r["pdf"], ref.with_suffix(".pdf"))


def _assert_same_png(png, ref, label):
    a, b = _img(png), _img(ref)
    assert a.shape == b.shape, (label, a.shape, b.shape)
    frac = float((a != b).any(-1).mean())
    strong = float((np.abs(a.astype(int) - b.astype(int)) > 32).any(-1).mean())
    print(f"  {label} vs figures/out PNG: {a.shape[1]}x{a.shape[0]} px, differing pixels {frac:.6f} (>32: {strong:.6f})")
    from matplotlib import ft2font
    if ft2font.__freetype_version__ == "2.12.1":     # the FreeType that rendered the references (Windows Anaconda)
        assert frac < 0.001, frac
    else:   # pip wheels bundle FreeType 2.6.1: glyph-edge anti-aliasing differs, layout and shapes must not
        assert frac < 0.005 and strong < 0.0005, (frac, strong, ft2font.__freetype_version__)


def _mediabox(pdf):
    m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]", Path(pdf).read_bytes())
    return float(m.group(1)), float(m.group(2))


def _draw(s):
    sp = render._prepare(s)
    with render._LOCK:
        fig, stats, warns = render._draw(sp)
    return fig, stats, warns


# --------------------------------------------------------------------------- fidelity with the paper scripts
def test_lines_reproduces_fig4():
    """figures/difficulty.py::by_task with the seed's counts, without re-run flags."""
    s = load_seed("difficulty_by_task")
    old = {"Mug": [[16, 20], [12, 20], [9, 20], [2, 20]], "Push T": [[15, 20], [4, 20], None, [1, 20]],
           "Rope": [[17, 20], [13, 20], None, [1, 20]]}
    for a in s["data"]["arms"]:
        a["counts"] = old[a["name"]]
    r = render.render(s, OUT / "difficulty_by_task.pdf")
    ref = FIXTURES / "reference" / "difficulty_by_task.png"
    _maybe_update_reference(r, ref)
    if not ref.is_file():
        pytest.skip(f"reference {ref.relative_to(ROOT)} is missing")
    _assert_same_png(r["png"], ref, "lines (Fig. 4)")
    a, b = _mediabox(r["pdf"]), _mediabox(ref.with_suffix(".pdf"))
    assert abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01, (a, b)
    assert abs(a[0] - 252 / 72.27 * 72) < 0.01 and abs(a[1] - 252 * 0.42 / 72.27 * 72) < 0.01
    assert r["warnings"] == [] and r["notes"] == []


def test_conditions_reproduces_mug_conditions():
    """figures/mug_conditions.py: sorted columns, gray Nominal, theme divider, 45 degree labels, legend top right."""
    s = load_seed("mug_conditions")
    lv = s["data"]["levels"]
    old = {"Nominal": 16, "BG": 17, "Light (easy)": 18, "Cloth": 15, "Distractor": 11, "View (easy)": 14,
           "View (hard)": 7, "Light (hard)": 12}
    s["data"]["arms"][0]["counts"] = [[old[l], 20] for l in lv]
    r = render.render(s, OUT / "mug_conditions.pdf")
    ref = FIXTURES / "reference" / "mug_conditions.png"
    _maybe_update_reference(r, ref)
    if not ref.is_file():
        pytest.skip(f"reference {ref.relative_to(ROOT)} is missing")
    _assert_same_png(r["png"], ref, "conditions")
    a, b = _mediabox(r["pdf"]), _mediabox(ref.with_suffix(".pdf"))
    assert abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01, (a, b)


# --------------------------------------------------------------------------- panels, global order, dup
def _bar_colors_by_group(ax):
    """{group index: [fill hex of each drawn bar, left to right]} plus the slot index of each bar."""
    out = {}
    for p in ax.patches:
        if isinstance(p, Rectangle) and p.get_width() < 1:
            xc = p.get_x() + p.get_width() / 2
            out.setdefault(int(round(xc)), []).append((xc, to_hex(p.get_facecolor())))
    return {g: [c for _, c in sorted(v)] for g, v in out.items()}


def test_panels_one_global_order_dup_cis_titles():
    s = load_seed("mug_baselines_panels")
    assert spec.validate(s) == []
    fig, stats, warns = _draw(s)
    try:
        axes = fig.axes
        assert len(axes) == 3
        names = [a["name"] for a in s["data"]["arms"]]
        cells = render.resolve_data(s)
        order = render.arm_order(s["style"], names, [[c for p in cells["panels"] for c in render._cells(p["arms"][i])]
                                                     for i in range(len(names))])
        assert [names[i] for i in order] == [BASE, "Ours", "RoboEngine", "Classical", "ZeroNVS"]
        # slot -> color is the same in all 9 groups: every bar of an arm sits at the same offset in its group
        sw = spec.swatches("acme")
        fill = {n: render._darken(sw[a["color"]], 0.7).lower() for n, a in zip(names, s["data"]["arms"])}
        gw = s["style"]["group_width"]
        slot_of = {}
        for ax in axes:
            for p in ax.patches:
                if isinstance(p, Rectangle) and p.get_width() < 1 and p.get_height() > 0:
                    xc = p.get_x() + p.get_width() / 2
                    g = int(round(xc))
                    slot = int(round((xc - (g - gw / 2)) / (gw / 5) - 0.5))
                    slot_of.setdefault(to_hex(p.get_facecolor()), set()).add(slot)
        for i, n in enumerate([BASE, "Ours", "RoboEngine", "Classical", "ZeroNVS"]):
            assert slot_of.get(fill[n]) in (None, {i}), (n, slot_of.get(fill[n]))
        assert slot_of[fill[BASE]] == {0} and slot_of[fill["Ours"]] == {1} and slot_of[fill["ZeroNVS"]] == {4}
        # yellow intervals exactly on the dup cells
        dup = to_hex(spec.DUP_CI_DEFAULT).lower()
        per_ax = []
        for ax in axes:
            cols = [to_hex(c.get_colors()[0]).lower() for c in ax.collections if len(c.get_colors())]
            per_ax.append(sum(c == dup for c in cols) // 3)      # each interval: 1 line + 2 caps (one per artist)
        dups = sorted((st["panel"], st["level"], st["arm"]) for st in stats if st.get("dup"))
        assert dups == sorted([("Mug", "Nominal", BASE), ("Mug", "Easy", BASE), ("Mug", "Nominal", "RoboEngine"),
                               ("Push-T", "Easy", BASE), ("Rope", "Nominal", BASE), ("Rope", "Easy", BASE)])
        assert all(st.get("note") == spec.DUP_NOTE for st in stats if st.get("dup"))
        # titles: one word, inside the top-right corner
        for ax, title in zip(axes, ("Mug", "Push-T", "Rope")):
            texts = [t for t in ax.texts if not isinstance(t, Annotation)]
            assert [t.get_text() for t in texts] == [title]
            t = texts[0]
            assert t.get_ha() == "right" and t.get_va() == "top" and t.get_position() == (1.0, 1.0)
            assert t.get_fontsize() == s["style"]["panel_title_fontsize"]
        # shared y range and ticks
        assert {ax.get_ylim() for ax in axes} == {(0.0, 100.0)}
        assert len({tuple(ax.get_yticks()) for ax in axes}) == 1
        assert not any("no data" in w for w in warns)
    finally:
        render.plt.close(fig)


def test_fraction_y_mode_labels_zero_to_one():
    """y_mode=fraction keeps the percent tick positions and labels them 0..1 without trailing zeros."""
    for step, want in ((20, ["0", "0.2", "0.4", "0.6", "0.8", "1"]), (25, ["0", "0.25", "0.5", "0.75", "1"]),
                       (50, ["0", "0.5", "1"])):
        fig, ax = render.plt.subplots()
        try:
            ax.set_ylim(0, 100)
            render._y_formatter(ax, {"y_mode": "fraction", "ytick_step": step, "out_of": 20})
            fig.canvas.draw()
            got = [t.get_text() for t in ax.get_yticklabels() if 0 <= t.get_position()[1] <= 100]
        finally:
            render.plt.close(fig)
        assert got == want, (step, got)
    for kind, rows in spec.controls()["style_by_kind"].items():
        y = [r for r in rows if r.get("key") == "y_mode"]
        assert y and "fraction" in [o["value"] for o in y[0]["options"]], kind


def test_panel_titles_left_as_y_labels():
    """panel_title_loc=left: each panel title is that panel's y label, the y label is one shared figure label."""
    s = load_seed("mug_baselines_panels")
    s["style"].update(panel_title_loc="left", panel_title_fontsize=9, ylabel="Success rate (n=20)",
                      ylabel_fontsize=10, panel_ylabel="middle")
    assert spec.validate(s) == []
    fig, stats, warns = _draw(s)
    try:
        axes = fig.axes
        assert [ax.get_ylabel() for ax in axes] == ["Mug", "Push-T", "Rope"]
        assert {ax.yaxis.label.get_fontsize() for ax in axes} == {9}
        assert all(not [t for t in ax.texts if not isinstance(t, Annotation)] for ax in axes)   # no corner titles
        sup = fig._supylabel
        assert sup is not None and sup.get_text() == "Success rate (n=20)" and sup.get_fontsize() == 10
        fig.canvas.draw()                    # the shared label sits left of every panel label, without overlap
        r = fig.canvas.get_renderer()
        sup_box = sup.get_window_extent(r)
        assert all(sup_box.x1 <= ax.yaxis.label.get_window_extent(r).x0 for ax in axes)
    finally:
        render.plt.close(fig)

    s["style"]["panel_title_loc"] = "right"
    assert spec.validate(s) == []
    fig, stats, warns = _draw(s)
    try:
        axes = fig.axes
        assert [ax.get_ylabel() for ax in axes] == ["Mug", "Push-T", "Rope"]
        assert {ax.yaxis.get_label_position() for ax in axes} == {"right"}
        assert {ax.yaxis.label.get_rotation() for ax in axes} == {90} == {fig._supylabel.get_rotation()}
        assert fig._supylabel.get_text() == "Success rate (n=20)"
        fig.canvas.draw()                    # titles right of the plot area, inside the figure; shared label left
        r = fig.canvas.get_renderer()
        for ax in axes:
            lab = ax.yaxis.label.get_window_extent(r)
            assert lab.x0 >= ax.get_window_extent(r).x1 and lab.x1 <= fig.bbox.x1 + 0.5
        assert fig._supylabel.get_window_extent(r).x1 <= min(ax.get_window_extent(r).x0 for ax in axes)
    finally:
        render.plt.close(fig)


def _bar_boxes_pt(fig):
    """[(x0, x1) in points] of the drawn bars, per axes, left to right."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    k = 72 / fig.dpi
    out = []
    for ax in fig.axes:
        boxes = [p.get_window_extent(r) for p in ax.patches
                 if isinstance(p, Rectangle) and p.get_width() < 1 and p.get_height() > 0]
        out.append(sorted((b.x0 * k, b.x1 * k) for b in boxes))
    return out


def test_bar_width_and_gap_in_points():
    """bar_width_pt / bar_gap_pt give exact sizes in points, in single and stacked charts."""
    for name in ("mug_ours_auxq2_levels_short", "mug_baselines_panels"):
        s = load_seed(name)
        s["style"].update(bar_width_pt=6, bar_gap_pt=1.5)
        assert spec.validate(s) == []
        fig, stats, warns = _draw(s)
        try:
            per_ax = _bar_boxes_pt(fig)
        finally:
            render.plt.close(fig)
        widths = [x1 - x0 for boxes in per_ax for x0, x1 in boxes]
        assert widths and all(abs(w - 6) < 0.02 for w in widths), (name, widths)
        # neighbours inside a group are 1.5 pt apart (the Mug Easy group alone has all 5 bars)
        bars = per_ax[0]
        gaps = [b[0] - a[1] for a, b in zip(bars, bars[1:])]
        assert sum(abs(g - 1.5) < 0.02 for g in gaps) >= 4, (name, gaps)
        assert not any("overlap" in w for w in warns), warns
    # width only: bars keep their ratio centres; too wide warns
    s = load_seed("mug_ours_auxq2_levels_short")
    s["style"].update(bar_width_pt=4)
    fig, _, warns = _draw(s)
    try:
        assert all(abs((x1 - x0) - 4) < 0.02 for x0, x1 in _bar_boxes_pt(fig)[0])
    finally:
        render.plt.close(fig)
    s["style"].update(bar_width_pt=40)
    fig, _, warns = _draw(s)
    render.plt.close(fig)
    assert any("bars overlap" in w for w in warns), warns


def test_dup_interval_colors():
    s = load_seed("mug_baselines_panels")
    s["style"]["dup_ci_color"] = "#00FF00"
    fig, stats, _ = _draw(s)
    try:
        greens = [sum(to_hex(c.get_colors()[0]) == "#00ff00" for c in ax.collections if len(c.get_colors()))
                  for ax in fig.axes]
        reds = [sum(to_hex(c.get_colors()[0]) == "#cc3311" for c in ax.collections if len(c.get_colors()))
                for ax in fig.axes]
    finally:
        render.plt.close(fig)
    # errorbar: a LineCollection for the line and one per cap row -> count intervals by the vertical collections
    assert greens[0] > 0 and greens[1] > 0 and greens[2] > 0
    assert greens[0] / greens[1] == 3 and greens[2] / greens[1] == 2, greens      # Mug 3, Push-T 1, Rope 2
    assert reds[0] > 0 and reds[1] == reds[2] == 0                                   # Classical partial (3/5, 3/10)
    # dup wins over partial
    s2 = load_seed("mug_baselines_panels")
    s2["data"]["panels"][0]["arms"][1]["counts"][0] = [3, 5, {"dup": True}]
    fig, stats, _ = _draw(s2)
    try:
        reds2 = sum(to_hex(c.get_colors()[0]) == "#cc3311" for c in fig.axes[0].collections if len(c.get_colors()))
    finally:
        render.plt.close(fig)
    assert reds2 == reds[0] // 2


def test_notes_only_when_a_yellow_interval_is_drawn():
    s = load_seed("mug_baselines_panels")
    assert render.preview(s, dpi=40)["notes"] == [spec.DUP_NOTE]
    s["style"]["ci"] = "none"
    assert render.preview(s, dpi=40)["notes"] == []
    assert render.preview(load_seed("mug_ours_auxq2_levels_short"), dpi=40)["notes"] == []


def test_arm_order_modes():
    names = ["A", "B", "C", "D", "E"]
    cells = [[(10, 20)], [(5, 10), (10, 10)], [(7, 15)], [], [(14, 20), (0, 20)]]
    # pooled: B 15/20, A 10/20, C 7/15, E 14/40, D no data (last)
    f = lambda **ao: [names[i] for i in render.arm_order({"arm_order": ao or None}, names, cells)]
    assert f() == names
    assert f(rest="pooled") == ["B", "A", "C", "E", "D"]
    assert f(fixed=["E", "A"], rest="pooled") == ["E", "A", "B", "C", "D"]
    # mean: B 0.75, A 0.5, C 0.467, E 0.35
    assert f(rest="mean") == ["B", "A", "C", "E", "D"]
    assert f(rest="manual", manual=["D", "C"]) == ["D", "C", "A", "B", "E"]
    assert f(fixed=["C"], rest="as_listed") == ["C", "A", "B", "D", "E"]
    # ties keep the listed order (exact fractions: 7/20 == 14/40)
    assert [names[i] for i in render.arm_order({"arm_order": {"rest": "pooled"}}, names,
                                                [[(7, 20)], [(14, 40)], [(1, 2)], [(7, 20)], []])] == \
        ["C", "A", "B", "D", "E"]


def test_level_sort_and_pin():
    lv = ["Nom", "a", "b", "c"]
    arms = [{"name": "x", "counts": [[16, 20], [10, 20], None, [15, 20]]},
            {"name": "y", "counts": [None, [10, 20], None, [5, 20]]}]
    L, A = render._sort_levels({"level_sort": "desc", "level_pin_first": 1}, lv, arms)
    assert L == ["Nom", "a", "c", "b"] and A[1]["counts"] == [None, [10, 20], [5, 20], None]   # a, c tie 20/40: stable
    L, _ = render._sort_levels({"level_sort": "asc", "level_pin_first": 0}, lv, arms)
    assert L == ["a", "c", "Nom", "b"]
    assert render._sort_levels({"level_sort": "none"}, lv, arms) == (lv, arms)


# --------------------------------------------------------------------------- validation and YAML
def test_count_formats():
    assert spec.count_parts([3, 4]) == (3, 4, False)
    assert spec.count_parts([3, 4, {"dup": True}]) == (3, 4, True)
    assert spec.count_parts({"k": 3, "n": 4, "dup": True}) == (3, 4, True)
    assert spec.count_parts(None) is None
    s = load_seed("mug_ours_auxq2_levels_short")

    def errs(c):
        t = copy.deepcopy(s)
        t["data"]["arms"][0]["counts"][0] = c
        return spec.validate(t)
    assert errs([16, 20, {"dup": True}]) == [] and errs({"k": 16, "n": 20, "dup": True}) == []
    assert any("dup" in e for e in errs([16, 20, {"dup": "yes"}]))
    assert any("third" in e for e in errs([16, 20, {"x": 1}]))
    assert any("outside 0..20" in e for e in errs({"k": 21, "n": 20}))
    assert any("{k, n" in e for e in errs({"k": 1}))
    # the two spellings render the same picture
    a, b = copy.deepcopy(s), copy.deepcopy(s)
    a["data"]["arms"][0]["counts"][0] = [16, 20, {"dup": True}]
    b["data"]["arms"][0]["counts"][0] = {"k": 16, "n": 20, "dup": True}
    assert render.preview_png(a, dpi=60) == render.preview_png(b, dpi=60)
    assert render.preview_png(a, dpi=60) != render.preview_png(s, dpi=60)


def test_panel_and_order_validation():
    good = load_seed("mug_baselines_panels")
    assert spec.validate(good) == []

    def errs(mut):
        t = copy.deepcopy(good)
        mut(t)
        return spec.validate(t)
    assert any("not in data.arms" in e for e in errs(lambda t: t["data"]["panels"][0]["arms"][0].update(name="Nope")))
    assert any("counts" in e for e in errs(lambda t: t["data"]["panels"][1]["arms"][0].update(counts=[[1, 2]])))
    assert any("levels" in e for e in errs(lambda t: (t["data"].pop("levels"))))
    assert errs(lambda t: t["data"]["panels"][2].update(levels=["Nominal", "Easy", "Medium", "Hard"])) != []
    assert errs(lambda t: [p.update(levels=["A", "B", "C"]) for p in t["data"]["panels"]]) == []
    assert any("arm_order.fixed" in e for e in errs(lambda t: t["style"]["arm_order"].update(fixed=["Ours", "x"])))
    assert any("arm_order.rest" in e for e in errs(lambda t: t["style"]["arm_order"].update(rest="best")))
    assert any("unknown key" in e for e in errs(lambda t: t["data"]["panels"][0].update(colour="x")))
    assert any("only grouped_bars" in e for e in errs(lambda t: t.update(kind="lines", style={})))
    assert any("level_colors" in e for e in errs(lambda t: t["style"].update(level_colors={"Nominal": "nocolor"})))
    # without a shared arm list each arm needs a color on some panel entry
    t = copy.deepcopy(good)
    del t["data"]["arms"]
    assert any("no color" in e for e in spec.validate(t))
    for p in t["data"]["panels"]:
        for a in p["arms"]:
            a["color"] = "tol-blue"
    assert spec.validate(t) == []
    # an arm left out of a panel is n/a there (same picture as listing it with nulls)
    u = copy.deepcopy(good)
    u["data"]["panels"][1]["arms"] = u["data"]["panels"][1]["arms"][:1]
    assert render.preview_png(u, dpi=50) == render.preview_png(good, dpi=50)
    # a lines spec rejects bar-only style keys
    ln = load_seed("difficulty_by_task")
    ln["style"]["group_width"] = 0.5
    assert any("unknown style key for kind lines" in e for e in spec.validate(ln))


def test_yaml_roundtrip_and_flow_counts():
    for name in ("mug_baselines_panels", "difficulty_by_task", "mug_conditions"):
        s = load_seed(name)
        text = spec.dumps(s)
        back = yaml.safe_load(text)
        assert back["data"] == s["data"], name
        assert spec.validate(back) == []
    text = spec.dumps(load_seed("mug_baselines_panels"))
    assert "counts: [[16, 20, {dup: true}], [12, 20, {dup: true}], [9, 20]]" in text
    style_keys = [l.strip().split(":")[0] for l in text.split("style:\n")[1].splitlines() if l.startswith("  ") and not l.startswith("   ")]
    assert style_keys == list(spec.defaults()), "style keys in table order"
    lt = spec.dumps(load_seed("difficulty_by_task"))
    assert [l.strip().split(":")[0] for l in lt.split("style:\n")[1].splitlines()] == list(spec.defaults("lines"))


def test_controls_by_kind():
    c = spec.controls(kind="lines")
    assert c["kind"] == "lines" and [r["key"] for r in c["style"]] == list(spec.defaults("lines"))
    assert set(c["style_by_kind"]) == set(spec.KINDS)
    assert [r["key"] for r in c["style_by_kind"]["grouped_bars"]] == list(spec.defaults())
    assert [r["key"] for r in spec.controls()["style"]] == list(spec.defaults())
    by = {r["key"]: r for r in spec.controls()["style"]}
    assert by["dup_ci_color"]["default"] == "#DDAA33" and by["dup_ci_color"]["type"] == "color"
    assert by["arm_order"]["type"] == "json" and by["arm_order"]["nullable"] and "example" in by["arm_order"]
    assert "dup" in c["data_schema"]["count"] and "panels" in c["data_schema"]
    assert spec.validate({**load_seed("difficulty_by_task"), "style": spec.defaults("lines")}) == []


def test_seeds_validate_and_render():
    for f in sorted(SEEDS.glob("*.yaml")):
        s = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert s["id"] == f.stem
        assert spec.validate(s) == [], (f.name, spec.validate(s))
        if (s.get("data") or {}).get("source", "inline") == "inline":
            assert render.preview_png(s, dpi=40)[:4] == b"\x89PNG"


# --------------------------------------------------------------------------- every new key does something
PANEL_CHANGES = {"panel_gap": 30, "share_x_labels": True, "panel_title_loc": "above left", "panel_title_fontsize": 6,
                 "panel_ylabel": "middle"}


def test_panel_keys_are_honoured():
    base = render.preview_png(load_seed("mug_baselines_panels"), dpi=60)
    for k, v in PANEL_CHANGES.items():
        s = load_seed("mug_baselines_panels")
        s["style"][k] = v
        assert spec.validate(s) == [], (k, spec.validate(s))
        assert render.preview_png(s, dpi=60) != base, f"style.{k} = {v!r} did not change the panels image"
    s = load_seed("mug_baselines_panels")
    s["style"]["panel_title_loc"] = "none"
    fig, _, _ = _draw(s)
    try:
        assert not any(not isinstance(t, Annotation) for ax in fig.axes for t in ax.texts)
    finally:
        render.plt.close(fig)


LINES_CHANGES = {
    "theme": "acme_tnr", "font_family": "DejaVu Serif", "width_pt": 300, "height_pt": 140, "line_width": 2.0,
    "marker": "o", "marker_size": 5, "marker_edge_width": 0.3, "gap_mode": "break",
    "arm_order": {"fixed": ["Rope"], "rest": "as_listed"}, "legend_loc": "right", "legend_align": "center",
    "legend_cols": 1, "legend_fontsize": 6, "legend_handlelength": 3.0, "legend_pad": 1.0, "tick_fontsize": 8,
    "xtick_fontsize": 6, "ylabel": "Rate", "ylabel_fontsize": 7, "y_mode": "percent", "out_of": 10,
    "ytick_step": 25, "x_margin": 0.1, "xlabel_pad": 8, "grid_color": "#FF0000", "grid_width": 1.0,
    "bottom_spine": "theme", "ci": "wilson", "palette": "paired", "fill_darken": 1.0, "transparent": True,
}
# visible only with intervals on: (setup, value)
LINES_COMBOS = {
    "ci_style": ({"ci": "wilson"}, "band"), "ci_level": ({"ci": "wilson"}, 0.8),
    "ci_linewidth": ({"ci": "wilson"}, 1.5), "ci_capsize": ({"ci": "wilson"}, 3.5),
    "band_alpha": ({"ci": "wilson", "ci_style": "band"}, 0.5),
    "partial_below": ({"ci": "wilson", "_partial": True}, None),
    "partial_ci_color": ({"ci": "wilson", "_partial": True}, "#00FF00"),
    "dup_ci_color": ({"ci": "wilson"}, "#00FF00"),
}


def test_lines_keys_are_honoured():
    assert set(LINES_CHANGES) | set(LINES_COMBOS) == set(spec.defaults("lines")), \
        set(spec.defaults("lines")) ^ (set(LINES_CHANGES) | set(LINES_COMBOS))
    base = render.preview_png(load_seed("difficulty_by_task"), dpi=80)
    for k, v in LINES_CHANGES.items():
        s = load_seed("difficulty_by_task")
        s["style"][k] = v
        assert spec.validate(s) == [], (k, spec.validate(s))
        assert render.preview_png(s, dpi=80) != base, f"style.{k} = {v!r} did not change the lines image"
    for k, (setup, v) in LINES_COMBOS.items():
        s = load_seed("difficulty_by_task")
        for sk, sv in setup.items():
            if sk == "_partial":
                s["data"]["arms"][1]["counts"][1] = [1, 10]
            else:
                s["style"][sk] = sv
        ref = render.preview_png(s, dpi=80)
        s["style"][k] = v
        assert spec.validate(s) == [], (k, spec.validate(s))
        assert render.preview_png(s, dpi=80) != ref, f"style.{k} = {v!r} did not change the lines image"


def test_lines_join_vs_break_and_stats():
    s = load_seed("difficulty_by_task")
    fig, stats, warns = _draw(s)
    try:
        lines = [l for l in fig.axes[0].get_lines() if l.get_label() in ("Mug", "Push T", "Rope")]
        assert [len(l.get_xdata()) for l in lines] == [4, 3, 3]       # joined across the missing Medium
        leg = fig.axes[0].get_legend()
        assert [t.get_text() for t in leg.get_texts()] == ["Mug", "Push T", "Rope"]
        assert {to_hex(l.get_color()).upper() for l in lines} == {"#305377", "#A74753", "#185F24"}
    finally:
        render.plt.close(fig)
    assert len(stats) == 10 and all(st["lo"] is None for st in stats)
    s["style"]["gap_mode"] = "break"
    fig, _, _ = _draw(s)
    try:
        assert [len(l.get_xdata()) for l in fig.axes[0].get_lines()][:3] == [4, 4, 4]
    finally:
        render.plt.close(fig)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
