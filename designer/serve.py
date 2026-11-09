"""Run the plot designer from any working directory:

    python /path/to/paperplot/designer/serve.py [--host H] [--port N] [--paper-root PATH] [--state-dir PATH]
        [--base-path /prefix] [--token T] [--readonly] [--latex-bin DIR] [--prod] [--allow-dev-server]

Puts the paperplot root on sys.path (so `designer` and `paperplot` import as packages), applies the flags before
designer.config is imported, and starts the server (127.0.0.1 unless --host; see designer/README.md).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../paperplot/designer
_ROOT = os.path.dirname(_HERE)                               # .../paperplot

# Running a script puts its own directory first on sys.path; drop it so designer's modules (config, spec, latex, ...)
# are only importable as `designer.<name>` and can never shadow a top-level module.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_HERE)]
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from designer.__main__ import main  # noqa: E402  (does not import designer.config)

if __name__ == "__main__":
    main(sys.argv[1:])
