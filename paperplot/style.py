"""Matplotlib style for print, driven entirely by the active YAML theme.

Figures are saved as PDF at exactly the theme's size: no bbox cropping, so the
PDF page is the figure size and text sizes are literal pt when the PDF is
included at natural size (\\includegraphics{fig.pdf}).
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib import font_manager

from . import theme as _theme
from .theme import Theme, length_in

# pt here is the PostScript/PDF point (1/72 in), which is what matplotlib uses
# for font sizes. LaTeX's pt is 1/72.27 in: the difference is 0.4%, below what
# font hinting can show; the page SIZE is converted exactly (see theme.length_in).


def figsize(width: str | float = "single", aspect: float | None = None,
            height: str | float | None = None) -> tuple[float, float]:
    """(w, h) in inches. `width` is a name from figure.widths or a length
    ('3.25in', '241pt', 3.25). Height = explicit `height`, else width * aspect."""
    fig = _theme.current().figure
    widths = fig.get("widths", {})
    if isinstance(width, str) and width in widths:
        w = length_in(widths[width], f"figure.widths.{width}")
    else:
        try:
            w = length_in(width, "width")
        except _theme.ThemeError:
            raise ValueError(f"unknown width {width!r}; theme defines {list(widths)} "
                             "or pass a length like '3.25in'") from None
    if height is not None:
        return (w, length_in(height, "height"))
    return (w, w * (aspect if aspect is not None else fig.get("aspect", 0.618)))


FONTS_DIR = Path(__file__).parent / "fonts"


def _register_fonts(files) -> None:
    """Make bundled font files (paths relative to paperplot/fonts/) available by name."""
    for f in files or []:
        path = Path(f) if Path(f).is_absolute() else FONTS_DIR / f
        if not path.exists():
            raise FileNotFoundError(f"font.files: {path} not found")
        font_manager.fontManager.addfont(str(path))


def _math_params(math: str, family: str) -> dict:
    """font.math: stix | cm | dejavusans | text. 'text' sets math in the text font
    itself (upright and italic from it), with STIX for glyphs it lacks (e.g. Greek)."""
    if math != "text":
        return {"mathtext.fontset": math}
    return {"mathtext.fontset": "custom", "mathtext.rm": family, "mathtext.it": f"{family}:italic",
            "mathtext.bf": f"{family}:bold", "mathtext.sf": family, "mathtext.fallback": "stix"}


def _check_font(family: list[str], kind: str) -> None:
    want = family[0]
    try:
        font_manager.findfont(font_manager.FontProperties(family=want), fallback_to_default=False)
    except ValueError:
        warnings.warn(f"font {want!r} not installed; matplotlib will fall back to the next "
                      f"entry in font.{kind}. Text will not match your paper.", stacklevel=3)


def use(theme: str | Path | Theme = "default") -> Theme:
    """Activate a theme (built-in name, .yaml path, or Theme) and apply it to matplotlib."""
    t = theme if isinstance(theme, Theme) else _theme.load(theme)
    _theme.set_current(t)

    font, fig, axes = t.font, t.figure, t.axes
    lines, leg = t.raw.get("lines", {}), t.raw.get("legend", {})
    sizes = font.get("sizes", {})
    base = float(sizes.get("base", 8))
    sz = lambda k: float(sizes.get(k, base))
    serif = font.get("family", "serif") == "serif"
    families = font.get("serif" if serif else "sans", [])
    _register_fonts(font.get("files"))
    _check_font(families, "serif" if serif else "sans")
    spines = set(axes.get("spines", ["left", "bottom"]))
    grid = axes.get("grid", "y")
    pad = length_in(fig.get("padding", "0.02in"), "figure.padding")

    mpl.rcParams.update({
        # type — every size is the literal pt size on the page
        "font.family": "serif" if serif else "sans-serif",
        "font.serif": font.get("serif", ["DejaVu Serif"]),
        "font.sans-serif": font.get("sans", ["DejaVu Sans"]),
        **_math_params(font.get("math", "stix" if serif else "dejavusans"), families[0]),
        "font.size": base,
        "axes.titlesize": sz("title"),
        "figure.titlesize": sz("title"),
        "axes.labelsize": sz("axis_label"),
        "xtick.labelsize": sz("tick"),
        "ytick.labelsize": float(sizes.get("ytick", sz("tick"))),
        "legend.fontsize": sz("legend"),
        "legend.title_fontsize": sz("legend"),
        # ink: text never wears series colors
        "text.color": t.ink,
        "axes.titlecolor": t.ink,
        "axes.labelcolor": t.ink_muted,
        "xtick.color": t.ink_muted,
        "ytick.color": t.ink_muted,
        "axes.edgecolor": t.baseline,
        # frame, grid, ticks
        **{f"axes.spines.{s}": s in spines for s in ("left", "right", "top", "bottom")},
        "axes.linewidth": axes.get("spine_width", 0.6),
        "axes.grid": grid != "none",
        "axes.grid.axis": grid if grid != "none" else "y",
        "axes.axisbelow": True,
        "axes.labelpad": axes.get("label_pad", 3),
        "grid.color": t.grid,
        "grid.linewidth": axes.get("grid_width", 0.5),
        "grid.linestyle": "-",
        "xtick.major.size": axes.get("tick_length", 0),
        "ytick.major.size": axes.get("tick_length", 0),
        "xtick.major.width": axes.get("tick_width", 0.6),
        "ytick.major.width": axes.get("tick_width", 0.6),
        "xtick.major.pad": axes.get("tick_pad", 2),
        "ytick.major.pad": axes.get("tick_pad", 2),
        # data
        "axes.prop_cycle": mpl.cycler(color=t.categorical),
        "lines.linewidth": lines.get("width", 1.25),
        "lines.markersize": lines.get("marker_size", 4),
        "lines.solid_capstyle": "round",
        "errorbar.capsize": 0,
        "hatch.linewidth": t.bars.get("hatch_width", 0.6),
        # legend
        "legend.frameon": leg.get("frame", False),
        "legend.handlelength": leg.get("handle_length", 1.0),
        "legend.handleheight": leg.get("handle_height", 0.8),
        "legend.handletextpad": leg.get("handle_text_pad", 0.4),
        "legend.columnspacing": leg.get("column_spacing", 1.0),
        "legend.borderaxespad": 0.2,
        # layout & output: exact page size, never cropped or scaled
        "figure.constrained_layout.use": True,
        "figure.constrained_layout.w_pad": pad,
        "figure.constrained_layout.h_pad": pad,
        "figure.facecolor": t.surface,
        "axes.facecolor": t.surface,
        "savefig.facecolor": t.surface,
        "savefig.format": "pdf",
        "savefig.bbox": None,          # NOT 'tight': tight would crop and change the size
        "savefig.dpi": fig.get("raster_dpi", 300),
        "pdf.fonttype": 42,            # TrueType: real, selectable text
    })
    return t


_MEDIABOX = re.compile(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]")


def save(fig, path: str | Path, transparent: bool | None = None) -> Path:
    """Save `fig` as a PDF whose page is exactly the figure size, and verify it.
    Also writes a same-named PNG when the theme sets figure.png.
    `transparent` drops the page and axes backgrounds (default: figure.transparent in the theme)."""
    path = Path(path).with_suffix(".pdf")
    if transparent is None:
        transparent = bool(_theme.current().figure.get("transparent", False))
    fig.savefig(path, format="pdf", bbox_inches=None, transparent=transparent)
    w, h = fig.get_size_inches()
    m = _MEDIABOX.search(path.read_bytes())
    if not m or abs(float(m.group(1)) - w * 72) > 0.5 or abs(float(m.group(2)) - h * 72) > 0.5:
        raise RuntimeError(f"{path}: PDF page size {m and m.groups()} != figure "
                           f"{w:.3f}x{h:.3f}in — something rescaled the figure")
    figcfg = _theme.current().figure
    if figcfg.get("png", False):  # same size, uncropped; for slides, docs and previews
        fig.savefig(path.with_suffix(".png"), format="png", bbox_inches=None,
                    dpi=figcfg.get("png_dpi", 300), transparent=transparent)
    return path


def subplots(width="single", aspect=None, height=None, **kw):
    """plt.subplots at a theme width (e.g. 'single', 'double') or an explicit length."""
    return plt.subplots(figsize=figsize(width, aspect, height), **kw)
