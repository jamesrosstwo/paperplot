"""python -m paperplot check [theme]   — load a theme and run the color checks."""
import sys

from . import colors, theme


def main(argv):
    if not argv or argv[0] != "check":
        print(__doc__.strip())
        return 2
    try:
        t = theme.load(argv[1] if len(argv) > 1 else "default")
    except theme.ThemeError as e:
        print(f"theme error: {e}")
        return 1
    theme.set_current(t)
    print(f"Theme '{t.name}' loaded: {len(t.swatches)} swatches, "
          f"{len(t.categorical)} categorical colors")
    ok, _ = colors.check_palette()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
