"""Load, merge and validate YAML themes. The active theme drives colors, style and bars."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

THEMES_DIR = Path(__file__).parent / "themes"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_COLOR_LIST_ROLES = ("categorical", "sequential", "diverging")
_COLOR_ROLES = ("ink", "ink_muted", "baseline", "grid", "surface")


class ThemeError(ValueError):
    pass


# LaTeX 'pt' is 1/72.27 in (TeX point), 'bp' is the PostScript point 1/72 in.
_UNITS = {"in": 1.0, "pt": 1 / 72.27, "bp": 1 / 72, "cm": 1 / 2.54, "mm": 1 / 25.4}
_LEN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(in|pt|bp|cm|mm)?\s*$")


def length_in(v, where="length") -> float:
    """'3.25in' | '241.14749pt' | '8.25cm' | '82.5mm' | 3.25 -> inches.
    'pt' is the TeX point, so \\the\\columnwidth can be pasted verbatim."""
    if isinstance(v, (int, float)):
        return float(v)
    m = _LEN.match(str(v))
    if not m:
        raise ThemeError(f"{where}: {v!r} is not a length (e.g. 3.25in, 241.1pt, 8.2cm)")
    return float(m.group(1)) * _UNITS[m.group(2) or "in"]


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _find(name_or_path: str | Path) -> Path:
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p
    builtin = THEMES_DIR / f"{name_or_path}.yaml"
    if builtin.exists():
        return builtin
    raise ThemeError(f"theme not found: {name_or_path!r} (looked for a file and in {THEMES_DIR})")


def _read(name_or_path, seen=()) -> dict:
    path = _find(name_or_path)
    if path in seen:
        raise ThemeError(f"circular 'extends' at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = data.pop("extends", None)
    if parent:
        data = _merge(_read(parent, seen + (path,)), data)
    return data


class Theme:
    """A resolved theme: every color role is a concrete '#RRGGBB' string."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.name = raw.get("name", "unnamed")
        self.swatches = {k: self._hex(v, f"swatches.{k}") for k, v in raw.get("swatches", {}).items()}
        c = raw.get("colors", {})
        self.categorical = [self.resolve(x, "colors.categorical") for x in c.get("categorical", [])]
        self.sequential = [self.resolve(x, "colors.sequential") for x in c.get("sequential", [])]
        self.diverging = [self.resolve(x, "colors.diverging") for x in c.get("diverging", [])]
        self.paired = {k: self.resolve(v, f"colors.paired.{k}") for k, v in c.get("paired", {}).items()}
        for role in _COLOR_ROLES:
            if role not in c:
                raise ThemeError(f"missing colors.{role}")
            setattr(self, role, self.resolve(c[role], f"colors.{role}"))
        if not self.categorical:
            raise ThemeError("colors.categorical is empty")
        self.font = raw.get("font", {})
        self.figure = raw.get("figure", {})
        self.axes = raw.get("axes", {})
        self.bars = raw.get("bars", {})

    @staticmethod
    def _hex(v, where):
        if not isinstance(v, str) or not _HEX.match(v.strip()):
            raise ThemeError(f"{where}: {v!r} is not a #rrggbb color (quote it in YAML)")
        return v.strip().upper()

    def resolve(self, ref: str, where: str = "color") -> str:
        """Swatch name or raw hex -> '#RRGGBB'."""
        if isinstance(ref, str) and ref in self.swatches:
            return self.swatches[ref]
        if isinstance(ref, str) and _HEX.match(ref.strip()):
            return ref.strip().upper()
        raise ThemeError(f"{where}: unknown swatch {ref!r}; known: {', '.join(self.swatches)}")

    def __repr__(self):
        return f"<Theme {self.name}: {len(self.categorical)} categorical colors>"


def load(name_or_path: str | Path = "default") -> Theme:
    """Load a built-in theme by name or a .yaml file by path (supports `extends:`)."""
    return Theme(_read(name_or_path))


_current: Theme | None = None


def current() -> Theme:
    global _current
    if _current is None:
        _current = load("default")
    return _current


def set_current(theme: Theme) -> None:
    global _current
    _current = theme
