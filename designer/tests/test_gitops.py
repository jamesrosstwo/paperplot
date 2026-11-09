"""Tests for designer.gitops on throwaway repositories only (git init + a bare repo as the remote).

    PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe designer/tests/test_gitops.py

Never touches the live Overleaf clone: every call passes repo=<temp dir>, and config.PAPER_ROOT is pointed at a
temp dir too, so a forgotten repo= would hit a throwaway path.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESIGNER_STATE_DIR", tempfile.mkdtemp(prefix="designer_state_"))

from designer import config, gitops  # noqa: E402

LIVE = os.path.normcase(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf")
_TMPS = []
_ORIG_ROOT = config.PAPER_ROOT  # restored in teardown so other test modules see the scratch clone


def sh(repo, *args, check=True):
    assert not os.path.normcase(str(repo)).startswith(LIVE)
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def ident(repo):
    sh(repo, "config", "user.name", "Designer Test")
    sh(repo, "config", "user.email", "designer-test@example.invalid")
    sh(repo, "config", "commit.gpgsign", "false")
    sh(repo, "config", "core.autocrlf", "false")


def make_repos():
    """remote.git (bare), work (the 'paper'), other (a second clone that plays Overleaf)."""
    t = Path(tempfile.mkdtemp(prefix="gitops_test_"))
    _TMPS.append(t)
    config.PAPER_ROOT = t / "work"  # safety net: a missing repo= hits a temp path, never the live clone
    bare, work, other = t / "remote.git", t / "work", t / "other"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    ident(work)
    (work / "paper.tex").write_bytes(b"\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
    (work / "figures").mkdir()
    (work / "figures" / "plot.pdf").write_bytes(b"%PDF-1.4 fake\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-m", "initial")
    sh(work, "remote", "add", "origin", str(bare))
    sh(work, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    ident(other)
    return t, bare, work, other


def test_status_clean_and_dirty():
    t, bare, work, other = make_repos()
    s = gitops.status(repo=work)
    assert s["ok"] and s["branch"] == "main" and s["upstream"] == "origin/main"
    assert s["ahead"] == 0 and s["behind"] == 0 and s["dirty"] == []
    assert s["last_commit"]["subject"] == "initial" and len(s["last_commit"]["sha"]) == 40 and s["last_commit"]["when"]
    (work / "paper.tex").write_bytes(b"changed\n")
    (work / "figures" / "new plot ü.png").write_bytes(b"png")
    s = gitops.status(repo=work)
    codes = {d["path"]: d["code"] for d in s["dirty"]}
    assert codes == {"paper.tex": " M", "figures/new plot ü.png": "??"}, codes
    sh(work, "mv", "figures/plot.pdf", "figures/plot2.pdf")
    s = gitops.status(repo=work)
    ren = [d for d in s["dirty"] if d["code"].startswith("R")]
    assert ren and ren[0]["path"] == "figures/plot2.pdf" and ren[0]["orig_path"] == "figures/plot.pdf"


def test_status_flags_untracked_latex_build_files():
    t, bare, work, other = make_repos()
    for name in ("paper.aux", "paper.log", "paper.synctex.gz", "paper.pdf", "notes.aux"):
        (work / name).write_bytes(b"x")
    (work / "figures" / "paper.log").write_bytes(b"x")   # not at the root -> a real file
    (work / "paper.tex").write_bytes(b"changed\n")       # tracked -> never an artefact
    s = gitops.status(repo=work)
    art = sorted(d["path"] for d in s["dirty"] if d.get("artifact"))
    assert art == ["paper.aux", "paper.log", "paper.pdf", "paper.synctex.gz"], art
    assert s["artifact_count"] == 4 and s["dirty_count"] == len(s["dirty"]) - 4
    assert not any(d.get("artifact") for d in s["dirty"] if d["path"] in ("paper.tex", "notes.aux", "figures/paper.log"))


def test_commit_only_given_paths_and_push():
    t, bare, work, other = make_repos()
    (work / "paper.tex").write_bytes(b"edited paper\n")
    (work / "figures" / "plot.pdf").write_bytes(b"%PDF-1.4 new\n")
    (work / "figures" / "plot.png").write_bytes(b"png")
    (work / "notes.txt").write_bytes(b"staged but not selected\n")
    sh(work, "add", "notes.txt")  # pre-staged unrelated file must NOT be swept into the commit
    r = gitops.commit(["figures/plot.pdf", str(work / "figures" / "plot.png")], "Update plot", repo=work)
    assert r["ok"], r
    assert r["paths"] == ["figures/plot.pdf", "figures/plot.png"]
    files = sh(work, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(files) == ["figures/plot.pdf", "figures/plot.png"], files
    assert r["sha"] == sh(work, "rev-parse", "HEAD")
    s = r["status"]
    assert s["ahead"] == 1
    codes = {d["path"]: d["code"] for d in s["dirty"]}
    assert codes == {"paper.tex": " M", "notes.txt": "A "}, codes  # untouched, still staged
    remote_before = sh(bare, "rev-parse", "main")
    p = gitops.push(repo=work)
    assert p["ok"], p
    assert sh(bare, "rev-parse", "main") == r["sha"] != remote_before
    assert p["status"]["ahead"] == 0


def test_commit_deleted_file():
    t, bare, work, other = make_repos()
    (work / "figures" / "plot.pdf").unlink()
    r = gitops.commit(["figures/plot.pdf"], "remove plot", repo=work)
    assert r["ok"], r
    assert sh(work, "show", "--name-status", "--format=", "HEAD") == "D\tfigures/plot.pdf"


def test_commit_refusals():
    t, bare, work, other = make_repos()
    head = sh(work, "rev-parse", "HEAD")
    (t / "elsewhere.txt").write_bytes(b"x")
    (work / "paper.tex").write_bytes(b"edit\n")
    for paths in (["../elsewhere.txt"], [str(t / "elsewhere.txt")], [".git/config"], ["-A"], [str(work)],
                  ["paper.tex", "../elsewhere.txt"], [], ["C:/Windows/win.ini"]):
        r = gitops.commit(paths, "msg", repo=work)
        assert r["ok"] is False and r["error"], (paths, r)
    assert gitops.commit(["paper.tex"], "   ", repo=work)["ok"] is False
    assert sh(work, "rev-parse", "HEAD") == head
    assert sh(work, "diff", "--cached", "--name-only") == ""  # nothing got staged by the refused calls
    # nothing to commit (unchanged file) -> ok False, no raise
    r = gitops.commit(["figures/plot.pdf"], "noop", repo=work)
    assert r["ok"] is False and sh(work, "rev-parse", "HEAD") == head


def test_commit_globs_and_magic_are_refused_or_literal():
    t, bare, work, other = make_repos()
    head = sh(work, "rev-parse", "HEAD")
    (work / "paper.tex").write_bytes(b"edit\n")
    (work / "notes_private.txt").write_bytes(b"secret\n")
    (work / "paper.aux").write_bytes(b"aux\n")
    for paths in (["*"], ["figures/*"], [":(top)"], [":(exclude)x"], ["pap?r.tex"], [":/paper.tex"]):
        r = gitops.commit(paths, "only the figure please", repo=work)
        assert r["ok"] is False and r["error"], (paths, r)
    assert sh(work, "rev-parse", "HEAD") == head
    assert sh(work, "diff", "--cached", "--name-only") == ""
    # a literal '[...]' name is taken as a file name (git runs with --literal-pathspecs), not a character class
    (work / "fig[1].pdf").write_bytes(b"%PDF\n")
    (work / "fig1.pdf").write_bytes(b"%PDF other\n")
    r = gitops.commit(["fig[1].pdf"], "bracket name", repo=work)
    assert r["ok"], r
    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == ["fig[1].pdf"]


def test_diff_stat():
    t, bare, work, other = make_repos()
    (work / "paper.tex").write_bytes(b"\\documentclass{article}\n\\begin{document}\nhello\nworld\n\\end{document}\n")
    (work / "figures" / "plot.pdf").write_bytes(b"\x00\x01binary now")
    (work / "figures" / "fresh.png").write_bytes(b"png")
    d = gitops.diff_stat(repo=work)
    assert d["ok"]
    by = {f["path"]: f for f in d["files"]}
    assert by["paper.tex"]["added"] == 2 and by["paper.tex"]["deleted"] == 1
    assert by["figures/plot.pdf"]["binary"] is True
    assert by["figures/fresh.png"]["untracked"] is True
    assert "paper.tex" in d["stat"]
    d = gitops.diff_stat(["paper.tex"], repo=work)
    assert [f["path"] for f in d["files"]] == ["paper.tex"]
    assert gitops.diff_stat(["../x"], repo=work)["ok"] is False


def test_fetch_behind_and_pull_ff_only():
    t, bare, work, other = make_repos()
    (other / "paper.tex").write_bytes(b"overleaf edit\n")
    sh(other, "commit", "-am", "overleaf edit")
    sh(other, "push")
    assert gitops.status(repo=work)["behind"] == 0  # not fetched yet
    f = gitops.fetch(repo=work)
    assert f["ok"], f
    assert f["status"]["behind"] == 1 and f["status"]["ahead"] == 0
    p = gitops.pull(repo=work)
    assert p["ok"], p
    assert (work / "paper.tex").read_bytes() == b"overleaf edit\n" and p["status"]["behind"] == 0


def test_diverged_pull_refused_and_push_rejected_without_force():
    t, bare, work, other = make_repos()
    (other / "paper.tex").write_bytes(b"remote side\n")
    sh(other, "commit", "-am", "remote side")
    sh(other, "push")
    remote_head = sh(bare, "rev-parse", "main")
    (work / "figures" / "plot.pdf").write_bytes(b"local side")
    assert gitops.commit(["figures/plot.pdf"], "local side", repo=work)["ok"]
    local_head = sh(work, "rev-parse", "HEAD")
    p = gitops.pull(repo=work)
    assert p["ok"] is False and p["error"]  # --ff-only refuses to merge
    assert sh(work, "rev-parse", "HEAD") == local_head  # no merge commit
    assert p["status"]["ahead"] == 1 and p["status"]["behind"] == 1
    r = gitops.push(repo=work)
    assert r["ok"] is False  # non-fast-forward rejected, and we never force
    assert sh(bare, "rev-parse", "main") == remote_head


def test_force_guard_and_failures_never_raise():
    t, bare, work, other = make_repos()
    for args in (["push", "--force"], ["push", "-f"], ["push", "--force-with-lease"], ["push", "origin", "+main"],
                 ["push", "--force-if-includes"], ["push", "--mirror"]):
        r = gitops._run(args, work)
        assert r["ok"] is False and "refused" in r["error"], args
    # '+name' after '--' is a pathspec, not a refspec
    assert gitops._run(["status", "--", "+notes"], work)["ok"] is True
    nonrepo = t / "plain"
    nonrepo.mkdir()
    for fn in (gitops.status, gitops.fetch, gitops.pull, gitops.push):
        r = fn(repo=nonrepo)
        assert r["ok"] is False and r["error"], fn
    assert gitops.commit(["a"], "m", repo=nonrepo)["ok"] is False
    assert gitops.status(repo=t / "missing")["ok"] is False
    # fetch with an unreachable remote returns an error dict
    sh(work, "remote", "set-url", "origin", str(t / "no-such-remote.git"))
    assert gitops.fetch(repo=work)["ok"] is False
    # a timeout becomes an error dict
    real = subprocess.run

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    gitops.subprocess.run = slow
    try:
        r = gitops.status(repo=work)
    finally:
        gitops.subprocess.run = real
    assert r["ok"] is False and "timed out" in r["error"]


def test_status_no_upstream_and_unborn():
    t = Path(tempfile.mkdtemp(prefix="gitops_test_"))
    _TMPS.append(t)
    subprocess.run(["git", "init", "-b", "trunk", str(t / "r")], check=True, capture_output=True)
    s = gitops.status(repo=t / "r")
    assert s["ok"] and s["branch"] == "trunk" and s["upstream"] is None and s["last_commit"] is None
    assert gitops._parse_branch("## main...origin/main [ahead 2, behind 3]") | {} == {
        "branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 3, "detached": False, "gone": False}
    assert gitops._parse_branch("## HEAD (no branch)")["detached"] is True
    assert gitops._parse_branch("## main...origin/main [gone]")["gone"] is True


def _cleanup():
    for t in _TMPS:
        def onerr(func, path, exc):  # git object files are read-only on Windows
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(t, onerror=onerr)


def teardown_module(module=None):
    _cleanup()
    config.PAPER_ROOT = _ORIG_ROOT


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception as ex:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {n}: {ex}")
    _cleanup()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
