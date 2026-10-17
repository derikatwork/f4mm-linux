"""Deploy mods into a game folder by linking staged files, and undo it.

The game folder only ever changes through :func:`apply_plan`, which carries
out a list of :class:`Op` computed by :func:`plan_sync`:

* ``unlink``  remove a link the app created (only if it is still exactly that
  link: same symlink target, or for hardlinks the same inode);
* ``forget``  drop a manifest record without touching the disk (the link is
  already gone, or something else has replaced it -- that file is left alone);
* ``restore`` move a backed-up original game file back into place;
* ``mkdir``   create a missing folder (recorded so it can be removed later);
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
  in the list) wins;
* each operation is journalled as an intent before it runs and marked done
  after its manifest record is saved, so an interrupted run can be cleaned up
  by :func:`repair`;
* ``dry_run`` computes and returns the same plan without touching anything.

Every file operation is also written to the log.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
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


def desired_files(manifest: Manifest, game_id: str) -> tuple[dict[str, tuple[ModRecord, ModFile]], dict[str, list[int]]]:
    """What should be deployed: folded destination -> (winning mod, file), and
    folded destination -> every enabled mod providing it, lowest priority first."""
    desired: dict[str, tuple[ModRecord, ModFile]] = {}
    providers: dict[str, list[int]] = {}
    for mod in manifest.mods(game_id):
        if not mod.enabled:
            continue
        for f in manifest.mod_files(mod.id):
            key = fold_path(f.dest)
            desired[key] = (mod, f)
            providers.setdefault(key, []).append(mod.id)
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
    if (st.st_ino, st.st_dev) == (record.inode, record.dev) and not os.path.islink(abs_path):
        return "intact"
    return "foreign"


def _same_dev(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def check_link_mode(target: GameTarget) -> None:
    """Hardlinks only work when the staged files and the game folder share a
    filesystem; raise :class:`DeployError` explaining that otherwise."""
    if target.link_mode != "hardlink":
        return
    staging = paths.mods_dir(target.game_id)
    staging.mkdir(parents=True, exist_ok=True)
    if not _same_dev(staging, target.root):
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
    if state is not None and Path(state.root) != root and manifest.has_footprint(game_id):
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
    desired, providers = ({}, {}) if purge else desired_files(manifest, game_id)
    plan.conflicts = {k: v for k, v in providers.items() if len(v) > 1}
    deployed = manifest.deployed(game_id)
    backups = manifest.backups(game_id)
    created = manifest.created_dirs(game_id)

    removals: list[Op] = []
    restores: list[Op] = []
    satisfied: set[str] = set()
    freed: set[str] = set()      # keys whose path will be empty after removals
    foreign: set[str] = set()    # keys whose path holds something we don't own
    final: dict[str, str] = {}   # key -> path of every link present after the plan

    for key, record in sorted(deployed.items()):
        status = _inspect(root / record.path, record)
        want = desired.get(key)
        if (
            status == "intact"
            and want is not None
            and record.source == str(staged_file(want[0], want[1].source))
            and record.link_type == target.link_mode
            and record.mod_id == want[0].id
        ):
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
            removals.append(Op("forget", record.path, record.mod_id, record.source,
                               "replaced by a file the app didn't create; left alone"))
            foreign.add(key)
            plan.warnings.append(f"{record.path} was replaced by something the app didn't create; it is left alone")

    for key, backup in sorted(backups.items()):
        if key in desired and key not in foreign:
            continue  # the path stays occupied by a mod file; keep the original backed up
        if key in foreign:
            plan.warnings.append(f"the original {backup.path} stays in {backup.backup} because the path is occupied")
            continue
        if key in deployed or not os.path.lexists(root / backup.path):
            restores.append(Op("restore", backup.path, None, backup.backup))
        else:
            plan.warnings.append(
                f"the original {backup.path} stays in {backup.backup}: something else now exists at that path"
            )

    resolver = CaseResolver(root)
    additions: list[Op] = []
    planned_dirs: dict[str, str] = {}
    for key, (mod, f) in sorted(desired.items(), key=lambda item: item[0]):
        if key in satisfied:
            continue
        if key in foreign:
            plan.blocked.append((deployed[key].path, "replaced by a file the app didn't create"))
            continue
        staged = staged_file(mod, f.source)
        if not staged.is_file():
            plan.blocked.append((f.dest, f"staged file {staged} is missing; reinstall '{mod.name}'"))
            continue
        if key in deployed:
            rel = PurePosixPath(deployed[key].path)
            resolved = resolver.resolve(rel, plan=True)
            new_dirs = resolved.new_dirs
        else:
            resolved = resolver.resolve(f.dest, plan=True)
            if resolved.type_conflict:
                plan.blocked.append((f.dest, "a folder (or file) of the other kind already exists there"))
                continue
            rel, new_dirs = resolved.rel, resolved.new_dirs
            plan.warnings.extend(w for w in resolved.ambiguous if w not in plan.warnings)
        for d in new_dirs:
            dkey = fold_path(d)
            if dkey not in planned_dirs and dkey not in created:
                planned_dirs[dkey] = str(d)
                additions.append(Op("mkdir", str(d), mod.id))
        path = str(rel)
        if resolved.exists_on_disk and key not in freed:
            if key in backups:
                plan.blocked.append((path, f"a backup of the original already exists ({backups[key].backup}) "
                                           "and the game folder has another copy; run 'bethmm verify'"))
                continue
            additions.append(Op("backup", path, None, str(_backup_path(game_id, path)), "original game file"))
        additions.append(Op("link", path, mod.id, str(staged)))
        final[key] = path

    # Folders the app created that no remaining link needs, deepest first.
    needed = {fold_path(PurePosixPath(p).parent) for p in final.values()}
    needed = {prefix for key in needed for prefix in _prefixes(key)}
    rmdirs = [
        Op("rmdir", path)
        for dkey, path in sorted(created.items(), key=lambda item: (-item[0].count("/"), item[0]))
        if dkey not in needed
    ]

    plan.ops = removals + restores + additions + rmdirs
    return plan


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
        log.info("FORGET %s (%s)", abs_path, op.note)
        return None

    if op.kind == "unlink":
        record = deployed.get(key)
        if record is None or _inspect(abs_path, record) != "intact":
            with manifest.transaction():
                manifest.forget_link(game_id, op.path)
            raise _Skip("it changed since the plan was made; left alone")
        os.unlink(abs_path)
        with manifest.transaction():
            manifest.forget_link(game_id, op.path)
        log.info("UNLINK %s", abs_path)
        return None

    if op.kind == "restore":
        backup = Path(op.source or "")
        if os.path.lexists(abs_path):
            raise _Skip(f"something is in the way; the original stays in {backup}")
        if not os.path.lexists(backup):
            with manifest.transaction():
                manifest.forget_backup(game_id, op.path)
            raise _Skip(f"the backup {backup} is gone")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        _move(backup, abs_path)
        with manifest.transaction():
            manifest.forget_backup(game_id, op.path)
        log.info("RESTORE %s <- %s", abs_path, backup)
        _prune_empty_dirs(backup.parent, paths.backups_dir(game_id))
        return None

    if op.kind == "mkdir":
        if os.path.lexists(abs_path):
            if abs_path.is_dir() and not abs_path.is_symlink():
                return None  # already there (not ours, so not recorded)
            raise _Skip("a file with that name is in the way")
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
        destination = _backup_path(game_id, op.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _move(abs_path, destination)
        with manifest.transaction():
            manifest.record_backup(BackupRecord(game_id, op.path, str(destination)))
        backups[key] = BackupRecord(game_id, op.path, str(destination))
        log.info("BACKUP %s -> %s", abs_path, destination)
        return None

    if op.kind == "link":
        source = Path(op.source or "")
        if os.path.lexists(abs_path):
            raise _Skip("something is in the way")
        if not source.is_file():
            raise _Skip(f"the staged file {source} is missing")
        if plan.link_mode == "symlink":
            os.symlink(str(source), abs_path)
            inode = dev = None
        else:
            os.link(source, abs_path)
            st = os.lstat(abs_path)
            inode, dev = st.st_ino, st.st_dev
        record = DeployedFile(game_id, op.path, op.mod_id or 0, str(source), plan.link_mode, inode, dev)
        with manifest.transaction():
            manifest.record_link(record)
        deployed[key] = record
        log.info("LINK %s -> %s (%s)", abs_path, source, plan.link_mode)
        return None

    if op.kind == "rmdir":
        if not os.path.lexists(abs_path):
            with manifest.transaction():
                manifest.forget_dir(game_id, op.path)
            return None
        try:
            os.rmdir(abs_path)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            with manifest.transaction():
                manifest.forget_dir(game_id, op.path)
            log.info("KEEP %s (contains files the app didn't create)", abs_path)
            return f"kept the folder {op.path}: it contains files the app didn't create"
        with manifest.transaction():
            manifest.forget_dir(game_id, op.path)
        log.info("RMDIR %s", abs_path)
        return None

    raise ValueError(f"unknown op kind {op.kind!r}")


def _move(src: Path, dst: Path) -> None:
    """Rename, falling back to copy + delete across filesystems."""
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst):
                os.unlink(dst)
                raise OSError(errno.EIO, f"copy of {src} is incomplete")
        os.unlink(src)


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

    mods_root = os.path.realpath(paths.mods_dir())
    known = set(deployed)
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if fold_path(rel) in known:
                    continue
                try:
                    target_path = os.path.realpath(os.path.join(dirpath, os.readlink(full)))
                except OSError:
                    continue
                if target_path == mods_root or target_path.startswith(mods_root + os.sep):
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

    for record in report.missing:
        if act(f"forget missing link {record.path}"):
            manifest.forget_link(game_id, record.path)
    for record in report.foreign:
        if act(f"forget {record.path} (replaced by another file, which is left alone)"):
            manifest.forget_link(game_id, record.path)
    for rel in report.orphan_links:
        if act(f"remove stray link {rel} (points into the app's mods folder)"):
            os.unlink(root / rel)
    for backup in report.missing_backups:
        if act(f"forget backup record of {backup.path} (the backup file {backup.backup} is gone)"):
            manifest.forget_backup(game_id, backup.path)
    backup_root = paths.backups_dir(game_id)
    for full in report.orphan_backups:
        rel = os.path.relpath(full, backup_root).replace(os.sep, "/")
        original = root / rel
        if os.path.lexists(original):
            actions.append(f"keep unrecorded backup {full}: {rel} exists in the game folder")
        elif act(f"restore unrecorded backup {full} -> {rel}"):
            original.parent.mkdir(parents=True, exist_ok=True)
            _move(Path(full), original)
            _prune_empty_dirs(Path(full).parent, backup_root)
    for path in report.missing_dirs:
        if act(f"forget missing folder {path}"):
            manifest.forget_dir(game_id, path)
    for entry in report.interrupted:
        if entry.action == "link" and entry.path and entry.source:
            abs_path = root / entry.path
            staged = Path(entry.source)
            ours = (fold_path(entry.path) not in manifest.deployed(game_id) and os.path.lexists(abs_path)
                    and staged.exists() and _same_file(abs_path, staged))
            if ours and act(f"remove half-made link {entry.path} from an interrupted run"):
                os.unlink(abs_path)
        if act(f"close interrupted {entry.action} of {entry.path or '?'}"):
            manifest.finish_journal(entry.id, "abandoned")
    return actions


def _same_file(a: Path, b: Path) -> bool:
    try:
        if os.path.islink(a):
            return os.path.realpath(a) == os.path.realpath(b)
        sa, sb = os.lstat(a), os.stat(b)
        return (sa.st_ino, sa.st_dev) == (sb.st_ino, sb.st_dev)
    except OSError:
        return False
