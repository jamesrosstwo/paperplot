"""Live paper clones that tests must never use or write to.

No side effects and no designer.config import, so tests/conftest.py can use it before config is loaded.

Defaults: the old Windows clone, borabora's clone (~/paper-rss) and its mount point inside the designer container
(/paper). DESIGNER_LIVE_PAPERS (os.pathsep-separated) adds more.
"""
import os
from pathlib import Path

LIVE_PAPER_WINDOWS = r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf"
DEFAULTS = (LIVE_PAPER_WINDOWS, "~/paper-rss", "/paper")


def _norm(p) -> str:
    return os.path.normcase(str(Path(os.path.expanduser(str(p))).resolve()))


def paths() -> list[str]:
    extra = [p for p in os.environ.get("DESIGNER_LIVE_PAPERS", "").split(os.pathsep) if p.strip()]
    return [_norm(p) for p in (*DEFAULTS, *extra)]


def is_live(p) -> bool:
    """True if `p` is a live paper clone or lies inside one."""
    q = _norm(p)
    return any(q == live or q.startswith(live.rstrip(os.sep) + os.sep) for live in paths())
