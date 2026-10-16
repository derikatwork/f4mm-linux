from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from bethmm import pathresolve
from bethmm.pathresolve import (
    CaseResolver,
    ResolvedPath,
    apply_convention,
    infer_dir_convention,
    resolve_case_insensitive,
)
from helpers import make_tree

P = PurePosixPath


def snapshot(base: Path) -> dict[str, tuple[bool, int]]:
    """Every path under *base* (symlinks not followed) with its type and mtime."""
    out = {}
    for root, dirs, files in os.walk(base):
        for name in dirs + files:
            p = Path(root, name)
            st = p.lstat()
            out[str(p.relative_to(base))] = (p.is_dir(), st.st_mtime_ns)
    return out


# --- conventions ------------------------------------------------------------


@pytest.mark.parametrize(
    "names, expected",
    [
        ([], "none"),
        (["meshes", "textures", "f4se"], "lower"),
        (["Meshes", "Textures", "F4SE", "MCM"], "capitalized"),
        (["meshes", "Textures"], "none"),
        (["meshes", "123", "__"], "lower"),
        (["123", "!!"], "none"),
        (["_Backup", "Video"], "capitalized"),
        (["Ärger", "Über"], "capitalized"),
        (["ärger", "straße"], "lower"),
    ],
)
def test_infer_dir_convention(names, expected):
    assert infer_dir_convention(names) == expected
    assert infer_dir_convention(iter(names)) == expected


@pytest.mark.parametrize(
    "name, convention, expected",
    [
        ("NewDir", "lower", "newdir"),
        ("F4SE", "lower", "f4se"),
        ("newdir", "capitalized", "Newdir"),
        ("F4SE", "capitalized", "F4SE"),
        ("MCM", "capitalized", "MCM"),
        ("myMod", "capitalized", "myMod"),
        ("_backup", "capitalized", "_Backup"),
        ("123", "capitalized", "123"),
        ("ärger", "capitalized", "Ärger"),
        ("NewDir", "none", "NewDir"),
        ("newdir", "none", "newdir"),
    ],
)
def test_apply_convention(name, convention, expected):
    assert apply_convention(name, convention) == expected


# --- matching existing entries ----------------------------------------------


def test_mod_textures_lands_in_existing_lowercase_textures(tmp_path):
    make_tree(tmp_path, ["Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/Textures/foo.dds")
    assert r.rel == P("Data/textures/foo.dds")
    assert r.path == tmp_path / "Data/textures/foo.dds"
    assert r.new_dirs == ()
    assert not r.exists and not r.exists_on_disk
    assert r.ambiguous == () and not r.type_conflict


def test_mod_lowercase_textures_lands_in_existing_capitalized_textures(tmp_path):
    make_tree(tmp_path, ["Data/Textures/"])
    r = CaseResolver(tmp_path).resolve("data/textures/foo.dds")
    assert r.rel == P("Data/Textures/foo.dds")
    assert r.new_dirs == ()


def test_nested_mixed_case_path(tmp_path):
    make_tree(tmp_path, ["data/meshes/armor/"])
    r = CaseResolver(tmp_path).resolve("Data/Meshes/Armor/x.nif")
    assert r.rel == P("data/meshes/armor/x.nif")
    assert r.new_dirs == ()
    assert not r.exists


def test_existing_file_with_different_case(tmp_path):
    make_tree(tmp_path, ["Data/Fallout4 - Textures1.ba2"])
    r = CaseResolver(tmp_path).resolve("data/FALLOUT4 - textures1.BA2")
    assert r.rel == P("Data/Fallout4 - Textures1.ba2")
    assert r.exists and r.exists_on_disk
    assert r.new_dirs == ()
    assert not r.type_conflict


def test_existing_directory_with_is_dir(tmp_path):
    make_tree(tmp_path, ["Data/F4SE/Plugins/"])
    r = CaseResolver(tmp_path).resolve("data/f4se/plugins", is_dir=True)
    assert r.rel == P("Data/F4SE/Plugins")
    assert r.exists and r.exists_on_disk
    assert r.new_dirs == ()
    assert not r.type_conflict


def test_new_file_keeps_mod_spelling(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/MyMod.ESP")
    assert r.rel == P("Data/MyMod.ESP")
    assert not r.exists
    assert r.new_dirs == ()


# --- spelling of new directories ----------------------------------------------


def test_new_dir_under_all_lowercase_parent_is_lowercased(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/textures/", "Data/Fallout4.esm"])
    r = CaseResolver(tmp_path).resolve("Data/NewFolder/Sub/x.nif")
    # "Sub" is under a brand-new folder with no siblings: kept as is.
    assert r.rel == P("Data/newfolder/Sub/x.nif")
    assert r.new_dirs == (P("Data/newfolder"), P("Data/newfolder/Sub"))


def test_new_dir_under_all_capitalized_parent_is_capitalized(tmp_path):
    make_tree(tmp_path, ["Data/Meshes/", "Data/Textures/", "Data/F4SE/", "Data/fallout4.esm"])
    res = CaseResolver(tmp_path)
    r = res.resolve("Data/sound/fx/x.wav")
    assert r.rel == P("Data/Sound/fx/x.wav")
    assert r.new_dirs == (P("Data/Sound"), P("Data/Sound/fx"))
    assert res.resolve("Data/MCM/config.json").rel == P("Data/MCM/config.json")
    assert res.resolve("Data/myMod/a").rel == P("Data/myMod/a")


def test_new_dir_with_mixed_siblings_keeps_mod_spelling(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/Textures/"])
    res = CaseResolver(tmp_path)
    assert res.resolve("Data/sound/x.wav").rel == P("Data/sound/x.wav")
    assert res.resolve("Data/NewDir/x").rel == P("Data/NewDir/x")


def test_new_dir_without_sibling_dirs_keeps_mod_spelling(tmp_path):
    make_tree(tmp_path, ["Data/Fallout4.esm", "Data/readme.txt"])
    r = CaseResolver(tmp_path).resolve("Data/NewDir/x")
    assert r.rel == P("Data/NewDir/x")
    assert r.new_dirs == (P("Data/NewDir"),)


def test_final_component_with_is_dir_follows_convention(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/Scripts", is_dir=True)
    assert r.rel == P("Data/scripts")
    assert r.new_dirs == (P("Data/scripts"),)
    assert not r.exists


def test_new_dirs_in_creation_order(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    r = res.resolve("Data/A/B/C/file.txt")
    assert r.new_dirs == (P("Data/A"), P("Data/A/B"), P("Data/A/B/C"))
    r = res.resolve("Data/A/B", is_dir=True)
    assert r.new_dirs == (P("Data/A"), P("Data/A/B"))


# --- planned entries ----------------------------------------------------------


def test_planned_dirs_merge_later_lookups(tmp_path):
    make_tree(tmp_path, ["Data/Fallout4.esm"])
    before = snapshot(tmp_path)
    res = CaseResolver(tmp_path)

    first = res.resolve("Data/NewDir/a", plan=True)
    assert first.rel == P("Data/NewDir/a")
    assert first.new_dirs == (P("Data/NewDir"),)
    assert not first.exists

    second = res.resolve("data/NEWDIR/b")
    assert second.rel == P("Data/NewDir/b")
    assert second.new_dirs == ()
    assert not second.exists and not second.exists_on_disk

    again = res.resolve("DATA/newdir/A")
    assert again.rel == P("Data/NewDir/a")
    assert again.exists and not again.exists_on_disk

    folder = res.resolve("data/newdir", is_dir=True)
    assert folder.rel == P("Data/NewDir")
    assert folder.exists and not folder.exists_on_disk
    assert folder.new_dirs == ()

    assert not (tmp_path / "Data/NewDir").exists()
    assert snapshot(tmp_path) == before


def test_planned_entries_under_missing_base(tmp_path):
    base = tmp_path / "game"
    res = CaseResolver(base)
    r = res.resolve("Data/Textures/x.dds", plan=True)
    assert r.new_dirs == (P("Data"), P("Data/Textures"))
    r = res.resolve("DATA/TEXTURES/y.dds")
    assert r.rel == P("Data/Textures/y.dds")
    assert r.new_dirs == ()
    assert not base.exists()


def test_plan_false_is_not_recorded(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/NewDir/a")
    r = res.resolve("data/NEWDIR/a")
    assert r.rel == P("Data/NEWDIR/a")
    assert r.new_dirs == (P("Data/NEWDIR"),)
    assert not r.exists


def test_clear_plan(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/NewDir/a", plan=True)
    res.clear_plan()
    r = res.resolve("data/NEWDIR/a")
    assert r.rel == P("Data/NEWDIR/a")
    assert not r.exists


def test_planned_dirs_count_for_convention(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/MyMod/textures/a.dds", plan=True)
    r = res.resolve("Data/MyMod/Meshes/a.nif")
    assert r.rel == P("Data/MyMod/meshes/a.nif")


def test_plans_are_per_resolver(tmp_path):
    make_tree(tmp_path, ["Data/"])
    one, two = CaseResolver(tmp_path), CaseResolver(tmp_path)
    one.resolve("Data/NewDir/a", plan=True)
    assert two.resolve("data/newdir/a").rel == P("Data/newdir/a")


def test_planned_entry_matching_disk_entry_is_not_ambiguous(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/NewDir/a", plan=True)
    make_tree(tmp_path, ["Data/NewDir/a"])
    res.invalidate()
    r = res.resolve("data/newdir/A")
    assert r.rel == P("Data/NewDir/a")
    assert r.exists and r.exists_on_disk
    assert r.ambiguous == ()


# --- ambiguity --------------------------------------------------------------


def test_ambiguous_duplicates_prefer_exact_match(tmp_path):
    make_tree(tmp_path, ["Data/Textures/", "Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/textures/x.dds")
    assert r.rel == P("Data/textures/x.dds")
    assert len(r.ambiguous) == 1
    assert "'Textures'" in r.ambiguous[0] and "'textures'" in r.ambiguous[0]


def test_ambiguous_duplicates_without_exact_match_use_first(tmp_path):
    make_tree(tmp_path, ["Data/textures/", "Data/Textures/"])
    r = CaseResolver(tmp_path).resolve("Data/TEXTURES/x.dds")
    assert r.rel == P("Data/Textures/x.dds")
    assert len(r.ambiguous) == 1
    assert "'Data'" in r.ambiguous[0]
    assert r.new_dirs == ()


def test_ambiguous_duplicate_files(tmp_path):
    make_tree(tmp_path, ["Data/MyMod.esp", "Data/mymod.esp"])
    r = CaseResolver(tmp_path).resolve("Data/MYMOD.ESP")
    assert r.rel == P("Data/MyMod.esp")
    assert r.exists and r.exists_on_disk
    assert len(r.ambiguous) == 1


def test_no_warning_without_duplicates(tmp_path):
    make_tree(tmp_path, ["Data/textures/", "Data/meshes/"])
    assert CaseResolver(tmp_path).resolve("Data/TEXTURES/x.dds").ambiguous == ()


# --- type conflicts -----------------------------------------------------------


def test_file_where_directory_exists(tmp_path):
    make_tree(tmp_path, ["Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/Textures")
    assert r.type_conflict
    assert r.rel == P("Data/textures")
    assert r.exists and r.exists_on_disk


def test_directory_where_file_exists(tmp_path):
    make_tree(tmp_path, ["Data/readme"])
    res = CaseResolver(tmp_path)
    r = res.resolve("Data/README/x.txt")
    assert r.type_conflict
    assert r.rel == P("Data/readme/x.txt")
    assert not r.exists and not r.exists_on_disk
    r = res.resolve("data/Readme", is_dir=True)
    assert r.type_conflict
    assert r.rel == P("Data/readme")


def test_type_conflict_is_not_planned(tmp_path):
    make_tree(tmp_path, ["Data/readme"])
    res = CaseResolver(tmp_path)
    first = res.resolve("Data/README/sub/x", plan=True)
    assert first.type_conflict
    assert first.new_dirs == (P("Data/readme/sub"),)
    second = res.resolve("Data/readme/SUB/y")
    assert second.rel == P("Data/readme/SUB/y")
    assert second.new_dirs == (P("Data/readme/SUB"),)


def test_type_conflict_with_planned_file(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/NewFile", plan=True)
    r = res.resolve("data/NEWFILE/x")
    assert r.type_conflict
    assert r.rel == P("Data/NewFile/x")
    assert not r.exists


# --- base directory -------------------------------------------------------------


def test_missing_base(tmp_path):
    base = tmp_path / "nope"
    r = CaseResolver(base).resolve("Data/Textures/x.dds")
    assert r.rel == P("Data/Textures/x.dds")
    assert r.path == base / "Data/Textures/x.dds"
    assert r.new_dirs == (P("Data"), P("Data/Textures"))
    assert not r.exists and not r.exists_on_disk
    assert not base.exists()


def test_base_is_a_file(tmp_path):
    base = tmp_path / "file"
    base.write_text("x")
    r = CaseResolver(str(base)).resolve("Data/x")
    assert r.new_dirs == (P("Data"),)
    assert not r.exists
    assert base.read_text() == "x"


def test_resolve_case_insensitive_wrapper(tmp_path):
    make_tree(tmp_path, ["data/Textures/foo.DDS"])
    r = resolve_case_insensitive(tmp_path, "Data/textures/FOO.dds")
    assert isinstance(r, ResolvedPath)
    assert r.rel == P("data/Textures/foo.DDS")
    assert r.exists_on_disk
    r = resolve_case_insensitive(str(tmp_path), "DATA/TEXTURES", is_dir=True)
    assert r.rel == P("data/Textures") and r.exists


# --- symlinks -----------------------------------------------------------------


def test_symlinked_dir_counts_as_dir(tmp_path):
    shared = make_tree(tmp_path / "shared", ["Foo.dds"])
    base = make_tree(tmp_path / "game", ["Data/"])
    (base / "Data/textures").symlink_to(shared, target_is_directory=True)
    res = CaseResolver(base)
    r = res.resolve("Data/Textures/foo.dds")
    assert r.rel == P("Data/textures/Foo.dds")
    assert r.exists and r.exists_on_disk
    assert not r.type_conflict
    r = res.resolve("data/TEXTURES", is_dir=True)
    assert r.exists and not r.type_conflict


def test_symlinked_base(tmp_path):
    real = make_tree(tmp_path / "real", ["Data/textures/"])
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    r = CaseResolver(link).resolve("DATA/Textures/x")
    assert r.rel == P("Data/textures/x")
    assert r.path == link / "Data/textures/x"


def test_dangling_and_file_symlinks_count_as_files(tmp_path):
    base = make_tree(tmp_path / "game", ["Data/", "target.esp"])
    (base / "Data/broken.esp").symlink_to(tmp_path / "missing.esp")
    (base / "Data/linked.esp").symlink_to(base / "target.esp")
    res = CaseResolver(base)
    r = res.resolve("data/BROKEN.ESP")
    assert r.rel == P("Data/broken.esp")
    assert r.exists and r.exists_on_disk and not r.type_conflict
    assert res.resolve("Data/Linked.esp").exists_on_disk
    r = res.resolve("Data/broken.esp/x")
    assert r.type_conflict and not r.exists


def test_symlink_loop_is_a_file(tmp_path):
    base = make_tree(tmp_path / "game", ["Data/"])
    (base / "Data/loop").symlink_to(base / "Data/loop")
    r = CaseResolver(base).resolve("Data/LOOP")
    assert r.rel == P("Data/loop")
    assert r.exists and not r.type_conflict


# --- caching and invalidation -------------------------------------------------


def test_listings_are_cached(tmp_path, monkeypatch):
    make_tree(tmp_path, ["Data/textures/a.dds", "Data/meshes/a.nif"])
    calls = []
    real_scandir = os.scandir

    def counting_scandir(path):
        calls.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(pathresolve.os, "scandir", counting_scandir)
    res = CaseResolver(tmp_path)
    for name in ("a.dds", "b.dds", "C.DDS"):
        res.resolve(f"Data/Textures/{name}")
    res.resolve("data/meshes/a.nif")
    assert sorted(calls) == sorted([tmp_path, tmp_path / "Data", tmp_path / "Data/textures", tmp_path / "Data/meshes"])
    # Nothing below a missing directory is scanned.
    calls.clear()
    res.resolve("Data/New/Deeper/x")
    assert calls == []


def test_invalidate_all(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    assert res.resolve("Data/Textures/x.dds").new_dirs == (P("Data/Textures"),)
    make_tree(tmp_path, ["Data/textures/x.dds"])
    # Still the cached (stale) answer until invalidated.
    assert res.resolve("Data/Textures/x.dds").new_dirs == (P("Data/Textures"),)
    res.invalidate()
    r = res.resolve("Data/Textures/x.dds")
    assert r.rel == P("Data/textures/x.dds")
    assert r.exists_on_disk and r.new_dirs == ()


def test_invalidate_one_dir_case_insensitively(tmp_path):
    make_tree(tmp_path, ["Data/textures/", "Data/meshes/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/textures/x.dds")
    res.resolve("Data/meshes/x.nif")
    make_tree(tmp_path, ["Data/textures/x.dds", "Data/meshes/x.nif"])

    res.invalidate("DATA/Textures")
    assert res.resolve("Data/textures/X.dds").exists_on_disk
    assert not res.resolve("Data/meshes/X.nif").exists_on_disk  # untouched, still cached

    res.invalidate(P("data"))  # also forgets everything cached below Data
    assert res.resolve("Data/meshes/X.nif").exists_on_disk


def test_invalidate_base_and_unknown_dirs(tmp_path):
    res = CaseResolver(tmp_path)
    assert not res.resolve("Data/x").exists
    make_tree(tmp_path, ["Data/x"])
    res.invalidate("Never/Seen")
    assert not res.resolve("Data/x").exists
    res.invalidate(".")
    assert res.resolve("Data/x").exists_on_disk


def test_invalidate_keeps_plan(tmp_path):
    make_tree(tmp_path, ["Data/"])
    res = CaseResolver(tmp_path)
    res.resolve("Data/NewDir/a", plan=True)
    res.invalidate()
    r = res.resolve("data/newdir/a")
    assert r.rel == P("Data/NewDir/a")
    assert r.exists and not r.exists_on_disk


def test_resolver_never_touches_disk(tmp_path):
    make_tree(
        tmp_path,
        ["Data/textures/a.dds", "Data/Meshes/b.nif", "Data/readme", "Fallout4.exe", "Data/Textures/"],
    )
    before = snapshot(tmp_path)
    res = CaseResolver(tmp_path)
    for rel, is_dir in [
        ("Data/Textures/new/a.dds", False),
        ("Data/meshes/x/y/z.nif", False),
        ("Data/README/x", False),
        ("Data/NewFolder", True),
        ("f4se_loader.exe", False),
        ("enbseries/x.fx", False),
    ]:
        res.resolve(rel, is_dir=is_dir, plan=True)
    res.invalidate()
    res.resolve("data/newfolder/x", plan=True)
    assert snapshot(tmp_path) == before


# --- input handling -----------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "",
        ".",
        "./",
        "//",
        "/",
        "/abs/path",
        P("/abs/path"),
        "..",
        "Data/../x",
        "Data\\..\\x",
        "../Data/x",
        "C:\\Games\\Fallout4\\Data",
        "c:Data",
        "\\\\server\\share\\x",
        "Data/\0x",
    ],
)
def test_invalid_paths(tmp_path, rel):
    with pytest.raises(ValueError):
        CaseResolver(tmp_path).resolve(rel)


def test_invalidate_rejects_bad_paths(tmp_path):
    res = CaseResolver(tmp_path)
    with pytest.raises(ValueError):
        res.invalidate("../x")
    with pytest.raises(ValueError):
        res.invalidate("/abs")


def test_backslashes_and_dot_components(tmp_path):
    make_tree(tmp_path, ["Data/textures/"])
    res = CaseResolver(tmp_path)
    assert res.resolve("Data\\Textures\\foo.dds").rel == P("Data/textures/foo.dds")
    assert res.resolve("./Data//Textures/./foo.dds").rel == P("Data/textures/foo.dds")
    assert res.resolve("Data\\/Textures/foo.dds/").rel == P("Data/textures/foo.dds")
    assert res.resolve(PureWindowsPath("Data\\Textures\\foo.dds")).rel == P("Data/textures/foo.dds")
    assert res.resolve(P("Data/TEXTURES/foo.dds")).rel == P("Data/textures/foo.dds")


def test_non_ascii_names(tmp_path):
    make_tree(tmp_path, ["Data/Sound/Voice/Ärger/", "Data/Straße/", "Data/ΣΟΦΙΑ/"])
    res = CaseResolver(tmp_path)
    assert res.resolve("data/sound/voice/äRGER/x.wav").rel == P("Data/Sound/Voice/Ärger/x.wav")
    # Simple per-character folding: "ß" matches only itself, never "ss".
    assert res.resolve("Data/STRAßE/x").rel == P("Data/Straße/x")
    r = res.resolve("Data/STRASSE/x")
    assert r.rel == P("Data/STRASSE/x")
    assert r.new_dirs == (P("Data/STRASSE"),)
    assert res.resolve("Data/σοφια/x").rel == P("Data/ΣΟΦΙΑ/x")


def test_final_sigma_matches_capital_sigma(tmp_path):
    make_tree(tmp_path, ["Data/ΛΟΓΟΣ.esp"])
    r = CaseResolver(tmp_path).resolve("Data/λογος.esp")
    assert r.rel == P("Data/ΛΟΓΟΣ.esp")
    assert r.exists_on_disk


def test_non_ascii_new_dir_follows_convention(tmp_path):
    make_tree(tmp_path, ["Data/meshes/", "Data/textures/"])
    r = CaseResolver(tmp_path).resolve("Data/ÜBERMOD/x")
    assert r.rel == P("Data/übermod/x")
    # Planned non-ASCII folder is found again under another spelling.
    res = CaseResolver(tmp_path)
    res.resolve("Data/ÜberMod/x", plan=True)
    assert res.resolve("DATA/üBERmOD/y").rel == P("Data/übermod/y")
