"""paperplot — plotting helpers for the paper. Visual choices live in YAML themes."""
from . import colors, stats, theme
from .axes3d import set_equal, style3d, subplots3d
from .bars import bar
from .style import figsize, save, subplots, use
from .theme import Theme, ThemeError, load

__all__ = ["colors", "stats", "theme", "bar", "use", "save", "subplots", "subplots3d",
           "style3d", "set_equal", "figsize",
           "load", "Theme", "ThemeError"]
