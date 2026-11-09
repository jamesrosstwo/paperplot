"""Color roles for the paper, read from the active YAML theme (see themes/default.yaml).

Attributes such as `CATEGORICAL`, `INK`, `SURFACE` are looked up on the *active*
theme at access time, so `pp.use("my_theme.yaml")` changes them everywhere.

`check_palette` is a Python port of the dataviz skill's validator (OKLab ΔE with
Machado-2009 CVD simulation), so any change to a theme can be re-verified.
"""
from __future__ import annotations

import math

from . import theme as _theme

_ROLE_ATTRS = {
    "CATEGORICAL": "categorical", "SEQUENTIAL": "sequential", "DIVERGING": "diverging",
    "PAIRED": "paired", "INK": "ink", "INK_MUTED": "ink_muted", "BASELINE": "baseline",
    "GRID": "grid", "SURFACE": "surface", "SWATCHES": "swatches",
}


def __getattr__(name):
    if name in _ROLE_ATTRS:
        return getattr(_theme.current(), _ROLE_ATTRS[name])
    raise AttributeError(f"module 'paperplot.colors' has no attribute {name!r}")


def swatch(name: str) -> str:
    """Hex for a named swatch in the active theme, e.g. swatch('navy')."""
    return _theme.current().resolve(name, "swatch")


def categorical(n: int) -> list[str]:
    """First n categorical colors in fixed order. Refuses to invent colors."""
    cat = _theme.current().categorical
    if n > len(cat):
        raise ValueError(
            f"{n} series > {len(cat)} categorical slots; fold extras "
            "into 'Other' (paperplot.colors.BASELINE) or use small multiples."
        )
    return cat[:n]


def marks(n: int) -> list[str]:
    """First n categorical colors as drawn on marks: darkened by the theme's
    bars.fill_darken, so lines and points match the bar fills."""
    k = _theme.current().bars.get("fill_darken", 1.0)
    out = []
    for c in categorical(n):
        r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
        out.append("#{:02X}{:02X}{:02X}".format(*(min(255, round(v * k)) for v in (r, g, b))))
    return out


# --- validator (port of dataviz/scripts/validate_palette.js) ------------------
_BAND = {"light": (0.43, 0.77), "dark": (0.48, 0.67)}
_CHROMA_FLOOR = 0.10
_CVD_TARGET, _CVD_FLOOR, _NORMAL_FLOOR, _CONTRAST_MIN = 8.0, 6.0, 15.0, 3.0
_MACHADO = {
    "protan": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
    "deutan": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
    "tritan": [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]],
}


def _lin(h):
    h = h.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def _oklab_from_lin(rgb):
    r, g, b = rgb
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _simulate(h, kind):
    r, g, b = _lin(h)
    M = _MACHADO[kind]
    return [min(1, max(0, M[i][0] * r + M[i][1] * g + M[i][2] * b)) for i in range(3)]


def delta_e(h1, h2, kind=None):
    """OKLab ΔE ×100, optionally under simulated CVD ('protan'/'deutan'/'tritan')."""
    a = _oklab_from_lin(_simulate(h1, kind) if kind else _lin(h1))
    b = _oklab_from_lin(_simulate(h2, kind) if kind else _lin(h2))
    return 100 * math.dist(a, b)


def oklch(h):
    L, a, b = _oklab_from_lin(_lin(h))
    return L, math.hypot(a, b)


def contrast(a, b):
    lum = lambda h: sum(w * c for w, c in zip((0.2126, 0.7152, 0.0722), _lin(h)))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check_palette(palette=None, surface=None, mode="light", pairs="adjacent", verbose=True):
    """Run the computable color checks. Returns (ok, rows)."""
    palette = palette or _theme.current().categorical
    surface = surface or _theme.current().surface
    lo, hi = _BAND[mode]
    n = len(palette)
    plist = ([(i, j) for i in range(n) for j in range(i + 1, n)] if pairs == "all"
             else [(i, i + 1) for i in range(n - 1)])
    rows, ok = [], True

    off = [(c, round(oklch(c)[0], 3)) for c in palette if not lo <= oklch(c)[0] <= hi]
    rows.append(("Lightness band", "FAIL" if off else "PASS", off or f"all in L {lo}-{hi}"))
    lowc = [(c, round(oklch(c)[1], 3)) for c in palette if oklch(c)[1] < _CHROMA_FLOOR]
    rows.append(("Chroma floor", "FAIL" if lowc else "PASS", lowc or f"all >= {_CHROMA_FLOOR}"))
    ok &= not off and not lowc

    worst = min(((delta_e(palette[i], palette[j], k), k, palette[i], palette[j])
                 for k in ("protan", "deutan") for i, j in plist), default=None)
    if worst:
        state = "PASS" if worst[0] >= _CVD_TARGET else "WARN" if worst[0] >= _CVD_FLOOR else "FAIL"
        ok &= state != "FAIL"
        rows.append(("CVD separation", state, f"worst {worst[2]}<->{worst[3]} ΔE {worst[0]:.1f} ({worst[1]})"))
        nw = min((delta_e(palette[i], palette[j]), palette[i], palette[j]) for i, j in plist)
        state = "PASS" if nw[0] >= _NORMAL_FLOOR else "FAIL"
        ok &= state == "PASS"
        rows.append(("Normal-vision floor", state, f"worst {nw[1]}<->{nw[2]} ΔE {nw[0]:.1f}"))

    low = [(c, round(contrast(c, surface), 2)) for c in palette if contrast(c, surface) < _CONTRAST_MIN]
    rows.append(("Contrast vs surface", "WARN" if low else "PASS",
                 f"needs labels/table: {low}" if low else f"all >= {_CONTRAST_MIN}:1"))

    if verbose:
        print(f"Palette ({mode}, surface {surface}, {pairs} pairs): {n} slots")
        for name, state, detail in rows:
            print(f"  [{state:4}] {name:22} {detail}")
        print("  ->", "ALL CHECKS PASS" if ok else "FAILED")
    return ok, rows
