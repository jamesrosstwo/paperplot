"""Tests for running the designer as a network service (CONTRACT section 7): token auth, bind refusal, read-only mode,
base path / reverse-proxy prefix, strict fonts (no silent fallback), atomic saves, file locks, the compile queue and
the cross-platform config.

Run:  PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe -m pytest designer/tests/test_deploy.py -q

conftest.py points DESIGNER_PAPER_ROOT at the scratch clone and DESIGNER_STATE_DIR at a fresh temp dir. Nothing here
compiles the paper, commits, pulls or pushes; servers started here are refused before they bind.
"""
from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
PAPERPLOT_ROOT = HERE.parent.parent
if str(PAPERPLOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PAPERPLOT_ROOT))

from designer import config, jobs, locks, server  # noqa: E402

LIVE_CLONE = Path(config.LIVE_PAPER_WINDOWS).resolve()   # the Windows default, checked below
from designer.live_papers import is_live  # noqa: E402
assert not is_live(config.PAPER_ROOT), "refusing to test against the live Overleaf clone"

TOKEN = "test-token-" + "k3Jx9q"          # a throwaway value for these tests only
HOST = "127.0.0.1:8768"
SEED = PAPERPLOT_ROOT / "designer" / "seeds" / "mug_ours_auxq2_levels_short.yaml"


@pytest.fixture()
def app():
    a = server.create_app()
    a.testing = True
    return a


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(config, "TOKEN", TOKEN)
    return TOKEN


def _get(c, url, **kw):
    headers = {"Host": HOST, **kw.pop("headers", {})}
    return c.get(url, headers=headers, **kw)


# ============================================================================================ auth
def test_no_token_401_on_api_and_pdfs(app, token):
    c = app.test_client()
    for url in ("/api/config", "/api/specs", "/api/git/status", "/paper.pdf", "/context/x.pdf", "/api/fonts/check",
                "/api/no-such-route"):
        r = _get(c, url)
        assert r.status_code == 401, (url, r.status_code)
        assert r.get_json()["error"] == "unauthorized"
        assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")
    r = c.post("/api/git/push", json={}, headers={"Host": HOST})
    assert r.status_code == 401


def test_public_routes_without_token(app, token):
    c = app.test_client()
    assert _get(c, "/").status_code == 200
    assert _get(c, "/static/app.js").status_code == 200
    r = _get(c, "/healthz")
    assert r.status_code == 200
    d = r.get_json()
    assert set(d) == {"ok", "version", "paper_root_exists", "latex_found"}
    assert d["ok"] is True and d["version"] == config.VERSION
    assert isinstance(d["paper_root_exists"], bool) and isinstance(d["latex_found"], bool)
    body = r.get_data(as_text=True)
    for secret in (str(config.PAPER_ROOT), config.PAPER_ROOT.as_posix(), str(config.STATE_DIR), TOKEN):
        assert secret not in body


def test_bearer_token(app, token):
    c = app.test_client()
    r = _get(c, "/api/config", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.get_json()["auth"] is True
    for bad in (f"Bearer {TOKEN}x", "Bearer ", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN[:-1]}"):
        assert _get(c, "/api/config", headers={"Authorization": bad}).status_code == 401, bad


def test_cookie_login_flow(app, token):
    c = app.test_client()
    r = _get(c, f"/?token={TOKEN}&x=1")
    assert r.status_code in (302, 303)
    loc = r.headers["Location"]
    assert TOKEN not in loc and "token" not in loc and loc.endswith("/?x=1")
    cookie = r.headers.get("Set-Cookie", "")
    assert cookie.startswith("designer_token=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie                      # plain http here; https gets Secure
    assert _get(c, "/api/config").status_code == 200  # the test client keeps the cookie
    r = c.get("/?token=" + TOKEN, headers={"Host": HOST, "X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers.get("Set-Cookie", "")


def test_wrong_token_and_wrong_cookie(app, token):
    c = app.test_client()
    r = _get(c, "/?token=nope")
    assert r.status_code == 401 and "Set-Cookie" not in r.headers
    c.set_cookie("designer_token", "nope", domain="127.0.0.1")
    assert _get(c, "/api/config").status_code == 401


def test_token_compared_with_compare_digest(app, token, monkeypatch):
    calls = []
    real = server.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(server.hmac, "compare_digest", spy)
    c = app.test_client()
    assert _get(c, "/api/config", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert _get(c, "/api/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert len(calls) >= 2 and all(t == (bytes, bytes) for t in calls)


def test_token_mode_allows_other_hosts_and_checks_origin(app, token):
    """Behind a container port mapping / tailnet name the Host is not loopback; Origin must match that Host."""
    c = app.test_client()
    auth = {"Authorization": f"Bearer {TOKEN}", "X-Designer": "1"}
    h = "borabora.tail1234.ts.net:8797"
    assert c.get("/api/config", headers={"Host": h, **auth}).status_code == 200
    # a same-origin write passes the guard (the readonly 403 proves we got past auth + origin)
    config_ro = config.READONLY
    try:
        config.READONLY = True
        r = c.post("/api/git/push", json={}, headers={"Host": h, "Origin": f"http://{h}", **auth})
        assert r.status_code == 403 and r.get_json()["error"] == "read-only"
        r = c.post("/api/git/push", json={}, headers={"Host": h, "Origin": "http://evil.example", **auth})
        assert r.status_code == 403 and r.get_json()["error"] == "forbidden origin"
        # reverse proxy: Host is the upstream address, the browser's origin is in X-Forwarded-Host / -Proto
        r = c.post("/api/git/push", json={}, headers={"Host": "127.0.0.1:8797", "X-Forwarded-Host": "designer.example",
                                                      "X-Forwarded-Proto": "https",
                                                      "Origin": "https://designer.example", **auth})
        assert r.get_json()["error"] == "read-only"
        r = c.post("/api/git/push", json={}, headers={"Host": "127.0.0.1:8797", "Origin": "https://designer.example",
                                                      **auth})
        assert r.get_json()["error"] == "forbidden origin"
        # (test_waitress_keeps_forwarded_headers checks the same through the real waitress server)
        # default ports are normalised: Origin https://x == Host x:443 over https
        r = c.post("/api/git/push", json={}, headers={"Host": "x.example:443", "X-Forwarded-Proto": "https",
                                                      "Origin": "https://x.example", **auth})
        assert r.get_json()["error"] == "read-only"
    finally:
        config.READONLY = config_ro


def test_tokenless_mode_stays_local_only(app, monkeypatch):
    monkeypatch.setattr(config, "TOKEN", None)
    c = app.test_client()
    assert c.get("/api/config", headers={"Host": "borabora:8797"}).status_code == 403
    assert c.get("/api/config", headers={"Host": HOST}).status_code == 200


# ============================================================================================ bind refusal
def test_check_bind_security():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
        server.check_bind_security(host, None)
    server.check_bind_security("0.0.0.0", "t")
    for host in ("0.0.0.0", "::", "100.64.0.5", "borabora"):
        with pytest.raises(SystemExit) as ei:
            server.check_bind_security(host, None)
        assert "token" in str(ei.value).lower()
        with pytest.raises(SystemExit):
            server.check_bind_security(host, "   ")


def _py(args, env_extra=None, timeout=60):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("DESIGNER_"):
            env.pop(k)
    env["PYTHONUTF8"] = "1"
    env["DESIGNER_PAPER_ROOT"] = os.environ["DESIGNER_PAPER_ROOT"]
    env["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="deploy_state_")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, *args], cwd=tempfile.gettempdir(), env=env, capture_output=True,
                          text=True, timeout=timeout)


def test_non_loopback_without_token_refuses_to_start():
    serve = str(PAPERPLOT_ROOT / "designer" / "serve.py")
    r = _py([serve, "--host", "0.0.0.0", "--port", "8769"])
    assert r.returncode != 0
    assert "refusing" in (r.stderr + r.stdout) and "token" in (r.stderr + r.stdout).lower()
    r = _py([serve, "--host", "0.0.0.0", "--port", "8769"], {"DESIGNER_TOKEN": ""})
    assert r.returncode != 0


def test_non_loopback_dev_server_refused_without_waitress():
    try:
        import waitress  # noqa: F401
        pytest.skip("waitress installed: --prod would really serve")
    except ImportError:
        pass
    serve = str(PAPERPLOT_ROOT / "designer" / "serve.py")
    r = _py([serve, "--host", "0.0.0.0", "--port", "8769", "--prod"], {"DESIGNER_TOKEN": TOKEN})
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "waitress" in out and "development server" in out
    assert TOKEN not in out, "the token must never be printed"


def test_waitress_keeps_forwarded_headers(token, monkeypatch):
    """Behind a reverse proxy that rewrites Host, same-origin writes must pass the Origin check under waitress too.
    waitress drops X-Forwarded-Host/-Proto by default (clear_untrusted_proxy_headers), which made every POST from the
    browser a 403 'forbidden origin' in the Docker image (found in the borabora E2E run)."""
    waitress = pytest.importorskip("waitress")
    import http.client
    monkeypatch.setattr(config, "READONLY", True)       # a write that gets past auth + origin answers 'read-only'
    srv = waitress.create_server(server.create_app(), host="127.0.0.1", port=0, **server.WAITRESS_OPTIONS)
    port = srv.effective_port
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        def post(headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/git/push", body=b"{}",
                      headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                               "X-Designer": "1", **headers})
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            return r.status, body
        status, body = post({"X-Forwarded-Host": "designer.example", "X-Forwarded-Proto": "https",
                             "Origin": "https://designer.example"})
        assert status == 403 and "read-only" in body, body
        status, body = post({"Origin": "https://evil.example"})
        assert status == 403 and "forbidden origin" in body, body
    finally:
        srv.close()


def test_wsgi_requires_token():
    r = _py(["-c", "import designer.wsgi"], {"PYTHONPATH": str(PAPERPLOT_ROOT)})
    assert r.returncode != 0 and "DESIGNER_TOKEN" in r.stderr
    r = _py(["-c", "import designer.wsgi as w; print(type(w.app).__name__)"],
            {"PYTHONPATH": str(PAPERPLOT_ROOT), "DESIGNER_TOKEN": TOKEN, "DESIGNER_HOST": "0.0.0.0"})
    assert r.returncode == 0 and r.stdout.strip() == "Flask", r.stderr
    r = _py(["-c", "import designer.wsgi"], {"PYTHONPATH": str(PAPERPLOT_ROOT), "DESIGNER_HOST": "127.0.0.1"})
    assert r.returncode == 0, r.stderr


def test_cli_flags_set_env():
    code = ("import os, sys; from designer.__main__ import parse_args, apply_env\n"
            "apply_env(parse_args(['--host','0.0.0.0','--base-path','/designer/','--token','abc','--readonly',"
            "'--latex-bin','.']))\n"
            "from designer import config\n"
            "print(config.HOST, config.BASE_PATH, config.TOKEN == 'abc', config.READONLY, config.LATEX_BIN is not None)")
    r = _py(["-c", code], {"PYTHONPATH": str(PAPERPLOT_ROOT)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0.0.0.0", "/designer", "True", "True", "True"]


# ============================================================================================ read-only
def _mutating_rules(app):
    out = []
    for rule in app.url_map.iter_rules():
        for m in sorted((rule.methods or set()) & {"POST", "PUT", "DELETE", "PATCH"}):
            out.append((rule, m))
    return out


def test_readonly_403_on_every_mutating_route(app, monkeypatch):
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "TOKEN", None)
    adapter = app.url_map.bind("127.0.0.1:8768")
    c = app.test_client()
    blocked, allowed = set(), set()
    for rule, method in _mutating_rules(app):
        url = adapter.build(rule.endpoint, {a: "x" for a in rule.arguments}, method=method)
        r = c.open(url, method=method, json={}, headers={"Host": HOST})
        is_ro = r.status_code == 403 and (r.get_json() or {}).get("error") == "read-only"
        if rule.endpoint in server.READONLY_ALLOWED:
            assert not is_ro, f"{method} {rule.rule} must stay usable read-only"
            allowed.add(rule.endpoint)
        else:
            assert is_ro, f"{method} {rule.rule} -> {r.status_code} {r.data[:200]!r}"
            assert "read-only" in r.get_json()["detail"]
            blocked.add(rule.endpoint)
    must_block = {"specs_create", "specs_put", "specs_delete", "specs_render", "figures_insert", "figure_tex_put",
                  "data_add", "data_delete", "data_refresh", "git_fetch", "git_pull", "git_commit", "git_push"}
    assert must_block <= blocked, must_block - blocked
    assert allowed == server.READONLY_ALLOWED
    r = c.get("/api/config", headers={"Host": HOST})
    assert r.status_code == 200 and r.get_json()["readonly"] is True


# ============================================================================================ base path
_ABS_APP_URL = re.compile(r"""(?:href|src)\s*=\s*["']/(?!designer/|proxy/)""")


def test_base_path_routes(app, monkeypatch):
    monkeypatch.setattr(config, "BASE_PATH", "/designer")
    monkeypatch.setattr(config, "TOKEN", None)
    c = app.test_client()
    r = _get(c, "/designer/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert '<base href="/designer/">' in html
    assert not _ABS_APP_URL.search(html), "index.html must not use root-absolute app URLs"
    assert not re.search(r"""["'`]/(api|static|paper\.pdf|context)\b""", html)
    assert _get(c, "/designer").status_code == 200             # no trailing slash: <base> still makes URLs right
    r = _get(c, "/designer/api/config")
    assert r.status_code == 200 and r.get_json()["base_path"] == "/designer"
    assert _get(c, "/designer/static/app.js").status_code == 200
    assert _get(c, "/designer/api/specs").status_code == 200
    assert _get(c, "/designer/healthz").status_code == 200
    # a proxy that already stripped the prefix still works
    assert _get(c, "/api/specs").status_code == 200


def test_base_path_cookie_and_redirect(app, monkeypatch, token):
    monkeypatch.setattr(config, "BASE_PATH", "/designer")
    c = app.test_client()
    r = _get(c, f"/designer/?token={TOKEN}")
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/designer/")
    assert "Path=/designer/" in r.headers["Set-Cookie"]
    assert _get(c, "/designer/api/config").status_code == 200


def test_x_forwarded_prefix(app, monkeypatch):
    monkeypatch.setattr(config, "BASE_PATH", "")
    monkeypatch.setattr(config, "TOKEN", None)
    c = app.test_client()
    r = _get(c, "/", headers={"X-Forwarded-Prefix": "/proxy/designer"})
    assert '<base href="/proxy/designer/">' in r.get_data(as_text=True)
    r = _get(c, "/api/config", headers={"X-Forwarded-Prefix": "/proxy/designer"})
    assert r.get_json()["base_path"] == "/proxy/designer"
    for bad in ('/x"><script>', "javascript:alert(1)", "//evil.example"):
        r = _get(c, "/", headers={"X-Forwarded-Prefix": bad})
        assert '<base href="/">' in r.get_data(as_text=True), bad
    r = _get(c, "/")
    assert '<base href="/">' in r.get_data(as_text=True)


def test_normalize_base_path():
    n = config.normalize_base_path
    assert n("") == "" and n("/") == "" and n(None) == ""
    assert n("designer") == "/designer" and n("/designer/") == "/designer" and n("/a/b") == "/a/b"
    with pytest.raises(config.ConfigError):
        n('/x"y')


def test_index_has_no_absolute_urls():
    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not _ABS_APP_URL.search(html)
    assert 'src="static/app.js"' in html and 'href="static/styles.css"' in html


# ============================================================================================ fonts
def _seed():
    return yaml.safe_load(SEED.read_text(encoding="utf-8"))


def test_missing_requested_font_is_an_error_not_a_fallback(app, monkeypatch):
    from designer import render
    s = _seed()
    s["style"]["font_family"] = "No Such Font Qx7"
    with pytest.raises(render.FontUnavailable) as ei:
        render.preview_png(s, dpi=50)
    assert "No Such Font Qx7" in str(ei.value) and "style.font_family" in str(ei.value)
    out = Path(tempfile.mkdtemp(prefix="font_render_")) / "x.pdf"
    with pytest.raises(render.FontUnavailable):
        render.render(s, out_pdf=out)
    assert not out.exists(), "nothing may be written when the font is missing"
    monkeypatch.setattr(config, "TOKEN", None)
    r = app.test_client().post("/api/preview", json=s, headers={"Host": HOST, "X-Designer": "1"})
    assert r.status_code == 422
    d = r.get_json()
    assert d["error"] == "font unavailable" and "No Such Font Qx7" in d["detail"]


def test_missing_theme_primary_font_is_an_error(monkeypatch):
    from designer import render
    real = render._theme.load

    def load(name):
        t = real(name)
        t.font = dict(t.font)
        t.font["serif"] = ["Missing Theme Font Zq9", "DejaVu Serif"]   # DejaVu Serif would be the silent fallback
        return t

    monkeypatch.setattr(render._theme, "load", load)
    s = _seed()
    s["style"]["font_family"] = None
    with pytest.raises(render.FontUnavailable) as ei:
        render.preview_png(s, dpi=50)
    assert "Missing Theme Font Zq9" in str(ei.value) and "first font family of theme" in str(ei.value)
    chk = render.check_fonts()
    assert chk["ok"] is False and all(not r["ok"] for r in chk["themes"] if r["family"] == "Missing Theme Font Zq9")


def test_theme_fonts_resolve_and_seed_renders_with_math_fallback():
    from designer import render
    chk = render.check_fonts()
    rows = {r["theme"]: r for r in chk["themes"]}
    assert rows["acme"]["ok"] and rows["acme"]["family"] == "Roboto Slab"
    assert rows["acme"]["file"].lower().startswith("roboto")
    png = render.preview_png(_seed(), dpi=60)           # arm names use $\mathrm{\pi}$: STIX math fallback, no error
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_fonts_check_endpoint(app, token):
    c = app.test_client()
    assert _get(c, "/api/fonts/check").status_code == 401
    r = _get(c, "/api/fonts/check", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    d = r.get_json()
    assert {"ok", "themes"} <= set(d) and any(t["theme"] == "acme" for t in d["themes"])


# ============================================================================================ atomic writes, locks
def test_spec_save_is_atomic(monkeypatch):
    from designer import spec
    s = spec.load("mug_ours_auxq2_levels_short")
    s = copy.deepcopy(s)
    s["id"] = "atomic_save_test"
    spec.save(s)
    p = config.SPECS_DIR / "atomic_save_test.yaml"
    before = p.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(locks.os, "replace", boom)
    s2 = copy.deepcopy(s)
    s2["title"] = "changed"
    with pytest.raises(OSError):
        spec.save(s2)
    monkeypatch.undo()
    assert p.read_text(encoding="utf-8") == before, "a failed save must leave the old file intact"
    leftovers = [f.name for f in config.SPECS_DIR.iterdir() if f.name.startswith(".atomic_save_test")]
    assert not leftovers, leftovers
    # concurrent saves: the file is always one complete spec
    errs = []

    def writer(i):
        try:
            t = copy.deepcopy(s)
            t["title"] = f"t{i}"
            spec.save(t)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ths = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert not errs, errs
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["title"].startswith("t")
    spec.delete("atomic_save_test")


def test_file_lock_excludes():
    target = Path(tempfile.mkdtemp(prefix="lock_")) / "registry.json"
    held = threading.Event()
    release = threading.Event()

    def holder():
        with locks.file_lock(target):
            held.set()
            release.wait(5)

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(5)
    t0 = time.monotonic()
    with pytest.raises(locks.LockTimeout):
        with locks.file_lock(target, timeout=0.3):
            pass
    assert time.monotonic() - t0 >= 0.25
    release.set()
    th.join()
    with locks.file_lock(target, timeout=1):
        pass


def test_file_lock_across_processes():
    target = Path(tempfile.mkdtemp(prefix="lockp_")) / "registry.json"
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from designer.locks import file_lock\n"
            "with file_lock(sys.argv[2]):\n    print('held', flush=True); time.sleep(3)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(PAPERPLOT_ROOT), str(target)], stdout=subprocess.PIPE,
                         text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        with pytest.raises(locks.LockTimeout):
            with locks.file_lock(target, timeout=0.3):
                pass
    finally:
        p.wait(10)
    with locks.file_lock(target, timeout=2):
        pass


def test_datastore_writes_use_the_registry_lock():
    import inspect
    from designer import datastore
    for fn in (datastore.add, datastore.remove, datastore.refresh):
        assert "_registry_lock()" in inspect.getsource(fn), fn.__name__


# ============================================================================================ compile queue
def test_compile_queue_positions():
    r = jobs.JobRunner(workers=1, name="test-queue")
    gate = threading.Event()
    try:
        a = r.submit("compile", gate.wait, 5)
        b = r.submit("compile", lambda: 1)
        c = r.submit("compile", lambda: 2)
        t0 = time.time()
        while r.get(a)["status"] != "running" and time.time() - t0 < 5:
            time.sleep(0.01)
        assert r.get(a)["position"] == 0 and r.get(a)["status"] == "running"
        assert r.get(b)["position"] == 1 and r.get(c)["position"] == 2
        assert r.submit("compile", lambda: 3, dedupe_queued=True) == b, "a queued compile is joined, not duplicated"
        gate.set()
        assert r.wait(c, timeout=5)["status"] == "done"
    finally:
        gate.set()
        r.shutdown()


# ============================================================================================ config
def test_config_cross_platform_bits(monkeypatch):
    assert config.PYTHON == sys.executable
    claude = r"C:\x\claude.exe\bin" if os.pathsep == ";" else "/x/claude.exe/bin"   # ":" would split "C:\..."
    monkeypatch.setenv("PATH", os.pathsep.join(["/opt/a", claude, "/opt/b"]))
    monkeypatch.setattr(config, "LATEX_BIN", None)
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    assert config.latex_env()["PATH"].split(os.pathsep) == ["/opt/a", claude, "/opt/b"]
    monkeypatch.delenv("max_print_line", raising=False)
    assert config.latex_env()["max_print_line"] == "10000"     # TeX Live must not wrap .log lines at 79
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    got = config.latex_env()["PATH"].split(os.pathsep)
    # claude.exe's entry is dropped; on a Windows box a perl dir may be appended (latexmk is a Perl script)
    assert got[:2] == ["/opt/a", "/opt/b"] and len(got) <= 3 and not any("claude.exe" in p for p in got), got
    d = tempfile.mkdtemp(prefix="latexbin_")
    monkeypatch.setattr(config, "LATEX_BIN", d)
    assert config.latex_env()["PATH"].split(os.pathsep)[0] == d
    monkeypatch.setattr(config, "LATEX_BIN", os.path.join(d, "missing"))
    assert config.latex_env()["PATH"].split(os.pathsep)[0] == "/opt/a"
    assert config.is_loopback("127.0.0.1") and config.is_loopback("::1") and config.is_loopback("[::1]")
    assert config.is_loopback("localhost") and not config.is_loopback("0.0.0.0") and not config.is_loopback("")


def test_paper_root_required_off_windows(monkeypatch):
    monkeypatch.delenv("DESIGNER_PAPER_ROOT", raising=False)
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    with pytest.raises(config.ConfigError) as ei:
        config._paper_root()
    assert "DESIGNER_PAPER_ROOT" in str(ei.value)
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    assert config._paper_root() == LIVE_CLONE           # Windows default unchanged (computed only, nothing touched)
    monkeypatch.setenv("DESIGNER_PAPER_ROOT", "/srv/paper")
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    assert config._paper_root().name == "paper"
