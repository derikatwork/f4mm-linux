"""Tests for deploying, undeploying and purging mods (bethmm.deploy)."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

from bethmm import deploy, paths
from bethmm.deploy import DeployError, GameTarget
from bethmm.manifest import Manifest, ModFile, ModRecord
from bethmm.pathresolve import fold_path

from helpers import make_tree

GAME = "fallout4"

VANILLA: dict[str, str] = {
    "Fallout4.exe": "vanilla exe",
    "Data/Fallout4.esm": "vanilla esm",
    "Data/Textures/Armor/a.dds": "vanilla armor a",
    "Data/Textures/Armor/b.dds": "vanilla armor b",
    "Data/meshes/actors/m.nif": "vanilla mesh",
    "Data/Scripts/s.pex": "vanilla script",
    "Data/Sound/Voice/v.fuz": "vanilla voice",
}


# ---------------------------------------------------------------- shared helpers


def snapshot(root: Path) -> dict[str, tuple]:
    """Every path below *root*: type, and for files size, content hash and inode."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(full))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir",)
            else:
                data = Path(full).read_bytes()
                out[rel] = ("file", st.st_size, hashlib.sha256(data).hexdigest(), st.st_ino)
    return out


#: Extracted archives keep the files' old modification times.
ARCHIVE_MTIME = 1_577_880_000  # 2020-01-01


def stage(game: str, folder: str, files: Mapping[str, str | bytes]) -> Path:
    """Write staged files (source == dest) into the mods folder."""
    base = make_tree(paths.mods_dir(game) / folder, dict(files))
    for rel in files:
        os.utime(base / rel, (ARCHIVE_MTIME, ARCHIVE_MTIME))
    return base


def add_mod(manifest: Manifest, name: str, files: Mapping[str, str | bytes], *, game: str = GAME,
            folder: str | None = None, enabled: bool = True) -> ModRecord:
    folder = folder or name
    stage(game, folder, files)
    return manifest.add_mod(game, name, folder, [ModFile(d, d) for d in files], enabled=enabled)


def staged(mod: ModRecord, source: str) -> Path:
    return paths.mods_dir(mod.game_id) / mod.folder / source


def winners(manifest: Manifest, game: str = GAME) -> dict[str, tuple[ModRecord, ModFile, Path]]:
    """Oracle: folded dest -> the enabled mod that should provide it (highest
    priority with an existing staged file)."""
    out: dict[str, tuple[ModRecord, ModFile, Path]] = {}
    for mod in manifest.mods(game):
        if not mod.enabled:
            continue
        for f in manifest.mod_files(mod.id):
            src = staged(mod, f.source)
            if src.is_file():
                out[fold_path(f.dest)] = (mod, f, src)
    return out


def folded_index(snap: Mapping[str, tuple]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for rel in snap:
        index.setdefault(fold_path(rel), []).append(rel)
    return index


@dataclass
class World:
    root: Path
    manifest: Manifest
    vanilla: dict[str, tuple]
    mode: str = "symlink"
    game: str = GAME

    def target(self, mode: str | None = None, root: Path | None = None) -> GameTarget:
        return GameTarget(self.game, root or self.root, mode or self.mode)

    def deploy(self, **kw) -> deploy.SyncResult:
        return deploy.deploy(self.manifest, self.target(), **kw)

    def purge(self, **kw) -> deploy.SyncResult:
        return deploy.purge(self.manifest, self.target(), **kw)

    def refresh(self, **kw) -> deploy.SyncResult | None:
        return deploy.refresh(self.manifest, self.target(), **kw)

    def plan(self) -> deploy.SyncPlan:
        return deploy.plan_sync(self.manifest, self.target())

    def path(self, rel: str) -> Path:
        return self.root / rel


def make_world(base: Path, manifest: Manifest, vanilla: Mapping[str, str | bytes] = VANILLA,
               mode: str = "symlink") -> World:
    root = base / "Fallout 4"
    root.mkdir(parents=True)
    make_tree(root, dict(vanilla))
    return World(root, manifest, snapshot(root), mode)


def assert_deployed(world: World, *, allow_foreign: Mapping[str, tuple] | None = None) -> None:
    """The game folder matches the manifest's enabled mods exactly (see the
    adversarial tests for what is checked)."""
    m, root, game = world.manifest, world.root, world.game
    snap = snapshot(root)
    index = folded_index(snap)
    want = winners(m, game)
    deployed = m.deployed(game)
    backups = m.backups(game)
    created = m.created_dirs(game)
    allow_foreign = dict(allow_foreign or {})

    # No case-variant duplicates were created.
    dupes = {k: v for k, v in index.items() if len(v) > 1}
    vanilla_dupes = {k: v for k, v in folded_index(world.vanilla).items() if len(v) > 1}
    assert dupes == vanilla_dupes, f"case duplicates: {dupes}"

    staged_ids = {}
    for mod in m.mods(game):
        for f in m.mod_files(mod.id):
            src = staged(mod, f.source)
            if src.is_file():
                st = os.stat(src)
                staged_ids[(st.st_ino, st.st_dev)] = src

    # Every winning file is linked to the right staged file.
    for key, (mod, f, src) in want.items():
        assert key in index, f"{f.dest} of {mod.name} is not deployed"
        (rel,) = index[key]
        entry = snap[rel]
        if world.mode == "symlink":
            assert entry == ("link", str(src)), f"{rel}: {entry} should link to {src}"
        else:
            assert entry[0] == "file" and entry[3] == os.stat(src).st_ino, f"{rel} is not a hardlink of {src}"
        assert key in deployed and deployed[key].mod_id == mod.id and deployed[key].link_type == world.mode

    # Nothing else links into the mods folder or shares a staged file's inode.
    mods_root = os.path.realpath(paths.mods_dir())
    for rel, entry in snap.items():
        key = fold_path(rel)
        if entry[0] == "link" and os.path.realpath(root / rel).startswith(mods_root + os.sep):
            assert key in want and str(want[key][2]) == entry[1], f"stray link {rel} -> {entry[1]}"
        if entry[0] == "file":
            full = root / rel
            st = os.lstat(full)
            if (st.st_ino, st.st_dev) in staged_ids:
                assert key in want and world.mode == "hardlink" and os.path.samefile(full, want[key][2]), \
                    f"stray hardlink {rel}"
    assert set(deployed) == set(want) - {fold_path(p) for p in allow_foreign}, \
        f"manifest links {sorted(deployed)} != wanted {sorted(want)}"

    # Every original file is in place (same inode) or safely in the backups.
    for rel, entry in world.vanilla.items():
        key = fold_path(rel)
        if entry[0] != "file":
            assert snap.get(rel) == entry, f"vanilla {rel} changed"
            continue
        if key in want or rel in allow_foreign:
            assert key in backups, f"original {rel} is neither in place nor backed up"
            b = Path(backups[key].backup)
            st = os.lstat(b)
            assert (st.st_size, st.st_ino) == (entry[1], entry[3]), f"backup of {rel} is not the original"
            assert hashlib.sha256(b.read_bytes()).hexdigest() == entry[2]
        else:
            assert snap.get(rel) == entry, f"original {rel} is not back in place: {snap.get(rel)}"
            assert key not in backups

    # Anything else in the folder is a folder the app created and still needs.
    needed_dirs = set()
    for key in deployed:
        parts = key.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            needed_dirs.add("/".join(parts[:i]))
    for rel, entry in snap.items():
        key = fold_path(rel)
        if rel in world.vanilla or key in want or rel in allow_foreign:
            continue
        assert entry == ("dir",), f"unexpected {rel}: {entry}"
        assert key in created, f"folder {rel} is neither original nor recorded"
    for key, rel in created.items():
        assert snap.get(rel) == ("dir",), f"recorded folder {rel} is missing"
        assert key in needed_dirs, f"recorded folder {rel} is no longer needed but still there"

    # A second deploy has nothing to do.
    plan = world.plan()
    assert plan.ops == [], f"second plan not empty: {plan.ops}"


def assert_vanilla(world: World, *, empty_records: bool = True) -> None:
    assert snapshot(world.root) == world.vanilla
    if empty_records:
        m = world.manifest
        assert m.deployed(world.game) == {}
        assert m.backups(world.game) == {}
        assert m.created_dirs(world.game) == {}
        assert not m.has_footprint(world.game)


@pytest.fixture(autouse=True)
def _fast_sqlite(monkeypatch):
    """Skip fsync in the manifest: these tests make thousands of tiny commits.
    (Simulated crashes are exceptions inside the process, so committed data is
    still there for the next connection.)"""
    init = Manifest.__init__

    def fast_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self._db.execute("PRAGMA synchronous = OFF")

    monkeypatch.setattr(Manifest, "__init__", fast_init)


@pytest.fixture
def manifest():
    with Manifest(lock=True) as m:
        yield m


@pytest.fixture
def world(tmp_path, manifest):
    return make_world(tmp_path, manifest)


# ---------------------------------------------------------------- basic deploy


def test_deploy_creates_per_file_symlinks_to_staged_files(world):
    mod = add_mod(world.manifest, "Mod A", {"Data/ModA.esp": "esp", "Data/Textures/ModA/t.dds": "tex"})
    result = world.deploy()
    assert not result.failed and not result.dry_run
    esp = world.path("Data/ModA.esp")
    assert esp.is_symlink() and os.readlink(esp) == str(staged(mod, "Data/ModA.esp"))
    assert os.path.isabs(os.readlink(esp))
    tex_dir = world.path("Data/Textures/ModA")
    assert tex_dir.is_dir() and not tex_dir.is_symlink()  # folders are real, only files are links
    assert world.path("Data/Textures/ModA/t.dds").read_text() == "tex"
    assert world.manifest.game_state(GAME).deployed
    assert list(world.manifest.created_dirs(GAME).values()) == ["Data/Textures/ModA"]
    assert_deployed(world)


def test_deploy_journals_every_operation(world):
    add_mod(world.manifest, "Mod A", {"Data/ModA.esp": "esp", "Data/Fallout4.esm": "replacement"})
    result = world.deploy()
    history = world.manifest.history(GAME)
    assert [(e.action, e.path, e.status) for e in history] == [(op.kind, op.path, "done") for op in result.done]
    assert {op.kind for op in result.done} == {"backup", "link"}
    assert world.manifest.unfinished_journal(GAME) == []


def test_case_insensitive_merge_into_existing_folders(world):
    add_mod(world.manifest, "Retex", {"DATA/textures/ARMOR/c.dds": "c", "data/MESHES/Actors/n.nif": "n"})
    world.deploy()
    assert world.path("Data/Textures/Armor/c.dds").is_symlink()
    assert world.path("Data/meshes/actors/n.nif").is_symlink()
    assert world.manifest.created_dirs(GAME) == {}
    names = os.listdir(world.root / "Data")
    assert sorted(names) == ["Fallout4.esm", "Scripts", "Sound", "Textures", "meshes"]
    assert_deployed(world)


def test_new_folders_follow_parent_convention(world):
    # Data/meshes has only lowercase sub-folders; Data/Textures only capitalized ones.
    add_mod(world.manifest, "Weapons", {"Data/Meshes/Weapons/Rifle/r.nif": "r", "Data/textures/weapons/r.dds": "t"})
    world.deploy()
    # A brand-new folder has no siblings to copy, so "Rifle" keeps the mod's spelling.
    assert world.path("Data/meshes/weapons/Rifle/r.nif").is_symlink()
    assert world.path("Data/Textures/Weapons/r.dds").is_symlink()
    assert set(world.manifest.created_dirs(GAME).values()) == {
        "Data/meshes/weapons", "Data/meshes/weapons/Rifle", "Data/Textures/Weapons"}
    assert_deployed(world)


def test_two_mods_with_different_case_share_a_new_folder(world):
    add_mod(world.manifest, "A", {"Data/Textures/NewArmor/a.dds": "a"})
    add_mod(world.manifest, "B", {"data/TEXTURES/newarmor/b.dds": "b"})
    world.deploy()
    assert sorted(os.listdir(world.root / "Data/Textures")) == ["Armor", "NewArmor"]
    assert sorted(os.listdir(world.root / "Data/Textures/NewArmor")) == ["a.dds", "b.dds"]
    assert_deployed(world)


def test_created_folders_are_removed_when_no_longer_needed(world):
    a = add_mod(world.manifest, "A", {"Data/Textures/New/Deep/a.dds": "a"})
    b = add_mod(world.manifest, "B", {"Data/Textures/New/b.dds": "b"})
    world.deploy()
    assert set(world.manifest.created_dirs(GAME).values()) == {"Data/Textures/New", "Data/Textures/New/Deep"}
    world.manifest.set_enabled(a.id, False)
    world.refresh()
    assert not world.path("Data/Textures/New/Deep").exists()
    assert world.path("Data/Textures/New/b.dds").is_symlink()
    assert set(world.manifest.created_dirs(GAME).values()) == {"Data/Textures/New"}
    assert_deployed(world)
    world.manifest.set_enabled(b.id, False)
    world.refresh()
    assert not world.path("Data/Textures/New").exists()
    assert world.manifest.created_dirs(GAME) == {}
    assert_vanilla(world)


def test_files_in_game_root(world):
    """A mod file directly in the game folder (f4se_loader.exe) must not crash planning."""
    mod = add_mod(world.manifest, "F4SE", {"f4se_loader.exe": "loader", "F4SE/Plugins/x.dll": "dll",
                                           "fallout4.EXE": "patched"})
    world.deploy()
    assert world.path("f4se_loader.exe").is_symlink()
    assert os.readlink(world.path("Fallout4.exe")) == str(staged(mod, "fallout4.EXE"))
    assert_deployed(world)
    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    assert_vanilla(world)


# ---------------------------------------------------------------- vanilla files


def test_vanilla_file_is_backed_up_and_restored_with_same_inode(world):
    original = world.vanilla["Data/Fallout4.esm"]
    mod = add_mod(world.manifest, "ESM", {"data/FALLOUT4.ESM": "modded"})
    world.deploy()
    link = world.path("Data/Fallout4.esm")
    assert link.is_symlink() and link.read_text() == "modded"
    assert not world.path("data").exists()
    (backup,) = world.manifest.backups(GAME).values()
    assert backup.path == "Data/Fallout4.esm"
    b = Path(backup.backup)
    assert b.is_relative_to(paths.backups_dir(GAME))
    assert os.lstat(b).st_ino == original[3]
    assert_deployed(world)

    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    assert snapshot(world.root)["Data/Fallout4.esm"] == original
    assert world.manifest.backups(GAME) == {}
    assert not b.exists() and not paths.backups_dir(GAME).joinpath("Data").exists()  # emptied folders pruned
    assert_vanilla(world)


@pytest.mark.parametrize("how", ["disable", "uninstall", "purge"])
def test_vanilla_restored_on_disable_uninstall_purge(world, how):
    mod = add_mod(world.manifest, "Retex", {"Data/Textures/Armor/a.dds": "mine", "Data/Textures/Armor/new.dds": "n"})
    world.deploy()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "mine"
    if how == "purge":
        world.purge()
        assert world.manifest.game_state(GAME).deployed is False
        assert world.manifest.mods(GAME)  # mods stay installed
    else:
        world.manifest.set_enabled(mod.id, False)
        world.refresh()
        if how == "uninstall":
            world.manifest.remove_mod(mod.id)
    assert_vanilla(world)


def test_two_mods_higher_priority_wins_and_reorder_switches_minimally(world):
    a = add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "A's a", "Data/A.esp": "a esp",
                                      "Data/shared.txt": "A shared"})
    b = add_mod(world.manifest, "B", {"data/textures/armor/A.DDS": "B's a", "Data/B.esp": "b esp",
                                      "Data/Shared.TXT": "B shared"})
    world.deploy()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "B's a"
    # A new file keeps the winning mod's spelling.
    assert world.path("Data/Shared.TXT").read_text() == "B shared"
    assert_deployed(world)

    world.manifest.set_order(GAME, [b.id, a.id])
    plan = world.plan()
    # Only the two contested paths change: unlink B's, link A's.
    assert sorted((op.kind, fold_path(op.path)) for op in plan.ops) == [
        ("link", "DATA/SHARED.TXT"), ("link", "DATA/TEXTURES/ARMOR/A.DDS"),
        ("unlink", "DATA/SHARED.TXT"), ("unlink", "DATA/TEXTURES/ARMOR/A.DDS"),
    ]
    assert plan.conflicts == {"DATA/SHARED.TXT": [b.id, a.id], "DATA/TEXTURES/ARMOR/A.DDS": [b.id, a.id]}
    world.refresh()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "A's a"
    # The original is still in the backups, untouched by the switch.
    assert_deployed(world)


def test_disable_winner_falls_back_to_next_provider_then_vanilla(world):
    a = add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "A"})
    b = add_mod(world.manifest, "B", {"Data/Textures/Armor/a.dds": "B"})
    c = add_mod(world.manifest, "C", {"Data/Textures/Armor/a.dds": "C"})
    path = world.path("Data/Textures/Armor/a.dds")
    world.deploy()
    assert path.read_text() == "C"
    world.manifest.set_enabled(c.id, False)
    world.refresh()
    assert path.read_text() == "B"
    assert_deployed(world)
    world.manifest.set_enabled(a.id, False)  # a loser: nothing changes on disk
    assert world.plan().ops == []
    world.manifest.set_enabled(b.id, False)
    world.refresh()
    assert snapshot(world.root)["Data/Textures/Armor/a.dds"] == world.vanilla["Data/Textures/Armor/a.dds"]
    assert_vanilla(world)


def test_conflict_summary(world):
    a = add_mod(world.manifest, "A", {"Data/x.txt": "a", "Data/y.txt": "a", "Data/only_a.txt": "a"})
    b = add_mod(world.manifest, "B", {"Data/X.txt": "b", "Data/z.txt": "b"})
    c = add_mod(world.manifest, "C", {"data/x.TXT": "c", "Data/y.txt": "c", "Data/z.txt": "c"})
    d = add_mod(world.manifest, "D", {"Data/x.txt": "d"}, enabled=False)
    summary = deploy.conflict_summary(world.manifest, GAME)
    assert summary[c.id].overwrites == {a.id: 2, b.id: 2}
    assert summary[c.id].overwritten_by == {}
    # Only the winner of each file counts: B never wins a file, so it overwrites nothing.
    assert summary[a.id].overwritten_by == {c.id: 2}
    assert summary[a.id].overwrites == {}
    assert summary[b.id].overwrites == {}
    assert summary[b.id].overwritten_by == {c.id: 2}
    assert d.id not in summary
    desired, providers = deploy.desired_files(world.manifest, GAME)
    assert desired["DATA/X.TXT"][0].id == c.id
    assert providers["DATA/X.TXT"] == [a.id, b.id, c.id]
    assert providers["DATA/ONLY_A.TXT"] == [a.id]


def test_second_deploy_is_a_noop(world):
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Textures/Armor/a.dds": "x", "Data/New/n.txt": "n"})
    world.deploy()
    before = snapshot(world.root)
    history = len(world.manifest.history(GAME, limit=1000))
    plan = world.plan()
    assert plan.ops == [] and plan.changes == []
    result = world.deploy()
    assert result.done == [] and result.failed == []
    assert snapshot(world.root) == before
    assert len(world.manifest.history(GAME, limit=1000)) == history


def test_dry_run_changes_nothing(world):
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "x", "Data/New/n.txt": "n"})
    data_before = snapshot(paths.data_dir())
    result = world.deploy(dry_run=True)
    assert result.dry_run
    assert {op.kind for op in result.done} == {"mkdir", "backup", "link"}
    assert result.done == result.plan.ops
    assert_vanilla(world)
    assert world.manifest.game_state(GAME) is None
    assert world.manifest.history(GAME) == []
    after = snapshot(paths.data_dir())
    # Only the database files may differ (sqlite bookkeeping); nothing was staged or backed up.
    strip = lambda snap: {k: v for k, v in snap.items() if not k.startswith("manifest.db")}  # noqa: E731
    assert strip(after) == strip(data_before)

    world.deploy()
    deployed_snap = snapshot(world.root)
    world.manifest.set_enabled(world.manifest.mods(GAME)[0].id, False)
    result = world.refresh(dry_run=True)
    assert {op.kind for op in result.done} == {"unlink", "restore", "rmdir"}
    assert snapshot(world.root) == deployed_snap
    result = world.purge(dry_run=True)
    assert result.plan.purge
    assert snapshot(world.root) == deployed_snap
    assert world.manifest.game_state(GAME).deployed


# ---------------------------------------------------------------- link modes


def test_hardlink_mode(world):
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm"})
    world.deploy()
    esp = world.path("Data/A.esp")
    assert not esp.is_symlink()
    assert os.stat(esp).st_ino == os.stat(staged(mod, "Data/A.esp")).st_ino
    record = world.manifest.deployed(GAME)["DATA/A.ESP"]
    assert record.link_type == "hardlink" and record.inode == os.stat(esp).st_ino
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_check_link_mode_across_devices(world, monkeypatch):
    monkeypatch.setattr(deploy, "_same_dev", lambda a, b: False)
    deploy.check_link_mode(world.target("symlink"))  # symlinks don't care
    with pytest.raises(DeployError, match="same filesystem"):
        deploy.check_link_mode(world.target("hardlink"))
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    with pytest.raises(DeployError):
        deploy.deploy(world.manifest, world.target("hardlink"))
    assert_vanilla(world)


def test_check_link_mode_missing_game_folder(tmp_path):
    with pytest.raises(DeployError):
        deploy.check_link_mode(GameTarget(GAME, tmp_path / "nope", "hardlink"))


def test_invalid_link_mode():
    with pytest.raises(ValueError):
        GameTarget(GAME, Path("/x"), "copy")


@pytest.mark.parametrize("first,second", [("symlink", "hardlink"), ("hardlink", "symlink")])
def test_switching_link_mode_relinks_everything(world, first, second):
    world.mode = first
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm", "Data/New/x.txt": "x"})
    world.deploy()
    assert_deployed(world)
    world.mode = second
    plan = world.plan()
    assert sorted(op.kind for op in plan.ops) == ["link"] * 3 + ["unlink"] * 3  # backups and folders stay
    world.deploy()
    assert all(r.link_type == second for r in world.manifest.deployed(GAME).values())
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_hardlinked_file_written_in_place_is_still_ours(world):
    """The game (or the user) may write to a deployed file; with hardlinks that
    is the staged file itself, so it is still the app's link."""
    world.mode = "hardlink"
    add_mod(world.manifest, "A", {"Data/MCM/Settings/a.ini": "x=1", "Data/Fallout4.esm": "esm"})
    world.deploy()
    with open(world.path("Data/MCM/Settings/a.ini"), "a") as f:
        f.write("\ny=2\n")
    assert world.plan().ops == []
    world.purge()
    assert_vanilla(world)


def test_hardlink_inode_reuse_is_not_mistaken_for_the_link(world):
    """If the staged file and the link are both deleted, a new file can get
    the same inode number; it must not be taken for the app's link."""
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "mod file"})
    world.deploy()
    (record,) = world.manifest.deployed(GAME).values()
    staged(mod, "Data/A.esp").unlink()
    p = world.path("Data/A.esp")
    p.unlink()
    p.write_text("the user's new file")
    st = os.stat(p)
    # Make the record look as if the new file had reused the inode number.
    world.manifest.record_link(deploy.DeployedFile(GAME, record.path, record.mod_id, record.source, "hardlink",
                                                   st.st_ino, st.st_dev, record.size, record.mtime_ns))
    report = deploy.verify(world.manifest, world.target())
    assert [r.path for r in report.foreign] == ["Data/A.esp"]
    world.purge()
    assert p.read_text() == "the user's new file"


def test_hardlink_relinks_when_staged_file_is_replaced(world):
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "old"})
    world.deploy()
    src = staged(mod, "Data/A.esp")
    tmp = src.with_name("new")
    tmp.write_text("new")
    os.replace(tmp, src)  # an editor's atomic save: new inode
    plan = world.plan()
    assert [op.kind for op in plan.ops] == ["unlink", "link"]
    world.deploy()
    assert world.path("Data/A.esp").read_text() == "new"
    assert_deployed(world)


# ---------------------------------------------------------------- purge / refresh / roots


def test_purge_restores_exact_original_state(world):
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "a", "DATA/TEXTURES/ARMOR/B.DDS": "b",
                                  "Data/Fallout4.esm": "esm", "data/new/deep/x.txt": "x", "f4se_loader.exe": "l"})
    add_mod(world.manifest, "B", {"Data/textures/armor/a.dds": "b's a", "Data/Sound/Voice/v.fuz": "v"})
    world.deploy()
    assert_deployed(world)
    result = world.purge()
    assert not result.failed
    assert_vanilla(world)
    assert not any(paths.backups_dir(GAME).rglob("*"))
    state = world.manifest.game_state(GAME)
    assert state.deployed is False
    assert len(world.manifest.mods(GAME)) == 2


def test_purge_twice_is_harmless(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "esm"})
    world.deploy()
    world.purge()
    result = world.purge()
    assert result.done == [] and result.failed == []
    assert_vanilla(world)


def test_refresh_does_nothing_after_purge_until_deploy(world):
    a = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    assert world.refresh() is not None  # never deployed: refresh deploys
    world.purge()
    add_mod(world.manifest, "B", {"Data/B.esp": "b"})
    world.manifest.set_enabled(a.id, False)
    assert world.refresh() is None
    assert_vanilla(world)
    world.deploy()
    assert world.manifest.game_state(GAME).deployed
    assert world.path("Data/B.esp").is_symlink() and not world.path("Data/A.esp").exists()
    world.manifest.set_enabled(a.id, True)
    assert world.refresh() is not None
    assert_deployed(world)


def test_game_root_moved(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm"})
    world.deploy()
    new_root = tmp_path / "moved"
    shutil.copytree(world.root, new_root, symlinks=True)
    with pytest.raises(DeployError, match="purge"):
        deploy.deploy(world.manifest, world.target(root=new_root))
    with pytest.raises(DeployError):
        deploy.plan_sync(world.manifest, world.target(root=new_root))
    # Purge uses the folder the mods were deployed into.
    result = deploy.purge(world.manifest, world.target(root=new_root))
    assert result.plan.root == world.root
    assert_vanilla(world)
    # Now the new folder is fine.
    shutil.rmtree(new_root)
    make_tree(new_root, VANILLA)
    deploy.deploy(world.manifest, world.target(root=new_root))
    assert (new_root / "Data/A.esp").is_symlink()
    assert world.manifest.game_state(GAME).root == str(new_root)


def test_game_root_through_symlink_is_the_same_folder(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    alias = tmp_path / "alias"
    alias.symlink_to(world.root)
    deploy.deploy(world.manifest, world.target(root=alias))  # no DeployError: same folder
    deploy.purge(world.manifest, world.target(root=alias))
    assert_vanilla(world)


def test_missing_game_folder(tmp_path, manifest):
    with pytest.raises(DeployError, match="does not exist"):
        deploy.deploy(manifest, GameTarget(GAME, tmp_path / "nope"))


def test_mods_of_other_games_are_ignored(world):
    add_mod(world.manifest, "Sky", {"Data/sky.esp": "s"}, game="skyrimse")
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    assert not world.path("Data/sky.esp").exists()
    assert_deployed(world)


def test_reinstall_with_new_folder_relinks(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "v1", "Data/old.txt": "o"})
    world.deploy()
    stage(GAME, "A v2", {"Data/A.esp": "v2", "Data/new.txt": "n"})
    world.manifest.update_mod(mod.id, folder="A v2", files=[ModFile("Data/A.esp", "Data/A.esp"),
                                                             ModFile("Data/new.txt", "Data/new.txt")])
    world.refresh()
    shutil.rmtree(paths.mods_dir(GAME) / "A")
    assert world.path("Data/A.esp").read_text() == "v2"
    assert not world.path("Data/old.txt").exists()
    assert_deployed(world)


def test_disabled_mod_is_never_linked(world):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"}, enabled=False)
    world.deploy()
    assert not world.path("Data/A.esp").exists()
    assert_vanilla(world, empty_records=False)
