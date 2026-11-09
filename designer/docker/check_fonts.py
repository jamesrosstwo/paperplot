"""Fail loudly unless matplotlib can find the paper fonts without falling back.

Run at image build time (Dockerfile) and useful at runtime for troubleshooting:
    docker run --rm paperplot-designer:latest python /app/designer/docker/check_fonts.py

Registers the bundled paperplot/fonts/*.ttf by path first (as paperplot.style does), then asks matplotlib for each
family with fallback_to_default=False. Exits 1 if any family is missing or resolves to a DejaVu file.
"""
import sys
from pathlib import Path

from matplotlib import font_manager

ROOT = Path(__file__).resolve().parents[2]          # /app (holds paperplot/ and designer/)
FONTS_DIR = ROOT / "paperplot" / "fonts"
WANT = ["Times New Roman", "Roboto Slab"]


def main() -> int:
    for ttf in sorted(FONTS_DIR.glob("*.ttf")):
        font_manager.fontManager.addfont(str(ttf))
    bad = 0
    for family in WANT:
        for weight in ("normal", "bold"):
            prop = font_manager.FontProperties(family=family, weight=weight)
            try:
                path = font_manager.findfont(prop, fallback_to_default=False)
            except ValueError as e:
                print(f"FONT MISSING: {family} ({weight}): {e}", file=sys.stderr)
                bad += 1
                continue
            name = font_manager.FontProperties(fname=path).get_name()
            ok = name.lower() == family.lower() and "dejavu" not in Path(path).name.lower()
            print(f"{'ok ' if ok else 'BAD'} {family:16s} {weight:6s} -> {path} ({name})")
            bad += 0 if ok else 1
    if bad:
        print(f"{bad} font check(s) failed: matplotlib would fall back to another font", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
