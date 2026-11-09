"""Cross-builder wiring (2026-11-09): the spec features added by the plots builder (stacked panels, arm order, dup
flags, the lines kind, group colors, render notes) are reachable from the UI built by the UI builder.

Backend parts run through the Flask test client; frontend parts are static checks on designer/static (no browser
here; the browser journey is done by hand on a scratch server).

Run:  python -m pytest designer/tests/test_integration.py
"""
import copy
import json
import re
from pathlib import Path
from urllib.parse import unquote

from designer import server, spec

STATIC = Path(__file__).resolve().parents[1] / "static"
SEEDS = Path(__file__).resolve().parents[1] / "seeds"
APP = server.create_app()
APP.testing = True


def _client():
    c = APP.test_client()
    c.environ_base["HTTP_X_DESIGNER"] = "1"
    return c


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _panels_seed():
    import yaml
    return yaml.safe_load((SEEDS / "mug_baselines_panels.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- backend
def test_controls_kind_param_and_rows_for_every_kind():
    c = _client()
    r = c.get("/api/controls?kind=lines")
    assert r.status_code == 200, r.data
    j = r.get_json()
    assert j["kind"] == "lines"
    assert {row["key"] for row in j["style"]} == set(spec.style_keys("lines"))
    assert set(j["style_by_kind"]) == set(spec.KINDS)
    assert j["groups"] == spec.GROUPS
    # the UI's arm-order / group-color editors are keyed on these rows
    keys = {row["key"]: row for row in j["style_by_kind"]["grouped_bars"]}
    assert keys["arm_order"]["type"] == "json" and keys["level_colors"]["type"] == "json"


def test_preview_sends_render_notes_header_for_dup_cells():
    s = _panels_seed()
    r = _client().post("/api/preview?dpi=60", json=s)
    assert r.status_code == 200, r.data[:300]
    notes = json.loads(unquote(r.headers["X-Render-Notes"]))
    assert notes == [spec.DUP_NOTE]
    # no dup flag anywhere -> no note
    s2 = copy.deepcopy(s)
    for p in s2["data"]["panels"]:
        for a in p["arms"]:
            a["counts"] = [c[:2] if isinstance(c, list) else c for c in a["counts"]]
    r2 = _client().post("/api/preview?dpi=60", json=s2)
    assert r2.status_code == 200, r2.data[:300]
    assert json.loads(unquote(r2.headers["X-Render-Notes"])) == []


def test_saved_yaml_has_no_anchors():
    """One shared {dup: true} dict (as YAML anchors load) is written out in full, never as &id001 / *id001."""
    flag = {"dup": True}
    s = {"id": "anchors", "kind": "lines", "output": "figures/x.pdf",
         "data": {"source": "inline", "levels": ["A", "B"],
                  "arms": [{"name": "m", "color": "tol-blue", "counts": [[1, 2, flag], [2, 2, flag]]}]},
         "style": spec.defaults("lines")}
    out = spec.dumps(s)
    assert "&id" not in out and "*id" not in out
    assert out.count("{dup: true}") == 2


def test_panel_editor_shapes_validate():
    """The shapes the panel editor writes: a new panel lists every shared arm with null counts; split / flatten."""
    s = _panels_seed()
    d = s["data"]
    d["panels"].append({"title": "Panel 4", "arms": [{"name": a["name"], "counts": [None] * len(d["levels"])} for a in d["arms"]]})
    assert spec.validate(s) == []
    # an empty panel is what the editor must never write
    bad = copy.deepcopy(s)
    bad["data"]["panels"][-1]["arms"] = []
    assert any("non-empty" in e for e in spec.validate(bad))
    # rename of a shared arm carried into the panels and arm_order (as DataEditor._renameArm does)
    old = d["arms"][1]["name"]
    s["style"] = dict(spec.defaults(), arm_order={"fixed": [old], "rest": "manual", "manual": [a["name"] for a in d["arms"][2:]]})
    for p in d["panels"]:
        for a in p["arms"]:
            if a["name"] == old:
                a["name"] = "Renamed"
    d["arms"][1]["name"] = "Renamed"
    s["style"]["arm_order"]["fixed"] = ["Renamed"]
    assert spec.validate(s) == []


# ---------------------------------------------------------------- frontend wiring (static)
def test_app_builds_controls_for_the_spec_kind():
    app = _read("app.js")
    assert "style_by_kind" in app and "kindRows(ctl, spec)" in app
    assert "controls.build(ctl.style," not in app, "a lines spec would get the bar controls"
    assert "getData: specDataSummary" in app
    assert "controls.refreshData()" in app          # data edits redraw the arm-order / group-color editors
    assert "async function changeKind(" in app and "defaults_by_kind" in app


def test_stats_show_notes_panel_and_dup():
    app = _read("app.js")
    for needle in ("notesBlock(", "s.panel", "s.dup", "flag.dup", "flag.partial", "S.preview.notes"):
        assert needle in app, needle
    api = _read("api.js")
    assert "X-Render-Notes" in api and "blob.notes" in api


def test_overlay_converts_tex_pt_to_pdf_bp():
    """width_pt is in TeX pt; synctex boxes are in bp. scale=1.25 figures must use 72/72.27."""
    app = _read("app.js")
    assert re.search(r"72\s*/\s*72\.27", app)
    assert "overlayRect(fig.box, g.options, p.w * BP_PER_PT, p.h * BP_PER_PT)" in app


def test_count_cells_keep_the_dup_flag():
    ctl = _read("controls.js")
    assert "arm.counts[li] = [kv, nv]" not in ctl, "editing a count would drop {dup: true}"
    assert "makeCount(kv, nv" in ctl and "cnt-dup" in ctl
    assert "export function countParts" in ctl


def test_panel_editor_and_structured_editors_exist():
    ctl = _read("controls.js")
    for needle in ("_panels(d)", "Split into panels", "Back to one chart", "+ panel", "_renameArm(", "_forgetArm(",
                   "_armOrderEditor(", "_levelColorsEditor(", "export function effectiveArmOrder"):
        assert needle in ctl, needle
    m = re.search(r"const GROUP_ORDER = \[([^\]]*)\]", ctl)
    assert m and all(g in m.group(1) for g in ("'Panels'", "'Lines'", "'Order'"))
    # DOM replaceChildren turns a null argument into the text "null"
    assert not re.search(r"replaceChildren\([^;]*\?\s*null\s*:", ctl)


def test_effective_arm_order_matches_render():
    """The JS order shown in the arm-order editor mirrors render.arm_order for the panel seed (pooled, ties exact)."""
    from designer import render
    s = _panels_seed()
    names = [a["name"] for a in s["data"]["arms"]]
    cells = {n: [] for n in names}
    for p in s["data"]["panels"]:
        for a in p["arms"]:
            for c in a["counts"]:
                parts = spec.count_parts(c)
                if parts:
                    cells[a["name"]].append(parts[:2])
    ao = {"fixed": names[:2], "rest": "pooled"}
    order = render.arm_order({"arm_order": ao}, names, [cells[n] for n in names])
    # the JS port, evaluated by hand with the same integer cross-multiplication rule
    fixed = names[:2]
    rest = [n for n in names if n not in fixed]

    def key(n):
        cs = cells[n]
        if not cs:
            return (1, 0, 1)
        return (0, -sum(k for k, _ in cs), sum(n_ for _, n_ in cs))
    from fractions import Fraction
    rest_sorted = sorted(rest, key=lambda n: (key(n)[0], Fraction(key(n)[1], key(n)[2])))
    assert [names[i] for i in order] == fixed + rest_sorted


def test_modal_never_focuses_a_destructive_button():
    ui = _read("ui.js")
    assert "btn-danger" in ui and "safe[safe.length - 1]" in ui
