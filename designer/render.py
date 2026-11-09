"""Render figure specs with paperplot (see CONTRACT.md section 2).

`grouped_bars` is a spec-driven port of figures/mug_view_mix_ab.py::chart() (Wilson intervals and their ink from
figures/mug_best_bars.py): for the seed spec it reproduces figures/out/mug_ours_auxq2_levels_short.png pixel for pixel.
Every style key of the spec is honoured; see spec.controls() for what each one does. It also draws stacked panels
(data.panels: one legend, one global arm order), group sorting / recoloring / angled labels (for the seed
mug_conditions it reproduces figures/out/mug_conditions.png, figures/mug_conditions.py), and colors the interval of
a count flagged {dup: true} in style.dup_ci_color.
`lines` (one line per arm over categorical levels) generalises figures/difficulty.py::by_task: the seed
difficulty_by_task with the old counts reproduces figures/out/difficulty_by_task.png.
render()/preview() results carry `notes` (legend-free explanations, e.g. what a yellow interval means).
"""
from __future__ import annotations

import copy
import io
import math
from fractions import Fraction
import threading
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg", force=True)        # headless: renders run in server threads

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter, MultipleLocator  # noqa: E402
from matplotlib.transforms import offset_copy  # noqa: E402
import numpy as np  # noqa: E402

import paperplot as pp  # noqa: E402
from paperplot import theme as _theme  # noqa: E402
from paperplot.bars import _darken  # noqa: E402

from . import config  # noqa: E402
from . import spec as _spec  # noqa: E402

class FontUnavailable(ValueError):
    """The requested font family (style.font_family, or the theme's first family) is not available to matplotlib.
    Rendering with a fallback font would silently produce a figure that does not match the paper, so we refuse."""

    status = 422


_LOCK = threading.RLock()               # matplotlib global state (rcParams, current theme) is shared
# z for the Wilson interval; 0.95 uses 1.96 exactly as mug_best_bars.CI_Z, so the seed figure is pixel-identical
_Z = {0.8: 1.2815515655446004, 0.9: 1.6448536269514722, 0.95: 1.96, 0.99: 2.5758293035489004}
PNG_DPI = 600
_registered_fonts: set[str] = set()


# --------------------------------------------------------------------------- statistics
def _z(level: float) -> float:
    for k, v in _Z.items():
        if abs(level - k) < 1e-9:
            return v
    from scipy.stats import norm
    return float(norm.ppf((1 + level) / 2))


def wilson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval (fractions), as figures/mug_best_bars.wilson."""
    z = _z(level)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def interval(k: int, n: int, method: str, level: float):
    if method == "wilson":
        return wilson(k, n, level)
    if method == "bayes":
        lo, hi = pp.stats.rate_interval(k, n, level=level)
        return float(lo), float(hi)
    return None, None


# --------------------------------------------------------------------------- data
def _key_label(entry):
    if isinstance(entry, dict):
        return entry.get("key"), entry.get("label", str(entry.get("key")))
    return entry, str(entry)


def resolve_data(spec: dict) -> dict:
    """{levels, arms: [{name, color, counts, hatch?}], warnings}. Inline data is returned as is; a dataset is read
    with datastore.load_table and mapped through data.query:
      level / arm / successes / trials : column names
      filter : {column: value or [values]}  (compared as strings)
      levels : [value | {key, label}]   default: values in order of appearance
      arms   : [{key, name, color, hatch?}]  default: every arm value, theme categorical colors
    Several rows for one (level, arm) are summed (with a warning); a missing pair is null (drawn as n/a)."""
    data = spec.get("data") or {}
    src = data.get("source", "inline")
    if src == "inline":
        if data.get("panels"):
            return _resolve_panels(data)
        return {"levels": list(data.get("levels") or []),
                "arms": [dict(a) for a in data.get("arms") or []], "warnings": []}
    if src != "dataset":
        raise ValueError(f"data.source {src!r} must be inline or dataset")
    from . import datastore           # lazy: written by the data/git builder
    q = data.get("query") or {}
    df = datastore.load_table(data["dataset"])
    for col in (q.get("level"), q.get("arm"), q.get("successes"), q.get("trials"), *(q.get("filter") or {})):
        if col not in df.columns:
            raise ValueError(f"dataset {data['dataset']!r} has no column {col!r} (columns: {', '.join(map(str, df.columns))})")
    for col, val in (q.get("filter") or {}).items():
        vals = [str(v) for v in val] if isinstance(val, list) else [str(val)]
        df = df[df[col].astype(str).isin(vals)]
    lev_col, arm_col = q["level"], q["arm"]
    levels = q.get("levels") or list(dict.fromkeys(df[lev_col].astype(str)))
    arms = q.get("arms")
    warns = []
    if not arms:
        names = list(dict.fromkeys(df[arm_col].astype(str)))
        cat = _theme.load((spec.get("style") or {}).get("theme", "acme")).categorical
        arms = [{"key": a, "name": a, "color": cat[i % len(cat)]} for i, a in enumerate(names)]
    out_arms = []
    lev_str = df[lev_col].astype(str)
    arm_str = df[arm_col].astype(str)
    for arm in arms:
        counts = []
        for lev in levels:
            key, label = _key_label(lev)
            rows = df[(lev_str == str(key)) & (arm_str == str(arm["key"]))]
            if rows.empty:
                counts.append(None)
                continue
            if len(rows) > 1:
                warns.append(f"{arm['key']} @ {key}: {len(rows)} rows summed")
            counts.append([int(rows[q["successes"]].astype(int).sum()), int(rows[q["trials"]].astype(int).sum())])
        a = {"name": arm.get("name", str(arm["key"])), "color": arm.get("color", "#808080"), "counts": counts}
        if arm.get("hatch"):
            a["hatch"] = arm["hatch"]
        out_arms.append(a)
    return {"levels": [_key_label(l)[1] for l in levels], "arms": out_arms, "warnings": warns}


def _resolve_panels(data: dict) -> dict:
    """Inline data with `panels`: {levels, arms (the shared list: name, color, hatch?), panels: [{title, levels,
    arms}], warnings}. Every panel lists every shared arm, in shared order; an arm missing from a panel is null
    (n/a) at each of its levels. Shared arms = data.arms, else every panel's arms in order of first appearance."""
    warns = []
    top_levels = list(data.get("levels") or [])
    shared = []
    for a in data.get("arms") or []:
        if any(c is not None for c in a.get("counts") or []):
            warns.append(f"data.arms {a.get('name')!r}: counts are ignored when data.panels is set "
                         "(each panel has its own)")
        shared.append({k: a[k] for k in ("name", "color", "hatch") if k in a})
    if not shared:
        seen = {}
        for p in data["panels"]:
            for a in p.get("arms") or []:
                entry = seen.setdefault(a["name"], {"name": a["name"]})
                for k in ("color", "hatch"):
                    if k in a and k not in entry:
                        entry[k] = a[k]
        shared = list(seen.values())
    panels = []
    for p in data["panels"]:
        levels = list(p.get("levels") or top_levels)
        by = {a["name"]: a for a in p.get("arms") or []}
        arms = []
        for s in shared:
            a = dict(s)
            src = by.get(s["name"])
            a["counts"] = list(src.get("counts") or []) if src else [None] * len(levels)
            if src and src.get("hatch") and "hatch" not in a:
                a["hatch"] = src["hatch"]
            arms.append(a)
        panels.append({"title": p.get("title"), "levels": levels, "arms": arms})
    return {"levels": top_levels or panels[0]["levels"], "arms": shared, "panels": panels, "warnings": warns}


# --------------------------------------------------------------------------- theme set-up
def _register(files) -> None:
    """Register theme font files once (paperplot.use would re-add them on every render, slowing findfont)."""
    for f in files or []:
        path = Path(f) if Path(f).is_absolute() else pp.style.FONTS_DIR / f
        key = str(path.resolve())
        if key not in _registered_fonts and path.exists():
            font_manager.fontManager.addfont(key)
            _registered_fonts.add(key)


def _register_bundled() -> None:
    for f in _spec.FONTS_DIR.glob("*.[ot]tf"):       # every bundled font is selectable in the font dropdown
        _register([f])


def font_file(family: str) -> str | None:
    """The font file matplotlib uses for `family`, or None if it would have to fall back to another family."""
    try:
        return font_manager.findfont(font_manager.FontProperties(family=family), fallback_to_default=False)
    except ValueError:
        return None


def _primary_family(t, fam_key: str, requested: str | None) -> str | None:
    return requested or (list(t.font.get(fam_key) or []) or [None])[0]


def _require_font(family: str | None, requested: bool, theme: str) -> None:
    if not family:
        return
    if font_file(family) is None:
        where = "style.font_family" if requested else f"the first font family of theme {theme!r}"
        raise FontUnavailable(
            f"font {family!r} ({where}) is not available to matplotlib on this machine, so the figure would be "
            "drawn in a fallback font that does not match the paper. Install the font (on Linux/Docker: put it in "
            "~/.fonts or /usr/share/fonts and rebuild the matplotlib font cache), or choose another font/theme.")


def _activate(style: dict, warns: list):
    """Load the spec's theme, apply the style's overrides to it, and pp.use() it. Returns the Theme.

    Raises FontUnavailable when the requested font_family, or the theme's first font family, is not resolvable
    (never a silent fallback). The theme's math fallback (STIX for glyphs Roboto Slab lacks, e.g. pi) is intended
    and untouched."""
    t = _theme.load(style["theme"])                  # a fresh object: safe to mutate
    t.swatches = _spec.swatches(style["theme"])      # + other themes' swatch names, so acme names stay valid
    t.font = copy.deepcopy(t.font)
    _register(t.font.pop("files", None))
    _register_bundled()
    fam_key = "serif" if t.font.get("family", "serif") == "serif" else "sans"
    fam = style.get("font_family")
    _require_font(_primary_family(t, fam_key, fam), bool(fam), style["theme"])
    if fam:
        t.font[fam_key] = [fam] + [f for f in t.font.get(fam_key, []) if f != fam]
    t.bars = dict(t.bars)
    for skey, bkey in (("fill_darken", "fill_darken"), ("edge_darken", "edge_darken"), ("bar_fill", "bar_width"),
                       ("na_label", "missing_label")):
        if skey in style:                            # kinds without bars (lines) lack some of these keys
            t.bars[bkey] = style[skey]
    t.bars["labels"] = "none"
    if style.get("edge_width") is not None:
        t.bars["edge_width"] = style["edge_width"]
    t.figure = dict(t.figure, png=True, png_dpi=PNG_DPI, transparent=bool(style.get("transparent")))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pp.use(t)
    msgs = [str(w.message) for w in caught]
    font_msgs = [m for m in msgs if "not installed" in m and "font" in m]
    if font_msgs:                                    # belt and braces: paperplot itself saw a missing font
        raise FontUnavailable("; ".join(font_msgs))
    warns.extend(msgs)
    return t


def check_fonts() -> dict:
    """{ok, themes: [{theme, family, ok, file}]}: whether each theme's primary font family resolves (no fallback)."""
    rows = []
    with _LOCK:
        _register_bundled()
        for name in _spec.themes():
            try:
                t = _theme.load(name)
                _register((t.font or {}).get("files"))
                fam_key = "serif" if t.font.get("family", "serif") == "serif" else "sans"
                fam = _primary_family(t, fam_key, None)
                path = font_file(fam) if fam else None
                rows.append({"theme": name, "family": fam, "ok": bool(path),
                             "file": Path(path).name if path else None})
            except Exception as e:  # a broken theme file is reported, not fatal
                rows.append({"theme": name, "family": None, "ok": False, "file": None,
                             "error": f"{type(e).__name__}: {e}"})
    return {"ok": all(r["ok"] for r in rows), "themes": rows}


def _arm_colors(t, style, arms, warns):
    pal = style.get("palette")
    if pal:
        colors = _spec.palettes(style["theme"]).get(pal)
        if not colors:
            warns.append(f"palette {pal!r} not in theme {style['theme']}; using per-arm colors")
        else:
            if len(arms) > len(colors):
                warns.append(f"palette {pal!r} has {len(colors)} colors for {len(arms)} arms; colors repeat")
            return [t.resolve(colors[i % len(colors)]) for i in range(len(arms))]
    return [t.resolve(a["color"], f"arm {a.get('name')!r} color") for a in arms]


# --------------------------------------------------------------------------- ordering
def _cells(arm):
    """[(k, n), ...] of an arm's evaluated cells."""
    return [(p[0], p[1]) for p in map(_spec.count_parts, arm.get("counts") or []) if p is not None]


def arm_order(style: dict, names: list, cell_lists: list) -> list:
    """Indices of `names` in drawing order, per style.arm_order (null = as listed). cell_lists[i] = [(k, n), ...]
    of arm i over EVERY panel and level, so the order is one global order. fixed names come first in the given
    order; the rest by `rest`: pooled = sum k / sum n, mean = mean of k/n (best first; arms without data last;
    ties keep the listed order), manual = the manual list then the others as listed, as_listed."""
    ao = style.get("arm_order")
    idx = list(range(len(names)))
    if not ao:
        return idx
    pos = {n: i for i, n in enumerate(names)}
    fixed = [pos[n] for n in ao.get("fixed") or [] if n in pos]
    rest = [i for i in idx if i not in fixed]
    mode = ao.get("rest", "as_listed")
    if mode in ("pooled", "mean"):
        def key(i):
            cells = cell_lists[i]
            if not cells:
                return (1, Fraction(0))
            if mode == "pooled":
                r = Fraction(sum(k for k, _ in cells), sum(n for _, n in cells))
            else:
                r = sum(Fraction(k, n) for k, n in cells) / len(cells)
            return (0, -r)
        rest = sorted(rest, key=key)              # stable: ties keep the listed order
    elif mode == "manual":
        man = [pos[n] for n in ao.get("manual") or [] if n in pos and pos[n] in rest]
        rest = man + [i for i in rest if i not in man]
    return fixed + rest


def _sort_levels(style, levels, arms):
    """(levels, arms) with the x groups sorted by pooled rate (style.level_sort), the first level_pin_first kept."""
    mode = style.get("level_sort", "none")
    if mode not in ("desc", "asc"):
        return levels, arms
    pin = min(int(style.get("level_pin_first") or 0), len(levels))

    def key(j):
        cells = [p for p in (_spec.count_parts(a["counts"][j]) for a in arms) if p is not None]
        if not cells:
            return (1, Fraction(0))
        r = Fraction(sum(c[0] for c in cells), sum(c[1] for c in cells))
        return (0, -r if mode == "desc" else r)
    perm = list(range(pin)) + sorted(range(pin, len(levels)), key=key)
    return [levels[j] for j in perm], [dict(a, counts=[a["counts"][j] for j in perm]) for a in arms]


def _y_formatter(ax, style):
    ax.yaxis.set_major_locator(MultipleLocator(style["ytick_step"]))
    if style["y_mode"] == "count":
        out_of = style.get("out_of")
        N = int(out_of) if out_of and float(out_of) == int(out_of) else (out_of or 20)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v * N / 100:.0f}/{N}"))
    else:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}"))


def _legend(ax, handles, names, style, default_align="center", columnspacing=None):
    """Legend 'above' (lower edge on the axes top, aligned by legend_align) or 'right'; None for 'none'."""
    if not handles or style["legend_loc"] not in ("above", "right"):
        return None
    fs = style["legend_fontsize"]
    box = style.get("legend_box_fontsize")
    kw = dict(frameon=False, fontsize=box if box is not None else fs, borderaxespad=style.get("legend_pad", 0.0))
    ncol = style.get("legend_cols")
    kw["ncol"] = int(ncol) if ncol is not None else len(handles)
    if style.get("legend_handlelength") is not None:
        kw["handlelength"] = style["legend_handlelength"]
    if columnspacing is not None:
        kw["columnspacing"] = columnspacing
    if style["legend_loc"] == "above":
        align = style.get("legend_align", default_align)
        leg = ax.legend(handles, names, loc="lower center" if align == "center" else f"lower {align}",
                        bbox_to_anchor=({"left": 0.0, "center": 0.5, "right": 1.0}[align], 1.0), **kw)
    else:
        leg = ax.legend(handles, names, loc="center left", bbox_to_anchor=(1.02, 0.5), **kw)
    if box is not None:
        for text in leg.get_texts():
            text.set_fontsize(fs)
    return leg


def _where(arm, level, panel):
    lv = str(level).replace(chr(10), " ")
    return f"{panel}: {arm} @ {lv}" if panel else f"{arm} @ {lv}"


def _ci_color_for(style, n, dup, normal, partial_color, dup_color):
    partial = style.get("partial_below") is not None and n < style["partial_below"]
    return (dup_color if dup else partial_color if partial else normal), partial


# --------------------------------------------------------------------------- grouped bars
def _bars_panel(ax, t, style, levels, arms, base_colors, warns, stats, ylabel, panel):
    """Bars, group colors and intervals of one panel (the first half of the original single-axes chart)."""
    parts = [[_spec.count_parts(c) for c in a["counts"]] for a in arms]
    # a None count = not evaluated; pp.bar marks it with na_label and draws no bar
    series = {a["name"]: [float("nan") if c is None else 100 * c[0] / c[1] for c in parts[i]]
              for i, a in enumerate(arms)}
    pp.bar(ax, levels, series, colors=base_colors, ylabel=ylabel or None, ylim=(0, 100), legend=None,
           group_width=style["group_width"])
    # pp.bar adds one Rectangle per drawn bar, arm by arm, level by level
    drawn = [p for p in ax.patches if isinstance(p, Rectangle)]
    present = [(i, j) for i in range(len(arms)) for j, c in enumerate(parts[i]) if c is not None]
    if len(drawn) != len(present):
        raise RuntimeError(f"expected {len(present)} bars from paperplot.bar, found {len(drawn)}")
    bar_at = dict(zip(present, drawn))
    edge_w = t.bars.get("edge_width", 0)
    for (i, j), bar in bar_at.items():
        if arms[i].get("hatch"):
            bar.set_hatch(arms[i]["hatch"])
    lc = style.get("level_colors") or {}
    if lc:          # as figures/mug_conditions.py: this fill as is, outline = gray as bright as the bar's own
        for (i, j), bar in bar_at.items():
            if levels[j] in lc:
                bar.set_facecolor(t.resolve(lc[levels[j]], f"style.level_colors[{levels[j]!r}]"))
                if edge_w:
                    e = bar.get_edgecolor()
                    v = max(e[:3])
                    bar.set_edgecolor((v, v, v, e[3]))
        for name in set(lc) - set(levels):
            warns.append(f"level_colors: no group {name!r}" + (f" in panel {panel!r}" if panel else ""))

    ci_color = t.resolve(style["ci_color"], "style.ci_color")
    partial_color = t.resolve(style["partial_ci_color"], "style.partial_ci_color")
    dup_color = t.resolve(style["dup_ci_color"], "style.dup_ci_color")
    out_of = style.get("out_of")
    count_mode = style["y_mode"] == "count"
    for j, level in enumerate(levels):           # level-major, as chart() draws its error bars
        for i, arm in enumerate(arms):
            c = parts[i][j]
            if c is None:
                continue
            k, n, dup = c
            lo, hi = interval(k, n, style["ci"], style["ci_level"])
            row = {"level": level, "arm": arm["name"], "k": k, "n": n, "rate": k / n, "lo": lo, "hi": hi}
            if panel is not None:
                row["panel"] = panel
            if dup:
                row.update(dup=True, note=_spec.DUP_NOTE)
            stats.append(row)
            color, partial = _ci_color_for(style, n, dup, ci_color, partial_color, dup_color)
            if partial:
                warns.append(f"{_where(arm['name'], level, panel)}: partial ({k}/{n})")
            if count_mode and out_of and n != out_of and not partial:
                warns.append(f"{_where(arm['name'], level, panel)}: {k}/{n} drawn at its rate on a /{out_of} axis")
            if lo is None:
                continue
            bar = bar_at[(i, j)]
            y = 100 * k / n
            ax.errorbar(bar.get_x() + bar.get_width() / 2, y, yerr=[[y - 100 * lo], [100 * hi - y]], fmt="none",
                        ecolor=color, elinewidth=style["ci_linewidth"],
                        capsize=style["ci_capsize"], capthick=style["ci_linewidth"], zorder=5)


def _bars_finish(fig, ax, t, style, levels, warns, *, multi=False, bottom=True, title=None):
    """Text sizes, divider, y ticks, label angle, panel title (the second half of the original chart)."""
    ytick = style.get("tick_fontsize")
    if ytick is not None:
        ax.tick_params(axis="y", labelsize=ytick)
    xtick = style.get("xtick_fontsize") if style.get("xtick_fontsize") is not None else ytick
    if xtick is not None:
        ax.tick_params(axis="x", labelsize=xtick)
    na_size = ytick if ytick is not None else matplotlib.rcParams["ytick.labelsize"]
    for text in ax.texts:          # the n/a mark: tick-label size
        text.set_fontsize(na_size)
    if style.get("ylabel_fontsize") is not None:
        ax.yaxis.label.set_fontsize(style["ylabel_fontsize"])
    d = style.get("divider_before")
    if d is not None:
        if not 0 < d < len(levels):
            warns.append(f"divider_before={d} is outside 1..{len(levels) - 1}; not drawn")
        elif style.get("divider_style", "ci") == "theme":     # exactly as paperplot.bar(dividers=[d - 1])
            dv = t.bars.get("divider") or {}
            w = dv.get("width", 0.5)
            dash = [x / w if matplotlib.rcParams["lines.scale_dashes"] else x for x in dv.get("dash", [3, 3])]
            ax.axvline(d - 0.5, color=t.resolve(dv.get("color", "gray"), "bars.divider.color"), linewidth=w,
                       dashes=dash, zorder=1.5)
        else:
            ax.axvline(d - 0.5, color=t.resolve(style["ci_color"], "style.ci_color"), linestyle=(0, (4, 3)),
                       linewidth=style["ci_linewidth"], zorder=0.5)
    _y_formatter(ax, style)
    rot = style.get("xtick_rotation") or 0
    if rot:
        for label in ax.get_xticklabels():   # angled, right end at the tick (figures/mug_conditions.py)
            label.set_rotation(rot)
            label.set_ha("right")
            label.set_rotation_mode("anchor")
    if multi and not bottom and style.get("share_x_labels"):
        ax.tick_params(axis="x", labelbottom=False)
    _panel_title(fig, ax, style, title)


def _panel_title(fig, ax, style, title):
    loc = style.get("panel_title_loc", "upper right")
    if not title or loc == "none":
        return
    fs = style.get("panel_title_fontsize")
    fs = fs if fs is not None else matplotlib.rcParams["axes.titlesize"]
    side = loc.split()[1]
    if loc.startswith("above"):
        ax.set_title(title, loc=side, fontsize=fs)
        return
    # inside the axes, in its top corner (a Text, not an Annotation: never taken for an n/a mark)
    x = {"left": 0.0, "center": 0.5, "right": 1.0}[side]
    dx = {"left": 2.0, "center": 0.0, "right": -2.0}[side]
    tr = offset_copy(ax.transAxes, fig=fig, x=dx, y=-1.0, units="points")
    ax.text(x, 1.0, title, transform=tr, ha=side, va="top", fontsize=fs, zorder=6)


def _panels_of(data):
    if data.get("panels"):
        return [dict(p) for p in data["panels"]], True
    return [{"title": None, "levels": data["levels"], "arms": data["arms"]}], False


def _grouped_bars(spec, style, data, warns):
    t = _activate(style, warns)
    panels, multi = _panels_of(data)
    shared = data["arms"]
    if not shared or any(not p["levels"] for p in panels):
        raise ValueError("nothing to plot: no levels or no arms")
    base_colors = _arm_colors(t, style, shared, warns)
    cells = [[c for p in panels for c in _cells(p["arms"][i])] for i in range(len(shared))]
    order = arm_order(style, [a["name"] for a in shared], cells)
    if order != list(range(len(shared))):        # one global order: every panel, every group
        shared = [shared[i] for i in order]
        base_colors = [base_colors[i] for i in order]
        panels = [dict(p, arms=[p["arms"][i] for i in order]) for p in panels]
    for p in panels:
        p["levels"], p["arms"] = _sort_levels(style, p["levels"], p["arms"])
    n_arms, n_p = len(shared), len(panels)
    if multi:
        fig, axes = pp.subplots(f"{style['width_pt']}pt", height=f"{style['height_pt']}pt", nrows=n_p, squeeze=False)
        axes = list(axes[:, 0])
        gap = style.get("panel_gap")
        if n_p > 1 and gap:      # constrained layout: each inner gap = hspace / nrows of the figure height
            fig.get_layout_engine().set(hspace=n_p * float(gap) / float(style["height_pt"]))
    else:
        fig, ax = pp.subplots(f"{style['width_pt']}pt", height=f"{style['height_pt']}pt")
        axes = [ax]
    ymode = style.get("panel_ylabel", "each")
    stats = []
    for pi, (ax, p) in enumerate(zip(axes, panels)):
        show_y = not multi or ymode == "each" or (ymode == "first" and pi == 0) or (ymode == "middle" and pi == n_p // 2)
        _bars_panel(ax, t, style, p["levels"], p["arms"], base_colors, warns, stats,
                    style["ylabel"] if show_y else None, p.get("title") if multi else None)
    edge_w = t.bars.get("edge_width", 0)
    fills = [_darken(c, style["fill_darken"]) for c in base_colors]
    edges = [_darken(f, style["edge_darken"]) if edge_w else "none" for f in fills]
    # legend in arm order; an arm with no data at any level (of any panel) keeps its slot but has no legend entry
    shown = [i for i in range(n_arms) if any(c is not None for p in panels for c in p["arms"][i]["counts"])]
    for i in sorted(set(range(n_arms)) - set(shown)):
        warns.append(f"arm {shared[i]['name']!r} has no data; left out of the legend")
    handles = [Patch(facecolor=fills[i], edgecolor=edges[i], linewidth=edge_w, hatch=shared[i].get("hatch"))
               for i in shown]
    names = [shared[i]["name"] for i in shown]
    _legend(axes[0] if style["legend_loc"] == "above" else axes[n_p // 2], handles, names, style,
            columnspacing=1.0)
    for pi, (ax, p) in enumerate(zip(axes, panels)):
        _bars_finish(fig, ax, t, style, p["levels"], warns, multi=multi, bottom=pi == n_p - 1,
                     title=p.get("title") if multi else None)
    _tidy_and_check(fig, axes, max(len(p["levels"]) for p in panels), warns)
    return fig, stats


# --------------------------------------------------------------------------- lines
def _lines(spec, style, data, warns):
    """One line per arm over categorical levels (figures/difficulty.py::by_task generalised)."""
    t = _activate(style, warns)
    rc = {}
    for skey, rkey in (("xtick_fontsize", "xtick.labelsize"), ("tick_fontsize", "ytick.labelsize"),
                       ("legend_fontsize", "legend.fontsize"), ("xlabel_pad", "xtick.major.pad")):
        if style.get(skey) is not None:          # pp.use() above resets every one of these on the next render
            rc[rkey] = style[skey]
    matplotlib.rcParams.update(rc)
    levels, arms = data["levels"], data["arms"]
    if not levels or not arms:
        raise ValueError("nothing to plot: no levels or no arms")
    base_colors = _arm_colors(t, style, arms, warns)
    order = arm_order(style, [a["name"] for a in arms], [_cells(a) for a in arms])
    arms = [arms[i] for i in order]
    base_colors = [base_colors[i] for i in order]
    fig, ax = pp.subplots(f"{style['width_pt']}pt", height=f"{style['height_pt']}pt")
    x = np.arange(len(levels))
    partial_color = t.resolve(style["partial_ci_color"], "style.partial_ci_color")
    dup_color = t.resolve(style["dup_ci_color"], "style.dup_ci_color")
    out_of = style.get("out_of")
    stats, handles, names = [], [], []
    for arm, base in zip(arms, base_colors):
        color = _darken(base, style["fill_darken"])
        parts = [_spec.count_parts(c) for c in arm["counts"]]
        ys = np.array([np.nan if c is None else 100 * c[0] / c[1] for c in parts])
        have = ~np.isnan(ys)
        if not have.any():
            warns.append(f"arm {arm['name']!r} has no data; left out of the legend")
            continue
        kw = dict(color=color, marker=None if style["marker"] == "none" else style["marker"],
                  markersize=style["marker_size"], linewidth=style["line_width"], label=arm["name"], clip_on=False,
                  zorder=3)
        if style.get("marker_edge_width") is not None:
            kw["markeredgewidth"] = style["marker_edge_width"]
        if style["gap_mode"] == "join":          # a missing level is joined straight across, not broken
            line, = ax.plot(x[have], ys[have], **kw)
        else:
            line, = ax.plot(x, ys, **kw)
        handles.append(line)
        names.append(arm["name"])
        los, his = [], []
        for j, c in enumerate(parts):
            if c is None:
                los.append(np.nan)
                his.append(np.nan)
                continue
            k, n, dup = c
            lo, hi = interval(k, n, style["ci"], style["ci_level"])
            row = {"level": levels[j], "arm": arm["name"], "k": k, "n": n, "rate": k / n, "lo": lo, "hi": hi}
            if dup:
                row.update(dup=True, note=_spec.DUP_NOTE)
            stats.append(row)
            ecolor, partial = _ci_color_for(style, n, dup, color, partial_color, dup_color)
            if partial:
                warns.append(f"{_where(arm['name'], levels[j], None)}: partial ({k}/{n})")
            if style["y_mode"] == "count" and out_of and n != out_of and not partial:
                warns.append(f"{_where(arm['name'], levels[j], None)}: {k}/{n} drawn at its rate on a /{out_of} axis")
            los.append(np.nan if lo is None else 100 * lo)
            his.append(np.nan if hi is None else 100 * hi)
            if lo is not None and style["ci_style"] == "whiskers":
                y = ys[j]
                ax.errorbar(x[j], y, yerr=[[y - 100 * lo], [100 * hi - y]], fmt="none", ecolor=ecolor,
                            elinewidth=style["ci_linewidth"], capsize=style["ci_capsize"],
                            capthick=style["ci_linewidth"], zorder=2.5, clip_on=False)
        if style["ci"] != "none" and style["ci_style"] == "band":
            lo_a, hi_a = np.array(los), np.array(his)
            sel = have if style["gap_mode"] == "join" else np.ones_like(have)
            ax.fill_between(x[sel], lo_a[sel], hi_a[sel], color=color, alpha=style["band_alpha"], linewidth=0,
                            zorder=2)
    ax.set_xticks(x, levels)
    xm = style["x_margin"]
    ax.set_xlim(-xm, len(levels) - 1 + xm)
    grid_color = style.get("grid_color")
    grid_color = t.resolve(grid_color, "style.grid_color") if grid_color is not None else matplotlib.rcParams["grid.color"]
    grid_width = style.get("grid_width")
    grid_width = grid_width if grid_width is not None else matplotlib.rcParams["grid.linewidth"]
    if style["bottom_spine"] == "grid":          # drawn like the other horizontal grid lines
        ax.spines["bottom"].set(color=grid_color, linewidth=grid_width)
    elif style["bottom_spine"] == "none":
        ax.spines["bottom"].set_visible(False)
    ax.set_ylim(0, 100)
    if style.get("ylabel"):
        if style.get("ylabel_fontsize") is not None:
            ax.set_ylabel(style["ylabel"], fontsize=style["ylabel_fontsize"])
        else:
            ax.set_ylabel(style["ylabel"])
    if style.get("grid_color") is not None or style.get("grid_width") is not None:
        ax.grid(True, axis="y", color=grid_color, linewidth=grid_width)
    _y_formatter(ax, style)
    _legend(ax, handles, names, style, default_align="right")
    _tidy_and_check(fig, [ax], len(levels), warns)
    return fig, stats


def _overlaps(a, b) -> bool:
    return a.x0 < b.x1 - 0.5 and b.x0 < a.x1 - 0.5 and a.y0 < b.y1 and b.y0 < a.y1


def _merge_na_marks(ax, r) -> bool:
    """Merge colliding n/a marks of one group into a single centred mark; True if any merged."""
    from matplotlib.text import Annotation
    marks = [t for t in ax.texts if isinstance(t, Annotation)]
    by_level: dict[int, list] = {}
    for t in marks:
        by_level.setdefault(int(round(t.xy[0])), []).append(t)
    merged = False
    for grp in by_level.values():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda t: t.xy[0])
        ext = [t.get_window_extent(r) for t in grp]
        runs, run = [], [0]
        for i in range(1, len(grp)):     # runs of neighbouring marks that collide ("n/an/an/a")
            if _overlaps(ext[i - 1], ext[i]):
                run.append(i)
            else:
                runs.append(run)
                run = [i]
        runs.append(run)
        for run in runs:
            if len(run) < 2:
                continue
            keep = grp[run[0]]
            keep.xy = (sum(grp[i].xy[0] for i in run) / len(run), keep.xy[1])
            for i in run[1:]:
                grp[i].remove()
            merged = True
    return merged


def _tidy_and_check(fig, axes, n_levels: int, warns: list) -> None:
    """Lay the figure out once, then (1) merge colliding n/a marks of one group into a single centred mark and
    (2) warn when the legend or axis labels do not fit the fixed figure size (they would be clipped).
    `axes` = one Axes or a list (stacked panels)."""
    from matplotlib.text import Annotation
    axes = list(axes) if isinstance(axes, (list, tuple)) else [axes]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig.draw_without_rendering()
    if any("collapsed to zero" in str(w.message) for w in caught):
        warns.append("the layout does not fit the figure size (axes collapsed to zero); reduce the legend / label "
                     "font sizes or legend columns")
    r = fig.canvas.get_renderer()
    merged_any = {}
    for ax in axes:
        merged_any[id(ax)] = _merge_na_marks(ax, r)
    if any(merged_any.values()):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fig.draw_without_rendering()
        r = fig.canvas.get_renderer()
    fb = fig.bbox
    tol = 1.0
    share = 1.0 / len(axes)
    for ax in axes:
        leg = ax.get_legend()
        if leg is not None:
            lb = leg.get_window_extent(r)
            if lb.x0 < fb.x0 - tol or lb.x1 > fb.x1 + tol or lb.y1 > fb.y1 + tol or lb.y0 < fb.y0 - tol:
                warns.append("the legend does not fit the figure and is clipped; reduce legend columns / legend font "
                             "size, or use legend position 'above' (with 'right', 1 column)")
        ab = ax.get_window_extent(r)
        if ab.width < 0.4 * fb.width:
            warns.append(f"the plot area is squeezed to {100 * ab.width / fb.width:.0f}% of the figure width "
                         "(legend or labels take the rest)")
        if ab.height < 0.25 * share * fb.height:
            warns.append(f"the plot area is squeezed to {100 * ab.height / fb.height:.0f}% of the figure height")
        (x0, x1), (y0, y1) = sorted(ax.get_xlim()), sorted(ax.get_ylim())
        ticks = [t for t in ax.get_xticklabels() if x0 - 1e-9 <= t.get_position()[0] <= x1 + 1e-9]
        ticks += [t for t in ax.get_yticklabels() if y0 - 1e-9 <= t.get_position()[1] <= y1 + 1e-9]  # drawn ones
        labels = [("y label", ax.yaxis.label)] + [("tick label", t) for t in ticks]
        for name, t in labels:
            if not t.get_visible() or not t.get_text():
                continue
            b = t.get_window_extent(r)
            if b.x0 < fb.x0 - tol or b.x1 > fb.x1 + tol or b.y0 < fb.y0 - tol or b.y1 > fb.y1 + tol:
                warns.append(f"the {name} {t.get_text()!r} is clipped at the figure edge; reduce its font size")
                break
        marks = [t for t in ax.texts if isinstance(t, Annotation)]
        if n_levels and len(marks) and not merged_any[id(ax)]:
            ext = [t.get_window_extent(r) for t in marks]
            if any(_overlaps(a, b) for i, a in enumerate(ext) for b in ext[i + 1:]):
                warns.append("n/a marks of neighbouring groups overlap; widen the figure or the group width")


RENDERERS = {"grouped_bars": _grouped_bars, "lines": _lines}


def notes_for(stats, style=None) -> list:
    """Legend-free notes for the render result: what a yellow interval means, when one is drawn (a dup-flagged
    count with an interval; a lines band has no per-point color)."""
    if (style or {}).get("ci_style") == "band":
        return []
    return [_spec.DUP_NOTE] if any(s.get("dup") and s.get("lo") is not None for s in stats or []) else []


def _prepare(spec: dict):
    spec = copy.deepcopy(spec)
    kind = spec.get("kind", "grouped_bars")
    spec["kind"] = kind
    spec["style"] = {**_spec.defaults(kind), **(spec.get("style") or {})}
    errors = _spec.validate(spec)
    if errors:
        raise _spec.SpecError(errors)
    if kind not in RENDERERS:
        raise ValueError(f"no renderer for kind {kind!r}")
    return spec


def _draw(spec):
    """(fig, stats, warnings) for a prepared spec. Caller holds _LOCK and closes fig."""
    warns: list[str] = []
    data = resolve_data(spec)
    warns.extend(data.get("warnings") or [])
    fig, stats = RENDERERS[spec["kind"]](spec, spec["style"], data, warns)
    return fig, stats, list(dict.fromkeys(warns))


def render(spec: dict, out_pdf: Path | None = None) -> dict:
    """Write the PDF (exact size, never cropped) and a same-size 600 dpi PNG beside it, via paperplot.save.
    Default out_pdf = PAPER_ROOT / spec["output"]."""
    spec = _prepare(spec)
    out = Path(out_pdf) if out_pdf is not None else config.PAPER_ROOT / spec["output"]
    out = out.with_suffix(".pdf")
    if out_pdf is None:     # validate() already refuses '..' and absolute paths; belt and braces
        out.resolve().relative_to(config.PAPER_ROOT.resolve())
    out.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fig = None
        try:
            fig, stats, warns = _draw(spec)
            pdf = pp.save(fig, out, transparent=bool(spec["style"]["transparent"]))
        finally:
            if fig is not None:
                plt.close(fig)
    return {"pdf": str(pdf), "png": str(pdf.with_suffix(".png")), "width_pt": spec["style"]["width_pt"],
            "height_pt": spec["style"]["height_pt"], "warnings": warns, "stats": stats, "notes": notes_for(stats, spec["style"])}


def preview_png(spec: dict, dpi: int = 200) -> bytes:
    """In-memory PNG at the figure's physical size (no files written anywhere)."""
    spec = _prepare(spec)
    with _LOCK:
        fig = None
        try:
            fig, _, _ = _draw(spec)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=dpi, bbox_inches=None, transparent=bool(spec["style"]["transparent"]))
        finally:
            if fig is not None:
                plt.close(fig)
    return buf.getvalue()


def preview(spec: dict, dpi: int = 200) -> dict:
    """preview_png plus the stats and warnings (for a UI that wants both from one call)."""
    spec = _prepare(spec)
    with _LOCK:
        fig = None
        try:
            fig, stats, warns = _draw(spec)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=dpi, bbox_inches=None, transparent=bool(spec["style"]["transparent"]))
        finally:
            if fig is not None:
                plt.close(fig)
    return {"png": buf.getvalue(), "stats": stats, "warnings": warns, "notes": notes_for(stats, spec["style"])}
