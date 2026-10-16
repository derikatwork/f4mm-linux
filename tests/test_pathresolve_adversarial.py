"""Adversarial tests for bethmm.pathresolve: orderings, realistic layouts,
symlinks, cache invalidation and scaling."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path, PurePosixPath

import pytest

from bethmm.pathresolve import CaseResolver, _fold
from helpers import make_tree

P = PurePosixPath


def case_duplicates(base: Path) -> int:
    """Number of entries under *base* that are case-variants of a sibling."""
    n = 0
    for _root, dirs, files in os.walk(base):
        names = dirs + files
        n += len(names) - len({_fold(name) for name in names})
    return n


def deploy(base: Path, r, is_dir: bool) -> None:
    """Create exactly what a resolve result says is new (what a deploy engine does)."""
    for d in r.new_dirs:
        (base / d).mkdir()
    if not is_dir and not r.exists:
        (base / r.rel).write_text("x")


# --- orderings: no new case-variant duplicates --------------------------------


def test_planned_dir_then_file_of_same_name_conflicts(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/Foo/x.nif", plan=True)
    r = res.resolve("data/FOO", plan=True)
    assert r.type_conflict and r.exists and not r.exists_on_disk
    assert r.rel == P("Data/Foo")
    # Not recorded: the planned directory is still a directory.
    assert not res.resolve("DATA/foo/y.nif").type_conflict


def test_deploying_the_plan_then_rescanning_keeps_one_folder(tmp_path):
    make_tree(tmp_path, ["Data/Video/", "Data/Fallout4.esm"])
    res = CaseResolver(tmp_path)
    for rel in ("Data/textures/a.dds", "Data/F4SE/Plugins/x.dll", "Data/meshes/m.nif"):
        deploy(tmp_path, res.resolve(rel, plan=True), False)
    res.invalidate()
    for rel in ("DATA/Textures/b.dds", "data/f4se/plugins/y.dll", "Data/MESHES/n.nif"):
        r = res.resolve(rel, plan=True)
        assert r.new_dirs == () and r.ambiguous == ()
        deploy(tmp_path, r, False)
    assert sorted(p.name for p in (tmp_path / "Data").iterdir() if p.is_dir()) == ["F4SE", "Meshes", "Textures", "Video"]
    assert case_duplicates(tmp_path) == 0


WORDS = ["data", "textures", "meshes", "f4se", "plugins", "mcm", "sound", "x", "foo", "_backup", "Ärger", "σοφιας"]


def _variant(word: str, rng: random.Random) -> str:
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in word)


def _random_tree(base: Path, rng: random.Random) -> None:
    base.mkdir()
    for _ in range(rng.randint(0, 6)):
        p = base.joinpath(*(_variant(rng.choice(WORDS), rng) for _ in range(rng.randint(1, 3))))
        try:
            if rng.random() < 0.6:
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("x")
        except (FileExistsError, NotADirectoryError):
            pass


@pytest.mark.parametrize("seed", range(60))
def test_plan_matches_deploy_then_resolve_and_never_adds_duplicates(tmp_path, seed):
    rng = random.Random(seed)
    base = tmp_path / "game"
    _random_tree(base, rng)
    before = case_duplicates(base)
    planner = CaseResolver(base)
    for step in range(30):
        rel = "/".join(_variant(rng.choice(WORDS), rng) for _ in range(rng.randint(1, 4)))
        is_dir = rng.random() < 0.2
        fresh = CaseResolver(base).resolve(rel, is_dir=is_dir)
        planned = planner.resolve(rel, is_dir=is_dir, plan=True)
        assert (planned.rel, planned.new_dirs, planned.exists, planned.type_conflict) == (
            fresh.rel, fresh.new_dirs, fresh.exists, fresh.type_conflict
        ), rel
        if not planned.type_conflict:
            deploy(base, planned, is_dir)
        if step % 7 == 0:
            planner.invalidate()
        assert case_duplicates(base) == before, rel


# --- realistic layouts ----------------------------------------------------------


def test_gog_fallout4_layout(tmp_path):
    make_tree(tmp_path, ["Fallout4.exe", "Data/Video/Intro.bk2", "Data/Fallout4.esm", "Data/Fallout4 - Textures1.ba2"])
    res = CaseResolver(tmp_path)
    assert res.resolve("f4se_loader.exe", plan=True).rel == P("f4se_loader.exe")
    assert res.resolve("Data/F4SE/Plugins/x.dll", plan=True).rel == P("Data/F4SE/Plugins/x.dll")
    assert res.resolve("data/textures/armor/a.dds", plan=True).rel == P("Data/Textures/armor/a.dds")
    assert res.resolve("Data/Textures/Armor/b.dds", plan=True).rel == P("Data/Textures/armor/b.dds")
    assert res.resolve("DATA/VIDEO/Intro.BK2").exists_on_disk


def test_skyrim_lowercase_layout(tmp_path):
    make_tree(tmp_path, ["SkyrimSE.exe", "Data/Skyrim.esm", "Data/meshes/", "Data/textures/", "Data/scripts/"])
    res = CaseResolver(tmp_path)
    assert res.resolve("Data/SKSE/Plugins/x.dll", plan=True).rel == P("Data/skse/Plugins/x.dll")
    assert res.resolve("Data/Interface/Translations/a.txt").rel == P("Data/interface/Translations/a.txt")
    assert res.resolve("Data/Scripts/Source/a.psc").rel == P("Data/scripts/Source/a.psc")


# --- symlinks -------------------------------------------------------------------


def test_symlinked_component_is_followed_but_dotdot_is_rejected(tmp_path):
    outside = make_tree(tmp_path / "elsewhere", ["textures/", "secret.txt"])
    base = make_tree(tmp_path / "game", ["Fallout4.exe"])
    (base / "Data").symlink_to(outside, target_is_directory=True)
    res = CaseResolver(base)
    r = res.resolve("data/Textures/x.dds")
    # The game's own symlink is honoured (a moved Data folder is a valid setup);
    # the result is still spelled relative to the base.
    assert r.rel == P("Data/textures/x.dds")
    assert Path(os.path.realpath(r.path.parent)) == outside.resolve() / "textures"
    for rel in ("Data/../../elsewhere/secret.txt", "..\\elsewhere", "Data/./../x"):
        with pytest.raises(ValueError):
            res.resolve(rel)


# --- caching --------------------------------------------------------------------


def test_invalidate_forgets_every_case_variant_and_reindexes(tmp_path):
    make_tree(tmp_path, ["Data/Textures/", "Data/textures/", "Data/meshes/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/Textures/a.dds")
    res.resolve("Data/textures/a.dds")
    res.resolve("Data/meshes/a.nif")
    make_tree(tmp_path, ["Data/Textures/a.dds", "Data/textures/a.dds", "Data/meshes/a.nif"])
    res.invalidate("DATA/TEXTURES")
    assert res.resolve("Data/Textures/A.dds").exists_on_disk
    assert res.resolve("Data/textures/A.dds").exists_on_disk
    assert not res.resolve("Data/meshes/A.nif").exists_on_disk
    # Listings cached again after an invalidate are found by the next one.
    (tmp_path / "Data/Textures/a.dds").unlink()
    res.invalidate("data")
    assert not res.resolve("Data/Textures/a.dds").exists_on_disk
    assert res.resolve("Data/meshes/A.nif").exists_on_disk


def test_planned_entry_changing_type_updates_convention(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/Video/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/Foo/x", plan=True)  # mixed siblings: spelling kept
    res.resolve("Data/bar/x", plan=True)
    # The tree changes: Foo turns out to be a file, the other folders go away.
    (tmp_path / "Data/meshes").rmdir()
    (tmp_path / "Data/Video").rmdir()
    make_tree(tmp_path, ["Data/Foo"])
    res.invalidate()
    assert not res.resolve("Data/Foo", plan=True).type_conflict
    # Only "bar" is left as a (planned) sub-directory: all lowercase.
    assert res.resolve("Data/NEW/x").rel == P("Data/new/x")


# --- scaling --------------------------------------------------------------------


def test_new_folders_in_a_huge_folder_scale_linearly(tmp_path):
    data = tmp_path / "Data"
    data.mkdir()
    for i in range(20_000):
        (data / f"plugin{i:05d}.esp").touch()
    make_tree(tmp_path, ["Data/Meshes/", "Data/Video/"])
    res = CaseResolver(tmp_path)
    start = time.perf_counter()
    for i in range(4000):
        r = res.resolve(f"Data/newdir{i}/x.nif", plan=True)
    for i in range(2000):
        res.resolve(f"Data/other{i}/x.nif")
    elapsed = time.perf_counter() - start
    assert r.rel == P("Data/Newdir3999/x.nif")
    # Re-reading every sibling for each new folder took ~20 s here.
    assert elapsed < 2.0


def test_invalidate_cost_does_not_grow_with_cache_size(tmp_path):
    for i in range(3000):
        (tmp_path / "Data/meshes" / f"d{i}").mkdir(parents=True)
    res = CaseResolver(tmp_path)
    for i in range(3000):
        res.resolve(f"Data/meshes/d{i}/x.nif")
    make_tree(tmp_path, ["Data/meshes/d5/x.nif"])
    start = time.perf_counter()
    for i in range(3000):
        res.invalidate(f"DATA/Meshes/D{i}")
    elapsed = time.perf_counter() - start
    assert res.resolve("Data/meshes/d5/X.NIF").exists_on_disk
    # Folding every cached key on each call took ~6 s here.
    assert elapsed < 1.0
