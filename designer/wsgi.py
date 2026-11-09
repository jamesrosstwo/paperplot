"""WSGI entry point: `app` for any WSGI server (configuration comes from the DESIGNER_* environment variables).

    waitress-serve --host 127.0.0.1 --port 8766 --threads 8 --no-clear-untrusted-proxy-headers designer.wsgi:app
    gunicorn -w 1 --threads 8 -b 127.0.0.1:8766 designer.wsgi:app                  (both from the paperplot folder)

Use ONE worker process (threads are fine): compile jobs, the render lock and the job table live in the process.

Behind a reverse proxy that rewrites Host, the browser's writes are checked against X-Forwarded-Host/-Proto. waitress
deletes those headers unless told not to (`--no-clear-untrusted-proxy-headers`, or trusted_proxy settings); without
that every POST/PUT through the proxy gets 403 "forbidden origin". serve.py --prod does this itself
(server.WAITRESS_OPTIONS).

A WSGI server's bind address is invisible to the app, so this module refuses to build the app without DESIGNER_TOKEN
unless DESIGNER_HOST is explicitly set to a loopback address (i.e. you promise the server binds loopback only).
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("PYTHONUTF8", "1")

from designer import config, server  # noqa: E402

if not config.TOKEN and not (os.environ.get("DESIGNER_HOST") and config.is_loopback(config.HOST)):
    raise RuntimeError("designer.wsgi: set DESIGNER_TOKEN (or set DESIGNER_HOST=127.0.0.1 and bind the WSGI server "
                       "to loopback only)")
server.check_bind_security(config.HOST, config.TOKEN)

app = server.create_app()
application = app
