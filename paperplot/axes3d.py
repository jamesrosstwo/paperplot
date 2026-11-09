"""3D axes styled from the active theme (the `axes3d:` section)."""
from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (registers the projection)

from . import theme as _theme
from .style import figsize


def subplots3d(width="single", aspect=None, height=None, elev=None, azim=None):
    """Figure with one 3D axes filling it, at a theme width. Returns (fig, ax).

    3D axes do not take part in constrained layout, so the axes rectangle is
    set from axes3d.rect (left, bottom, width, height in figure fractions).
    """
    t = _theme.current()
    cfg = t.raw.get("axes3d", {})
    fig = plt.figure(figsize=figsize(width, aspect, height))
    fig.set_layout_engine("none")
    ax = fig.add_axes(cfg.get("rect", [0.0, 0.0, 1.0, 1.0]), projection="3d",
                      proj_type=cfg.get("projection", "ortho"))
    ax.view_init(elev=cfg.get("elev", 25) if elev is None else elev,
                 azim=cfg.get("azim", -60) if azim is None else azim)
    ax.computed_zorder = False  # draw order follows each artist's zorder, not depth sorting
    style3d(ax)
    return fig, ax


def style3d(ax) -> None:
    """Flat panes, hairline grid and recessive axis lines, per the theme."""
    t = _theme.current()
    cfg = t.raw.get("axes3d", {})
    sizes = t.font.get("sizes", {})
    pane = t.resolve(cfg.get("pane_color", t.surface), "axes3d.pane_color")
    grid_w = cfg.get("grid_width", t.axes.get("grid_width", 0.25))
    line_w = cfg.get("axis_width", t.axes.get("spine_width", 0.6))
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_pane_color(pane)
        axis.pane.set_edgecolor(t.grid)
        axis.pane.set_linewidth(grid_w)
        axis.line.set_color(t.baseline)
        axis.line.set_linewidth(line_w)
        axis.line.set_visible(line_w > 0)
        axis._axinfo["grid"].update(color=t.grid, linewidth=grid_w, linestyle="-")
        axis._axinfo["tick"].update(inward_factor=0, outward_factor=0)  # no tick marks
        axis.set_tick_params(labelsize=sizes.get("tick3d", sizes.get("ytick", sizes.get("tick", 6))),
                             pad=cfg.get("tick_pad", -4), colors=t.ink_muted)
        axis.label.set_size(sizes.get("axis_label3d", sizes.get("tick", 6)))
        axis.label.set_color(t.ink_muted)
        axis.labelpad = cfg.get("label_pad", -10)
    ax.grid(cfg.get("grid", True))


def set_equal(ax, xyz, pad: float = 0.02, zoom: float | None = None) -> None:
    """Equal data scale on all three axes, with limits fitted to `xyz` (N, 3) in
    plot coordinates. The box aspect follows the data extents. `zoom` scales the
    drawn box within the axes (default: axes3d.zoom in the theme)."""
    xyz = np.asarray(xyz, float)
    lo, hi = xyz.min(0), xyz.max(0)
    span = hi - lo
    lo, hi = lo - pad * span.max(), hi + pad * span.max()
    ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2])
    if zoom is None:
        zoom = _theme.current().raw.get("axes3d", {}).get("zoom", 1.0)
    ax.set_box_aspect(hi - lo, zoom=zoom)
