"""Static checks for the designer frontend (designer/static/).

No browser here: these tests check the wiring that breaks silently — pinned CDN URLs, module imports/exports,
element ids, and that every backend URL the frontend calls is a contract endpoint. If the Flask app is importable
they also fetch the pages through its test client (with DESIGNER_PAPER_ROOT / DESIGNER_STATE_DIR pointed at temp dirs).

Run:  python -m pytest designer/tests/test_static.py   or   python designer/tests/test_static.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
JS_FILES = sorted(STATIC.glob("*.js"))

PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"
PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js"
CM_BASE = "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/"
CM = [CM_BASE + "codemirror.min.js", CM_BASE + "codemirror.min.css", CM_BASE + "mode/stex/stex.min.js"]
# section editor: Ctrl+F search (search + searchcursor + dialog), go to line, active-line highlight
CM_ADDONS = [CM_BASE + "addon/dialog/dialog.min.css", CM_BASE + "addon/dialog/dialog.min.js",
             CM_BASE + "addon/search/searchcursor.min.js", CM_BASE + "addon/search/search.min.js",
             CM_BASE + "addon/search/jump-to-line.min.js", CM_BASE + "addon/selection/active-line.min.js",
             CM_BASE + "addon/comment/comment.min.js"]

# contract section 5 (method, path regex); <x> segments become [^/]+ (fid may contain encoded slashes)
CONTRACT = [
    ("GET", r"/api/config"), ("GET", r"/api/specs"), ("POST", r"/api/specs"),
    ("GET", r"/api/specs/X"), ("PUT", r"/api/specs/X"), ("DELETE", r"/api/specs/X"),
    ("GET", r"/api/controls"), ("POST", r"/api/preview"), ("POST", r"/api/specs/X/render"),
    ("POST", r"/api/compile"), ("GET", r"/api/jobs/X"), ("GET", r"/paper\.pdf"),
    ("GET", r"/api/figures"), ("GET", r"/api/figures/X/tex"), ("PUT", r"/api/figures/X/tex"),
    ("POST", r"/api/figures/insert"), ("POST", r"/api/context/X"), ("POST", r"/api/synctex/reverse"),
    ("GET", r"/api/data"), ("POST", r"/api/data"), ("GET", r"/api/data/X"), ("DELETE", r"/api/data/X"),
    ("GET", r"/api/data/X/preview"), ("GET", r"/api/git/status"), ("POST", r"/api/git/fetch"),
    ("POST", r"/api/git/pull"), ("POST", r"/api/git/commit"), ("POST", r"/api/git/push"),
]
# added with CONTRACT section 7 (deployment); the frontend may use them but does not have to
OPTIONAL = [("GET", r"/api/fonts/check")]
# the section editor (whole .tex files) and synctex forward search, added 2026-11-09
EDITOR = [("GET", r"/api/tex/sections"), ("GET", r"/api/tex/file"), ("PUT", r"/api/tex/file"),
          ("POST", r"/api/synctex/forward")]


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_files_exist():
    for f in ("index.html", "app.js", "pdfview.js", "controls.js", "styles.css", "api.js", "ui.js"):
        assert (STATIC / f).is_file(), f


def test_editor_toggle_comment_binding():
    js = read("app.js")
    assert "'Ctrl-/': 'toggleComment', 'Cmd-/': 'toggleComment'" in js


def test_cdn_pinned_exactly():
    html = read("index.html")
    assert PDFJS in html
    for u in CM + CM_ADDONS:
        assert u in html, u
    assert PDFJS_WORKER in read("pdfview.js")
    # every external URL anywhere in static/ is one of the pinned cdnjs files
    allowed = {PDFJS, PDFJS_WORKER, *CM, *CM_ADDONS, "http://www.w3.org/2000/svg"}
    for f in STATIC.iterdir():
        if f.suffix not in (".html", ".js", ".css"):
            continue
        for u in re.findall(r"https?://[^\s'\"`)<>]+", f.read_text(encoding="utf-8")):
            u = u.rstrip("',;")
            assert u in allowed or u.startswith("http://www.w3.org/2000/svg"), f"{f.name}: unpinned external URL {u}"


def test_no_hardcoded_machine_paths():
    for f in STATIC.iterdir():
        if f.suffix in (".html", ".js", ".css"):
            t = f.read_text(encoding="utf-8")
            assert not re.search(r"[A-Za-z]:[\\/]+Users", t), f"{f.name} hard-codes a local path"
            assert "4D_Curricula_RSS_overleaf" not in t, f.name


def _exports(src):
    names = set(re.findall(r"export\s+(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][\w$]*)", src))
    for grp in re.findall(r"export\s*\{([^}]*)\}", src):
        names |= {n.split(" as ")[-1].strip() for n in grp.split(",") if n.strip()}
    return names


def test_module_imports_resolve():
    html = read("index.html")
    assert re.search(r'<script type="module" src="static/app\.js"></script>', html)   # relative (base path)
    for f in JS_FILES:
        src = f.read_text(encoding="utf-8")
        for names, path in re.findall(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'", src):
            assert path.startswith("./"), f"{f.name}: non-relative import {path}"
            target = STATIC / path[2:]
            assert target.is_file(), f"{f.name}: missing module {path}"
            exported = _exports(target.read_text(encoding="utf-8"))
            for n in [x.strip().split(" as ")[0] for x in names.split(",") if x.strip()]:
                assert n in exported, f"{f.name} imports {n} from {path}, which does not export it"


def test_element_ids_exist():
    html = read("index.html")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    dynamic = {"ins-files"}  # created by dialogs at runtime
    for f in JS_FILES:
        src = f.read_text(encoding="utf-8")
        used = set(re.findall(r"\$\('#([\w-]+)", src)) | set(re.findall(r"getElementById\('([\w-]+)'\)", src))
        missing = used - ids - dynamic
        assert not missing, f"{f.name} references ids not in index.html: {sorted(missing)}"
    for split in re.findall(r"data-split=(\w+)\]", read("app.js")):
        assert f'data-split="{split}"' in html, split


def _norm_url(u):
    u = u.split("?")[0]
    u = re.sub(r"\$\{[^}]+\}", "X", u)
    return u


def test_api_urls_are_contract_endpoints():
    src = read("api.js")
    calls = re.findall(r"req\('(GET|POST|PUT|DELETE)',\s*(`[^`]*`|'[^']*')", src)
    assert len(calls) >= 25, calls
    for method, raw in calls:
        url = _norm_url(raw.strip("`'"))
        url = url.split("'")[0].rstrip(" +")
        assert not url.startswith("/"), f"{method} {url}: frontend URLs must be relative (subpath deployments)"
        url = "/" + url
        ok = any(m == method and re.fullmatch(p.replace("X", "[^/]+"), url) for m, p in CONTRACT + OPTIONAL + EDITOR)
        assert ok, f"{method} {url} is not a contract endpoint"
    # no other module talks to the backend directly, except the keep-alive spec save on unload (same PUT endpoint)
    for f in JS_FILES:
        if f.name == "api.js":
            continue
        for u in re.findall(r"fetch\(\s*[`'\"]([^`'\"]+)", f.read_text(encoding="utf-8")):
            u = _norm_url(u)
            assert re.fullmatch(r"api/specs/X", u), f"{f.name} fetches {u} outside api.js"
    # every contract endpoint the UI needs is wired
    for m, p in CONTRACT:
        hit = any(m == method and re.fullmatch(p.replace("X", "[^/]+"), "/" + _norm_url(raw.strip("`'")).split("'")[0].rstrip(" +"))
                  for method, raw in calls) or (p == r"/paper\.pdf" and "abs(`paper.pdf" in src)
        assert hit, f"contract endpoint {m} {p} is not used by the frontend"


def test_no_absolute_app_urls():
    """Subpath-safe (CONTRACT section 7): no root-absolute URL to the app's own routes anywhere in static/."""
    pat = re.compile(r"""(?:['"`(]|\bhref=|\bsrc=)\s*/(?:api|static|paper\.pdf|context|healthz)\b""")
    for f in STATIC.iterdir():
        if f.suffix in (".html", ".js", ".css"):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                assert not pat.search(line), f"{f.name}:{i}: root-absolute app URL: {line.strip()[:120]}"


def test_push_only_from_confirmed_dialog():
    app = read("app.js")
    # git push is reached only through doPush, which awaits a modal titled "Push to Overleaf?"
    assert app.count("api.git.push") == 0
    assert "gitAction('push'" in app
    body = app[app.index("async function doPush"):app.index("async function openCommitDialog")]
    assert "Push to Overleaf?" in body and body.index("Push to Overleaf?") < body.index("gitAction('push'")
    # the push runs only when that dialog resolved to true (either guard style)
    guard = body.find("ok === true") if "ok === true" in body else body.find("if (ok !== true) return")
    assert 0 <= guard < body.index("gitAction('push'"), "push must be guarded by the confirm dialog's result"


def test_fid_is_url_encoded():
    api = read("api.js")
    for frag in ("api/figures/${enc(fid)}/tex", "api/context/${enc(fid)}"):
        assert frag in api, frag


def test_css_tokens_and_accent():
    css = read("styles.css")
    assert "--accent: #B5454F" in css
    assert re.search(r"body\s*\{[^}]*background", css)


def test_served_by_flask_app():
    """If the backend is importable, check / and /static/* through its test client (temp paper/state dirs)."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    tmp = tempfile.mkdtemp(prefix="designer_static_")
    os.environ.setdefault("DESIGNER_PAPER_ROOT", os.path.join(tmp, "paper"))
    os.environ.setdefault("DESIGNER_STATE_DIR", os.path.join(tmp, "state"))
    os.makedirs(os.environ["DESIGNER_PAPER_ROOT"], exist_ok=True)
    try:
        from designer import server  # noqa: WPS433
    except Exception as e:  # backend not there yet / import error: nothing to check here
        print(f"skip: designer.server not importable ({type(e).__name__}: {e})")
        return
    make = getattr(server, "create_app", None)
    app = make() if make else getattr(server, "app", None)
    if app is None:
        print("skip: no Flask app in designer.server")
        return
    c = app.test_client()
    r = c.get("/", headers={"Host": "127.0.0.1:8766"})
    assert r.status_code == 200 and b'src="static/app.js"' in r.data and b'<base href="/">' in r.data
    for f in ("app.js", "api.js", "ui.js", "pdfview.js", "controls.js", "styles.css"):
        r = c.get(f"/static/{f}", headers={"Host": "127.0.0.1:8766"})
        assert r.status_code == 200, f
        ctype = r.headers.get("Content-Type", "")
        assert ("javascript" in ctype) if f.endswith(".js") else ("css" in ctype), (f, ctype)


def test_page_view_scrolls_through_every_page():
    """The Page mode stacks every compiled page in one scrolling column (as in Overleaf), drawn lazily."""
    html, pv, app = read("index.html"), read("pdfview.js"), read("app.js")
    assert 'id="page" class="pages hidden"' in html and 'id="page-canvas"' not in html
    viewer = pv[pv.index("export class PageViewer"):pv.index("export async function renderStack")]
    assert "_renderNear" in viewer and "p.canvas.width = 0" in viewer    # only pages near the viewport keep a bitmap
    assert "stage.addEventListener('scroll'" in viewer and "_trackPage" in viewer   # the pager follows scrolling
    assert "box.page || this.pageNum" in viewer                         # focusBox scrolls to any page
    assert "pagesEl: $('#page')" in app and "flashMark({ ...r.box, page: r.page })" in app


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
