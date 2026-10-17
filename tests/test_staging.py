from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from bethmm import archive, paths, staging
from bethmm.archive import ArchiveError
from bethmm.manifest import Manifest, ModFile
from bethmm.placement import FileMapping, Rule, Target, analyze_files
from bethmm.staging import (
    Prepared,
    StagingError,
    commit,
    discard,
    find_existing,
    folder_name,
    prepare,
    remove_staging,
    suggested_name,
)
from helpers import make_plugin, make_tree

needs_7z = pytest.mark.skipif(not any(shutil.which(n) for n in archive.SEVENZIP_NAMES), reason="7z not installed")

FO4_PLUGIN = make_plugin(["Fallout4.esm"], hedr_version=1.0, form_version=131)

#: A typical Nexus download: a wrapper folder with Data content and a readme.
COOL_MOD = {
    "CoolMod v1.2/CoolMod.esp": FO4_PLUGIN,
    "CoolMod v1.2/Textures/Armor/a.dds": b"dds-a",
    "CoolMod v1.2/Meshes/b.nif": b"nif-b",
    "readme.txt": b"read me",
}
COOL_MOD_DESTS = {
    "CoolMod v1.2/CoolMod.esp": "Data/CoolMod.esp",
    "CoolMod v1.2/Meshes/b.nif": "Data/Meshes/b.nif",
    "CoolMod v1.2/Textures/Armor/a.dds": "Data/Textures/Armor/a.dds",
}


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _7z(path: Path, entries: dict[str, bytes], tmp_path: Path) -> Path:
    src = make_tree(tmp_path / "7z-src", entries)
    exe = archive.find_sevenzip()
    subprocess.run([exe, "a", "-bd", str(path), *sorted(os.listdir(src))], cwd=src, check=True, capture_output=True)
    shutil.rmtree(src)
    return path


def _workdirs() -> list[str]:
    tmp = paths.tmp_dir()
    return sorted(os.listdir(tmp)) if tmp.exists() else []


def _dests(mappings) -> dict[str, str]:
    return {m.source: m.dest for m in mappings}


def _fake_prepared(tmp_path: Path, source_name: str, files: dict[str, bytes] | None = None) -> Prepared:
    """A Prepared without extraction: files written straight into a tmp workdir."""
    files = files if files is not None else COOL_MOD
    paths.tmp_dir().mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="install-", dir=paths.tmp_dir()))
    files_dir = make_tree(workdir / "x", files)
    plan = analyze_files(list(files), archive_name=source_name)
    return Prepared(tmp_path / "downloads" / source_name, workdir, files_dir, sorted(files), plan)


@pytest.fixture
def manifest():
    m = Manifest()
    yield m
    m.close()


def _snapshot(base: Path) -> dict[str, tuple]:
    """Everything under *base*: type, link target or content hash, mode, mtime."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            full = Path(dirpath, name)
            st = full.lstat()
            rel = full.relative_to(base).as_posix()
            if full.is_symlink():
                out[rel] = ("link", os.readlink(full))
            elif full.is_dir():
                out[rel] = ("dir", st.st_mode)
            elif full.is_file():
                out[rel] = ("file", st.st_mode, st.st_mtime_ns, hashlib.sha256(full.read_bytes()).hexdigest())
            else:
                out[rel] = ("other", st.st_mode)
    return out


# ---------------------------------------------------------------- prepare


@needs_7z
def test_prepare_zip_extracts_into_tmp_and_plans(tmp_path):
    src = _zip(tmp_path / "dl" / "CoolMod-1234-1-2-1680000000.zip", COOL_MOD)
    before = _snapshot(tmp_path / "dl")
    prepared = prepare(src, installed_games=["fallout4"])
    try:
        assert prepared.source == src
        assert prepared.workdir.parent == paths.tmp_dir()
        assert prepared.workdir.name.startswith("install-")
        assert prepared.files_dir == prepared.workdir / "x"
        assert prepared.files == sorted(COOL_MOD)
        for rel, data in COOL_MOD.items():
            assert (prepared.files_dir / rel).read_bytes() == data
        plan = prepared.plan
        assert plan.target is Target.DATA
        assert plan.wrapper_dirs == ["CoolMod v1.2"]
        assert _dests(plan.mappings) == COOL_MOD_DESTS
        assert "readme.txt" in plan.skipped
        assert plan.game.game_id == "fallout4"
        assert plan.nexus is not None and plan.nexus.mod_id == 1234
        assert prepared.warnings == []
    finally:
        discard(prepared)
    assert _snapshot(tmp_path / "dl") == before
    assert _workdirs() == []


@needs_7z
def test_prepare_7z(tmp_path):
    src = _7z(tmp_path / "SkyUI_5_2_SE-12604-5-2SE.7z", {
        "Data/SkyUI_SE.esp": b"not a real plugin",
        "Data/Interface/skyui/a.swf": b"swf",
        "Data/Interface/Ünïcode.txt": b"u",
    }, tmp_path)
    prepared = prepare(src)
    try:
        assert prepared.files == ["Data/Interface/skyui/a.swf", "Data/Interface/Ünïcode.txt", "Data/SkyUI_SE.esp"]
        assert prepared.plan.target is Target.GAME_ROOT
        assert prepared.plan.rule is Rule.DATA_FOLDER
        assert _dests(prepared.plan.mappings)["Data/SkyUI_SE.esp"] == "Data/SkyUI_SE.esp"
        assert suggested_name(prepared) == "SkyUI 5 2 SE"
    finally:
        discard(prepared)


@needs_7z
def test_prepare_reports_symlinks_removed_from_archive(tmp_path):
    src_dir = make_tree(tmp_path / "src", {"Textures/a.dds": b"a"})
    os.symlink("/etc/passwd", src_dir / "Textures" / "evil.dds")
    exe = archive.find_sevenzip()
    subprocess.run([exe, "a", "-bd", "-snl", str(tmp_path / "links.7z"), "Textures"], cwd=src_dir,
                   check=True, capture_output=True)
    prepared = prepare(tmp_path / "links.7z")
    try:
        assert prepared.files == ["Textures/a.dds"]
        assert any("evil.dds" in w for w in prepared.warnings)
        assert not os.path.lexists(prepared.files_dir / "Textures" / "evil.dds")
    finally:
        discard(prepared)


def test_prepare_folder_copies_without_touching_it(tmp_path):
    secret = make_tree(tmp_path / "secret", {"private.txt": b"keep out"})
    src = make_tree(tmp_path / "My Mod Folder", {
        "Data/MyMod.esp": FO4_PLUGIN,
        "Data/Textures/a.dds": b"a",
        "Docs/readme.md": b"docs",
    })
    os.symlink(secret / "private.txt", src / "Data" / "Textures" / "linked.dds")
    os.symlink(secret, src / "Data" / "Linked Folder")
    os.mkfifo(src / "Data" / "pipe")
    os.chmod(src / "Data" / "Textures" / "a.dds", 0o444)
    before = _snapshot(src)

    prepared = prepare(src)
    try:
        assert _snapshot(src) == before
        assert prepared.files == ["Data/MyMod.esp", "Data/Textures/a.dds", "Docs/readme.md"]
        copied = prepared.files_dir
        assert (copied / "Data" / "Textures" / "a.dds").read_bytes() == b"a"
        assert not (copied / "Data" / "Textures" / "a.dds").is_symlink()
        assert not os.path.lexists(copied / "Data" / "Textures" / "linked.dds")
        assert not os.path.lexists(copied / "Data" / "Linked Folder")
        assert not os.path.lexists(copied / "Data" / "pipe")
        warnings = " | ".join(prepared.warnings)
        assert "linked.dds" in warnings and "Linked Folder" in warnings and "pipe" in warnings
        assert prepared.plan.target is Target.GAME_ROOT
        assert _dests(prepared.plan.mappings) == {
            "Data/MyMod.esp": "Data/MyMod.esp",
            "Data/Textures/a.dds": "Data/Textures/a.dds",
        }
        assert suggested_name(prepared) == "My Mod Folder"
    finally:
        discard(prepared)
    assert _snapshot(src) == before
    assert (secret / "private.txt").read_bytes() == b"keep out"


def test_prepare_folder_splits_backslash_names(tmp_path, manifest):
    # Unpacked on Linux by a tool that kept a Windows zip's backslashes, like archives.
    src = tmp_path / "Bs Mod"
    src.mkdir()
    (src / "Data\\Bs.esp").write_bytes(FO4_PLUGIN)
    (src / "Data\\Textures\\b.dds").write_bytes(b"b")
    before = _snapshot(src)
    prepared = prepare(src)
    try:
        assert prepared.files == ["Data/Bs.esp", "Data/Textures/b.dds"]
        mod = commit(prepared, manifest, "fallout4").mod
    finally:
        discard(prepared)
    assert _dests(manifest.mod_files(mod.id)) == {"Data/Bs.esp": "Data/Bs.esp",
                                                  "Data/Textures/b.dds": "Data/Textures/b.dds"}
    assert (mod.staging_dir() / "Data" / "Textures" / "b.dds").read_bytes() == b"b"
    assert _snapshot(src) == before


def test_prepare_refuses_folder_containing_data_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("BETHMM_DATA_DIR", str(home / "data"))
    make_tree(home, {"Textures/a.dds": b"a"})
    with pytest.raises(StagingError, match="data folder"):
        prepare(home)
    assert _workdirs() == []


@needs_7z
def test_prepare_with_roots_and_target(tmp_path):
    src = _zip(tmp_path / "odd.zip", {
        "Extras/Option A/stuff/a.dds": b"a",
        "Extras/Option B/stuff/b.dds": b"b",
        "Main/thing.dat": b"t",
    })
    prepared = prepare(src, roots=["Extras/Option A"], target=Target.DATA)
    try:
        assert prepared.plan.rule is Rule.MANUAL
        assert prepared.plan.target is Target.DATA
        assert _dests(prepared.plan.mappings) == {"Extras/Option A/stuff/a.dds": "Data/stuff/a.dds"}
    finally:
        discard(prepared)


@needs_7z
def test_prepare_target_without_roots_uses_detected_root(tmp_path):
    src = _zip(tmp_path / "wrapped.zip", {"Wrapper/stuff/a.dds": b"a", "Wrapper/b.dat": b"b"})
    prepared = prepare(src, target=Target.DATA)
    try:
        assert _dests(prepared.plan.mappings) == {
            "Wrapper/b.dat": "Data/b.dat",
            "Wrapper/stuff/a.dds": "Data/stuff/a.dds",
        }
    finally:
        discard(prepared)


@needs_7z
def test_prepare_invalid_root_removes_workdir(tmp_path):
    src = _zip(tmp_path / "m.zip", {"Textures/a.dds": b"a"})
    with pytest.raises(ValueError):
        prepare(src, roots=["Nope"], target=Target.DATA)
    assert _workdirs() == []
    with pytest.raises(ValueError):
        prepare(src, roots=["Textures"], target=Target.UNKNOWN)
    assert _workdirs() == []


@needs_7z
def test_prepare_extraction_failure_removes_workdir(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04 this is not really a zip file" * 10)
    with pytest.raises(ArchiveError):
        prepare(broken)
    assert _workdirs() == []
    unsafe = _zip(tmp_path / "evil.zip", {"../evil.txt": b"x", "ok.txt": b"y"})
    with pytest.raises(ArchiveError, match="unsafe"):
        prepare(unsafe)
    assert _workdirs() == []
    assert not (paths.tmp_dir().parent / "evil.txt").exists()


def test_prepare_failure_in_planning_removes_workdir(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "Mod", {"Textures/a.dds": b"a"})

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(staging.placement, "analyze_files", boom)
    with pytest.raises(KeyboardInterrupt):
        prepare(src)
    assert _workdirs() == []


def test_prepare_missing_source(tmp_path):
    with pytest.raises(StagingError, match="does not exist"):
        prepare(tmp_path / "nope.zip")
    os.symlink(tmp_path / "gone", tmp_path / "dangling.zip")
    with pytest.raises(StagingError):
        prepare(tmp_path / "dangling.zip")
    os.mkfifo(tmp_path / "fifo.zip")
    with pytest.raises(StagingError, match="neither"):
        prepare(tmp_path / "fifo.zip")
    assert _workdirs() == []


def test_prepare_folder_copy_error_removes_workdir(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "Mod", {"Textures/a.dds": b"a"})

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device", "a.dds")

    monkeypatch.setattr(staging.shutil, "copy2", fail)
    with pytest.raises(StagingError, match="No space left"):
        prepare(src)
    assert _workdirs() == []


# ---------------------------------------------------------------- names


@pytest.mark.parametrize("source_name, expected", [
    ("Unofficial Fallout 4 Patch-4598-2-1-5-1680000000.zip", "Unofficial Fallout 4 Patch"),
    ("SkyUI_5_2_SE-12604-5-2SE.7z", "SkyUI 5 2 SE"),
    ("Mod Name - Main File-1234-1-0a-1600000000 (1).7z", "Mod Name - Main File"),
    ("my_random_archive.zip", "my_random_archive"),
    ("Cool Mod v1.2.rar", "Cool Mod v1.2"),
    ("CoolMod (1).7z", "CoolMod"),
    ("Textures-2.zip", "Textures-2"),
    ("Some Folder v1.2", "Some Folder v1.2"),
    ("weird.tar.gz", "weird"),
    ("bad\x1bname.zip", "bad name"),
    ("bad\udcffbytes.zip", "bad\ufffdbytes"),
    (".zip", ".zip"),
])
def test_suggested_name(tmp_path, source_name, expected):
    assert suggested_name(_fake_prepared(tmp_path, source_name)) == expected


@pytest.mark.parametrize("name, expected", [
    ("Cool Mod", "Cool Mod"),
    ("a/b", "a_b"),
    ("a\\b", "a_b"),
    ("..", "mod"),
    (".", "mod"),
    ("", "mod"),
    ("   ", "mod"),
    (".hidden", "hidden"),
    ("..  .hidden", "hidden"),
    ("  padded  ", "padded"),
    ("tab\there", "tab_here"),
    ("nul\0byte", "nul_byte"),
    ("bell\x07\x7f", "bell__"),
    ("../../etc", "_.._etc"),
    ("Ünïcode Mod ✓ 日本語", "Ünïcode Mod ✓ 日本語"),
    ("bad\udcffbytes", "bad_bytes"),
])
def test_folder_name_sanitising(name, expected):
    assert folder_name(name) == expected


def test_folder_name_length_limits():
    assert len(folder_name("x" * 300)) == 100
    assert folder_name("x" * 300) == "x" * 100
    wide = folder_name("日" * 300)
    assert len(wide.encode("utf-8")) <= 200 and set(wide) == {"日"}
    assert folder_name("a" + " " * 120 + "b") == "a"
    suffixed = folder_name("x" * 300, ["x" * 100])
    assert suffixed == "x" * 96 + " (2)"
    assert len(suffixed) == 100


def test_folder_name_unique_case_insensitively():
    assert folder_name("Cool Mod", ["cool mod"]) == "Cool Mod (2)"
    assert folder_name("Cool Mod", ["COOL MOD", "cool mod (2)", "Cool Mod (3)"]) == "Cool Mod (4)"
    assert folder_name("Ärger", ["äRGER"]) == "Ärger (2)"
    assert folder_name("Cool Mod", ["Other"]) == "Cool Mod"
    assert folder_name("..", ["mod"]) == "mod (2)"


def test_find_existing_by_nexus_id_then_name(tmp_path, manifest):
    prepared = _fake_prepared(tmp_path, "Cool Mod-1234-2-0-1700000000.zip")
    assert find_existing(manifest, "fallout4", prepared) is None
    by_name = manifest.add_mod("fallout4", "cool mod", "cool mod", [])
    assert find_existing(manifest, "fallout4", prepared).id == by_name.id
    by_id = manifest.add_mod("fallout4", "Renamed by user", "renamed", [], nexus_id=1234)
    assert find_existing(manifest, "fallout4", prepared).id == by_id.id
    # Several mods from the same Nexus page: the one with the same name wins.
    same_both = manifest.add_mod("fallout4", "Cool Mod", "cm", [], nexus_id=1234)
    assert find_existing(manifest, "fallout4", prepared).id == same_both.id
    # Other games never match.
    assert find_existing(manifest, "skyrimse", prepared) is None


def test_find_existing_plain_name(tmp_path, manifest):
    prepared = _fake_prepared(tmp_path, "my_mod.zip")
    other = manifest.add_mod("fallout4", "Something", "s", [], nexus_id=99)
    assert find_existing(manifest, "fallout4", prepared) is None
    mod = manifest.add_mod("fallout4", "MY_MOD", "my_mod", [])
    assert find_existing(manifest, "fallout4", prepared).id == mod.id
    assert find_existing(manifest, "fallout4", prepared, name="something").id == other.id
    assert find_existing(manifest, "fallout4", prepared, name="nothing") is None


# ---------------------------------------------------------------- commit


@needs_7z
def test_commit_moves_files_and_records_mapping(tmp_path, manifest):
    first = manifest.add_mod("fallout4", "Earlier Mod", "Earlier Mod", [ModFile("a.esp", "Data/a.esp")])
    src = _zip(tmp_path / "CoolMod-1234-1-2-1680000000.zip", COOL_MOD)
    prepared = prepare(src)
    try:
        result = commit(prepared, manifest, "fallout4")
    finally:
        discard(prepared)
    mod = result.mod
    assert result.replaced is False and result.old_folder is None
    assert mod.name == "CoolMod"
    assert mod.folder == "CoolMod"
    assert mod.priority > first.priority
    assert [m.id for m in manifest.mods("fallout4")] == [first.id, mod.id]
    assert mod.enabled is True
    assert mod.nexus_id == 1234 and mod.version == "1.2"
    assert mod.source_archive == str(src)
    assert mod.plan == {"root": "CoolMod v1.2", "target": "data", "rule": "data_content",
                        "wrapper_dirs": ["CoolMod v1.2"], "skipped": 1}
    files = manifest.mod_files(mod.id)
    assert sorted(files, key=lambda f: f.source) == sorted(
        (ModFile(m.source, m.dest) for m in prepared.plan.mappings), key=lambda f: f.source)
    assert _dests(files) == COOL_MOD_DESTS

    folder = paths.mods_dir("fallout4") / "CoolMod"
    assert mod.staging_dir() == folder
    for f in files:
        assert (folder / f.source).read_bytes() == COOL_MOD[f.source]
    assert (folder / "readme.txt").read_bytes() == b"read me"  # kept, never deployed
    assert not prepared.workdir.exists()
    assert src.exists()


def test_commit_uses_given_name_and_unique_folder(tmp_path, manifest):
    make_tree(paths.mods_dir("fallout4"), {"cool mod/leftover.txt": b"x"})
    manifest.add_mod("fallout4", "x", "COOL MOD (2)", [])
    prepared = _fake_prepared(tmp_path, "whatever.zip")
    result = commit(prepared, manifest, "fallout4", name="  Cool\tMod ")
    assert result.mod.name == "Cool Mod"
    assert result.mod.folder == "Cool Mod (3)"
    assert (paths.mods_dir("fallout4") / "cool mod" / "leftover.txt").read_bytes() == b"x"
    discard(prepared)


def test_commit_refuses_unknown_and_fomod_plans(tmp_path, manifest):
    unknown = _fake_prepared(tmp_path, "unknown.zip", {"stuff/thing.xyz": b"x"})
    assert unknown.plan.target is Target.UNKNOWN
    with pytest.raises(StagingError, match="root folder"):
        commit(unknown, manifest, "fallout4")
    fomod = _fake_prepared(tmp_path, "fomod.zip", {
        "fomod/ModuleConfig.xml": b"<config/>",
        "Option A/Textures/a.dds": b"a",
        "Option B/Textures/a.dds": b"b",
    })
    assert fomod.plan.is_fomod and not fomod.plan.mappings
    with pytest.raises(StagingError, match="FOMOD"):
        commit(fomod, manifest, "fallout4")
    # Mappings with an UNKNOWN target (not a FOMOD) are refused too.
    unknown.plan.mappings = [FileMapping("stuff/thing.xyz", "Data/thing.xyz")]
    with pytest.raises(StagingError, match="Data or the game folder"):
        commit(unknown, manifest, "fallout4")
    assert manifest.mods("fallout4") == []
    assert not paths.mods_dir("fallout4").exists()
    assert unknown.files_dir.is_dir() and fomod.files_dir.is_dir()
    discard(unknown)
    discard(fomod)


def test_commit_accepts_fomod_with_mappings(tmp_path, manifest):
    fomod = _fake_prepared(tmp_path, "fomod.zip", {
        "fomod/ModuleConfig.xml": b"<config/>",
        "Option A/Textures/a.dds": b"a",
    })
    fomod.plan.mappings = [FileMapping("Option A/Textures/a.dds", "Data/Textures/a.dds")]
    result = commit(fomod, manifest, "fallout4")
    assert manifest.mod_files(result.mod.id) == [ModFile("Option A/Textures/a.dds", "Data/Textures/a.dds")]
    assert result.mod.plan["rule"] == "fomod"
    discard(fomod)


def test_commit_refuses_bad_mappings(tmp_path, manifest):
    prepared = _fake_prepared(tmp_path, "m.zip", {"Textures/a.dds": b"a"})
    outside = make_tree(tmp_path / "outside", {"secret.dds": b"s"})
    os.symlink(outside / "secret.dds", prepared.files_dir / "Textures" / "link.dds")
    for mapping in (
        FileMapping("Textures/link.dds", "Data/Textures/link.dds"),
        FileMapping("../outside/secret.dds", "Data/Textures/s.dds"),
        FileMapping("Textures/missing.dds", "Data/Textures/m.dds"),
        FileMapping("Textures", "Data/Textures"),
        FileMapping("Textures/a.dds", "../escape.dds"),
        FileMapping("Textures/a.dds", "/abs.dds"),
    ):
        prepared.plan.mappings = [mapping]
        with pytest.raises(StagingError, match="can't install"):
            commit(prepared, manifest, "fallout4")
    assert manifest.mods("fallout4") == []
    assert prepared.files_dir.is_dir()
    discard(prepared)


def test_commit_twice_or_after_discard(tmp_path, manifest):
    prepared = _fake_prepared(tmp_path, "m.zip")
    commit(prepared, manifest, "fallout4")
    with pytest.raises(StagingError, match="already committed"):
        commit(prepared, manifest, "fallout4")
    other = _fake_prepared(tmp_path, "n.zip")
    discard(other)
    with pytest.raises(StagingError):
        commit(other, manifest, "fallout4")
    assert len(manifest.mods("fallout4")) == 1


def test_commit_moves_files_back_when_manifest_fails(tmp_path, manifest, monkeypatch):
    prepared = _fake_prepared(tmp_path, "m.zip")

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(manifest, "add_mod", broken)
    with pytest.raises(RuntimeError):
        commit(prepared, manifest, "fallout4")
    assert sorted(os.listdir(paths.mods_dir("fallout4"))) == []
    assert (prepared.files_dir / "readme.txt").read_bytes() == b"read me"
    discard(prepared)


def test_commit_falls_back_to_move_across_filesystems(tmp_path, manifest, monkeypatch):
    prepared = _fake_prepared(tmp_path, "m.zip")
    real_rename = os.rename

    def rename(src, dst):
        if Path(src) == prepared.files_dir:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(staging.os, "rename", rename)
    result = commit(prepared, manifest, "fallout4")
    assert (result.mod.staging_dir() / "readme.txt").read_bytes() == b"read me"
    assert not prepared.files_dir.exists()
    discard(prepared)


@pytest.mark.parametrize("failure", ["disk full", "interrupted"])
def test_commit_failed_copy_across_filesystems_leaves_no_orphan(tmp_path, manifest, monkeypatch, failure):
    # A mods folder relocated to another drive: it fills up during the copy, or
    # Ctrl+C arrives while the temporary copy is being removed afterwards.
    prepared = _fake_prepared(tmp_path, "m.zip")
    real_rename, real_copyfile, real_rmtree = os.rename, shutil.copyfile, shutil.rmtree

    def rename(src, dst):
        if Path(src) == prepared.files_dir:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst)

    def copyfile(src, dst, **kwargs):
        if os.fspath(src).endswith("b.nif"):
            raise OSError(28, "No space left on device", os.fspath(dst))
        return real_copyfile(src, dst, **kwargs)

    def rmtree(path, *args, **kwargs):
        if Path(path) == prepared.files_dir:
            raise KeyboardInterrupt
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(staging.os, "rename", rename)
    if failure == "disk full":
        monkeypatch.setattr(staging.shutil, "copyfile", copyfile)
        with pytest.raises(StagingError, match="No space left"):
            commit(prepared, manifest, "fallout4")
    else:
        monkeypatch.setattr(staging.shutil, "rmtree", rmtree)
        with pytest.raises(KeyboardInterrupt):
            commit(prepared, manifest, "fallout4")
    assert os.listdir(paths.mods_dir("fallout4")) == []
    assert manifest.mods("fallout4") == []
    for rel, data in COOL_MOD.items():
        assert (prepared.files_dir / rel).read_bytes() == data
    discard(prepared)


@pytest.mark.parametrize("replacing", [False, True])
def test_commit_interrupted_after_manifest_write_leaves_no_half_recorded_mod(tmp_path, manifest, monkeypatch,
                                                                              replacing):
    # SIGTERM/SIGHUP raise SystemExit anywhere, e.g. just after the mod row was saved.
    old = None
    if replacing:
        first = _fake_prepared(tmp_path, "Cool Mod.zip")
        old = commit(first, manifest, "fallout4").mod
        discard(first)
    method = "update_mod" if replacing else "add_mod"
    real = getattr(manifest, method)

    def saved_then_interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise SystemExit(143)

    monkeypatch.setattr(manifest, method, saved_then_interrupted)
    prepared = _fake_prepared(tmp_path, "Cool Mod.zip", {"Textures/new.dds": b"new"})
    with pytest.raises(SystemExit):
        commit(prepared, manifest, "fallout4", replace=old)
    mods = manifest.mods("fallout4")
    assert sorted(os.listdir(paths.mods_dir("fallout4"))) == sorted(m.folder for m in mods)
    for mod in mods:
        assert all((mod.staging_dir() / f.source).is_file() for f in manifest.mod_files(mod.id))
    if replacing:
        assert [(m.id, m.folder) for m in mods] == [(old.id, old.folder)]
    else:
        assert mods == []
    assert (prepared.files_dir / "Textures" / "new.dds").read_bytes() == b"new"
    discard(prepared)


def test_commit_refuses_files_outside_the_apps_tmp(tmp_path, manifest):
    # Only an install folder made by prepare() is ever moved into the mods folder.
    user = make_tree(tmp_path / "home" / "My Mod", {"Textures/a.dds": b"a", "notes.txt": b"n"})
    prepared = _fake_prepared(tmp_path, "m.zip", {"Textures/a.dds": b"a"})
    link = paths.tmp_dir() / "install-link"
    os.symlink(user.parent, link)
    for files_dir in (user, link / "My Mod", paths.tmp_dir() / "x", prepared.workdir / "sub" / "x"):
        forged = Prepared(prepared.source, prepared.workdir, files_dir, prepared.files, prepared.plan)
        with pytest.raises(StagingError):
            commit(forged, manifest, "fallout4")
    assert (user / "Textures" / "a.dds").read_bytes() == b"a" and (user / "notes.txt").exists()
    assert manifest.mods("fallout4") == []
    assert not paths.mods_dir("fallout4").exists() or os.listdir(paths.mods_dir("fallout4")) == []
    discard(prepared)


def test_commit_replace_keeps_priority_enabled_and_id(tmp_path, manifest):
    old_prepared = _fake_prepared(tmp_path, "Cool Mod-1234-1-0-1600000000.zip")
    old = commit(old_prepared, manifest, "fallout4", name="My Cool Mod").mod
    discard(old_prepared)
    later = manifest.add_mod("fallout4", "Later", "Later", [])
    manifest.set_enabled(old.id, False)
    old_folder = old.staging_dir()

    new_files = {"Cool Mod/CoolMod.esp": FO4_PLUGIN, "Cool Mod/Textures/new.dds": b"new"}
    new_prepared = _fake_prepared(tmp_path, "Cool Mod-1234-2-0-1700000000.zip", new_files)
    assert find_existing(manifest, "fallout4", new_prepared).id == old.id
    result = commit(new_prepared, manifest, "fallout4", replace=manifest.mod(old.id))
    discard(new_prepared)

    mod = result.mod
    assert result.replaced is True
    assert result.old_folder == old_folder
    assert old_folder.is_dir()  # still there until the caller redeploys and removes it
    assert mod.id == old.id
    assert mod.priority == old.priority < later.priority
    assert mod.enabled is False
    assert mod.name == "My Cool Mod"
    assert mod.version == "2.0"
    assert mod.folder == "My Cool Mod (2)"
    assert mod.source_archive.endswith("Cool Mod-1234-2-0-1700000000.zip")
    assert _dests(manifest.mod_files(mod.id)) == {
        "Cool Mod/CoolMod.esp": "Data/CoolMod.esp",
        "Cool Mod/Textures/new.dds": "Data/Textures/new.dds",
    }
    assert (mod.staging_dir() / "Cool Mod/Textures/new.dds").read_bytes() == b"new"
    assert [m.id for m in manifest.mods("fallout4")] == [old.id, later.id]

    remove_staging(result.old_folder)
    assert not old_folder.exists()

    renamed = _fake_prepared(tmp_path, "Cool Mod-1234-3-0-1800000000.zip", new_files)
    again = commit(renamed, manifest, "fallout4", replace=mod, name="Cool Mod Renamed")
    discard(renamed)
    assert again.mod.name == "Cool Mod Renamed" and again.mod.folder == "Cool Mod Renamed"
    assert again.mod.version == "3.0"


def test_commit_replace_must_be_same_game_and_still_installed(tmp_path, manifest):
    other_game = manifest.add_mod("skyrimse", "Cool Mod", "Cool Mod", [])
    prepared = _fake_prepared(tmp_path, "m.zip")
    with pytest.raises(StagingError, match="skyrimse"):
        commit(prepared, manifest, "fallout4", replace=other_game)
    gone = manifest.add_mod("fallout4", "Gone", "Gone", [])
    manifest.remove_mod(gone.id)
    with pytest.raises(StagingError, match="no longer installed"):
        commit(prepared, manifest, "fallout4", replace=gone)
    with pytest.raises(StagingError, match="empty"):
        commit(prepared, manifest, "fallout4", name=" \t ")
    assert prepared.files_dir.is_dir()
    discard(prepared)


# ---------------------------------------------------------------- deleting


def test_discard_is_idempotent(tmp_path):
    src = make_tree(tmp_path / "Mod", {"Textures/a.dds": b"a"})
    prepared = prepare(src)
    assert prepared.workdir.is_dir()
    discard(prepared)
    assert not prepared.workdir.exists()
    discard(prepared)
    assert src.is_dir()


def test_discard_logs_disk_errors_instead_of_raising(tmp_path, monkeypatch, caplog):
    prepared = _fake_prepared(tmp_path, "m.zip")
    real_rmtree = shutil.rmtree

    def fail(path, *args, **kwargs):
        raise OSError(5, "Input/output error", path)

    monkeypatch.setattr(staging.shutil, "rmtree", fail)
    discard(prepared)
    assert "Input/output error" in caplog.text
    assert prepared.workdir.exists()
    monkeypatch.setattr(staging.shutil, "rmtree", real_rmtree)
    discard(prepared)
    assert not prepared.workdir.exists()


def test_discard_refuses_foreign_workdir(tmp_path):
    victim = make_tree(tmp_path / "install-victim", {"keep.txt": b"k"})
    prepared = _fake_prepared(tmp_path, "m.zip")
    nested = make_tree(prepared.workdir / "install-nested", {"keep.txt": b"k"})
    for workdir in (victim, paths.tmp_dir(), prepared.files_dir, nested, tmp_path / "data"):
        prepared.workdir = workdir
        with pytest.raises(StagingError):
            discard(prepared)
    assert (victim / "keep.txt").exists() and (nested / "keep.txt").exists()
    link = paths.tmp_dir() / "install-link"
    os.symlink(victim, link)
    prepared.workdir = link
    with pytest.raises(StagingError, match="symlink"):
        discard(prepared)
    assert (victim / "keep.txt").exists()


def test_remove_staging_deletes_only_inside_mods_dir(tmp_path):
    mods = paths.mods_dir()
    target = make_tree(mods / "fallout4" / "Cool Mod", {"Textures/a.dds": b"a", "readme.txt": b"r"})
    outside = make_tree(tmp_path / "outside", {"keep.txt": b"k"})
    make_tree(mods / "fallout4" / "Other", {"keep.txt": b"k"})

    os.symlink(outside, mods / "fallout4" / "Evil Link")
    os.symlink(outside, mods / "skyrimse")
    refused = [
        outside,
        tmp_path,
        mods,
        mods / "fallout4",
        mods / "fallout4" / "..",
        mods / "fallout4" / "Cool Mod" / ".." / ".." / ".." / "outside",
        mods / "fallout4" / "Cool Mod" / "Textures",
        mods / "fallout4" / "Evil Link",
        mods / "skyrimse" / "keep.txt",
        Path("/"),
        Path(os.path.relpath(outside)),
    ]
    for path in refused:
        with pytest.raises(StagingError):
            remove_staging(path)
    assert (outside / "keep.txt").read_bytes() == b"k"
    assert (mods / "fallout4" / "Other" / "keep.txt").exists()
    assert target.is_dir()

    (target / "Textures").chmod(0o500)
    remove_staging(target)
    assert not target.exists()
    assert (mods / "fallout4").is_dir()
    remove_staging(target)  # already gone: fine
    assert (outside / "keep.txt").exists()


def test_remove_staging_does_not_follow_links_inside(tmp_path):
    outside = make_tree(tmp_path / "outside", {"keep.txt": b"k"})
    target = make_tree(paths.mods_dir("fallout4") / "Mod", {"a.dds": b"a"})
    os.symlink(outside, target / "linked dir")
    os.symlink(outside / "keep.txt", target / "linked.txt")
    remove_staging(target)
    assert not target.exists()
    assert (outside / "keep.txt").read_bytes() == b"k"


def test_remove_staging_with_relocated_mods_folder(tmp_path, manifest):
    drive = make_tree(tmp_path / "drive", {"photos/2020/keep.jpg": b"p"})
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    os.symlink(drive, paths.mods_dir())
    # Resolved, the photos folder is two levels below the mods folder, but
    # the app only ever deletes paths it spells inside its own mods folder.
    with pytest.raises(StagingError):
        remove_staging(drive / "photos" / "2020")
    assert (drive / "photos" / "2020" / "keep.jpg").exists()

    prepared = _fake_prepared(tmp_path, "m.zip")
    result = commit(prepared, manifest, "fallout4")
    discard(prepared)
    assert (drive / "fallout4" / result.mod.folder / "readme.txt").exists()
    remove_staging(result.mod.staging_dir())
    assert not (drive / "fallout4" / result.mod.folder).exists()
    assert (drive / "photos" / "2020" / "keep.jpg").exists()


def test_remove_staging_refuses_a_file(tmp_path):
    f = paths.mods_dir("fallout4") / "file.txt"
    make_tree(paths.mods_dir("fallout4"), {"file.txt": b"x"})
    with pytest.raises(StagingError, match="not a folder"):
        remove_staging(f)
    assert f.exists()


# ---------------------------------------------------------------- logging and isolation


def test_file_operations_are_logged(tmp_path, manifest, caplog):
    caplog.set_level(logging.INFO, logger="bethmm.staging")
    src = make_tree(tmp_path / "Mod", {"Textures/a.dds": b"a"})
    prepared = prepare(src, target=Target.DATA)
    result = commit(prepared, manifest, "fallout4")
    discard(prepared)
    remove_staging(result.mod.staging_dir())
    messages = [r.getMessage() for r in caplog.records if r.name == "bethmm.staging"]
    assert any(m.startswith("MKDIR ") and "install-" in m for m in messages)
    assert any(m.startswith("COPY ") for m in messages)
    assert any(m.startswith("MOVE ") and str(result.mod.staging_dir()) in m for m in messages)
    assert any(m.startswith("DELETE ") and str(prepared.workdir) in m for m in messages)
    assert any(m.startswith("DELETE ") and str(result.mod.staging_dir()) in m for m in messages)


@needs_7z
def test_nothing_outside_tmp_and_mods_is_written(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / ".local" / "share" / "beth-mod-manager"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BETHMM_DATA_DIR", str(data))
    monkeypatch.setenv("BETHMM_CONFIG_DIR", str(home / ".config" / "beth-mod-manager"))
    downloads = home / "Downloads"
    _zip(downloads / "CoolMod-1234-1-2-1680000000.zip", COOL_MOD)
    make_tree(downloads / "Loose Mod", {"Data/Loose.esp": FO4_PLUGIN, "Data/Textures/l.dds": b"l"})
    os.symlink(downloads / "CoolMod-1234-1-2-1680000000.zip", downloads / "Loose Mod" / "link.zip")
    make_tree(home / "Games" / "fallout-4" / "drive_c" / "Fallout 4", {"Fallout4.exe": b"exe", "Data/Fallout4.esm": b"esm"})
    manifest = Manifest()  # created up front: the manifest is not staging's to write
    manifest.close()

    def allowed(rel: str) -> bool:
        prefix = ".local/share/beth-mod-manager/"
        return rel.startswith((prefix + "tmp", prefix + "mods", prefix + "manifest.db"))

    def outside_snapshot() -> dict[str, tuple]:
        return {k: v for k, v in _snapshot(home).items() if not allowed(k)}

    before = outside_snapshot()
    with Manifest() as manifest:
        for source in (downloads / "CoolMod-1234-1-2-1680000000.zip", downloads / "Loose Mod"):
            prepared = prepare(source)
            try:
                result = commit(prepared, manifest, "fallout4")
            finally:
                discard(prepared)
            assert paths.mods_dir("fallout4") in result.mod.staging_dir().parents
        failing = _zip(downloads / "broken.zip", {"x/y.dat": b"y"})
        before[failing.relative_to(home).as_posix()] = _snapshot(home)[failing.relative_to(home).as_posix()]
        with pytest.raises(ValueError):
            prepare(failing, roots=["nope"], target=Target.DATA)
        for mod in manifest.mods("fallout4"):
            remove_staging(mod.staging_dir())
    assert outside_snapshot() == before
    assert sorted(os.listdir(data / "tmp")) == []
    assert sorted(os.listdir(data / "mods" / "fallout4")) == []
