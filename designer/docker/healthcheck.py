"""Docker HEALTHCHECK: GET /healthz (no auth) on the local server; exit 0 if it answers {"ok": true}.

Tries <DESIGNER_BASE_PATH>/healthz first (in case the app mounts everything under the base path), then /healthz.
"""
import json
import os
import sys
import urllib.request

port = os.environ.get("DESIGNER_PORT", "8766")
base = os.environ.get("DESIGNER_BASE_PATH", "").strip().rstrip("/")
if base and not base.startswith("/"):
    base = "/" + base

paths = [f"{base}/healthz"] if base else []
paths.append("/healthz")

for path in paths:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=4) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        if r.status == 200 and body.get("ok", True):
            sys.exit(0)
    except Exception as e:  # noqa: BLE001  (any failure = try the next path / unhealthy)
        print(f"{path}: {e}", file=sys.stderr)
sys.exit(1)
