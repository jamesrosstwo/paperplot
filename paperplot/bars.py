"""Bar charts: grouped/single square-ended bars with a surface gap between neighbours."""
from __future__ import annotations

from typing import Mapping, Sequence

import matplotlib as mpl
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.transforms import offset_copy

from . import colors as C
from . import theme as _theme

_UNSET = object()


def _side(v, baseline, rest) -> int:
    """+1 above the baseline, -1 below. Zero-height marks use `rest`, the side the
    other bars grow toward (so a zero in a chart of drops hangs down like them)."""
    if v != baseline:
        return 1 if v > baseline else -1
    return rest


def _darken(hex_color: str, k: float) -> str:
    """Scale RGB by k (k < 1 darkens), as ACME does for its 'muted' bar fills."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02X}{:02X}{:02X}".format(*(min(255, round(c * k)) for c in (r, g, b)))


def _rgba(hex_color: str, alpha: float):
    return (*mpl.colors.to_rgb(hex_color), alpha)


def _as_lower_upper(err, m):
    """Symmetric (m,) or asymmetric (2, m) [below, above] distances -> (2, m)."""
    err = np.asarray(err, float)
    if err.ndim == 1:
        err = np.vstack([err, err])
    if err.shape != (2, m):
        raise ValueError(f"errors must have shape ({m},) or (2, {m}); got {err.shape}")
    return err


def bar(ax,
        categories: Sequence[str],
        series: Mapping[str, Sequence[float]],
        errors: Mapping[str, Sequence[float]] | None = None,
        colors: Mapping[str, str] | Sequence[str] | None = None,
        highlight: str | None = None,
        labels=_UNSET,
        fmt=_UNSET,
        group_width=_UNSET,
        gap_pt=_UNSET,
        hatch: bool = False,
        baseline: float = 0.0,
        ylabel: str | None = None,
        ylim: tuple[float, float] | None = None,
        legend=_UNSET,
        dividers: Sequence[int] = ()):
    """Grouped bar chart.

    categories : x-axis groups (e.g. tasks).
    series     : {name: values}, one bar per series per category. Order = color order.
    errors     : optional {name: err} drawn as thin ink whiskers. err is (m,) for
                 symmetric, or (2, m) = [distance below, distance above] each value
                 (e.g. a credible interval: [v - lo, hi - v]).
    colors     : override mapping/list; default = fixed categorical order.
    highlight  : series name the story is about (gets value labels when labels='highlight').
    labels     : 'highlight' | 'all' | 'max' | None — value labels at bar tips (in ink).
    hatch      : add 45°/135° texture per series (grayscale print / CVD backup).
    dividers   : category indices to draw a dashed vertical rule AFTER (e.g. [0] separates
                 the first category from the rest); styled by bars.divider in the theme.
    NaN values leave an empty slot marked with bars.missing_label (e.g. 'n/a').
    Fill darkening, outlines, bar width, baseline and group separators come from
    the theme's `bars:` section (see themes/acme.yaml for all of them in use).
    legend     : 'top' (one row above plot, aligned by bars.legend_align) | 'right' | None.
                 A single series gets no legend unless `legend` is passed explicitly.

    Styling arguments left unset fall back to the active theme's `bars:` section.
    """
    t = _theme.current()
    cfg = t.bars
    pick = lambda v, key, default: cfg.get(key, default) if v is _UNSET else v
    labels = pick(labels, "labels", "highlight")
    labels = None if labels in (None, "none", False) else labels
    fmt = pick(fmt, "label_format", "{:.1f}")
    group_width = pick(group_width, "group_width", 0.8)
    gap_pt = pick(gap_pt, "gap_pt", 1.0)
    legend_forced = legend is not _UNSET  # asked for explicitly: show even for one series
    legend = pick(legend, "legend", "top")
    legend = None if legend in (None, "none", False) else legend
    hatches = cfg.get("hatches", [""])
    err_w = cfg.get("error_width", 0.6)
    err_color = _rgba(t.resolve(cfg.get("error_color", "black"), "bars.error_color"),
                      cfg.get("error_alpha", 1.0))
    darken = cfg.get("fill_darken", 1.0)
    edge_w = cfg.get("edge_width", 0)
    edge_dk = cfg.get("edge_darken")  # outline = own fill darkened by this factor
    edge = (_rgba(t.resolve(cfg.get("edge_color", "black"), "bars.edge_color"),
                  cfg.get("edge_alpha", 1.0)) if edge_w and not edge_dk else "none")
    base_color = t.resolve(cfg.get("baseline_color", t.baseline), "bars.baseline_color")
    base_w = cfg.get("baseline_width", t.axes.get("spine_width", 0.6))
    sep = cfg.get("group_separator") or {}
    err_cap = cfg.get("error_cap_pt", 0)
    zero_pt = cfg.get("zero_bar_pt", 1.0)
    _sizes = t.font.get("sizes", {})
    ann_size = _sizes.get("annotation", _sizes.get("base", 8))
    names = list(series)
    n, m = len(names), len(categories)
    if colors is None:
        cmap = dict(zip(names, C.categorical(n)))
    elif isinstance(colors, Mapping):
        cmap = dict(colors)
    else:
        cmap = dict(zip(names, colors))
    if darken != 1.0:
        cmap = {k: _darken(t.resolve(v, "bar color"), darken) for k, v in cmap.items()}

    x = np.arange(m)
    slot = group_width / n
    # the surface gap is specified in points; convert to data units
    fig = ax.figure
    ax_w_pt = ax.get_position().width * fig.get_figwidth() * 72
    gap = gap_pt / ax_w_pt * (m - 1 + 1.0)  # approx data units per point over the x-range
    width = max(slot - gap, slot * 0.5)
    if "bar_width" in cfg:  # fraction of each bar's slot; overrides gap_pt
        width = slot * cfg["bar_width"]

    handles = []
    tips = []
    missing = []
    zeros = []  # (x0, width, color) of bars whose value equals the baseline
    for i, name in enumerate(names):
        vals = np.asarray(series[name], dtype=float)
        err = None if errors is None or name not in errors else _as_lower_upper(errors[name], m)
        offs = x - group_width / 2 + slot * i + (slot - width) / 2
        h = hatches[i % len(hatches)] if hatch else ""
        ec = _darken(t.resolve(cmap[name], "bar color"), edge_dk) if edge_w and edge_dk else edge
        for j, v in enumerate(vals):
            if np.isnan(v):
                missing.append(offs[j] + width / 2)
                continue
            p = Rectangle((offs[j], baseline), width, v - baseline,
                          facecolor=cmap[name], edgecolor=C.SURFACE if h else ec,
                          linewidth=0 if h else edge_w, hatch=h, zorder=2)
            ax.add_patch(p)
            if v == baseline:
                zeros.append((offs[j], width, cmap[name]))
            lo_e, hi_e = (err[0, j], err[1, j]) if err is not None else (0.0, 0.0)
            tips.append((name, j, offs[j] + width / 2, v, v - lo_e, v + hi_e))
        if err is not None:
            ax.errorbar(offs + width / 2, vals, yerr=err, fmt="none",
                        ecolor=err_color, elinewidth=err_w, capsize=err_cap,
                        capthick=err_w, zorder=3)
        handles.append(Rectangle((0, 0), 1, 1, facecolor=cmap[name], hatch=h,
                                 edgecolor=C.SURFACE if h else ec,
                                 linewidth=0 if h else edge_w, label=name))

    # axes
    ax.set_xticks(x, categories)
    ax.set_xlim(-0.5, m - 0.5)
    ax.axhline(baseline, color=base_color, linewidth=base_w, zorder=2.5)
    def _vrules(positions, style, where):  # dashed vertical rules; dash lengths in pt
        w = style.get("width", 0.5)
        dash = [d / w if mpl.rcParams["lines.scale_dashes"] else d
                for d in style.get("dash", [3, 3])]
        for xs in positions:
            ax.axvline(xs, color=t.resolve(style.get("color", "gray"), f"bars.{where}.color"),
                       linewidth=w, dashes=dash, zorder=1.5)

    if sep:  # between every pair of category groups
        _vrules(x[:-1] + 0.5, sep, "group_separator")
    if len(dividers):  # after the requested categories only
        _vrules([i + 0.5 for i in dividers], cfg.get("divider") or {}, "divider")
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    allv = [t[4] for t in tips] + [t[5] for t in tips] + [baseline]
    below = sum(t[3] < baseline for t in tips)
    rest = -1 if below > sum(t[3] > baseline for t in tips) else 1
    if ylim is None:
        lo, hi = min(allv), max(allv)
        pad = 0.12 * (hi - lo or 1)
        ylim = (min(lo - (pad if lo < baseline else 0), baseline), hi + pad)
    ax.set_ylim(*ylim)
    if ylabel:
        ax.set_ylabel(ylabel)

    # selective direct labels, in ink, at the bar tip (above whisker if any)
    if labels:
        chosen = []
        if labels == "all":
            chosen = tips
        elif labels == "highlight" and highlight in series:
            chosen = [t for t in tips if t[0] == highlight]
        elif labels == "max" or (labels == "highlight" and highlight is None):
            for j in range(m):
                col = [t for t in tips if t[1] == j]
                if col:
                    chosen.append(max(col, key=lambda t: t[3]))
        for name, _, xc, v, lo_v, hi_v in chosen:
            d = _side(v, baseline, rest)
            top = hi_v if d > 0 else lo_v
            text = fmt.format(v)
            if mpl.rcParams["axes.unicode_minus"]:
                text = text.replace("-", "−")  # match the axis tick labels
            pad = 1.5 + (zero_pt if v == baseline else 0)
            ax.annotate(text, (xc, top), xytext=(0, pad * d),
                        textcoords="offset points", ha="center",
                        va="bottom" if d > 0 else "top",
                        fontsize=ann_size,
                        color=C.INK, zorder=4)

    # a value equal to the baseline is still data: draw a sliver of fixed
    # thickness (pt) on the side of the baseline the other bars grow toward
    if zero_pt:
        d = rest
        tr = offset_copy(ax.transData, fig=ax.figure, y=d * zero_pt / 2, units="points")
        for x0, w, color in zeros:
            ax.add_line(Line2D([x0, x0 + w], [baseline, baseline], transform=tr, color=color,
                               linewidth=zero_pt, solid_capstyle="butt", zorder=2.6))

    # series with no data in a category keep their slot, marked in muted ink
    missing_label = cfg.get("missing_label", "n/a")
    if missing_label:
        d = rest
        for xc in missing:
            ax.annotate(missing_label, (xc, baseline), xytext=(0, 1.5 * d),
                        textcoords="offset points", ha="center",
                        va="bottom" if d > 0 else "top",
                        fontsize=ann_size, color=C.INK_MUTED, zorder=4)

    if legend and (n >= 2 or legend_forced):
        if legend == "top":
            align = cfg.get("legend_align", "right")
            ax.legend(handles=handles, loc=f"lower {align}",
                      bbox_to_anchor=({"left": 0.0, "center": 0.5, "right": 1.0}[align], 1.0),
                      ncol=n, frameon=False)
        else:
            ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    return handles
