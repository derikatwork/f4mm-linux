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
import logging
import os
import shutil
import stat
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
OP_KINDS = ("unlink", "forget", "restore", "mkdir", "backup", "link", "rmdir")
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


def plan_sync(manifest: Manifest, target: GameTarget, *, purge: bool = False) -> SyncPlan:
    """Work out how to bring the game folder to the desired state (every
    enabled mod deployed, or with ``purge`` nothing at all). Reads the disk,
    changes nothing."""
    game_id = target.game_id
    root = Path(target.root)
    state = manifest.game_state(game_id)
    if state is not None and not _same_root(Path(state.root), root) and manifest.has_footprint(game_id):
        if purge:
            root = Path(state.root)
        else:
            raise DeployError(
                f"mods were deployed into {state.root}, but the game is now at {root}. Run "
                f"'bethmm purge --game {game_id}' (it uses the old folder) before deploying to the new one."
            )
    if not root.is_dir():
        raise DeployError(f"game folder {root} does not exist")

    plan = SyncPlan(game_id, root, target.link_mode, purge=purge)
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
    unfinished = manifest.unfinished_journal(game_id)
    if unfinished:
        plan.warnings.append(f"a previous run was interrupted ({len(unfinished)} unfinished operations); "
                             "run 'bethmm verify --repair' to clean up")
    interrupted_links = {fold_path(e.path): e for e in unfinished if e.action == "link" and e.path and e.source}

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
        if status == "intact" and want is not None and _satisfies(record, want, target.link_mode):
            satisfied.add(key)
            final[key] = record.path
            continue
        if status == "intact":
            removals.append(Op("unlink", record.path, record.mod_id, record.source))
            freed.add(key)
        elif status == "missing":
            removals.append(Op("forget", record.path, record.mod_id, record.source, "the link was already gone"))
            freed.add(key)
        else:
            removals.append(Op("forget", record.path, record.mod_id, record.source, FOREIGN_NOTE))
            foreign[key] = record.path
            plan.warnings.append(f"{record.path} was replaced by something the app didn't create; it is left alone")
    for key, path in sorted(manifest.left_alone(game_id).items()):
        if key in deployed:
            continue
        if os.path.lexists(root / path) and not _points_into_mods(root / path):
            foreign[key] = path
        else:
            removals.append(Op("forget", path, None, None, LEFT_ALONE_GONE_NOTE))

    resolver = CaseResolver(root)
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
        # (Something at the path in another spelling also counts as being in the way.)
        if key in deployed or not resolver.resolve(backup.path).exists_on_disk:
            restores.append(Op("restore", backup.path, None, backup.backup))
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
            leftover = interrupted_links.get(key)
            if _is_link_to(abs_path, staged, target.link_mode):
                note = "already in place (left by an interrupted run); recording it"
            elif leftover is not None and _is_our_link(abs_path, Path(leftover.source)):
                # Another link the app was making when it died: provably ours, so it goes.
                removals.append(Op("unlink", path, None, leftover.source, LEFTOVER_NOTE))
                interrupted_links.pop(key)
            elif _points_into_mods(abs_path):
                plan.blocked.append((path, "an unrecorded link into the app's mods folder is in the way; "
                                           "run 'bethmm verify --repair'"))
                kept.append(path)
                continue
            elif key in backups and key not in stale:
                plan.blocked.append((path, f"a backup of the original already exists ({backups[key].backup}) "
                                           "and the game folder has another copy; run 'bethmm verify'"))
                continue
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

    # Unrecorded links an interrupted run made that nothing wants any more.
    for key, entry in sorted(interrupted_links.items()):
        if key in deployed or key in final or key in desired:
            continue
        if _is_our_link(root / entry.path, Path(entry.source)):
            removals.append(Op("unlink", entry.path, None, entry.source, LEFTOVER_NOTE))

    # Folders the app created that no remaining link needs, deepest first.
    needed: set[str] = set()
    for p in [*final.values(), *kept]:
        parent = PurePosixPath(p).parent
        if parent.parts:  # not a file directly in the game root
            needed.update(_prefixes(fold_path(parent)))
    rmdirs = [
        Op("rmdir", path)
        for dkey, path in sorted(created.items(), key=lambda item: (-item[0].count("/"), item[0]))
        if dkey not in needed
    ]

    plan.ops = removals + restores + additions + rmdirs
    return plan


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
    deployed = manifest.deployed(plan.game_id)
    backups = manifest.backups(plan.game_id)
    for op in plan.ops:
        entry = manifest.journal(plan.game_id, op.kind, op.path, op.source, op.note or None, status="intent")
        try:
            note = _run(op, plan, manifest, deployed, backups)
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
    return result


class _Skip(Exception):
    """An op that would be unsafe now (the disk changed since planning)."""


def _run(op: Op, plan: SyncPlan, manifest: Manifest, deployed: dict[str, DeployedFile],
         backups: dict[str, BackupRecord]) -> str | None:
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
        resolved = CaseResolver(root).resolve(op.path)
        if os.path.lexists(abs_path) or resolved.exists_on_disk or resolved.type_conflict:
            raise _Skip(f"something is in the way; the original stays in {backup}")
        abs_path = root / resolved.rel  # into the folders as they are spelled now
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        _move(backup, abs_path)
        with manifest.transaction():
            manifest.forget_backup(game_id, op.path)
        backups.pop(key, None)
        log.info("RESTORE %s <- %s", abs_path, backup)
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
        destination.parent.mkdir(parents=True, exist_ok=True)
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
            raise _Skip(f"the staged file {source} is missing")
        elif plan.link_mode == "symlink":
            os.symlink(str(source), abs_path)
        else:
            os.link(source, abs_path)
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
            with manifest.transaction():
                manifest.forget_dir(game_id, op.path)
            log.info("KEEP %s (contains files the app didn't create)", abs_path)
            return f"kept the folder {op.path}: it contains files the app didn't create"
        with manifest.transaction():
            manifest.forget_dir(game_id, op.path)
        log.info("RMDIR %s", abs_path)
        return None

    raise ValueError(f"unknown op kind {op.kind!r}")


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
    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst):
                raise OSError(errno.EIO, f"copy of {src} is incomplete")
        os.unlink(src)
    except BaseException:
        # *dst* did not exist before, so a copy there is our own; drop it while
        # the source is still in place.
        if os.path.lexists(src) and os.path.lexists(dst):
            os.unlink(dst)
        raise


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
        current = current.parent


# ---------------------------------------------------------------- high level


def deploy(manifest: Manifest, target: GameTarget, *, dry_run: bool = False) -> SyncResult:
    """Bring the game folder in line with the enabled mods (and mark the game deployed)."""
    check_link_mode(target)
    return apply_plan(manifest, plan_sync(manifest, target), dry_run=dry_run)


def purge(manifest: Manifest, target: GameTarget, *, dry_run: bool = False) -> SyncResult:
    """Remove everything the app put in the game folder and restore the
    originals. Mods stay installed; ``deploy`` puts them back."""
    return apply_plan(manifest, plan_sync(manifest, target, purge=True), dry_run=dry_run)


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

    @property
    def ok(self) -> bool:
        return not (self.missing or self.foreign or self.orphan_links or self.missing_backups
                    or self.orphan_backups or self.missing_dirs or self.interrupted)


def verify(manifest: Manifest, target: GameTarget) -> VerifyReport:
    """Compare the manifest with the disk. Read-only."""
    game_id = target.game_id
    state = manifest.game_state(game_id)
    root = Path(state.root) if state is not None else Path(target.root)
    report = VerifyReport(game_id, root)
    deployed = manifest.deployed(game_id)
    for record in deployed.values():
        status = _inspect(root / record.path, record)
        if status == "missing":
            report.missing.append(record)
        elif status == "foreign":
            report.foreign.append(record)

    known = set(deployed)
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if fold_path(rel) not in known and _points_into_mods(full):
                    report.orphan_links.append(rel)

    backups = manifest.backups(game_id)
    for backup in backups.values():
        if not os.path.lexists(backup.backup):
            report.missing_backups.append(backup)
    recorded = {os.path.realpath(b.backup) for b in backups.values()}
    backup_root = paths.backups_dir(game_id)
    if backup_root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(backup_root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.realpath(full) not in recorded:
                    report.orphan_backups.append(full)

    for path in manifest.created_dirs(game_id).values():
        if not (root / path).is_dir():
            report.missing_dirs.append(path)

    report.left_alone = sorted(path for path in manifest.left_alone(game_id).values()
                               if os.path.lexists(root / path))
    report.interrupted = manifest.unfinished_journal(game_id)
    report.orphan_links.sort()
    report.orphan_backups.sort()
    return report


def repair(manifest: Manifest, target: GameTarget, report: VerifyReport, *, dry_run: bool = False) -> list[str]:
    """Fix what :func:`verify` found, as far as that is safe. Returns what was
    (or would be) done. Afterwards a ``deploy`` recreates missing links."""
    game_id, root = report.game_id, report.root
    actions: list[str] = []

    def act(text: str) -> bool:
        actions.append(text)
        log.info("REPAIR %s%s", text, " (dry run)" if dry_run else "")
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
    for backup in report.missing_backups:
        if os.path.lexists(backup.backup):
            changed(backup.backup)
        elif act(f"forget backup record of {backup.path} (the backup file {backup.backup} is gone)"):
            manifest.forget_backup(game_id, backup.path)
    backup_root = paths.backups_dir(game_id)
    for full in report.orphan_backups:
        rel = os.path.relpath(full, backup_root).replace(os.sep, "/")
        original = root / rel
        recorded = {os.path.realpath(b.backup) for b in manifest.backups(game_id).values()}
        if not os.path.lexists(full) or os.path.realpath(full) in recorded:
            changed(full)
        elif os.path.lexists(original):
            actions.append(f"keep unrecorded backup {full}: {rel} exists in the game folder")
        elif act(f"restore unrecorded backup {full} -> {rel}"):
            original.parent.mkdir(parents=True, exist_ok=True)
            _move(Path(full), original)
            _prune_empty_dirs(Path(full).parent, backup_root)
    for path in report.missing_dirs:
        if (root / path).is_dir():
            changed(path)
        elif act(f"forget missing folder {path}"):
            manifest.forget_dir(game_id, path)
    for entry in report.interrupted:
        if entry.action == "link" and entry.path and entry.source:
            abs_path, staged = root / entry.path, Path(entry.source)
            ours = fold_path(entry.path) not in manifest.deployed(game_id) and _is_our_link(abs_path, staged)
            if ours and act(f"remove half-made link {entry.path} from an interrupted run"):
                os.unlink(abs_path)
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
    return actions
