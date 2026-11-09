"""Tests for designer.datastore. Runs under pytest or as a plain script:

    PYTHONUTF8=1 C:/ProgramData/anaconda3/python.exe designer/tests/test_datastore.py

Every test points the registry and links folder at a fresh temp dir (never the real designer state).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .../paperplot
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESIGNER_STATE_DIR", tempfile.mkdtemp(prefix="designer_state_"))

from designer import config, datastore  # noqa: E402
from designer.datastore import DataStoreError, DatasetNotFound  # noqa: E402

_TMPS = []
_ORIG = (config.REGISTRY_PATH, config.LINKS_DIR)  # restored in teardown so other test modules see real paths
IS_WIN = os.name == "nt"


def _is_dir_link(p) -> bool:
    """The kind of directory link the data store makes: a junction on Windows, a symlink on POSIX."""
    return os.path.isjunction(p) if IS_WIN else os.path.islink(p)


def _make_dir_link(link: Path, target: Path) -> None:
    """A pre-existing directory link made outside the data store (junction on Windows, symlink on POSIX)."""
    if IS_WIN:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def fresh() -> Path:
    """New temp tree with state/ (registry + links) and 'src data/' fixtures. Patches config paths to it."""
    t = Path(tempfile.mkdtemp(prefix="ds_test_"))
    _TMPS.append(t)
    state = t / "state"
    config.REGISTRY_PATH = state / "data" / "registry.json"
    config.LINKS_DIR = state / "data" / "links"
    config.LINKS_DIR.mkdir(parents=True)
    src = t / "src data"  # a space on purpose
    (src / "episodes dir" / "sub").mkdir(parents=True)
    (src / "episodes dir" / "a.txt").write_bytes(("hello\n").encode("utf-8"))
    (src / "episodes dir" / "sub" / "b.tsv").write_bytes(("x\ty\n1\t2\n3\t4\n").encode("utf-8"))
    (src / "evals.tsv").write_bytes(("task\tarm\tk\tn\nmug\tbase\t16\t20\nmug\tours\t20\t20\n").encode("utf-8"))
    (src / "evals.csv").write_bytes(("task,arm,k,n\nmug,base,16,20\n").encode("utf-8"))
    (src / "rec.json").write_bytes((json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])).encode("utf-8"))
    (src / "tables.json").write_bytes(json.dumps({"main": [{"a": 1}], "other": [{"a": 2}, {"a": 3}]}).encode())
    (src / "rows.yaml").write_bytes(b"- {level: Easy, k: 3}\n- {level: Hard, k: 1}\n")
    (src / "notes.md").write_bytes(("# notes\nline two\n").encode("utf-8"))
    (src / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return t


def _links_only(d: Path):
    """Every entry directly in LINKS_DIR must be a link (proves nothing was copied there)."""
    for p in d.iterdir():
        assert os.path.islink(p) or os.path.isjunction(p), f"{p} is not a link -- data was copied?"


# ------------------------------------------------------------------------------------------------------------ add
def test_junction_with_shell_metacharacters_runs_nothing():
    """A folder name with & , ; ^ % must link fine and never be interpreted by a shell (no cmd /c mklink)."""
    t = fresh()
    target = t / "R&D,x;y^z%PATH%&md PWNED"
    target.mkdir()
    (target / "f.txt").write_bytes(b"x")
    cwd = os.getcwd()
    work = t / "cwd"
    work.mkdir()
    os.chdir(work)
    try:
        e = datastore.add("meta", str(target))
    finally:
        os.chdir(cwd)
    assert e["link"] and _is_dir_link(e["link"]), e
    assert "warning" not in e, e
    assert os.listdir(e["link"]) == ["f.txt"]
    assert os.listdir(work) == [], "a shell ran part of the folder name as a command"
    datastore.remove("meta")
    assert target.is_dir() and (target / "f.txt").exists()


def test_add_dir_makes_junction_and_remove_keeps_target():
    t = fresh()
    target = t / "src data" / "episodes dir"
    e = datastore.add("episodes", str(target), notes="n1")
    assert e["kind"] == "dir" and e["path"] == str(target.resolve())
    assert e["link"] and _is_dir_link(e["link"]), "directory link (junction / symlink) was not created"
    assert Path(e["link"]).parent == config.LINKS_DIR
    assert sorted(os.listdir(e["link"])) == sorted(os.listdir(target))
    assert e["size_bytes"] == len("hello\n") + len("x\ty\n1\t2\n3\t4\n") and e["size_partial"] is False
    assert "warning" not in e
    _links_only(config.LINKS_DIR)
    reg = json.loads(config.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert [r["name"] for r in reg] == ["episodes"]
    r = datastore.remove("episodes")
    assert r["link_removed"] == e["link"] and r["target_exists"] is True
    assert not os.path.lexists(e["link"])
    assert (target / "a.txt").read_text(encoding="utf-8") == "hello\n"  # target untouched
    assert (target / "sub" / "b.tsv").exists()
    assert datastore.list_datasets() == []


def test_add_file_symlink_and_remove_keeps_target():
    t = fresh()
    target = t / "src data" / "evals.tsv"
    e = datastore.add("evals", str(target))
    assert e["kind"] == "tsv" and e["size_bytes"] == target.stat().st_size
    if e["link"]:
        assert os.path.islink(e["link"]) and e["link"].endswith("evals.tsv")
        assert Path(e["link"]).read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
        print("  file symlink: created (Developer Mode / privilege allows os.symlink)")
    else:
        assert "Developer Mode" in e["warning"] or "symlink" in e["warning"]
        print("  file symlink: refused by Windows ->", e["warning"])
    datastore.remove("evals")
    assert target.exists() and target.read_text(encoding="utf-8").startswith("task\t")


def test_symlink_refused_falls_back_to_reference():
    t = fresh()
    real = os.symlink

    def deny(*a, **k):
        err = OSError(None, "A required privilege is not held by the client", None, 1314)
        raise err
    os.symlink = deny
    try:
        e = datastore.add("evals", str(t / "src data" / "evals.csv"))
    finally:
        os.symlink = real
    assert e["link"] is None and ("Developer Mode" if IS_WIN else "os.symlink failed") in e["warning"], e
    assert list(config.LINKS_DIR.iterdir()) == []  # no copy, no hard link
    assert datastore.get("evals")["path"] == str((t / "src data" / "evals.csv").resolve())


def test_make_link_false():
    t = fresh()
    e = datastore.add("d", str(t / "src data" / "episodes dir"), make_link=False)
    assert e["link"] is None and list(config.LINKS_DIR.iterdir()) == []


def test_file_url_ref():
    t = fresh()
    target = (t / "src data" / "evals.tsv").resolve()
    url = target.as_uri()  # file:///C:/.../src%20data/evals.tsv
    assert "%20" in url
    e = datastore.add("viaurl", url)
    assert e["path"] == str(target)
    e2 = datastore.add("viaurl2", '"' + str(target) + '"')  # pasted with quotes
    assert e2["path"] == str(target)


def test_lnk_shortcut_ref():
    t = fresh()
    if not IS_WIN:                                  # .lnk shortcuts are resolved only on Windows
        lnk = t / "src data" / "shortcut.lnk"
        lnk.write_bytes(b"L-not-a-shortcut, not resolved on POSIX")
        e = datastore.add("vialnk", str(lnk), make_link=False)
        assert e["path"] == str(lnk.resolve()) and e["kind"] != "dir", e
        return
    target = (t / "src data" / "episodes dir").resolve()
    lnk = t / "shortcut to episodes.lnk"
    env = dict(os.environ, T_LNK=str(lnk), T_TGT=str(target))
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:T_LNK); "
          "$s.TargetPath=$env:T_TGT; $s.Save()")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], check=True, env=env,
                   capture_output=True, timeout=60)
    assert lnk.exists()
    e = datastore.add("vialnk", str(lnk))
    assert e["path"] == str(target) and e["kind"] == "dir"


def test_existing_symlink_and_junction_refs_resolve():
    t = fresh()
    target_dir = (t / "src data" / "episodes dir").resolve()
    j = t / "my junction"
    _make_dir_link(j, target_dir)
    e = datastore.add("viajunction", str(j))
    assert e["path"] == str(target_dir)
    target_file = (t / "src data" / "rec.json").resolve()
    s = t / "rec link.json"
    try:
        os.symlink(str(target_file), str(s))
    except OSError:
        print("  (skipping file-symlink ref: os.symlink not permitted)")
        return
    e2 = datastore.add("viasymlink", str(s))
    assert e2["path"] == str(target_file) and e2["kind"] == "json"


def test_bad_refs_and_names():
    t = fresh()
    for bad in ["", "relative/path.tsv", str(t / "does not exist.tsv"), "file:///C:/no/such/file.tsv"]:
        try:
            datastore.add("x", bad)
        except DataStoreError:
            pass
        else:
            raise AssertionError(f"accepted bad ref {bad!r}")
    for bad_name in ["", "../evil", "a/b", "with space", "-dash", "x" * 81]:
        try:
            datastore.add(bad_name, str(t / "src data" / "evals.tsv"))
        except DataStoreError:
            pass
        else:
            raise AssertionError(f"accepted bad name {bad_name!r}")
    datastore.add("ok", str(t / "src data" / "evals.tsv"))
    try:
        datastore.add("ok", str(t / "src data" / "evals.csv"))
    except DataStoreError:
        pass
    else:
        raise AssertionError("duplicate name accepted")
    # a ref that resolves into LINKS_DIR itself is refused
    try:
        datastore.add("loop", str(config.LINKS_DIR))
    except DataStoreError:
        pass
    else:
        raise AssertionError("LINKS_DIR accepted as a dataset")
    try:
        datastore.get("nope")
    except DatasetNotFound:
        pass
    else:
        raise AssertionError("unknown name did not raise")


# ------------------------------------------------------------------------------------------------------------ remove safety
def test_remove_refuses_link_outside_links_dir():
    t = fresh()
    target = t / "src data" / "episodes dir"
    datastore.add("eps", str(target), make_link=False)
    outside = t / "outside junction"
    _make_dir_link(outside, target)
    reg = json.loads(config.REGISTRY_PATH.read_text(encoding="utf-8"))
    reg[0]["link"] = str(outside)  # tampered registry
    config.REGISTRY_PATH.write_text(json.dumps(reg), encoding="utf-8")
    try:
        datastore.remove("eps")
    except DataStoreError as ex:
        assert "outside" in str(ex)
    else:
        raise AssertionError("remove() accepted a link outside LINKS_DIR")
    assert _is_dir_link(outside) and (target / "a.txt").exists()
    assert datastore.get("eps")["name"] == "eps"  # registry unchanged
    # a registry pointing the link at the target itself (inside nothing) must not delete the target either
    reg[0]["link"] = str(target)
    config.REGISTRY_PATH.write_text(json.dumps(reg), encoding="utf-8")
    try:
        datastore.remove("eps")
    except DataStoreError:
        pass
    else:
        raise AssertionError("remove() accepted the target as its link")
    assert (target / "a.txt").exists() and (target / "sub" / "b.tsv").exists()


def test_remove_refuses_real_dir_at_link_path():
    t = fresh()
    datastore.add("eps", str(t / "src data" / "episodes dir"), make_link=False)
    fake = config.LINKS_DIR / "eps"
    fake.mkdir()
    (fake / "keep.txt").write_text("k", encoding="utf-8")
    reg = json.loads(config.REGISTRY_PATH.read_text(encoding="utf-8"))
    reg[0]["link"] = str(fake)
    config.REGISTRY_PATH.write_text(json.dumps(reg), encoding="utf-8")
    try:
        datastore.remove("eps")
    except DataStoreError:
        pass
    else:
        raise AssertionError("remove() would have deleted a real directory")
    assert (fake / "keep.txt").exists()


def test_refresh_flags_missing_and_list():
    t = fresh()
    victim = t / "src data" / "notes.md"
    datastore.add("notes", str(victim), make_link=False)
    assert datastore.refresh("notes")["missing"] is False
    victim.unlink()  # our own temp fixture
    r = datastore.refresh("notes")
    assert r["missing"] is True
    assert datastore.list_datasets()[0]["missing"] is True
    assert datastore.get("notes")["name"] == "notes"  # entry kept


# ------------------------------------------------------------------------------------------------------------ preview / load
def test_previews():
    t = fresh()
    s = t / "src data"
    for name, f in [("tsv", "evals.tsv"), ("csv", "evals.csv"), ("json", "rec.json"), ("yaml", "rows.yaml"),
                    ("text", "notes.md"), ("bin", "blob.bin"), ("dir", "episodes dir")]:
        datastore.add(name, str(s / f))
    p = datastore.preview("tsv")
    assert p["kind"] == "tsv" and p["columns"] == ["task", "arm", "k", "n"] and p["rows"][0] == ["mug", "base", 16, 20]
    assert len(datastore.preview("tsv", rows=1)["rows"]) == 1
    p = datastore.preview("csv")
    assert p["kind"] == "csv" and p["rows"] == [["mug", "base", 16, 20]]
    p = datastore.preview("json")
    assert p["kind"] == "json" and '"a": 1' in p["head"] and p["columns"] == ["a", "b"]
    p = datastore.preview("yaml")
    assert p["kind"] == "yaml" and "level" in p["head"] and p["rows"][1] == ["Hard", 1]
    p = datastore.preview("text")
    assert p["kind"] == "text" and p["head"].startswith("# notes")
    assert datastore.preview("bin")["kind"] == "binary"
    p = datastore.preview("dir")
    assert p["kind"] == "dir" and [x["name"] for x in p["entries"]] == ["sub", "a.txt"]
    assert p["entries"][1]["size_bytes"] == 6 and p["entries"][0]["is_dir"] is True
    json.dumps([datastore.preview(n) for n in ("tsv", "csv", "json", "yaml", "text", "bin", "dir")])  # serialisable


def test_npz_preview():
    import numpy as np
    t = fresh()
    f = t / "src data" / "arr.npz"
    np.savez(f, a=np.zeros((2, 3)), b=np.arange(4))
    datastore.add("arr", str(f))
    p = datastore.preview("arr")
    assert p["kind"] == "npz" and {e["name"]: e["shape"] for e in p["entries"]} == {"a": [2, 3], "b": [4]}


def test_load_table():
    t = fresh()
    s = t / "src data"
    datastore.add("tsv", str(s / "evals.tsv"))
    datastore.add("csv", str(s / "evals.csv"))
    datastore.add("rec", str(s / "rec.json"))
    datastore.add("tables", str(s / "tables.json"))
    datastore.add("rows", str(s / "rows.yaml"))
    datastore.add("dir", str(s / "episodes dir"))
    datastore.add("text", str(s / "notes.md"))
    df = datastore.load_table("tsv")
    assert list(df.columns) == ["task", "arm", "k", "n"] and df["k"].tolist() == [16, 20]
    assert datastore.load_table("csv").shape == (1, 4)
    assert datastore.load_table("rec")["b"].tolist() == ["x", "y"]
    assert datastore.load_table("tables", sheet="other")["a"].tolist() == [2, 3]
    assert datastore.load_table("rows")["level"].tolist() == ["Easy", "Hard"]
    assert datastore.load_table("dir", sheet="sub/b.tsv")["y"].tolist() == [2, 4]
    for bad in [("dir", None), ("dir", "../evals.tsv"), ("text", None), ("tables", None)]:
        try:
            datastore.load_table(*bad)
        except DataStoreError:
            pass
        else:
            raise AssertionError(f"load_table{bad} should have failed")


def test_real_difficulty_eval_by_reference():
    """Register the real paperplot/data/difficulty_eval.tsv into the temp registry; it must be untouched after."""
    fresh()
    real = ROOT / "data" / "difficulty_eval.tsv"
    if not real.exists():
        print("  (skipping: difficulty_eval.tsv not present)")
        return
    before = (real.stat().st_size, real.stat().st_mtime_ns)
    datastore.add("difficulty_eval", str(real))
    df = datastore.load_table("difficulty_eval")
    assert {"task", "policy", "condition", "successes", "trials"} <= set(df.columns)
    datastore.remove("difficulty_eval")
    assert real.exists() and (real.stat().st_size, real.stat().st_mtime_ns) == before


def _cleanup():
    for t in _TMPS:
        # remove links first (never follow them), then our own fixture trees
        for root, dirs, files in os.walk(t, topdown=True):
            for d in list(dirs):
                p = os.path.join(root, d)
                if os.path.isjunction(p) or os.path.islink(p):
                    (os.rmdir if IS_WIN else os.unlink)(p)   # POSIX: a dir symlink is a file to rmdir
                    dirs.remove(d)
        shutil.rmtree(t, ignore_errors=True)


def teardown_module(module=None):
    _cleanup()
    config.REGISTRY_PATH, config.LINKS_DIR = _ORIG


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception as ex:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {n}: {ex}")
    _cleanup()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
