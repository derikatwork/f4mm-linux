"""Regression tests for problems found in review of the deploy engine: each
test reproduces the reported scenario and checks the game folder ends up
where it should."""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

import pytest

from bethmm import deploy, paths
from bethmm.deploy import GameTarget
from bethmm.manifest import Manifest

import test_deploy_adversarial as adv
from test_deploy import GAME, add_mod, assert_deployed, assert_vanilla, snapshot, staged
from test_deploy_adversarial import Boom, Killed, fail_once, verify_and_repair

# The adversarial suite's fixtures: a fast manifest and a fake game folder.
_fast_sqlite, manifest, world = adv._fast_sqlite, adv.manifest, adv.world


def contents(root: Path) -> dict[str, tuple]:
    """Like snapshot() but without inodes: files copied across filesystems get new ones."""
    return {k: v[:3] for k, v in snapshot(root).items()}


def exdev_between_game_and_data(monkeypatch, after_temp_rename=None):
    """Make renames between the game folder and the app's data folder fail
    with EXDEV, as when they are on different filesystems. *after_temp_rename*
    runs right after a temp copy is renamed into place."""
    real_rename = os.rename

    def rename(src, dst):
        if str(dst).startswith(str(paths.data_dir())) != str(src).startswith(str(paths.data_dir())):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst)
        if after_temp_rename and os.path.basename(src).startswith(deploy.TEMP_PREFIX):
            after_temp_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)


# ---------------------------------------------------------------- H1: leftovers of an interrupted run


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_killed_between_link_and_record_one_purge_restores(world, monkeypatch, mode):
    """The original was backed up and the link made, then the app died before
    recording the link: one purge removes the link and restores the original."""
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/Zz.esp": "z"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")  # the esm's link: the first one made
        with pytest.raises(Boom):
            world.deploy()
    assert world.manifest.backups(GAME)  # the original is in the backups
    result = world.purge()
    assert not result.failed, result.failed
    assert_vanilla(world, empty_records=False)
    verify_and_repair(world)  # closes the interrupted journal entry
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_killed_between_link_and_record_then_disable_restores(world, monkeypatch, mode):
    world.mode = mode
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    world.manifest.set_enabled(mod.id, False)
    result = world.deploy()
    assert not result.failed, result.failed
    assert snapshot(world.root) == world.vanilla
    assert world.manifest.backups(GAME) == {}


# ---------------------------------------------------------------- H2: moves across filesystems


def test_killed_during_cross_device_copy_keeps_original(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    exdev_between_game_and_data(monkeypatch)

    def copy2(src, dst, **kw):
        Path(dst).write_bytes(b"half")
        raise Killed()

    with monkeypatch.context() as mp:
        mp.setattr(shutil, "copy2", copy2)
        with pytest.raises(Killed):
            world.deploy()
    # The original never moved; at most a temp file is left (removed on the way out here).
    assert snapshot(world.root)["Data/Fallout4.esm"] == world.vanilla["Data/Fallout4.esm"]
    world.deploy()
    assert world.path("Data/Fallout4.esm").read_text() == "modded"
    world.purge()
    assert contents(world.root) == {k: v[:3] for k, v in world.vanilla.items()}
    verify_and_repair(world)
    assert deploy.verify(world.manifest, world.target()).ok


def test_temp_leftover_is_reported_and_removed(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    leftover = paths.backups_dir(GAME) / "Data" / f"{deploy.TEMP_PREFIX}abc123"
    leftover.write_bytes(b"half")
    in_game = world.path(f"Data/{deploy.TEMP_PREFIX}def456")
    in_game.write_bytes(b"half")
    report = deploy.verify(world.manifest, world.target())
    assert not report.ok
    assert sorted(report.temp_leftovers) == sorted([str(leftover), str(in_game)])
    assert not report.orphan_backups  # not mistaken for an unrecorded original
    actions = deploy.repair(world.manifest, world.target(), report)
    assert any("half-copied" in a for a in actions)
    assert not leftover.exists() and not in_game.exists()
    assert deploy.verify(world.manifest, world.target()).ok
    world.purge()
    assert_vanilla(world)


def test_killed_after_cross_device_backup_copy_before_delete(world, monkeypatch):
    """The original was copied into the backups but the app died before
    deleting it from the game folder: two identical copies. The next deploy
    drops the spare copy and carries on."""
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})

    def die(src, dst):
        raise Killed()

    with monkeypatch.context() as mp:
        exdev_between_game_and_data(mp, die)
        with pytest.raises(Killed):
            world.deploy()
    (backup,) = world.manifest.backups(GAME).values()
    assert Path(backup.backup).read_text() == "vanilla esm"
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"
    plan = world.plan()
    assert [op.kind for op in plan.ops] == ["dropbackup", "backup", "link"]
    result = world.deploy()
    assert not result.failed, result.failed
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_killed_after_cross_device_backup_copy_then_purge(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})

    def die(src, dst):
        raise Killed()

    with monkeypatch.context() as mp:
        exdev_between_game_and_data(mp, die)
        with pytest.raises(Killed):
            world.deploy()
    result = world.purge()
    assert not result.failed, result.failed
    assert_vanilla(world, empty_records=False)  # the original never left
    verify_and_repair(world)
    assert_vanilla(world)


def test_killed_after_cross_device_restore_copy_before_delete(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        exdev_between_game_and_data(mp)
        world.deploy()

    def die(src, dst):
        raise Killed()

    with monkeypatch.context() as mp:
        exdev_between_game_and_data(mp, die)
        with pytest.raises(Killed):
            world.purge()
    # The original is back in the game folder and still in the backups.
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"
    (backup,) = world.manifest.backups(GAME).values()
    assert Path(backup.backup).read_text() == "vanilla esm"
    result = world.purge()
    assert not result.failed, result.failed
    assert [op.kind for op in result.done] == ["dropbackup"]
    verify_and_repair(world)
    assert contents(world.root) == {k: v[:3] for k, v in world.vanilla.items()}
    assert not world.manifest.has_footprint(GAME)
    assert not [p for p in paths.backups_dir(GAME).rglob("*") if p.is_file()]


def test_spare_backup_is_kept_when_the_game_file_differs(world):
    """A backup is only dropped as a spare when it is byte-for-byte the file
    in the game folder."""
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    os.unlink(world.path("Data/Fallout4.esm"))
    world.path("Data/Fallout4.esm").write_text("patched by a game update")
    (backup,) = world.manifest.backups(GAME).values()
    result = world.purge()
    assert any("Fallout4.esm" in w for w in result.warnings)
    assert Path(backup.backup).read_text() == "vanilla esm"
    assert world.path("Data/Fallout4.esm").read_text() == "patched by a game update"


# ---------------------------------------------------------------- M1: a failed link keeps the original


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_link_failure_puts_the_original_back(world, monkeypatch, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    name = "symlink" if mode == "symlink" else "link"
    real = getattr(os, name)

    def refuse(src, dst, *a, **kw):
        if str(dst).endswith("Fallout4.esm"):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(src, dst, *a, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(os, name, refuse)
        result = world.deploy()
    assert [op.path for op, _ in result.failed] == ["Data/Fallout4.esm"]
    assert snapshot(world.root)["Data/Fallout4.esm"] == world.vanilla["Data/Fallout4.esm"]  # same inode
    assert world.manifest.backups(GAME) == {}
    assert world.path("Data/A.esp").read_text() == "a"
    world.deploy()
    assert_deployed(world)
    world.purge()
    assert_vanilla(world)


def test_missing_staged_file_keeps_the_original(world):
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    staged(mod, "Data/Fallout4.esm").unlink()
    result = world.deploy()
    assert any("reinstall" in reason for _, reason in result.plan.blocked)
    assert snapshot(world.root) == world.vanilla
    assert world.manifest.backups(GAME) == {}


# ---------------------------------------------------------------- M6/L3: case changes behind the app's back


def test_folder_renamed_in_another_case_purge_still_cleans_up(world):
    add_mod(world.manifest, "A", {"Data/NewMod/x.dds": "x", "Data/NewMod/sub/y.dds": "y"})
    world.deploy()
    os.rename(world.path("Data/NewMod"), world.path("Data/newmod"))
    result = world.purge()
    assert not result.failed, result.failed
    assert_vanilla(world)


def test_folder_renamed_in_another_case_deploy_keeps_working(world):
    add_mod(world.manifest, "A", {"Data/NewMod/x.dds": "x"})
    world.deploy()
    os.rename(world.path("Data/NewMod"), world.path("Data/newmod"))
    result = world.deploy()
    assert not result.failed, result.failed
    assert world.path("Data/newmod/x.dds").read_text() == "x"
    world.purge()
    assert_vanilla(world)


def test_link_replaced_by_user_file_in_another_case_is_left_alone(world):
    add_mod(world.manifest, "A", {"Data/X.esp": "mod"})
    world.deploy()
    os.unlink(world.path("Data/X.esp"))
    world.path("Data/x.esp").write_text("mine")
    result = world.deploy()
    assert not result.failed, result.failed
    assert world.path("Data/x.esp").read_text() == "mine"
    assert not os.path.lexists(world.path("Data/X.esp"))
    assert world.manifest.backups(GAME) == {}
    world.deploy()  # stable: still left alone
    assert world.path("Data/x.esp").read_text() == "mine"
    assert world.manifest.backups(GAME) == {}
    world.purge()
    assert world.path("Data/x.esp").read_text() == "mine"
    assert world.manifest.deployed(GAME) == {} and world.manifest.backups(GAME) == {}


# ---------------------------------------------------------------- M7/L2: where an original goes back


def test_restore_uses_the_exact_file_name(world):
    """A user file differing only in case is a different file on Linux: it
    doesn't block the original going back."""
    world.path("Data/Textures/dup.dds").write_text("vanilla dup")
    world.vanilla = snapshot(world.root)
    add_mod(world.manifest, "A", {"Data/Textures/dup.dds": "mod dup"})
    world.deploy()
    world.path("Data/Textures/DUP.dds").write_text("user's")
    result = world.purge()
    assert not result.failed, result.failed
    assert world.path("Data/Textures/dup.dds").read_text() == "vanilla dup"
    assert world.path("Data/Textures/DUP.dds").read_text() == "user's"
    assert world.manifest.backups(GAME) == {}


def test_restore_goes_into_the_folder_as_spelled_now(world):
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a"})
    world.deploy()
    os.rename(world.path("Data/Textures"), world.path("Data/textures"))
    result = world.purge()
    assert not result.failed, result.failed
    assert world.path("Data/textures/Armor/a.dds").read_text() == "vanilla armor a"
    assert not os.path.lexists(world.path("Data/Textures"))  # no second folder in another case
    assert not world.manifest.has_footprint(GAME)


def test_restore_logs_the_folders_it_makes(world, caplog):
    add_mod(world.manifest, "A", {"Data/Scripts/s.pex": "mod s"})
    world.deploy()
    os.unlink(world.path("Data/Scripts/s.pex"))
    os.rmdir(world.path("Data/Scripts"))
    with caplog.at_level(logging.INFO, logger="bethmm"):
        world.purge()
    assert world.path("Data/Scripts/s.pex").read_text() == "vanilla script"
    assert any(r.getMessage().startswith("MKDIR") and "Scripts" in r.getMessage() for r in caplog.records)
    assert any(r.getMessage().startswith("RESTORE") for r in caplog.records)


# ---------------------------------------------------------------- L4: game state recorded first


def test_game_state_recorded_before_the_first_change(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    state = world.manifest.game_state(GAME)
    assert state is not None and Path(state.root) == world.root
    # So moving the game afterwards is noticed.
    moved = world.root.with_name("Fallout 4 moved")
    os.rename(world.root, moved)
    with pytest.raises(deploy.DeployError, match="purge"):
        deploy.deploy(world.manifest, world.target(root=moved))
    deploy.purge(world.manifest, world.target(root=moved))
    assert snapshot(moved) == world.vanilla


# ---------------------------------------------------------------- moved or vanished game folder


def test_purge_after_game_folder_moved(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/NewMod/x.dds": "x"})
    world.deploy()
    moved = world.root.with_name("Fallout 4 moved")
    os.rename(world.root, moved)
    with pytest.raises(deploy.DeployError, match="purge"):
        deploy.deploy(world.manifest, world.target(root=moved))
    result = deploy.purge(world.manifest, world.target(root=moved))
    assert not result.failed, result.failed
    assert any("moved" in w for w in result.warnings)
    assert snapshot(moved) == world.vanilla
    assert not world.manifest.has_footprint(GAME)
    world.root = moved
    world.deploy()
    assert_deployed(world)


def test_purge_after_game_folder_moved_with_old_folder_back(world):
    """When the old folder exists again (e.g. a reinstall there), purge cleans up the old folder."""
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    other = world.root.with_name("Other")
    other.mkdir()
    result = deploy.purge(world.manifest, world.target(root=other))
    assert not result.failed
    assert_vanilla(world)


def test_purge_after_game_folder_vanished(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    (backup,) = world.manifest.backups(GAME).values()
    shutil.rmtree(world.root)
    dry = world.purge(dry_run=True)
    assert world.manifest.has_footprint(GAME) and Path(backup.backup).exists()
    assert any("would keep" in w for w in dry.warnings)
    result = world.purge()
    assert any("no longer exist" in w or "does not exist" in w for w in result.warnings)
    assert not world.manifest.has_footprint(GAME)
    kept = list((paths.backups_dir(GAME) / deploy.SUPERSEDED_DIR).rglob("Fallout4.esm"))
    assert len(kept) == 1 and kept[0].read_text() == "vanilla esm"  # nothing deleted
    assert not world.manifest.game_state(GAME).deployed
    world.purge()  # nothing left to do, no error


def test_purge_after_game_folder_vanished_and_target_moved_too(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    shutil.rmtree(world.root)
    result = deploy.purge(world.manifest, GameTarget(GAME, tmp_path / "elsewhere"))
    assert not result.failed
    assert not world.manifest.has_footprint(GAME)


# ---------------------------------------------------------------- held originals


def _game_update_rewrites_esm(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    os.unlink(world.path("Data/Fallout4.esm"))
    world.path("Data/Fallout4.esm").write_text("updated esm")
    (backup,) = world.manifest.backups(GAME).values()
    return backup


def test_held_backup_is_kept_unless_released(world):
    backup = _game_update_rewrites_esm(world)
    report = deploy.verify(world.manifest, world.target())
    assert [b.path for b in report.held_backups] == ["Data/Fallout4.esm"]
    actions = deploy.repair(world.manifest, world.target(), report)
    assert any("--release-held" in a for a in actions)
    assert Path(backup.backup).read_text() == "vanilla esm"
    assert world.manifest.backups(GAME)
    # Deleting the newer file and purging brings the original back.
    os.unlink(world.path("Data/Fallout4.esm"))
    world.purge()
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"
    assert world.manifest.backups(GAME) == {}


def test_held_backup_released(world):
    backup = _game_update_rewrites_esm(world)
    report = deploy.verify(world.manifest, world.target())
    dry = deploy.repair(world.manifest, world.target(), report, dry_run=True, release_held=True)
    assert any("stop tracking" in a for a in dry) and Path(backup.backup).exists()
    deploy.repair(world.manifest, world.target(), report, release_held=True)
    assert world.manifest.backups(GAME) == {}
    assert world.path("Data/Fallout4.esm").read_text() == "updated esm"
    kept = list((paths.backups_dir(GAME) / deploy.SUPERSEDED_DIR).rglob("Fallout4.esm"))
    assert len(kept) == 1 and kept[0].read_text() == "vanilla esm"
    assert deploy.verify(world.manifest, world.target()).ok


def test_held_backup_identical_to_game_file_is_a_spare(world):
    backup = _game_update_rewrites_esm(world)
    world.path("Data/Fallout4.esm").write_text("vanilla esm")
    report = deploy.verify(world.manifest, world.target())
    actions = deploy.repair(world.manifest, world.target(), report)
    assert any("spare copy" in a for a in actions)
    assert not Path(backup.backup).exists()
    assert world.manifest.backups(GAME) == {}


# ---------------------------------------------------------------- logging and dry run


def test_every_file_operation_is_logged(world, caplog):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/NewMod/x.dds": "x"})
    with caplog.at_level(logging.INFO, logger="bethmm"):
        world.deploy()
        world.purge()
    verbs = {r.getMessage().split()[0] for r in caplog.records}
    assert {"LINK", "UNLINK", "BACKUP", "RESTORE", "MKDIR", "RMDIR"} <= verbs


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_dry_run_reports_what_the_real_run_does(world, mode):
    world.mode = mode
    a = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "A", "Data/NewMod/x.dds": "x"})
    add_mod(world.manifest, "B", {"Data/fallout4.ESM": "B", "f4se_loader.exe": "l"})
    for change in ("deploy", "disable", "purge"):
        if change == "disable":
            world.manifest.set_enabled(a.id, False)
        run = world.purge if change == "purge" else world.deploy
        before = snapshot(world.root)
        records = (world.manifest.deployed(GAME), world.manifest.backups(GAME), world.manifest.created_dirs(GAME))
        dry = run(dry_run=True)
        assert snapshot(world.root) == before
        assert (world.manifest.deployed(GAME), world.manifest.backups(GAME),
                world.manifest.created_dirs(GAME)) == records
        real = run()
        assert not real.failed
        assert [(op.kind, op.path) for op in dry.done] == [(op.kind, op.path) for op in real.done], change
    assert_vanilla(world)
