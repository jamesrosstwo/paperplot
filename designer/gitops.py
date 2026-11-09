"""Git operations on the paper repo (contract section 4).

Every function runs ``git`` through ``subprocess`` with a timeout and returns a dict; nothing here raises on a git
failure (``{"ok": False, "error": ...}`` instead). The repo defaults to ``config.PAPER_ROOT``; every function also takes
an optional ``repo=`` so tests can point it at a throwaway repository.

Safety: pull is ``--ff-only``; push is a plain ``git push`` and the argument guard below rejects any force flag or
``+refspec`` outright; commit stages and commits only the paths it is given (git runs with literal pathspecs, and
wildcards / ``:(magic)`` are refused), and refuses paths outside the repo or inside ``.git``. Nothing in this module pushes on its own -- ``push()`` runs only when the server is asked to.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from designer import config

__all__ = ["status", "fetch", "pull", "diff_stat", "commit", "push"]

LOCAL_TIMEOUT = 30
NETWORK_TIMEOUT = 180
_FORBIDDEN = {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d",
              "--no-verify", "--prune"}


def _git_exe() -> str:
    found = shutil.which("git")
    if found or os.name != "nt":
        return found or "git"
    for cand in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe",
                 r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if os.path.exists(cand):
            return cand
    return "git"


def _repo(repo) -> Path:
    return Path(repo if repo is not None else config.PAPER_ROOT).resolve()


def _run(args: list[str], repo: Path, timeout: float = LOCAL_TIMEOUT) -> dict:
    """Run ``git -C repo <args>``. Returns {ok, code, stdout, stderr, cmd}; never raises."""
    opts = args[:args.index("--")] if "--" in args else args   # pathspecs after "--" are not options
    bad = [a for a in opts if a in _FORBIDDEN or (a.startswith("+") and len(a) > 1) or a.startswith("--force")]
    if bad:
        return {"ok": False, "code": None, "stdout": "", "stderr": "", "cmd": "git " + " ".join(args),
                "error": f"refused: forbidden git arguments {bad}"}
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # never hang on a credential prompt in the terminal
    env["LC_ALL"] = env.get("LC_ALL", "C")
    env["GIT_LITERAL_PATHSPECS"] = "1"   # paths are file names, never globs or :(magic) -- "*" must not stage everything
    env["GIT_OPTIONAL_LOCKS"] = "0"      # read-only commands (status) never rewrite .git/index
    # commit identity for a service / container without a user-level git config (CONTRACT section 8)
    for var, keys in (("DESIGNER_GIT_NAME", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                      ("DESIGNER_GIT_EMAIL", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        val = (os.environ.get(var) or "").strip()
        if val:
            for k in keys:
                env[k] = val
    cmd = [_git_exe(), "-c", "core.quotepath=off", "--literal-pathspecs", "-C", str(repo), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "stdout": "", "stderr": "", "cmd": "git " + " ".join(args),
                "error": f"git timed out after {timeout:.0f}s"}
    except OSError as e:
        return {"ok": False, "code": None, "stdout": "", "stderr": "", "cmd": "git " + " ".join(args),
                "error": f"could not run git: {e}"}
    out = r.stdout.decode("utf-8", "replace")
    err = scrub(r.stderr.decode("utf-8", "replace"))      # git can echo a remote URL with its token
    res = {"ok": r.returncode == 0, "code": r.returncode, "stdout": out, "stderr": err,
           "cmd": "git " + " ".join(args)}
    if r.returncode != 0:
        res["error"] = (err.strip() or out.strip() or f"git exited {r.returncode}")
    return res


_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s:]*(?::[^/@\s]*)?@")


def scrub(text: str) -> str:
    """Hide credentials embedded in URLs (https://git:<token>@git.overleaf.com/... -> https://***@git.overleaf.com/...)
    before git output reaches the browser."""
    return _URL_CREDS_RE.sub(r"\1***@", text or "")


def _result(r: dict, **extra) -> dict:
    d = {"ok": r["ok"], "output": scrub((r.get("stdout", "") + r.get("stderr", "")).strip())}
    if not r["ok"]:
        d["error"] = scrub(r.get("error", "git failed"))
    d.update(extra)
    return d


def _is_repo(repo: Path) -> dict | None:
    """None if ``repo`` is a git work tree, else an error dict."""
    if not repo.is_dir():
        return {"ok": False, "error": f"{repo} is not a directory"}
    r = _run(["rev-parse", "--is-inside-work-tree"], repo)
    if not r["ok"] or r["stdout"].strip() != "true":
        return {"ok": False, "error": f"{repo} is not a git work tree: {r.get('error', '')}".strip()}
    return None


# ------------------------------------------------------------------------------------------------------- status
def _parse_branch(line: str) -> dict:
    """Parse the porcelain v1 '## ...' line."""
    info = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "detached": False, "gone": False}
    s = line[3:] if line.startswith("## ") else line
    for prefix in ("No commits yet on ", "Initial commit on "):
        if s.startswith(prefix):
            info["branch"] = s[len(prefix):].split("...")[0].strip()
            return info
    if s.startswith("HEAD (no branch)"):
        info["detached"] = True
        return info
    track = ""
    if " [" in s and s.endswith("]"):
        s, track = s[:s.index(" [")], s[s.index(" [") + 2:-1]
    if "..." in s:
        info["branch"], info["upstream"] = s.split("...", 1)
    else:
        info["branch"] = s.strip()
    for part in track.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            info["ahead"] = int(part[6:])
        elif part.startswith("behind "):
            info["behind"] = int(part[7:])
        elif part == "gone":
            info["gone"] = True
    return info


_BUILD_EXTS = (".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out", ".pdf", ".synctex.gz", ".synctex",
               ".toc", ".lof", ".lot", ".bcf", ".run.xml", ".xdv", ".nav", ".snm")


def _is_build_artifact(path: str, code: str) -> bool:
    """An untracked LaTeX build file of the main document at the repo root (paper.aux, paper.pdf, ...).
    The paper repo has no .gitignore, so the designer's compiles leave these untracked; the UI keeps them out of the
    commit defaults and the dirty count. Tracked files are never classed as artefacts."""
    if code != "??" or "/" in path:
        return False
    stem = Path(config.MAIN_TEX).stem
    return any(path == stem + ext for ext in _BUILD_EXTS)


def status(repo=None) -> dict:
    """{ok, branch, upstream, ahead, behind, dirty: [{path, code}], last_commit: {sha, subject, when} | None}.

    ahead/behind compare against the last fetched remote ref; call fetch() to update it.
    """
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return {**bad, "branch": None, "upstream": None, "ahead": 0, "behind": 0, "dirty": [], "last_commit": None}
    r = _run(["--no-optional-locks", "status", "--porcelain=v1", "-b", "-z", "--untracked-files=all"], root)
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "branch": None, "upstream": None, "ahead": 0, "behind": 0,
                "dirty": [], "last_commit": None}
    recs = r["stdout"].split("\0")
    info = _parse_branch(recs[0]) if recs and recs[0].startswith("## ") else _parse_branch("")
    dirty = []
    i = 1 if recs and recs[0].startswith("## ") else 0
    while i < len(recs):
        rec = recs[i]
        i += 1
        if len(rec) < 4:
            continue
        code, path = rec[:2], rec[3:]
        item = {"path": path, "code": code}
        if _is_build_artifact(path, code):
            item["artifact"] = True
        if code[0] in "RC" or code[1] in "RC":  # -z: 'XY new\0orig\0'
            if i < len(recs):
                item["orig_path"] = recs[i]
                i += 1
        dirty.append(item)
    last = None
    lg = _run(["log", "-1", "--format=%H%x1f%s%x1f%cI"], root)
    if lg["ok"] and lg["stdout"].strip():
        sha, subject, when = (lg["stdout"].strip().split("\x1f") + ["", ""])[:3]
        last = {"sha": sha, "subject": subject, "when": when}
    n_art = sum(1 for d in dirty if d.get("artifact"))
    return {"ok": True, "repo": str(root), **info, "dirty": dirty, "dirty_count": len(dirty) - n_art,
            "artifact_count": n_art, "last_commit": last}


# ------------------------------------------------------------------------------------------------------- network
def fetch(repo=None) -> dict:
    """``git fetch`` (updates remote-tracking refs only; the work tree is untouched). Returns result + status."""
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return bad
    r = _run(["fetch"], root, timeout=NETWORK_TIMEOUT)
    return _result(r, status=status(root))


def pull(repo=None) -> dict:
    """``git pull --ff-only`` -- never merges or rebases. Returns result + status."""
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return bad
    r = _run(["pull", "--ff-only"], root, timeout=NETWORK_TIMEOUT)
    return _result(r, status=status(root))


def push(repo=None) -> dict:
    """Plain ``git push`` of the current branch to its upstream. Never ``--force``. Returns result + status."""
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return bad
    r = _run(["push"], root, timeout=NETWORK_TIMEOUT)
    return _result(r, status=status(root))


# ------------------------------------------------------------------------------------------------------- paths
def _rel_paths(paths, root: Path) -> tuple[list[str], list[str]]:
    """Normalise to repo-relative posix paths. Returns (good, errors)."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    good, errors = [], []
    real_root = os.path.realpath(root)
    root_n = os.path.normcase(real_root)
    for p in paths or []:
        s = str(p).strip()
        if not s:
            continue
        if s.startswith("-"):
            errors.append(f"{s}: paths may not start with '-'")
            continue
        if s.startswith(":") or any(ch in s for ch in "*?"):
            errors.append(f"{s}: wildcards and pathspec magic are not allowed; list each file")
            continue
        q = Path(s)
        absp = Path(os.path.abspath(q if q.is_absolute() else Path(real_root) / q))
        # resolve the parent (not the leaf: a deleted file must still be committable)
        full = os.path.join(os.path.realpath(absp.parent), absp.name)
        full_n = os.path.normcase(full)
        try:
            inside = os.path.commonpath([full_n, root_n]) == root_n and full_n != root_n
        except ValueError:  # different drives
            inside = False
        if not inside:
            errors.append(f"{s}: outside the repository {root}")
            continue
        rel_posix = Path(os.path.relpath(full, real_root)).as_posix()
        if rel_posix.lower() == ".git" or rel_posix.lower().startswith(".git/"):
            errors.append(f"{s}: inside .git")
            continue
        good.append(rel_posix)
    return list(dict.fromkeys(good)), errors


def diff_stat(paths=None, repo=None) -> dict:
    """{ok, files: [{path, added, deleted, binary, untracked}], stat (text)} for ``paths`` (all changes if None)."""
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return bad
    rels, errors = _rel_paths(paths, root) if paths else ([], [])
    if errors:
        return {"ok": False, "error": "; ".join(errors), "files": [], "stat": ""}
    has_head = _run(["rev-parse", "--verify", "-q", "HEAD"], root)["ok"]
    base = ["HEAD"] if has_head else ["--cached"]
    spec = ["--", *rels] if rels else []
    num = _run(["diff", "--numstat", *base, *spec], root)
    st = _run(["diff", "--stat", *base, *spec], root)
    if not num["ok"]:
        return {"ok": False, "error": num["error"], "files": [], "stat": ""}
    files = []
    for line in num["stdout"].splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, p = parts
        binary = a == "-"
        files.append({"path": p, "added": None if binary else int(a), "deleted": None if binary else int(d),
                      "binary": binary, "untracked": False})
    unt = _run(["ls-files", "--others", "--exclude-standard", "-z", *spec], root)
    if unt["ok"]:
        for p in filter(None, unt["stdout"].split("\0")):
            files.append({"path": p, "added": None, "deleted": None, "binary": None, "untracked": True})
    return {"ok": True, "files": files, "stat": st["stdout"] if st["ok"] else ""}


def commit(paths, message: str, repo=None) -> dict:
    """Stage exactly ``paths`` (``git add -- <paths>``) and commit only them (``git commit -m msg -- <paths>``).

    Anything else already staged stays staged but is not part of this commit. Refuses paths outside the repo.
    Returns {ok, sha, output, status} or {ok: False, error}.
    """
    root = _repo(repo)
    bad = _is_repo(root)
    if bad:
        return bad
    if not isinstance(message, str) or not message.strip():
        return {"ok": False, "error": "commit message is empty"}
    rels, errors = _rel_paths(paths, root)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    if not rels:
        return {"ok": False, "error": "no paths given"}
    add = _run(["add", "--", *rels], root)
    if not add["ok"]:
        return _result(add)
    c = _run(["commit", "-m", message.strip(), "--", *rels], root, timeout=LOCAL_TIMEOUT * 2)
    if not c["ok"]:
        return _result(c)
    sha = _run(["rev-parse", "HEAD"], root)
    return _result(c, sha=sha["stdout"].strip() if sha["ok"] else None, paths=rels, status=status(root))
