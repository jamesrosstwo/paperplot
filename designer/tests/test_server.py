"""Tests for designer/server.py, jobs.py, __main__.py, serve.py (contract section 5).

Run:  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe -m pytest designer/tests/test_server.py -q
  or  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe designer/tests/test_server.py

Uses Flask's test client. Backend modules that are not there yet are replaced by stubs injected into sys.modules
(the server imports them lazily through importlib); where a real module exists, a few real calls are made too.
Safety: DESIGNER_PAPER_ROOT is forced to the scratch clone (never the live Overleaf clone), DESIGNER_STATE_DIR to a
fresh temp dir. Nothing here commits, pulls or pushes in any repo. A real full compile of the scratch clone runs only
with DESIGNER_TEST_COMPILE=1.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
PAPERPLOT_ROOT = HERE.parent.parent
if str(PAPERPLOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PAPERPLOT_ROOT))

LIVE_CLONE = Path(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf").resolve()
SCRATCH = Path(r"C:\Users\James\AppData\Local\Temp\claude\9119a02420dddb37\7a66152e-6919-48a8-b3a9-c4911b85a0e4"
               r"\scratchpad\designer_test")
SCRATCH_PAPER = Path(os.environ.get("DESIGNER_TEST_PAPER", str(SCRATCH / "paper")))

if "designer.config" in sys.modules:
    _cfg = sys.modules["designer.config"]
    assert Path(_cfg.PAPER_ROOT).resolve() != LIVE_CLONE, "designer.config already points at the live clone"
else:
    os.environ["DESIGNER_PAPER_ROOT"] = str(SCRATCH_PAPER)
    _state_parent = SCRATCH if SCRATCH.is_dir() else None
    os.environ["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="state_server_test_",
                                                        dir=str(_state_parent) if _state_parent else None)
os.environ.setdefault("MPLBACKEND", "Agg")

from designer import config, jobs, server  # noqa: E402

assert config.PAPER_ROOT.resolve() != LIVE_CLONE, "refusing to test against the live Overleaf clone"

try:
    import pytest  # noqa: F401
except ImportError:  # plain runner
    pytest = None


class _Skip(Exception):
    pass


def skip(msg):
    if pytest is not None:
        pytest.skip(msg)
    raise _Skip(msg)


APP = server.create_app()
APP.testing = True
_MISSING = object()


def client():
    c = APP.test_client()
    c.environ_base["HTTP_X_DESIGNER"] = "1"   # what static/api.js sends on every request
    return c


@contextmanager
def stubbed(**mods):
    """Replace designer.<name> in sys.modules; a value of None makes the import fail (simulates a missing module)."""
    saved = {}
    for name, mod in mods.items():
        key = f"designer.{name}"
        saved[key] = sys.modules.get(key, _MISSING)
        sys.modules[key] = mod
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old


@contextmanager
def paper_root(path: Path):
    old = config.PAPER_ROOT
    config.PAPER_ROOT = Path(path)
    try:
        yield
    finally:
        config.PAPER_ROOT = old


def real(name):
    """The real designer.<name>, or None if it does not exist / fails to import."""
    try:
        import importlib
        return importlib.import_module(f"designer.{name}")
    except Exception:
        return None


def _ns(**fns):
    return types.SimpleNamespace(**fns)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FAKE_PNG = PNG_MAGIC + b"fake"
FAKE_PDF = b"%PDF-1.5\n%fake\n%%EOF\n"


def _snapshot(root: Path) -> dict:
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            p = os.path.join(dp, f)
            try:
                out[p] = os.stat(p).st_mtime_ns
            except OSError:
                pass
    return out


# ============================================================================================ fake backend modules
def fake_spec_module():
    store = {"seed": {"id": "seed", "kind": "grouped_bars", "title": "Seed", "output": "figures/evals/seed.pdf",
                      "data": {"source": "inline", "levels": ["A"], "arms": [{"name": "x", "color": "tol-blue",
                                                                              "counts": [[1, 2]]}]},
                      "style": {"theme": "acme", "width_pt": 252}}}
    calls = {"controls": []}

    def list_specs():
        return [{"id": s["id"], "title": s["title"], "kind": s["kind"], "output": s["output"]} for s in store.values()]

    def load(sid):
        if sid not in store:
            raise FileNotFoundError(f"no spec {sid!r}")
        return json.loads(json.dumps(store[sid]))

    def validate(spec):
        errs = []
        w = (spec.get("style") or {}).get("width_pt", 252)
        if not (120 <= w <= 516):
            errs.append(f"style.width_pt {w} out of range 120..516")
        return errs

    def save(spec):
        assert not validate(spec)
        store[spec["id"]] = json.loads(json.dumps(spec))

    def create(sid, template_id=None):
        if sid in store:
            raise FileExistsError(sid)
        base = load(template_id) if template_id else load("seed")
        base["id"] = sid
        store[sid] = base
        return load(sid)

    def delete(sid):
        if sid not in store:
            raise FileNotFoundError(sid)
        del store[sid]

    def controls(theme=None):
        calls["controls"].append(theme)
        return {"style": [{"key": "width_pt", "type": "slider", "min": 120, "max": 516, "group": "Size"}],
                "theme": theme}

    return _ns(list_specs=list_specs, load=load, validate=validate, save=save, create=create, delete=delete,
               controls=controls, defaults=lambda: {}, swatches=lambda t: {}, _store=store, _calls=calls)


def fake_render_module(tmp: Path):
    calls = {"preview": [], "render": []}

    def preview_png(spec, dpi=200):
        calls["preview"].append(dpi)
        return FAKE_PNG

    def render(spec, out_pdf=None):
        calls["render"].append(spec["id"])
        return {"pdf": tmp / "x.pdf", "png": tmp / "x.png", "width_pt": 252.0, "height_pt": 69.0,
                "warnings": [], "stats": [{"level": "A", "arm": "x", "k": 1, "n": 2, "rate": 0.5, "lo": 0.1, "hi": 0.9}]}

    return _ns(preview_png=preview_png, render=render, _calls=calls)


class StaleError(Exception):
    pass


def fake_latex_module(compile_delay=0.0, compile_raises=False):
    state = {"active": 0, "max_active": 0, "compiles": 0, "env": {}, "writes": []}
    lock = threading.Lock()
    FID = "sections/method.tex:fig:mug conditions"
    state["env"][FID] = {"text": "\\begin{figure}x\\end{figure}", "file": "sections/method.tex",
                         "begin_line": 3, "end_line": 9, "sha": "abc"}

    def compile_paper(root=None, timeout=240):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            time.sleep(compile_delay)
            if compile_raises:
                raise RuntimeError("latexmk exploded")
            state["compiles"] += 1
            return {"ok": True, "pdf": str(Path(root) / "paper.pdf"), "seconds": compile_delay, "errors": [],
                    "warnings_count": 0, "log_tail": ""}
        finally:
            with lock:
                state["active"] -= 1

    def locate_all(root=None):
        return {FID: {"page": 2, "x": 50.0, "y": 60.0, "w": 240.0, "h": 70.0}}

    def scan_figures(root=None):
        return [{"fid": FID, "file": "sections/method.tex", "begin_line": 3, "end_line": 9, "env": "figure",
                 "label": "fig:mug conditions", "caption": "c", "graphics": [], "spec_id": None},
                {"fid": "sections/x.tex:other", "file": "sections/x.tex", "begin_line": 1, "end_line": 2,
                 "env": "figure*", "label": None, "caption": "", "graphics": [], "spec_id": "seed"}]

    def read_env(fid, root=None):
        if fid not in state["env"]:
            raise KeyError(fid)
        return dict(state["env"][fid], fid=fid)

    def write_env(fid, text, expected_sha, root=None):
        cur = read_env(fid)
        if expected_sha != cur["sha"]:
            raise StaleError("file changed")
        state["env"][fid].update(text=text, sha="def")
        state["writes"].append((fid, text))
        return read_env(fid)

    def insert_figure(file_rel, after_line, graphics_rel, caption, label, env="figure", width="\\columnwidth",
                      root=None):
        state["inserted"] = (file_rel, after_line, graphics_rel, caption, label, env, width)
        return f"{file_rel}:{label}"

    def compile_context(fid, text_override=None, lines_before=12, lines_after=12, root=None):
        state["context"] = (fid, text_override)
        return {"ok": True, "pdf": str(config.CACHE_DIR / "context" / "ctx_abc.pdf"), "seconds": 0.1, "errors": []}

    def reverse(page, x, y, root=None):
        if page == 99:
            return None
        return {"file": "sections/method.tex", "line": 5}

    return _ns(compile_paper=compile_paper, locate_all=locate_all, scan_figures=scan_figures, read_env=read_env,
               write_env=write_env, insert_figure=insert_figure, compile_context=compile_context, reverse=reverse,
               StaleError=StaleError, _state=state, FID=FID)


def fake_gitops_module():
    calls = []

    def mk(name):
        def f(*a, **k):
            calls.append((name, a))
            return {"ok": True, "op": name}
        return f

    return _ns(status=lambda: {"ok": True, "branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0,
                               "dirty": [], "last_commit": None},
               fetch=mk("fetch"), pull=mk("pull"), push=mk("push"), commit=mk("commit"), diff_stat=mk("diff_stat"),
               _calls=calls)


# ============================================================================================ jobs.py
def test_jobs_runner_serial_and_states():
    r = jobs.JobRunner(workers=1)
    active, peak, order = [0], [0], []
    lk = threading.Lock()

    def work(i):
        with lk:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        order.append(i)
        with lk:
            active[0] -= 1
        return i * 10

    ids = [r.submit("compile", work, i) for i in range(4)]
    assert r.get(ids[-1])["status"] in ("queued", "running")
    for i, jid in enumerate(ids):
        j = r.wait(jid, timeout=5)
        assert j["status"] == "done" and j["result"] == i * 10, j
        assert j["elapsed"] > 0
    assert peak[0] == 1, "jobs overlapped"
    assert order == [0, 1, 2, 3]

    def boom():
        raise RuntimeError("nope")

    j = r.wait(r.submit("compile", boom), timeout=5)
    assert j["status"] == "error" and "RuntimeError: nope" in j["error"] and "Traceback" in j["detail"]
    assert r.get("nope") is None
    r.shutdown()


def test_jobs_dedupe_queued():
    r = jobs.JobRunner(workers=1)
    gate = threading.Event()
    first = r.submit("compile", gate.wait, 5)
    time.sleep(0.05)
    a = r.submit("compile", lambda: "a", dedupe_queued=True)
    b = r.submit("compile", lambda: "b", dedupe_queued=True)
    assert a == b, "a second queued compile should reuse the queued one"
    assert first != a
    gate.set()
    assert r.wait(a, timeout=5)["result"] == "a"
    r.shutdown()


# ============================================================================================ basics & errors
def test_config_endpoint():
    c = client()
    r = c.get("/api/config")
    assert r.status_code == 200
    d = r.get_json()
    for k in ("paper_root", "main_tex", "port", "has_pdf"):
        assert k in d, k
    assert Path(d["paper_root"]).resolve() == config.PAPER_ROOT.resolve()
    assert d["main_tex"] == config.MAIN_TEX
    assert isinstance(d["has_pdf"], bool)


def test_index_served():
    r = client().get("/")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_static_missing_is_json_404():
    r = client().get("/static/definitely_not_here.js")
    assert r.status_code == 404
    assert r.is_json and r.get_json()["error"]


def test_unknown_api_json_404_and_405():
    c = client()
    r = c.get("/api/nope")
    assert r.status_code == 404 and set(r.get_json()) >= {"error", "detail"}
    r = c.get("/api/git/push")       # push is POST-only
    assert r.status_code == 405 and r.is_json


def test_missing_module_is_503_and_others_still_work():
    with stubbed(spec=None):
        c = client()
        r = c.get("/api/specs")
        assert r.status_code == 503, r.data
        d = r.get_json()
        assert d["error"] == "module unavailable" and "designer.spec" in d["detail"]
        assert c.get("/api/config").status_code == 200
    with stubbed(latex=None):
        r = client().post("/api/compile")
        assert r.status_code == 503


def test_malformed_json_400():
    with stubbed(spec=fake_spec_module()):
        r = client().post("/api/specs", data="{not json", content_type="application/json")
        assert r.status_code == 400 and r.get_json()["error"] == "invalid JSON body"
        r = client().post("/api/specs", data="[1,2]", content_type="application/json")
        assert r.status_code == 400


def test_local_only_guards():
    c = client()
    r = c.get("/api/config", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    r = c.get("/api/config", headers={"Host": "127.0.0.1:8767"})
    assert r.status_code == 200
    r = c.get("/api/config", headers={"Host": "[::1]:8767"})
    assert r.status_code == 200
    g = fake_gitops_module()
    with stubbed(gitops=g):
        r = c.post("/api/git/push", headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 403
        r = c.post("/api/git/push", headers={"Origin": "null"})
        assert r.status_code == 403
        assert g._calls == [], "a cross-origin push must never reach gitops"
        # another localhost port is another origin (a dev server page must not be able to push)
        for o in ("http://localhost:5173", "http://127.0.0.1:8888", "https://127.0.0.1:8767"):
            r = c.post("/api/git/push", headers={"Origin": o, "Host": "127.0.0.1:8767"})
            assert r.status_code == 403, o
        # a "simple" cross-site-style request (text/plain, no X-Designer, no Origin) is refused too
        raw = APP.test_client()
        r = raw.post("/api/git/push", data="x", content_type="text/plain", headers={"Host": "127.0.0.1:8767"})
        assert r.status_code == 403
        assert g._calls == [], "a cross-origin push must never reach gitops"
        r = raw.post("/api/git/push", json={}, headers={"Host": "127.0.0.1:8767", "Origin": "http://127.0.0.1:8767"})
        assert r.status_code == 200 and g._calls == [("push", ())]
        r = c.post("/api/git/push", headers={"Origin": "http://127.0.0.1:8767", "Host": "127.0.0.1:8767"})
        assert r.status_code == 200 and g._calls == [("push", ()), ("push", ())]


# ============================================================================================ specs (stub)
def test_specs_crud_with_stub():
    sp = fake_spec_module()
    with stubbed(spec=sp):
        c = client()
        r = c.get("/api/specs")
        assert r.status_code == 200 and [s["id"] for s in r.get_json()] == ["seed"]
        r = c.post("/api/specs", json={"id": "new_one", "template_id": "seed"})
        assert r.status_code == 201 and r.get_json()["id"] == "new_one"
        assert c.post("/api/specs", json={"id": "new_one"}).status_code == 409
        assert c.post("/api/specs", json={"id": "Bad-Id"}).status_code == 400
        assert c.post("/api/specs", json={}).status_code == 400
        r = c.get("/api/specs/new_one")
        assert r.status_code == 200
        s = r.get_json()
        s["style"]["width_pt"] = 300
        r = c.put("/api/specs/new_one", json=s)
        assert r.status_code == 200 and r.get_json()["ok"] is True
        assert sp._store["new_one"]["style"]["width_pt"] == 300
        s["style"]["width_pt"] = 9999
        r = c.put("/api/specs/new_one", json=s)
        assert r.status_code == 400
        d = r.get_json()
        assert d["error"] == "invalid spec" and d["errors"] and "width_pt" in d["errors"][0] and d["detail"]
        assert sp._store["new_one"]["style"]["width_pt"] == 300, "invalid spec must not be saved"
        s["style"]["width_pt"] = 252
        s["id"] = "other"
        assert c.put("/api/specs/new_one", json=s).status_code == 400
        assert c.put("/api/specs/new_one", json={}).status_code == 400
        assert c.get("/api/specs/does_not_exist").status_code == 404
        assert c.get("/api/specs/..%2Fetc").status_code in (400, 404)
        assert c.delete("/api/specs/new_one").status_code == 200
        assert c.get("/api/specs/new_one").status_code == 404
        assert c.delete("/api/specs/new_one").status_code == 404


def test_controls_theme_passthrough():
    sp = fake_spec_module()
    with stubbed(spec=sp):
        c = client()
        assert c.get("/api/controls").get_json()["style"][0]["key"] == "width_pt"
        assert c.get("/api/controls?theme=acme").get_json()["theme"] == "acme"
        assert sp._calls["controls"] == [None, "acme"]


def test_preview_with_stub(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    sp, rd = fake_spec_module(), fake_render_module(tmp)
    with stubbed(spec=sp, render=rd):
        c = client()
        body = sp.load("seed")
        r = c.post("/api/preview", json=body)
        assert r.status_code == 200 and r.content_type == "image/png" and r.data == FAKE_PNG
        assert "no-store" in r.headers["Cache-Control"]
        r = c.post("/api/preview?dpi=100", json=body)
        assert rd._calls["preview"][-1] == 100
        body["style"]["width_pt"] = 5
        r = c.post("/api/preview", json=body)
        assert r.status_code == 400 and r.get_json()["errors"]
        assert c.post("/api/preview").status_code == 400


def test_render_with_stub_paths_serialised_and_optional_compile():
    tmp = Path(tempfile.mkdtemp())
    sp, rd, lx = fake_spec_module(), fake_render_module(tmp), fake_latex_module()
    with stubbed(spec=sp, render=rd, latex=lx), paper_root(tmp):
        c = client()
        r = c.post("/api/specs/seed/render")
        assert r.status_code == 200, r.data
        d = r.get_json()
        assert d["pdf"] == str(tmp / "x.pdf") and d["png"] == str(tmp / "x.png")
        assert d["stats"][0]["k"] == 1 and d["warnings"] == [] and "job" not in d
        # posting the spec saves it first, and ?compile=1 queues a compile job
        s = sp.load("seed")
        s["style"]["width_pt"] = 400
        r = c.post("/api/specs/seed/render?compile=1", json=s)
        d = r.get_json()
        assert r.status_code == 200 and d["job"]
        assert sp._store["seed"]["style"]["width_pt"] == 400
        j = jobs.runner.wait(d["job"], timeout=10)
        assert j["status"] == "done", j
        assert c.post("/api/specs/nope/render").status_code == 404


# ============================================================================================ compile & pdf (stub)
def test_compile_jobs_one_at_a_time():
    tmp = Path(tempfile.mkdtemp())
    lx = fake_latex_module(compile_delay=0.3)
    with stubbed(latex=lx), paper_root(tmp):
        c = client()
        r1 = c.post("/api/compile")
        assert r1.status_code == 202
        j1 = r1.get_json()["job"]
        time.sleep(0.05)
        j2 = c.post("/api/compile").get_json()["job"]
        j3 = c.post("/api/compile").get_json()["job"]
        assert j2 == j3, "queued compiles are deduplicated"
        for jid in (j1, j2):
            t0 = time.time()
            while True:
                d = c.get(f"/api/jobs/{jid}").get_json()
                assert d["status"] in jobs.STATUSES
                if d["status"] in ("done", "error"):
                    break
                assert time.time() - t0 < 10
                time.sleep(0.05)
            assert d["status"] == "done", d
            assert d["result"]["ok"] is True
            assert d["result"]["boxes"][lx.FID]["page"] == 2
        assert lx._state["max_active"] == 1, "two LaTeX jobs ran at once"
        assert lx._state["compiles"] == 2
        assert c.get("/api/jobs/doesnotexist").status_code == 404
        assert isinstance(c.get("/api/jobs").get_json(), list)


def test_compile_job_error_status():
    tmp = Path(tempfile.mkdtemp())
    with stubbed(latex=fake_latex_module(compile_raises=True)), paper_root(tmp):
        c = client()
        jid = c.post("/api/compile").get_json()["job"]
        d = jobs.runner.wait(jid, timeout=10)
        assert d["status"] == "error" and "latexmk exploded" in d["error"]
        assert c.get(f"/api/jobs/{jid}").get_json()["status"] == "error"


def test_paper_pdf_no_cache():
    tmp = Path(tempfile.mkdtemp())
    with paper_root(tmp):
        c = client()
        r = c.get("/paper.pdf")
        assert r.status_code == 404 and r.is_json
        (tmp / "paper.pdf").write_bytes(FAKE_PDF)
        r = c.get("/paper.pdf")
        assert r.status_code == 200 and r.data == FAKE_PDF and r.content_type == "application/pdf"
        cc = r.headers["Cache-Control"]
        assert "no-store" in cc and "no-cache" in cc and r.headers["Pragma"] == "no-cache"
        assert c.get("/api/config").get_json()["has_pdf"] is True


# ============================================================================================ figures / tex (stub)
def test_figures_merge_boxes():
    tmp = Path(tempfile.mkdtemp())
    lx = fake_latex_module()
    with stubbed(latex=lx), paper_root(tmp):
        c = client()
        d = c.get("/api/figures").get_json()
        assert d["has_pdf"] is False and all(f["box"] is None for f in d["figures"]), "no pdf -> no boxes"
        (tmp / "paper.pdf").write_bytes(FAKE_PDF)
        d = c.get("/api/figures").get_json()
        by = {f["fid"]: f for f in d["figures"]}
        assert by[lx.FID]["box"]["page"] == 2
        assert by["sections/x.tex:other"]["box"] is None
        assert by["sections/x.tex:other"]["spec_id"] == "seed"


def test_figure_tex_url_encoded_fid_and_stale():
    lx = fake_latex_module()
    fid = lx.FID                               # contains '/', ':' and a space
    with stubbed(latex=lx):
        c = client()
        enc = quote(fid, safe="")
        r = c.get(f"/api/figures/{enc}/tex")
        assert r.status_code == 200, r.data
        d = r.get_json()
        assert d["fid"] == fid and d["sha"] == "abc"
        r = c.get(f"/api/figures/{quote(enc, safe='')}/tex")      # double-encoded also resolves
        assert r.status_code == 200 and r.get_json()["fid"] == fid
        assert c.get(f"/api/figures/{quote('nope:x', safe='')}/tex").status_code == 404
        r = c.put(f"/api/figures/{enc}/tex", json={"text": "new", "sha": "WRONG"})
        assert r.status_code == 409
        d = r.get_json()
        assert d["error"] == "stale" and d["current"]["sha"] == "abc"
        assert lx._state["writes"] == []
        r = c.put(f"/api/figures/{enc}/tex", json={"text": "new", "sha": "abc"})
        assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["sha"] == "def"
        assert lx._state["writes"] == [(fid, "new")]
        assert c.put(f"/api/figures/{enc}/tex", json={"text": "x"}).status_code == 400
        assert c.put(f"/api/figures/{enc}/tex", json={"sha": "def"}).status_code == 400


def test_figure_insert():
    lx, sp = fake_latex_module(), fake_spec_module()
    with stubbed(latex=lx, spec=sp):
        c = client()
        r = c.post("/api/figures/insert", json={"file": "sections/results.tex", "after_line": 12,
                                                 "graphics": "figures/evals/seed.pdf", "caption": "Cap",
                                                 "label": "fig:seed"})
        assert r.status_code == 201 and r.get_json()["fid"] == "sections/results.tex:fig:seed"
        assert lx._state["inserted"][:5] == ("sections/results.tex", 12, "figures/evals/seed.pdf", "Cap", "fig:seed")
        r = c.post("/api/figures/insert", json={"file_rel": "sections/results.tex", "after_line": "3",
                                                 "spec_id": "seed", "label": "fig:s2", "env": "figure*",
                                                 "width": "\\textwidth"})
        assert r.status_code == 201
        assert lx._state["inserted"] == ("sections/results.tex", 3, "figures/evals/seed.pdf", "", "fig:s2",
                                         "figure*", "\\textwidth")
        assert c.post("/api/figures/insert", json={"file": "a.tex"}).status_code == 400


def test_context_compile_and_pdf():
    lx = fake_latex_module()
    ctx = config.CACHE_DIR / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "ctx_abc.pdf").write_bytes(FAKE_PDF)
    with stubbed(latex=lx):
        c = client()
        r = c.post(f"/api/context/{quote(lx.FID, safe='')}", json={"text": "\\begin{figure}y\\end{figure}"})
        assert r.status_code == 200, r.data
        d = r.get_json()
        assert d["pdf_url"] == "context/ctx_abc.pdf" and d["ok"] is True     # relative to the app's mount point
        assert lx._state["context"] == (lx.FID, "\\begin{figure}y\\end{figure}")
        r = c.post(f"/api/context/{quote(lx.FID, safe='')}")
        assert r.status_code == 200 and lx._state["context"] == (lx.FID, None)
        r = c.get("/" + d["pdf_url"])
        assert r.status_code == 200 and r.data == FAKE_PDF and "no-store" in r.headers["Cache-Control"]
        assert c.get("/context/missing.pdf").status_code == 404
        assert c.get("/context/..%2F..%2Fpaper.pdf").status_code in (400, 404)


def test_synctex_reverse():
    with stubbed(latex=fake_latex_module()):
        c = client()
        r = c.post("/api/synctex/reverse", json={"page": 2, "x": 100.5, "y": 200})
        assert r.get_json() == {"file": "sections/method.tex", "line": 5}
        r = c.post("/api/synctex/reverse", json={"page": 99, "x": 1, "y": 1})
        assert r.status_code == 200 and r.get_json() == {"file": None, "line": None}
        assert c.post("/api/synctex/reverse", json={"page": "x"}).status_code == 400


# ============================================================================================ git (stub)
def test_git_endpoints_with_stub():
    g = fake_gitops_module()
    with stubbed(gitops=g):
        c = client()
        assert c.get("/api/git/status").get_json()["branch"] == "main"
        assert c.post("/api/git/fetch").get_json()["op"] == "fetch"
        assert c.post("/api/git/pull").get_json()["op"] == "pull"
        assert c.post("/api/git/commit", json={"paths": [], "message": "m"}).status_code == 400
        assert c.post("/api/git/commit", json={"paths": ["a.tex"], "message": "  "}).status_code == 400
        assert c.post("/api/git/commit", json={"paths": "a.tex", "message": "m"}).status_code == 400
        assert not any(n == "commit" for n, _ in g._calls)
        r = c.post("/api/git/commit", json={"paths": ["figures/evals/x.pdf", "sections/r.tex"], "message": "fig"})
        assert r.status_code == 200
        assert ("commit", (["figures/evals/x.pdf", "sections/r.tex"], "fig")) in g._calls
        assert not any(n == "push" for n, _ in g._calls), "nothing pushes unless /api/git/push is POSTed"
        assert c.post("/api/git/push").get_json()["op"] == "push"


# ============================================================================================ real modules
def _first_loadable_spec(c, specs):
    """(id, spec) of the first spec that loads; skip if none does (seed specs may still be in progress)."""
    problems, loaded = [], []
    for item in specs:
        r = c.get(f"/api/specs/{item['id']}")
        if r.status_code == 200:
            loaded.append((item["id"], r.get_json()))
        else:
            problems.append(f"{item['id']}: {r.status_code} {r.get_json().get('detail', '')[:120]}")
    # inline specs first: a dataset-backed one needs its dataset registered in this (fresh) state dir
    # ...and the reference grouped-bars seed first: other seeds (line plots, panels) lay out differently, and the
    # checks below (e.g. a 12 pt legend overflowing) are about that seed
    loaded.sort(key=lambda t: ((t[1].get("data") or {}).get("source", "inline") != "inline",
                               t[0] != "mug_ours_auxq2_levels_short"))
    if loaded:
        return loaded[0]
    skip("no loadable spec yet: " + "; ".join(problems))


def test_real_spec_and_render_if_available():
    sp, rd = real("spec"), real("render")
    if sp is None:
        skip("designer.spec not available yet")
    c = client()
    r = c.get("/api/specs")
    assert r.status_code == 200, r.data
    specs = r.get_json()
    assert isinstance(specs, list)
    r = c.get("/api/controls")
    assert r.status_code == 200 and isinstance(r.get_json()["style"], list), r.data
    sid, s = _first_loadable_spec(c, specs)
    assert s["id"] == sid
    # create a copy in the scratch state dir, edit it, save it, delete it
    cid = "server_test_copy"
    c.delete(f"/api/specs/{cid}")
    r = c.post("/api/specs", json={"id": cid, "template_id": sid})
    assert r.status_code == 201, r.data
    cp = r.get_json()
    cp["style"]["width_pt"] = 300
    r = c.put(f"/api/specs/{cid}", json=cp)
    assert r.status_code == 200, r.data
    assert c.get(f"/api/specs/{cid}").get_json()["style"]["width_pt"] == 300
    cp["style"]["width_pt"] = 99999
    r = c.put(f"/api/specs/{cid}", json=cp)
    assert r.status_code == 400 and r.get_json()["errors"], r.data
    if rd is not None:
        before = _snapshot(config.PAPER_ROOT)
        t0 = time.time()
        r = c.post("/api/preview", json=s)
        dt = time.time() - t0
        assert r.status_code == 200, r.data[:500]
        assert r.data[:8] == PNG_MAGIC
        assert _snapshot(config.PAPER_ROOT) == before, "preview wrote into the paper repo"
        print(f"  real preview: {len(r.data)} bytes in {dt:.2f}s")
        import json as _json
        from urllib.parse import unquote as _unq
        assert isinstance(_json.loads(_unq(r.headers["X-Render-Warnings"])), list)
        big = dict(s, style=dict(s.get("style") or {}, legend_fontsize=12))
        r = c.post("/api/preview", json=big)
        assert r.status_code == 200
        assert any("legend" in w for w in _json.loads(_unq(r.headers["X-Render-Warnings"])))
    assert c.delete(f"/api/specs/{cid}").status_code == 200


def test_real_render_into_temp_root_if_available():
    sp, rd = real("spec"), real("render")
    if sp is None or rd is None:
        skip("designer.spec/render not available yet")
    specs = client().get("/api/specs").get_json()
    sid, _ = _first_loadable_spec(client(), specs)
    tmp = Path(tempfile.mkdtemp(prefix="render_root_"))
    with paper_root(tmp):
        r = client().post(f"/api/specs/{sid}/render")
        assert r.status_code == 200, r.data[:1000]
        d = r.get_json()
        assert Path(d["pdf"]).exists() and Path(d["png"]).exists(), d
        if str(tmp) not in str(Path(d["pdf"]).resolve()):
            print("  note: render.render did not honour config.PAPER_ROOT at call time:", d["pdf"])
        assert isinstance(d["stats"], list)


def test_real_datastore_if_available():
    ds = real("datastore")
    if ds is None:
        skip("designer.datastore not available yet")
    c = client()
    target_dir = Path(tempfile.mkdtemp(prefix="ds_target_"))
    f = target_dir / "table.tsv"
    f.write_text("level\tarm\tk\tn\nEasy\tours\t3\t5\n", encoding="utf-8")
    name = "server_test_table"
    c.delete(f"/api/data/{name}")
    r = c.post("/api/data", json={"name": name, "ref": str(f), "notes": "t"})
    assert r.status_code == 201, r.data
    e = r.get_json()
    assert Path(e["path"]).resolve() == f.resolve()
    assert any(x["name"] == name for x in c.get("/api/data").get_json())
    assert c.get(f"/api/data/{name}").get_json()["name"] == name
    pv = c.get(f"/api/data/{name}/preview?rows=5").get_json()
    assert "arm" in pv.get("columns", []), pv
    assert c.post("/api/data", json={"name": name, "ref": str(f)}).status_code in (400, 409)
    assert c.post("/api/data", json={"name": "x_missing", "ref": str(target_dir / "nope.tsv")}).status_code in (400, 404)
    assert c.post("/api/data", json={"name": "x"}).status_code == 400
    r = c.delete(f"/api/data/{name}")
    assert r.status_code == 200, r.data
    assert f.exists(), "removing a dataset must never delete its target"
    assert c.get(f"/api/data/{name}").status_code == 404
    assert c.get("/api/data/bad%20name").status_code == 400
    # a file:// URL also works
    r = c.post("/api/data", json={"name": name, "ref": f.resolve().as_uri(), "make_link": False})
    assert r.status_code == 201 and r.get_json()["link"] is None, r.data
    c.delete(f"/api/data/{name}")
    assert f.exists()


def test_real_gitops_status_if_available():
    g = real("gitops")
    if g is None:
        skip("designer.gitops not available yet")
    r = client().get("/api/git/status")
    assert r.status_code == 200
    d = r.get_json()
    assert "branch" in d and "dirty" in d, d


def test_real_latex_figures_if_available():
    lx = real("latex")
    if lx is None:
        skip("designer.latex not available yet")
    r = client().get("/api/figures")
    assert r.status_code == 200, r.data[:1000]
    figs = r.get_json()["figures"]
    assert figs and all("fid" in f and "box" in f for f in figs)
    fid = figs[0]["fid"]
    r = client().get(f"/api/figures/{quote(fid, safe='')}/tex")
    assert r.status_code == 200, r.data[:1000]
    assert r.get_json().get("sha")


def test_real_compile_if_requested():
    if os.environ.get("DESIGNER_TEST_COMPILE") != "1":
        skip("set DESIGNER_TEST_COMPILE=1 for a real full compile of the scratch clone")
    if real("latex") is None:
        skip("designer.latex not available yet")
    c = client()
    jid = c.post("/api/compile").get_json()["job"]
    d = jobs.runner.wait(jid, timeout=600)
    assert d["status"] == "done", d
    assert d["result"]["ok"], d["result"].get("errors")
    print(f"  real compile: {d['elapsed']:.1f}s, {len(d['result']['boxes'])} figure boxes")
    r = c.get("/paper.pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"


# ============================================================================================ CLI entry points
def _py(args, cwd, env_extra=None):
    env = dict(os.environ)
    for k in ("DESIGNER_PAPER_ROOT", "DESIGNER_STATE_DIR", "DESIGNER_PORT"):
        env.pop(k, None)
    env["PYTHONUTF8"] = "1"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def test_cli_flags_applied_before_config_import():
    state = tempfile.mkdtemp(prefix="cli_state_")
    code = ("import sys; from designer.__main__ import parse_args, apply_env\n"
            "assert 'designer.config' not in sys.modules\n"
            f"apply_env(parse_args(['--paper-root', r'{SCRATCH_PAPER}', '--state-dir', r'{state}', '--port', '8799']))\n"
            "from designer import config\n"
            "print(config.PAPER_ROOT); print(config.STATE_DIR); print(config.PORT)\n")
    r = _py(["-c", code], cwd=str(PAPERPLOT_ROOT))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert Path(lines[0]).resolve() == SCRATCH_PAPER.resolve()
    assert Path(lines[1]).resolve() == Path(state).resolve()
    assert lines[2] == "8799"


def test_serve_py_from_other_cwd():
    other = tempfile.mkdtemp(prefix="cwd_")
    r = _py([str(PAPERPLOT_ROOT / "designer" / "serve.py"), "--help"], cwd=other)
    assert r.returncode == 0, r.stderr
    assert "--paper-root" in r.stdout and "--state-dir" in r.stdout and "--port" in r.stdout
    r = _py(["-m", "designer", "--help"], cwd=str(PAPERPLOT_ROOT))
    assert r.returncode == 0 and "--paper-root" in r.stdout, r.stderr


def test_server_binds_localhost_by_default():
    r = _py(["-c", "from designer import config; print(config.HOST)"], cwd=str(PAPERPLOT_ROOT),
            env_extra={"DESIGNER_HOST": "", "DESIGNER_PAPER_ROOT": str(SCRATCH_PAPER),
                       "DESIGNER_STATE_DIR": tempfile.mkdtemp(prefix="host_state_")})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "127.0.0.1"


# ============================================================================================ plain runner
if __name__ == "__main__":
    pytest = None          # plain runner: skips raise _Skip, not pytest's exception
    tests = [(n, f) for n, f in sorted(globals().items(), key=lambda kv: getattr(kv[1], "__code__", None)
                                       and kv[1].__code__.co_firstlineno or 0)
             if n.startswith("test_") and callable(f)]
    failed = skipped = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except _Skip as e:
            skipped += 1
            print(f"SKIP {n}: {e}")
        except Exception:
            failed += 1
            import traceback
            print(f"FAIL {n}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
    sys.exit(1 if failed else 0)
