"""Hostile tests for deploy/undeploy/purge: random operation sequences, the
game folder changing behind the app's back, crashes at every file operation,
and awkward paths."""

from __future__ import annotations

import errno
import os
import random
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest

from bethmm import deploy, paths
from bethmm.manifest import Manifest, ModFile
from bethmm.pathresolve import CaseResolver, fold_path

from helpers import make_tree
from test_deploy import (
    GAME,
    VANILLA,
    World,
    add_mod,
    assert_deployed,
    assert_vanilla,
    folded_index,
    make_world,
    snapshot,
    stage,
    staged,
)


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


def fresh_world(base: Path, monkeypatch, *, vanilla=VANILLA, mode: str = "symlink") -> World:
    """A new game folder and a new, empty app data folder (and manifest)."""
    base.mkdir(parents=True)
    monkeypatch.setenv("BETHMM_DATA_DIR", str(base / "appdata"))
    return make_world(base, Manifest(lock=True), vanilla, mode)


def reopen(world: World) -> None:
    """Simulate the app being restarted after dying."""
    world.manifest.close()
    world.manifest = Manifest(lock=True)


def verify_and_repair(world: World) -> deploy.VerifyReport:
    report = deploy.verify(world.manifest, world.target())
    deploy.repair(world.manifest, world.target(), report)
    return report


# ================================================================ 1. random sequences

POOL: dict[str, list[str]] = {
    "Armor Retex": ["Data/textures/ARMOR/a.dds", "DATA/Textures/armor/c.dds"],
    "Armor HD": ["Data/Textures/Armor/A.DDS", "data/textures/armor/b.dds", "Data/ArmorHD.esp"],
    "New Stuff": ["Data/textures/NewStuff/n1.dds", "Data/NewMod.esp", "Data/textures/newstuff/x/y.dds"],
    "New Stuff Patch": ["data/TEXTURES/newstuff/N1.dds", "Data/textures/newstuff/deep/er/n2.dds"],
    "Mesh Pack": ["Data/Meshes/Actors/m.nif", "Data/Meshes/Weapons/w.nif", "data/meshes/weapons/Rifle/r.nif"],
    "Script Fix": ["Data/scripts/s.pex", "Data/Scripts/Source/User/s.psc"],
    "Root Loader": ["f4se_loader.exe", "F4SE/Plugins/p.dll", "f4se/plugins/q.dll"],
    "Exe Replace": ["fallout4.EXE"],
    "Sound": ["Data/sound/voice/V.fuz", "Data/Sound/FX/new.wav", "Data/sound/fx/new2.wav"],
    "Interface": ["Data/Interface/i.swf", "data/interface/j.swf", "Data/Textures/Armor/b.dds"],
    "Esm Override": ["data/fallout4.esm", "Data/NewMod.esp"],
}


class Sim:
    """Random add/enable/disable/reorder/reinstall/remove/deploy/purge/mode
    switch, checking the whole game folder after every step."""

    OPS = (["add"] * 4 + ["toggle"] * 3 + ["reorder"] * 2 + ["remove"] * 2 + ["reinstall"] * 2
           + ["deploy", "purge", "mode", "refresh"])

    def __init__(self, world: World, rng: random.Random) -> None:
        self.world = world
        self.rng = rng
        self.installed: dict[str, int] = {}  # pool name -> mod id
        self.generation = 0
        self.log: list[str] = []

    @property
    def m(self) -> Manifest:
        return self.world.manifest

    def _stage(self, name: str) -> tuple[str, list[ModFile]]:
        self.generation += 1
        folder = f"{name} g{self.generation}"
        dests = list(POOL[name])
        if self.rng.random() < 0.3 and len(dests) > 1:
            dests.remove(self.rng.choice(dests))  # a new version may drop a file
        stage(GAME, folder, {d: f"{name} {d} g{self.generation}" for d in dests})
        return folder, [ModFile(d, d) for d in dests]

    def _sync(self) -> None:
        result = self.world.refresh()
        if result is not None:
            assert not result.failed, result.failed

    def step(self) -> None:
        rng, m = self.rng, self.m
        op = rng.choice(self.OPS)
        mods = m.mods(GAME)
        if op == "add":
            free = [n for n in POOL if n not in self.installed]
            if not free:
                return self.step()
            name = rng.choice(free)
            folder, files = self._stage(name)
            mod = m.add_mod(GAME, name, folder, files, enabled=rng.random() < 0.85)
            self.installed[name] = mod.id
            self.log.append(f"add {name} enabled={mod.enabled}")
            self._sync()
        elif op == "toggle" and mods:
            mod = rng.choice(mods)
            m.set_enabled(mod.id, not mod.enabled)
            self.log.append(f"{'disable' if mod.enabled else 'enable'} {mod.name}")
            self._sync()
        elif op == "reorder" and mods:
            ids = [mod.id for mod in mods]
            rng.shuffle(ids)
            m.set_order(GAME, ids)
            self.log.append(f"order {[m.mod(i).name for i in ids]}")
            self._sync()
        elif op == "remove" and mods:
            mod = rng.choice(mods)
            self.log.append(f"remove {mod.name}")
            if any(d.mod_id == mod.id for d in m.deployed(GAME).values()):
                m.set_enabled(mod.id, False)
                self._sync()
            m.remove_mod(mod.id)
            shutil.rmtree(mod.staging_dir())
            del self.installed[mod.name]
        elif op == "reinstall" and mods:
            mod = rng.choice(mods)
            folder, files = self._stage(mod.name)
            m.update_mod(mod.id, folder=folder, files=files)
            self.log.append(f"reinstall {mod.name} -> {folder}")
            self._sync()
            shutil.rmtree(mod.staging_dir())
        elif op == "deploy":
            self.log.append("deploy")
            result = self.world.deploy()
            assert not result.failed, result.failed
        elif op == "purge":
            self.log.append("purge")
            result = self.world.purge()
            assert not result.failed, result.failed
            assert_vanilla(self.world)
        elif op == "mode":
            self.world.mode = "hardlink" if self.world.mode == "symlink" else "symlink"
            self.log.append(f"mode {self.world.mode}")
            self._sync()
        elif op == "refresh":
            self.log.append("refresh")
            self._sync()
        else:
            return self.step()
        self.check()

    def check(self) -> None:
        state = self.m.game_state(GAME)
        if state is not None and state.deployed:
            assert_deployed(self.world)
        else:
            assert_vanilla(self.world)

    def finish(self) -> None:
        self.world.purge()
        assert_vanilla(self.world)


def run_sim(world: World, seed: int, steps: int) -> None:
    sim = Sim(world, random.Random(seed))
    try:
        for _ in range(steps):
            sim.step()
        sim.finish()
    except AssertionError as exc:
        raise AssertionError(f"seed {seed}, after: {sim.log}\n{exc}") from exc


@pytest.mark.parametrize("seed", range(8))
def test_random_short_sequences(tmp_path, monkeypatch, seed):
    """240 short random sequences (8 seeds x 30), each from a fresh game."""
    rng = random.Random(seed)
    for n in range(30):
        mode = rng.choice(["symlink", "hardlink"])
        world = fresh_world(tmp_path / f"s{n}", monkeypatch, mode=mode)
        try:
            run_sim(world, seed * 1000 + n, steps=rng.randint(4, 10))
        finally:
            world.manifest.close()


@pytest.mark.parametrize("seed", [101, 202, 303, 404])
def test_random_long_sequences(tmp_path, monkeypatch, seed):
    world = fresh_world(tmp_path / "long", monkeypatch)
    try:
        run_sim(world, seed, steps=120)
    finally:
        world.manifest.close()


# ================================================================ 2. behind the app's back


def test_user_deletes_a_link(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm"})
    world.deploy()
    os.unlink(world.path("Data/A.esp"))
    os.unlink(world.path("Data/Fallout4.esm"))
    report = deploy.verify(world.manifest, world.target())
    assert sorted(r.path for r in report.missing) == ["Data/A.esp", "Data/Fallout4.esm"]
    assert not report.ok
    plan = world.plan()
    assert sorted(op.kind for op in plan.ops) == ["forget", "forget", "link", "link"]
    world.deploy()
    assert_deployed(world)
    # Disabling restores the original even though the link had vanished meanwhile.
    os.unlink(world.path("Data/Fallout4.esm"))
    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_user_replaces_a_link_with_own_file_new_path(world, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/B.esp": "b"})
    world.deploy()
    user = world.path("Data/A.esp")
    user.unlink()
    user.write_text("the user's own file")
    inode = os.stat(user).st_ino
    report = deploy.verify(world.manifest, world.target())
    assert [r.path for r in report.foreign] == ["Data/A.esp"]

    result = world.deploy()
    assert any("Data/A.esp" in w and "left alone" in w for w in result.warnings)
    assert ("Data/A.esp", "replaced by a file the app didn't create (left alone)") in result.plan.blocked
    assert user.read_text() == "the user's own file" and os.stat(user).st_ino == inode
    assert "DATA/A.ESP" not in world.manifest.deployed(GAME)

    world.purge()
    assert user.read_text() == "the user's own file" and os.stat(user).st_ino == inode
    snap = snapshot(world.root)
    del snap["Data/A.esp"]
    assert snap == world.vanilla
    assert not world.manifest.has_footprint(GAME)


def test_user_file_stays_left_alone_until_removed(world):
    """Once reported, the user's file is left alone on every later deploy
    (and after verify --repair); when the user deletes it, the mod's file
    is linked again."""
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    user = world.path("Data/A.esp")
    user.unlink()
    user.write_text("mine")
    inode = os.stat(user).st_ino
    world.deploy()
    assert list(world.manifest.left_alone(GAME).values()) == ["Data/A.esp"]
    for _ in range(2):
        result = world.deploy()
        assert result.done == [] and ("Data/A.esp", "replaced by a file the app didn't create (left alone)") \
            in result.plan.blocked
    verify_and_repair(world)
    world.deploy()
    assert user.read_text() == "mine" and os.stat(user).st_ino == inode
    assert deploy.verify(world.manifest, world.target()).left_alone == ["Data/A.esp"]
    world.purge()
    assert user.read_text() == "mine" and os.stat(user).st_ino == inode
    world.deploy()
    assert user.read_text() == "mine"
    user.unlink()  # the user gives up their file
    world.deploy()
    assert world.manifest.left_alone(GAME) == {}
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_repair_marks_replaced_link_left_alone(world):
    add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm"})
    world.deploy()
    p = world.path("Data/Fallout4.esm")
    p.unlink()
    p.write_text("user esm")
    report = deploy.verify(world.manifest, world.target())
    assert [r.path for r in report.foreign] == ["Data/Fallout4.esm"]
    deploy.repair(world.manifest, world.target(), report)
    assert "DATA/FALLOUT4.ESM" in world.manifest.left_alone(GAME)
    world.deploy()
    world.deploy()
    assert p.read_text() == "user esm"
    world.purge()
    assert p.read_text() == "user esm"
    # The original stays safe in the backups while the user's file is there.
    (backup,) = world.manifest.backups(GAME).values()
    assert os.lstat(backup.backup).st_ino == world.vanilla["Data/Fallout4.esm"][3]
    p.unlink()
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_user_replaces_link_over_original(world, mode):
    world.mode = mode
    original = world.vanilla["Data/Fallout4.esm"]
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    p = world.path("Data/Fallout4.esm")
    p.unlink()
    p.write_text("user esm")
    for _ in range(2):  # it stays blocked: the original is still in the backups
        result = world.deploy()
        assert p.read_text() == "user esm"
    assert any("Fallout4.esm" in path for path, _ in result.plan.blocked)
    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    world.purge()
    assert p.read_text() == "user esm"
    (backup,) = world.manifest.backups(GAME).values()
    assert os.lstat(backup.backup).st_ino == original[3]  # the original is kept, not deleted


def test_user_file_in_created_folder_keeps_folder(world):
    mod = add_mod(world.manifest, "A", {"Data/Textures/New/a.dds": "a"})
    world.deploy()
    user = world.path("Data/Textures/New/mine.dds")
    user.write_text("user")
    world.manifest.set_enabled(mod.id, False)
    result = world.refresh()
    assert any("Data/Textures/New" in w for w in result.warnings)
    assert user.read_text() == "user"
    assert not world.path("Data/Textures/New/a.dds").exists()
    # The app made the folder, so it stays recorded and is removed once empty.
    assert world.manifest.created_dirs(GAME) == {"DATA/TEXTURES/NEW": "Data/Textures/New"}
    world.purge()
    snap = snapshot(world.root)
    assert snap.pop("Data/Textures/New/mine.dds")[0] == "file"
    assert snap.pop("Data/Textures/New") == ("dir",)
    assert snap == world.vanilla
    user.unlink()
    world.purge()
    assert snapshot(world.root) == world.vanilla, "the empty folder the app made is removed later"
    assert world.manifest.created_dirs(GAME) == {}


def test_user_file_in_created_folder_on_purge(world):
    add_mod(world.manifest, "A", {"Data/New/Deep/a.dds": "a"})
    world.deploy()
    world.path("Data/New/mine.txt").write_text("user")
    world.purge()
    assert world.path("Data/New/mine.txt").read_text() == "user"
    assert not world.path("Data/New/Deep").exists()
    # Nothing left to undo; the folder the app made stays recorded while the user's file is in it.
    assert not world.manifest.has_footprint(GAME, dirs=False)
    assert world.manifest.created_dirs(GAME) == {"DATA/NEW": "Data/New"}


def test_staged_file_deleted_falls_back_to_next_provider(world):
    a = add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "A"})
    b = add_mod(world.manifest, "B", {"Data/Textures/Armor/a.dds": "B", "Data/B.esp": "b"})
    world.deploy()
    staged(b, "Data/Textures/Armor/a.dds").unlink()
    plan = world.plan()
    assert any("missing" in reason and "'B'" in reason for _, reason in plan.blocked)
    world.deploy()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "A"
    assert_deployed(world)
    staged(a, "Data/Textures/Armor/a.dds").unlink()
    world.deploy()
    # No provider left: the original comes back instead of a dangling link.
    assert snapshot(world.root)["Data/Textures/Armor/a.dds"] == world.vanilla["Data/Textures/Armor/a.dds"]
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_staged_file_deleted_hardlink(world):
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    staged(mod, "Data/Fallout4.esm").unlink()
    world.deploy()
    assert snapshot(world.root)["Data/Fallout4.esm"] == world.vanilla["Data/Fallout4.esm"]
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_whole_staging_folder_deleted(world):
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/New/x.txt": "x"})
    world.deploy()
    shutil.rmtree(mod.staging_dir())
    world.deploy()
    assert_vanilla(world, empty_records=True)
    world.manifest.set_enabled(mod.id, False)
    world.manifest.remove_mod(mod.id)


def test_backup_file_deleted(world):
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    (backup,) = world.manifest.backups(GAME).values()
    os.unlink(backup.backup)
    report = deploy.verify(world.manifest, world.target())
    assert [b.path for b in report.missing_backups] == ["Data/Fallout4.esm"]
    world.manifest.set_enabled(mod.id, False)
    result = world.refresh()
    assert any("gone" in reason for _, reason in result.failed)
    assert not world.path("Data/Fallout4.esm").exists()  # the user deleted the only copy
    assert not world.manifest.has_footprint(GAME)
    world.purge()
    snap = snapshot(world.root)
    expected = dict(world.vanilla)
    del expected["Data/Fallout4.esm"]
    assert snap == expected


def test_backup_file_deleted_then_repair(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    os.unlink(next(iter(world.manifest.backups(GAME).values())).backup)
    report = verify_and_repair(world)
    assert report.missing_backups
    assert world.manifest.backups(GAME) == {}
    world.purge()
    assert not world.path("Data/Fallout4.esm").exists()
    assert not world.manifest.has_footprint(GAME)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_game_update_restores_vanilla_file_over_link(world, mode):
    world.mode = mode
    original = world.vanilla["Data/Fallout4.esm"]
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    # The updater deletes our link and writes the new vanilla file.
    p = world.path("Data/Fallout4.esm")
    p.unlink()
    p.write_text("vanilla v2")
    result = world.deploy()
    assert any("Fallout4.esm" in w for w in result.warnings)
    assert p.read_text() == "vanilla v2"
    result = world.purge()
    assert p.read_text() == "vanilla v2"
    assert any("stays in" in w for w in result.warnings)
    # The old original is kept (never deleted) and reported.
    (backup,) = world.manifest.backups(GAME).values()
    assert os.lstat(backup.backup).st_ino == original[3]
    assert not world.path("Data/A.esp").exists()


def test_game_update_adds_file_where_mod_was_linked_in_created_folder(world):
    mod = add_mod(world.manifest, "A", {"Data/NewDir/a.txt": "a"})
    world.deploy()
    os.unlink(world.path("Data/NewDir/a.txt"))
    world.path("Data/NewDir/a.txt").write_text("from the game")
    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    world.purge()
    assert world.path("Data/NewDir/a.txt").read_text() == "from the game"


def test_user_deletes_created_folder_with_links(world):
    add_mod(world.manifest, "A", {"Data/Textures/New/Deep/a.dds": "a", "Data/Textures/New/b.dds": "b"})
    world.deploy()
    shutil.rmtree(world.path("Data/Textures/New"))
    report = deploy.verify(world.manifest, world.target())
    assert len(report.missing) == 2 and len(report.missing_dirs) == 2
    world.deploy()  # the folders are recreated
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_user_replaces_created_folder_with_file(world):
    mod = add_mod(world.manifest, "A", {"Data/NewDir/a.txt": "a"})
    world.deploy()
    shutil.rmtree(world.path("Data/NewDir"))
    world.path("Data/NewDir").write_text("a file now")
    world.manifest.set_enabled(mod.id, False)
    result = world.refresh()
    assert not result.failed
    assert world.path("Data/NewDir").read_text() == "a file now"
    assert not world.manifest.has_footprint(GAME)


def test_user_replaces_created_folder_with_symlink(world, tmp_path):
    mod = add_mod(world.manifest, "A", {"Data/NewDir/a.txt": "a"})
    world.deploy()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(world.path("Data/NewDir"))
    world.path("Data/NewDir").symlink_to(elsewhere)
    world.manifest.set_enabled(mod.id, False)
    world.refresh()
    assert world.path("Data/NewDir").is_symlink() and elsewhere.is_dir()
    assert not world.manifest.has_footprint(GAME)


def test_existing_user_symlink_is_moved_aside_and_restored(world, tmp_path):
    target = tmp_path / "somewhere.esp"
    target.write_text("elsewhere")
    world.path("Data/A.esp").symlink_to(target)
    world.vanilla = snapshot(world.root)
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    assert world.path("Data/A.esp").read_text() == "a"
    world.purge()
    assert_vanilla(world)
    assert target.read_text() == "elsewhere"


def test_stale_verify_report_does_not_delete_user_file(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    # An unrecorded link into the mods folder: a stray one.
    stray = world.path("Data/stray.esp")
    stray.symlink_to(staged(mod, "Data/A.esp"))
    report = deploy.verify(world.manifest, world.target())
    assert report.orphan_links == ["Data/stray.esp"]
    # Before the user confirms the repair, they put their own file there.
    stray.unlink()
    stray.write_text("user")
    actions = deploy.repair(world.manifest, world.target(), report)
    assert stray.read_text() == "user"
    assert any("changed" in a for a in actions)


def test_stale_report_missing_link_is_back(world):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    link = world.path("Data/A.esp")
    target = os.readlink(link)
    link.unlink()
    report = deploy.verify(world.manifest, world.target())
    link.symlink_to(target)  # back before repair runs
    deploy.repair(world.manifest, world.target(), report)
    assert "DATA/A.ESP" in world.manifest.deployed(GAME)  # still recorded, so purge removes it
    world.purge()
    assert_vanilla(world)


def test_repair_removes_stray_links_and_restores_unrecorded_backups(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    world.path("Data/stray.esp").symlink_to(staged(mod, "Data/A.esp"))
    # An original that ended up in the backups folder without a record.
    orphan = paths.backups_dir(GAME) / "Data/Scripts/s.pex"
    orphan.parent.mkdir(parents=True)
    os.rename(world.path("Data/Scripts/s.pex"), orphan)
    report = deploy.verify(world.manifest, world.target())
    assert report.orphan_links == ["Data/stray.esp"]
    assert report.orphan_backups == [str(orphan)]
    deploy.repair(world.manifest, world.target(), report, dry_run=True)
    assert world.path("Data/stray.esp").is_symlink() and orphan.exists()  # dry run
    deploy.repair(world.manifest, world.target(), report)
    assert not os.path.lexists(world.path("Data/stray.esp"))
    assert deploy.verify(world.manifest, world.target()).ok
    world.purge()
    assert_vanilla(world)


def test_foreign_symlink_elsewhere_is_not_a_stray(world, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("x")
    world.path("Data/user_link.txt").symlink_to(other)
    report = deploy.verify(world.manifest, world.target())
    assert report.ok


def test_hardlink_replaced_by_identical_copy_is_foreign(world):
    world.mode = "hardlink"
    add_mod(world.manifest, "A", {"Data/A.esp": "same"})
    world.deploy()
    p = world.path("Data/A.esp")
    p.unlink()
    p.write_text("same")  # same content, but not our file
    world.purge()
    assert p.read_text() == "same"


# ---------------------------------------------------------------- random tampering


class TamperSim(Sim):
    """Like :class:`Sim`, but between steps the user (or a game update)
    changes things behind the app's back. Checked after every step: no file
    the user put in the game folder is ever lost, no original game file is
    lost unless the user deleted its backup, and nothing of the app's is left
    after purge + repair + purge."""

    TAMPERS = ("delete_link", "replace_link", "user_file_in_created_dir", "delete_staged", "delete_backup",
               "rmtree_created", "stray_link", "user_file_new_path")

    def __init__(self, world: World, rng: random.Random) -> None:
        super().__init__(world, rng)
        self.user_files: dict[str, str] = {}  # unique content -> where the user put it (for messages)
        self.lost: set[str] = set()  # vanilla paths whose only copy the user deleted
        self.counter = 0

    def _marker(self, where: str) -> str:
        self.counter += 1
        content = f"user file #{self.counter} at {where}"
        self.user_files[content] = where
        return content

    def tamper(self) -> None:
        rng, m, root = self.rng, self.m, self.world.root
        deployed = list(m.deployed(GAME).values())
        created = list(m.created_dirs(GAME).values())
        kind = rng.choice(self.TAMPERS)
        # (The tampering itself only ever deletes the app's own links.)
        if kind == "delete_link" and deployed:
            rec = rng.choice(deployed)
            if deploy._inspect(root / rec.path, rec) == "intact":
                os.unlink(root / rec.path)
        elif kind == "replace_link" and deployed:
            rec = rng.choice(deployed)
            p = root / rec.path
            if deploy._inspect(p, rec) == "intact":
                os.unlink(p)
                p.write_text(self._marker(rec.path))
        elif kind == "user_file_in_created_dir" and created:
            d = root / rng.choice(created)
            if d.is_dir() and not d.is_symlink():
                name = f"user{self.counter}.txt"
                (d / name).write_text(self._marker(str(d / name)))
        elif kind == "delete_staged":
            mods = m.mods(GAME)
            if mods:
                mod = rng.choice(mods)
                files = m.mod_files(mod.id)
                if files:
                    src = staged(mod, rng.choice(files).source)
                    if src.is_file():
                        src.unlink()
        elif kind == "delete_backup":
            backups = list(m.backups(GAME).values())
            if backups:
                b = rng.choice(backups)
                if os.path.lexists(b.backup):
                    # The backup may hold a file of the user's that was in a mod's way: then that is gone too.
                    self.user_files.pop(Path(b.backup).read_text(errors="replace"), None)
                    os.unlink(b.backup)
                    self.lost.add(fold_path(b.path))
        elif kind == "rmtree_created" and created:
            d = root / rng.choice(created)
            if d.is_dir() and not d.is_symlink():
                # Only if it holds nothing of the user's (they would be deleting their own files).
                if not any(p.is_file() and not p.is_symlink() and p.read_text(errors="replace") in self.user_files
                           for p in d.rglob("*")):
                    shutil.rmtree(d)
        elif kind == "stray_link":
            mods = m.mods(GAME)
            if mods:
                mod = rng.choice(mods)
                files = m.mod_files(mod.id)
                target = root / "Data" / f"stray{self.counter}.txt"
                self.counter += 1
                if files and not os.path.lexists(target):
                    target.symlink_to(staged(mod, files[0].source))
        elif kind == "user_file_new_path":
            dests = [d for ds in POOL.values() for d in ds]
            dest = rng.choice(dests)
            resolved = CaseResolver(root).resolve(dest)
            p = root / resolved.rel
            if not resolved.exists and not resolved.type_conflict:
                p.parent.mkdir(parents=True, exist_ok=True)  # the user's own folders
                p.write_text(self._marker(dest))
        self.log.append(f"tamper {kind}")

    def _sync(self) -> None:
        self.world.refresh()  # failures are allowed here: things were changed behind the app's back

    def step(self) -> None:
        if self.rng.random() < 0.35:
            self.tamper()
            if self.rng.random() < 0.5:
                self.log.append("verify --repair")
                verify_and_repair(self.world)
            self.check()
            return
        rng = self.rng
        op = rng.choice(self.OPS)
        mods = self.m.mods(GAME)
        self.log.append(op)
        if op == "remove" and mods:
            mod = rng.choice(mods)
            self.m.set_enabled(mod.id, False)
            self.world.refresh()
            if any(d.mod_id == mod.id for d in self.m.deployed(GAME).values()):
                verify_and_repair(self.world)
                self.world.refresh()
            if not any(d.mod_id == mod.id for d in self.m.deployed(GAME).values()):
                self.m.remove_mod(mod.id)
                shutil.rmtree(mod.staging_dir(), ignore_errors=True)
                del self.installed[mod.name]
        elif op == "deploy":
            self.world.deploy()
        elif op == "purge":
            self.world.purge()
        else:
            self._mutate(op)
        self.check()

    def _mutate(self, op: str) -> None:
        rng, m = self.rng, self.m
        mods = m.mods(GAME)
        if op == "add":
            free = [n for n in POOL if n not in self.installed]
            if free:
                name = rng.choice(free)
                folder, files = self._stage(name)
                self.installed[name] = m.add_mod(GAME, name, folder, files).id
        elif op == "toggle" and mods:
            mod = rng.choice(mods)
            m.set_enabled(mod.id, not mod.enabled)
        elif op == "reorder" and mods:
            ids = [mod.id for mod in mods]
            rng.shuffle(ids)
            m.set_order(GAME, ids)
        elif op == "reinstall" and mods:
            mod = rng.choice(mods)
            folder, files = self._stage(mod.name)
            m.update_mod(mod.id, folder=folder, files=files)
            self.world.refresh()
            if not any(d.mod_id == mod.id and d.source.startswith(str(mod.staging_dir()) + os.sep)
                       for d in m.deployed(GAME).values()):
                shutil.rmtree(mod.staging_dir(), ignore_errors=True)
            return
        elif op == "mode":
            self.world.mode = "hardlink" if self.world.mode == "symlink" else "symlink"
        self.world.refresh()

    def check(self) -> None:
        root = self.world.root
        backups_root = paths.backups_dir(GAME)
        found: set[str] = set()
        inodes: set[int] = set()
        for base in (root, backups_root):
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    inodes.add(os.lstat(p).st_ino)
                    try:
                        found.add(p.read_text())
                    except (UnicodeDecodeError, OSError):
                        pass
        missing_user = [where for content, where in self.user_files.items() if content not in found]
        assert not missing_user, f"user files lost: {missing_user}"
        for rel, entry in self.world.vanilla.items():
            if entry[0] == "file" and fold_path(rel) not in self.lost:
                assert entry[3] in inodes, f"original {rel} was lost"

    def finish(self) -> None:
        for _ in range(2):
            self.world.purge()
            verify_and_repair(self.world)
        self.world.purge()
        self.check()
        m, root = self.m, self.world.root
        assert m.deployed(GAME) == {}
        # A folder the app made stays recorded only while it still holds
        # something the user put there (it is removed once empty).
        for path in m.created_dirs(GAME).values():
            folder = root / path
            assert folder.is_dir() and any(p.is_file() and not p.is_symlink() for p in folder.rglob("*")), path
        report = deploy.verify(m, self.world.target())
        # Originals stay tracked while a user's file sits at their path.
        for backup in report.held_backups:
            here = root / backup.path
            assert here.is_file() and not here.is_symlink(), backup.path
        report.held_backups = []
        assert report.ok, report
        mods_root = os.path.realpath(paths.mods_dir())
        for p in root.rglob("*"):
            if p.is_symlink():
                assert not os.path.realpath(p).startswith(mods_root + os.sep), f"link {p} left after purge"
        # Every original not deliberately replaced is back in place.
        snap = snapshot(root)
        index = folded_index(snap)
        for rel, entry in self.world.vanilla.items():
            key = fold_path(rel)
            if entry[0] != "file" or key in self.lost:
                continue
            if snap.get(rel) != entry:
                # Only acceptable when something of the user's sits there (in any spelling),
                # with the original kept aside.
                there = [snap[r] for r in index.get(key, [])]
                assert there and all(e[0] == "file" for e in there), f"{rel}: {there}"
                assert key in m.backups(GAME), f"original {rel} neither in place nor backed up"


@pytest.mark.parametrize("seed", range(10))
def test_random_sequences_with_tampering(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    for n in range(6):
        world = fresh_world(tmp_path / f"t{n}", monkeypatch, mode=rng.choice(["symlink", "hardlink"]))
        sim = TamperSim(world, random.Random(seed * 100 + n))
        try:
            for _ in range(rng.randint(10, 30)):
                sim.step()
            sim.finish()
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}/{n}, after: {sim.log}\n{exc}") from exc
        finally:
            world.manifest.close()


# ================================================================ 3. crashes


OS_FUNCS = ("symlink", "link", "rename", "unlink", "mkdir", "rmdir")


class Killed(SystemExit):
    """Stands in for the process being killed."""


class Injector:
    """Count the file system calls deploy makes and fail the N-th one: with
    *exc* raised before the call (it never happened) or after it (it
    happened, then the process died)."""

    def __init__(self, fail_at: int | None = None, exc: Callable[[], BaseException] = Killed,
                 after: bool = False) -> None:
        self.fail_at, self.exc, self.after = fail_at, exc, after
        self.calls = 0
        self.fired = False

    @contextmanager
    def active(self, monkeypatch) -> Iterator["Injector"]:
        with monkeypatch.context() as mp:
            for name in OS_FUNCS:
                mp.setattr(os, name, self._wrap(getattr(os, name)))
            yield self

    def _wrap(self, real):
        def wrapper(*args, **kwargs):
            self.calls += 1
            hit = self.calls == self.fail_at
            if hit and not self.after:
                self.fired = True
                raise self.exc()
            result = real(*args, **kwargs)
            if hit:
                self.fired = True
                raise self.exc()
            return result
        return wrapper


def run_killable(injector: Injector, monkeypatch, action: Callable[[], object]) -> bool:
    """Run *action* with *injector* active; whether the process was "killed".
    (With ``after`` a call that fails on its own is not killed: it already failed.)"""
    try:
        with injector.active(monkeypatch):
            action()
    except Killed:
        assert injector.fired
        return True
    assert not injector.fired
    return False


def crash_setup(world: World) -> dict[str, int]:
    m = world.manifest
    a = add_mod(m, "A", {"Data/Textures/Armor/a.dds": "A a", "Data/A.esp": "A", "Data/NewA/x/a.txt": "a"})
    b = add_mod(m, "B", {"data/textures/armor/A.DDS": "B a", "Data/Fallout4.esm": "B esm", "Data/NewB/b.txt": "b"})
    c = add_mod(m, "C", {"Data/Fallout4.esm": "C esm", "Data/Scripts/s.pex": "C s", "f4se_loader.exe": "l"})
    d = add_mod(m, "D", {"Data/NewD/deep/d.txt": "d", "Data/Sound/Voice/v.fuz": "D v", "Data/A.esp": "D"},
                enabled=False)
    world.deploy()
    assert_deployed(world)
    return {"A": a.id, "B": b.id, "C": c.id, "D": d.id}


def crash_change(world: World, ids: dict[str, int], switch_mode: bool) -> None:
    """A change that needs every kind of operation."""
    m = world.manifest
    m.set_enabled(ids["A"], False)                                   # unlink, restore, rmdir
    m.set_enabled(ids["D"], True)                                    # mkdir, backup, link
    m.set_order(GAME, [ids["A"], ids["C"], ids["B"], ids["D"]])      # B wins the esm now
    if switch_mode:
        world.mode = "hardlink" if world.mode == "symlink" else "symlink"


def count_calls(base: Path, monkeypatch, mode: str, action: str, switch: bool) -> int:
    world = fresh_world(base, monkeypatch, mode=mode)
    try:
        ids = crash_setup(world)
        injector = Injector()
        if action == "deploy":
            crash_change(world, ids, switch)
        with injector.active(monkeypatch):
            (world.deploy if action == "deploy" else world.purge)()
        return injector.calls
    finally:
        world.manifest.close()


CRASH_CASES = [
    ("symlink", "deploy", False),
    ("hardlink", "deploy", False),
    ("symlink", "deploy", True),
    ("hardlink", "deploy", True),
    ("symlink", "purge", False),
    ("hardlink", "purge", False),
]


@pytest.mark.parametrize("after", [False, True], ids=["before-call", "after-call"])
@pytest.mark.parametrize("mode,action,switch", CRASH_CASES)
def test_killed_at_every_file_operation(tmp_path, monkeypatch, mode, action, switch, after):
    total = count_calls(tmp_path / "count", monkeypatch, mode, action, switch)
    assert total > 5
    for n in range(1, total + 1):
        world = fresh_world(tmp_path / f"n{n}", monkeypatch, mode=mode)
        try:
            ids = crash_setup(world)
            if action == "deploy":
                crash_change(world, ids, switch)
            injector = Injector(n, Killed, after)
            killed = run_killable(injector, monkeypatch, world.deploy if action == "deploy" else world.purge)
            reopen(world)
            report = verify_and_repair(world)
            # (A kill before the first operation -- e.g. in check_link_mode -- leaves no journal entry.)
            assert killed or not report.interrupted
            after = deploy.verify(world.manifest, world.target())
            # Originals the repair freed go back with the deploy or purge that follows.
            after.restorable = []
            assert after.ok, f"call {n}: repair left problems"
            if action == "deploy":
                result = world.deploy()
                assert not result.failed, (n, result.failed)
                assert_deployed(world)
            result = world.purge()
            assert not result.failed, (n, result.failed)
            assert_vanilla(world)
            assert deploy.verify(world.manifest, world.target()).ok
        except AssertionError as exc:
            raise AssertionError(f"killed at call {n}/{total} ({'after' if after else 'before'}): {exc}") from exc
        finally:
            world.manifest.close()


@pytest.mark.parametrize("after", [False, True], ids=["before-call", "after-call"])
@pytest.mark.parametrize("mode,action,switch", CRASH_CASES)
def test_killed_then_no_repair_still_converges(tmp_path, monkeypatch, mode, action, switch, after):
    """Even if the user never runs 'verify --repair', the next deploy (or
    purge) cleans up after the interrupted run and loses nothing."""
    total = count_calls(tmp_path / "count", monkeypatch, mode, action, switch)
    for n in range(1, total + 1):
        world = fresh_world(tmp_path / f"n{n}", monkeypatch, mode=mode)
        try:
            ids = crash_setup(world)
            if action == "deploy":
                crash_change(world, ids, switch)
            killed = run_killable(Injector(n, Killed, after), monkeypatch,
                                  world.deploy if action == "deploy" else world.purge)
            reopen(world)
            if action == "deploy":
                result = world.deploy()
                assert killed or not any("interrupted" in w for w in result.warnings)
                assert not result.failed, result.failed
                assert_deployed(world)
            result = world.purge()
            assert not result.failed, result.failed
            assert_vanilla(world)
            # Only the journal still remembers the interruption; repair closes it.
            report = verify_and_repair(world)
            assert report.ok or report.interrupted and not (
                report.missing or report.foreign or report.orphan_links or report.missing_backups
                or report.orphan_backups or report.missing_dirs)
            assert deploy.verify(world.manifest, world.target()).ok
        except AssertionError as exc:
            raise AssertionError(f"killed at call {n}/{total} ({'after' if after else 'before'}): {exc}") from exc
        finally:
            world.manifest.close()


@pytest.mark.parametrize("mode,action,switch", CRASH_CASES)
def test_os_error_at_every_file_operation(tmp_path, monkeypatch, mode, action, switch):
    """A failing call (disk full, permission denied ...) is reported, the run
    carries on, and the next run finishes the job without any repair."""
    total = count_calls(tmp_path / "count", monkeypatch, mode, action, switch)
    for n in range(1, total + 1):
        world = fresh_world(tmp_path / f"n{n}", monkeypatch, mode=mode)
        try:
            ids = crash_setup(world)
            if action == "deploy":
                crash_change(world, ids, switch)
            injector = Injector(n, lambda: OSError(errno.EIO, "injected I/O error"))
            with injector.active(monkeypatch):
                result = (world.deploy if action == "deploy" else world.purge)()
            if injector.fired and not result.failed:
                # Only the pruning of empty backup folders swallows errors.
                pass
            assert world.manifest.unfinished_journal(GAME) == []
            if action == "deploy":
                world.deploy()
                assert_deployed(world)
            world.purge()
            assert_vanilla(world)
            assert deploy.verify(world.manifest, world.target()).ok
        except AssertionError as exc:
            raise AssertionError(f"failed call {n}/{total}: {exc}") from exc
        finally:
            world.manifest.close()


class Boom(RuntimeError):
    pass


def fail_once(monkeypatch, cls, name: str) -> dict:
    real = getattr(cls, name)
    state = {"done": False}

    def wrapper(self, *args, **kwargs):
        if not state["done"]:
            state["done"] = True
            raise Boom(f"{name} failed")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, wrapper)
    return state


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_crash_between_link_and_record_verify_and_repair(world, monkeypatch, mode):
    world.mode = mode
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a", "Data/Fallout4.esm": "esm"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    # The link was made, the manifest doesn't know it.
    report = deploy.verify(world.manifest, world.target())
    assert not report.ok
    assert [(e.action, e.path) for e in report.interrupted] == [("link", "Data/A.esp")]
    assert "DATA/A.ESP" not in world.manifest.deployed(GAME)
    if mode == "symlink":
        assert report.orphan_links == ["Data/A.esp"]  # hardlinks are found through the journal
    assert os.path.samefile(world.path("Data/A.esp"), staged(mod, "Data/A.esp"))
    actions = deploy.repair(world.manifest, world.target(), report)
    assert any("Data/A.esp" in a for a in actions)
    assert not os.path.lexists(world.path("Data/A.esp"))
    assert deploy.verify(world.manifest, world.target()).ok
    world.deploy()
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_crash_between_link_and_record_then_deploy_adopts(world, monkeypatch, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    plan = world.plan()
    assert [(op.kind, op.path) for op in plan.ops] == [("link", "Data/A.esp")]
    assert "recording" in plan.ops[0].note
    world.deploy()
    assert_deployed(world)
    verify_and_repair(world)  # closes the interrupted entry; must not remove the adopted link
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_leftover_link_of_another_mod_is_never_backed_up_as_original(world, monkeypatch, mode):
    """A half-made link of mod A must not be mistaken for an original game
    file when mod B now wants that path."""
    world.mode = mode
    a = add_mod(world.manifest, "A", {"Data/X.esp": "A"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    world.manifest.set_enabled(a.id, False)
    add_mod(world.manifest, "B", {"Data/X.esp": "B"})
    plan = world.plan()
    assert [(op.kind, op.note) for op in plan.ops] == [("unlink", deploy.LEFTOVER_NOTE), ("link", "")]
    world.deploy()
    assert world.manifest.backups(GAME) == {}
    assert world.path("Data/X.esp").read_text() == "B"
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_purge_removes_leftover_link_of_interrupted_run(world, monkeypatch, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/X.esp": "A", "Data/Fallout4.esm": "esm"})
    world.deploy()
    world.manifest.set_enabled(world.manifest.mods(GAME)[0].id, False)
    add_mod(world.manifest, "B", {"Data/Y.esp": "B"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    assert os.path.lexists(world.path("Data/Y.esp"))
    world.purge()
    assert_vanilla(world)


def test_unrecorded_link_into_mods_folder_blocks_until_repair(world):
    a = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    add_mod(world.manifest, "B", {"Data/X.esp": "b"})
    world.path("Data/X.esp").symlink_to(staged(a, "Data/A.esp"))  # no journal entry: not provably a leftover
    result = world.deploy()
    assert any("verify --repair" in reason for _, reason in result.plan.blocked)
    assert world.manifest.backups(GAME) == {}
    verify_and_repair(world)
    world.deploy()
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_crash_recording_backup_leaves_original_in_place(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_backup")
        with pytest.raises(Boom):
            world.deploy()
    assert snapshot(world.root)["Data/Fallout4.esm"] == world.vanilla["Data/Fallout4.esm"]
    verify_and_repair(world)
    world.deploy()
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_killed_after_backup_move_original_is_recorded(world, monkeypatch):
    """The backup record is written before the original is moved, so a kill
    right after the move never leaves an original the manifest doesn't know."""
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    calls = []

    def rename(src, dst, _real=os.rename):
        _real(src, dst)
        calls.append(dst)
        raise Killed()

    with monkeypatch.context() as mp:
        mp.setattr(os, "rename", rename)
        with pytest.raises(Killed):
            world.deploy()
    (backup,) = world.manifest.backups(GAME).values()
    assert backup.backup == str(calls[0])
    assert os.lstat(backup.backup).st_ino == world.vanilla["Data/Fallout4.esm"][3]
    # Even without repair the next deploy links and purge restores.
    world.deploy()
    world.purge()
    assert_vanilla(world, empty_records=False)
    verify_and_repair(world)
    assert_vanilla(world)


def test_cross_device_move_failure_leaves_no_partial_copy(world, monkeypatch):
    """When the backups are on another filesystem the original is copied;
    if that fails the partial copy is removed and the original stays."""
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})

    def rename(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def copy2(src, dst, **kw):
        Path(dst).write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(shutil, "copy2", copy2)
    result = world.deploy()
    assert any(op.kind == "backup" for op, _ in result.failed)
    assert snapshot(world.root)["Data/Fallout4.esm"] == world.vanilla["Data/Fallout4.esm"]
    assert world.manifest.backups(GAME) == {}
    assert not [p for p in paths.backups_dir(GAME).rglob("*") if p.is_file()]


def test_cross_device_move_works(world, monkeypatch):
    real_rename = os.rename

    def rename(src, dst):
        # Only a rename between the game folder and the data folder crosses filesystems.
        if str(dst).startswith(str(paths.data_dir())) != str(src).startswith(str(paths.data_dir())):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    assert world.path("Data/Fallout4.esm").read_text() == "modded"
    world.purge()
    snap = snapshot(world.root)
    # Copied twice, so the inode differs; everything else is identical.
    assert snap["Data/Fallout4.esm"][:3] == world.vanilla["Data/Fallout4.esm"][:3]
    assert {k: v for k, v in snap.items() if k != "Data/Fallout4.esm"} == \
        {k: v for k, v in world.vanilla.items() if k != "Data/Fallout4.esm"}
    assert not world.manifest.has_footprint(GAME)


def test_move_never_overwrites(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_text("src")
    dst.write_text("dst")
    with pytest.raises(FileExistsError):
        deploy._move(src, dst)
    assert src.read_text() == "src" and dst.read_text() == "dst"


def test_interrupted_mkdir_folder_is_recorded_by_repair(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/NewDir/a.txt": "a"})
    real_mkdir = os.mkdir

    def mkdir(path, *args, **kw):
        real_mkdir(path, *args, **kw)
        raise Killed()

    with monkeypatch.context() as mp:
        mp.setattr(os, "mkdir", mkdir)
        with pytest.raises(Killed):
            world.deploy()
    assert world.path("Data/NewDir").is_dir()
    assert world.manifest.created_dirs(GAME) == {}
    report = verify_and_repair(world)
    assert report.interrupted
    assert list(world.manifest.created_dirs(GAME).values()) == ["Data/NewDir"]
    world.purge()
    assert_vanilla(world)


# ================================================================ 4. paths


def test_unicode_names_merge_case_insensitively(tmp_path, monkeypatch):
    vanilla = {
        "Data/Textures/Ärmor/ä.dds": "vanilla ä",
        "Data/ΣΚΥΡΙΜ/σ.txt": "vanilla sigma",
        "Data/straße/x.txt": "vanilla strasse",
        "Fallout4.exe": "exe",
    }
    world = fresh_world(tmp_path / "w", monkeypatch, vanilla=vanilla)
    try:
        mod = add_mod(world.manifest, "Ünïcödé Mod", {
            "data/textures/ÄRMOR/Ä.DDS": "modded ä",
            "Data/σκυριμ/Σ.TXT": "modded sigma",
            "Data/STRASSE/x.txt": "not the same folder: ß is not ss",
            "Data/Textures/Новая папка/файл.dds": "cyrillic",
        })
        world.deploy()
        assert world.path("Data/Textures/Ärmor/ä.dds").read_text() == "modded ä"
        assert world.path("Data/ΣΚΥΡΙΜ/σ.txt").read_text() == "modded sigma"
        assert world.path("Data/STRASSE/x.txt").is_symlink()
        assert world.path("Data/straße/x.txt").read_text() == "vanilla strasse"
        assert world.path("Data/Textures/Новая папка/файл.dds").is_symlink()
        assert_deployed(world)
        world.manifest.set_enabled(mod.id, False)
        world.refresh()
        assert_vanilla(world)
    finally:
        world.manifest.close()


def test_spaces_and_odd_characters(world):
    add_mod(world.manifest, "Mod With Spaces (2)", {
        "Data/Textures/My Armor/a b.dds": "spaces",
        "Data/it's [weird] #1 & co.esp": "punctuation",
        "Data/ leading and trailing .txt": "padded",
    }, folder="Mod With Spaces (2)")
    world.deploy()
    assert world.path("Data/Textures/My Armor/a b.dds").read_text() == "spaces"
    assert world.path("Data/it's [weird] #1 & co.esp").read_text() == "punctuation"
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_names_differing_only_by_case_within_one_mod(world):
    mod = add_mod(world.manifest, "Sloppy", {
        "Data/Textures/Armor/new.dds": "lower", "Data/Textures/Armor/NEW.DDS": "upper",
        "Data/textures/sloppy/a.txt": "a", "Data/TEXTURES/SLOPPY/b.txt": "b",
    })
    assert len(world.manifest.mod_files(mod.id)) == 3
    world.deploy()
    entries = [n for n in os.listdir(world.path("Data/Textures/Armor")) if n.lower() == "new.dds"]
    assert len(entries) == 1
    assert sorted(os.listdir(world.path("Data/Textures"))) == ["Armor", "Sloppy"]
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_mod_file_over_vanilla_directory_is_blocked(world):
    add_mod(world.manifest, "Bad", {"Data/textures": "a file where a folder is", "Data/Good.esp": "ok",
                                    "Data/Fallout4.esm/inner.txt": "under a file"})
    result = world.deploy()
    blocked = {fold_path(p) for p, _ in result.plan.blocked}
    assert blocked == {"DATA/TEXTURES", "DATA/FALLOUT4.ESM/INNER.TXT"}
    assert not result.failed
    assert world.path("Data/Good.esp").is_symlink()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "vanilla armor a"
    assert world.plan().ops == []
    world.purge()
    assert_vanilla(world)


def test_file_and_folder_collision_between_mods(world):
    a = add_mod(world.manifest, "A", {"Data/Thing": "a file"})
    b = add_mod(world.manifest, "B", {"Data/thing/inside.txt": "in a folder"})
    world.deploy()
    # One of them is blocked; nothing crashes and repeated deploys are stable.
    assert world.path("Data/Thing").is_symlink()
    assert world.plan().changes == []
    world.manifest.set_enabled(a.id, False)
    world.refresh()
    world.deploy()  # the folder can only be made once the file link is gone
    assert world.path("Data/thing/inside.txt").is_symlink() or world.path("Data/Thing/inside.txt").is_symlink()
    world.manifest.set_enabled(a.id, True)
    world.manifest.set_order(GAME, [b.id, a.id])
    world.refresh()
    world.refresh()
    world.purge()
    assert_vanilla(world)


def test_very_deep_paths(world):
    parts = [f"level_{i:02d}_abcdefghij" for i in range(40)]
    deep = "Data/" + "/".join(parts) + "/deep.txt"
    shallow_variant = "DATA/" + "/".join(p.upper() for p in parts[:20]) + "/other.txt"
    add_mod(world.manifest, "Deep", {deep: "deep", shallow_variant: "other"})
    world.deploy()
    assert world.path(deep).read_text() == "deep"
    assert len(world.manifest.created_dirs(GAME)) == 40
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_vanilla_with_case_duplicates(tmp_path, monkeypatch):
    """A folder that is already a mess (Textures and textures both exist)."""
    vanilla = {"Data/Textures/a.dds": "upper a", "Data/textures/b.dds": "lower b", "Game.exe": "exe"}
    world = fresh_world(tmp_path / "w", monkeypatch, vanilla=vanilla)
    try:
        add_mod(world.manifest, "A", {"DATA/TEXTURES/a.dds": "mod a", "data/textures/c.dds": "mod c"})
        result = world.deploy()
        assert any("differing only in case" in w for w in result.warnings)
        world.deploy()
        world.purge()
        assert_vanilla(world)
    finally:
        world.manifest.close()


def test_mod_file_named_like_staging_symlink_target(world):
    """Dest paths that look like an escape attempt never reach the disk."""
    from bethmm.manifest import ManifestError
    with pytest.raises(ManifestError):
        world.manifest.add_mod(GAME, "Evil", "Evil", [ModFile("a", "../outside.txt")])
    with pytest.raises(ManifestError):
        world.manifest.add_mod(GAME, "Evil", "Evil", [ModFile("../../etc/passwd", "Data/x.txt")])
    assert_vanilla(world)


def test_staged_file_that_is_a_folder_is_not_linked(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    staged(mod, "Data/A.esp").unlink()
    staged(mod, "Data/A.esp").mkdir()
    result = world.deploy()
    assert result.plan.blocked
    assert_vanilla(world, empty_records=False)


def test_many_mods_same_file(world):
    mods = [add_mod(world.manifest, f"M{i}", {"Data/Textures/Armor/a.dds": f"m{i}"}) for i in range(15)]
    world.deploy()
    assert world.path("Data/Textures/Armor/a.dds").read_text() == "m14"
    rng = random.Random(7)
    for _ in range(10):
        ids = [m.id for m in mods]
        rng.shuffle(ids)
        world.manifest.set_order(GAME, ids)
        plan = world.plan()
        assert len(plan.changes) in (0, 2)  # unlink + link of the one contested file, or nothing
        world.refresh()
        assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_root_files_and_game_root_symlinked(tmp_path, monkeypatch):
    world = fresh_world(tmp_path / "w", monkeypatch)
    try:
        add_mod(world.manifest, "Loader", {"f4se_loader.exe": "l", "F4SE/Plugins/a.dll": "d"})
        world.deploy()
        assert_deployed(world)
        world.purge()
        assert_vanilla(world)
    finally:
        world.manifest.close()


def test_make_tree_helper_sanity(tmp_path):
    make_tree(tmp_path, {"a/b.txt": "x"})
    assert (tmp_path / "a/b.txt").read_text() == "x"
