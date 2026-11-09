"""LaTeX side of the plot designer: find figures in the paper, compile it, locate figures on the PDF pages,
edit figure environments in place, and build fast "context" previews.

Contract section 3 (designer/CONTRACT.md). All paths come from designer.config; ``root`` defaults to
``config.PAPER_ROOT`` (resolved at call time).

Locating figures
----------------
``synctex view`` on an ``\\includegraphics`` line returns the box of the *unscaled* image (graphicx scales it with a
PDF transform that SyncTeX does not know about): for ``projection.pdf`` (``width=\\linewidth``) it reports W=583
H=547 bp, the file's natural size, on a 612 bp wide page. So ``locate`` reads ``paper.synctex.gz`` directly and
takes the *outermost* hbox record attributed to the ``\\includegraphics`` line, which is the scaled image box
(``\\Gscale@box``). When that line has no records (content read as a macro argument, e.g. the headline inside
``\\twocolumn[{...}]`` is attributed to the closing ``}]`` line), it searches the following lines of the same file for
an hbox whose aspect ratio matches the image file (PDF /CropBox|/MediaBox, PNG/JPEG header). ``synctex view`` is the
last resort (page only is reliable; the box is flagged ``approx``).

Returned boxes are in PDF points (bp), origin top-left of the page: ``{page (1-based), x, y, w, h, page_w, page_h,
method, images: [...], float_box?, stale?}``. ``x, y, w, h`` cover the image(s) only (union of all
``\\includegraphics`` of the figure on its page); ``float_box`` (when found) covers the whole float incl. caption.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import config


class StaleError(Exception):
    """The file changed since the sha the caller holds was taken."""


# --------------------------------------------------------------------------------------------- helpers

FIG_ENVS = ("figure", "figure*", "wrapfigure", "wrapfigure*", "SCfigure", "SCfigure*")
GRAPHIC_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".mps", ".jbig2", ".jb2", ".eps",
                ".PDF", ".PNG", ".JPG", ".JPEG")
BP_PER_SP = 72.0 / 72.27 / 65536.0
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_COMPILE_LOCK = threading.Lock()      # one full paper compile at a time
_CONTEXT_LOCK = threading.Lock()      # one context compile at a time
_WRITE_LOCK = threading.Lock()        # serialise read-check-write of .tex files


def _root(root) -> Path:
    return Path(root).resolve() if root is not None else config.PAPER_ROOT


def _main_stem() -> str:
    return Path(config.MAIN_TEX).stem


def _exe(name: str) -> str:
    """The TeX program `name` from config.LATEX_BIN / PATH (MiKTeX on Windows, TeX Live on Linux)."""
    return config.latex_tool(name)


_MIKTEX: dict[str, bool] = {}


def _is_miktex() -> bool:
    """MiKTeX's pdflatex takes -include-directory=; TeX Live's does not (it uses TEXINPUTS). Cached per binary."""
    exe = _exe("pdflatex")
    if exe not in _MIKTEX:
        if "miktex" in exe.lower():
            _MIKTEX[exe] = True
        else:
            try:
                r = subprocess.run([exe, "--version"], capture_output=True, timeout=30, env=config.latex_env(),
                                   stdin=subprocess.DEVNULL, creationflags=_NOWINDOW)
                _MIKTEX[exe] = b"miktex" in (r.stdout + r.stderr).lower()
            except (OSError, subprocess.TimeoutExpired):
                _MIKTEX[exe] = False
    return _MIKTEX[exe]


def _no_shell_escape() -> str:
    """pdflatex flag that turns \\write18 off completely (TeX Live's default is a restricted whitelist)."""
    return "-disable-write18" if _is_miktex() else "-no-shell-escape"


CONTEXT_TIMEOUT = 30          # seconds; a context preview is one pdflatex pass over a page or two


class Busy(RuntimeError):
    """Too many context compiles are already running or waiting (the server answers 429)."""


_CONTEXT_WAITERS = threading.BoundedSemaphore(2)      # one running + one waiting; more are refused (Busy)


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _rel(path, root: Path) -> str:
    """Path relative to root (posix) when inside it, else the absolute posix path."""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        return Path(os.path.normpath(str(p))).relative_to(Path(os.path.normpath(str(root)))).as_posix()
    except ValueError:
        return Path(os.path.normpath(str(p))).as_posix()


def _code(line: str) -> str:
    """The part of a line before the first unescaped %."""
    i = 0
    while True:
        j = line.find("%", i)
        if j < 0:
            return line
        k, bs = j - 1, 0
        while k >= 0 and line[k] == "\\":
            bs += 1
            k -= 1
        if bs % 2 == 0:
            return line[:j]
        i = j + 1


def _is_commented(line: str) -> bool:
    return line.lstrip().startswith("%")


def _uncomment(line: str) -> str:
    return _code(line.lstrip().lstrip("%"))


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _split_lines(text: str) -> list[str]:
    return text.splitlines()


def _braced(s: str, i: int):
    """s[i] == '{' -> (content, index after closing brace) with brace balancing; None if unbalanced."""
    if i >= len(s) or s[i] != "{":
        return None
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None


def _bracketed(s: str, i: int):
    """s[i] == '[' -> (content, index after ']') counting brackets only at brace depth 0."""
    if i >= len(s) or s[i] != "[":
        return None
    depth_b, j = 0, i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth_b += 1
        elif c == "}":
            depth_b -= 1
        elif c == "]" and depth_b == 0:
            return s[i + 1:j], j + 1
        j += 1
    return None


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


_TOKEN_RE = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}|\\captionof\*?\s*\{figure\}|\\twocolumn\s*\[|\\[\\{}]|[{}]")


def _balanced(code_lines: list[str]) -> bool:
    depth, stack = 0, []
    for line in code_lines:
        for m in _TOKEN_RE.finditer(line):
            t = m.group(0)
            if m.group(1) == "begin":
                stack.append(m.group(2).strip())
            elif m.group(1) == "end":
                if not stack or stack[-1] != m.group(2).strip():
                    return False
                stack.pop()
            elif t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0 and not stack


# --------------------------------------------------------------------------------------------- scanning

_INPUT_RE = re.compile(r"\\(input|include)\s*(?:\{([^{}]+)\}|([^\s{}\\%]+))")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})*)\s*\}")


def _graphicspath(root: Path) -> list[str]:
    main = root / config.MAIN_TEX
    if not main.exists():
        return []
    code = "\n".join(_code(l) for l in _split_lines(_read_text(main)))
    m = _GRAPHICSPATH_RE.search(code)
    return re.findall(r"\{([^{}]*)\}", m.group(1)) if m else []


def _resolve_graphic(path: str, root: Path, gpaths: list[str]) -> str | None:
    p = path.strip().strip('"')
    if not p:
        return None
    dirs = [""] + gpaths
    has_ext = os.path.splitext(p)[1].lower() in {e.lower() for e in GRAPHIC_EXTS}
    for d in dirs:
        base = Path(d) / p if d else Path(p)
        if not base.is_absolute():
            base = root / base
        cands = [base] if has_ext else [Path(str(base) + e) for e in GRAPHIC_EXTS] + [base]
        for c in cands:
            if c.is_file():
                return _rel(c, root)
    return None


def _spec_outputs() -> dict[str, str]:
    """{normalised output path: spec id}. Uses designer.spec when available, else reads SPECS_DIR/*.yaml."""
    out = {}
    try:
        from . import spec as _spec  # lazy: owned by another builder
        for s in _spec.list_specs():
            if s.get("output"):
                out[Path(s["output"]).as_posix().lstrip("./")] = s["id"]
        return out
    except Exception:
        pass
    try:
        import yaml
        for f in sorted(Path(config.SPECS_DIR).glob("*.yaml")):
            try:
                d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if d.get("output"):
                out[Path(str(d["output"])).as_posix().lstrip("./")] = d.get("id") or f.stem
    except Exception:
        pass
    return out


def _resolve_input(name: str, root: Path) -> Path | None:
    name = name.strip().strip('"')
    p = root / name
    for c in (p, Path(str(p) + ".tex")):
        if c.is_file():
            try:
                c.resolve().relative_to(root)
            except ValueError:
                return None
            return c
    return None


def _env_info(lines: list[str], b: int, e: int, commented: bool) -> dict:
    """Extract label/caption/graphics from lines[b..e] (0-based, inclusive)."""
    texts, starts, pos = [], [], 0
    for i in range(b, e + 1):
        t = _uncomment(lines[i]) if commented else _code(lines[i])
        starts.append(pos)
        texts.append(t)
        pos += len(t) + 1
    s = "\n".join(texts)

    def line_of(off):
        k = 0
        while k + 1 < len(starts) and starts[k + 1] <= off:
            k += 1
        return b + k + 1

    label = None
    m = re.search(r"\\label\s*\{([^{}]*)\}", s)
    if m:
        label = m.group(1).strip()
    caption = None
    for m in re.finditer(r"\\caption(?:of\*?\s*\{figure\})?\*?", s):
        i = _skip_ws(s, m.end())
        if i < len(s) and s[i] == "[" and not m.group(0).startswith("\\captionof"):
            r = _bracketed(s, i)
            if r:
                i = _skip_ws(s, r[1])
        r = _braced(s, i)
        if r is not None:
            caption = re.sub(r"\s+", " ", r[0]).strip()[:200]
            break
    graphics = []
    for m in re.finditer(r"\\includegraphics\*?", s):
        i = _skip_ws(s, m.end())
        opts = None
        while i < len(s) and s[i] == "[":
            r = _bracketed(s, i)
            if not r:
                break
            opts = r[0].strip()
            i = _skip_ws(s, r[1])
        r = _braced(s, i)
        if r is None:
            continue
        graphics.append({"path": r[0].strip(), "options": opts, "line": line_of(m.start())})
    return {"label": label, "caption": caption, "graphics": graphics}


def _in_twocolumn(code: str, pos: int) -> bool:
    """Is offset ``pos`` of ``code`` inside the optional argument of the last ``\\twocolumn[`` before it?"""
    for m in re.finditer(r"\\twocolumn(?![A-Za-z@])", code[:pos]):
        i = _skip_ws(code, m.end())
        if i < len(code) and code[i] == "[":
            r = _bracketed(code, i)
            if r is None or r[1] > pos:
                return True
    return False


def _scan_file(rel: str, root: Path) -> tuple[list[dict], list[tuple[int, str]]]:
    """(figure envs of this file, [(line, input rel path)]) — lines 1-based."""
    lines = _split_lines(_read_text(root / rel))
    codes = [_code(l) for l in lines]
    figs, inputs = [], []

    for i, c in enumerate(codes):
        for m in _INPUT_RE.finditer(c):
            name = m.group(2) or m.group(3)
            p = _resolve_input(name, root)
            if p is not None:
                inputs.append((i + 1, _rel(p, root)))

    # active environments
    flat = "\n".join(codes)
    line_starts = [0]
    for c in codes:
        line_starts.append(line_starts[-1] + len(c) + 1)

    def lno(off):  # 0-based line index of an offset
        lo, hi = 0, len(codes) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo

    stack = []  # [name, begin_line_idx, begin_off, has_captionof]
    for m in _TOKEN_RE.finditer(flat):
        t = m.group(0)
        if m.group(1) == "begin":
            stack.append([m.group(2).strip(), lno(m.start()), m.start(), False])
        elif m.group(1) == "end":
            name = m.group(2).strip()
            # pop to the matching begin (tolerate mismatches)
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == name:
                    fr = stack[k]
                    del stack[k:]
                    inside_fig = any(f[0] in FIG_ENVS for f in stack)
                    if name in FIG_ENVS or (fr[3] and name != "document" and not inside_fig):
                        figs.append({"env": name, "b": fr[1], "e": lno(m.start()), "commented": False,
                                     "in_twocolumn": _in_twocolumn(flat, fr[2])})
                    break
        elif t.startswith("\\captionof"):
            for fr in reversed(stack):
                if fr[0] in FIG_ENVS:
                    break
                if fr[0] != "document":
                    fr[3] = True
                    break

    # fully commented figure environments
    i = 0
    while i < len(lines):
        if _is_commented(lines[i]):
            m = re.search(r"\\begin\s*\{(" + "|".join(re.escape(x) for x in FIG_ENVS) + r")\}", _uncomment(lines[i]))
            if m:
                name, j, ok = m.group(1), i + 1, False
                end_re = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
                while j < len(lines):
                    if lines[j].strip() == "":
                        j += 1
                        continue
                    if not _is_commented(lines[j]):
                        break
                    if end_re.search(_uncomment(lines[j])):
                        ok = True
                        break
                    j += 1
                if ok:
                    figs.append({"env": name, "b": i, "e": j, "commented": True, "in_twocolumn": False})
                    i = j + 1
                    continue
        i += 1

    out = []
    for f in figs:
        info = _env_info(lines, f["b"], f["e"], f["commented"])
        out.append({"file": rel, "begin_line": f["b"] + 1, "end_line": f["e"] + 1, "env": f["env"],
                    "commented": f["commented"], "in_twocolumn": f["in_twocolumn"], **info})
    return out, inputs


_INPUTS_CACHE: dict[str, tuple] = {}


def _inputs_of(rel: str, root: Path) -> list[tuple[int, str]]:
    """[(line, input rel path)] of one file, as _scan_file finds them. The \\input/\\include names are cached per file
    on (mtime_ns, size); only their resolution (a few stats) runs on every call, so a file that appears later is
    still picked up. Every TeX-editor request walks the tree, so this keeps polling cheap for a large paper."""
    path = root / rel
    st = path.stat()
    key = _norm(path)
    hit = _INPUTS_CACHE.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        names = hit[1]
    else:
        names = []
        for i, line in enumerate(_split_lines(_read_text(path))):
            for m in _INPUT_RE.finditer(_code(line)):
                names.append((i + 1, m.group(2) or m.group(3)))
        _INPUTS_CACHE[key] = ((st.st_mtime_ns, st.st_size), names)
    out = []
    for ln, name in names:
        q = _resolve_input(name, root)
        if q is not None:
            out.append((ln, _rel(q, root)))
    return out


def _reachable_files(root: Path) -> list[str]:
    """Posix paths (relative to root) of MAIN_TEX and every file it pulls in via \\input/\\include."""
    seen: set[str] = set()
    out: list[str] = []

    def walk(rel: str):
        key = _norm(root / rel)
        if key in seen or not (root / rel).is_file():
            return
        seen.add(key)
        out.append(rel)
        for _, r in _inputs_of(rel, root):
            walk(r)

    walk(Path(config.MAIN_TEX).as_posix())
    return out


def scan_figures(root=None) -> list[dict]:
    """Every figure environment reachable from MAIN_TEX via \\input/\\include, in document order.

    Also reports ``center``-like environments that hold a ``\\captionof{figure}`` (e.g. the headline inside
    ``\\twocolumn[...]``) with ``env`` = that environment's name, and fully commented figure environments with
    ``commented: true``.
    """
    root = _root(root)
    gpaths = _graphicspath(root)
    specs = _spec_outputs()
    ordered: list[dict] = []
    seen: set[str] = set()

    def walk(rel: str):
        key = _norm(root / rel)
        if key in seen or not (root / rel).is_file():
            return
        seen.add(key)
        figs, inputs = _scan_file(rel, root)
        events = [(f["begin_line"], 0, f) for f in figs] + [(ln, 1, r) for ln, r in inputs]
        for _, kind, payload in sorted(events, key=lambda x: (x[0], x[1])):
            if kind == 0:
                ordered.append(payload)
            else:
                walk(payload)

    walk(Path(config.MAIN_TEX).as_posix())

    counts: dict[str, int] = {}
    for f in ordered:
        for g in f["graphics"]:
            g["resolved"] = _resolve_graphic(g["path"], root, gpaths)
        stem = Path(f["graphics"][0]["path"]).stem if f["graphics"] else None
        base = f"{f['file']}:{f['label'] or stem or 'L%d' % f['begin_line']}"
        counts[base] = counts.get(base, 0) + 1
        f["fid"] = base if counts[base] == 1 else f"{base}#{counts[base]}"
        f["spec_id"] = None
        for g in f["graphics"]:
            if g["resolved"] and g["resolved"] in specs:
                f["spec_id"] = specs[g["resolved"]]
                break
    keys = ["fid", "file", "begin_line", "end_line", "env", "label", "caption", "graphics", "spec_id",
            "commented", "in_twocolumn"]
    return [{k: f[k] for k in keys} for f in ordered]


def _find_fig(fid: str, root: Path) -> dict:
    for f in scan_figures(root):
        if f["fid"] == fid:
            return f
    raise KeyError(f"unknown figure id: {fid}")


# --------------------------------------------------------------------------------------------- log parsing

_FLE_RE = re.compile(r"^(.*?\.(?:tex|sty|cls|bbl|aux|def|cfg|clo|fd|ltx|out|toc)):(\d+): (.*)$")


def parse_log(text: str, root=None) -> tuple[list[dict], int]:
    """LaTeX errors from a .log -> ([{file, line, message}], warnings_count).

    Understands both ``-file-line-error`` lines (``./sections/x.tex:15: Undefined control sequence.``) and classic
    ``! message`` blocks followed by ``l.<n>``. ``file`` is relative to root (posix) when known, else None.
    """
    root = _root(root)
    lines = text.splitlines()
    errors, seen = [], set()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _FLE_RE.match(ln)
        msg = file = line = None
        if m and not ln.startswith("l."):
            file, line, msg = m.group(1), int(m.group(2)), m.group(3).strip()
        elif ln.startswith("! "):
            msg = ln[2:].strip()
            for j in range(i + 1, min(i + 40, len(lines))):
                mm = re.match(r"^l\.(\d+)", lines[j])
                if mm:
                    line = int(mm.group(1))
                    break
                if lines[j].startswith("! ") or _FLE_RE.match(lines[j]):
                    break
        if msg is not None:
            # a message that continues on the next line (e.g. "LaTeX Error: File `x' not found.")
            if not msg.endswith((".", "!", "?", ")")) and i + 1 < len(lines) and lines[i + 1].strip() \
                    and not lines[i + 1].startswith(("l.", "! ", "See the", "Type ")):
                msg = (msg + " " + lines[i + 1].strip()).strip()
            if file is not None:
                f = file.strip()
                file = _rel(f[2:] if f.startswith("./") else f, root)
            key = (file, line, msg)
            if key not in seen:
                seen.add(key)
                errors.append({"file": file, "line": line, "message": msg})
        i += 1
    warnings = sum(1 for l in lines if re.search(r"(LaTeX|Package [\w@-]+|Class [\w@-]+|pdfTeX) Warning", l))
    return errors, warnings


# --------------------------------------------------------------------------------------------- running TeX

def _run(cmd: list[str], cwd: Path, timeout: float, env_extra: dict | None = None) -> tuple[int | None, str, bool]:
    """Run with latex_env(); kill the whole process tree on timeout. -> (returncode, combined output, timed_out)."""
    env = config.latex_env()
    env.update(env_extra or {})
    kw = {"creationflags": _NOWINDOW} if os.name == "nt" else {"start_new_session": True}   # POSIX: own process group
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", errors="replace"), False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=_NOWINDOW)
        else:
            import signal
            try:
                os.killpg(p.pid, signal.SIGKILL)      # latexmk and the pdflatex/bibtex it started
            except OSError:
                p.kill()
        out, _ = p.communicate()
        return None, out.decode("utf-8", errors="replace"), True


def _tail(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[-n:])


def compile_paper(root=None, timeout=240) -> dict:
    """Full compile: ``latexmk -pdf -interaction=nonstopmode -f -synctex=1 -file-line-error paper.tex`` in root.

    ``ok`` means a fresh PDF was written (latexmk's exit code is non-zero for this paper because of BibTeX errors
    even when the PDF is fine; it is returned as ``exit_code``). LaTeX errors come from the .log.
    """
    root = _root(root)
    stem = _main_stem()
    pdf = root / f"{stem}.pdf"
    log = root / f"{stem}.log"
    with _COMPILE_LOCK:
        t0 = time.time()
        before = pdf.stat().st_mtime if pdf.exists() else None
        try:  # figure line numbers of the source being compiled; locate_all() uses them for this PDF
            snap = _fig_lines(scan_figures(root))
            hashes = _file_hashes(root)
        except Exception:
            snap = hashes = None
        # -norc: no latexmkrc (a Perl file anyone who can commit to the paper repo could plant) and no user/system
        # rc files either; pdflatex runs with \write18 disabled. %O = latexmk's options, %S = the source file.
        cmd = [_exe("latexmk"), "-norc", "-pdf", f"-pdflatex=pdflatex {_no_shell_escape()} %O %S",
               "-interaction=nonstopmode", "-f", "-synctex=1", "-file-line-error",
               "-max-print-line=10000", config.MAIN_TEX]
        sync = root / f"{stem}.synctex.gz"
        # A PDF built elsewhere (e.g. James's own latexmk run without -synctex) leaves latexmk "up-to-date" with no
        # synctex file, so click-to-source and figure boxes never work. Force a full rebuild (-g) in that case.
        if pdf.exists() and (not sync.exists() or sync.stat().st_mtime < pdf.stat().st_mtime - 1):
            cmd.insert(1, "-g")
        code, out, timed_out = _run(cmd, root, timeout)
        secs = time.time() - t0
        if snap is not None and pdf.exists():
            _save_snapshot(root, pdf, snap, hashes)
    fresh = pdf.exists() and (before is None or pdf.stat().st_mtime > before or pdf.stat().st_mtime >= t0 - 1)
    up_to_date = "up-to-date" in out          # latexmk had nothing to do: the existing PDF is current
    fresh = fresh or (pdf.exists() and up_to_date)
    # a .log older than this run is a previous compile's: don't report its contents as this run's
    log_text = _read_text(log) if log.exists() and log.stat().st_mtime >= t0 - 1 else ""
    errors, nwarn = parse_log(log_text, root) if log_text else ([], 0)
    if timed_out:
        errors.insert(0, {"file": None, "line": None, "message": f"compile timed out after {timeout} s"})
    if not log_text and not fresh:
        errors.append({"file": None, "line": None, "message": "no log written; see log_tail (latexmk output)"})
    if pdf.exists() and not (root / f"{stem}.synctex.gz").exists():
        errors.append({"file": None, "line": None, "message": "no synctex file was written, so clicking the page and "
                       "figure boxes will not work; see log_tail (latexmk output)"})
    bib_errors = 0
    blg = root / f"{stem}.blg"
    if blg.exists() and blg.stat().st_mtime >= t0 - 1:
        m = re.search(r"\(There (?:were|was) (\d+) error", _read_text(blg))
        bib_errors = int(m.group(1)) if m else 0
    return {"ok": bool(fresh and not timed_out), "pdf": str(pdf), "seconds": round(secs, 2), "errors": errors,
            "warnings_count": nwarn, "log_tail": _tail(log_text or out), "exit_code": code,
            "bib_errors_count": bib_errors, "up_to_date": up_to_date}


# --------------------------------------------------------------------------------------------- synctex

_REC_RE = re.compile(r"^([\[\(hv])(\d+),(\d+)(?:,-?\d+)?:(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)")
_SYNC_CACHE: dict[str, tuple] = {}


def _fig_lines(figs: list[dict]) -> dict:
    return {f["fid"]: {"file": f["file"], "begin_line": f["begin_line"], "end_line": f["end_line"],
                       "commented": f["commented"],
                       "graphics": [{"line": g["line"], "path": g["path"], "resolved": g.get("resolved")}
                                    for g in f["graphics"]]}
            for f in figs}


def _snapshot_file(root: Path) -> Path:
    tag = hashlib.sha1(_norm(root).encode("utf-8")).hexdigest()[:12]
    return Path(config.CACHE_DIR) / f"compiled_figures_{tag}.json"


def _file_hashes(root: Path) -> dict:
    """{rel path: sha1} of MAIN_TEX and every file it inputs (the source a compile sees)."""
    out = {}
    for rel in _reachable_files(root):
        try:
            out[rel] = hashlib.sha1((root / rel).read_bytes()).hexdigest()
        except OSError:
            pass
    return out


def _is_stale(rel: str, root: Path, snap_doc: dict | None, pdf_mtime: float) -> bool:
    """True if ``rel`` differs from what the last compile saw. Content hashes when the compile was the designer's
    (latexmk skips a compile when the content is unchanged, so mtimes alone would flag a reverted edit forever),
    else file mtime vs PDF mtime."""
    hashes = (snap_doc or {}).get("hashes")
    if hashes and rel in hashes:
        try:
            return hashlib.sha1((root / rel).read_bytes()).hexdigest() != hashes[rel]
        except OSError:
            return True
    try:
        return (root / rel).stat().st_mtime > pdf_mtime
    except OSError:
        return False


def _save_snapshot(root: Path, pdf: Path, figs: dict, hashes: dict | None = None) -> None:
    try:
        f = _snapshot_file(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"root": _norm(root), "pdf_mtime_ns": pdf.stat().st_mtime_ns, "figs": figs,
                                   "hashes": hashes or {}}), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


def _load_snapshot(root: Path, pdf: Path) -> dict | None:
    """{figs, hashes} recorded when ``pdf`` was compiled by compile_paper(), or None if unknown."""
    try:
        d = json.loads(_snapshot_file(root).read_text(encoding="utf-8"))
        if d.get("root") == _norm(root) and d.get("pdf_mtime_ns") == pdf.stat().st_mtime_ns:
            return d
    except Exception:
        pass
    return None


def _compiled_lines(f: dict, snap: dict | None, root: Path, pdf_mtime: float) -> dict | None:
    """The line numbers of figure ``f`` as they were when the PDF was compiled.

    Locating a figure with line numbers from a source edited after the compile gives the box of whatever sits at
    those lines in the old PDF (another figure, a text line). With a compile snapshot the old lines are used and a
    figure added since the compile has no box. Without a snapshot (PDF compiled outside the designer) the current
    lines are used only if the figure's file has not been modified since the PDF was written.
    """
    if snap is not None:
        return snap.get(f["fid"])
    try:
        if (root / f["file"]).stat().st_mtime > pdf_mtime:
            return None
    except OSError:
        return None
    return f


def _synctex_path(pdf: Path) -> Path | None:
    for c in (pdf.with_suffix(".synctex.gz"), pdf.with_suffix(".synctex")):
        if c.exists():
            return c
    return None


def _load_synctex(path: Path) -> dict:
    """Parse a .synctex(.gz): {inputs: {tag: abs path}, records: [(page, kind, tag, line, x, v, W, H, D)] in bp}."""
    st = path.stat()
    key = _norm(path)
    hit = _SYNC_CACHE.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    inputs, recs = {}, []
    unit, mag, xoff, yoff = 1.0, 1000.0, 0.0, 0.0
    page = 0
    in_content = False
    for ln in text.splitlines():
        if not in_content:
            if ln.startswith("Input:"):
                tag, _, p = ln[6:].partition(":")
                inputs[int(tag)] = p
            elif ln.startswith("Unit:"):
                unit = float(ln[5:])
            elif ln.startswith("Magnification:"):
                mag = float(ln[14:])
            elif ln.startswith("X Offset:"):
                xoff = float(ln[9:])
            elif ln.startswith("Y Offset:"):
                yoff = float(ln[9:])
            elif ln.startswith("Content:"):
                in_content = True
            continue
        if not ln:
            continue
        c = ln[0]
        if c == "{":
            page = int(ln[1:])
        elif c in "[(hv":
            m = _REC_RE.match(ln)
            if m:
                s = unit * mag / 1000.0 * BP_PER_SP
                h, v, w, hh, d = (int(m.group(k)) for k in range(4, 9))
                recs.append((page, m.group(1), int(m.group(2)), int(m.group(3)),
                             (h * unit + xoff) * mag / 1000.0 * BP_PER_SP, (v * unit + yoff) * mag / 1000.0 * BP_PER_SP,
                             w * s, hh * s, d * s))
        elif ln.startswith("Input:"):
            tag, _, p = ln[6:].partition(":")
            inputs[int(tag)] = p
        elif ln.startswith("Postamble:"):
            break
    data = {"inputs": inputs, "records": recs}
    _SYNC_CACHE[key] = ((st.st_mtime_ns, st.st_size), data)
    return data


def _tags_for(data: dict, file_abs: Path, base: Path) -> set[int]:
    want = _norm(file_abs)
    out = set()
    for tag, p in data["inputs"].items():
        q = Path(p)
        if not q.is_absolute():
            q = base / q
        if _norm(q) == want:
            out.add(tag)
    return out


_ASPECT_CACHE: dict[str, tuple] = {}


def _image_aspect(path: Path) -> float | None:
    """Natural width/height of a graphic: PDF /CropBox or /MediaBox, PNG IHDR, JPEG SOF."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = _norm(path)
    hit = _ASPECT_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns:
        return hit[1]
    asp = None
    try:
        data = path.read_bytes()
        ext = path.suffix.lower()
        if ext == ".pdf":
            num = r"(-?[\d.]+)"
            if b"/MediaBox" not in data:                # page dicts packed into object streams
                data = _with_object_streams(data)
            for box in (b"CropBox", b"MediaBox"):
                m = re.search(rb"/" + box + rb"\s*\[\s*" + (rb"\s+".join([num.encode()] * 4)) + rb"\s*\]", data)
                if m:
                    x0, y0, x1, y1 = (float(g) for g in m.groups())
                    if y1 != y0:
                        asp = abs((x1 - x0) / (y1 - y0))
                        break
        elif ext == ".png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            asp = w / h if h else None
        elif ext in (".jpg", ".jpeg"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    asp = w / h if h else None
                    break
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
    except Exception:
        asp = None
    _ASPECT_CACHE[key] = (st.st_mtime_ns, asp)
    return asp


_OBJSTM_RE = re.compile(rb"<<((?:(?!>>).){0,400}?/Type\s*/ObjStm(?:(?!>>).){0,400}?)>>\s*stream\r?\n", re.S)


def _with_object_streams(data: bytes) -> bytes:
    """`data` plus the decompressed contents of its PDF object streams. MiKTeX's pdftex writes page dicts as plain
    objects, but TeX Live's (PDF 1.5, \\pdfobjcompresslevel=2) packs them into /ObjStm streams, where a byte search
    for /MediaBox finds nothing."""
    import zlib
    parts = [data]
    for m in _OBJSTM_RE.finditer(data):
        if b"/FlateDecode" not in m.group(1):
            continue
        start = m.end()
        lm = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", m.group(1))
        end = start + int(lm.group(1)) if lm else data.find(b"endstream", start)
        try:
            parts.append(zlib.decompressobj().decompress(data[start:end]))
        except zlib.error:
            continue
    return b"\n".join(parts)


def _page_size(pdf: Path) -> tuple[float, float] | None:
    """Most common /MediaBox in the PDF (plain page dicts, or page dicts inside object streams)."""
    try:
        data = pdf.read_bytes()
    except OSError:
        return None
    pat = rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]"
    boxes = re.findall(pat, data) or re.findall(pat, _with_object_streams(data))
    if not boxes:
        return None
    sizes = {}
    for b in boxes:
        x0, y0, x1, y1 = (float(v) for v in b)
        k = (round(x1 - x0, 2), round(y1 - y0, 2))
        sizes[k] = sizes.get(k, 0) + 1
    return max(sizes, key=sizes.get)


def _box(rec) -> dict:
    page, _, _, _, x, v, w, h, d = rec
    return {"page": page, "x": round(x, 2), "y": round(v - h, 2), "w": round(w, 2), "h": round(h + d, 2)}


def _image_record(recs, tags, line, aspect, window_end):
    """Outermost hbox of an \\includegraphics line (scaled image), or an aspect-matched hbox just after it."""
    exact = [r for r in recs if r[1] == "(" and r[2] in tags and r[3] == line and r[6] > 0.5 and r[7] > 0.5]
    if exact:
        if aspect:
            m = [r for r in exact if abs(r[6] / r[7] - aspect) <= 0.02 * aspect]
            if m:
                return m[0]
        return exact[0]
    if not aspect:
        return None
    for r in recs:
        if (r[1] == "(" and r[2] in tags and line <= r[3] <= window_end and r[7] >= 10
                and abs(r[6] / r[7] - aspect) <= 0.01 * aspect):
            return r
    return None


def _synctex_view(pdf: Path, file_abs: Path, line: int) -> list[dict]:
    """``synctex view`` results: [{page, x, y, w, h}] (box as reported; for a scaled graphic it is the unscaled one)."""
    exe = _exe("synctex")
    try:
        code, out, _ = _run([exe, "view", "-i", f"{line}:1:{os.path.normpath(str(file_abs))}", "-o", pdf.name],
                            pdf.parent, 30)
    except OSError:
        return []
    res, cur = [], {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        if k == "Page":
            if cur:
                res.append(cur)
            cur = {"page": int(v)}
        elif k in ("h", "v", "W", "H") and cur is not None:
            try:
                cur[k] = float(v)
            except ValueError:
                pass
    if cur:
        res.append(cur)
    out_boxes = []
    for r in res:
        if {"h", "v", "W", "H"} <= r.keys():
            out_boxes.append({"page": r["page"], "x": round(r["h"], 2), "y": round(r["v"] - r["H"], 2),
                              "w": round(r["W"], 2), "h": round(r["H"], 2)})
    return out_boxes


def _locate_in(pdf: Path, file_abs: Path, graphics: list[dict], begin_line: int, end_line: int,
               base: Path) -> dict | None:
    """Core of locate(): graphics = [{line, abs (Path|None), path}]."""
    syn = _synctex_path(pdf)
    if not pdf.exists() or syn is None:
        return None
    data = _load_synctex(syn)
    tags = _tags_for(data, file_abs, base)
    psize = _page_size(pdf)
    images = []
    if tags:
        recs = data["records"]
        for g in graphics:
            asp = _image_aspect(g["abs"]) if g.get("abs") else None
            r = _image_record(recs, tags, g["line"], asp, max(end_line, g["line"]) + 60)
            if r is not None:
                images.append({"graphics": g["path"], **_box(r)})
    method = "synctex-record"
    if not images:
        lines = [g["line"] for g in graphics] or [begin_line]
        for ln in lines:
            v = _synctex_view(pdf, file_abs, ln)
            if v:
                images.append({"graphics": graphics[0]["path"] if graphics else None, **v[0]})
                method = "synctex-view"
                break
    if not images:
        return None
    page = images[0]["page"]
    same = [b for b in images if b["page"] == page]
    x0 = min(b["x"] for b in same)
    y0 = min(b["y"] for b in same)
    x1 = max(b["x"] + b["w"] for b in same)
    y1 = max(b["y"] + b["h"] for b in same)
    out = {"page": page, "x": round(x0, 2), "y": round(y0, 2), "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
           "page_w": psize[0] if psize else None, "page_h": psize[1] if psize else None,
           "method": method, "images": images}
    if method == "synctex-view":
        out["approx"] = True
    if tags:
        for r in data["records"]:
            if r[0] == page and r[1] == "[" and r[2] in tags and r[3] == end_line and r[7] > 1:
                fb = _box(r)
                if fb["y"] <= out["y"] + 1 and fb["y"] + fb["h"] >= out["y"] + out["h"] - 1:
                    out["float_box"] = {k: fb[k] for k in ("x", "y", "w", "h")}
                break
    return out


def _graphics_for(fig: dict, root: Path) -> list[dict]:
    return [{"line": g["line"], "path": g["path"], "abs": (root / g["resolved"]) if g.get("resolved") else None}
            for g in fig.get("graphics", [])]


def locate(fig: dict, root=None) -> dict | None:
    """Page and image box of a figure in the last compiled PDF (see module docstring). None if not found.

    Uses the figure's line numbers from the time of the compile (see _compiled_lines); ``stale: true`` means its
    source file was edited after that compile (the box still matches the PDF on disk)."""
    root = _root(root)
    if fig.get("commented"):
        return None
    pdf = root / f"{_main_stem()}.pdf"
    if not pdf.exists():
        return None
    doc = _load_snapshot(root, pdf)
    src = _compiled_lines(fig, doc["figs"] if doc else None, root, pdf.stat().st_mtime)
    if src is None or src.get("commented"):
        return None
    box = _locate_in(pdf, root / src["file"], _graphics_for(src, root), src["begin_line"], src["end_line"], root)
    if box is not None:
        box["stale"] = _is_stale(fig["file"], root, doc, pdf.stat().st_mtime)
    return box


def locate_all(root=None) -> dict:
    """{fid: box or None} for every figure; cached in CACHE_DIR/locate.json keyed by the PDF/synctex mtimes.

    Boxes always describe the PDF on disk: figures are located with their line numbers at compile time."""
    root = _root(root)
    figs = scan_figures(root)
    pdf = root / f"{_main_stem()}.pdf"
    syn = _synctex_path(pdf)
    if not pdf.exists() or syn is None:
        return {f["fid"]: None for f in figs}
    pm = pdf.stat().st_mtime
    doc = _load_snapshot(root, pdf)
    snap = doc["figs"] if doc else None
    srcs = {f["fid"]: _compiled_lines(f, snap, root, pm) for f in figs}
    sig = hashlib.sha1(json.dumps([[fid, s["file"], s["begin_line"], s["end_line"],
                                    [g["line"] for g in s["graphics"]]] if s else [fid, None]
                                   for fid, s in srcs.items()]).encode()).hexdigest()
    key = f"{_norm(root)}|{pdf.stat().st_mtime_ns}|{syn.stat().st_mtime_ns}|{sig}"
    cache = Path(config.CACHE_DIR) / "locate.json"
    boxes = None
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("key") == key:
            boxes = d["boxes"]
    except Exception:
        pass
    if boxes is None:
        boxes = {}
        for f in figs:
            s = srcs[f["fid"]]
            b = None if (f["commented"] or s is None or s.get("commented")) else _locate_in(
                pdf, root / s["file"], _graphics_for(s, root), s["begin_line"], s["end_line"], root)
            boxes[f["fid"]] = b
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "boxes": boxes}), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass
    stale = {}
    for f in figs:
        b = boxes.get(f["fid"])
        if b is not None:
            if f["file"] not in stale:
                stale[f["file"]] = _is_stale(f["file"], root, doc, pm)
            b["stale"] = stale[f["file"]]
    return boxes


def reverse(page, x, y, root=None) -> dict | None:
    """Click on the page (bp, origin top-left) -> {file (rel to root when inside it), line, column}."""
    root = _root(root)
    pdf = root / f"{_main_stem()}.pdf"
    if not pdf.exists():
        return None
    code, out, _ = _run([_exe("synctex"), "edit", "-o", f"{int(page)}:{float(x):.2f}:{float(y):.2f}:{pdf.name}"],
                        root, 30)
    res = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        if k == "Input" and "file" not in res:
            res["file"] = _rel(v.strip(), root)
        elif k == "Line" and "line" not in res:
            try:
                res["line"] = int(v)
            except ValueError:
                pass
        elif k == "Column" and "column" not in res:
            try:
                res["column"] = int(v)
            except ValueError:
                pass
    if "file" not in res or res.get("line", 0) <= 0:
        # synctex edit answers Line:0 inside an image; use the smallest box record that contains the point
        syn = _synctex_path(pdf)
        if syn is not None:
            data = _load_synctex(syn)
            best = None
            for r in data["records"]:
                if r[0] != int(page) or r[3] <= 0 or r[1] not in "([" or r[6] <= 0:
                    continue
                if r[4] <= x <= r[4] + r[6] and r[5] - r[7] <= y <= r[5] + r[8]:
                    area = r[6] * (r[7] + r[8])
                    if best is None or area < best[0]:
                        best = (area, r)
            if best is not None and best[1][2] in data["inputs"]:
                return {"file": _rel(data["inputs"][best[1][2]], root), "line": best[1][3], "column": -1}
        return None
    return res


# --------------------------------------------------------------------------------------------- editing

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tex_path(file_rel: str, root: Path) -> Path:
    p = (root / file_rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError(f"{file_rel} is outside the paper root")
    if p.suffix.lower() != ".tex" or not p.is_file():
        raise ValueError(f"{file_rel} is not an existing .tex file")
    return p


def read_env(fid, root=None) -> dict:
    """The exact source lines of a figure environment plus the sha256 of its whole file."""
    root = _root(root)
    fig = _find_fig(fid, root)
    path = _tex_path(fig["file"], root)
    raw = path.read_bytes()
    lines = _split_lines(raw.decode("utf-8", errors="replace"))
    text = "\n".join(lines[fig["begin_line"] - 1:fig["end_line"]])
    return {"fid": fig["fid"], "text": text, "file": fig["file"], "begin_line": fig["begin_line"],
            "end_line": fig["end_line"], "sha": _sha(raw), "env": fig["env"], "commented": fig["commented"]}


def _write_lines(path: Path, raw: bytes, new_lines: list[str]):
    nl = "\r\n" if b"\r\n" in raw else "\n"
    text = nl.join(new_lines)
    if raw.endswith((b"\n", b"\r")) or not raw:
        text += nl
    tmp = path.with_name(path.name + ".designer-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _check_env_text(text: str):
    if not text or not text.strip():
        raise ValueError("empty figure environment text")
    codes = [_code(l) for l in text.splitlines()]
    if not any(re.search(r"\\begin\s*\{", c) for c in codes) and not all(_is_commented(l) or not l.strip()
                                                                       for l in text.splitlines()):
        raise ValueError("text has no \\begin{...}")
    if not _balanced(codes):
        raise ValueError("unbalanced braces or \\begin/\\end in the new text")


def write_env(fid, text, expected_sha, root=None) -> dict:
    """Replace exactly the lines of figure ``fid`` with ``text``. Raises StaleError if the file's sha256 differs
    from ``expected_sha``. Returns ``{fid, file, begin_line, end_line, sha}`` for the new environment."""
    root = _root(root)
    _check_env_text(text)
    with _WRITE_LOCK:
        fig = _find_fig(fid, root)
        path = _tex_path(fig["file"], root)
        raw = path.read_bytes()
        if _sha(raw) != expected_sha:
            raise StaleError(f"{fig['file']} changed since it was read")
        lines = _split_lines(raw.decode("utf-8", errors="replace"))
        new = text.replace("\r\n", "\n").rstrip("\n").split("\n")
        b, e = fig["begin_line"], fig["end_line"]
        _write_lines(path, raw, lines[:b - 1] + new + lines[e:])
        new_raw = path.read_bytes()
    new_fid = fig["fid"]
    for f in scan_figures(root):
        if f["file"] == fig["file"] and f["begin_line"] == b:
            new_fid = f["fid"]
            break
    return {"fid": new_fid, "file": fig["file"], "begin_line": b, "end_line": b + len(new) - 1,
            "sha": _sha(new_raw)}


_ENV_IF_RE = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}|\\newif\s*\\if[A-Za-z@]*|\\iffalse(?![A-Za-z@])"
                        r"|\\fi(?![A-Za-z@])|\\if[A-Za-z@]*")


def _open_envs_and_iffalse(code_lines: list[str]) -> tuple[list[str], bool]:
    """(environments still open, whether inside an \\iffalse block) after ``code_lines`` (comment-stripped)."""
    stack: list[str] = []
    depth = 0  # > 0 while inside \iffalse ... \fi (nested \if.. / \fi counted)
    for c in code_lines:
        for m in _ENV_IF_RE.finditer(c):
            t = m.group(0)
            if m.group(1) == "begin":
                if not depth:
                    stack.append(m.group(2).strip())
            elif m.group(1) == "end":
                if not depth:
                    name = m.group(2).strip()
                    if name in stack:
                        del stack[len(stack) - 1 - stack[::-1].index(name):]
            elif t.startswith("\\newif"):
                continue
            elif t.startswith("\\iffalse"):
                depth += 1
            elif t.startswith("\\fi"):
                depth = max(0, depth - 1)
            elif depth:  # another \if.. nested inside \iffalse
                depth += 1
    return stack, depth > 0


def insert_figure(file_rel, after_line, graphics_rel, caption, label, env="figure", width="\\columnwidth",
                  root=None, placement="t", expected_sha=None) -> str:
    """Insert a new figure environment after line ``after_line`` (0 = top of file) of ``file_rel``; returns its fid.

    Everything is validated before the file is written (ValueError -> HTTP 400): the file must be part of the paper
    (MAIN_TEX or a file it \\input-s), the insertion point must not be in the preamble, inside another environment
    or inside an \\iffalse block, and caption/label/graphics must give a brace-balanced block. If the new figure
    cannot be found afterwards the file is restored byte for byte. ``expected_sha`` (optional) is checked like
    write_env's.
    """
    root = _root(root)
    path = _tex_path(file_rel, root)
    file_rel = _rel(path, root)
    if env not in FIG_ENVS:
        raise ValueError(f"env must be one of {FIG_ENVS}")
    reachable = _reachable_files(root)
    if file_rel not in reachable:
        raise ValueError(f"{file_rel} is not part of the paper (not \\input from {config.MAIN_TEX}); "
                         f"choose one of: {', '.join(reachable)}")
    caption = "" if caption is None else str(caption).replace("\r\n", "\n")
    label = "" if label is None else str(label).strip()
    graphics_rel = str(graphics_rel or "").strip()
    if not graphics_rel or re.search(r"[{}\\%\n]", graphics_rel):
        raise ValueError(f"bad graphics path {graphics_rel!r} (no braces, backslashes, % or newlines)")
    if label and re.search(r"[{}\\%#\s]", label):
        raise ValueError(f"bad label {label!r} (no braces, backslashes, %, # or spaces)")
    if re.search(r"\n\s*\n", caption):
        raise ValueError("the caption may not contain a blank line")
    if width and re.search(r"[\[\]\n]", str(width)):
        raise ValueError(f"bad width {width!r}")
    if placement and not re.fullmatch(r"[htbpH!]+", str(placement)):
        raise ValueError(f"bad placement {placement!r}")
    opt = f"[{placement}]" if placement else ""
    gopt = f"[width={width}]" if width else ""
    cap_lines = caption.split("\n")
    block = ["", f"\\begin{{{env}}}{opt}", "    \\centering", f"    \\includegraphics{gopt}{{{graphics_rel}}}",
             f"    \\caption{{{cap_lines[0]}"] + [f"    {l}" for l in cap_lines[1:]]
    block[-1] += "}"
    if label:
        block.append(f"    \\label{{{label}}}")
    block += [f"\\end{{{env}}}", ""]
    try:
        _check_env_text("\n".join(block))
    except ValueError as e:
        raise ValueError(f"caption/label would break the TeX: {e}") from None
    with _WRITE_LOCK:
        raw = path.read_bytes()
        if expected_sha is not None and _sha(raw) != expected_sha:
            raise StaleError(f"{file_rel} changed since it was read")
        lines = _split_lines(raw.decode("utf-8", errors="replace"))
        after_line = int(after_line)
        if not 0 <= after_line <= len(lines):
            raise ValueError(f"after_line must be in 0..{len(lines)}")
        for f in scan_figures(root):
            if f["file"] == file_rel and not f["commented"] and f["begin_line"] <= after_line < f["end_line"]:
                raise ValueError(f"line {after_line} is inside figure {f['fid']}")
        if file_rel == Path(config.MAIN_TEX).as_posix():
            begin_doc = next((i + 1 for i, l in enumerate(lines) if re.search(r"\\begin\s*\{document\}", _code(l))),
                             None)
            if begin_doc is None or after_line < begin_doc:
                raise ValueError("cannot insert a figure in the preamble")
        open_envs, in_iffalse = _open_envs_and_iffalse([_code(l) for l in lines[:after_line]])
        if in_iffalse:
            raise ValueError(f"line {after_line} is inside an \\iffalse block (that text is not in the paper)")
        inner = [e for e in open_envs if e != "document"]
        if inner:
            raise ValueError(f"line {after_line} is inside \\begin{{{inner[-1]}}}; insert between paragraphs")
        _write_lines(path, raw, lines[:after_line] + block + lines[after_line:])
        begin = after_line + 2
        try:
            fid = next((f["fid"] for f in scan_figures(root) if f["file"] == file_rel and f["begin_line"] == begin),
                       None)
        except Exception:
            fid = None
        if fid is None:
            tmp = path.with_name(path.name + ".designer-tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
            raise ValueError("the inserted figure could not be found again; the file was restored unchanged")
    return fid


# --------------------------------------------------------------------------------------------- context preview

_CTX_SKIP_RE = re.compile(r"\\(input|include|includeonly|bibliography|bibliographystyle|maketitle|"
                          r"IEEEpeerreviewmaketitle|twocolumn|onecolumn|newpage|clearpage|cleardoublepage|appendix)\b"
                          r"|\\(begin|end)\s*\{document\}")


def _ctx_name(fid: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", fid).strip("_")[:60]
    return f"ctx_{slug}_{hashlib.sha1(fid.encode()).hexdigest()[:8]}"


def _preamble(root: Path) -> tuple[list[str], int]:
    main = root / config.MAIN_TEX
    lines = _split_lines(_read_text(main))
    for i, l in enumerate(lines):
        if re.search(r"\\begin\s*\{document\}", _code(l)):
            return lines[:i], i
    raise ValueError(f"no \\begin{{document}} in {config.MAIN_TEX}")


def _fix_graphicspath(pre: list[str], root: Path, absolute: bool = True) -> tuple[list[str], bool]:
    """absolute=True (MiKTeX): every relative \\graphicspath entry becomes root/<entry>. absolute=False (TeX Live,
    where openin_any=p refuses absolute names): entries stay relative, minus a leading './', and are found through
    TEXINPUTS (kpathsea searches its path for relative names with a directory part, but not for ./names)."""
    rp = root.as_posix().rstrip("/") + "/"
    found = False

    def fix(m):
        nonlocal found
        found = True
        dirs = re.findall(r"\{([^{}]*)\}", m.group(1))
        if not absolute:
            out = []
            for d in dirs:
                rel = re.sub(r"^(\./)+", "", d)
                out.append("{" + rel + "}")
            return "\\graphicspath{" + "".join(out) + "}"
        out = ["{" + rp + "}"]
        for d in dirs:
            out.append("{" + (d if (os.path.isabs(d) or re.match(r"^[A-Za-z]:", d)) else rp + d.lstrip("./")) + "}")
        return "\\graphicspath{" + "".join(out) + "}"

    new = []
    for l in pre:
        c = _code(l)
        if "\\graphicspath" in c:
            new.append(_GRAPHICSPATH_RE.sub(fix, c))
        else:
            new.append(l)
    return new, found


def _snippet(lines: list[str], lo: int, hi: int, want_lo: int, want_hi: int, before: bool) -> tuple[int, int]:
    """Choose a balanced sub-range of lines[lo..hi] (0-based inclusive), extended to paragraph boundaries.
    before=True keeps the end fixed at hi and moves the start; else keeps lo and moves the end."""
    if hi < lo:
        return lo, lo - 1
    codes = ["" if _CTX_SKIP_RE.search(_code(l)) else _code(l) for l in lines]
    if before:
        s = max(lo, want_lo)
        steps = 0
        while s > lo and lines[s - 1].strip() and steps < 40:
            s -= 1
            steps += 1
        for st in range(s, hi + 1):
            if _balanced(codes[st:hi + 1]):
                return st, hi
        return hi + 1, hi
    e = min(hi, want_hi)
    steps = 0
    while e < hi and lines[e + 1].strip() and steps < 40:
        e += 1
        steps += 1
    for en in range(e, lo - 1, -1):
        if _balanced(codes[lo:en + 1]):
            return lo, en
    return lo, lo - 1


def compile_context(fid, text_override=None, lines_before=12, lines_after=12, root=None) -> dict:
    """Fast preview of one figure in its surrounding text with the paper's real preamble.

    Writes ``CACHE_DIR/context/<name>.tex`` (preamble of MAIN_TEX with ``\\graphicspath`` made absolute, input path
    = root) and runs one ``pdflatex`` pass there (no BibTeX). The paper's last ``.aux`` is reused so citations,
    references and the figure number match the paper; anything missing prints as ??. Errors inside the context file
    are mapped back to the original ``{file, line}``. Returns ``{ok, pdf, seconds, errors, name, warnings_count,
    log_tail, fig_box}`` where ``fig_box`` locates the figure's image in the context PDF (same shape as locate()).
    """
    root = _root(root)
    t0 = time.time()
    fig = _find_fig(fid, root)
    pre, begin_doc = _preamble(root)
    # MiKTeX: absolute paths into the paper (-include-directory + absolute \input@path / \graphicspath). TeX Live:
    # openin_any=p (config.latex_env) refuses absolute names, so everything stays relative and the paper root is
    # on TEXINPUTS instead.
    absolute = _is_miktex()
    pre, has_gp = _fix_graphicspath(pre, root, absolute=absolute)
    rp = root.as_posix().rstrip("/") + "/"

    src_lines = _split_lines(_read_text(root / fig["file"]))
    b0, e0 = fig["begin_line"] - 1, fig["end_line"] - 1
    lo = begin_doc + 1 if fig["file"] == Path(config.MAIN_TEX).as_posix() else 0
    hi = len(src_lines) - 1
    if fig["file"] == Path(config.MAIN_TEX).as_posix():
        end_doc = next((i for i in range(len(src_lines) - 1, -1, -1)
                        if re.search(r"\\end\s*\{document\}", _code(src_lines[i]))), hi + 1)
        hi = end_doc - 1
    bs, be = _snippet(src_lines, lo, b0 - 1, b0 - lines_before, b0 - 1, before=True)
    as_, ae = _snippet(src_lines, e0 + 1, hi, e0 + 1, e0 + lines_after, before=False)

    out_lines: list[str] = []
    origin: list[tuple[str, int] | None] = []

    def add(line, org=None):
        out_lines.append(line)
        origin.append(org)

    main_rel = Path(config.MAIN_TEX).as_posix()
    for i, l in enumerate(pre):
        add(l, (main_rel, i + 1))
    if absolute:
        add("\\makeatletter")
        add("\\providecommand\\input@path{}")
        add("\\edef\\input@path{{" + rp + "}\\input@path}")
        add("\\makeatother")
        if not has_gp and " " not in rp:
            add("\\graphicspath{{" + rp + "}}")
    add("\\begin{document}")
    for i in range(bs, be + 1):
        l = src_lines[i]
        add("" if _CTX_SKIP_RE.search(_code(l)) else l, (fig["file"], i + 1))

    env_text = text_override if text_override is not None else "\n".join(src_lines[b0:e0 + 1])
    env_lines = env_text.replace("\r\n", "\n").split("\n")
    # figure number from the paper's aux
    aux_src = root / f"{_main_stem()}.aux"
    mlab = re.search(r"\\label\s*\{([^{}]*)\}", "\n".join(_code(l) for l in env_lines))
    if mlab and aux_src.exists():
        ma = re.search(r"\\newlabel\{" + re.escape(mlab.group(1).strip()) + r"\}\{\{(\d+)\}", _read_text(aux_src))
        if ma:
            add(f"\\setcounter{{figure}}{{{int(ma.group(1)) - 1}}}")
    if fig.get("in_twocolumn"):
        add("\\twocolumn[{%")
    env_start = len(out_lines) + 1
    for i, l in enumerate(env_lines):
        add(l, (fig["file"], fig["begin_line"] + i))
    env_end = len(out_lines)
    if fig.get("in_twocolumn"):
        add("}]")
    for i in range(as_, ae + 1):
        l = src_lines[i]
        add("" if _CTX_SKIP_RE.search(_code(l)) else l, (fig["file"], i + 1))
    add("\\end{document}")

    name = _ctx_name(fid)
    cdir = Path(config.CACHE_DIR) / "context"
    cdir.mkdir(parents=True, exist_ok=True)
    tex = cdir / f"{name}.tex"
    pdf = cdir / f"{name}.pdf"
    log = cdir / f"{name}.log"

    # graphics lines inside the context file (for fig_box)
    gl = []
    gpaths = _graphicspath(root)
    info = _env_info(env_lines, 0, len(env_lines) - 1, False)
    for g in info["graphics"]:
        res = _resolve_graphic(g["path"], root, gpaths)
        gl.append({"line": env_start + g["line"] - 1, "path": g["path"], "abs": (root / res) if res else None})

    def run(use_aux: bool):
        tex.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        aux = cdir / f"{name}.aux"
        if use_aux and aux_src.exists():
            shutil.copyfile(aux_src, aux)
        elif aux.exists():
            aux.unlink()
        before = pdf.stat().st_mtime if pdf.exists() else None
        t = time.time()
        cmd = [_exe("pdflatex"), _no_shell_escape(), "-interaction=nonstopmode", "-file-line-error", "-synctex=1",
               "-max-print-line=10000"]
        env_extra = None
        if _is_miktex():
            cmd.append(f"-include-directory={root}")
        else:   # TeX Live: the paper root on the input path (trailing separator = then the default search path)
            env_extra = {"TEXINPUTS": str(root) + os.pathsep + os.environ.get("TEXINPUTS", "")}
            if not env_extra["TEXINPUTS"].endswith(os.pathsep):
                env_extra["TEXINPUTS"] += os.pathsep
        cmd.append(tex.name)
        code, out, timed_out = _run(cmd, cdir, CONTEXT_TIMEOUT, env_extra)
        fresh = pdf.exists() and (before is None or pdf.stat().st_mtime > before or pdf.stat().st_mtime >= t - 1)
        log_text = _read_text(log) if log.exists() else out
        errs, nw = parse_log(log_text, root)
        return fresh, timed_out, errs, nw, log_text

    # at most one context compile running and one waiting: a burst of requests (or a TeX \loop that runs until the
    # timeout) cannot tie up every server thread
    if not _CONTEXT_WAITERS.acquire(blocking=False):
        raise Busy("a context preview is already running and another is waiting; try again in a moment")
    try:
        with _CONTEXT_LOCK:
            fresh, timed_out, errs, nw, log_text = run(True)
            if any(e["file"] and e["file"].endswith(".aux") for e in errs):
                fresh, timed_out, errs, nw, log_text = run(False)
    finally:
        _CONTEXT_WAITERS.release()

    tex_rel = _rel(tex, root)
    mapped = []
    for e in errs:
        f = e["file"]
        if f and (f == tex.name or f == tex_rel or Path(f).name == tex.name) and e["line"]:
            org = origin[e["line"] - 1] if 0 < e["line"] <= len(origin) else None
            if org:
                e = {**e, "file": org[0], "line": org[1], "context_line": e["line"]}
            else:
                e = {**e, "file": tex.name, "context_line": e["line"]}
        mapped.append(e)
    if timed_out:
        mapped.insert(0, {"file": None, "line": None, "message": "context compile timed out"})

    fig_box = None
    if fresh:
        try:
            fig_box = _locate_in(pdf, tex, gl, env_start, env_end, cdir)
        except Exception:
            fig_box = None
    return {"ok": bool(fresh and not timed_out), "pdf": str(pdf), "name": name,
            "seconds": round(time.time() - t0, 2), "errors": mapped, "warnings_count": nw,
            "log_tail": _tail(log_text), "fig_box": fig_box}


# --------------------------------------------------------------------------------------------- section editor
# The UI edits whole .tex files ("sections"): MAIN_TEX and every file it pulls in via \input/\include. Only those
# files can be read or written here (no other path in the repo, nothing outside the root, no symlink that leads out).
# Writes are compare-and-swap on the sha256 of the file's bytes: a file changed on disk since it was read (Overleaf
# pull, another editor) is never overwritten silently (StaleError -> HTTP 409 with the current text).

_HEADING_RE = re.compile(r"\\(part|chapter|section|subsection|title)\*?\s*(?:\[[^\]]*\])?\s*\{")


def _heading(lines: list[str]) -> str | None:
    """First \\section heading of a file (else \\chapter / \\part / \\subsection / \\title), as plain-ish text."""
    best: tuple[int, str] | None = None
    rank = {"part": 0, "chapter": 0, "section": 0, "title": 2, "subsection": 1}
    for ln in lines:
        c = _code(ln)
        for m in _HEADING_RE.finditer(c):
            b = _braced(c, m.end() - 1)
            text = (b[0] if b else c[m.end():]).strip()
            text = re.sub(r"\\([&%_#$])", r"\1", text)                  # \& -> &
            text = re.sub(r"\\[-/]|\\[,;:! ]", lambda m: "" if m.group(0) in ("\\-", "\\/") else " ", text)
            text = re.sub(r"\\[A-Za-z@]+\*?\s*", "", text).replace("{", "").replace("}", "").replace("~", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            r = rank[m.group(1)]
            if r == 0:
                return text
            if best is None or r < best[0]:
                best = (r, text)
    return best[1] if best else None


def _decode_tex(raw: bytes) -> tuple[str, bool, bool]:
    """(text with \\n newlines, valid utf-8, had a BOM)."""
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    try:
        text, ok = body.decode("utf-8"), True
    except UnicodeDecodeError:
        text, ok = body.decode("utf-8", errors="replace"), False
    return text.replace("\r\n", "\n"), ok, bom


def _editable_tex(path, root: Path) -> tuple[Path, str]:
    """Validate a section path from a client: (absolute path, canonical posix rel path).

    Allowed: a relative path ending in .tex that is MAIN_TEX or reachable from it via \\input/\\include, and whose
    real location (symlinks resolved) is inside the paper root. PermissionError (403) otherwise, ValueError (400)
    for malformed input, FileNotFoundError (404) if it vanished."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required (a .tex file relative to the paper root)")
    p = path.strip().replace("\\", "/")
    if "\x00" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise PermissionError(f"{path!r}: absolute paths are not allowed")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        raise PermissionError(f"{path!r}: path traversal is not allowed")
    rel = "/".join(parts)
    if not rel.lower().endswith(".tex"):
        raise PermissionError(f"{path!r}: only .tex files can be edited")
    allowed = {_norm(root / r): r for r in _reachable_files(root)}
    key = _norm(root / rel)
    if key not in allowed:
        raise PermissionError(f"{rel} is not part of the paper (not reachable from {config.MAIN_TEX} via "
                              "\\input/\\include)")
    rel = allowed[key]
    target = root / rel
    try:
        real = target.resolve(strict=True)
    except OSError:
        raise FileNotFoundError(f"{rel} does not exist")
    try:
        real.relative_to(root.resolve())
    except ValueError:
        raise PermissionError(f"{rel} resolves outside the paper root")
    if not real.is_file():
        raise FileNotFoundError(f"{rel} is not a file")
    return target, rel


def _count_lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


_SECTION_CACHE: dict[str, tuple] = {}


def _section_info(p: Path) -> dict:
    """{heading, lines, bytes, mtime, hash} of one section file, cached on (mtime_ns, size): tex_sections is polled
    every 30 s and on every window focus, and re-hashing a large file each time is wasted work."""
    st = p.stat()
    key = _norm(p)
    hit = _SECTION_CACHE.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return dict(hit[1])
    raw = p.read_bytes()
    text, _, _ = _decode_tex(raw)
    lines = text.split("\n")
    heading = _heading(lines)
    if heading is None and any(re.search(r"\\begin\s*\{abstract\}", _code(x)) for x in lines):
        heading = "Abstract"
    info = {"heading": heading, "lines": _count_lines(text), "bytes": len(raw), "mtime": st.st_mtime,
            "hash": _sha(raw)}
    _SECTION_CACHE[key] = ((st.st_mtime_ns, st.st_size), info)
    return dict(info)


def tex_sections(root=None) -> list[dict]:
    """The editable .tex files in document order: MAIN_TEX first, then every \\input/\\include (depth-first, as TeX
    reads them). ``[{path, title, heading, lines, bytes, mtime, hash, main, depth}]``; ``title`` is the first
    \\section heading (or the file stem), ``hash`` the sha256 of the file's bytes (what PUT expects as base_hash)."""
    root = _root(root)
    main = Path(config.MAIN_TEX).as_posix()
    depth: dict[str, int] = {main: 0}
    out = []
    for rel in _reachable_files(root):
        p = root / rel
        try:
            info = _section_info(p)
        except OSError:
            continue
        is_main = _norm(p) == _norm(root / main)
        for _, r in _inputs_of(rel, root):
            depth.setdefault(r, depth.get(rel, 0) + 1)
        out.append({"path": rel, "title": info["heading"] or Path(rel).stem, "main": is_main,
                    "depth": 0 if is_main else depth.get(rel, 1), **info})
    return out


def read_tex_file(path, root=None) -> dict:
    """``{path, text, hash, mtime, lines, newline, bom, editable, warning?}`` of one section file. ``text`` uses \\n
    line ends (the file's own CRLF/LF style and BOM are restored on write); ``hash`` = sha256 of the bytes on disk."""
    root = _root(root)
    p, rel = _editable_tex(path, root)
    raw = p.read_bytes()
    text, ok, bom = _decode_tex(raw)
    out = {"path": rel, "text": text, "hash": _sha(raw), "mtime": p.stat().st_mtime, "lines": _count_lines(text),
           "newline": "\r\n" if b"\r\n" in raw else "\n", "bom": bom, "editable": ok}
    if not ok:
        out["warning"] = f"{rel} is not valid UTF-8; it is shown read-only so saving cannot corrupt it"
    return out


_EOL_RE = re.compile(r"(\r\n|\r|\n)")


def _split_eol(text: str) -> list[tuple[str, str]]:
    """[(line content, its terminator: '\r\n' | '\r' | '\n' | '')]; the last line may have no terminator."""
    parts = _EOL_RE.split(text)
    out = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
    if parts[-1]:
        out.append((parts[-1], ""))
    return out


def _restore_line_ends(old_text: str, body: str) -> str:
    """``body`` (\n line ends, from the editor) with each line's original terminator where the line is kept or
    edited in place, and the file's majority style (CRLF vs LF) for new lines. So an unchanged save is byte-identical
    even for a mixed-ending file or one with lone CRs, and a one-line edit stays a one-line diff."""
    old = _split_eol(old_text)
    new_c = body.split("\n")
    last_has_nl = new_c[-1] == ""
    if last_has_nl:
        new_c.pop()
    crlf = sum(1 for _, t in old if t == "\r\n")
    lf = sum(1 for _, t in old if t == "\n")
    maj = "\r\n" if crlf > lf else "\n"
    old_c = [c for c, _ in old]
    terms: list[str | None] = [None] * len(new_c)       # None = the majority style
    # common prefix / suffix first (an edit is usually local), then difflib on the middle when it is small enough
    a = 0
    while a < len(old_c) and a < len(new_c) and old_c[a] == new_c[a]:
        terms[a] = old[a][1]
        a += 1
    b = 0
    while b < len(old_c) - a and b < len(new_c) - a and old_c[-1 - b] == new_c[-1 - b]:
        terms[len(new_c) - 1 - b] = old[len(old_c) - 1 - b][1]
        b += 1
    mid_old, mid_new = old_c[a:len(old_c) - b], new_c[a:len(new_c) - b]
    if mid_old and mid_new and len(mid_old) * len(mid_new) <= 4_000_000:
        import difflib
        sm = difflib.SequenceMatcher(None, mid_old, mid_new, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("equal", "replace"):           # kept or edited in place: that line keeps its own terminator
                for k in range(min(i2 - i1, j2 - j1)):
                    terms[a + j1 + k] = old[a + i1 + k][1]
    out = []
    for j, c in enumerate(new_c):
        if j == len(new_c) - 1 and not last_has_nl:
            out.append(c)
            break
        out.append(c + (terms[j] or maj))            # '' (the old last line had none) -> the majority style
    return "".join(out)


def write_tex_file(path, text, base_hash, root=None) -> dict:
    """Replace a section file with ``text`` if its bytes still hash to ``base_hash`` (else StaleError).

    Keeps the file's line ends line by line (CRLF stays CRLF; a mixed or lone-CR file is not normalised, see
    _restore_line_ends) and its BOM; written atomically (temp file + os.replace).
    Returns ``{path, hash, mtime, lines, changed}``."""
    from .locks import atomic_write_text
    root = _root(root)
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not isinstance(base_hash, str) or not base_hash:
        raise ValueError("base_hash is required (the hash from GET /api/tex/file): it guards against overwriting "
                         "changes made on disk since the file was loaded")
    with _WRITE_LOCK:
        p, rel = _editable_tex(path, root)
        raw = p.read_bytes()
        if _sha(raw) != base_hash:
            raise StaleError(f"{rel} changed on disk since it was loaded")
        _, ok, bom = _decode_tex(raw)
        if not ok:
            raise ValueError(f"{rel} is not valid UTF-8; refusing to rewrite it")
        old_text = (raw[3:] if bom else raw).decode("utf-8")
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = _count_lines(body)
        new = ("\ufeff" if bom else "") + _restore_line_ends(old_text, body)
        changed = new.encode("utf-8") != raw
        if changed:
            atomic_write_text(p, new)
        new_raw = p.read_bytes()
    return {"path": rel, "hash": _sha(new_raw), "mtime": p.stat().st_mtime, "lines": lines, "changed": changed}


_ANYREC_RE = re.compile(r"^[\[\(hvxkg$](\d+),(\d+)")
_LINES_CACHE: dict[str, tuple] = {}


def _synctex_lines(path: Path, tags: set[int]) -> list[int]:
    """Sorted source lines (of the given input tags) that have any synctex record (boxes, glyphs, kerns, math)."""
    if not tags:
        return []
    st = path.stat()
    key = _norm(path)
    hit = _LINES_CACHE.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        by_tag = hit[1]
    else:
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        by_tag: dict[int, set[int]] = {}
        for ln in raw.decode("utf-8", errors="replace").splitlines():
            m = _ANYREC_RE.match(ln)
            if m:
                by_tag.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
        _LINES_CACHE[key] = ((st.st_mtime_ns, st.st_size), by_tag)
    out: set[int] = set()
    for t in tags:
        out |= by_tag.get(t, set())
    return sorted(x for x in out if x > 0)


def forward(file, line, root=None) -> dict | None:
    """Source -> page (synctex forward search): ``{page, x, y, w, h, page_w, page_h, line, file, stale}`` in PDF
    points (origin top-left) for ``file:line`` in the last compiled PDF, or None when nothing is found.

    ``line`` is tried first; a line without output (blank, comment, preamble) falls back to the nearest line that
    has some (a tie goes to the line below; up to 40 lines away; None if there is none); ``line`` in the result is
    the one used. A line past the end of the file is taken as its last line. The box is the union of the .synctex
    hbox records of that exact line (else of what ``synctex view`` reports), clipped to the page.
    ``stale`` = the file changed since that compile, so line numbers may have moved."""
    root = _root(root)
    p, rel = _editable_tex(file, root)
    try:
        line = int(line)
    except (TypeError, ValueError):
        raise ValueError("line must be an integer")
    if line < 1:
        raise ValueError("line must be >= 1")
    try:
        n_lines = max(1, _count_lines(_decode_tex(p.read_bytes())[0]))
    except OSError:
        n_lines = 1
    line = min(line, n_lines)          # a cursor past the end (unsaved lines, a bogus client value) -> last line
    pdf = root / f"{_main_stem()}.pdf"
    syn = _synctex_path(pdf)
    if not pdf.exists() or syn is None:
        return None
    data = _load_synctex(syn)
    tags = _tags_for(data, p, root)
    # lines of this file that produced output, from the .synctex itself (cheap, cached): pick the nearest one, so
    # the synctex binary (~0.4 s per call on Windows) runs once or twice instead of probing line by line
    have = _synctex_lines(syn, tags)
    if have:
        after = [x for x in have if line <= x <= line + 40]
        before = [x for x in have if line - 40 <= x < line]
        # a line with no record at all (preamble, comment, a macro argument TeX attributes elsewhere) is not asked
        # directly: synctex view then answers the boxes of whatever it considers nearest, often a whole page block
        near = ([min(after)] if after else []) + ([max(before)] if before else [])
        near.sort(key=lambda x: (abs(x - line), x < line))       # nearest first; a tie goes to the line below
        order = ([line] if line in have else []) + near
        if not order:
            return None
    else:
        order = [line]
    order = list(dict.fromkeys(order))     # synctex view itself may find the exact line when no record names it
    hit = None
    for ln in order:
        boxes = _synctex_view(pdf, p, ln)
        if boxes:
            hit = (ln, boxes)
            break
    if hit is None:
        return None
    ln, boxes = hit
    page = boxes[0]["page"]
    # Prefer the .synctex's own hbox records for exactly this file:line on that page. `synctex view` can answer
    # dozens of boxes for one line (every line of a \twocolumn[{...}] block in the main file, say), whose union is
    # meaningless; the exact records are the typeset lines built from this source line.
    exact = [_box(r) for r in data["records"]
             if r[0] == page and r[1] == "(" and r[2] in tags and r[3] == ln and r[6] > 0.5 and r[7] + r[8] > 0.5]
    same = exact or [b for b in boxes if b["page"] == page and b["w"] > 0] or [boxes[0]]
    # synctex answers the boxes of every typeset line built from this source line: report their union
    x0 = min(b["x"] for b in same)
    y0 = min(b["y"] for b in same)
    x1 = max(b["x"] + max(b["w"], 1) for b in same)
    y1 = max(b["y"] + max(b["h"], 1) for b in same)
    psize = _page_size(pdf)
    if psize:                          # never a box that runs off the page
        x0, x1 = max(0.0, min(x0, psize[0] - 1)), min(psize[0], max(x1, 1.0))
        y0, y1 = max(0.0, min(y0, psize[1] - 1)), min(psize[1], max(y1, 1.0))
        x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    doc = _load_snapshot(root, pdf)
    return {"page": page, "x": round(x0, 2), "y": round(y0, 2), "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
            "page_w": psize[0] if psize else None, "page_h": psize[1] if psize else None, "line": ln,
            "file": rel, "stale": _is_stale(rel, root, doc, pdf.stat().st_mtime)}
