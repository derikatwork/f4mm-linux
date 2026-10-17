"""Deploy mods into a game folder by linking staged files, and undo it.

The game folder only ever changes through :func:`apply_plan`, which carries
out a list of :class:`Op` computed by :func:`plan_sync`:

* ``unlink``  remove a link the app created (only if it is still exactly that
  link: same symlink target, or for hardlinks the same inode), or a link an
  interrupted run made before it could record it (note ``LEFTOVER_NOTE``);
* ``forget``  drop a manifest record without touching the disk (the link is
  already gone, or something else has replaced it -- that file is left alone,
  and stays left alone (``Manifest.left_alone``) until it is gone);
* ``restore`` move a backed-up original game file back into place;
* ``mkdir``   create a missing folder (recorded so it can be removed later), or
  record one an interrupted run made (note ``ADOPT_DIR_NOTE``);
* ``backup``  move an original game file aside to make room for a link;
* ``link``    create a symlink (or hardlink) to a staged mod file;
* ``rmdir``   remove a folder the app created, once it is empty.

Rules that keep the game folder safe:

* links are made per file, never per folder, so several mods share folders;
* every path is resolved case-insensitively against what exists
  (``pathresolve.CaseResolver``), so a mod's ``Textures/`` lands in an
  existing ``textures/``;
* the app never deletes or edits a file it did not create; an original game
  file that a mod replaces is moved to ``backups/<game>/`` first and moved back
  when no enabled mod provides that path any more;
* when two mods provide the same file the one with the higher priority (lower
  in the list) wins; if its staged file is missing the next one (or the
  original) is used and the missing file reported;
* a backup is recorded before the original is moved, so an original is never
  in the backups folder without a record; a folder the app made is only ever
  removed when empty, and stays recorded while it holds anything of the app's;
* each operation is journalled as an intent before it runs and marked done
  after its manifest record is saved, so an interrupted run can be cleaned up
  by :func:`repair`; planning also cleans up after one by itself (a link that
  is exactly what is wanted is recorded, other unrecorded links from the run
  are removed, folders it made are recorded);
* ``dry_run`` computes and returns the same plan without touching anything.

Every file operation is also written to the log.
"""

from __future__ import annotations

import errno
import filecmp
import logging
import os
import secrets
import shutil
import stat
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from bethmm import paths
from bethmm.manifest import (
    BackupRecord,
    DeployedFile,
    JournalEntry,
    Manifest,
    ModFile,
    ModRecord,
)
from bethmm.pathresolve import CaseResolver, fold_path

log = logging.getLogger(__name__)

LINK_MODES = ("symlink", "hardlink")
OP_KINDS = ("unlink", "forget", "restore", "dropbackup", "mkdir", "backup", "link", "rmdir")
#: Name prefix of the temporary file a move across filesystems copies into.
TEMP_PREFIX = ".bethmm-move-"
#: Folder under ``backups/<game>/`` where originals the app no longer needs
#: (superseded by a game update, or of a game folder that is gone) are kept.
SUPERSEDED_DIR = ".superseded"
#: Note of a ``mkdir`` op that records a folder an interrupted run made.
ADOPT_DIR_NOTE = "made by an interrupted run; recording it"
#: Note of an ``unlink`` op that removes an unrecorded link an interrupted run made.
LEFTOVER_NOTE = "left by an interrupted run"
#: Notes of ``forget`` ops: a link replaced by the user's own file (which is
#: then left alone), and such a file that is gone again.
FOREIGN_NOTE = "replaced by a file the app didn't create; left alone"
LEFT_ALONE_GONE_NOTE = "the file the app left alone is gone"


class DeployError(Exception):
    pass


class GameFolderGone(DeployError):
    """Neither the folder the mods were deployed into nor the current game folder exists."""


@dataclass(frozen=True)
class GameTarget:
    """What deployment needs to know about a game."""

    game_id: str
    root: Path
    link_mode: str = "symlink"

    def __post_init__(self) -> None:
        if self.link_mode not in LINK_MODES:
            raise ValueError(f"link mode must be one of {LINK_MODES}, not {self.link_mode!r}")


@dataclass(frozen=True)
class Op:
    kind: str
    #: Relative to the game root, spelled as on disk.
    path: str
    mod_id: int | None = None
    #: link: the staged file; backup: where the original goes; restore: the backup file.
    source: str | None = None
    note: str = ""


@dataclass
class SyncPlan:
    game_id: str
    root: Path
    link_mode: str
    ops: list[Op] = field(default_factory=list)
    #: Folded destination -> enabled mods providing it, lowest priority first
    #: (the last one wins). Only destinations with more than one provider.
    conflicts: dict[str, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: (path, reason) for files that can't be deployed.
    blocked: list[tuple[str, str]] = field(default_factory=list)
    purge: bool = False
    #: The folder the mods were deployed into before, when this deploy moves
    #: to a new one and nothing is left to undo there: its leftover records
    #: are dropped before the first change (see :func:`forget_game`).
    switch_from: Path | None = None
    #: Ids of journal entries an interrupted run left unfinished; this plan
    #: reconciles them, so they are closed when it runs without failures.
    unfinished: list[int] = field(default_factory=list)

    @property
    def changes(self) -> list[Op]:
        """Ops that touch the disk (everything but ``forget``)."""
        return [op for op in self.ops if op.kind != "forget"]


@dataclass
class SyncResult:
    plan: SyncPlan
    dry_run: bool
    #: Ops carried out (or, in a dry run, that would be).
    done: list[Op] = field(default_factory=list)
    #: Ops that could not be carried out, with the reason.
    failed: list[tuple[Op, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- desired state


def staged_file(mod: ModRecord, source: str) -> Path:
    return mod.staging_dir() / source


def _candidates(manifest: Manifest, game_id: str) -> dict[str, list[tuple[ModRecord, ModFile]]]:
    """Folded destination -> every enabled (mod, file) providing it, lowest priority first."""
    candidates: dict[str, list[tuple[ModRecord, ModFile]]] = {}
    for mod in manifest.mods(game_id):
        if not mod.enabled:
            continue
        for f in manifest.mod_files(mod.id):
            candidates.setdefault(fold_path(f.dest), []).append((mod, f))
    return candidates


def desired_files(manifest: Manifest, game_id: str) -> tuple[dict[str, tuple[ModRecord, ModFile]], dict[str, list[int]]]:
    """What should be deployed: folded destination -> (winning mod, file), and
    folded destination -> every enabled mod providing it, lowest priority first.
    (Planning additionally skips a winner whose staged file is missing.)"""
    desired: dict[str, tuple[ModRecord, ModFile]] = {}
    providers: dict[str, list[int]] = {}
    for key, candidates in _candidates(manifest, game_id).items():
        desired[key] = candidates[-1]
        providers[key] = [mod.id for mod, _ in candidates]
    return desired, providers


@dataclass
class ModConflicts:
    #: Other mod id -> number of files this mod provides instead of it.
    overwrites: dict[int, int] = field(default_factory=dict)
    #: Other mod id -> number of this mod's files that the other mod replaces.
    overwritten_by: dict[int, int] = field(default_factory=dict)


def conflict_summary(manifest: Manifest, game_id: str) -> dict[int, ModConflicts]:
    """Per enabled mod: which other enabled mods it overwrites / is overwritten by."""
    _, providers = desired_files(manifest, game_id)
    summary: dict[int, ModConflicts] = {}
    for mod_ids in providers.values():
        if len(mod_ids) < 2:
            continue
        winner = mod_ids[-1]
        for loser in mod_ids[:-1]:
            w = summary.setdefault(winner, ModConflicts())
            w.overwrites[loser] = w.overwrites.get(loser, 0) + 1
            lo = summary.setdefault(loser, ModConflicts())
            lo.overwritten_by[winner] = lo.overwritten_by.get(winner, 0) + 1
    return summary


# ---------------------------------------------------------------- inspection


def _inspect(abs_path: Path, record: DeployedFile) -> str:
    """"intact" when *abs_path* is still exactly the link we made, "missing"
    when nothing is there, "foreign" when something else is."""
    try:
        st = os.lstat(abs_path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "foreign"
    if record.link_type == "symlink":
        try:
            return "intact" if os.path.islink(abs_path) and os.readlink(abs_path) == record.source else "foreign"
        except OSError:
            return "foreign"
    if (st.st_ino, st.st_dev) != (record.inode, record.dev) or not stat.S_ISREG(st.st_mode):
        return "foreign"
    if _file_id(record.source) == (st.st_ino, st.st_dev):
        return "intact"  # it is the staged file itself
    # The staged file is gone or replaced, so the inode number may have been
    # freed and reused by a new file: also check size and mtime.
    if record.size is not None and (st.st_size, st.st_mtime_ns) != (record.size, record.mtime_ns):
        return "foreign"
    return "intact"


def _file_id(path: str | Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_dev


def _is_link_to(abs_path: Path, source: Path, link_mode: str) -> bool:
    """Whether *abs_path* is already exactly the link the app would create to *source*."""
    try:
        if link_mode == "symlink":
            return os.path.islink(abs_path) and os.readlink(abs_path) == str(source)
        st = os.lstat(abs_path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and (st.st_ino, st.st_dev) == _file_id(source)


def _points_into_mods(abs_path: str | Path) -> bool:
    """Whether *abs_path* is a symlink into the app's mods folder (which only the app creates)."""
    try:
        if not os.path.islink(abs_path):
            return False
        target = os.path.realpath(os.path.join(os.path.dirname(abs_path), os.readlink(abs_path)))
    except OSError:
        return False
    mods_root = os.path.realpath(paths.mods_dir())
    return target == mods_root or target.startswith(mods_root + os.sep)


def _is_our_link(abs_path: Path, source: Path) -> bool:
    """Whether *abs_path* is a symlink or hardlink to the staged file *source*."""
    return _is_link_to(abs_path, source, "symlink") or _is_link_to(abs_path, source, "hardlink")


def _same_root(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _same_dev(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def check_link_mode(target: GameTarget) -> None:
    """Hardlinks only work when the staged files and the game folder share a
    filesystem; raise :class:`DeployError` explaining that otherwise."""
    if target.link_mode != "hardlink":
        return
    staging = paths.mods_dir(target.game_id)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        same = _same_dev(staging, target.root)
    except OSError as exc:
        raise DeployError(f"can't check the game folder {target.root}: {exc.strerror or exc}") from None
    if not same:
        raise DeployError(
            f"hardlinks need the mods folder ({staging}) and the game folder ({target.root}) on the same "
            "filesystem, and they are not. Use symlinks for this game "
            f"('bethmm override {target.game_id} --link-mode symlink') or move one of them."
        )


# ---------------------------------------------------------------- planning


def _gone(root: Path) -> bool:
    """Whether a game folder is missing -- or empty, as the mount point of a
    drive that isn't mounted is."""
    try:
        with os.scandir(root) as entries:
            return next(entries, None) is None
    except OSError:
        return True


def _gone_message(game_id: str, root: Path) -> str:
    return (f"the game folder {root} is missing or empty. If it is on a drive that isn't mounted, mount it and "
            f"try again. If the game is really gone, 'bethmm purge --forget-missing --game {game_id}' forgets "
            f"the app's links there (the originals it backed up are kept in "
            f"{paths.backups_dir(game_id) / SUPERSEDED_DIR})")


def _has_records(manifest: Manifest, game_id: str) -> bool:
    """Whether the app recorded anything in the game's folder (links, backups,
    folders, or operations an interrupted run started)."""
    return manifest.has_footprint(game_id)


def _blocking(manifest: Manifest, game_id: str, old: Path) -> bool:
    """Whether a purge still has something to undo in *old*, the folder the
    mods were deployed into: links, operations an interrupted run started, or
    originals that can go back. Folders kept because they hold the user's
    files, and originals held because a newer file is at their path, don't
    count -- a purge can't remove those."""
    if manifest.deployed(game_id) or manifest.unfinished_journal(game_id):
        return True
    gone = _gone(old)
    for backup in manifest.backups(game_id).values():
        if os.path.lexists(backup.backup) and (gone or not os.path.lexists(_restore_target(old, backup.path))):
            return True
    return False


def _check_root(manifest: Manifest, target: GameTarget, *,
                purge: bool = False) -> tuple[Path, list[str], Path | None]:
    """The game folder to work on, warnings, and (for a deploy) the folder
    the mods were deployed into before when the game moved and nothing is
    left to undo there. The app's records are relative to the folder the
    mods were deployed into; when the game has moved, a purge cleans up
    wherever the mods are now, and a deploy waits for that.

    A folder that is missing or empty is never taken for "gone for good":
    it may be on a drive that isn't mounted. A purge raises
    :class:`GameFolderGone` and only :func:`purge` with ``forget_missing``
    drops the records."""
    game_id = target.game_id
    root = Path(target.root)
    state = manifest.game_state(game_id)
    records = _has_records(manifest, game_id)
    warnings: list[str] = []
    switch_from: Path | None = None
    if state is not None and not _same_root(Path(state.root), root) and records:
        old = Path(state.root)
        if purge:
            if not _gone(old):
                if not _gone(root):
                    warnings.append(f"cleaning up {old}, where the mods were deployed (the game is now at {root})")
                root = old
            elif not _gone(root):
                warnings.append(f"the folder the mods were deployed into ({old}) is missing or empty; "
                                f"cleaning up {root} instead (the game was moved)")
            else:
                raise GameFolderGone(_gone_message(game_id, old))
        elif _blocking(manifest, game_id, old):
            if _gone(old):
                raise DeployError(
                    f"mods were deployed into {old}, which is missing or empty. If the game was moved to {root}, "
                    f"run 'bethmm purge --game {game_id}' first: it cleans up there. If it is on a drive that isn't "
                    "mounted, mount it. Then deploy again."
                )
            raise DeployError(
                f"mods were deployed into {old}, but the game is now at {root}. Run "
                f"'bethmm purge --game {game_id}' (it uses the old folder) before deploying to the new one."
            )
        else:
            switch_from = old
    if _gone(root):
        if purge:
            raise GameFolderGone(_gone_message(game_id, root))
        if not root.is_dir():
            raise DeployError(f"game folder {root} does not exist")
        if records and switch_from is None:
            raise DeployError(f"game folder {root} is empty; if it is on a drive that isn't mounted, mount it first")
    return root, warnings, switch_from


def preflight(manifest: Manifest, target: GameTarget) -> None:
    """Raise :class:`DeployError` now if a deploy to *target* is bound to
    fail (moved game folder, hardlinks across filesystems), before the caller
    changes the mod list."""
    _check_root(manifest, target)
    check_link_mode(target)


def plan_sync(manifest: Manifest, target: GameTarget, *, purge: bool = False) -> SyncPlan:
    """Work out how to bring the game folder to the desired state (every
    enabled mod deployed, or with ``purge`` nothing at all). Reads the disk,
    changes nothing."""
    game_id = target.game_id
    root, root_warnings, switch_from = _check_root(manifest, target, purge=purge)

    plan = SyncPlan(game_id, root, target.link_mode, purge=purge, warnings=root_warnings, switch_from=switch_from)
    desired: dict[str, tuple[ModRecord, ModFile]] = {}
    for key, candidates in ({} if purge else _candidates(manifest, game_id)).items():
        if len(candidates) > 1:
            plan.conflicts[key] = [mod.id for mod, _ in candidates]
        # The highest-priority provider whose staged file exists wins; a missing
        # one is reported and the next provider (or the original file) takes over.
        for mod, f in reversed(candidates):
            staged = staged_file(mod, f.source)
            if staged.is_file():
                desired[key] = (mod, f)
                break
            plan.blocked.append((f.dest, f"staged file {staged} is missing; reinstall '{mod.name}'"))
    deployed = manifest.deployed(game_id)
    backups = manifest.backups(game_id)
    created = manifest.created_dirs(game_id)
    left_alone = manifest.left_alone(game_id)
    if switch_from is not None:
        # Nothing is left to undo in the old folder; what is still recorded
        # there (folders holding the user's files, originals held because a
        # newer file is at their path) doesn't apply to this one.
        held = sum(os.path.lexists(b.backup) for b in backups.values())
        plan.warnings.append(
            f"the mods were deployed into {switch_from} before; switching to {root}. The app forgets "
            f"{len(created)} folder(s) it had made there (they hold files it didn't create, so they stay)"
            + (f" and keeps {held} original(s) from there in {paths.backups_dir(game_id) / SUPERSEDED_DIR}"
               if held else "")
        )
        backups, created, left_alone = {}, {}, {}
    unfinished = manifest.unfinished_journal(game_id)
    plan.unfinished = [e.id for e in unfinished]
    if unfinished:
        plan.warnings.append(f"a previous run was interrupted ({len(unfinished)} unfinished operations); "
                             "this run cleans up after it")
    interrupted_links = {fold_path(e.path): e for e in unfinished if e.action == "link" and e.path and e.source}
    resolver = CaseResolver(root)
    #: Links of the app that the manifest doesn't record (made by an interrupted
    #: run, or moved by a case rename of their folder): key -> (path, staged source).
    ours_unrecorded: dict[str, tuple[str, str]] = {
        key: (e.path, e.source) for key, e in interrupted_links.items()
        if key not in deployed and _is_our_link(root / e.path, Path(e.source))
    }
    #: Where the app's links are on disk now (to tell its renamed folders from the user's).
    app_paths: list[str] = [path for path, _ in ours_unrecorded.values()]

    removals: list[Op] = []
    restores: list[Op] = []
    satisfied: set[str] = set()
    freed: set[str] = set()      # keys whose path will be empty after removals
    foreign: dict[str, str] = {}  # key -> path holding something we don't own (left alone)
    final: dict[str, str] = {}   # key -> path of every link present after the plan
    kept: list[str] = []         # paths whose folders must stay although no link needs them

    for key, record in sorted(deployed.items()):
        status = _inspect(root / record.path, record)
        want = desired.get(key)
        if status == "intact":
            app_paths.append(record.path)
        if status == "intact" and want is not None and _satisfies(record, want, target.link_mode):
            satisfied.add(key)
            final[key] = record.path
            continue
        if status == "intact":
            removals.append(Op("unlink", record.path, record.mod_id, record.source))
            freed.add(key)
        elif status == "missing":
            moved = _case_variant(resolver, record.path)
            if moved is None:
                removals.append(Op("forget", record.path, record.mod_id, record.source, "the link was already gone"))
                freed.add(key)
            elif _is_our_link(root / moved, Path(record.source)):
                # A folder above it was renamed (only the case changed): the link moved along.
                removals.append(Op("forget", record.path, record.mod_id, record.source, f"now at {moved}"))
                ours_unrecorded[key] = (moved, record.source)
                app_paths.append(moved)
            else:
                removals.append(Op("forget", moved, record.mod_id, record.source, FOREIGN_NOTE))
                foreign[key] = moved
                plan.warnings.append(f"{moved} was replaced by something the app didn't create; it is left alone")
        else:
            removals.append(Op("forget", record.path, record.mod_id, record.source, FOREIGN_NOTE))
            foreign[key] = record.path
            plan.warnings.append(f"{record.path} was replaced by something the app didn't create; it is left alone")
    for key, path in sorted(left_alone.items()):
        if key in deployed:
            continue
        here = path if os.path.lexists(root / path) else _case_variant(resolver, path)
        if here is not None and not _points_into_mods(root / here):
            foreign[key] = here
        else:
            removals.append(Op("forget", path, None, None, LEFT_ALONE_GONE_NOTE))

    stale: set[str] = set()  # backup records whose file is gone
    for key, backup in sorted(backups.items()):
        if not os.path.lexists(backup.backup):
            # Deleted by the user, or the app died before moving the original:
            # nothing to restore, so the record is dropped.
            restores.append(Op("restore", backup.path, None, backup.backup, "the backup file is gone"))
            stale.add(key)
            continue
        if key in desired and key not in foreign:
            continue  # the path stays occupied by a mod file; keep the original backed up
        if key in foreign:
            plan.warnings.append(f"the original {backup.path} stays in {backup.backup} because the path is occupied")
            continue
        place = _restore_target(root, backup.path)
        if key in deployed or key in ours_unrecorded or not os.path.lexists(place):
            restores.append(Op("restore", backup.path, None, backup.backup))
        elif _same_content(place, Path(backup.backup)):
            # The original is back in place (an interrupted move): the copy is spare.
            restores.append(Op("dropbackup", backup.path, None, backup.backup, "the original is back in place"))
        else:
            plan.warnings.append(
                f"the original {backup.path} stays in {backup.backup}: something else now exists at that path"
            )

    additions: list[Op] = []
    planned_dirs: set[str] = set()
    # Folders made just before the app died are recorded now, so they are
    # removed again (only ever when empty) once nothing needs them.
    for entry in unfinished:
        if entry.action != "mkdir" or not entry.path:
            continue
        dkey = fold_path(entry.path)
        abs_dir = root / entry.path
        if dkey not in created and dkey not in planned_dirs and abs_dir.is_dir() and not abs_dir.is_symlink():
            planned_dirs.add(dkey)
            created = {**created, dkey: entry.path}
            additions.append(Op("mkdir", entry.path, None, None, ADOPT_DIR_NOTE))
    for key, (mod, f) in sorted(desired.items(), key=lambda item: item[0]):
        if key in satisfied:
            continue
        if key in foreign:
            plan.blocked.append((foreign[key], "replaced by a file the app didn't create (left alone)"))
            continue
        staged = staged_file(mod, f.source)
        # A path the app linked before keeps its spelling; resolving it again
        # finds folders that have to be recreated.
        resolved = resolver.resolve(deployed[key].path if key in deployed else f.dest, plan=True)
        if resolved.type_conflict:
            plan.blocked.append((f.dest, "a folder (or file) of the other kind already exists there"))
            continue
        plan.warnings.extend(w for w in resolved.ambiguous if w not in plan.warnings)
        path = str(resolved.rel)
        backup_op: Op | None = None
        note = ""
        if resolved.exists_on_disk and key not in freed:
            abs_path = root / path
            leftover = ours_unrecorded.get(key)
            if _is_link_to(abs_path, staged, target.link_mode):
                note = "already in place (left by an interrupted run); recording it"
                ours_unrecorded.pop(key, None)
            elif leftover is not None and _is_our_link(abs_path, Path(leftover[1])):
                # Another link of the app's that isn't recorded: provably ours, so it goes.
                removals.append(Op("unlink", path, None, leftover[1], LEFTOVER_NOTE))
                ours_unrecorded.pop(key)
            elif _points_into_mods(abs_path):
                plan.blocked.append((path, "an unrecorded link into the app's mods folder is in the way; "
                                           "run 'bethmm verify --repair'"))
                kept.append(path)
                continue
            elif key in backups and key not in stale:
                if not _same_content(abs_path, Path(backups[key].backup)):
                    plan.blocked.append((path, f"a backup of the original already exists ({backups[key].backup}) "
                                               "and the game folder has another copy; run 'bethmm verify'"))
                    continue
                # An interrupted move left the original in both places: drop the spare copy.
                restores.append(Op("dropbackup", path, None, backups[key].backup, "the original is back in place"))
                backup_op = Op("backup", path, None, str(_backup_path(game_id, path)), "original game file")
            else:
                backup_op = Op("backup", path, None, str(_backup_path(game_id, path)), "original game file")
        for d in resolved.new_dirs:
            dkey = fold_path(d)
            if dkey not in planned_dirs:  # also when a folder the app made earlier has disappeared
                planned_dirs.add(dkey)
                additions.append(Op("mkdir", str(d), mod.id))
        if backup_op is not None:
            additions.append(backup_op)
        additions.append(Op("link", path, mod.id, str(staged), note))
        final[key] = path

    # Unrecorded links of the app's that nothing wants any more.
    for key, (path, source) in sorted(ours_unrecorded.items()):
        if key in final or (key in desired and key not in foreign):
            continue
        removals.append(Op("unlink", path, None, source, LEFTOVER_NOTE))

    # Folders the app created that no remaining link needs, deepest first.
    needed: set[str] = set()
    for p in [*final.values(), *kept]:
        parent = PurePosixPath(p).parent
        if parent.parts:  # not a file directly in the game root
            needed.update(_prefixes(fold_path(parent)))
    def on_disk(path: str) -> str:
        """A folder the app made and the user renamed in another case is
        removed under its new spelling -- recognised by the app's links in it."""
        if os.path.lexists(root / path):
            return path
        variant = _case_variant(resolver, path)
        if variant is not None and any(p.startswith(variant + "/") for p in app_paths):
            return variant
        return path

    rmdirs = [
        Op("rmdir", on_disk(path))
        for dkey, path in sorted(created.items(), key=lambda item: (-item[0].count("/"), item[0]))
        if dkey not in needed
    ]

    plan.ops = removals + restores + additions + rmdirs
    return plan


def _case_variant(resolver: CaseResolver, rel: str) -> str | None:
    """The on-disk spelling of *rel* when it exists only in another case."""
    try:
        resolved = resolver.resolve(rel)
    except ValueError:
        return None
    if resolved.exists_on_disk and str(resolved.rel) != rel:
        return str(resolved.rel)
    return None


def _restore_target(root: Path, rel: str) -> Path:
    """Where an original recorded as *rel* goes back: into its folders as
    they are spelled on disk now (matched case-insensitively), under its own
    exact file name -- a case-variant sibling is a different file."""
    rel_path = PurePosixPath(rel)
    if len(rel_path.parts) <= 1:
        return root / rel_path
    parent: Path = root
    for i, part in enumerate(rel_path.parts[:-1]):
        match = _dir_match(parent, part)
        if match is None:
            return parent.joinpath(*rel_path.parts[i:])
        parent = parent / match
    return parent / rel_path.parts[-1]


def _dir_match(parent: Path, name: str) -> str | None:
    """The sub-folder of *parent* called *name*, exact spelling first, else any case."""
    if (parent / name).is_dir() and not (parent / name).is_symlink():
        return name
    try:
        entries = sorted(e.name for e in os.scandir(parent) if e.is_dir(follow_symlinks=False))
    except OSError:
        return None
    folded = fold_path(name)
    return next((e for e in entries if fold_path(e) == folded), None)


def _same_content(a: Path, b: Path) -> bool:
    try:
        if os.path.islink(a) or os.path.islink(b) or not (os.path.isfile(a) and os.path.isfile(b)):
            return False
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _makedirs_logged(path: Path, stop: Path) -> None:
    """Create *path* and any missing parents below *stop*, logging each one.
    Used for the app's backups folder and for recreating folders that held an
    original game file."""
    missing: list[Path] = []
    current = path
    while not current.exists() and current != stop and current != current.parent:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        folder.mkdir()
        log.info("MKDIR %s", folder)


def _satisfies(record: DeployedFile, want: tuple[ModRecord, ModFile], link_mode: str) -> bool:
    """Whether an intact link *record* is exactly what *want* asks for."""
    mod, f = want
    staged = staged_file(mod, f.source)
    if record.mod_id != mod.id or record.source != str(staged) or record.link_type != link_mode:
        return False
    # A hardlink is a copy of the staged file at link time; relink if the staged file was replaced.
    return link_mode == "symlink" or _file_id(staged) == (record.inode, record.dev)


def _prefixes(key: str) -> Iterable[str]:
    parts = key.split("/")
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i])


def _backup_path(game_id: str, rel: str) -> Path:
    """A free path for the original of *rel* under ``backups/<game>/``."""
    base = paths.backups_dir(game_id) / rel
    candidate, n = base, 1
    while os.path.lexists(candidate):
        candidate = base.with_name(f"{base.name}.{n}")
        n += 1
    return candidate


# ---------------------------------------------------------------- applying


def apply_plan(manifest: Manifest, plan: SyncPlan, *, dry_run: bool = False) -> SyncResult:
    """Carry out *plan*. Each op is checked again right before it runs; one
    that can't be done safely is skipped (``failed``) and the rest continue."""
    result = SyncResult(plan, dry_run, warnings=list(plan.warnings))
    if dry_run:
        result.done = list(plan.ops)
        return result
    if plan.switch_from is not None:
        result.warnings.extend(forget_game(manifest, plan.game_id))
    deployed = manifest.deployed(plan.game_id)
    backups = manifest.backups(plan.game_id)
    # Record where the mods go before the first change, so the moved-folder
    # check works even if this run is interrupted.
    state = manifest.game_state(plan.game_id)
    if state is None or not _same_root(Path(state.root), plan.root):
        manifest.set_game_state(plan.game_id, plan.root, deployed=True if state is None else state.deployed)
    backed_up_now: set[str] = set()
    for op in plan.ops:
        entry = manifest.journal(plan.game_id, op.kind, op.path, op.source, op.note or None, status="intent")
        try:
            note = _run(op, plan, manifest, deployed, backups, backed_up_now)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            manifest.finish_journal(entry, "failed", reason)
            log.error("FAILED %s %s: %s", op.kind.upper(), plan.root / op.path, reason)
            result.failed.append((op, reason))
            continue
        except _Skip as skip:
            manifest.finish_journal(entry, "skipped", str(skip))
            log.warning("SKIPPED %s %s: %s", op.kind.upper(), plan.root / op.path, skip)
            result.failed.append((op, str(skip)))
            continue
        if note:
            result.warnings.append(note)
        manifest.finish_journal(entry, "done")
        result.done.append(op)
    manifest.set_game_state(plan.game_id, plan.root, deployed=not plan.purge)
    if not result.failed:
        # The disk now matches the records, whatever the interrupted run was doing.
        for entry_id in plan.unfinished:
            manifest.finish_journal(entry_id, "abandoned", "cleaned up by a later run")
    return result


class _Skip(Exception):
    """An op that would be unsafe now (the disk changed since planning)."""


def _run(op: Op, plan: SyncPlan, manifest: Manifest, deployed: dict[str, DeployedFile],
         backups: dict[str, BackupRecord], backed_up_now: set[str]) -> str | None:
    game_id, root = plan.game_id, plan.root
    abs_path = root / op.path
    key = fold_path(op.path)

    if op.kind == "forget":
        with manifest.transaction():
            manifest.forget_link(game_id, op.path)
            if op.note == FOREIGN_NOTE:
                manifest.record_left_alone(game_id, op.path)
            elif op.note == LEFT_ALONE_GONE_NOTE:
                manifest.forget_left_alone(game_id, op.path)
        deployed.pop(key, None)
        log.info("FORGET %s (%s)", abs_path, op.note)
        return None

    if op.kind == "unlink":
        record = deployed.get(key)
        if record is None and op.note == LEFTOVER_NOTE:
            if not _is_our_link(abs_path, Path(op.source or "")):
                raise _Skip("it changed since the plan was made; left alone")
            os.unlink(abs_path)
            log.info("UNLINK %s (%s)", abs_path, op.note)
            return None
        if record is None or _inspect(abs_path, record) != "intact":
            with manifest.transaction():
                manifest.forget_link(game_id, op.path)
            deployed.pop(key, None)
            raise _Skip("it changed since the plan was made; left alone")
        os.unlink(abs_path)
        with manifest.transaction():
            manifest.forget_link(game_id, op.path)
        deployed.pop(key, None)
        log.info("UNLINK %s", abs_path)
        return None

    if op.kind == "restore":
        backup = Path(op.source or "")
        if not os.path.lexists(backup):
            with manifest.transaction():
                manifest.forget_backup(game_id, op.path)
            backups.pop(key, None)
            if os.path.lexists(abs_path):
                log.info("FORGET backup of %s (%s is gone; something is in place)", abs_path, backup)
                return f"dropped the record of the backup {backup}: it is gone and {op.path} exists"
            raise _Skip(f"the backup {backup} is gone")
        abs_path = _restore_target(root, op.path)  # into the folders as they are spelled now
        if os.path.lexists(abs_path):
            raise _Skip(f"something is in the way; the original stays in {backup}")
        if os.path.lexists(abs_path.parent) and not abs_path.parent.is_dir():
            raise _Skip(f"a file is in the way of its folder; the original stays in {backup}")
        _makedirs_logged(abs_path.parent, root)
        _move(backup, abs_path)
        with manifest.transaction():
            manifest.forget_backup(game_id, op.path)
        backups.pop(key, None)
        log.info("RESTORE %s <- %s", abs_path, backup)
        _prune_empty_dirs(backup.parent, paths.backups_dir(game_id))
        return None

    if op.kind == "dropbackup":
        backup = Path(op.source or "")
        place = _restore_target(root, op.path)
        if os.path.lexists(backup):
            if not _same_content(place, backup):
                raise _Skip("the file in the game folder no longer matches the backup; keeping the backup")
            os.unlink(backup)
            log.info("DELETE %s (spare copy: the original is back at %s)", backup, place)
        with manifest.transaction():
            manifest.forget_backup(game_id, op.path)
        backups.pop(key, None)
        _prune_empty_dirs(backup.parent, paths.backups_dir(game_id))
        return None

    if op.kind == "mkdir":
        if os.path.lexists(abs_path):
            if abs_path.is_dir() and not abs_path.is_symlink():
                if op.note == ADOPT_DIR_NOTE:
                    with manifest.transaction():
                        manifest.record_dir(game_id, op.path)
                    log.info("ADOPT %s (%s)", abs_path, op.note)
                return None  # otherwise already there (not ours, so not recorded)
            raise _Skip("a file with that name is in the way")
        if op.note == ADOPT_DIR_NOTE:
            return None
        os.mkdir(abs_path)
        with manifest.transaction():
            manifest.record_dir(game_id, op.path)
        log.info("MKDIR %s", abs_path)
        return None

    if op.kind == "backup":
        if not os.path.lexists(abs_path):
            return None  # nothing to move aside any more
        if abs_path.is_dir() and not abs_path.is_symlink():
            raise _Skip("it is a folder")
        if key in backups:
            raise _Skip(f"a backup of the original already exists ({backups[key].backup})")
        destination = _backup_path(game_id, op.path)
        _makedirs_logged(destination.parent, paths.data_dir())
        # Recorded before the move: if the app dies in between, the record
        # points at a missing file (which verify/repair drop) and the original
        # is still in place -- it is never moved without a record.
        record = BackupRecord(game_id, op.path, str(destination))
        with manifest.transaction():
            manifest.record_backup(record)
        try:
            _move(abs_path, destination)
        except OSError:
            if os.path.lexists(abs_path) and not os.path.lexists(destination):  # not moved
                with manifest.transaction():
                    manifest.forget_backup(game_id, op.path)
            raise
        backups[key] = record
        backed_up_now.add(key)
        log.info("BACKUP %s -> %s", abs_path, destination)
        return None

    if op.kind == "link":
        source = Path(op.source or "")
        adopted = False
        if os.path.lexists(abs_path):
            # Exactly the link we want, left by an interrupted run: just record it.
            if not _is_link_to(abs_path, source, plan.link_mode):
                raise _Skip("something is in the way")
            adopted = True
        elif not source.is_file():
            _undo_backup(key, abs_path, game_id, manifest, backups, backed_up_now)
            raise _Skip(f"the staged file {source} is missing")
        else:
            try:
                if plan.link_mode == "symlink":
                    os.symlink(str(source), abs_path)
                else:
                    os.link(source, abs_path)
            except OSError:
                # Don't leave the game without the original it had a moment ago.
                _undo_backup(key, abs_path, game_id, manifest, backups, backed_up_now)
                raise
        record = DeployedFile(game_id, op.path, op.mod_id or 0, str(source), plan.link_mode)
        if plan.link_mode == "hardlink":
            st = os.lstat(abs_path)
            record = DeployedFile(game_id, op.path, op.mod_id or 0, str(source), plan.link_mode,
                                  st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)
        with manifest.transaction():
            manifest.record_link(record)
        deployed[key] = record
        log.info("%s %s -> %s (%s)", "ADOPT" if adopted else "LINK", abs_path, source, plan.link_mode)
        return None

    if op.kind == "rmdir":
        if not os.path.lexists(abs_path):
            with manifest.transaction():
                manifest.forget_dir(game_id, op.path)
            return None
        if os.path.islink(abs_path) or not abs_path.is_dir():
            with manifest.transaction():
                manifest.forget_dir(game_id, op.path)
            log.info("KEEP %s (replaced by something the app didn't create)", abs_path)
            return f"left {op.path} alone: the folder the app made there was replaced by something else"
        try:
            os.rmdir(abs_path)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            if _holds_app_files(manifest, game_id, key, deployed):
                # E.g. a link whose removal failed: keep the record so a later run removes the folder.
                log.info("KEEP %s (still holds files of the app)", abs_path)
                return f"kept the folder {op.path} for now: it still holds files of the app"
            # The app made it, so it stays recorded: removed once it is empty.
            log.info("KEEP %s (contains files the app didn't create)", abs_path)
            return f"kept the folder {op.path}: it contains files the app didn't create"
        with manifest.transaction():
            manifest.forget_dir(game_id, op.path)
        log.info("RMDIR %s", abs_path)
        return None

    raise ValueError(f"unknown op kind {op.kind!r}")


def _undo_backup(key: str, abs_path: Path, game_id: str, manifest: Manifest,
                 backups: dict[str, BackupRecord], backed_up_now: set[str]) -> None:
    """Put back an original this run moved aside when its link can't be made."""
    record = backups.get(key)
    if key not in backed_up_now or record is None or os.path.lexists(abs_path):
        return
    try:
        _move(Path(record.backup), abs_path)
    except OSError as exc:
        log.error("could not put back %s from %s: %s", abs_path, record.backup, exc)
        return
    with manifest.transaction():
        manifest.forget_backup(game_id, record.path)
    backups.pop(key, None)
    backed_up_now.discard(key)
    log.info("RESTORE %s <- %s (the link could not be made)", abs_path, record.backup)
    _prune_empty_dirs(Path(record.backup).parent, paths.backups_dir(game_id))


def _holds_app_files(manifest: Manifest, game_id: str, key: str, deployed: dict[str, DeployedFile]) -> bool:
    """Whether anything the app made (or was making when it died) lies below folded folder *key*."""
    below = key + "/"
    if any(k.startswith(below) for k in deployed) or any(k.startswith(below) for k in manifest.created_dirs(game_id)):
        return True
    return any(e.path and e.action in ("mkdir", "link") and fold_path(e.path).startswith(below)
               for e in manifest.unfinished_journal(game_id))


def _move(src: Path, dst: Path) -> None:
    """Rename, falling back to copy + delete across filesystems. Never
    replaces an existing *dst*."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "the destination already exists", str(dst))
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # Across filesystems: copy to a temporary name next to *dst*, flush it,
    # rename it into place (atomic), then remove *src*. A kill at any point
    # leaves either a whole file under the final name or a TEMP_PREFIX file
    # that verify reports and repair removes -- never a truncated original.
    tmp = dst.with_name(f"{TEMP_PREFIX}{secrets.token_hex(6)}")
    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), tmp)
        else:
            shutil.copy2(src, tmp)
            if os.path.getsize(src) != os.path.getsize(tmp):
                raise OSError(errno.EIO, f"copy of {src} is incomplete")
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "the destination already exists", str(dst))
        os.rename(tmp, dst)
        _fsync_dir(dst.parent)  # the new name is on disk before the old file goes
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    os.unlink(src)
    _fsync_dir(src.parent)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # not supported by every filesystem
        log.debug("could not fsync %s: %s", path, exc)
    finally:
        os.close(fd)


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove empty folders from *start* up to (not including) *stop* -- only
    used inside the app's own backups folder."""
    try:
        start.relative_to(stop)
    except ValueError:
        return
    current = start
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        log.info("RMDIR %s", current)
        current = current.parent


# ---------------------------------------------------------------- high level


def deploy(manifest: Manifest, target: GameTarget, *, dry_run: bool = False) -> SyncResult:
    """Bring the game folder in line with the enabled mods (and mark the game deployed)."""
    check_link_mode(target)
    return apply_plan(manifest, plan_sync(manifest, target), dry_run=dry_run)


def purge(manifest: Manifest, target: GameTarget, *, dry_run: bool = False,
          forget_missing: bool = False) -> SyncResult:
    """Remove everything the app put in the game folder and restore the
    originals. Mods stay installed; ``deploy`` puts them back.

    A game folder that is missing or empty raises :class:`GameFolderGone`
    (its drive may just not be mounted) -- unless *forget_missing*: then the
    game is taken to be gone for good, and the app forgets its links and
    folders there and puts the originals it had backed up aside in
    ``backups/<game>/.superseded/`` (see :func:`forget_game`)."""
    try:
        plan = plan_sync(manifest, target, purge=True)
    except GameFolderGone as exc:
        plan = SyncPlan(target.game_id, Path(target.root), target.link_mode, purge=True)
        result = SyncResult(plan, dry_run)
        if not _has_records(manifest, target.game_id):
            result.warnings.append(f"nothing is recorded for {target.root}; nothing to undo")
            return result
        if not forget_missing:
            raise
        result.warnings.append(f"{str(exc).split('. ')[0]}; forgetting what the app had there")
        result.warnings.extend(forget_game(manifest, target.game_id, dry_run=dry_run))
        return result
    return apply_plan(manifest, plan, dry_run=dry_run)


def forget_game(manifest: Manifest, game_id: str, *, dry_run: bool = False) -> list[str]:
    """Drop every record of the app's links and folders in a game folder that
    no longer exists, and move the originals it backed up to
    ``backups/<game>/.superseded/<time>/`` (nothing is deleted). Returns what
    was (or would be) done."""
    notes: list[str] = []
    backups = manifest.backups(game_id)
    links, dirs = manifest.deployed(game_id), manifest.created_dirs(game_id)
    if links or dirs:
        notes.append(f"{'would forget' if dry_run else 'forgot'} {len(links)} link(s) and {len(dirs)} folder(s)")
    if backups:
        where = _superseded_dir(game_id)
        notes.append(f"{'would keep' if dry_run else 'kept'} {len(backups)} original file(s) the app had backed up "
                     f"in {where}")
    if dry_run:
        return notes
    for record in links.values():
        manifest.forget_link(game_id, record.path)
    for path in dirs.values():
        manifest.forget_dir(game_id, path)
    for path in manifest.left_alone(game_id).values():
        manifest.forget_left_alone(game_id, path)
    for record in backups.values():
        _supersede(manifest, game_id, record)
    for entry in manifest.unfinished_journal(game_id):
        manifest.finish_journal(entry.id, "abandoned", "the game folder is gone")
    state = manifest.game_state(game_id)
    if state is not None:
        manifest.set_game_state(game_id, state.root, deployed=False)
    manifest.journal(game_id, "forget-game", None, None, "; ".join(notes))
    log.info("FORGET-GAME %s: %s", game_id, "; ".join(notes))
    return notes


def _superseded_dir(game_id: str) -> Path:
    return paths.backups_dir(game_id) / SUPERSEDED_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")


def _supersede(manifest: Manifest, game_id: str, record: BackupRecord, where: Path | None = None) -> Path | None:
    """Move a backed-up original the app no longer needs aside and forget it."""
    where = where or _superseded_dir(game_id)
    source = Path(record.backup)
    destination = None
    if os.path.lexists(source):
        destination = where / record.path
        n = 1
        while os.path.lexists(destination):
            destination = destination.with_name(f"{destination.name}.{n}")
            n += 1
        _makedirs_logged(destination.parent, paths.data_dir())
        _move(source, destination)
        log.info("MOVE %s -> %s (superseded original)", source, destination)
        _prune_empty_dirs(source.parent, paths.backups_dir(game_id))
    with manifest.transaction():
        manifest.forget_backup(game_id, record.path)
    return destination


def refresh(manifest: Manifest, target: GameTarget, *, dry_run: bool = False) -> SyncResult | None:
    """Redeploy after a change to the mod list -- unless the game was purged,
    in which case nothing is touched and None is returned."""
    state = manifest.game_state(target.game_id)
    if state is not None and not state.deployed:
        return None
    return deploy(manifest, target, dry_run=dry_run)


# ---------------------------------------------------------------- verify / repair


@dataclass
class VerifyReport:
    game_id: str
    root: Path
    #: Recorded links that no longer exist.
    missing: list[DeployedFile] = field(default_factory=list)
    #: Recorded links replaced by something the app didn't create.
    foreign: list[DeployedFile] = field(default_factory=list)
    #: Files of the user's the app leaves alone where its links were (informational).
    left_alone: list[str] = field(default_factory=list)
    #: Symlinks into the app's mods folder that the manifest doesn't know (relative paths).
    orphan_links: list[str] = field(default_factory=list)
    #: Recorded backups whose file is gone.
    missing_backups: list[BackupRecord] = field(default_factory=list)
    #: Files in the game's backups folder the manifest doesn't know (absolute paths).
    orphan_backups: list[str] = field(default_factory=list)
    #: Recorded created folders that no longer exist.
    missing_dirs: list[str] = field(default_factory=list)
    #: Operations that started but never finished (the app was interrupted).
    interrupted: list[JournalEntry] = field(default_factory=list)
    #: Recorded originals that can't go back because a file the app didn't
    #: create now sits at their path (e.g. a game update rewrote it).
    held_backups: list[BackupRecord] = field(default_factory=list)
    #: Half-copied files from a move across filesystems that was interrupted.
    temp_leftovers: list[str] = field(default_factory=list)
    #: Recorded originals whose path is free: the next purge (or deploy) puts them back.
    restorable: list[BackupRecord] = field(default_factory=list)
    #: (recorded path, path now) of the app's links whose folder was renamed in
    #: another case (informational: the next deploy or purge records them).
    case_moved: list[tuple[str, str]] = field(default_factory=list)
    #: The game folder the mods were deployed into is missing or empty (e.g.
    #: on a drive that isn't mounted): nothing else was checked.
    root_missing: bool = False
    #: The folder the mods were deployed into, when it is gone and the game
    #: was found at ``root`` instead (moved).
    moved_from: Path | None = None

    @property
    def ok(self) -> bool:
        return not (self.missing or self.foreign or self.orphan_links or self.missing_backups
                    or self.orphan_backups or self.missing_dirs or self.interrupted
                    or self.held_backups or self.temp_leftovers or self.restorable or self.root_missing)


def verify(manifest: Manifest, target: GameTarget) -> VerifyReport:
    """Compare the manifest with the disk. Read-only."""
    game_id = target.game_id
    state = manifest.game_state(game_id)
    root = Path(state.root) if state is not None else Path(target.root)
    report = VerifyReport(game_id, root)
    records = _has_records(manifest, game_id)
    if records and _gone(root) and not _same_root(root, Path(target.root)) and not _gone(Path(target.root)):
        report.moved_from, root = root, Path(target.root)
        report.root = root
    if _gone(root):
        # Never judge the links while the folder is away: they may all be fine
        # on a drive that isn't mounted.
        report.root_missing = records
        return report
    resolver = CaseResolver(root)
    deployed = manifest.deployed(game_id)
    for record in deployed.values():
        status = _inspect(root / record.path, record)
        if status == "missing":
            moved = _case_variant(resolver, record.path)
            if moved is not None and _is_our_link(root / moved, Path(record.source)):
                report.case_moved.append((record.path, moved))
            else:
                report.missing.append(record)
        elif status == "foreign":
            report.foreign.append(record)

    known = set(deployed)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if name.startswith(TEMP_PREFIX) and (name in filenames or os.path.islink(full)):
                report.temp_leftovers.append(full)
                continue
            if not os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if fold_path(rel) not in known and _points_into_mods(full):
                report.orphan_links.append(rel)

    unfinished_links = _unfinished_links(manifest, game_id)
    backups = manifest.backups(game_id)
    for key, backup in backups.items():
        if not os.path.lexists(backup.backup):
            report.missing_backups.append(backup)
            continue
        place = _restore_target(root, backup.path)
        if not os.path.lexists(place):
            if key not in deployed:
                report.restorable.append(backup)
        elif not _app_file_at(place, deployed.get(key), unfinished_links.get(key)):
            report.held_backups.append(backup)
    recorded = {os.path.realpath(b.backup) for b in backups.values()}
    backup_root = paths.backups_dir(game_id)
    if backup_root.is_dir():
        for dirpath, dirnames, filenames in os.walk(backup_root):
            if os.path.normpath(dirpath) == os.path.normpath(backup_root):
                dirnames[:] = [d for d in dirnames if d != SUPERSEDED_DIR]
            for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                full = os.path.join(dirpath, name)
                if name.startswith(TEMP_PREFIX):
                    report.temp_leftovers.append(full)
                elif os.path.realpath(full) not in recorded and name in filenames:
                    report.orphan_backups.append(full)

    for path in manifest.created_dirs(game_id).values():
        here = path if (root / path).is_dir() else (_case_variant(resolver, path) or path)
        if not (root / here).is_dir():
            report.missing_dirs.append(path)

    report.left_alone = sorted(path for path in manifest.left_alone(game_id).values()
                               if os.path.lexists(root / path))
    report.interrupted = manifest.unfinished_journal(game_id)
    report.orphan_links.sort()
    report.orphan_backups.sort()
    report.temp_leftovers.sort()
    return report


def _unfinished_links(manifest: Manifest, game_id: str) -> dict[str, JournalEntry]:
    return {fold_path(e.path): e for e in manifest.unfinished_journal(game_id)
            if e.action == "link" and e.path and e.source}


def _app_file_at(place: Path, record: DeployedFile | None, entry: JournalEntry | None) -> bool:
    """Whether *place*, where an original goes back, holds a link of the
    app's: a symlink into its mods folder, the recorded link (also after a
    case rename of its folder), or the link of an interrupted run."""
    if _points_into_mods(place):
        return True
    if record is not None and _is_our_link(place, Path(record.source)):
        return True
    return entry is not None and _is_our_link(place, Path(entry.source or ""))


def repair(manifest: Manifest, target: GameTarget, report: VerifyReport, *, dry_run: bool = False,
           release_held: bool = False) -> list[str]:
    """Fix what :func:`verify` found, as far as that is safe. Returns what was
    (or would be) done. Afterwards a ``deploy`` recreates missing links (or,
    for a purged game, a ``purge`` restores freed originals).

    Nothing is changed while the game folder is missing or empty. Originals
    held in the backups because another file sits at their path stay tracked
    (deleting that file and purging brings them back) -- unless
    *release_held*, which moves them to ``backups/<game>/.superseded/`` and
    stops tracking them (for when a game update put a newer file there). A
    backup identical to the file at its path is a spare copy and dropped.
    Half-copied files of an interrupted move are moved to ``.superseded``
    too, never deleted."""
    game_id, root = report.game_id, report.root
    actions: list[str] = []
    if report.root_missing:
        return [f"change nothing: the game folder {root} is missing or empty. Mount its drive if it isn't "
                f"mounted; if the game is gone, 'bethmm purge --forget-missing --game {game_id}'"]

    def act(text: str) -> bool:
        actions.append(text)
        log.info("REPAIR %s%s", text, " (dry run)" if dry_run else "")
        if not dry_run:
            manifest.journal(game_id, "repair", None, None, text)
        return not dry_run

    # The report may be stale (shown to the user first), so everything is
    # checked again right before it is done.
    def changed(text: str) -> None:
        actions.append(f"leave {text}: it changed since the check")

    for record in report.missing + report.foreign:
        current = manifest.deployed(game_id).get(fold_path(record.path))
        if current is None:
            continue
        if _inspect(root / current.path, current) == "intact":
            changed(current.path)
        elif record in report.missing:
            if act(f"forget missing link {record.path}"):
                manifest.forget_link(game_id, record.path)
        elif act(f"forget {record.path} (replaced by another file, which is left alone)"):
            with manifest.transaction():
                manifest.forget_link(game_id, record.path)
                manifest.record_left_alone(game_id, record.path)
    for rel in report.orphan_links:
        if fold_path(rel) in manifest.deployed(game_id) or not _points_into_mods(root / rel):
            changed(rel)
        elif act(f"remove stray link {rel} (points into the app's mods folder)"):
            os.unlink(root / rel)
            log.info("UNLINK %s (stray link)", root / rel)
    for backup in report.missing_backups:
        if os.path.lexists(backup.backup):
            changed(backup.backup)
        elif act(f"forget backup record of {backup.path} (the backup file {backup.backup} is gone)"):
            manifest.forget_backup(game_id, backup.path)
    backup_root = paths.backups_dir(game_id)
    for full in report.orphan_backups:
        rel = os.path.relpath(full, backup_root).replace(os.sep, "/")
        recorded = {os.path.realpath(b.backup) for b in manifest.backups(game_id).values()}
        if not os.path.lexists(full) or os.path.realpath(full) in recorded:
            changed(full)
        elif os.path.lexists(_restore_target(root, rel)):
            actions.append(f"keep unrecorded backup {full}: {rel} exists in the game folder")
        elif act(f"restore unrecorded backup {full} -> {rel}"):
            original = _restore_target(root, rel)
            _makedirs_logged(original.parent, root)
            _move(Path(full), original)
            log.info("RESTORE %s <- %s (unrecorded backup)", original, full)
            _prune_empty_dirs(Path(full).parent, backup_root)
    resolver = CaseResolver(root)
    for path in report.missing_dirs:
        if (root / path).is_dir() or _case_variant(resolver, path):
            changed(path)
        elif act(f"forget missing folder {path}"):
            manifest.forget_dir(game_id, path)
    superseded = _superseded_dir(game_id)
    for full in report.temp_leftovers:
        name = os.path.basename(full)
        if not name.startswith(TEMP_PREFIX) or not os.path.lexists(full) or (
                os.path.isdir(full) and not os.path.islink(full)):
            changed(full)
        elif act(f"move {full} (left by an interrupted move) to {superseded}"):
            destination = superseded / "interrupted-moves" / name
            _makedirs_logged(destination.parent, paths.data_dir())
            _move(Path(full), destination)
            log.info("MOVE %s -> %s (left by an interrupted move)", full, destination)
    # Interrupted links first: a half-made link at an original's path makes
    # that original look held until it is gone.
    for entry in report.interrupted:
        if entry.action == "link" and entry.path and entry.source:
            abs_path, staged = root / entry.path, Path(entry.source)
            ours = fold_path(entry.path) not in manifest.deployed(game_id) and _is_our_link(abs_path, staged)
            if ours and act(f"remove half-made link {entry.path} from an interrupted run"):
                os.unlink(abs_path)
                log.info("UNLINK %s (half-made link)", abs_path)
        elif entry.action == "mkdir" and entry.path:
            # The folder may have been made just before the app died: record it
            # so it is removed (only ever when empty) once nothing needs it.
            abs_path = root / entry.path
            if (abs_path.is_dir() and not abs_path.is_symlink()
                    and fold_path(entry.path) not in manifest.created_dirs(game_id)
                    and act(f"record folder {entry.path} made by an interrupted run")):
                manifest.record_dir(game_id, entry.path)
        if act(f"close interrupted {entry.action} of {entry.path or '?'}"):
            manifest.finish_journal(entry.id, "abandoned")
    deployed, unfinished_links = manifest.deployed(game_id), _unfinished_links(manifest, game_id)
    for backup in report.held_backups:
        key = fold_path(backup.path)
        current = manifest.backups(game_id).get(key)
        place = _restore_target(root, backup.path)
        if (current is None or not os.path.lexists(current.backup) or not os.path.lexists(place)
                or _app_file_at(place, deployed.get(key), unfinished_links.get(key))):
            changed(backup.path)
        elif _same_content(place, Path(current.backup)):
            if act(f"delete the spare copy {current.backup}: the same original is back at {backup.path}"):
                os.unlink(current.backup)
                log.info("DELETE %s (spare copy: the original is back at %s)", current.backup, place)
                manifest.forget_backup(game_id, backup.path)
                _prune_empty_dirs(Path(current.backup).parent, paths.backups_dir(game_id))
        elif not release_held:
            actions.append(f"keep the original of {backup.path} in {current.backup}: another file is at its path "
                           "(delete that file and purge to get the original back, or release it with "
                           "--release-held)")
        elif act(f"move the old original of {backup.path} to {superseded} and stop tracking it"):
            _supersede(manifest, game_id, current, superseded)
    for backup in report.restorable:
        actions.append(f"the original {backup.path} goes back with the next purge (or deploy)")
    return actions
