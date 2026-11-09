"""Flask server for the plot designer (contract sections 5 and 7).

JSON in / JSON out; every error is `{error, detail}` with a proper status code.
The backend modules (spec, render, latex, datastore, gitops) are imported lazily inside the handlers, so the server
starts (and the other endpoints keep working) even while one of them is missing or broken; a missing module answers
503 `{error: "module unavailable", detail}`.

Deployment (CONTRACT section 7), all read from designer.config at request time:
- Bind address: 127.0.0.1 by default. A non-loopback bind refuses to start without DESIGNER_TOKEN (check_bind_security).
- Auth (when DESIGNER_TOKEN is set): every route except /, /static/*, /favicon.ico and /healthz needs the token, as
  `Authorization: Bearer <t>` or the `designer_token` cookie (HttpOnly, SameSite=Strict) that `GET /?token=<t>` sets
  before redirecting to the same URL without the token. Compared with hmac.compare_digest.
- Without a token the server is a local tool: requests whose Host is not loopback are refused (DNS rebinding).
- Writes (POST/PUT/DELETE) must be same-origin: Origin, when sent, must equal this request's own origin as the browser
  sees it (Host / X-Forwarded-Host, scheme / X-Forwarded-Proto), and /api writes must be JSON or carry X-Designer: 1.
- DESIGNER_READONLY: every mutating route except preview / context / compile / synctex / diff_stat answers 403
  (so PUT /api/tex/file, the section editor's save, is refused).
- Base path: DESIGNER_BASE_PATH or X-Forwarded-Prefix becomes SCRIPT_NAME (PrefixMiddleware); index.html gets a
  matching <base href>, and the frontend only uses relative URLs.

Start it with `python -m designer` or `python designer/serve.py` (flags in designer/__main__.py, applied before
designer.config is imported). `create_app()` builds the app; `run()` serves it; designer/wsgi.py exposes `app`.
"""
from __future__ import annotations

import hmac
import html as _html
import importlib
import json
import inspect
import os
import re
import shutil
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path, PurePath
from urllib.parse import quote, unquote, urlencode, urlsplit

os.environ.setdefault("MPLBACKEND", "Agg")      # renders happen on request threads: never a GUI backend

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from designer import config, jobs

SPEC_ID_RE = re.compile(r"^[a-z0-9_]+$")
DATA_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~\-]+)+$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
BACKEND_MODULES = ("spec", "render", "latex", "datastore", "gitops")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
TOKEN_COOKIE = "designer_token"
# endpoints reachable without the token (static assets may be public; /healthz is for container healthchecks)
PUBLIC_ENDPOINTS = {"index", "static", "favicon", "healthz", "login_form", "login"}
# mutating-method endpoints that stay allowed in read-only mode: they write only the cache / LaTeX build artefacts
# (context_compile refuses a TeX text override in read-only mode: only the stored figure text + a live spec)
READONLY_ALLOWED = {"preview", "compile_paper", "context_compile", "synctex_reverse", "synctex_forward",
                    "git_diff_stat", "login"}
TOKEN_QS_RE = re.compile(r"([?&])token=[^&\s]*")
READONLY_DETAIL = ("this designer runs in read-only mode (DESIGNER_READONLY=1): it cannot change specs, the paper repo, "
                   "the data registry or git. Preview, context preview and recompile still work.")
NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


# ---------------------------------------------------------------------------------------------- errors & helpers
class ApiError(Exception):
    """Raise inside a handler to answer `{error, detail, **extra}` with `status`."""

    def __init__(self, status: int, error: str, detail: str = "", **extra):
        super().__init__(error)
        self.status, self.error, self.detail, self.extra = status, error, detail, extra


class ModuleUnavailable(ApiError):
    pass


def _mod(name: str):
    """Lazily import designer.<name>; 503 if it is missing or fails to import."""
    try:
        return importlib.import_module(f"designer.{name}")
    except Exception as e:  # ImportError, or an error raised while the module imports
        raise ModuleUnavailable(503, "module unavailable",
                                f"designer.{name} could not be imported: {type(e).__name__}: {e}") from e


def _call(fn, *args, **kwargs):
    """Call fn with only the keyword arguments its signature accepts (tolerates small signature drift)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _body() -> dict:
    """Request JSON as a dict ({} when empty). Content-Type is not required. 400 on malformed JSON."""
    raw = request.get_data(cache=True)
    if not raw or not raw.strip():
        return {}
    data = request.get_json(force=True, silent=True)
    if data is None:
        raise ApiError(400, "invalid JSON body", "the request body is not valid JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid JSON body", "expected a JSON object")
    return data


def _spec_id(spec_id: str) -> str:
    if not SPEC_ID_RE.match(spec_id or ""):
        raise ApiError(400, "invalid spec id", f"{spec_id!r} does not match [a-z0-9_]+")
    return spec_id


def _data_name(name: str) -> str:
    if not DATA_NAME_RE.match(name or ""):
        raise ApiError(400, "invalid dataset name", f"{name!r} does not match [A-Za-z0-9_.-]+")
    return name


def _fid(fid: str) -> str:
    """Figure ids arrive URL-encoded (encodeURIComponent); Werkzeug decodes once — decode a second layer if present."""
    if "%" in fid:
        fid = unquote(fid)
    return fid


def _pdf_path() -> Path:
    return config.PAPER_ROOT / (Path(config.MAIN_TEX).stem + ".pdf")


def _pdf_file_response(path: Path, download_name: str) -> Response:
    """Serve a PDF from memory (Windows: never hold a handle that would block latexmk from replacing it)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ApiError(404, "pdf not found", f"{path} does not exist (compile first)")
    except PermissionError as e:   # being rewritten right now
        raise ApiError(503, "pdf busy", f"{path} is being written: {e}")
    resp = Response(data, mimetype="application/pdf")
    resp.headers.update(NO_CACHE)
    resp.headers["Content-Disposition"] = f'inline; filename="{download_name}"'
    try:
        resp.headers["Last-Modified"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(path.stat().st_mtime))
    except OSError:
        pass
    return resp


def _exception_response(e: Exception):
    """Map exceptions from backend modules to JSON errors."""
    name = type(e).__name__
    if isinstance(e, ApiError):
        body = {"error": e.error, "detail": e.detail}
        extra = dict(e.extra)
        challenge = extra.pop("www_authenticate", False)
        body.update(extra)
        resp = jsonify(body)
        resp.status_code = e.status
        if challenge:
            resp.headers["WWW-Authenticate"] = 'Bearer realm="plot designer"'
        if e.status in (401, 403):
            resp.headers.update(NO_CACHE)
        return resp
    if isinstance(e, HTTPException):
        return jsonify({"error": e.name, "detail": e.description or ""}), e.code or 500
    if name == "StaleError":
        return jsonify({"error": "stale", "detail": str(e)}), 409
    if name == "FontUnavailable":
        return jsonify({"error": "font unavailable", "detail": str(e)}), 422
    if name == "LockTimeout":
        return jsonify({"error": "busy", "detail": str(e)}), 503
    if name == "Busy":
        resp = jsonify({"error": "busy", "detail": str(e)})
        resp.status_code = 429
        resp.headers["Retry-After"] = "5"
        return resp
    if isinstance(e, FileExistsError):
        return jsonify({"error": "already exists", "detail": str(e)}), 409
    if isinstance(e, (FileNotFoundError, KeyError)):
        detail = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        return jsonify({"error": "not found", "detail": str(detail)}), 404
    if isinstance(e, PermissionError):
        return jsonify({"error": "forbidden", "detail": str(e)}), 403
    if isinstance(e, ValueError):
        return jsonify({"error": "bad request", "detail": f"{name}: {e}"}), 400
    if isinstance(e, TimeoutError) or name == "TimeoutExpired":
        return jsonify({"error": "timeout", "detail": str(e)}), 504
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    print(tb, file=sys.stderr)
    body = {"error": "internal error", "detail": f"{name}: {e}"}
    if config.show_tracebacks():      # DESIGNER_DEBUG, or the tokenless loopback local tool; else stderr only
        body["traceback"] = tb[-3000:]
    return jsonify(body), 500


class _JSONProvider(DefaultJSONProvider):
    """Paths -> str, numpy scalars/arrays -> python, sets -> lists, datetimes -> ISO."""
    sort_keys = False

    @staticmethod
    def default(o):
        if isinstance(o, PurePath):
            return o.as_posix() if not o.is_absolute() else str(o)
        if isinstance(o, (set, frozenset)):
            return list(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, bytes):
            return o.decode("utf-8", "replace")
        mod = type(o).__module__
        if mod == "numpy":
            if hasattr(o, "tolist"):
                return o.tolist()
            if hasattr(o, "item"):
                return o.item()
        if hasattr(o, "to_dict"):           # pandas objects
            try:
                return o.to_dict()
            except Exception:
                pass
        return DefaultJSONProvider.default(o)


def _module_status() -> dict:
    import importlib.util
    out = {}
    for name in BACKEND_MODULES:
        try:
            out[name] = importlib.util.find_spec(f"designer.{name}") is not None
        except Exception:
            out[name] = False
    return out


# ---------------------------------------------------------------------------------------------- deployment helpers
class PrefixMiddleware:
    """Serve the app under a URL prefix (reverse-proxy subpath).

    The prefix is config.BASE_PATH, else a well-formed X-Forwarded-Prefix header. It becomes SCRIPT_NAME (so
    request.script_root, redirects and the injected <base href> carry it); if the proxy did not strip it from the path,
    it is stripped here — so both `/designer/api/x` and an already-stripped `/api/x` reach the same route."""

    def __init__(self, app, base_path: str | None = None):
        self.app = app
        self.base_path = base_path

    def __call__(self, environ, start_response):
        prefix = config.BASE_PATH if self.base_path is None else self.base_path
        if not prefix:
            fwd = (environ.get("HTTP_X_FORWARDED_PREFIX") or "").split(",")[0].strip().rstrip("/")
            prefix = fwd if PREFIX_RE.match(fwd) else ""
        if prefix:
            path = environ.get("PATH_INFO", "") or ""
            if path == prefix or path.startswith(prefix + "/"):
                environ["PATH_INFO"] = path[len(prefix):] or "/"
            script = (environ.get("SCRIPT_NAME") or "").rstrip("/")
            if not script.endswith(prefix):
                environ["SCRIPT_NAME"] = script + prefix
        return self.app(environ, start_response)


def check_bind_security(host: str, token: str | None) -> None:
    """Refuse (SystemExit with a clear message) to serve on a non-loopback address without a token."""
    if not config.is_loopback(host) and not (token or "").strip():
        raise SystemExit(
            f"refusing to listen on {host!r} without a token: anyone who can reach this address could edit the paper "
            "and push to Overleaf. Set DESIGNER_TOKEN (or pass --token), e.g. "
            "DESIGNER_TOKEN=$(python -c \"import secrets; print(secrets.token_urlsafe(32))\"), "
            "or bind to 127.0.0.1.")


def _token_ok(given: str | None) -> bool:
    want = config.TOKEN
    if not want or not given:
        return False
    return hmac.compare_digest(given.strip().encode("utf-8"), want.encode("utf-8"))


def _request_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.cookies.get(TOKEN_COOKIE)


def _first(header: str) -> str:
    return (request.headers.get(header) or "").split(",")[0].strip()


def _norm_origin(scheme: str, netloc: str) -> str:
    scheme, netloc = scheme.lower(), netloc.lower()
    for s, port in (("http", ":80"), ("https", ":443")):
        if scheme == s and netloc.endswith(port):
            netloc = netloc[: -len(port)]
    return f"{scheme}://{netloc}"


def _own_origins() -> set[str]:
    """The origins a same-origin page can have for this request: Host (or X-Forwarded-Host, set by a reverse proxy)
    with the request scheme (or X-Forwarded-Proto). A cross-site page cannot set these headers without a CORS
    preflight, which this server never answers."""
    hosts = {request.host or ""} | ({_first("X-Forwarded-Host")} - {""})
    schemes = {request.scheme} | ({_first("X-Forwarded-Proto")} - {""})
    return {_norm_origin(s, h) for s in schemes for h in hosts if h}


def _is_https() -> bool:
    return request.scheme == "https" or _first("X-Forwarded-Proto").lower() == "https"


def _latex_found() -> bool:
    return shutil.which("latexmk", path=config.latex_env().get("PATH")) is not None


# ---------------------------------------------------------------------------------------------- compile job
def compile_job() -> dict:
    """Full paper compile (runs on the single LaTeX worker). Result = compile_paper(...) + `boxes` = locate_all()."""
    latex = _mod("latex")
    res = dict(_call(latex.compile_paper, root=config.PAPER_ROOT) or {})
    boxes, locate_error = {}, None
    if res.get("pdf") or _pdf_path().exists():
        try:
            boxes = _call(latex.locate_all, root=config.PAPER_ROOT) or {}
        except Exception as e:   # a compile result is still useful without boxes
            locate_error = f"{type(e).__name__}: {e}"
    res["boxes"] = boxes
    if locate_error:
        res["locate_error"] = locate_error
    try:
        res["pdf_mtime"] = _pdf_path().stat().st_mtime
    except OSError:
        res["pdf_mtime"] = None
    return res


# ---------------------------------------------------------------------------------------------- app
def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
    app.json_provider_class = _JSONProvider
    app.json = _JSONProvider(app)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    app.wsgi_app = PrefixMiddleware(app.wsgi_app)

    # ---- guards: Host (tokenless = local tool), token, Origin / X-Designer (CSRF), read-only ----
    @app.before_request
    def _guard():
        if not config.TOKEN:
            # Local tool without a token: only loopback Host headers (DNS rebinding protection).
            try:
                host = urlsplit("//" + (request.host or "")).hostname or ""
            except ValueError:
                host = "?"
            if host and host.lower() not in LOCAL_HOSTS:
                raise ApiError(403, "forbidden host", f"Host {request.host!r} is not local (set DESIGNER_TOKEN to "
                                                      "serve other hosts)")
        elif request.endpoint not in PUBLIC_ENDPOINTS and not _token_ok(_request_token()):
            raise ApiError(401, "unauthorized",
                           "this designer needs its access token: open the page once as <url>/?token=<token>, or "
                           "send 'Authorization: Bearer <token>'", www_authenticate=True)
        if request.method not in SAFE_METHODS:
            # Writes must come from this very origin (scheme + host + port): a page on another localhost port
            # (a Vite/Jupyter dev server, ...) is a different origin and must not be able to push or commit.
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    o = urlsplit(origin.strip())
                    norm = _norm_origin(o.scheme, o.netloc) if o.scheme and o.netloc else origin
                except ValueError:
                    norm = origin
                if norm not in _own_origins():
                    raise ApiError(403, "forbidden origin", f"cross-origin {request.method} from {origin!r} refused")
            # ...and must be a non-"simple" request (JSON body or the X-Designer header), which a foreign page can
            # only send after a CORS preflight that this server never answers. Covers clients that omit Origin.
            if request.path.startswith("/api/") and request.mimetype != "application/json" \
                    and request.headers.get("X-Designer") != "1":
                raise ApiError(403, "forbidden request",
                               f"{request.method} {request.path} needs Content-Type: application/json "
                               "or the header X-Designer: 1")
            if config.READONLY and request.endpoint not in READONLY_ALLOWED:
                raise ApiError(403, "read-only", READONLY_DETAIL, readonly=True)

    @app.errorhandler(Exception)
    def _on_error(e):
        return _exception_response(e)

    # ------------------------------------------------------------------ static
    def _signed_in_redirect(location: str) -> Response:
        """303 to `location` with the HttpOnly token cookie set (scoped to the app's base path)."""
        base = (request.script_root or "").rstrip("/") + "/"
        resp = redirect(location, code=303)
        resp.set_cookie(TOKEN_COOKIE, config.TOKEN, max_age=30 * 24 * 3600, path=base, httponly=True,
                        samesite="Strict", secure=_is_https())
        resp.headers.update(NO_CACHE)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def _login_page(message: str = "", status: int = 200) -> Response:
        msg = f"<p class='err'>{_html.escape(message)}</p>" if message else ""
        page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Plot Designer</title>"
                "<style>body{font-family:system-ui,sans-serif;background:#f6f6f7;color:#222;display:flex;"
                "justify-content:center;padding:12vh 16px}form{background:#fff;border:1px solid #ddd;border-radius:8px;"
                "padding:24px;max-width:360px;width:100%}h1{font-size:18px;margin:0 0 12px}input{width:100%;"
                "box-sizing:border-box;padding:8px;margin:8px 0 12px;font:inherit}button{padding:8px 16px;"
                "background:#B5454F;color:#fff;border:0;border-radius:4px;font:inherit;cursor:pointer}"
                ".err{color:#B5454F}</style></head><body>"
                "<form method='post' action='login' autocomplete='off'><h1>Plot Designer</h1>" + msg +
                "<label for='token'>Access token</label>"
                "<input id='token' name='token' type='password' autofocus required>"
                "<button type='submit'>Sign in</button></form></body></html>")
        return Response(page, status=status, mimetype="text/html", headers=NO_CACHE)

    @app.get("/login")
    def login_form():
        if not config.TOKEN:
            return redirect((request.script_root or "").rstrip("/") + "/", code=303)
        return _login_page()

    @app.post("/login")
    def login():
        """Sign in with the token in a POST body (never in a URL, so it stays out of access logs and history)."""
        base = (request.script_root or "").rstrip("/") + "/"
        if not config.TOKEN:
            return redirect(base, code=303)
        if not _token_ok(request.form.get("token")):
            return _login_page("Wrong access token.", status=401)
        return _signed_in_redirect(base)

    @app.get("/")
    def index():
        base = (request.script_root or "").rstrip("/") + "/"
        if "token" in request.args and config.TOKEN:
            # one-time login link (fallback; the /login form keeps the token out of URLs and logs): set the HttpOnly
            # cookie and redirect to the same URL without the token
            if not _token_ok(request.args.get("token")):
                html = ("<!doctype html><meta charset='utf-8'><title>Plot Designer</title>"
                        "<p style='font-family:sans-serif'>Wrong or expired access token.</p>")
                return Response(html, status=401, mimetype="text/html", headers=NO_CACHE)
            rest = [(k, v) for k, v in request.args.items(multi=True) if k != "token"]
            return _signed_in_redirect(base + (("?" + urlencode(rest)) if rest else ""))
        idx = config.STATIC_DIR / "index.html"
        if not idx.exists():
            html = ("<!doctype html><meta charset='utf-8'><title>Plot Designer</title>"
                    "<p style='font-family:sans-serif'>Plot Designer API is running; the frontend is not built yet.</p>")
            return Response(html, mimetype="text/html", headers=NO_CACHE)
        # <base href> = where the app is mounted, so the frontend's relative URLs work under any subpath
        page = idx.read_text(encoding="utf-8")
        tag = f'<base href="{_html.escape(base, quote=True)}">'
        page = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + "\n" + tag, page, count=1)
        resp = Response(page, mimetype="text/html")
        resp.headers.update(NO_CACHE)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/healthz")
    def healthz():
        """Unauthenticated liveness check for container healthchecks: no paths, no secrets."""
        return jsonify({"ok": True, "version": config.VERSION,
                        "paper_root_exists": (config.PAPER_ROOT / config.MAIN_TEX).is_file(),
                        "latex_found": _latex_found()})

    @app.get("/api/fonts/check")
    def fonts_check():
        return jsonify(_mod("render").check_fonts())

    @app.get("/favicon.ico")
    def favicon():
        if (config.STATIC_DIR / "favicon.ico").exists():
            return send_from_directory(config.STATIC_DIR, "favicon.ico")
        return Response(status=204)

    # ------------------------------------------------------------------ config
    @app.get("/api/config")
    def api_config():
        pdf = _pdf_path()
        has_pdf = pdf.exists()
        return jsonify({
            "paper_root": str(config.PAPER_ROOT),
            "main_tex": config.MAIN_TEX,
            "port": config.PORT,
            "has_pdf": has_pdf,
            "pdf_mtime": pdf.stat().st_mtime if has_pdf else None,
            "state_dir": str(config.STATE_DIR),
            "specs_dir": str(config.SPECS_DIR),
            "modules": _module_status(),
            "version": config.VERSION,
            "readonly": bool(config.READONLY),
            "auth": bool(config.TOKEN),
            "base_path": request.script_root or "",
            "platform": "windows" if os.name == "nt" else "posix",
        })

    # ------------------------------------------------------------------ specs
    @app.get("/api/specs")
    def specs_list():
        return jsonify(_mod("spec").list_specs())

    @app.post("/api/specs")
    def specs_create():
        body = _body()
        spec_id = _spec_id(str(body.get("id", "")))
        template_id = body.get("template_id") or None
        if template_id is not None:
            _spec_id(str(template_id))
        spec = _mod("spec")
        created = spec.create(spec_id, template_id=template_id)
        return jsonify(created), 201

    def _spec_base_hash():
        """The version the client edited from: header X-Spec-Base-Hash (or ?base_hash=). None = unconditional save
        (scripts, old clients); the UI always sends it, so a stale tab can't overwrite a newer file."""
        v = request.headers.get("X-Spec-Base-Hash") or request.args.get("base_hash") or None
        if v is not None and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ApiError(400, "bad base_hash", "X-Spec-Base-Hash must be the 64-hex sha256 from GET /api/specs/<id>")
        return v

    def _spec_save(spec, body, spec_id):
        """spec.save with compare-and-swap; a stale base answers 409 with the spec now on disk and its hash."""
        base = _spec_base_hash()
        params = inspect.signature(spec.save).parameters
        try:
            new_hash = spec.save(body, base_hash=base) if "base_hash" in params else spec.save(body)
        except Exception as e:
            if type(e).__name__ != "StaleError":
                raise
            current = None
            try:
                current = spec.load(spec_id)
            except FileNotFoundError:
                pass
            raise ApiError(409, "stale", str(e), current=current, hash=getattr(e, "current_hash", None))
        return new_hash

    @app.get("/api/specs/<spec_id>")
    def specs_get(spec_id):
        spec = _mod("spec")
        sid = _spec_id(spec_id)
        resp = jsonify(spec.load(sid))
        h = spec.file_hash(sid) if hasattr(spec, "file_hash") else None
        if h:
            resp.headers["X-Spec-Hash"] = h
        return resp

    @app.put("/api/specs/<spec_id>")
    def specs_put(spec_id):
        _spec_id(spec_id)
        body = _body()
        if not body:
            raise ApiError(400, "empty spec", "PUT body must be the full spec object")
        if body.get("id") is None:
            body["id"] = spec_id
        if body["id"] != spec_id:
            raise ApiError(400, "id mismatch", f"body id {body['id']!r} != URL id {spec_id!r}",
                           errors=[f"id {body['id']!r} does not match {spec_id!r}"])
        spec = _mod("spec")
        errors = list(spec.validate(body) or [])
        if errors:
            raise ApiError(400, "invalid spec", "; ".join(errors), errors=errors)
        new_hash = _spec_save(spec, body, spec_id)
        resp = jsonify({"ok": True, "id": spec_id, "spec": body, "hash": new_hash})
        if new_hash:
            resp.headers["X-Spec-Hash"] = new_hash
        return resp

    @app.delete("/api/specs/<spec_id>")
    def specs_delete(spec_id):
        _mod("spec").delete(_spec_id(spec_id))
        return jsonify({"ok": True, "id": spec_id})

    @app.get("/api/controls")
    def controls():
        spec = _mod("spec")
        params = inspect.signature(spec.controls).parameters
        kwargs = {}
        for arg in ("theme", "kind"):           # ?kind= picks `style` for that kind (style_by_kind has them all)
            if request.args.get(arg) and arg in params:
                kwargs[arg] = request.args[arg]
        return jsonify(spec.controls(**kwargs))

    # ------------------------------------------------------------------ render
    @app.post("/api/preview")
    def preview():
        body = _body()
        if not body:
            raise ApiError(400, "empty spec", "POST body must be a spec object")
        spec = _mod("spec")
        errors = list(spec.validate(body) or [])
        if errors:
            raise ApiError(400, "invalid spec", "; ".join(errors), errors=errors)
        render = _mod("render")
        kwargs = {}
        if request.args.get("dpi"):
            try:
                dpi = int(request.args["dpi"])
            except ValueError:
                raise ApiError(400, "bad dpi", request.args["dpi"])
            kwargs["dpi"] = max(30, min(dpi, 600))
        warns, notes = [], []
        if hasattr(render, "preview"):       # PNG + warnings (layout overflow, partial data, ...) in one call
            out = render.preview(body, **kwargs)
            png, warns, notes = out["png"], list(out.get("warnings") or []), list(out.get("notes") or [])
        else:
            png = render.preview_png(body, **kwargs)
        resp = Response(png, mimetype="image/png")
        resp.headers.update(NO_CACHE)
        # URL-encoded JSON list: header values must be latin-1, warnings may hold TeX/unicode arm names
        resp.headers["X-Render-Warnings"] = quote(json.dumps(warns[:50]), safe="")
        resp.headers["X-Render-Notes"] = quote(json.dumps(notes[:10]), safe="")   # e.g. what a yellow interval means
        return resp

    @app.post("/api/specs/<spec_id>/render")
    def specs_render(spec_id):
        """Render the saved spec into the paper repo. Optional body = the spec (validated + saved first, so a
        debounced PUT can't race the render). `?compile=1` (or body `compile: true`) also queues a full compile."""
        _spec_id(spec_id)
        spec = _mod("spec")
        body = _body()
        saved_hash = None
        want_compile = request.args.get("compile") in ("1", "true", "yes") or bool(body.pop("compile", False))
        if body.get("spec") and isinstance(body["spec"], dict):
            body = body["spec"]
        if body.get("style") or body.get("data"):      # a full spec was posted
            body.setdefault("id", spec_id)
            if body["id"] != spec_id:
                raise ApiError(400, "id mismatch", f"body id {body['id']!r} != URL id {spec_id!r}")
            errors = list(spec.validate(body) or [])
            if errors:
                raise ApiError(400, "invalid spec", "; ".join(errors), errors=errors)
            saved_hash = _spec_save(spec, body, spec_id)
            s = body
        else:
            s = spec.load(spec_id)
            errors = list(spec.validate(s) or [])
            if errors:
                raise ApiError(400, "invalid spec", "; ".join(errors), errors=errors)
        res = dict(_mod("render").render(s) or {})
        out = {k: res.get(k) for k in ("pdf", "png", "stats", "warnings", "width_pt", "height_pt")}
        out.update({k: v for k, v in res.items() if k not in out})
        out["id"] = spec_id
        if saved_hash:
            out["hash"] = saved_hash
        if s.get("output"):
            out["output"] = s["output"]
        if want_compile:
            out["job"] = jobs.runner.submit("compile", compile_job, dedupe_queued=True)
        return jsonify(out)

    # ------------------------------------------------------------------ compile & jobs
    @app.post("/api/compile")
    def compile_paper():
        _mod("latex")       # fail fast (503) if the LaTeX module is missing
        job_id = jobs.runner.submit("compile", compile_job, dedupe_queued=True)
        job = jobs.runner.get(job_id)
        return jsonify({"job": job_id, "status": job["status"] if job else "queued",
                        "position": job.get("position", 0) if job else 0}), 202

    @app.get("/api/jobs")
    def jobs_list():
        return jsonify(jobs.runner.list(request.args.get("kind") or None))

    @app.get("/api/jobs/<job_id>")
    def job_get(job_id):
        job = jobs.runner.get(job_id)
        if job is None:
            raise ApiError(404, "no such job", job_id)
        return jsonify(job)

    @app.get("/paper.pdf")
    def paper_pdf():
        return _pdf_file_response(_pdf_path(), _pdf_path().name)

    # ------------------------------------------------------------------ figures / TeX
    @app.get("/api/figures")
    def figures():
        latex = _mod("latex")
        figs = list(_call(latex.scan_figures, root=config.PAPER_ROOT) or [])
        boxes, locate_error = {}, None
        pdf = _pdf_path()
        if pdf.exists():
            try:
                boxes = _call(latex.locate_all, root=config.PAPER_ROOT) or {}
            except Exception as e:
                locate_error = f"{type(e).__name__}: {e}"
        out = []
        for f in figs:
            f = dict(f)
            f["box"] = boxes.get(f.get("fid")) if isinstance(boxes, dict) else None
            out.append(f)
        body = {"figures": out, "has_pdf": pdf.exists(),
                "pdf_mtime": pdf.stat().st_mtime if pdf.exists() else None}
        if locate_error:
            body["locate_error"] = locate_error
        return jsonify(body)

    @app.post("/api/figures/insert")
    def figures_insert():
        body = _body()
        file_rel = body.get("file_rel") or body.get("file")
        graphics_rel = body.get("graphics_rel") or body.get("graphics")
        if not graphics_rel and body.get("spec_id"):
            graphics_rel = _mod("spec").load(_spec_id(str(body["spec_id"]))).get("output")
        missing = [k for k, v in (("file_rel", file_rel), ("after_line", body.get("after_line")),
                                  ("graphics_rel", graphics_rel)) if v in (None, "")]
        if missing:
            raise ApiError(400, "missing fields", ", ".join(missing))
        try:
            after_line = int(body["after_line"])
        except (TypeError, ValueError):
            raise ApiError(400, "bad after_line", repr(body.get("after_line")))
        kwargs = {"caption": body.get("caption", ""), "label": body.get("label", ""), "root": config.PAPER_ROOT}
        if body.get("env"):
            kwargs["env"] = body["env"]
        if body.get("width"):
            kwargs["width"] = body["width"]
        fid = _call(_mod("latex").insert_figure, file_rel, after_line, graphics_rel, **kwargs)
        return jsonify({"ok": True, "fid": fid}), 201

    @app.get("/api/figures/<path:fid>/tex")
    def figure_tex_get(fid):
        return jsonify(_call(_mod("latex").read_env, _fid(fid), root=config.PAPER_ROOT))

    @app.put("/api/figures/<path:fid>/tex")
    def figure_tex_put(fid):
        fid = _fid(fid)
        body = _body()
        text = body.get("text")
        sha = body.get("expected_sha", body.get("sha"))
        if not isinstance(text, str):
            raise ApiError(400, "missing text", "body needs {text, sha}")
        if not sha:
            raise ApiError(400, "missing sha", "body needs the sha from GET .../tex (stale-file check)")
        latex = _mod("latex")
        try:
            res = _call(latex.write_env, fid, text, sha, root=config.PAPER_ROOT)
        except Exception as e:
            if type(e).__name__ != "StaleError":
                raise
            current = None
            try:
                current = _call(latex.read_env, fid, root=config.PAPER_ROOT)
            except Exception:
                pass
            raise ApiError(409, "stale", str(e) or "the file changed since it was read", current=current)
        out = {"ok": True, "fid": fid}
        if isinstance(res, dict):
            out.update(res)
        elif isinstance(res, str):
            out["new_fid"] = res
        return jsonify(out)

    @app.post("/api/context/<path:fid>")
    def context_compile(fid):
        fid = _fid(fid)
        body = _body()
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise ApiError(400, "bad text", "text must be a string")
        if text is not None and config.READONLY:
            # read-only viewers may preview the stored figure (plus live slider settings), not compile arbitrary TeX
            raise ApiError(403, "read-only", "context preview of edited TeX is disabled in read-only mode; the stored "
                                             "figure text (with the current slider settings) can still be previewed",
                           readonly=True)
        live = False
        live_spec = body.get("spec")
        if live_spec is not None:
            # Context with the *current* (unsaved, unrendered) slider state: render the spec into the cache and
            # point this figure's \includegraphics at that file in the context .tex only. The paper is untouched.
            if not isinstance(live_spec, dict):
                raise ApiError(400, "bad spec", "spec must be an object")
            sid = _spec_id(str(live_spec.get("id") or ""))
            lx = _mod("latex")
            fig = next((f for f in _call(lx.scan_figures, config.PAPER_ROOT) if f["fid"] == fid), None)
            if fig is None:
                raise ApiError(404, "not found", f"unknown figure id: {fid}")
            out_rel = PurePath(str(live_spec.get("output") or "")).with_suffix(".pdf").as_posix()
            g = next((g for g in fig.get("graphics") or [] if g.get("resolved") == out_rel), None)
            if g is not None:
                errors = list(_mod("spec").validate(live_spec) or [])
                if errors:
                    raise ApiError(400, "invalid spec", "; ".join(errors), errors=errors)
                live_pdf = Path(config.CACHE_DIR) / "context" / "live" / f"{sid}.pdf"
                _call(_mod("render").render, live_spec, out_pdf=live_pdf)
                if text is None:
                    text = _call(lx.read_env, fid, root=config.PAPER_ROOT)["text"]
                swapped = text.replace("{" + g["path"] + "}", "{live/" + live_pdf.name + "}")
                live = swapped != text
                text = swapped
        res = dict(_call(_mod("latex").compile_context, fid, text_override=text, root=config.PAPER_ROOT) or {})
        pdf = res.get("pdf")
        res["pdf_url"] = f"context/{Path(pdf).stem}.pdf" if pdf else None    # relative: works under a base path
        res["live"] = live
        return jsonify(res)

    @app.get("/context/<name>.pdf")
    def context_pdf(name):
        if not CONTEXT_NAME_RE.match(name) or ".." in name:
            raise ApiError(400, "bad name", name)
        return _pdf_file_response(config.CACHE_DIR / "context" / f"{name}.pdf", f"{name}.pdf")

    @app.post("/api/synctex/reverse")
    def synctex_reverse():
        body = _body()
        try:
            page, x, y = int(body["page"]), float(body["x"]), float(body["y"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "bad position", "body needs numeric {page, x, y} (PDF points, origin top-left)")
        res = _call(_mod("latex").reverse, page, x, y, root=config.PAPER_ROOT)
        # a click on empty margin is not an error: answer nulls (keeps the browser console clean)
        return jsonify(res if res else {"file": None, "line": None})

    @app.post("/api/synctex/forward")
    def synctex_forward():
        """Source -> page: {file, line} -> {page, box: {x, y, w, h}, page_w, page_h, line, stale} (PDF points,
        origin top-left); {page: null} when the line has no output in the compiled PDF."""
        body = _body()
        file = body.get("file")
        try:
            line = int(body["line"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "bad position", "body needs {file, line} (line: 1-based integer)")
        if not isinstance(file, str) or not file:
            raise ApiError(400, "bad position", "body needs {file, line} (file: a .tex path relative to the paper)")
        res = _call(_mod("latex").forward, file, line, root=config.PAPER_ROOT)
        if not res:
            return jsonify({"page": None, "box": None, "file": file, "line": line})
        out = {k: v for k, v in res.items() if k not in ("x", "y", "w", "h")}
        out["box"] = {k: res[k] for k in ("x", "y", "w", "h")}
        return jsonify(out)

    # ------------------------------------------------------------------ section editor (whole .tex files)
    # Only MAIN_TEX and the files it pulls in via \input/\include (latex._editable_tex refuses everything else:
    # traversal, absolute paths, symlinks out of the root, non-.tex). PUT is compare-and-swap on the file's hash.
    @app.get("/api/tex/sections")
    def tex_sections():
        return jsonify({"main_tex": config.MAIN_TEX,
                        "sections": _call(_mod("latex").tex_sections, root=config.PAPER_ROOT)})

    @app.get("/api/tex/file")
    def tex_file_get():
        path = request.args.get("path", "")
        if not path:
            raise ApiError(400, "missing path", "GET /api/tex/file?path=<file.tex relative to the paper root>")
        return jsonify(_call(_mod("latex").read_tex_file, path, root=config.PAPER_ROOT))

    @app.put("/api/tex/file")
    def tex_file_put():
        body = _body()
        path, text, base = body.get("path"), body.get("text"), body.get("base_hash")
        if not isinstance(path, str) or not path:
            raise ApiError(400, "missing path", "body needs {path, text, base_hash}")
        if not isinstance(text, str):
            raise ApiError(400, "missing text", "body needs {path, text, base_hash}")
        if not isinstance(base, str) or not base:
            raise ApiError(400, "missing base_hash", "body needs the hash from GET /api/tex/file (stale-file check)")
        latex = _mod("latex")
        try:
            res = _call(latex.write_tex_file, path, text, base, root=config.PAPER_ROOT)
        except Exception as e:
            if type(e).__name__ != "StaleError":
                raise
            current = None
            try:
                cur = _call(latex.read_tex_file, path, root=config.PAPER_ROOT)
                current = {k: cur.get(k) for k in ("path", "text", "hash", "mtime", "lines")}
            except Exception:
                pass
            raise ApiError(409, "stale", str(e) or "the file changed on disk since it was loaded", current=current)
        return jsonify({"ok": True, **(res or {})})

    # ------------------------------------------------------------------ data store
    @app.get("/api/data")
    def data_list():
        return jsonify(_mod("datastore").list_datasets())

    @app.post("/api/data")
    def data_add():
        body = _body()
        name = _data_name(str(body.get("name", "")))
        ref = body.get("ref") or body.get("path") or body.get("url")
        if not ref or not isinstance(ref, str):
            raise ApiError(400, "missing ref", "body needs {name, ref} (absolute path, file:// URL, .lnk, or symlink)")
        kwargs = {"notes": body.get("notes", "") or ""}
        if "make_link" in body:
            kwargs["make_link"] = bool(body["make_link"])
        entry = _mod("datastore").add(name, ref, **kwargs)
        return jsonify(entry), 201

    @app.get("/api/data/<name>")
    def data_get(name):
        entry = _mod("datastore").get(_data_name(name))
        if entry is None:
            raise ApiError(404, "no such dataset", name)
        return jsonify(entry)

    @app.delete("/api/data/<name>")
    def data_delete(name):
        res = _mod("datastore").remove(_data_name(name))
        out = {"ok": True, "name": name}
        if isinstance(res, dict):
            out.update(res)
        return jsonify(out)

    @app.get("/api/data/<name>/preview")
    def data_preview(name):
        try:
            rows = int(request.args.get("rows", 50))
        except ValueError:
            raise ApiError(400, "bad rows", request.args.get("rows"))
        return jsonify(_mod("datastore").preview(_data_name(name), rows=max(1, min(rows, 5000))))

    @app.post("/api/data/<name>/refresh")
    def data_refresh(name):
        return jsonify(_mod("datastore").refresh(_data_name(name)))

    # ------------------------------------------------------------------ git (never automatic)
    @app.get("/api/git/status")
    def git_status():
        return jsonify(_mod("gitops").status())

    @app.post("/api/git/fetch")
    def git_fetch():
        return jsonify(_mod("gitops").fetch())

    @app.post("/api/git/pull")
    def git_pull():
        return jsonify(_mod("gitops").pull())

    @app.post("/api/git/diff_stat")
    def git_diff_stat():
        paths = _body().get("paths") or []
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ApiError(400, "bad paths", "paths must be a list of strings")
        return jsonify(_mod("gitops").diff_stat(paths))

    @app.post("/api/git/commit")
    def git_commit():
        body = _body()
        paths, message = body.get("paths"), body.get("message")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
            raise ApiError(400, "bad paths", "paths must be a non-empty list of repo-relative paths")
        if not isinstance(message, str) or not message.strip():
            raise ApiError(400, "missing message", "a commit message is required")
        return jsonify(_mod("gitops").commit(paths, message))

    @app.post("/api/git/push")
    def git_push():
        # Only reachable from an explicit user click in the UI (the frontend confirms "Push to Overleaf?").
        return jsonify(_mod("gitops").push())

    return app


# waitress deletes X-Forwarded-Host / -Proto / -For / Forwarded from every request unless the peer is a configured
# trusted_proxy (clear_untrusted_proxy_headers defaults to True). The Origin check (_own_origins) needs the first two
# to accept same-origin writes behind a reverse proxy that rewrites Host, so keep them and let this app decide; it
# uses them only to compute its own origin (see _own_origins for why that is safe) and never for client addresses.
WAITRESS_OPTIONS = {"threads": 8, "ident": "plot-designer", "clear_untrusted_proxy_headers": False}


def run(port: int | None = None, host: str | None = None, prod: bool = False,
        allow_dev_server: bool = False) -> None:
    """Serve the app. host/port default to config.HOST / config.PORT (DESIGNER_HOST / DESIGNER_PORT).

    - A non-loopback host needs DESIGNER_TOKEN (refuses to start otherwise).
    - prod=True: waitress (threads=8) if importable; else Flask's threaded server, but only on loopback (or with
      allow_dev_server). Without prod, Flask's threaded server, again loopback-only unless allow_dev_server.
    Renders are serialized by render's own lock, LaTeX jobs by jobs.runner (one at a time, others queue)."""
    host = host or config.HOST
    port = port or config.PORT
    check_bind_security(host, config.TOKEN)
    app = create_app()
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{port}{config.BASE_PATH}/"
    print(f"Plot designer {config.VERSION}: {url}", flush=True)
    if config.TOKEN:
        print(f"  auth:       token required (sign in at {url}login with DESIGNER_TOKEN)", flush=True)
    print(f"  paper root: {config.PAPER_ROOT}", flush=True)
    print(f"  state dir:  {config.STATE_DIR}", flush=True)
    if config.READONLY:
        print("  read-only:  yes (no writes to specs, paper, registry or git)", flush=True)
    if not _latex_found():
        print("  warning:    latexmk not found (set DESIGNER_LATEX_BIN or PATH); compiles will fail", flush=True)
    if prod:
        try:
            import waitress
        except ImportError:
            waitress = None
            print("  note:       --prod asked for waitress, but it is not installed (pip install waitress); "
                  "falling back to Flask's threaded server", flush=True)
        if waitress is not None:
            import logging
            logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
            check_port_free(host, port)
            print("  server:     waitress (threads=8)", flush=True)
            waitress.serve(app, host=host, port=port, **WAITRESS_OPTIONS)
            return
    if not config.is_loopback(host) and not allow_dev_server:
        raise SystemExit(f"refusing to run Flask's development server on non-loopback {host!r}: install waitress and "
                         "use --prod, or pass --allow-dev-server if you really mean it")
    check_port_free(host, port)
    print("  server:     Flask threaded (development server)", flush=True)
    try:
        srv = exclusive_server_class()(host, port, app, handler=_redacting_request_handler())
    except OSError as e:       # lost a race with another server after check_port_free
        raise SystemExit(f"cannot bind {host}:{port}: {e}") from None
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def _probe_host(host: str) -> str:
    """Where to look for an existing server: the wildcard addresses are answered on loopback."""
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "::1"
    return host.strip("[]")


def check_port_free(host: str, port: int, timeout: float = 0.5) -> None:
    """Refuse to start when something already accepts connections on host:port.

    On Windows, werkzeug's (and socketserver's) SO_REUSEADDR lets a second server bind a port that is already
    LISTENING, with no error; requests are then split between the two processes (different tokens, paper roots,
    code). A stray `python -m designer` next to James's running server would silently share :8766 with it."""
    import socket
    probe = _probe_host(host)
    try:
        infos = socket.getaddrinfo(probe, port, type=socket.SOCK_STREAM)
    except OSError:
        return
    for family, socktype, proto, _canon, addr in infos:
        with socket.socket(family, socktype, proto) as s:
            s.settimeout(timeout)
            try:
                s.connect(addr)
            except OSError:
                continue
        raise SystemExit(f"port {port} on {host} is already in use (another designer or program answers there); "
                         f"stop it or pick another --port")


def exclusive_server_class():
    """werkzeug's ThreadedWSGIServer, but its socket can't share a port with another listener: no SO_REUSEADDR on
    Windows (where it means 'steal a listening port') and SO_EXCLUSIVEADDRUSE instead. POSIX keeps SO_REUSEADDR,
    which there only allows rebinding over TIME_WAIT and already refuses a second listener."""
    import socket
    from werkzeug.serving import ThreadedWSGIServer

    class ExclusiveThreadedWSGIServer(ThreadedWSGIServer):
        allow_reuse_address = os.name != "nt"
        allow_reuse_port = False

        def server_bind(self):
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    return ExclusiveThreadedWSGIServer


def _redacting_request_handler():
    """Werkzeug's request handler, but the access log line shows `token=***` instead of a ?token= value."""
    from werkzeug.serving import WSGIRequestHandler

    class RedactingRequestHandler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            # werkzeug logs self.path (self.requestline when the request line was malformed); the request has
            # already been handled, so rewriting them only changes the log line
            for attr in ("path", "requestline"):
                v = getattr(self, attr, None)
                if isinstance(v, str):
                    setattr(self, attr, TOKEN_QS_RE.sub(r"\1token=***", v))
            super().log_request(code, size)

    return RedactingRequestHandler
