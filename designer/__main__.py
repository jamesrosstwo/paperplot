"""`python -m designer [flags]` — run the plot designer (see designer/README.md).

    --host H          bind address (DESIGNER_HOST; default 127.0.0.1). Not loopback => a token is required.
    --port N          port (DESIGNER_PORT; default 8766)
    --paper-root P    LaTeX project (DESIGNER_PAPER_ROOT; default on Windows: the live Overleaf clone; required elsewhere)
    --state-dir P     specs / data registry / cache (DESIGNER_STATE_DIR; default designer/)
    --base-path /x    URL prefix behind a reverse proxy (DESIGNER_BASE_PATH)
    --token T         access token (DESIGNER_TOKEN). Prefer the environment variable: argv is visible to other users.
    --readonly        DESIGNER_READONLY=1
    --latex-bin DIR   directory with latexmk / pdflatex / synctex (DESIGNER_LATEX_BIN)
    --prod            serve with waitress if installed
    --allow-dev-server  allow Flask's development server on a non-loopback address

The flags set the DESIGNER_* variables *before* designer.config is imported (config reads them at import time), so
nothing here may import designer.config, designer.server or any backend module at module level.
"""
from __future__ import annotations

import argparse
import os
import sys


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m designer",
                                description="Plot designer: design paperplot figures inside the RSS paper. "
                                            "Serves on 127.0.0.1 unless --host says otherwise (then a token is "
                                            "required).")
    p.add_argument("--host", metavar="HOST",
                   help="bind address (sets DESIGNER_HOST; default 127.0.0.1; non-loopback needs --token)")
    p.add_argument("--port", type=int, metavar="N", help="port (sets DESIGNER_PORT; default 8766)")
    p.add_argument("--paper-root", metavar="PATH",
                   help="LaTeX project to work on (sets DESIGNER_PAPER_ROOT; Windows default: the live Overleaf "
                        "clone; required on Linux/macOS)")
    p.add_argument("--state-dir", metavar="PATH",
                   help="where specs / data registry / cache live (sets DESIGNER_STATE_DIR; default: designer/)")
    p.add_argument("--base-path", metavar="PREFIX",
                   help="URL prefix when served under a reverse-proxy subpath, e.g. /designer (sets DESIGNER_BASE_PATH)")
    p.add_argument("--token", metavar="TOKEN",
                   help="access token (sets DESIGNER_TOKEN). The environment variable is safer: command lines are "
                        "visible to other users of the machine")
    p.add_argument("--readonly", action="store_true", help="read-only mode (sets DESIGNER_READONLY=1)")
    p.add_argument("--latex-bin", metavar="DIR",
                   help="directory holding latexmk / pdflatex / synctex (sets DESIGNER_LATEX_BIN)")
    p.add_argument("--prod", action="store_true", help="serve with waitress (threads=8) if it is installed")
    p.add_argument("--allow-dev-server", action="store_true",
                   help="allow Flask's development server on a non-loopback address (not recommended)")
    return p.parse_args(argv)


def apply_env(args: argparse.Namespace) -> None:
    """Export the flags as environment variables. Must run before designer.config is imported."""
    if args.paper_root:
        os.environ["DESIGNER_PAPER_ROOT"] = os.path.abspath(os.path.expanduser(args.paper_root))
    if args.state_dir:
        os.environ["DESIGNER_STATE_DIR"] = os.path.abspath(os.path.expanduser(args.state_dir))
    if args.port:
        if not (0 < args.port < 65536):
            raise SystemExit(f"--port {args.port}: out of range")
        os.environ["DESIGNER_PORT"] = str(args.port)
    if getattr(args, "host", None):
        os.environ["DESIGNER_HOST"] = args.host.strip()
    if getattr(args, "base_path", None) is not None:
        os.environ["DESIGNER_BASE_PATH"] = args.base_path
    if getattr(args, "token", None):
        os.environ["DESIGNER_TOKEN"] = args.token
    if getattr(args, "readonly", False):
        os.environ["DESIGNER_READONLY"] = "1"
    if getattr(args, "latex_bin", None):
        os.environ["DESIGNER_LATEX_BIN"] = os.path.abspath(os.path.expanduser(args.latex_bin))
    os.environ.setdefault("PYTHONUTF8", "1")       # child processes (LaTeX helpers, git) inherit UTF-8 mode
    os.environ.setdefault("MPLBACKEND", "Agg")


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):        # Windows consoles: never die on a non-cp1252 character
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
    args = parse_args(argv)
    flags = (args.paper_root, args.state_dir, args.port, args.host, args.base_path, args.token, args.readonly,
             args.latex_bin)
    if "designer.config" in sys.modules and any(flags):
        print("warning: designer.config was imported before the CLI flags were applied; they may be ignored",
              file=sys.stderr)
    apply_env(args)
    try:
        from designer import config             # imports designer.config — only now
    except Exception as e:                      # e.g. ConfigError: no paper root on Linux
        raise SystemExit(f"designer: {e}")
    from designer import server
    server.run(host=config.HOST, port=config.PORT, prod=args.prod, allow_dev_server=args.allow_dev_server)


if __name__ == "__main__":
    main()
