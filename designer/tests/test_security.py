"""Security review fixes (2026-11-08): data-root allow-list and deny-list, TeX file policy and shell escape, read-only
context previews, context compile back-pressure, tracebacks, the POST login form, token redaction in dev-server logs,
git output scrubbing, and seeds kept out of any state dir.

Run:  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe -m pytest designer/tests/test_security.py -q

conftest.py points DESIGNER_PAPER_ROOT at the scratch clone and DESIGNER_STATE_DIR at a fresh temp dir. The data
store registry is pointed at a temp dir per test. Nothing here commits, pulls or pushes.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from designer import config, datastore, gitops, latex, server, spec
from designer.datastore import DataStoreError

LIVE_CLONE = Path(config.LIVE_PAPER_WINDOWS).resolve()
assert config.PAPER_ROOT.resolve() != LIVE_CLONE, "refusing to test against the live Overleaf clone"

TOKEN = "test-token-" + "Zq81vR"          # a throwaway value for these tests only
HOST = "127.0.0.1:8768"
HAS_PAPER = (config.PAPER_ROOT / config.MAIN_TEX).is_file()


@pytest.fixture()
def tree(monkeypatch):
    """Temp tree: registry + links under state/, data/ (allowed), outside/ (not allowed). Local tool by default."""
    t = Path(tempfile.mkdtemp(prefix="sec_test_"))
    (t / "state" / "data" / "links").mkdir(parents=True)
    (t / "data").mkdir()
    (t / "outside").mkdir()
    (t / "data" / "evals.tsv").write_text("task\tk\tn\nmug\t16\t20\n", encoding="utf-8")
    (t / "outside" / "secret.conf").write_text("NOT_A_REAL_SECRET=1\n", encoding="utf-8")
    (t / "outside" / "table.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setattr(config, "REGISTRY_PATH", t / "state" / "data" / "registry.json")
    monkeypatch.setattr(config, "LINKS_DIR", t / "state" / "data" / "links")
    monkeypatch.setattr(config, "TOKEN", None)
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(config, "DATA_ROOTS", None)
    monkeypatch.setattr(config, "READONLY", False)
    yield t
    shutil.rmtree(t, ignore_errors=True)


def _restrict(monkeypatch, *roots):
    monkeypatch.setattr(config, "TOKEN", TOKEN)
    monkeypatch.setattr(config, "DATA_ROOTS", [Path(os.path.realpath(r)) for r in roots])


def _dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


# ============================================================================================ data roots
def test_data_roots_policy(monkeypatch, tree):
    assert config.data_roots_policy() is None                       # tokenless loopback: the local tool
    monkeypatch.setattr(config, "TOKEN", TOKEN)
    assert config.data_roots_policy() == []                         # token, no roots: nothing allowed
    monkeypatch.setattr(config, "TOKEN", None)
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    assert config.data_roots_policy() == []
    monkeypatch.setattr(config, "DATA_ROOTS", [tree / "data"])
    assert config.data_roots_policy() == [tree / "data"]


def test_token_without_roots_registers_nothing(monkeypatch, tree):
    monkeypatch.setattr(config, "TOKEN", TOKEN)
    with pytest.raises(DataStoreError, match="DESIGNER_DATA_ROOTS"):
        datastore.add("evals", str(tree / "data" / "evals.tsv"), make_link=False)


def test_roots_allow_inside_refuse_outside(monkeypatch, tree):
    _restrict(monkeypatch, tree / "data")
    e = datastore.add("evals", str(tree / "data" / "evals.tsv"), make_link=False)
    assert Path(e["path"]) == Path(os.path.realpath(tree / "data" / "evals.tsv"))
    assert datastore.preview("evals")["kind"] == "tsv"
    for ref in (tree / "outside" / "secret.conf", tree / "outside", tree):
        with pytest.raises(DataStoreError, match="outside the data directories"):
            datastore.add("probe", str(ref), make_link=False)
    # a link inside the root that points outside is judged by its target
    _dir_link(tree / "data" / "escape", tree / "outside")
    with pytest.raises(DataStoreError, match="outside the data directories"):
        datastore.add("probe", str(tree / "data" / "escape" / "secret.conf"), make_link=False)
    with pytest.raises(DataStoreError, match="outside the data directories"):
        datastore.add("probe", (tree / "outside" / "secret.conf").as_uri(), make_link=False)
    assert [x["name"] for x in datastore.list_datasets()] == ["evals"]


def test_restricted_refs_are_literal(monkeypatch, tree):
    monkeypatch.setenv("SEC_TEST_DIR", str(tree / "data"))
    ref = ("%SEC_TEST_DIR%" if os.name == "nt" else "$SEC_TEST_DIR") + os.sep + "evals.tsv"
    assert datastore.add("local", ref, make_link=False)["kind"] == "tsv"     # the local tool expands variables
    _restrict(monkeypatch, tree / "data")
    with pytest.raises(DataStoreError, match="absolute"):
        datastore.add("net", ref, make_link=False)
    with pytest.raises(DataStoreError, match="absolute"):
        datastore.add("net2", "~" + os.sep + "x.tsv", make_link=False)


def test_denied_places_even_for_the_local_tool(monkeypatch, tree):
    home = tree / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("not a key\n", encoding="utf-8")
    (home / ".git-credentials").write_text("https://u:p@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for ref in (home / ".ssh" / "id_test", home / ".ssh", home / ".git-credentials"):
        with pytest.raises(DataStoreError, match="can never be registered"):
            datastore.add("probe", str(ref), make_link=False)
    state_file = Path(config.STATE_DIR) / "sec_probe.txt"
    state_file.write_text("x\n", encoding="utf-8")
    try:
        with pytest.raises(DataStoreError, match="can never be registered"):
            datastore.add("probe", str(state_file), make_link=False)
    finally:
        state_file.unlink()
    if (config.PAPER_ROOT / ".git").exists():
        with pytest.raises(DataStoreError, match="can never be registered"):
            datastore.add("probe", str(config.PAPER_ROOT / ".git"), make_link=False)
    cred = tree / "outside" / "mounted-credentials"
    cred.write_text("https://u:p@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("DESIGNER_GIT_CREDENTIALS_FILE", str(cred))
    with pytest.raises(DataStoreError, match="can never be registered"):
        datastore.add("probe", str(cred), make_link=False)


def test_reads_recheck_the_roots(monkeypatch, tree):
    """An entry registered before the roots were set (or edited into the registry) cannot be read through."""
    datastore.add("old_text", str(tree / "outside" / "secret.conf"), make_link=False)
    datastore.add("old_table", str(tree / "outside" / "table.tsv"), make_link=False)
    assert "NOT_A_REAL_SECRET" in datastore.preview("old_text")["head"]      # local tool: fine
    _restrict(monkeypatch, tree / "data")
    with pytest.raises(DataStoreError, match="outside the data directories"):
        datastore.preview("old_text")
    with pytest.raises(DataStoreError, match="outside the data directories"):
        datastore.load_table("old_table")
    datastore.remove("old_text")                    # removing an entry stays possible
    app = server.create_app()
    c = app.test_client()
    r = c.get("/api/data/old_table/preview", headers={"Host": HOST, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400 and "NOT_A_REAL_SECRET" not in r.get_data(as_text=True)
    r = c.post("/api/data", json={"name": "probe", "ref": str(tree / "outside" / "secret.conf"), "make_link": False},
               headers={"Host": HOST, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400 and "outside the data directories" in r.get_json()["detail"]


# ============================================================================================ TeX
def test_latex_env_forces_paranoid_file_access(monkeypatch):
    monkeypatch.setenv("openin_any", "a")
    monkeypatch.setenv("openout_any", "a")
    env = config.latex_env()
    assert env["openin_any"] == "p" and env["openout_any"] == "p"


def test_compiles_disable_shell_escape_and_rc_files(monkeypatch):
    calls = []

    def fake_run(cmd, cwd, timeout, env_extra=None):
        calls.append((list(cmd), timeout, env_extra))
        return 0, "", False

    monkeypatch.setattr(latex, "_run", fake_run)
    flag = "-disable-write18" if latex._is_miktex() else "-no-shell-escape"
    root = Path(tempfile.mkdtemp(prefix="sec_paper_"))
    (root / config.MAIN_TEX).write_text("\\documentclass{article}\\begin{document}x\\end{document}\n",
                                        encoding="utf-8")
    try:
        latex.compile_paper(root=root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    cmd = calls[-1][0]
    assert "-norc" in cmd, cmd
    assert f"-pdflatex=pdflatex {flag} %O %S" in cmd, cmd
    if HAS_PAPER:
        fig = next(f for f in latex.scan_figures(config.PAPER_ROOT) if not f.get("commented"))
        latex.compile_context(fig["fid"], root=config.PAPER_ROOT)
        cmd, timeout, env_extra = calls[-1]
        assert flag in cmd and timeout == latex.CONTEXT_TIMEOUT <= 30
        tex = (Path(config.CACHE_DIR) / "context" / (latex._ctx_name(fig["fid"]) + ".tex")).read_text("utf-8")
        if not latex._is_miktex():          # TeX Live: no absolute paths (openin_any=p would refuse them)
            assert config.PAPER_ROOT.as_posix() not in tex
            assert str(config.PAPER_ROOT) in env_extra["TEXINPUTS"]


@pytest.mark.skipif(not HAS_PAPER, reason="needs the scratch paper clone")
def test_texlive_context_cannot_read_absolute_paths():
    """The reviewer's probe: \\input{<absolute path outside the paper>} in a context override must not be read."""
    if shutil.which("pdflatex", path=config.latex_env().get("PATH")) is None:
        pytest.skip("no pdflatex")
    if latex._is_miktex():
        pytest.skip("MiKTeX ignores openin_any (the Windows tool is loopback-only)")
    d = Path(tempfile.mkdtemp(prefix="sec_marker_"))
    marker = d / "marker_outside.tex"
    marker.write_text("SECRETMARKERTEXT\n", encoding="utf-8")
    fig = next(f for f in latex.scan_figures(config.PAPER_ROOT) if not f.get("commented"))
    text = "\\begin{figure}[t]\\centering\\input{" + marker.as_posix() + "}\\caption{x}\\end{figure}"
    try:
        res = latex.compile_context(fig["fid"], text_override=text, root=config.PAPER_ROOT)
        log = (Path(config.CACHE_DIR) / "context" / (res["name"] + ".log")).read_text("utf-8", "replace")
        assert "(" + marker.as_posix() not in log, "pdflatex opened a file outside the paper"
        assert "SECRETMARKERTEXT" not in log
        res = latex.compile_context(fig["fid"], root=config.PAPER_ROOT)      # the stored figure still works
        assert res["ok"], res["errors"][:3]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_readonly_context_refuses_tex_override(monkeypatch):
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "TOKEN", None)
    c = server.create_app().test_client()
    r = c.post("/api/context/paper.tex%3Ano_such_fig", json={"text": "\\input{/etc/passwd}"}, headers={"Host": HOST})
    assert r.status_code == 403 and r.get_json()["error"] == "read-only"
    r = c.post("/api/context/paper.tex%3Ano_such_fig", json={}, headers={"Host": HOST})
    assert r.status_code == 404                     # no override: allowed (this fid just does not exist)


@pytest.mark.skipif(not HAS_PAPER, reason="needs the scratch paper clone")
def test_context_compile_back_pressure(monkeypatch):
    monkeypatch.setattr(config, "TOKEN", None)
    fig = next(f for f in latex.scan_figures(config.PAPER_ROOT) if not f.get("commented"))
    taken = 0
    while latex._CONTEXT_WAITERS.acquire(blocking=False):
        taken += 1
    try:
        assert taken == 2
        c = server.create_app().test_client()
        from urllib.parse import quote
        r = c.post("/api/context/" + quote(fig["fid"], safe=""), json={}, headers={"Host": HOST})
        assert r.status_code == 429 and r.get_json()["error"] == "busy" and r.headers["Retry-After"]
    finally:
        for _ in range(taken):
            latex._CONTEXT_WAITERS.release()


# ============================================================================================ server
def test_tracebacks_only_for_the_local_tool(monkeypatch):
    app = server.create_app()
    with app.test_request_context("/"):
        monkeypatch.setattr(config, "TOKEN", None)
        monkeypatch.setattr(config, "HOST", "127.0.0.1")
        monkeypatch.setattr(config, "DEBUG", False)
        resp, code = server._exception_response(RuntimeError("boom"))
        assert code == 500 and "traceback" in resp.get_json()
        monkeypatch.setattr(config, "TOKEN", TOKEN)
        resp, code = server._exception_response(RuntimeError("boom"))
        assert code == 500 and "traceback" not in resp.get_json() and resp.get_json()["detail"] == "RuntimeError: boom"
        monkeypatch.setattr(config, "DEBUG", True)
        resp, code = server._exception_response(RuntimeError("boom"))
        assert "traceback" in resp.get_json()


def test_login_form(monkeypatch):
    monkeypatch.setattr(config, "TOKEN", TOKEN)
    c = server.create_app().test_client()
    r = c.get("/login", headers={"Host": HOST})
    assert r.status_code == 200 and b"<form method='post' action='login'" in r.data and TOKEN.encode() not in r.data
    r = c.post("/login", data={"token": "wrong"}, headers={"Host": HOST, "Origin": f"http://{HOST}"})
    assert r.status_code == 401 and "Set-Cookie" not in r.headers
    r = c.post("/login", data={"token": TOKEN}, headers={"Host": HOST, "Origin": "http://evil.example"})
    assert r.status_code == 403 and "Set-Cookie" not in r.headers
    r = c.post("/login", data={"token": TOKEN}, headers={"Host": HOST, "Origin": f"http://{HOST}"})
    assert r.status_code == 303 and r.headers["Location"].endswith("/") and TOKEN not in r.headers["Location"]
    ck = r.headers["Set-Cookie"]
    assert "designer_token=" in ck and "HttpOnly" in ck and "SameSite=Strict" in ck
    assert c.get("/api/config", headers={"Host": HOST}).status_code == 200      # the cookie now authenticates
    monkeypatch.setattr(config, "TOKEN", None)
    assert server.create_app().test_client().get("/login", headers={"Host": HOST}).status_code == 303


def test_dev_server_log_redacts_token():
    H = server._redacting_request_handler()
    h = H.__new__(H)
    h.command, h.request_version = "GET", "HTTP/1.1"
    h.path = f"/?token={TOKEN}&x=1"
    h.requestline = f"GET /?token={TOKEN}&x=1 HTTP/1.1"
    lines = []
    h.log = lambda kind, msg, *args: lines.append(msg % args)
    h.log_request(303, "-")
    assert lines and TOKEN not in lines[0] and "token=***" in lines[0] and "x=1" in lines[0]


def test_git_output_scrubbed():
    msg = "fatal: unable to access 'https://git:olp_SECRET123@git.overleaf.com/abc/': Could not resolve host"
    out = gitops.scrub(msg)
    assert "olp_SECRET123" not in out and "https://***@git.overleaf.com/abc/" in out
    assert gitops.scrub("https://james@example.com/x") == "https://***@example.com/x"
    assert gitops.scrub("To https://git.overleaf.com/abc\n   a..b  main -> main") == \
        "To https://git.overleaf.com/abc\n   a..b  main -> main"
    r = gitops._result({"ok": False, "stdout": "", "stderr": msg, "error": msg})
    assert "olp_SECRET123" not in r["output"] and "olp_SECRET123" not in r["error"]


# ============================================================================================ seeds
def test_seeds_are_not_a_state_dir():
    """The default state dir is designer/, whose specs/ a running designer edits; seeds live in designer/seeds."""
    assert spec.SEED_DIR.resolve() == (config.DESIGNER_DIR / "seeds").resolve()
    assert spec.SEED_DIR.resolve() != (config.DESIGNER_DIR / "specs").resolve()
    assert spec.SEED_DIR.resolve() != Path(config.SPECS_DIR).resolve()
    s = yaml.safe_load((spec.SEED_DIR / "mug_ours_auxq2_levels_short.yaml").read_text(encoding="utf-8"))
    assert s["output"] == "figures/evals/mug_ours_auxq2_levels_short.pdf"
    assert s["style"]["width_pt"] == 252 and s["style"]["height_pt"] == 69 and s["style"]["legend_loc"] == "above"
