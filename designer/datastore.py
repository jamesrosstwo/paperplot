"""Data store: datasets registered *by reference* (contract section 4).

The registry (``config.REGISTRY_PATH``) is a JSON list of entries::

    {name, path, kind, link, added, notes, size_bytes, mtime, ref, missing, size_partial}

``path`` is the absolute, resolved location of the data on this machine. Nothing is ever copied: ``add`` optionally
creates a link inside ``config.LINKS_DIR`` -- on Windows a directory junction (``_winapi.CreateJunction``, no admin, no
shell) or a file symlink (``os.symlink``, needs Developer Mode; without it the entry is kept as a plain reference); on
POSIX an ``os.symlink`` for both. ``.lnk`` shortcuts are resolved only on Windows (elsewhere a ``.lnk`` is an ordinary
file). Registry read-modify-writes hold a cross-process file lock (``designer.locks``) and replace the file atomically. ``remove`` deletes only
the registry entry and that link -- never the target -- and refuses to touch a link path outside ``LINKS_DIR``.

Allowed places (``check_allowed``, on add and again on every read): never credentials / system dirs / the state dir /
the paper's .git; and when the server is a network service (token or non-loopback host) only inside
``DESIGNER_DATA_ROOTS`` (``config.data_roots_policy``), with refs taken literally (no ~ / $VAR expansion).

Errors: ``DatasetNotFound`` (a ``KeyError``) for unknown names, ``DataStoreError`` (a ``ValueError``) for everything
else the caller did wrong (bad name, missing target, unsupported kind, refused removal).
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from designer import config
from designer.locks import atomic_write_text, file_lock

__all__ = [
    "DataStoreError", "DatasetNotFound", "add", "list_datasets", "get", "remove", "refresh", "preview", "load_table",
    "resolve_ref", "check_allowed",
]

_LOCK = threading.RLock()
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")
_KINDS = {".tsv": "tsv", ".tab": "tsv", ".csv": "csv", ".json": "json", ".jsonl": "json", ".ndjson": "json",
          ".yaml": "yaml", ".yml": "yaml", ".npz": "npz"}
_TEXT_EXT = {".txt", ".md", ".log", ".tex", ".bib", ".py", ".sh", ".cfg", ".ini", ".toml", ".rst", ".xml", ".html",
             ".css", ".js", ".sty", ".cls"}
_DIR_SIZE_MAX_ENTRIES = 50_000
_DIR_SIZE_MAX_SECONDS = 3.0
_PS_TIMEOUT = 30


class DataStoreError(ValueError):
    """The request cannot be honoured (bad name, missing target, refused removal, unsupported kind, ...)."""


class DatasetNotFound(KeyError):
    """No dataset with that name in the registry."""

    def __str__(self):  # KeyError's default repr-quotes the message
        return str(self.args[0]) if self.args else "dataset not found"


# --------------------------------------------------------------------------------------------------- registry I/O
def _registry_path() -> Path:
    return Path(config.REGISTRY_PATH)


def _links_dir() -> Path:
    d = Path(config.LINKS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read() -> list[dict]:
    p = _registry_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as e:
        raise DataStoreError(f"registry {p} is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise DataStoreError(f"registry {p} must hold a JSON list")
    return data


def _write(entries: list[dict]) -> None:
    atomic_write_text(_registry_path(), json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def _registry_lock():
    """Cross-process lock for a registry read-modify-write (another designer process / container on the same state)."""
    return file_lock(_registry_path())


def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("DESIGNER_IN_CONTAINER", "").strip() in ("1", "true", "yes")


def _missing_msg(p) -> str:
    """'<p> does not exist on this machine', or inside a container: which directory to mount."""
    if not _in_container():
        return f"{p} does not exist on this machine"
    parent = os.path.dirname(str(p).rstrip("/")) or str(p)
    return (f"{p} does not exist inside the container: mount {parent} into the container at the same absolute path "
            f"(read-only, e.g. -v {parent}:{parent}:ro) and restart it")


def _isjunction(p) -> bool:
    """os.path.isjunction exists from Python 3.12; junctions exist only on Windows."""
    fn = getattr(os.path, "isjunction", None)
    return bool(fn(p)) if fn else False


def _find(entries: list[dict], name: str) -> dict:
    for e in entries:
        if e.get("name") == name:
            return e
    raise DatasetNotFound(f"no dataset named {name!r}")


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------------------- ref resolution
def _resolve_lnk(p: Path) -> str:
    """Target of a Windows .lnk shortcut via WScript.Shell (path passed through the environment, never interpolated)."""
    env = dict(os.environ)
    env["DESIGNER_LNK_PATH"] = str(p)
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
           "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
           "(New-Object -ComObject WScript.Shell).CreateShortcut($env:DESIGNER_LNK_PATH).TargetPath"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_PS_TIMEOUT, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DataStoreError(f"could not resolve shortcut {p}: {e}") from e
    out = r.stdout.decode("utf-8", "replace").strip().lstrip("\ufeff")
    if r.returncode != 0 or not out:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise DataStoreError(f"shortcut {p} has no file-system target" + (f" ({err})" if err else ""))
    return out


def resolve_ref(ref: str) -> Path:
    """Absolute, resolved path for ``ref``: a path, ``file:///`` URL, ``.lnk`` shortcut, or symlink/junction.

    Raises DataStoreError if it cannot be resolved or does not exist on this machine.
    """
    if ref is None or not str(ref).strip():
        raise DataStoreError("empty reference")
    s = str(ref).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":  # pasted "C:\...\x" with quotes
        s = s[1:-1].strip()
    if s.lower().startswith("file:"):
        u = urllib.parse.urlparse(s)
        if u.netloc and u.netloc.lower() not in ("", "localhost"):
            if os.name != "nt":
                raise DataStoreError(f"{ref!r} names a remote host; use file:///<absolute path> on this machine")
            s = urllib.request.url2pathname(f"//{u.netloc}{u.path}")  # UNC: file://server/share/x
        else:
            # Windows: '/C:/Users/x%20y' -> 'C:\\Users\\x y'; POSIX: '/home/x%20y' -> '/home/x y'
            s = urllib.request.url2pathname(u.path)
    if not os.path.lexists(s) and config.data_roots_policy() is None:
        # local tool only: '~' and %VAR% / $VAR expansion. A literal name like 'x%PATH%y' wins over expansion. A
        # network service (token / data roots) takes paths literally.
        s = os.path.expandvars(os.path.expanduser(s))
    p = Path(s)
    if not p.is_absolute():
        raise DataStoreError(f"reference must be an absolute path or file:/// URL, got {ref!r}")
    if os.name == "nt" and p.suffix.lower() == ".lnk" and p.is_file() and not p.is_symlink():
        p = Path(_resolve_lnk(p))
    if not os.path.lexists(p):
        raise DataStoreError(_missing_msg(p))
    real = Path(os.path.realpath(p))  # follows symlinks and junctions (also chains of them)
    if not real.exists():
        raise DataStoreError(f"{p} is a dangling link (target {real} does not exist)")
    check_allowed(real)
    return real


# --------------------------------------------------------------------------------------------------- allowed places
def _denied_paths() -> list[Path]:
    """Never data, whatever DESIGNER_DATA_ROOTS says: credentials, system dirs, the designer's own state, .git."""
    home = Path(os.path.expanduser("~"))
    out = [home / n for n in (".ssh", ".gnupg", ".git-credentials", ".netrc", ".aws", ".docker", ".kube")]
    out += [home / ".config" / n for n in ("git", "gh", "gcloud")]
    cred = os.environ.get("DESIGNER_GIT_CREDENTIALS_FILE")
    if cred:
        out.append(Path(cred))
    if os.name != "nt":
        out += [Path(d) for d in ("/proc", "/sys", "/dev", "/etc", "/run", "/boot", "/root", "/var/run")]
    out += [Path(config.STATE_DIR), Path(config.PAPER_ROOT) / ".git"]
    return out


def _under(child: Path, parent: Path) -> bool:
    """child == parent or inside it (both made real: symlinks resolved)."""
    try:
        c = os.path.normcase(os.path.realpath(child))
        p = os.path.normcase(os.path.realpath(parent))
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives on Windows
        return False


def check_allowed(real: Path) -> None:
    """Raise DataStoreError unless ``real`` (already resolved) may be used as data: never in a denied place, and
    inside DESIGNER_DATA_ROOTS when the server is a network service (config.data_roots_policy)."""
    for d in _denied_paths():
        if _under(real, d):
            raise DataStoreError(f"{real} is inside {d}, which can never be registered as data")
    roots = config.data_roots_policy()
    if roots is None:
        return
    if not roots:
        raise DataStoreError("no data directories are configured for this server: set DESIGNER_DATA_ROOTS to the "
                             "data directories (separated by os.pathsep) and restart it (the Docker scripts set it "
                             "to the mounted data dirs)")
    if not any(_under(real, r) for r in roots):
        raise DataStoreError(f"{real} is outside the data directories this server allows "
                             f"({os.pathsep.join(str(r) for r in roots)}; DESIGNER_DATA_ROOTS)"
                             + (": mount it into the container and add it to DESIGNER_DATA_ROOTS"
                                if _in_container() else ""))


def _kind(p: Path) -> str:
    if p.is_dir():
        return "dir"
    return _KINDS.get(p.suffix.lower(), "other")


# --------------------------------------------------------------------------------------------------- sizes
def _is_link_entry(de: os.DirEntry) -> bool:
    try:
        if de.is_symlink():
            return True
        return bool(getattr(de, "is_junction", lambda: False)())
    except OSError:
        return True


def _dir_size(root: Path) -> tuple[int, bool]:
    """Bounded recursive size (does not follow links). Returns (bytes, partial)."""
    total, seen, t0 = 0, 0, time.monotonic()
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for de in it:
                seen += 1
                if seen > _DIR_SIZE_MAX_ENTRIES or time.monotonic() - t0 > _DIR_SIZE_MAX_SECONDS:
                    return total, True
                if _is_link_entry(de):
                    continue
                try:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                    else:
                        total += de.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return total, False


def _stat_fields(p: Path) -> dict:
    if not p.exists():
        return {"missing": True}
    st = p.stat()
    if p.is_dir():
        size, partial = _dir_size(p)
    else:
        size, partial = st.st_size, False
    return {"size_bytes": size, "size_partial": partial, "mtime": _iso(st.st_mtime), "missing": False}


# --------------------------------------------------------------------------------------------------- links
def _is_link(p: Path) -> bool:
    return os.path.islink(p) or _isjunction(p)


def _inside(child: Path, parent: Path) -> bool:
    """Lexical containment (does NOT follow ``child`` if it is a link -- following it would land on the target)."""
    c = os.path.normcase(os.path.abspath(child))
    par = os.path.normcase(os.path.abspath(parent))
    return os.path.commonpath([c, par]) == par and c != par


def _unlink_link(link: Path) -> None:
    """Remove a junction / symlink itself. Never recurses, never touches a real file or directory."""
    if not os.path.lexists(link):
        return
    if not _is_link(link):
        raise DataStoreError(f"{link} is a real file or directory, not a link; refusing to delete it")
    if _isjunction(link):
        os.rmdir(link)  # removes the reparse point only; the target directory is untouched
    elif os.name == "nt" and os.path.isdir(link):  # Windows directory symlink (POSIX: a symlink is unlinked)
        os.rmdir(link)
    else:
        os.unlink(link)


def _make_junction(link: Path, target: Path) -> str | None:
    """Create a directory junction ``link`` -> ``target``. Returns None on success, else an error message.

    Uses the stdlib ``_winapi.CreateJunction`` (no admin, no shell): going through ``cmd /c mklink /J`` would let cmd
    interpret ``& | ^ %`` in a folder name as commands. On POSIX a directory symlink is made instead.
    """
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except OSError as e:
        return f"junction failed: {e}"
    if not _is_link(link):
        return "junction failed: no link was created"
    if os.path.normcase(os.path.realpath(link)) != os.path.normcase(os.path.realpath(target)):
        _unlink_link(link)
        return "the junction pointed somewhere unexpected; removed it"
    return None


def _link_path(name: str, target: Path) -> Path:
    d = _links_dir()
    if target.is_dir():
        return d / name
    suf = target.suffix
    return d / (name if suf and name.lower().endswith(suf.lower()) else name + suf)


def _make_link(name: str, target: Path, registered_links: set[str]) -> tuple[str | None, str | None]:
    """Create LINKS_DIR/<name> -> target. Returns (link_path or None, warning or None). Never copies."""
    link = _link_path(name, target)
    if os.path.lexists(link):
        if _is_link(link) and os.path.normcase(str(link)) not in registered_links:
            _unlink_link(link)  # stale link left behind by an earlier registry; the link only
        else:
            return None, f"{link} already exists; registered by reference only (no link made)"
    if target.is_dir():
        err = _make_junction(link, target)
        if err:
            return None, f"could not create a junction ({err}); registered by reference only"
        return str(link), None
    try:
        os.symlink(str(target), str(link))
    except OSError as e:
        why = ("Windows refused to create a file symlink -- enable Developer Mode to allow it"
               if getattr(e, "winerror", None) == 1314 else f"os.symlink failed: {e}")
        return None, f"{why}; registered by reference only (no copy, no hard link)"
    return str(link), None


# --------------------------------------------------------------------------------------------------- public API
def add(name: str, ref: str, notes: str = "", make_link: bool = True) -> dict:
    """Register ``ref`` under ``name`` by reference. Returns the entry (plus ``warning`` if a link could not be made)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DataStoreError("name must be 1-80 chars of letters, digits, '_', '-', '.', starting with a letter/digit")
    target = resolve_ref(ref)
    links_dir = _links_dir()
    if _inside(target, links_dir) or os.path.normcase(str(target)) == os.path.normcase(str(links_dir.resolve())):
        raise DataStoreError("the reference resolves into the designer's own links folder")
    with _LOCK, _registry_lock():
        entries = _read()
        if any(e.get("name") == name for e in entries):
            raise DataStoreError(f"a dataset named {name!r} already exists")
        warning = None
        link = None
        if make_link:
            registered = {os.path.normcase(e["link"]) for e in entries if e.get("link")}
            link, warning = _make_link(name, target, registered)
        entry = {
            "name": name,
            "path": str(target),
            "kind": _kind(target),
            "link": link,
            "added": _now_iso(),
            "notes": notes or "",
            "ref": str(ref),
        }
        entry.update(_stat_fields(target))
        entries.append(entry)
        _write(entries)
    out = dict(entry)
    if warning:
        out["warning"] = warning
    return out


def list_datasets() -> list[dict]:
    """All entries; ``missing`` is re-checked cheaply (existence only)."""
    with _LOCK:
        entries = _read()
    for e in entries:
        e["missing"] = not os.path.exists(e.get("path", ""))
        if e.get("link"):
            e["link_ok"] = os.path.lexists(e["link"]) and _is_link(Path(e["link"]))
    return entries


def get(name: str) -> dict:
    with _LOCK:
        e = _find(_read(), name)
    e["missing"] = not os.path.exists(e.get("path", ""))
    return e


def remove(name: str) -> dict:
    """Drop the registry entry and its link. Never deletes the target. Refuses a link path outside LINKS_DIR."""
    with _LOCK, _registry_lock():
        entries = _read()
        e = _find(entries, name)
        link = e.get("link")
        removed_link = None
        if link:
            lp = Path(link)
            if not _inside(lp, Path(config.LINKS_DIR)):
                raise DataStoreError(f"link {link} is outside {config.LINKS_DIR}; refusing to remove anything")
            if os.path.lexists(lp):
                if not _is_link(lp):
                    raise DataStoreError(f"{link} is not a link (real file/dir); refusing to remove anything")
                _unlink_link(lp)
                removed_link = link
        entries = [x for x in entries if x.get("name") != name]
        _write(entries)
    return {"removed": name, "link_removed": removed_link, "path": e.get("path"),
            "target_exists": os.path.exists(e.get("path", ""))}


def refresh(name: str) -> dict:
    """Re-stat the target: size/mtime, ``missing: true`` if it is gone (the entry is kept)."""
    with _LOCK, _registry_lock():
        entries = _read()
        e = _find(entries, name)
        p = Path(e["path"])
        e.update(_stat_fields(p))
        if not e["missing"]:
            e["kind"] = _kind(p)
        _write(entries)
    return dict(e)


# --------------------------------------------------------------------------------------------------- preview / load
def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):  # numpy scalar
        try:
            return _clean(v.item())
        except Exception:
            return str(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _df_preview(df, rows: int) -> dict:
    head = df.head(rows)
    return {"columns": [str(c) for c in head.columns],
            "rows": [[_clean(v) for v in r] for r in head.itertuples(index=False, name=None)],
            "n_rows": int(len(df))}


def _text_head(p: Path, max_lines: int = 200, max_bytes: int = 64_000) -> str | None:
    with open(p, "rb") as f:
        raw = f.read(max_bytes)
    if b"\x00" in raw:
        return None
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        txt = raw.decode("cp1252", "replace")  # not UTF-8 but no NULs: show it anyway
    return "\n".join(txt.splitlines()[:max_lines])


def _structured(p: Path):
    """Parse json / jsonl / yaml file."""
    suf = p.suffix.lower()
    text = p.read_text(encoding="utf-8")
    if suf in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suf == ".json":
        return json.loads(text)
    import yaml
    return yaml.safe_load(text)


def _records_frame(obj, sheet=None):
    import pandas as pd
    if sheet is not None:
        if not isinstance(obj, dict) or sheet not in obj:
            raise DataStoreError(f"no table {sheet!r} in this file")
        obj = obj[sheet]
    if isinstance(obj, list):
        if all(isinstance(r, dict) for r in obj):
            return pd.DataFrame.from_records(obj)
        if all(isinstance(r, (list, tuple)) for r in obj) and obj:
            return pd.DataFrame(obj[1:], columns=[str(c) for c in obj[0]])
        raise DataStoreError("expected a list of records")
    if isinstance(obj, dict):
        for key in ("records", "rows", "data"):
            if isinstance(obj.get(key), list):
                return _records_frame(obj[key])
        if obj and all(isinstance(v, list) for v in obj.values()):
            try:
                return pd.DataFrame(obj)  # column-oriented
            except ValueError as e:
                raise DataStoreError(f"not a single table ({e}); pass sheet=<key>") from e
        raise DataStoreError("not a table: a dict without a records list; pass sheet=<key>")
    raise DataStoreError("not a table")


def _table_from_path(p: Path, sheet=None):
    try:
        return _table_from_path_raw(p, sheet)
    except DataStoreError:
        raise
    except (ValueError, OSError, UnicodeDecodeError) as e:  # pandas parser errors are ValueErrors
        raise DataStoreError(f"cannot read {p.name} as a table: {e}") from e


def _table_from_path_raw(p: Path, sheet=None):
    import pandas as pd
    kind = _kind(p)
    if kind == "tsv":
        return pd.read_csv(p, sep="\t")
    if kind == "csv":
        return pd.read_csv(p)
    if kind in ("json", "yaml"):
        return _records_frame(_structured(p), sheet)
    raise DataStoreError(f"{p.name}: kind {kind!r} is not a table (tsv/csv/json/yaml)")


def _path_of(name: str) -> tuple[dict, Path]:
    e = get(name)
    p = Path(e["path"])
    if not p.exists():
        raise DataStoreError(f"dataset {name!r}: target missing ({_missing_msg(p)})")
    # re-checked on every read: an entry registered before DESIGNER_DATA_ROOTS was set (or edited into the
    # registry by hand), or a symlink retargeted since, must not become a way to read files outside the data roots
    check_allowed(Path(os.path.realpath(p)))
    return e, p


def load_table(name: str, sheet: str | None = None):
    """pandas.DataFrame for a tsv/csv/json-records/yaml-list dataset.

    ``sheet``: for a json/yaml dict of tables, the key to use; for a ``dir`` dataset, a file path relative to it.
    """
    e, p = _path_of(name)
    if p.is_dir():
        if not sheet:
            raise DataStoreError(f"dataset {name!r} is a directory; pass sheet=<relative file>")
        f = Path(os.path.realpath(p / sheet))
        if not _inside(f, Path(os.path.realpath(p))):
            raise DataStoreError("sheet must stay inside the dataset directory")
        if not f.is_file():
            raise DataStoreError(f"{sheet!r} not found in {p}")
        check_allowed(f)
        return _table_from_path(f)
    return _table_from_path(p, sheet)


def preview(name: str, rows: int = 50) -> dict:
    """Small JSON-friendly preview of a dataset (see contract)."""
    rows = max(1, min(int(rows), 1000))
    e, p = _path_of(name)
    kind = _kind(p)
    out: dict = {"name": name, "path": str(p)}
    if kind == "dir":
        entries = []
        total = 0
        try:
            with os.scandir(p) as it:
                items = sorted(it, key=lambda d: (not d.is_dir(), d.name.lower()))
        except OSError as ex:
            raise DataStoreError(f"cannot list {p}: {ex}") from ex
        total = len(items)
        for de in items[:200]:
            try:
                is_dir = de.is_dir()
                size = None if is_dir else de.stat().st_size
            except OSError:
                is_dir, size = False, None
            entries.append({"name": de.name, "is_dir": is_dir, "size_bytes": size,
                            "link": _is_link_entry(de)})
        out.update(kind="dir", entries=entries, total=total, truncated=total > 200)
        return out
    if kind in ("tsv", "csv"):
        import pandas as pd
        df = pd.read_csv(p, sep="\t" if kind == "tsv" else ",", nrows=rows)
        out.update(kind=kind, **_df_preview(df, rows))
        out.pop("n_rows", None)
        return out
    if kind in ("json", "yaml"):
        try:
            obj = _structured(p)
        except Exception as ex:
            out.update(kind="text", head=_text_head(p) or "", parse_error=str(ex))
            return out
        if kind == "json":
            pretty = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
        else:
            import yaml
            pretty = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=None)
        lines = pretty.splitlines()
        out.update(kind=kind, head="\n".join(lines[:200]), truncated=len(lines) > 200)
        try:
            out.update(_df_preview(_records_frame(obj), rows))
        except Exception:
            pass
        return out
    if kind == "npz":
        try:
            import numpy as np
            with np.load(p, allow_pickle=False) as z:
                arrs = [{"name": k, "shape": list(z[k].shape), "dtype": str(z[k].dtype)} for k in z.files[:200]]
            out.update(kind="npz", entries=arrs)
        except Exception as ex:
            out.update(kind="binary", size_bytes=p.stat().st_size, error=str(ex))
        return out
    head = _text_head(p)
    if head is None:
        out.update(kind="binary", size_bytes=p.stat().st_size)
    else:
        out.update(kind="text", head=head)
    return out
