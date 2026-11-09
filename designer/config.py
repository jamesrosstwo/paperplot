"""Paths and settings for the plot designer. Every module imports these instead of hard-coding paths.

Everything is read from the environment once, at import time (the CLI flags of `python -m designer` / serve.py set these
variables *before* this module is imported):

  DESIGNER_PAPER_ROOT   the LaTeX project. Windows default: James's live Overleaf clone; required on Linux/macOS.
  DESIGNER_STATE_DIR    where specs / data registry / cache live (default: designer/ itself)
  DESIGNER_HOST         bind address (default 127.0.0.1). Not loopback => DESIGNER_TOKEN is required.
  DESIGNER_PORT         server port (default 8766)
  DESIGNER_BASE_PATH    URL prefix when served under a reverse-proxy subpath, e.g. /designer (default: none)
  DESIGNER_TOKEN        shared secret; when set, /api/*, /paper.pdf and /context/* need it
  DESIGNER_READONLY     1/true/yes: every endpoint that writes the paper repo, specs, the registry or git answers 403
  DESIGNER_LATEX_BIN    directory holding latexmk / pdflatex / synctex (default: the MiKTeX bin dir on Windows if it
                        exists, else whatever is on PATH)
  DESIGNER_DATA_ROOTS   directories (os.pathsep-separated: ':' on Linux, ';' on Windows) that datasets may live in.
                        Unset on a tokenless loopback server = anywhere (the local tool); unset with a token or a
                        non-loopback host = nothing can be registered until it is set. The Docker scripts set it to the
                        mounted data dirs.
  DESIGNER_DEBUG        1/true: 500 responses include the Python traceback (default: only on a tokenless loopback
                        server; otherwise the traceback goes to stderr only)
"""
import os
import re
import shutil
import sys
from pathlib import Path

VERSION = "0.3.0"
IS_WINDOWS = os.name == "nt"


class ConfigError(RuntimeError):
    """The environment does not describe a usable configuration (raised at import time, with a clear message)."""


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() else default


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def normalize_base_path(p: str | None) -> str:
    """'' or '/a/b' (leading slash, no trailing slash). Raises ConfigError on characters that do not belong in a path."""
    p = (p or "").strip()
    if p in ("", "/"):
        return ""
    p = "/" + p.strip("/")
    if not re.fullmatch(r"(/[A-Za-z0-9._~\-]+)+", p):
        raise ConfigError(f"DESIGNER_BASE_PATH {p!r}: use a plain URL path such as /designer")
    return p


DESIGNER_DIR = Path(__file__).resolve().parent
PAPERPLOT_ROOT = DESIGNER_DIR.parent                       # the folder that holds the paperplot package

from designer.live_papers import LIVE_PAPER_WINDOWS  # noqa: E402  (the Windows default paper root)


def _paper_root() -> Path:
    p = _env("DESIGNER_PAPER_ROOT")
    if p:
        return Path(os.path.expanduser(p)).resolve()
    if IS_WINDOWS:
        return Path(LIVE_PAPER_WINDOWS).resolve()
    raise ConfigError("DESIGNER_PAPER_ROOT is not set. On Linux/macOS there is no default paper: set "
                      "DESIGNER_PAPER_ROOT (or pass --paper-root) to the LaTeX project directory.")


PAPER_ROOT = _paper_root()
MAIN_TEX = "paper.tex"                                      # relative to PAPER_ROOT; body text is \input from sections/

STATE_DIR = Path(os.path.expanduser(_env("DESIGNER_STATE_DIR", str(DESIGNER_DIR)))).resolve()
SPECS_DIR = STATE_DIR / "specs"                             # one <id>.yaml per designed figure
DATA_DIR = STATE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"                  # datasets by reference (never copies)
LINKS_DIR = DATA_DIR / "links"                              # junctions / symlinks to registered data
CACHE_DIR = STATE_DIR / ".cache"                            # previews, context compiles, synctex results

STATIC_DIR = DESIGNER_DIR / "static"

HOST = _env("DESIGNER_HOST", "127.0.0.1")
PORT = int(_env("DESIGNER_PORT", "8766"))
BASE_PATH = normalize_base_path(_env("DESIGNER_BASE_PATH", ""))
TOKEN = _env("DESIGNER_TOKEN")
READONLY = _truthy(_env("DESIGNER_READONLY"))
DEBUG = _truthy(_env("DESIGNER_DEBUG"))


def _data_roots() -> list[Path] | None:
    v = _env("DESIGNER_DATA_ROOTS")
    if v is None:
        return None
    return [Path(os.path.realpath(os.path.expanduser(p.strip()))) for p in v.split(os.pathsep) if p.strip()]


DATA_ROOTS = _data_roots()      # None = not configured (see data_roots_policy)


def data_roots_policy() -> list[Path] | None:
    """Where datasets may live: None = anywhere (tokenless loopback local tool, DESIGNER_DATA_ROOTS unset), else the
    list of allowed directories ([] = nothing may be registered). Read at call time so tests can patch config."""
    if DATA_ROOTS is not None:
        return list(DATA_ROOTS)
    if TOKEN or not is_loopback(HOST):
        return []
    return None


def show_tracebacks() -> bool:
    """Tracebacks in 500 responses only for DESIGNER_DEBUG or the tokenless loopback local tool."""
    return DEBUG or (not TOKEN and is_loopback(HOST))

PYTHON = sys.executable

MIKTEX_BIN = r"C:\Users\James\AppData\Local\Programs\MiKTeX\miktex\bin\x64"   # the Windows default for LATEX_BIN


def _latex_bin() -> str | None:
    p = _env("DESIGNER_LATEX_BIN")
    if p:
        return os.path.expanduser(p)
    if IS_WINDOWS and os.path.isdir(MIKTEX_BIN):
        return MIKTEX_BIN
    return None                                             # rely on PATH


LATEX_BIN = _latex_bin()


def latex_env() -> dict:
    """os.environ for running LaTeX tools: LATEX_BIN (if set and existing) first on PATH. On Windows the PATH entry
    containing claude.exe breaks MiKTeX, so it is dropped (Linux has no such entry)."""
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if IS_WINDOWS:
        parts = [p for p in parts if "claude.exe" not in p.lower()]
    if LATEX_BIN and os.path.isdir(LATEX_BIN):
        parts = [LATEX_BIN] + [p for p in parts if os.path.normcase(p) != os.path.normcase(LATEX_BIN)]
    if IS_WINDOWS and not shutil.which("perl", path=os.pathsep.join(parts)):
        # MiKTeX's latexmk is a Perl script. A Git Bash shell has Git's perl on PATH, but a server started from a
        # desktop app or service usually doesn't ("MiKTeX could not find the script engine 'perl'"). Append (not
        # prepend) a known perl so nothing else on PATH is shadowed.
        for d in (r"C:\Strawberry\perl\bin", r"C:\Program Files\Git\usr\bin", r"C:\Program Files (x86)\Git\usr\bin"):
            if os.path.isfile(os.path.join(d, "perl.exe")):
                parts.append(d)
                break
    env["PATH"] = os.pathsep.join(parts)
    # TeX Live wraps .log lines at 79 characters (texmf.cnf max_print_line), which splits "! Undefined control
    # sequence." and file names across lines and breaks the log parser; kpathsea lets the environment override it.
    # MiKTeX does not wrap and ignores these.
    env.setdefault("max_print_line", "10000")
    env.setdefault("error_line", "254")
    env.setdefault("half_error_line", "238")
    # TeX Live (kpathsea) file-access policy, forced: TeX may read only files below the working directory or found on
    # its search paths (TEXINPUTS, the TeX tree) and write only below the working directory. Debian's default
    # openin_any=a would let \input{/abs/path} or \verbatiminput typeset any readable file (mounted credentials,
    # ~/.ssh) into a PDF the client downloads. MiKTeX ignores these variables.
    env["openin_any"] = "p"
    env["openout_any"] = "p"
    return env


def latex_tool(name: str) -> str:
    """Absolute path of a TeX program (latexmk, pdflatex, synctex, ...) found via latex_env()'s PATH, else `name`."""
    found = shutil.which(name, path=latex_env().get("PATH"))
    return found or name


def is_loopback(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and 'localhost'."""
    import ipaddress
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


for _d in (SPECS_DIR, DATA_DIR, LINKS_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
