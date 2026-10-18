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
from helpers import make_tree
from test_deploy import GAME, VANILLA, add_mod, assert_deployed, assert_vanilla, snapshot, staged
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
    assert any("interrupted move" in a for a in actions)
    assert not leftover.exists() and not in_game.exists()
    # Moved aside, never deleted: it may be the only copy of a file.
    kept = sorted(p.name for p in (paths.backups_dir(GAME) / deploy.SUPERSEDED_DIR).rglob("*") if p.is_file())
    assert kept == sorted([leftover.name, in_game.name])
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
    # It may only be on a drive that isn't mounted: nothing is forgotten unless asked.
    with pytest.raises(deploy.GameFolderGone, match="forget-missing"):
        world.purge()
    assert world.manifest.has_footprint(GAME) and Path(backup.backup).exists()
    dry = world.purge(dry_run=True, forget_missing=True)
    assert world.manifest.has_footprint(GAME) and Path(backup.backup).exists()
    assert any("would keep" in n for n in dry.forgotten)
    result = world.purge(forget_missing=True)
    assert any("forgot" in n for n in result.forgotten)
    assert not world.manifest.has_footprint(GAME)
    kept = list((paths.backups_dir(GAME) / deploy.SUPERSEDED_DIR).rglob("Fallout4.esm"))
    assert len(kept) == 1 and kept[0].read_text() == "vanilla esm"  # nothing deleted
    assert not world.manifest.game_state(GAME).deployed
    world.purge()  # nothing left to do, no error
    world.purge(forget_missing=True)


def test_purge_after_game_folder_vanished_and_target_moved_too(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    shutil.rmtree(world.root)
    with pytest.raises(deploy.GameFolderGone):
        deploy.purge(world.manifest, GameTarget(GAME, tmp_path / "elsewhere"))
    result = deploy.purge(world.manifest, GameTarget(GAME, tmp_path / "elsewhere"), forget_missing=True)
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


# ================================================================ second review round


def _unmount(world) -> Path:
    """Simulate the game's drive being unmounted: the folder disappears."""
    away = world.root.with_name("unmounted")
    os.rename(world.root, away)
    return away


def _remount(world, away: Path) -> None:
    os.rename(away, world.root)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_purge_with_the_drive_unmounted_changes_nothing(world, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    records = (world.manifest.deployed(GAME), world.manifest.backups(GAME), world.manifest.created_dirs(GAME))
    away = _unmount(world)
    with pytest.raises(deploy.GameFolderGone, match="mounted"):
        world.purge()
    with pytest.raises(deploy.DeployError):
        world.deploy()
    assert (world.manifest.deployed(GAME), world.manifest.backups(GAME),
            world.manifest.created_dirs(GAME)) == records
    _remount(world, away)
    world.purge()
    assert_vanilla(world)


def test_empty_mount_point_is_treated_as_missing(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    away = _unmount(world)
    world.root.mkdir()  # the empty mount point
    with pytest.raises(deploy.GameFolderGone):
        world.purge()
    with pytest.raises(deploy.DeployError, match="empty"):
        world.deploy()
    assert os.listdir(world.root) == []  # nothing restored into it
    report = deploy.verify(world.manifest, world.target())
    assert report.root_missing and not report.ok
    world.root.rmdir()
    _remount(world, away)
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_verify_and_repair_with_the_drive_unmounted_change_nothing(world, mode):
    world.mode = mode
    a = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/NewMod/x.dds": "x"})
    world.deploy()
    records = (world.manifest.deployed(GAME), world.manifest.backups(GAME), world.manifest.created_dirs(GAME))
    away = _unmount(world)
    report = deploy.verify(world.manifest, world.target())
    assert report.root_missing and not report.missing and not report.missing_dirs
    actions = deploy.repair(world.manifest, world.target(), report)
    assert len(actions) == 1 and "change nothing" in actions[0]
    assert (world.manifest.deployed(GAME), world.manifest.backups(GAME),
            world.manifest.created_dirs(GAME)) == records
    _remount(world, away)
    world.manifest.set_enabled(a.id, False)
    world.deploy()
    world.purge()
    assert_vanilla(world)


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_verify_after_the_game_moved_checks_the_new_folder(world, mode):
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    moved = world.root.with_name("Fallout 4 moved")
    os.rename(world.root, moved)
    report = deploy.verify(world.manifest, world.target(root=moved))
    assert report.moved_from == world.root and report.root == moved
    assert not report.missing and report.ok
    assert deploy.repair(world.manifest, world.target(root=moved), report) == []
    assert len(world.manifest.deployed(GAME)) == 2  # live links stay recorded
    deploy.purge(world.manifest, world.target(root=moved))
    assert snapshot(moved) == world.vanilla
    assert not world.manifest.has_footprint(GAME)


def test_half_made_hardlink_at_an_original_is_not_a_held_original(world, monkeypatch):
    world.mode = "hardlink"
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    report = deploy.verify(world.manifest, world.target())
    assert not report.held_backups
    deploy.repair(world.manifest, world.target(), report, release_held=True)
    assert world.manifest.backups(GAME)  # the original stays tracked
    world.deploy()
    world.purge()
    assert_vanilla(world)


def test_case_renamed_folder_with_hardlink_is_not_a_held_original(world):
    world.mode = "hardlink"
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a"})
    world.deploy()
    os.rename(world.path("Data/Textures"), world.path("Data/textures"))
    report = deploy.verify(world.manifest, world.target())
    assert not report.held_backups and not report.missing
    assert report.case_moved == [("Data/Textures/Armor/a.dds", "Data/textures/Armor/a.dds")]
    deploy.repair(world.manifest, world.target(), report, release_held=True)
    world.deploy()
    world.purge()
    assert world.path("Data/textures/Armor/a.dds").read_text() == "vanilla armor a"
    assert not world.manifest.has_footprint(GAME)


def test_cross_device_move_makes_the_new_name_durable_before_deleting(world, monkeypatch):
    events: list[str] = []
    real_fsync_dir, real_unlink = deploy._fsync_dir, os.unlink

    def fsync_dir(path):
        events.append(f"fsync {path}")
        real_fsync_dir(path)

    def unlink(path, *a, **kw):
        events.append(f"unlink {path}")
        return real_unlink(path, *a, **kw)

    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    exdev_between_game_and_data(monkeypatch)
    monkeypatch.setattr(deploy, "_fsync_dir", fsync_dir)
    monkeypatch.setattr(os, "unlink", unlink)
    world.deploy()
    events.clear()
    world.purge()
    original = str(world.path("Data/Fallout4.esm"))
    fsync_at = events.index(f"fsync {world.path('Data')}")
    backup_unlink = next(i for i, e in enumerate(events) if e.startswith("unlink") and "backups" in e)
    assert fsync_at < backup_unlink, events
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm" and os.path.isfile(original)


def test_switching_to_a_new_install_after_purge_leaves_records_behind(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/F4SE/Plugins/p.dll": "dll", "Data/Fallout4.esm": "modded"})
    world.deploy()
    world.path("Data/F4SE/Plugins/p.log").write_text("written by the plugin")  # keeps the folder
    os.unlink(world.path("Data/Fallout4.esm"))
    world.path("Data/Fallout4.esm").write_text("updated esm")  # holds the original
    new = tmp_path / "New Install"
    make_tree(new, {"Fallout4.exe": "exe", "Data/Fallout4.esm": "new vanilla"})
    target = world.target(root=new)
    with pytest.raises(deploy.DeployError, match="purge"):
        deploy.deploy(world.manifest, target)
    deploy.purge(world.manifest, target)  # cleans up the old folder
    assert world.path("Data/F4SE/Plugins/p.log").exists()
    result = deploy.deploy(world.manifest, target)  # no longer refused
    assert not result.failed
    assert any("switching" in w for w in result.warnings)
    assert (new / "Data/F4SE/Plugins/p.dll").read_text() == "dll"
    assert (new / "Data/Fallout4.esm").read_text() == "modded"
    (backup,) = world.manifest.backups(GAME).values()
    assert Path(backup.backup).read_text() == "new vanilla"
    kept = list((paths.backups_dir(GAME) / deploy.SUPERSEDED_DIR).rglob("Fallout4.esm"))
    assert [k.read_text() for k in kept] == ["vanilla esm"]
    deploy.purge(world.manifest, target)
    assert (new / "Data/Fallout4.esm").read_text() == "new vanilla"
    assert not os.path.lexists(new / "Data/F4SE")
    assert world.path("Data/F4SE/Plugins/p.log").exists()  # the old folder is never touched


def test_temp_symlink_leftovers_are_found_and_moved_aside(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    game_tmp = world.path(f"Data/{deploy.TEMP_PREFIX}aaa")
    game_tmp.symlink_to("/somewhere")
    backup_tmp = paths.backups_dir(GAME) / f"{deploy.TEMP_PREFIX}bbb"
    backup_tmp.symlink_to("/elsewhere")
    report = deploy.verify(world.manifest, world.target())
    assert sorted(report.temp_leftovers) == sorted([str(game_tmp), str(backup_tmp)])
    deploy.repair(world.manifest, world.target(), report)
    assert not os.path.lexists(game_tmp) and not os.path.lexists(backup_tmp)
    world.purge()
    assert_vanilla(world)


def test_empty_user_folder_in_another_case_is_not_removed(world):
    add_mod(world.manifest, "A", {"Data/NewMod/x.dds": "x"})
    world.deploy()
    os.unlink(world.path("Data/NewMod/x.dds"))
    os.rmdir(world.path("Data/NewMod"))
    world.path("Data/newmod").mkdir()  # the user's own, empty
    world.purge()
    assert world.path("Data/newmod").is_dir()
    assert not world.manifest.has_footprint(GAME)


def test_interrupted_first_link_counts_as_footprint_until_purged(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    assert world.manifest.has_footprint(GAME)
    result = world.purge()
    assert not result.failed
    assert_vanilla(world)  # including: no unfinished journal entries left


def test_restorable_original_is_reported(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    os.unlink(world.path("Data/Fallout4.esm"))
    world.path("Data/Fallout4.esm").write_text("updated esm")
    world.purge()  # the original is held
    os.unlink(world.path("Data/Fallout4.esm"))
    report = deploy.verify(world.manifest, world.target())
    assert [b.path for b in report.restorable] == ["Data/Fallout4.esm"] and not report.ok
    world.purge()
    assert_vanilla(world)


# ================================================================ third review round


def _second_install(tmp_path) -> Path:
    other = tmp_path / "Fallout 4 copy"
    make_tree(other, dict(VANILLA))
    return other


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_unmounted_drive_with_a_second_install_is_not_a_move(world, tmp_path, mode):
    """The game is found in another folder while the drive with the modded one
    isn't mounted: that other copy is not where the mods are."""
    world.mode = mode
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    other = _second_install(tmp_path)
    other_snapshot = snapshot(other)
    records = (world.manifest.deployed(GAME), world.manifest.backups(GAME))
    away = _unmount(world)
    target = world.target(root=other)
    with pytest.raises(deploy.GameFolderGone, match="not in"):
        deploy.purge(world.manifest, target)
    report = deploy.verify(world.manifest, target)
    assert report.root_missing and report.moved_from is None
    assert "change nothing" in deploy.repair(world.manifest, target, report)[0]
    with pytest.raises(deploy.DeployError, match="forget-missing"):
        deploy.deploy(world.manifest, target)
    assert (world.manifest.deployed(GAME), world.manifest.backups(GAME)) == records
    assert snapshot(other) == other_snapshot
    _remount(world, away)
    world.purge()
    assert_vanilla(world)


def test_old_folder_left_as_empty_folders_after_a_move(world):
    """Some ways of moving leave the old folder tree behind, empty."""
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    moved = world.root.with_name("Fallout 4 moved")
    shutil.copytree(world.root, moved, symlinks=True)
    for dirpath, _dirs, files in os.walk(world.root):
        for name in files:
            os.unlink(os.path.join(dirpath, name))
    result = deploy.purge(world.manifest, world.target(root=moved))
    assert not result.failed and any("moved" in w for w in result.warnings)
    assert contents(moved) == {k: v[:3] for k, v in world.vanilla.items()}  # (copied: new inodes)
    assert not [f for _d, _s, f in os.walk(world.root) if f]  # nothing restored into the empty tree


def test_data_folder_on_a_drive_that_is_not_mounted(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    elsewhere = tmp_path / "second drive" / "Data"
    elsewhere.parent.mkdir()
    os.rename(world.path("Data"), elsewhere)
    os.symlink(elsewhere, world.path("Data"))
    os.rename(elsewhere.parent, tmp_path / "unmounted")
    with pytest.raises(deploy.GameFolderGone):
        world.purge()
    assert world.manifest.deployed(GAME)
    os.rename(tmp_path / "unmounted", elsewhere.parent)
    world.purge()
    assert (world.path("Data/Fallout4.esm")).read_text() == "vanilla esm"


def test_hardlink_whose_staged_file_was_replaced_is_not_a_held_original(world):
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    staged_file = staged(mod, "Data/Fallout4.esm")
    replacement = staged_file.with_name("new")
    replacement.write_text("modded v2")
    os.replace(replacement, staged_file)
    report = deploy.verify(world.manifest, world.target())
    assert not report.held_backups
    deploy.repair(world.manifest, world.target(), report, release_held=True)
    assert world.manifest.backups(GAME)
    world.purge()
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"


def test_old_install_deleted_new_install_elsewhere(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    shutil.rmtree(world.root)
    new = _second_install(tmp_path)
    target = world.target(root=new)
    with pytest.raises(deploy.DeployError, match="forget-missing"):
        deploy.deploy(world.manifest, target)
    with pytest.raises(deploy.GameFolderGone):
        deploy.purge(world.manifest, target)
    result = deploy.purge(world.manifest, target, forget_missing=True)
    assert result.forgotten and snapshot(new) == snapshot(new)
    assert world.manifest.left_alone(GAME) == {}
    deploy.deploy(world.manifest, target)
    assert (new / "Data/Fallout4.esm").read_text() == "modded"
    deploy.purge(world.manifest, target)
    assert (new / "Data/Fallout4.esm").read_text() == "vanilla esm"


def test_left_alone_records_of_the_old_folder_dont_apply_to_a_new_one(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a"})
    world.deploy()
    os.unlink(world.path("Data/Textures/Armor/a.dds"))
    world.path("Data/Textures/Armor/a.dds").write_text("from a game update")
    world.purge()
    deploy.repair(world.manifest, world.target(), deploy.verify(world.manifest, world.target()), release_held=True)
    assert world.manifest.left_alone(GAME) and not world.manifest.has_footprint(GAME)
    new = _second_install(tmp_path)
    result = deploy.deploy(world.manifest, world.target(root=new))
    assert not result.plan.blocked
    assert (new / "Data/Textures/Armor/a.dds").read_text() == "mod a"
    assert world.manifest.left_alone(GAME) == {}


def test_never_deploys_into_an_empty_folder(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    empty = tmp_path / "mount point"
    empty.mkdir()
    with pytest.raises(deploy.DeployError, match="empty"):
        deploy.deploy(world.manifest, world.target(root=empty))
    assert os.listdir(empty) == []


def test_user_folder_in_another_case_survives_a_deploy_then_purge(world):
    add_mod(world.manifest, "A", {"Data/NewMod/x.dds": "x"})
    world.deploy()
    shutil.rmtree(world.path("Data/NewMod"))
    world.path("Data/newmod").mkdir()  # the user's own
    world.deploy()  # links into the user's folder
    assert world.path("Data/newmod/x.dds").read_text() == "x"
    world.purge()
    assert world.path("Data/newmod").is_dir()
    assert not world.manifest.has_footprint(GAME)


def test_interrupted_link_in_a_folder_renamed_in_another_case(world, monkeypatch):
    world.mode = "hardlink"
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    os.rename(world.path("Data/Textures"), world.path("Data/textures"))
    world.purge()
    assert world.path("Data/textures/Armor/a.dds").read_text() == "vanilla armor a"
    assert not world.manifest.has_footprint(GAME)


def test_unprovable_link_of_an_interrupted_run_is_left_alone(world, monkeypatch):
    """The app can't prove a hardlink is its own once the staged file is gone:
    the file is left alone (never deleted) and the original stays tracked."""
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    staged(mod, "Data/Fallout4.esm").unlink()
    world.purge()
    assert world.path("Data/Fallout4.esm").read_text() == "modded"
    (backup,) = world.manifest.backups(GAME).values()
    assert Path(backup.backup).read_text() == "vanilla esm"


def test_killed_between_backup_and_link_purge_closes_the_entry(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    real = os.symlink

    def killed(src, dst, *a, **kw):
        if str(dst).endswith("Fallout4.esm"):
            raise Killed()
        return real(src, dst, *a, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(os, "symlink", killed)
        with pytest.raises(Killed):
            world.deploy()
    world.purge()
    assert_vanilla(world)  # also: no unfinished journal entry left


# ================================================================ fourth review round


def test_data_folder_is_an_empty_mount_point(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/NewMod/x.dds": "x"})
    world.deploy()
    away = world.root.with_name("data drive")
    os.rename(world.path("Data"), away)
    world.path("Data").mkdir()  # the drive isn't mounted: an empty folder
    records = (world.manifest.deployed(GAME), world.manifest.backups(GAME), world.manifest.created_dirs(GAME))
    with pytest.raises(deploy.GameFolderGone, match="Data folder"):
        world.purge()
    with pytest.raises(deploy.DeployError, match="Data folder"):
        world.deploy()
    report = deploy.verify(world.manifest, world.target())
    assert report.root_missing and "Data folder" in report.missing_reason
    deploy.repair(world.manifest, world.target(), report)
    assert (world.manifest.deployed(GAME), world.manifest.backups(GAME),
            world.manifest.created_dirs(GAME)) == records
    assert os.listdir(world.path("Data")) == []
    world.path("Data").rmdir()
    os.rename(away, world.path("Data"))
    world.purge()
    assert_vanilla(world)


def test_folder_that_does_not_look_like_the_game_is_refused(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    mount = tmp_path / "mount point"
    make_tree(mount, {".directory": "x"})
    with pytest.raises(deploy.DeployError, match="doesn't look like"):
        deploy.deploy(world.manifest, world.target(root=mount))
    assert sorted(os.listdir(mount)) == [".directory"]


def test_hardlink_in_a_renamed_folder_with_a_replaced_staged_file(world):
    world.mode = "hardlink"
    mod = add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a"})
    world.deploy()
    os.rename(world.path("Data/Textures"), world.path("Data/textures"))
    staged_file = staged(mod, "Data/Textures/Armor/a.dds")
    replacement = staged_file.with_name("new")
    replacement.write_text("mod a v2")
    os.replace(replacement, staged_file)
    report = deploy.verify(world.manifest, world.target())
    assert not report.missing and not report.held_backups
    deploy.repair(world.manifest, world.target(), report, release_held=True)
    world.purge()
    assert world.path("Data/textures/Armor/a.dds").read_text() == "vanilla armor a"
    assert not world.manifest.has_footprint(GAME)


def test_unrecorded_link_is_adopted_with_a_warning(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    away = _unmount(world)
    world.purge(forget_missing=True)
    _remount(world, away)
    result = world.deploy()
    note = next(w for w in result.warnings if "no record" in w)
    assert ".superseded" in note  # says where the original is


def test_moved_game_with_a_file_left_in_the_old_folder(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded", "Data/A.esp": "a"})
    world.deploy()
    moved = world.root.with_name("Fallout 4 moved")
    shutil.copytree(world.root, moved, symlinks=True)
    for dirpath, _dirs, files in os.walk(world.root):
        for name in files:
            os.unlink(os.path.join(dirpath, name))
    world.path("ReShade.ini").write_text("left behind")
    target = world.target(root=moved)
    report = deploy.verify(world.manifest, target)
    assert report.moved_from == world.root and not report.missing
    assert deploy.repair(world.manifest, target, report) == []
    deploy.purge(world.manifest, target)
    assert contents(moved) == {k: v[:3] for k, v in world.vanilla.items()}


def test_only_folders_with_user_files_left_and_the_game_moved(world):
    add_mod(world.manifest, "A", {"Data/MCM/Config/A/config.json": "{}"})
    world.deploy()
    world.path("Data/MCM/Settings").mkdir()
    world.path("Data/MCM/Settings/A.ini").write_text("user settings")
    world.purge()
    assert world.manifest.created_dirs(GAME)  # kept: it holds the user's file
    moved = world.root.with_name("Fallout 4 moved")
    os.rename(world.root, moved)
    result = deploy.purge(world.manifest, world.target(root=moved))
    assert any("nothing to undo" in w for w in result.warnings)
    report = deploy.verify(world.manifest, world.target(root=moved))
    assert report.ok and report.notes
    deploy.deploy(world.manifest, world.target(root=moved))  # switches to the new folder


def test_links_of_the_app_in_another_copy_are_reported(world, tmp_path):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    world.deploy()
    copy = tmp_path / "copy"
    shutil.copytree(world.root, copy, symlinks=True)
    report = deploy.verify(world.manifest, world.target(root=copy))
    assert report.links_elsewhere == ["Data/A.esp"] and not report.ok
    deploy.repair(world.manifest, world.target(root=copy), report)
    assert os.path.islink(copy / "Data/A.esp")  # never touched


def test_forget_missing_with_only_an_interrupted_run_says_so(world, monkeypatch):
    add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    with monkeypatch.context() as mp:
        fail_once(mp, Manifest, "record_link")
        with pytest.raises(Boom):
            world.deploy()
    shutil.rmtree(world.root)
    result = world.purge(forget_missing=True)
    assert any("interrupted" in n for n in result.forgotten)
    assert not world.manifest.has_footprint(GAME)


# ================================================================ fifth review round


def test_data_mount_point_with_a_stray_file(world):
    add_mod(world.manifest, "A", {"Data/Textures/Armor/a.dds": "mod a", "Data/A.esp": "a"})
    world.deploy()
    away = world.root.with_name("data drive")
    os.rename(world.path("Data"), away)
    make_tree(world.path("Data"), {".directory": "x"})
    with pytest.raises(deploy.GameFolderGone, match="Fallout4.esm"):
        world.purge()
    assert deploy.verify(world.manifest, world.target()).root_missing
    shutil.rmtree(world.path("Data"))
    os.rename(away, world.path("Data"))
    world.purge()
    assert_vanilla(world)


def test_empty_duplicate_data_folder_doesnt_block(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    world.path("data").mkdir()  # an empty one next to the real Data (case-sensitive filesystem)
    world.vanilla = {**world.vanilla, "data": ("dir",)}
    world.purge()
    assert_vanilla(world)


def test_repair_gives_back_an_original_kept_in_superseded(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    away = _unmount(world)
    world.purge(forget_missing=True)
    _remount(world, away)
    report = deploy.verify(world.manifest, world.target())
    assert report.orphan_links == ["Data/Fallout4.esm"]
    actions = deploy.repair(world.manifest, world.target(), report)
    assert any("put back the original" in a for a in actions)
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"


def test_adopted_link_gets_its_original_back_on_purge(world):
    add_mod(world.manifest, "A", {"Data/Fallout4.esm": "modded"})
    world.deploy()
    away = _unmount(world)
    world.purge(forget_missing=True)
    _remount(world, away)
    result = world.deploy()
    assert any("tracked again" in w for w in result.warnings)
    world.purge()
    assert world.path("Data/Fallout4.esm").read_text() == "vanilla esm"
    assert not world.manifest.has_footprint(GAME)


def test_copy_of_a_mod_file_is_backed_up_with_a_warning(world):
    mod = add_mod(world.manifest, "A", {"Data/A.esp": "a"})
    shutil.copy2(staged(mod, "Data/A.esp"), world.path("Data/A.esp"))  # what a copy-based move leaves
    result = world.deploy()
    assert any("copy of A's file" in w for w in result.warnings)
    world.purge()
    assert world.path("Data/A.esp").read_text() == "a"  # kept: nothing is deleted
