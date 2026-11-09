"""pytest setup shared by every designer test module (runs before any of them imports designer.config).

- State: always a fresh temp DESIGNER_STATE_DIR, even if one is set in the environment (a UI session's state dir
  holds edited specs, so tests that read seeds would depend on what someone dragged in the browser).
- Paper: the disposable clone named by DESIGNER_TEST_PAPER (else DESIGNER_PAPER_ROOT, else this build's scratch
  clone). Never the live Overleaf clone. Tests that need a real paper skip when it does not exist.
"""
import os
import sys
import tempfile
from pathlib import Path

LIVE = Path(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf").resolve()
_DEFAULT_SCRATCH = (r"C:\Users\James\AppData\Local\Temp\claude\9119a02420dddb37\7a66152e-6919-48a8-b3a9-c4911b85a0e4"
                    r"\scratchpad\designer_test\paper")


def _pick_paper() -> str:
    for cand in (os.environ.get("DESIGNER_TEST_PAPER"), os.environ.get("DESIGNER_PAPER_ROOT"), _DEFAULT_SCRATCH):
        if cand and Path(cand).resolve() != LIVE and (Path(cand) / "paper.tex").is_file():
            return str(Path(cand).resolve())
    return tempfile.mkdtemp(prefix="designer_no_paper_")  # tests needing the paper skip on this


if "designer.config" not in sys.modules:
    paper = _pick_paper()
    os.environ["DESIGNER_TEST_PAPER"] = paper
    os.environ["DESIGNER_PAPER_ROOT"] = paper
    os.environ["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="designer_test_state_")
    os.environ.setdefault("MPLBACKEND", "Agg")
    # a known deployment config: the tokenless loopback local tool (the Docker image sets DESIGNER_HOST=0.0.0.0, and
    # a shell may carry a token); tests that need a token, read-only mode or data roots patch designer.config
    os.environ["DESIGNER_HOST"] = "127.0.0.1"
    for _v in ("DESIGNER_TOKEN", "DESIGNER_READONLY", "DESIGNER_BASE_PATH", "DESIGNER_DATA_ROOTS", "DESIGNER_DEBUG"):
        os.environ.pop(_v, None)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
