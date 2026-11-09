"""Tests for designer/spec.py. Run: python -m pytest designer/tests/test_spec.py  (or python designer/tests/test_spec.py).

Uses a fresh temporary state dir; never touches the live Overleaf clone."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # Desktop/paperplot
sys.path.insert(0, str(ROOT))
LIVE = Path(r"C:\Users\James\Desktop\4D_Curricula_RSS_overleaf").resolve()
SCRATCH_PAPER = os.environ.get("DESIGNER_TEST_PAPER") or tempfile.mkdtemp(prefix="designer_paper_")  # see conftest.py
if "designer.config" not in sys.modules:
    os.environ.setdefault("DESIGNER_PAPER_ROOT", SCRATCH_PAPER)
    os.environ["DESIGNER_STATE_DIR"] = tempfile.mkdtemp(prefix="designer_spec_test_")

import yaml  # noqa: E402

from designer import config, spec  # noqa: E402

assert config.PAPER_ROOT != LIVE, "tests must not run against the live Overleaf clone"
SEED = ROOT / "designer" / "seeds" / "mug_ours_auxq2_levels_short.yaml"
SEED2 = ROOT / "designer" / "seeds" / "mug_difficulty_dataset.yaml"


def seed():
    return yaml.safe_load(SEED.read_text(encoding="utf-8"))


def test_defaults_match_controls():
    d = spec.defaults()
    c = spec.controls()
    keys = [row["key"] for row in c["style"]]
    assert keys == list(d), "every default style key has exactly one control, in the same order"
    for k in ("theme", "font_family", "width_pt", "height_pt", "group_width", "bar_fill", "legend_loc", "legend_cols",
              "legend_fontsize", "tick_fontsize", "xtick_fontsize", "ylabel", "ylabel_fontsize", "y_mode", "out_of",
              "ytick_step", "ci", "ci_level", "ci_color", "ci_linewidth", "ci_capsize", "partial_below",
              "partial_ci_color", "divider_before", "fill_darken", "edge_darken", "edge_width", "palette", "na_label",
              "transparent"):
        assert k in d, k
    assert spec.validate({**seed(), "style": d}) == []


def test_controls_shape_and_discovery():
    c = spec.controls()
    groups = {"Theme", "Size", "Panels", "Bars", "Lines", "Order", "Axes", "Legend", "Error bars", "Colors"}
    types = {"slider", "dropdown", "number", "text", "color", "toggle", "json"}
    by = {row["key"]: row for row in c["style"]}
    for row in c["style"]:
        assert row["group"] in groups and row["type"] in types, row
        for k in ("key", "label", "type", "min", "max", "step", "options", "nullable", "help", "group", "default"):
            assert k in row, (row["key"], k)
        if row["type"] == "slider":
            assert row["min"] < row["max"] and row["step"] > 0
            if row["default"] is not None:
                assert row["min"] <= row["default"] <= row["max"], row
        if row["type"] in ("dropdown", "color"):
            assert row["options"], row["key"]
    assert by["width_pt"]["min"] == 120 and by["width_pt"]["max"] == 516
    assert {o["value"] for o in by["theme"]["options"]} >= {"acme", "acme_tnr", "default"}
    fonts = [o["value"] for o in by["font_family"]["options"]]
    assert fonts[0] is None and by["font_family"]["nullable"]
    present = [f for f in spec.PREFERRED_FONTS if f in fonts]
    assert fonts[1:1 + len(present)] == present, "common paper fonts come first"
    assert "Roboto Slab" in fonts and "DejaVu Serif" in fonts
    assert len(set(fonts[1:])) == len(fonts) - 1, "fonts deduplicated"
    pals = {o["value"] for o in by["palette"]["options"]}
    assert None in pals and "categorical" in pals
    assert any(o["label"] == "tol-blue" and o["hex"] == "#4477AA" for o in by["ci_color"]["options"])
    assert c["swatches"]["tol-red"] == "#EE6677"
    # theme-dependent options are discovered for the theme asked for
    d = spec.controls(theme="default")
    own = spec._load_theme("default").swatches
    assert "navy" in d["swatches"] and list(d["swatches"])[:len(own)] == list(own)   # the theme's own swatches first
    # acme swatch names stay usable under another theme (both seed specs use them)
    assert d["swatches"]["tol-blue"] == "#4477AA"
    s = seed()
    s["style"]["theme"] = "default"
    assert spec.validate(s) == [], spec.validate(s)
    fonts = [o["value"] for o in by["font_family"]["options"]]
    assert not {"Webdings", "Wingdings", "Segoe MDL2 Assets", "cmex10", "cmsy10"} & set(fonts)
    assert by["theme"]["group"] == by["font_family"]["group"] == "Theme"
    assert d["palettes"]["categorical"][0] == "#385F96"
    assert spec.controls(theme="nope")["theme"] == "acme"


def test_swatches():
    sw = spec.swatches("acme")
    assert sw["tol-blue"] == "#4477AA" and sw["tol-yellow"] == "#CCBB44"


def test_validate_seed_and_errors():
    assert spec.validate(seed()) == []
    assert spec.validate(yaml.safe_load(SEED2.read_text(encoding="utf-8"))) == []

    def errs(mut):
        s = seed()
        mut(s)
        return spec.validate(s)

    assert any("id" in e for e in errs(lambda s: s.update(id="Bad-Id")))
    assert any("counts" in e for e in errs(lambda s: s["data"]["arms"][0].update(counts=[[1, 2]])))
    assert any("outside 0..20" in e for e in errs(lambda s: s["data"]["arms"][0].update(counts=[[21, 20], None, None])))
    assert any("trials" in e for e in errs(lambda s: s["data"]["arms"][0].update(counts=[[0, 0], None, None])))
    assert any("color" in e for e in errs(lambda s: s["data"]["arms"][0].update(color="not-a-swatch")))
    assert errs(lambda s: s["data"]["arms"][0].update(color="#123456")) == []
    assert any("outside" in e for e in errs(lambda s: s["style"].update(width_pt=1000)))
    assert any("unknown style key" in e for e in errs(lambda s: s["style"].update(bogus=1)))
    assert any("theme" in e for e in errs(lambda s: s["style"].update(theme="nope")))
    assert any("palette" in e for e in errs(lambda s: s["style"].update(palette="nope")))
    assert errs(lambda s: s["style"].update(palette="categorical")) == []
    assert any("ci_level" in e for e in errs(lambda s: s["style"].update(ci_level=0.5)))
    assert any("legend_loc" in e for e in errs(lambda s: s["style"].update(legend_loc="below")))
    assert any("null" in e for e in errs(lambda s: s["style"].update(width_pt=None)))
    assert errs(lambda s: s["style"].update(edge_width=None, tick_fontsize=None, partial_below=None)) == []
    assert any("output" in e for e in errs(lambda s: s.update(output="../evil.pdf")))
    assert any("output" in e for e in errs(lambda s: s.update(output="C:/evil.pdf")))
    assert any("output" in e for e in errs(lambda s: s.update(output="figures/x.png")))
    assert any("duplicate" in e for e in errs(lambda s: s["data"]["arms"][1].update(name="Ours")))
    assert any("hatch" in e for e in errs(lambda s: s["data"]["arms"][1].update(hatch="abc")))
    assert errs(lambda s: s["data"]["arms"][1].update(hatch="...")) == []
    assert any("unknown top-level" in e for e in errs(lambda s: s.update(extra=1)))
    assert errs(lambda s: s.update(_ui_selected=True)) == []
    d = yaml.safe_load(SEED2.read_text(encoding="utf-8"))
    del d["data"]["query"]["trials"]
    assert any("query.trials" in e for e in spec.validate(d))
    assert spec.validate("nope") == ["spec must be a mapping"]


def test_crud_roundtrip():
    sid = "unit_test_fig"
    if (config.SPECS_DIR / f"{sid}.yaml").exists():
        spec.delete(sid)
    s = spec.create(sid)
    assert s["id"] == sid and s["output"] == f"figures/evals/{sid}.pdf"
    assert set(s["style"]) == set(spec.defaults())
    assert any(x["id"] == sid for x in spec.list_specs())
    try:
        spec.create(sid)
        raise AssertionError("create of an existing id must fail")
    except FileExistsError:
        pass
    s["style"]["group_width"] = 0.5
    s["data"]["arms"][0]["counts"] = [None, [3, 7]]
    s["_ui"] = "dropped"
    spec.save(s)
    back = spec.load(sid)
    assert back["style"]["group_width"] == 0.5 and back["data"]["arms"][0]["counts"] == [None, [3, 7]]
    assert "_ui" not in back
    text = (config.SPECS_DIR / f"{sid}.yaml").read_text(encoding="utf-8")
    top = [line.split(":")[0] for line in text.splitlines() if line and not line.startswith((" ", "-", "#"))]
    assert top == ["id", "kind", "title", "output", "data", "style"], top
    assert "counts: [null, [3, 7]]" in text, "counts written inline"
    style_keys = [l.strip().split(":")[0] for l in text.split("style:\n")[1].splitlines()]
    assert style_keys == list(spec.defaults()), "style keys in stable order"
    bad = dict(back, style=dict(back["style"], width_pt=5))
    try:
        spec.save(bad)
        raise AssertionError("invalid spec must not save")
    except spec.SpecError as e:
        assert e.errors and "width_pt" in e.errors[0]
    assert spec.load(sid)["style"]["width_pt"] == back["style"]["width_pt"], "failed save leaves the file alone"
    # partial style on disk is completed from defaults on load
    (config.SPECS_DIR / f"{sid}.yaml").write_text(yaml.safe_dump({**{k: back[k] for k in ("id", "kind", "output", "data")},
                                                                   "style": {"height_pt": 99}}), encoding="utf-8")
    loaded = spec.load(sid)
    assert loaded["style"]["height_pt"] == 99 and loaded["style"]["bar_fill"] == spec.defaults()["bar_fill"]
    copy_id = sid + "_copy"
    if (config.SPECS_DIR / f"{copy_id}.yaml").exists():
        spec.delete(copy_id)
    c = spec.create(copy_id, template_id="mug_ours_auxq2_levels_short")
    assert c["data"] == seed()["data"] and c["output"] == f"figures/evals/{copy_id}.pdf"
    spec.delete(copy_id)
    spec.delete(sid)
    assert not any(x["id"] == sid for x in spec.list_specs())
    for bad_id in ("../x", "A", ""):
        try:
            spec.load(bad_id)
            raise AssertionError(bad_id)
        except ValueError:
            pass


def test_seeds_copied_into_state_dir():
    ids = {x["id"] for x in spec.list_specs()}
    if config.SPECS_DIR.resolve() != spec.SEED_DIR.resolve():
        assert (config.SPECS_DIR / ".seeded").exists()
    assert {"mug_ours_auxq2_levels_short", "mug_difficulty_dataset"} <= ids
    # compare with the seed file itself, not the state dir (a UI session may have edited the loaded copy)
    s = seed()
    assert s["style"]["width_pt"] == 252 and s["style"]["group_width"] == 0.66
    assert spec.load("mug_ours_auxq2_levels_short")["id"] == s["id"]


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
