"""Figure specs: one YAML file per designed figure in config.SPECS_DIR (see CONTRACT.md section 1).

A spec says what to plot (`data`: inline counts or a data-store table plus a query) and how (`style`: every key is
one UI control). `controls()` describes those controls for the frontend, with dropdown options discovered at call
time (themes, fonts, palettes, swatches), and `validate()` checks a spec against the same table.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import math
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

import yaml

from . import config

KINDS = ("grouped_bars", "lines")  # renderers live in render.RENDERERS; add a kind there and a style table here
ID_RE = re.compile(r"^[a-z0-9_]+$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SEED_DIR = config.DESIGNER_DIR / "seeds"          # seed specs shipped with the designer (read-only; never a state dir)
THEMES_DIR = config.PAPERPLOT_ROOT / "paperplot" / "themes"
FONTS_DIR = config.PAPERPLOT_ROOT / "paperplot" / "fonts"
TOP_KEYS = ("id", "kind", "title", "output", "data", "style")
CI_LEVELS = (0.8, 0.9, 0.95, 0.99)
PALETTE_ROLES = ("categorical", "sequential", "diverging", "paired")
# fonts listed first in the font dropdown when installed (James's paper fonts, then Helvetica/Arial-likes)
PREFERRED_FONTS = ("Times New Roman", "Roboto Slab", "STIX Two Text", "STIXGeneral", "DejaVu Serif", "Helvetica",
                   "Arial", "Liberation Sans", "Nimbus Sans", "TeX Gyre Heros", "DejaVu Sans")


# symbol / icon / math-extension fonts: useless as a figure's text font, kept out of the Font dropdown
_SYMBOL_FONT_RE = re.compile(r"^(Webdings|Wingdings.*|Symbol|Marlett|MT Extra|Bookshelf Symbol.*|Segoe MDL2 Assets|"
                             r"Segoe Fluent Icons|HoloLens MDL2 Assets|Segoe UI Symbol|Segoe UI Emoji|"
                             r"cm(ex|sy|mi)10|STIXSize.*|STIXNonUnicode.*|Font Awesome.*|Material Icons.*)$", re.I)


class StaleError(RuntimeError):
    """save(spec, base_hash=...) found the file changed since base_hash was read. `current_hash` is the file's hash
    now (None if it no longer exists); nothing was written."""

    def __init__(self, sid: str, current_hash: str | None):
        super().__init__(f"spec {sid!r} changed on disk since it was loaded (nothing was written)")
        self.sid = sid
        self.current_hash = current_hash


_SAVE_LOCK = threading.Lock()      # makes save()'s compare-and-swap atomic within one server process


class SpecError(ValueError):
    """Raised by save() when validate() finds problems; .errors is the list."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# --------------------------------------------------------------------------- style table
# (key, label, type, default, group, extra). `extra` may hold min/max/step/options/nullable/help/example.
# Order here is the order of the style block in saved YAML and of the controls in the UI. One table per kind.
# Types: slider | dropdown | number | text | color | toggle | json (a structured value edited as JSON; see help).
def _opts(*values):
    return [{"value": v, "label": v} for v in values]


ARM_ORDER_REST = ("pooled", "mean", "manual", "as_listed")
PANEL_TITLE_LOCS = ("upper right", "upper left", "upper center", "above left", "above right", "left", "none")
DUP_CI_DEFAULT = "#DDAA33"          # Paul Tol high-contrast yellow (Classical bars are tol-yellow #CCBB44)
DUP_NOTE = "yellow CI = positions re-run; latest rollout counted"


def _head_rows(width=252, height=69):
    return [
        ("theme", "Theme", "dropdown", "acme", "Theme",
         dict(help="paperplot theme (paperplot/themes/*.yaml): fonts, sizes, swatches")),
        ("font_family", "Font", "dropdown", None, "Theme",
         dict(nullable=True, help="Text font; auto = the theme's font")),
        ("width_pt", "Width (pt)", "slider", width, "Size",
         dict(min=120, max=516, step=1, help="Figure width in TeX pt (RSS single column 252, double 516)")),
        ("height_pt", "Height (pt)", "slider", height, "Size",
         dict(min=40, max=800, step=1, help="Figure height in TeX pt (with panels: the whole figure, all panels)")),
    ]


def _arm_order_row():
    return ("arm_order", "Arm order", "json", None, "Order",
            dict(nullable=True, example={"fixed": [], "rest": "pooled"},
                 help="null = as listed in the data. {fixed: [names drawn first, in this order], rest: pooled | mean "
                      "| manual | as_listed, manual: [names]}. One order for every panel and group, best to worst: "
                      "pooled = total successes / total trials over every evaluated cell, mean = mean rate; ties "
                      "keep the listed order. n/a bars keep their slot."))


def _grouped_bars_table():
    return _head_rows() + [
        ("panel_gap", "Panel gap (pt)", "slider", 6, "Panels",
         dict(min=0, max=60, step=0.5, help="data.panels: space between one panel's x labels and the next panel")),
        ("share_x_labels", "Shared x labels", "toggle", False, "Panels",
         dict(help="data.panels: x tick labels only under the bottom panel")),
        ("panel_title_loc", "Panel title", "dropdown", "upper right", "Panels",
         dict(options=_opts(*PANEL_TITLE_LOCS), help="upper *: inside the axes; above *: over the axes; left: rotated beside the y ticks, "
                                                               "with the y label shared by all panels")),
        ("panel_title_fontsize", "Panel title size (pt)", "slider", None, "Panels",
         dict(min=4, max=16, step=0.5, nullable=True, help="auto = theme title size")),
        ("panel_ylabel", "Panel y label", "dropdown", "each", "Panels",
         dict(options=_opts("each", "middle", "first"), help="Which panels carry the y label")),
        ("group_width", "Group width / group gap", "slider", 0.66, "Bars",
         dict(min=0.3, max=1.0, step=0.005, help="Fraction of each group slot the bars span; the space between groups "
                                                 "is 1 - this (0.66 leaves a 0.34 gap)")),
        ("bar_fill", "Bar width / bar gap", "slider", 0.583, "Bars",
         dict(min=0.2, max=1.0, step=0.001, help="Bar width as a fraction of its sub-slot inside a group; the gap "
                                                 "between neighbouring bars is 1 - this")),
        ("edge_width", "Edge width (pt)", "slider", None, "Bars",
         dict(min=0.0, max=1.5, step=0.05, nullable=True, help="Bar outline width; auto = theme")),
        ("na_label", "n/a label", "text", "n/a", "Bars", dict(help="Mark for a level an arm was not evaluated on")),
        _arm_order_row(),
        ("level_sort", "Sort groups", "dropdown", "none", "Order",
         dict(options=_opts("none", "desc", "asc"),
              help="Order the x groups by their pooled success rate (stable; groups without data last)")),
        ("level_pin_first", "Keep first N groups", "number", 0, "Order",
         dict(min=0, max=50, step=1, help="With sorting: the first N groups stay where they are")),
        ("legend_loc", "Legend", "dropdown", "above", "Legend", dict(options=_opts("above", "right", "none"))),
        ("legend_align", "Legend align", "dropdown", "center", "Legend",
         dict(options=_opts("center", "left", "right"), help="Legend 'above': centred, or flush left / right")),
        ("legend_cols", "Legend columns", "slider", 5, "Legend", dict(min=1, max=8, step=1)),
        ("legend_fontsize", "Legend size (pt)", "slider", 6, "Legend", dict(min=4, max=12, step=0.5)),
        ("legend_box_fontsize", "Legend spacing size (pt)", "slider", None, "Legend",
         dict(min=4, max=12, step=0.5, nullable=True,
              help="Font size that sets the legend's handle and padding sizes; auto = the legend size")),
        ("legend_pad", "Legend gap", "slider", 0.0, "Legend",
         dict(min=0.0, max=2.0, step=0.05, help="Space between legend and axes, in legend font sizes "
                                                "(paperplot's theme uses 0.2)")),
        ("tick_fontsize", "Tick size (pt)", "slider", None, "Axes",
         dict(min=4, max=12, step=0.5, nullable=True, help="Value (y) tick labels and the n/a mark; auto = theme")),
        ("xtick_fontsize", "Group label size (pt)", "slider", 7, "Axes",
         dict(min=4, max=14, step=0.5, nullable=True, help="x tick labels (group names); auto = tick size")),
        ("xtick_rotation", "Group label angle", "slider", 0, "Axes",
         dict(min=0, max=90, step=5, help="Degrees; angled labels are right-aligned to their tick")),
        ("ylabel", "y label", "text", "Successes", "Axes", dict()),
        ("ylabel_fontsize", "y label size (pt)", "slider", None, "Axes",
         dict(min=4, max=14, step=0.5, nullable=True, help="auto = theme axis_label size")),
        ("y_mode", "y axis", "dropdown", "count", "Axes",
         dict(options=[{"value": "percent", "label": "percent"}, {"value": "fraction", "label": "fraction (0-1)"},
                      {"value": "count", "label": "count (k/N)"}],
              help="fraction: tick labels 0-1; count: tick labels k/out_of; bars stay at each arm's rate")),
        ("out_of", "Out of (N)", "number", 20, "Axes", dict(min=1, max=1000, step=1, help="N for count tick labels")),
        ("ytick_step", "y tick step (%)", "slider", 25, "Axes", dict(min=5, max=50, step=5)),
        ("divider_before", "Divider before group", "number", None, "Axes",
         dict(min=1, max=50, step=1, nullable=True, help="Dashed vertical rule left of that group index (0-based)")),
        ("divider_style", "Divider style", "dropdown", "ci", "Axes",
         dict(options=[{"value": "ci", "label": "interval ink"}, {"value": "theme", "label": "theme divider"}],
              help="interval ink: interval color/width, 4-3 dashes; theme: the theme's bars.divider (paperplot)")),
        ("ci", "Interval", "dropdown", "wilson", "Error bars",
         dict(options=[{"value": "wilson", "label": "Wilson score"}, {"value": "bayes", "label": "Bayes (Beta posterior)"},
                       {"value": "none", "label": "none"}])),
        ("ci_level", "Level", "dropdown", 0.95, "Error bars",
         dict(options=[{"value": v, "label": f"{v:.0%}"} for v in CI_LEVELS])),
        ("ci_color", "Interval color", "color", "#222222", "Error bars", dict()),
        ("ci_linewidth", "Interval width (pt)", "slider", 0.6, "Error bars", dict(min=0.2, max=2.0, step=0.05)),
        ("ci_capsize", "Cap size (pt)", "slider", 1.5, "Error bars", dict(min=0.0, max=4.0, step=0.1)),
        ("partial_below", "Partial below n", "number", 20, "Error bars",
         dict(min=1, max=1000, step=1, nullable=True,
              help="Intervals of arms with fewer trials than this at a level use the partial color (unfinished evals)")),
        ("partial_ci_color", "Partial color", "color", "#CC3311", "Error bars", dict()),
        ("dup_ci_color", "Re-run color", "color", DUP_CI_DEFAULT, "Error bars",
         dict(help="Interval color of a count flagged {dup: true} (positions re-run; latest rollout counted). "
                   "Wins over the partial color.")),
        ("palette", "Palette", "dropdown", None, "Colors",
         dict(nullable=True, help="auto = each arm's own color; else recolor arms in order from a theme palette")),
        ("level_colors", "Group colors", "json", None, "Colors",
         dict(nullable=True, example={"Nominal": "tol-grey"},
              help="{group name: color}: every bar of that group is filled with this color as is (not darkened), "
                   "outlined in a gray as bright as its own outline")),
        ("fill_darken", "Fill darken", "slider", 0.7, "Colors", dict(min=0.4, max=1.0, step=0.01, help="Fill = color x this")),
        ("edge_darken", "Edge darken", "slider", 0.6, "Colors",
         dict(min=0.3, max=1.0, step=0.01, help="Outline = fill x this")),
        ("transparent", "Transparent background", "toggle", False, "Colors", dict()),
    ]


def _lines_table():
    """Defaults reproduce figures/difficulty.py::by_task (Fig. 4)."""
    return _head_rows(252, 105.84) + [
        ("line_width", "Line width (pt)", "slider", 1.2, "Lines", dict(min=0.2, max=4.0, step=0.05)),
        ("marker", "Marker", "dropdown", "s", "Lines",
         dict(options=[{"value": "s", "label": "square"}, {"value": "o", "label": "circle"},
                       {"value": "^", "label": "triangle"}, {"value": "D", "label": "diamond"},
                       {"value": "v", "label": "triangle down"}, {"value": "none", "label": "none"}])),
        ("marker_size", "Marker size (pt)", "slider", 3, "Lines", dict(min=0.5, max=10, step=0.25)),
        ("marker_edge_width", "Marker edge (pt)", "slider", None, "Lines",
         dict(min=0.0, max=3.0, step=0.05, nullable=True, help="auto = matplotlib's default (1 pt)")),
        ("gap_mode", "Missing level", "dropdown", "join", "Lines",
         dict(options=[{"value": "join", "label": "join across"}, {"value": "break", "label": "break the line"}],
              help="A level an arm was not evaluated on: draw straight across it, or leave a gap")),
        _arm_order_row(),
        ("legend_loc", "Legend", "dropdown", "above", "Legend", dict(options=_opts("above", "right", "none"))),
        ("legend_align", "Legend align", "dropdown", "right", "Legend", dict(options=_opts("center", "left", "right"))),
        ("legend_cols", "Legend columns", "slider", None, "Legend",
         dict(min=1, max=8, step=1, nullable=True, help="auto = all entries in one row")),
        ("legend_fontsize", "Legend size (pt)", "slider", 8, "Legend", dict(min=4, max=12, step=0.5)),
        ("legend_handlelength", "Legend line length", "slider", None, "Legend",
         dict(min=0.5, max=4.0, step=0.1, nullable=True, help="In legend font sizes; auto = theme")),
        ("legend_pad", "Legend gap", "slider", 0.2, "Legend",
         dict(min=0.0, max=2.0, step=0.05, help="Space between legend and axes, in legend font sizes")),
        ("tick_fontsize", "y tick size (pt)", "slider", 6, "Axes",
         dict(min=4, max=12, step=0.5, nullable=True, help="auto = theme")),
        ("xtick_fontsize", "Level label size (pt)", "slider", 8, "Axes",
         dict(min=4, max=14, step=0.5, nullable=True, help="auto = theme")),
        ("ylabel", "y label", "text", "Success Rate", "Axes", dict()),
        ("ylabel_fontsize", "y label size (pt)", "slider", 10, "Axes",
         dict(min=4, max=14, step=0.5, nullable=True, help="auto = theme axis_label size")),
        ("y_mode", "y axis", "dropdown", "count", "Axes",
         dict(options=[{"value": "percent", "label": "percent"}, {"value": "fraction", "label": "fraction (0-1)"},
                      {"value": "count", "label": "count (k/N)"}])),
        ("out_of", "Out of (N)", "number", 20, "Axes", dict(min=1, max=1000, step=1, help="N for count tick labels")),
        ("ytick_step", "y tick step (%)", "slider", 20, "Axes", dict(min=5, max=50, step=5)),
        ("x_margin", "x margin", "slider", 0.45, "Axes",
         dict(min=0.0, max=1.0, step=0.01, help="Space left of the first and right of the last level, in level units")),
        ("xlabel_pad", "Level label gap (pt)", "slider", 4.0, "Axes",
         dict(min=0.0, max=12.0, step=0.5, nullable=True, help="Gap between the x axis and its labels; auto = theme")),
        ("grid_color", "Grid color", "color", "#EBEBEB", "Axes",
         dict(nullable=True, help="Horizontal grid lines; auto = theme")),
        ("grid_width", "Grid width (pt)", "slider", 0.4, "Axes",
         dict(min=0.1, max=2.0, step=0.05, nullable=True, help="auto = theme")),
        ("bottom_spine", "Bottom axis line", "dropdown", "grid", "Axes",
         dict(options=[{"value": "grid", "label": "like the grid"}, {"value": "theme", "label": "theme"},
                       {"value": "none", "label": "none"}])),
        ("ci", "Interval", "dropdown", "none", "Error bars",
         dict(options=[{"value": "none", "label": "none"}, {"value": "wilson", "label": "Wilson score"},
                       {"value": "bayes", "label": "Bayes (Beta posterior)"}])),
        ("ci_style", "Interval style", "dropdown", "whiskers", "Error bars", dict(options=_opts("whiskers", "band"))),
        ("ci_level", "Level", "dropdown", 0.95, "Error bars",
         dict(options=[{"value": v, "label": f"{v:.0%}"} for v in CI_LEVELS])),
        ("ci_linewidth", "Interval width (pt)", "slider", 0.6, "Error bars", dict(min=0.2, max=2.0, step=0.05)),
        ("ci_capsize", "Cap size (pt)", "slider", 1.5, "Error bars", dict(min=0.0, max=4.0, step=0.1)),
        ("band_alpha", "Band opacity", "slider", 0.18, "Error bars", dict(min=0.05, max=0.6, step=0.01)),
        ("partial_below", "Partial below n", "number", 20, "Error bars", dict(min=1, max=1000, step=1, nullable=True)),
        ("partial_ci_color", "Partial color", "color", "#CC3311", "Error bars", dict()),
        ("dup_ci_color", "Re-run color", "color", DUP_CI_DEFAULT, "Error bars",
         dict(help="Whisker color of a count flagged {dup: true}; wins over the partial color")),
        ("palette", "Palette", "dropdown", None, "Colors",
         dict(nullable=True, help="auto = each arm's own color; else recolor arms in order from a theme palette")),
        ("fill_darken", "Line darken", "slider", 0.7, "Colors",
         dict(min=0.4, max=1.0, step=0.01, help="Line color = arm color x this")),
        ("transparent", "Transparent background", "toggle", False, "Colors", dict()),
    ]


_TABLES = {"grouped_bars": _grouped_bars_table, "lines": _lines_table}
GROUPS = ["Theme", "Size", "Panels", "Bars", "Lines", "Order", "Axes", "Legend", "Error bars", "Colors"]


def _style_table(kind: str = "grouped_bars"):
    return _TABLES[kind]()


STYLE_KEYS = tuple(row[0] for row in _style_table())          # grouped_bars (kept for importers)


def style_keys(kind: str = "grouped_bars") -> tuple:
    return tuple(row[0] for row in _style_table(kind))


def defaults(kind: str = "grouped_bars") -> dict:
    """A complete default style block for `kind`."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    return {key: copy.deepcopy(default) for key, _, _, default, _, _ in _style_table(kind)}


# --------------------------------------------------------------------------- counts
def count_parts(c):
    """(k, n, dup) of one count, or None for null. A count is [k, n], [k, n, {dup: true}] or {k, n, dup?}.
    Raises ValueError with a readable message when malformed."""
    if c is None:
        return None
    flags = {}
    if isinstance(c, dict):
        if set(c) - {"k", "n", "dup"} or "k" not in c or "n" not in c:
            raise ValueError("must be {k, n, dup?}")
        k, n, flags = c["k"], c["n"], {"dup": c.get("dup", False)}
    elif isinstance(c, (list, tuple)) and len(c) in (2, 3):
        k, n = c[0], c[1]
        if len(c) == 3:
            if not isinstance(c[2], dict) or set(c[2]) - {"dup"}:
                raise ValueError("the third entry must be {dup: true}")
            flags = c[2]
    else:
        raise ValueError("must be [successes, trials] (whole numbers), [successes, trials, {dup: true}] or null")
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (k, n)):
        raise ValueError("must be [successes, trials] (whole numbers)")
    dup = flags.get("dup", False)
    if not isinstance(dup, bool):
        raise ValueError("dup must be true or false")
    if n <= 0:
        raise ValueError("trials must be > 0")
    if not 0 <= k <= n:
        raise ValueError(f"successes {k} outside 0..{n}")
    return k, n, dup


DATA_SCHEMA = {
    "count": "[successes, trials] | [successes, trials, {dup: true}] | {k, n, dup?} | null (not evaluated: n/a). "
             "dup = positions re-run (latest rollout counted): its interval is drawn in style.dup_ci_color.",
    "inline": "{source: inline, levels: [names], arms: [{name, color, hatch?, counts: [count per level]}]}",
    "panels": "grouped_bars only: {source: inline, levels: [default levels], arms: [{name, color, hatch?}] (shared: "
              "colors and slots), panels: [{title, levels?, arms: [{name, counts}]}]}; panels are stacked "
              "vertically, arms matched by name, an arm missing from a panel is n/a there.",
    "dataset": "{source: dataset, dataset: name, query: {level, arm, successes, trials, filter?, levels?, arms?}}",
}


# --------------------------------------------------------------------------- discovery
def themes() -> list[str]:
    return sorted(p.stem for p in THEMES_DIR.glob("*.yaml"))


def _theme_mtimes(name):
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in THEMES_DIR.glob("*.yaml")))


@functools.lru_cache(maxsize=32)
def _load_theme_cached(name, _mtimes):
    from paperplot import theme as _theme
    return _theme.load(name)


def _load_theme(name):
    """A loaded paperplot Theme, cached until a theme file changes. Callers must not mutate it."""
    return _load_theme_cached(name, _theme_mtimes(name))


def swatches(theme: str) -> dict:
    """{swatch name: '#RRGGBB'} usable with a theme: the theme's own swatches first, then (for names it does not
    define) those of acme and the other themes. So a spec written for acme ('tol-blue', ...) stays valid and keeps
    its colors when the theme dropdown switches to a theme without those swatches."""
    out = dict(_load_theme(theme).swatches)
    for other in ["acme"] + [t for t in themes() if t not in ("acme", theme)]:
        try:
            for k, v in _load_theme(other).swatches.items():
                out.setdefault(k, v)
        except Exception:
            pass
    return out


def palettes(theme: str) -> dict:
    """{palette name: [hex, ...]} from the theme's colors: lists (paired is flattened in its key order)."""
    t = _load_theme(theme)
    out = {}
    for role in PALETTE_ROLES:
        v = getattr(t, role, None)
        if isinstance(v, dict):
            v = list(v.values())
        if v:
            out[role] = list(v)
    return out


@functools.lru_cache(maxsize=1)
def _font_names() -> tuple:
    from matplotlib import font_manager
    names = set()
    for f in sorted(FONTS_DIR.glob("*.[ot]tf")):       # theme font files (bundled with paperplot)
        try:
            font_manager.fontManager.addfont(str(f))
            names.add(font_manager.FontProperties(fname=str(f)).get_name())
        except Exception:
            pass
    for entry in font_manager.fontManager.ttflist:
        if entry.name and not entry.name.startswith("."):
            names.add(entry.name)
    names = {n for n in names if not _SYMBOL_FONT_RE.search(n)}
    first = [f for f in PREFERRED_FONTS if f in names]
    rest = sorted(names - set(first), key=str.lower)
    return tuple(first + rest)


def fonts() -> list[str]:
    """Installed font family names: common paper fonts first, then the rest alphabetically."""
    return list(_font_names())


def _theme_font(theme: str) -> str:
    t = _load_theme(theme)
    fam = t.font.get("serif" if t.font.get("family", "serif") == "serif" else "sans") or ["DejaVu Sans"]
    return fam[0]


def _control_rows(kind, theme, sw, pals):
    color_opts = [{"value": hexv, "label": name, "hex": hexv} for name, hexv in sw.items()]
    rows = []
    for key, label, typ, default, group, extra in _style_table(kind):
        row = {"key": key, "label": label, "type": typ, "default": copy.deepcopy(default), "group": group,
               "nullable": bool(extra.get("nullable", False)), "help": extra.get("help", ""),
               "min": extra.get("min"), "max": extra.get("max"), "step": extra.get("step"),
               "options": extra.get("options")}
        if "example" in extra:
            row["example"] = copy.deepcopy(extra["example"])
        if key == "theme":
            row["options"] = [{"value": n, "label": n} for n in themes()]
        elif key == "font_family":
            fl = fonts()
            n_first = sum(1 for f in fl if f in PREFERRED_FONTS)
            row["options"] = ([{"value": None, "label": f"theme ({_theme_font(theme)})"}]
                              + [{"value": f, "label": f} for f in fl[:n_first]]
                              + [{"value": "__sep__", "label": "──────────", "disabled": True}]
                              + [{"value": f, "label": f} for f in fl[n_first:]])
        elif key == "palette":
            row["options"] = ([{"value": None, "label": "per-arm colors"}]
                              + [{"value": n, "label": f"{n} ({len(v)})", "colors": v} for n, v in pals.items()])
        elif typ == "color":
            row["options"] = color_opts
        rows.append(row)
    return rows


def controls(theme: str | None = None, kind: str | None = None) -> dict:
    """Everything the UI needs to draw the style controls, for `theme` (default: the default style's theme).

    `style` holds the rows of `kind` (default grouped_bars); `style_by_kind` the rows of every kind, so a UI can
    pick `style_by_kind[spec.kind]`. `data_schema` describes the data block (counts, dup flag, panels)."""
    kind = kind if kind in KINDS else "grouped_bars"
    theme = theme if theme in themes() else defaults()["theme"]
    sw = swatches(theme)
    pals = palettes(theme)
    by_kind = {k: _control_rows(k, theme, sw, pals) for k in KINDS}
    return {"theme": theme, "kind": kind, "style": by_kind[kind], "style_by_kind": by_kind, "groups": list(GROUPS),
            "themes": themes(), "fonts": fonts(), "palettes": pals, "swatches": sw,
            "arm_colors": [{"value": name, "label": name, "hex": hexv} for name, hexv in sw.items()],
            "kinds": list(KINDS), "defaults": defaults(kind), "defaults_by_kind": {k: defaults(k) for k in KINDS},
            "data_schema": dict(DATA_SCHEMA), "notes": {"dup": DUP_NOTE}}


# --------------------------------------------------------------------------- validation
def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _color_ok(c, sw):
    return isinstance(c, str) and (c in sw or bool(HEX_RE.match(c.strip())))


_WHOLE = ("legend_cols", "out_of", "divider_before", "partial_below", "level_pin_first")


def _check_json(key, v, where, errors, sw, theme):
    if key == "arm_order":
        if not isinstance(v, dict):
            errors.append(f"{where}: must be {{fixed: [names], rest: {'|'.join(ARM_ORDER_REST)}, manual: [names]}} "
                          "or null")
            return
        for k in v:
            if k not in ("fixed", "rest", "manual"):
                errors.append(f"{where}.{k}: unknown key (fixed, rest, manual)")
        for lk in ("fixed", "manual"):
            lst = v.get(lk, [])
            if lst is None:
                continue
            if not isinstance(lst, list) or not all(isinstance(x, str) for x in lst):
                errors.append(f"{where}.{lk}: must be a list of arm names")
            elif len(set(lst)) != len(lst):
                errors.append(f"{where}.{lk}: names repeat")
        if v.get("rest", "as_listed") not in ARM_ORDER_REST:
            errors.append(f"{where}.rest: {v.get('rest')!r} not one of {list(ARM_ORDER_REST)}")
    elif key == "level_colors":
        if not isinstance(v, dict) or not all(isinstance(k, str) for k in v):
            errors.append(f"{where}: must be {{group name: color}} or null")
            return
        for k, c in v.items():
            if sw and not _color_ok(c, sw):
                errors.append(f"{where}.{k}: {c!r} is not a #rrggbb color or a {theme} swatch")


def _check_style(style, errors, kind="grouped_bars"):
    table = {row[0]: row for row in _style_table(kind)}
    if not isinstance(style, dict):
        errors.append("style must be a mapping")
        return
    for k in style:
        if k not in table:
            errors.append(f"style.{k}: unknown style key" + (f" for kind {kind}" if kind != "grouped_bars" else ""))
    full = {**defaults(kind), **style}
    theme = full["theme"]
    if theme not in themes():
        errors.append(f"style.theme: unknown theme {theme!r} (have {', '.join(themes())})")
        sw, pals = {}, {}
    else:
        sw, pals = swatches(theme), palettes(theme)
    for key, label, typ, default, group, extra in table.values():
        v = full[key]
        where = f"style.{key}"
        if v is None:
            if not extra.get("nullable"):
                errors.append(f"{where}: may not be null")
            continue
        if typ in ("slider", "number"):
            if not _is_num(v):
                errors.append(f"{where}: {v!r} is not a number")
                continue
            lo, hi = extra.get("min"), extra.get("max")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(f"{where}: {v} outside {lo}..{hi}")
            if key in _WHOLE and v != int(v):
                errors.append(f"{where}: {v} must be a whole number")
        elif typ == "toggle":
            if not isinstance(v, bool):
                errors.append(f"{where}: must be true or false")
        elif typ == "text":
            if not isinstance(v, str):
                errors.append(f"{where}: must be text")
        elif typ == "color":
            if sw and not _color_ok(v, sw):
                errors.append(f"{where}: {v!r} is not a #rrggbb color or a {theme} swatch")
        elif typ == "json":
            _check_json(key, v, where, errors, sw, theme)
        elif key == "font_family":
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{where}: must be a font family name")
        elif key == "palette":
            if pals and v not in pals:
                errors.append(f"{where}: {v!r} not a palette of {theme} (have {', '.join(pals)})")
        elif key == "ci_level":
            if not _is_num(v) or not any(abs(v - c) < 1e-9 for c in CI_LEVELS):
                errors.append(f"{where}: {v!r} not one of {CI_LEVELS}")
        elif key != "theme":
            opts = [o["value"] for o in extra.get("options") or []]
            if v not in opts:
                errors.append(f"{where}: {v!r} not one of {opts}")
    return sw


def _check_counts(counts, nlev, where, errors):
    if not isinstance(counts, list) or len(counts) != nlev:
        errors.append(f"{where}: counts needs one entry per level ({nlev}), got "
                      f"{len(counts) if isinstance(counts, list) else type(counts).__name__}")
        return
    for j, c in enumerate(counts):
        try:
            count_parts(c)
        except ValueError as e:
            errors.append(f"{where}[{j}]: {e}")


def _check_hatch(arm, where, errors):
    h = arm.get("hatch")
    if h is not None and (not isinstance(h, str) or set(h) - set("/\\|-+xoO.*")):
        errors.append(f"{where}.hatch: {h!r} is not a matplotlib hatch pattern")


def _check_levels(levels, where, errors):
    if not isinstance(levels, list) or not levels or not all(isinstance(l, str) for l in levels):
        errors.append(f"{where}: must be a non-empty list of strings")
        return False
    return True


def _check_arm_list(arms, where, errors, sw, need_color=True):
    """Checks names (required, unique) and colors/hatches of a list of arm mappings; returns the names."""
    if not isinstance(arms, list) or not arms:
        errors.append(f"{where}: must be a non-empty list")
        return []
    names = [a.get("name") for a in arms if isinstance(a, dict)]
    for dup in sorted({n for n in names if isinstance(n, str) and names.count(n) > 1}):
        errors.append(f"{where}: duplicate arm name {dup!r}")
    for i, arm in enumerate(arms):
        w = f"{where}[{i}]"
        if not isinstance(arm, dict):
            errors.append(f"{w}: must be a mapping")
            continue
        if not isinstance(arm.get("name"), str) or not arm.get("name"):
            errors.append(f"{w}.name: required text")
        if (need_color or "color" in arm) and sw and not _color_ok(arm.get("color"), sw):
            errors.append(f"{w}.color: {arm.get('color')!r} is not a #rrggbb color or a theme swatch")
        _check_hatch(arm, w, errors)
    return [n for n in names if isinstance(n, str)]


def _check_panels(data, kind, errors, sw):
    """data.panels (grouped_bars, inline): returns the arm names (shared list, or every panel's in order)."""
    panels = data.get("panels")
    if kind != "grouped_bars":
        errors.append(f"data.panels: only grouped_bars has panels (kind {kind})")
    if not isinstance(panels, list) or not panels:
        errors.append("data.panels: must be a non-empty list of {title, levels?, arms}")
        return []
    top_levels = data.get("levels")
    if top_levels is not None:
        _check_levels(top_levels, "data.levels", errors)
    shared = data.get("arms")
    shared_names = None
    if shared is not None:
        shared_names = _check_arm_list(shared, "data.arms", errors, sw)
        for i, a in enumerate(shared if isinstance(shared, list) else []):
            if isinstance(a, dict) and a.get("counts") is not None and isinstance(top_levels, list):
                _check_counts(a["counts"], len(top_levels), f"data.arms[{i}].counts", errors)
    seen, colored = [], set()
    for pi, p in enumerate(panels):
        where = f"data.panels[{pi}]"
        if not isinstance(p, dict):
            errors.append(f"{where}: must be a mapping {{title, levels?, arms}}")
            continue
        for k in p:
            if k not in ("title", "levels", "arms"):
                errors.append(f"{where}.{k}: unknown key (title, levels, arms)")
        if p.get("title") is not None and not isinstance(p.get("title"), str):
            errors.append(f"{where}.title: must be text")
        levels = p.get("levels", top_levels)
        if levels is None:
            errors.append(f"{where}.levels: required (or give data.levels for every panel)")
            levels = []
        elif not _check_levels(levels, f"{where}.levels", errors):
            levels = []
        names = _check_arm_list(p.get("arms"), f"{where}.arms", errors, sw, need_color=False)
        for ai, arm in enumerate(p.get("arms") if isinstance(p.get("arms"), list) else []):
            if not isinstance(arm, dict):
                continue
            w = f"{where}.arms[{ai}]"
            for k in arm:
                if k not in ("name", "color", "hatch", "counts"):
                    errors.append(f"{w}.{k}: unknown key (name, counts, color?, hatch?)")
            if shared_names is not None and arm.get("name") not in shared_names:
                errors.append(f"{w}.name: {arm.get('name')!r} is not in data.arms (the shared arm list)")
            if "color" in arm:
                colored.add(arm.get("name"))
            _check_counts(arm.get("counts"), len(levels), f"{w}.counts", errors)
        seen += [n for n in names if n not in seen]
    if shared_names is None:
        for n in seen:
            if n not in colored:
                errors.append(f"data.panels: arm {n!r} has no color (give data.arms [{{name, color}}] or a color "
                              "on its first panel entry)")
        return seen
    return shared_names


def _check_arm_order_names(style, names, errors):
    ao = (style or {}).get("arm_order")
    if not isinstance(ao, dict) or names is None:
        return
    for lk in ("fixed", "manual"):
        for n in ao.get(lk) or []:
            if isinstance(n, str) and n not in names:
                errors.append(f"style.arm_order.{lk}: {n!r} is not an arm name (have {', '.join(map(repr, names))})")


def validate(spec) -> list[str]:
    """Problems with `spec` as human-readable strings; empty list = ok."""
    errors = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    sid = spec.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        errors.append(f"id: {sid!r} must match [a-z0-9_]+")
    kind = spec.get("kind", "grouped_bars")
    if kind not in KINDS:
        errors.append(f"kind: {kind!r} not one of {KINDS}")
        kind = "grouped_bars"
    for k in spec:
        if k not in TOP_KEYS and not str(k).startswith("_"):   # "_..." = UI scratch keys, dropped on save
            errors.append(f"{k}: unknown top-level key")
    if "title" in spec and not isinstance(spec["title"], str):
        errors.append("title: must be text")
    out = spec.get("output")
    if not isinstance(out, str) or not out.strip():
        errors.append("output: required (a .pdf path relative to the paper root)")
    else:
        p = Path(out)
        if p.is_absolute() or out.startswith(("/", "\\")) or ".." in p.parts or re.match(r"^[A-Za-z]:", out):
            errors.append(f"output: {out!r} must be a relative path inside the paper repo")
        if p.suffix.lower() != ".pdf":
            errors.append(f"output: {out!r} must end in .pdf")
    style = spec.get("style") or {}
    sw = _check_style(style, errors, kind)
    data = spec.get("data")
    if not isinstance(data, dict):
        errors.append("data: required mapping")
        return errors
    src = data.get("source", "inline")
    names = None
    if src == "inline" and data.get("panels") is not None:
        names = _check_panels(data, kind, errors, sw)
    elif src == "inline":
        levels = data.get("levels")
        if not _check_levels(levels, "data.levels", errors):
            levels = levels if isinstance(levels, list) else []
        arms = data.get("arms")
        names = _check_arm_list(arms, "data.arms", errors, sw)
        for i, arm in enumerate(arms if isinstance(arms, list) else []):
            if isinstance(arm, dict):
                _check_counts(arm.get("counts"), len(levels), f"data.arms[{i}].counts", errors)
    elif src == "dataset":
        if data.get("panels") is not None:
            errors.append("data.panels: only with source inline")
        if not isinstance(data.get("dataset"), str) or not data.get("dataset"):
            errors.append("data.dataset: required (a data-store name)")
        q = data.get("query")
        if not isinstance(q, dict):
            errors.append("data.query: required mapping")
        else:
            for k in ("level", "arm", "successes", "trials"):
                if not isinstance(q.get(k), str) or not q.get(k):
                    errors.append(f"data.query.{k}: required column name")
            if "filter" in q and not isinstance(q["filter"], dict):
                errors.append("data.query.filter: must be a mapping {column: value or [values]}")
            for lk in ("levels", "arms"):
                if lk in q and not isinstance(q[lk], list):
                    errors.append(f"data.query.{lk}: must be a list")
            for i, lev in enumerate(q.get("levels") or []):
                if not (isinstance(lev, str) or (isinstance(lev, dict) and "key" in lev)):
                    errors.append(f"data.query.levels[{i}]: a value or {{key, label}}")
            for i, arm in enumerate(q.get("arms") or []):
                where = f"data.query.arms[{i}]"
                if not isinstance(arm, dict) or "key" not in arm:
                    errors.append(f"{where}: must be {{key, name, color}}")
                    continue
                if "color" in arm and sw and not _color_ok(arm["color"], sw):
                    errors.append(f"{where}.color: {arm['color']!r} is not a #rrggbb color or a theme swatch")
                _check_hatch(arm, where, errors)
            if isinstance(q.get("arms"), list) and q["arms"]:
                names = [a.get("name", str(a.get("key"))) for a in q["arms"] if isinstance(a, dict)]
    else:
        errors.append(f"data.source: {src!r} must be inline or dataset")
    if isinstance(style, dict):
        _check_arm_order_names(style, names, errors)
    return errors


# --------------------------------------------------------------------------- YAML I/O
class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        # a shared object (e.g. one {dup: true} dict loaded from an anchor and reused) is written out in full each
        # time, never as "&id001" / "*id001", so the saved YAML stays readable and hand-editable
        return True


def _is_count(x):
    """[k, n] or [k, n, {flags}] (a count as written in data.*.counts)."""
    return (isinstance(x, (list, tuple)) and len(x) in (2, 3) and all(isinstance(y, (int, float)) for y in x[:2])
            and (len(x) == 2 or isinstance(x[2], dict)))


def _repr_list(dumper, data):
    # short lists of scalars / counts inline ("[16, 20]", "[[16, 20], null, [17, 20, {dup: true}]]"); others as blocks
    flat = all(x is None or isinstance(x, (str, int, float, bool)) for x in data)
    pairs = all(x is None or (isinstance(x, list) and all(isinstance(y, (int, float)) for y in x)) for x in data)
    counts = all(x is None or _is_count(x) or (isinstance(x, dict) and set(x) <= {"k", "n", "dup"}) for x in data)
    flow = ((flat and all(not isinstance(x, str) or len(x) < 40 for x in data)) or (pairs and len(data) > 0)
            or _is_count(data) or (counts and len(data) > 0))
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _repr_list)
_Dumper.add_representer(tuple, _repr_list)


def _ordered(spec):
    """Stable key order: id, kind, title, output, data, style (style keys in table order)."""
    out = {k: spec[k] for k in TOP_KEYS if k in spec}
    kind = spec.get("kind", "grouped_bars")
    kind = kind if kind in KINDS else "grouped_bars"
    style = {**defaults(kind), **(spec.get("style") or {})}
    out["style"] = {k: style[k] for k in style_keys(kind) if k in style}
    if isinstance(out.get("data"), dict):
        d = out["data"]
        order = ("source", "levels", "arms", "panels", "dataset", "query")
        out["data"] = {**{k: d[k] for k in order if k in d}, **{k: v for k, v in d.items() if k not in order}}
    return out


def dumps(spec: dict) -> str:
    return yaml.dump(_ordered(spec), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120,
                     default_flow_style=False)


def _path(sid: str) -> Path:
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ValueError(f"bad spec id {sid!r} (must match [a-z0-9_]+)")
    return config.SPECS_DIR / f"{sid}.yaml"


def ensure_seeds() -> None:
    """Copy the seed specs (designer/seeds) into SPECS_DIR once; existing specs of the same id are never overwritten."""
    try:
        if config.SPECS_DIR.resolve() == SEED_DIR.resolve() or not SEED_DIR.is_dir():
            return
    except OSError:
        return
    marker = config.SPECS_DIR / ".seeded"
    if marker.exists():
        return
    config.SPECS_DIR.mkdir(parents=True, exist_ok=True)
    seeds = sorted(SEED_DIR.glob("*.yaml"))
    # a state dir that already holds one of the seed specs was seeded before the marker existed (e.g. the old
    # designer/specs default): mark it and add nothing, so a seed added later does not appear in someone's live dir
    already = any((config.SPECS_DIR / f.name).exists() for f in seeds)
    for f in ([] if already else seeds):
        dst = config.SPECS_DIR / f.name
        if not dst.exists():
            shutil.copyfile(f, dst)
    marker.write_text("seed specs copied from designer/seeds; delete this file to copy them again\n", encoding="utf-8")


def list_specs() -> list[dict]:
    ensure_seeds()
    out = []
    for f in sorted(config.SPECS_DIR.glob("*.yaml")):
        try:
            s = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as e:   # a broken file still shows up, so the user can fix or delete it
            out.append({"id": f.stem, "title": f"(unreadable: {e})", "kind": None, "output": None})
            continue
        data = s.get("data") if isinstance(s.get("data"), dict) else {}
        out.append({"id": s.get("id", f.stem), "title": s.get("title", ""), "kind": s.get("kind", "grouped_bars"),
                    "output": s.get("output"), "source": data.get("source", "inline"),
                    "dataset": data.get("dataset") if data.get("source") == "dataset" else None})
    return out


def load(sid: str) -> dict:
    """The spec with its style completed from defaults() (so the UI always sees every key)."""
    ensure_seeds()
    p = _path(sid)
    if not p.exists():
        raise FileNotFoundError(f"no spec {sid!r} in {config.SPECS_DIR}")
    s = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s.setdefault("id", sid)
    s.setdefault("kind", "grouped_bars")
    s.setdefault("title", "")
    s["style"] = {**defaults(s["kind"] if s["kind"] in KINDS else "grouped_bars"), **(s.get("style") or {})}
    return s


def file_hash(sid: str) -> str | None:
    """sha256 (hex) of SPECS_DIR/<id>.yaml's bytes, or None if there is no such file. The version token for save()."""
    try:
        return hashlib.sha256(_path(sid).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def save(spec: dict, base_hash: str | None = None) -> str:
    """Validate and write SPECS_DIR/<id>.yaml atomically; returns the new file_hash. Raises SpecError(errors) if
    invalid. With base_hash (the file_hash the caller loaded), it is compare-and-swap: if the file changed since
    (a hand edit, another tab, another process), StaleError is raised and nothing is written. An unchanged spec
    is not rewritten."""
    spec = copy.deepcopy(spec)
    spec.setdefault("kind", "grouped_bars")
    errors = validate(spec)
    if errors:
        raise SpecError(errors)
    from designer.locks import atomic_write_text
    path = _path(spec["id"])
    text = dumps(spec)
    with _SAVE_LOCK:
        cur = file_hash(spec["id"])
        if base_hash is not None and cur != base_hash:
            raise StaleError(spec["id"], cur)
        new = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if cur == new:
            return cur
        atomic_write_text(path, text)     # temp file + os.replace; written with LF newlines, so bytes == text
        return new


def _blank(sid: str) -> dict:
    return {"id": sid, "kind": "grouped_bars", "title": sid.replace("_", " "),
            "output": f"figures/evals/{sid}.pdf",
            "data": {"source": "inline", "levels": ["Nominal", "Easy"],
                     "arms": [{"name": "Base", "color": "tol-blue", "counts": [[15, 20], [10, 20]]},
                              {"name": "Ours", "color": "tol-red", "counts": [[18, 20], [16, 20]]}]},
            "style": defaults()}


def create(sid: str, template_id: str | None = None) -> dict:
    """New spec `sid` (a copy of `template_id`, or a small example). Output goes to figures/evals/<sid>.pdf."""
    p = _path(sid)
    if p.exists():
        raise FileExistsError(f"spec {sid!r} already exists")
    if template_id:
        s = load(template_id)
        s["id"] = sid
        s["title"] = f"{s.get('title') or template_id} (copy)"
        s["output"] = f"figures/evals/{sid}.pdf"
    else:
        s = _blank(sid)
    save(s)
    return load(sid)


def delete(sid: str) -> None:
    """Remove the spec file only (never its rendered output in the paper)."""
    p = _path(sid)
    if not p.exists():
        raise FileNotFoundError(f"no spec {sid!r}")
    p.unlink()
