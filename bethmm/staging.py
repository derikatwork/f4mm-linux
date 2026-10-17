"""Turn a downloaded archive (or an already-extracted folder) into an installed mod.

Flow, driven by the CLI (and later the GUI)::

    prepared = prepare(source, installed_games=..., roots=..., target=...)
    try:
        ... show prepared.plan, let the user confirm game and name ...
        result = commit(prepared, manifest, game_id, name=..., replace=...)
        ... deploy.refresh(...); then remove_staging(result.old_folder) if set ...
    finally:
        discard(prepared)

``prepare`` extracts an archive into ``paths.tmp_dir()/install-XXXX/x`` (or
copies a folder there, without following symlinks, so the user's folder is
never touched) and runs the placement rules on it: ``placement.analyze_files``
or, when *roots*/*target* are given, ``placement.plan_for_roots``.

``commit`` moves the extracted files to ``paths.mods_dir(game_id)/<folder>/``
(a rename -- tmp and mods live in the same data folder -- falling back to a
move across filesystems) and records the mod in the manifest with one
``ModFile`` per plan mapping (``source`` relative to the staging folder,
``dest`` relative to the game root). Files the plan skipped (readmes ...) stay
in the staging folder but are never deployed.

With *replace* (an existing ``ModRecord`` of the same game) the existing
record is updated in place -- same priority, same enabled state, new folder,
files and details -- and the old staging folder is returned in
``CommitResult.old_folder`` so the caller can delete it *after* redeploying
(the old links still point into it until then).

Staging folder names are derived from the mod name: path separators, NUL and
control characters replaced, leading dots and surrounding whitespace stripped,
at most 100 characters, never "." or "..", unique within the game's mods
folder case-insensitively (" (2)", " (3)" ... appended).

Only ``discard`` and ``remove_staging`` delete anything, and only inside the
app's own tmp and mods folders (checked with realpath; anything else raises
``StagingError``). The one exception is ``commit``'s move across filesystems,
which removes the extracted files once copied, or its own unfinished copy.
Every file operation is logged.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from bethmm import archive, paths, placement
from bethmm.manifest import Manifest, ManifestError, ModFile, ModRecord
from bethmm.nexus import parse_nexus_filename
from bethmm.pathresolve import fold_path
from bethmm.placement import PlacementPlan, Target

log = logging.getLogger(__name__)

MAX_FOLDER_CHARS = 100
#: ext4 and most Linux filesystems allow 255 bytes per name; leave room for " (NNN)".
MAX_FOLDER_BYTES = 200
#: Used when nothing usable is left of a mod name.
FALLBACK_NAME = "mod"

#: Extensions stripped from an archive's file name for the suggested mod name.
_ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".7z", ".zip", ".rar", ".tar", ".gz", ".bz2", ".xz")
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)$")
_WORKDIR_PREFIX = "install-"


class StagingError(Exception):
    pass


@dataclass
class Prepared:
    #: The archive or folder the user picked.
    source: Path
    #: ``tmp/install-XXXX`` -- the app's own temporary folder; ``discard`` removes it.
    workdir: Path
    #: ``workdir/x``: the extracted files.
    files_dir: Path
    #: Regular files in ``files_dir`` (relative POSIX paths).
    files: list[str]
    plan: PlacementPlan
    warnings: list[str] = field(default_factory=list)


@dataclass
class CommitResult:
    mod: ModRecord
    #: Set when an existing mod was replaced: its previous staging folder,
    #: to delete with ``remove_staging`` once the game has been redeployed.
    old_folder: Path | None = None
    replaced: bool = False


# ---------------------------------------------------------------- prepare


def prepare(
    source: str | Path,
    *,
    sevenzip: str | None = None,
    installed_games: Sequence[str] = (),
    roots: Sequence[str] | None = None,
    target: Target | None = None,
) -> Prepared:
    """Extract/copy *source* into a new temp folder and plan where its files go.

    Raises ``StagingError`` for a missing source, ``archive.ArchiveError`` /
    ``SevenZipMissing`` for archive problems and ``ValueError`` from
    ``plan_for_roots`` for an invalid root/target choice. The temp folder is
    removed again if anything fails.

    A *target* without *roots* applies to the root the placement rules found
    (after stepping into wrapper folders).
    """
    source = Path(source).expanduser().absolute()
    if not source.exists():
        raise StagingError(f"{source} does not exist")
    is_folder = source.is_dir()
    if not is_folder and not source.is_file():
        raise StagingError(f"{source} is neither a file nor a folder")

    tmp = paths.tmp_dir()
    _mkdir(tmp)
    if is_folder and _inside_or_same(os.path.realpath(tmp), os.path.realpath(source)):
        raise StagingError(f"can't install {source}: it contains the app's own data folder")
    workdir = Path(tempfile.mkdtemp(prefix=_WORKDIR_PREFIX, dir=tmp))
    log.info("MKDIR %s", workdir)
    files_dir = workdir / "x"
    try:
        warnings: list[str] = []
        if is_folder:
            warnings += _copy_folder(source, files_dir)
            # Like an archive's: "Data\x.esp" becomes Data/x.esp, as the placement rules read it.
            archive._split_backslash_names(files_dir, warnings)
        else:
            warnings += archive.extract_archive(source, files_dir, sevenzip).warnings
        files, reader, scan_warnings = placement.scan_directory(files_dir)
        warnings += scan_warnings
        options = dict(header_reader=reader, installed_games=installed_games, archive_name=source.name)
        if roots is None and target is None:
            plan = placement.analyze_files(files, **options)
        else:
            if roots is None:
                roots = [placement.analyze_files(files).root]
            plan = placement.plan_for_roots(files, roots, target, **options)
    except BaseException:
        try:
            _remove_tree(workdir, paths.tmp_dir(), 1, "the app's temporary folder")
        except (OSError, StagingError) as exc:
            log.error("could not remove the temporary folder %s: %s", workdir, exc)
        raise
    return Prepared(source, workdir, files_dir, sorted(files), plan, warnings)


def _copy_folder(source: Path, dest: Path) -> list[str]:
    """Copy the folders and regular files below *source* into *dest* without
    following symlinks; symlinks and special files are skipped with a warning."""
    log.info("COPY %s -> %s", source, dest)
    warnings: list[str] = []
    count = 0
    dest.mkdir()
    pending = [(source, dest, "")]
    try:
        while pending:
            src_dir, dst_dir, prefix = pending.pop()
            with os.scandir(src_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
            for entry in entries:
                rel = prefix + entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    warnings.append(f"skipped symlink {rel} in the mod folder")
                    log.warning("skipped symlink %s in %s", rel, source)
                elif stat.S_ISDIR(mode):
                    target = dst_dir / entry.name
                    target.mkdir()
                    pending.append((Path(entry.path), target, rel + "/"))
                elif stat.S_ISREG(mode):
                    shutil.copy2(entry.path, dst_dir / entry.name, follow_symlinks=False)
                    count += 1
                else:
                    warnings.append(f"skipped special file {rel} in the mod folder")
                    log.warning("skipped special file %s in %s", rel, source)
    except OSError as exc:
        where = exc.filename or source
        raise StagingError(f"could not copy {where}: {exc.strerror or exc}") from exc
    log.info("COPIED %d files from %s", count, source)
    return warnings


# ---------------------------------------------------------------- names


def suggested_name(prepared: Prepared) -> str:
    """Display name for the mod: the Nexus name from the archive file name,
    else the archive/folder name without extension."""
    nexus = prepared.plan.nexus
    if nexus is not None and nexus.is_nexus and nexus.display_name.strip():
        return _clean_text(nexus.display_name) or FALLBACK_NAME
    name = prepared.source.name.strip()
    low = name.lower()
    for ext in _ARCHIVE_SUFFIXES:
        if low.endswith(ext) and len(name) > len(ext):
            name = _DUPLICATE_SUFFIX.sub("", name[: -len(ext)])
            break
    return _clean_text(name) or FALLBACK_NAME


def _clean_text(text: str) -> str:
    """*text* safe to store and print: control characters become spaces,
    undecodable bytes (lone surrogates) U+FFFD, whitespace collapsed."""
    out = []
    for c in text:
        if 0xD800 <= ord(c) <= 0xDFFF:
            out.append("\ufffd")
        elif unicodedata.category(c) == "Cc":
            out.append(" ")
        else:
            out.append(c)
    return " ".join("".join(out).split())


def _truncate(name: str, max_chars: int, max_bytes: int) -> str:
    name = name[:max_chars]
    while len(name.encode("utf-8")) > max_bytes:
        name = name[:-1]
    return name.rstrip()


def _sanitize_folder(name: str) -> str:
    out = []
    for c in name.strip():
        if c in "/\\" or c == "\0" or unicodedata.category(c) == "Cc" or 0xD800 <= ord(c) <= 0xDFFF:
            out.append("_")
        else:
            out.append(c)
    text = "".join(out)
    while True:
        stripped = text.strip().lstrip(".")
        if stripped == text:
            break
        text = stripped
    return _truncate(text, MAX_FOLDER_CHARS, MAX_FOLDER_BYTES) or FALLBACK_NAME


def folder_name(name: str, existing: Sequence[str] = ()) -> str:
    """A safe staging folder name for *name*, unique against *existing*
    (case-insensitively). See module docstring for the rules."""
    base = _sanitize_folder(name)
    taken: set[str] = set()
    for e in existing:
        taken.add(e.casefold())
        taken.add(e.upper())

    def free(candidate: str) -> bool:
        return candidate.casefold() not in taken and candidate.upper() not in taken

    if free(base):
        return base
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = _truncate(base, MAX_FOLDER_CHARS - len(suffix), MAX_FOLDER_BYTES - len(suffix)) + suffix
        if free(candidate):
            return candidate
        n += 1


def find_existing(manifest: Manifest, game_id: str, prepared: Prepared, name: str | None = None) -> ModRecord | None:
    """An installed mod this one would update, or None:

    * a mod with the same name (case-insensitive), unless both have a Nexus
      mod ID and they differ ("2K" or "Main File" of two unrelated mods) --
      with several, the one with the same Nexus mod ID;
    * else a mod with the same Nexus mod ID whose original download had the
      same Nexus file name, ignoring version numbers in it ("Cool Mod 1.2" ->
      "Cool Mod 1.3"). A mod the user renamed still gets updated, while
      another file from the same Nexus page (an optional patch, an add-on,
      "PART 2") is installed as a mod of its own.
    """
    mods = manifest.mods(game_id)
    wanted = _clean_text(name if name is not None else suggested_name(prepared)).casefold()
    same_name = [m for m in mods if _clean_text(m.name).casefold() == wanted]
    nexus = prepared.plan.nexus
    is_nexus = nexus is not None and nexus.is_nexus
    if is_nexus:
        same_name = sorted((m for m in same_name if m.nexus_id in (nexus.mod_id, None)),
                           key=lambda m: m.nexus_id != nexus.mod_id)
    if same_name:
        return same_name[0]
    if is_nexus:
        for m in mods:
            if m.nexus_id != nexus.mod_id or not m.source_archive:
                continue
            original = parse_nexus_filename(Path(m.source_archive).name)
            versions = (nexus.version, original.version)
            if original.mod_id == nexus.mod_id and \
                    base_name(original.display_name, *versions) == base_name(nexus.display_name, *versions):
                return m
    return None


#: A version number in a Nexus file name: "1.2", "10.0.1", "5_2", "v3", "v1.4b".
_VERSION_IN_NAME = re.compile(r"(?<![A-Za-z0-9])(?:v\d+(?:[._]\d+)*|\d+(?:[._]\d+)+)[a-z]?(?![A-Za-z0-9])", re.I)


def base_name(name: str, *versions: str | None) -> str:
    """*name* without version numbers (also the given *versions*, however
    they are written in it), folded: what stays the same from one version of
    a Nexus file to the next. Plain numbers ("PART 2") are kept."""
    text = _clean_text(name)
    for version in versions:
        m = re.fullmatch(r"v?(\d+(?:[._-]\d+)+)([A-Za-z]*)", (version or "").strip())
        if m is None:
            continue
        digits = r"[ ._-]".join(re.escape(p) for p in re.split(r"[._-]", m.group(1)))
        suffix = rf"(?:[ ._-]?{re.escape(m.group(2))})?" if m.group(2) else ""
        text = re.sub(rf"(?<![A-Za-z0-9])v?{digits}{suffix}(?![A-Za-z0-9])", " ", text, flags=re.I)
    text = _VERSION_IN_NAME.sub(" ", text)
    return " ".join(w for w in re.split(r"[\s_]+|\s-\s|-$|^-", text) if w and w != "-").casefold()


# ---------------------------------------------------------------- retired folders

#: Marks a staging folder an update replaced while links still pointed into
#: it; 'verify --repair' deletes such folders once nothing uses them. Folders
#: in the mods folder without it are never deleted by the app.
RETIRED_MARKER = ".bethmm-retired"


def retire(folder: Path) -> None:
    """Mark *folder*, a mod's previous staging folder, as no longer needed."""
    _check_location(folder, paths.mods_dir(), 2, "a game's mods folder", "retire")
    marker = folder / RETIRED_MARKER
    marker.write_text("This folder held a previous version of a mod. The app deletes it once no link uses it.\n")
    log.info("WRITE %s", marker)


def is_retired(folder: Path) -> bool:
    return (folder / RETIRED_MARKER).is_file() and not (folder / RETIRED_MARKER).is_symlink()


# ---------------------------------------------------------------- commit


def commit(
    prepared: Prepared,
    manifest: Manifest,
    game_id: str,
    *,
    name: str | None = None,
    replace: ModRecord | None = None,
    enabled: bool | None = None,
) -> CommitResult:
    """Move the extracted files into the mods folder and record the mod.

    Raises ``StagingError`` when the plan has no mappings or its target is
    UNKNOWN (the user must choose a root/target first), when the plan is a
    FOMOD without mappings, when *replace* belongs to another game, or when
    ``prepared.files_dir`` is not an install folder made by ``prepare``. A
    failure or interruption before the manifest is saved moves the files back.
    The manifest's ``plan`` field stores ``{"root", "target", "rule",
    "wrapper_dirs", "skipped": <count>}`` from the PlacementPlan.

    Without *name* a new mod is called ``suggested_name(prepared)`` and a
    replaced mod keeps its name. Without *enabled* a new mod is enabled and a
    replaced mod keeps its state.
    """
    plan = prepared.plan
    if not plan.mappings:
        if plan.is_fomod:
            raise StagingError(
                "this mod is a FOMOD installer; choose which of its folders to install (and where) first"
            )
        raise StagingError("no files would be installed; choose the mod's root folder and target first")
    if plan.target is Target.UNKNOWN and not plan.is_fomod:
        raise StagingError("it is not clear where these files belong; choose Data or the game folder first")

    current: ModRecord | None = None
    if replace is not None:
        if replace.game_id != game_id:
            raise StagingError(f"{replace.name!r} is installed for {replace.game_id}, not {game_id}")
        try:
            current = manifest.mod(replace.id)
        except ManifestError as exc:
            raise StagingError(f"{replace.name!r} is no longer installed") from exc
        if current.game_id != game_id:
            raise StagingError(f"{current.name!r} is installed for {current.game_id}, not {game_id}")

    if name is not None:
        mod_name = _clean_text(name)
        if not mod_name:
            raise StagingError("the mod name is empty")
    else:
        mod_name = current.name if current is not None else suggested_name(prepared)

    # Only a folder prepare() made is ever moved (and later deleted with the mod).
    if not Path(os.path.abspath(prepared.files_dir)).parent.name.startswith(_WORKDIR_PREFIX):
        raise StagingError(f"refusing to install from {prepared.files_dir}: not one of the app's install folders")
    _check_location(prepared.files_dir, paths.tmp_dir(), 2, "the app's temporary folder", "install from")
    if not prepared.files_dir.is_dir():
        raise StagingError(f"{prepared.files_dir} is gone; this install was already committed or discarded")
    files = _checked_files(prepared)

    mods_root = paths.mods_dir(game_id)
    _mkdir(mods_root)
    existing = os.listdir(mods_root) + [m.folder for m in manifest.mods(game_id)]
    folder = folder_name(mod_name, existing)
    staging = mods_root / folder
    if os.path.lexists(staging):
        raise StagingError(f"{staging} already exists")

    nexus = plan.nexus if plan.nexus is not None and plan.nexus.is_nexus else None
    details = dict(
        version=nexus.version if nexus else None,
        nexus_id=nexus.mod_id if nexus else None,
        source_archive=_clean_path(prepared.source),
        plan={
            "root": plan.root,
            "target": plan.target.value,
            "rule": plan.rule.value,
            "wrapper_dirs": list(plan.wrapper_dirs),
            "skipped": len(plan.skipped),
        },
    )

    _move(prepared.files_dir, staging)
    try:
        # One transaction around the write and its read-back: an interruption
        # anywhere before it is saved rolls it back, and the files go back too.
        with manifest.transaction():
            if current is None:
                mod = manifest.add_mod(game_id, mod_name, folder, files, enabled=enabled is not False, **details)
                result = CommitResult(mod)
            else:
                if enabled is not None:
                    manifest.set_enabled(current.id, enabled)
                mod = manifest.update_mod(current.id, folder=folder, files=files, name=mod_name, **details)
                result = CommitResult(mod, old_folder=mods_root / current.folder, replaced=True)
    except BaseException:
        try:
            _move(staging, prepared.files_dir)
        except (OSError, StagingError) as exc:
            log.error("could not move %s back to %s: %s", staging, prepared.files_dir, exc)
        raise
    log.info("%s mod %r (#%d) for %s in %s with %d files", "UPDATED" if result.replaced else "INSTALLED",
             mod.name, mod.id, game_id, staging, len(files))
    return result


def _checked_files(prepared: Prepared) -> list[ModFile]:
    """The plan's mappings as ModFiles, refusing anything that isn't a regular
    file inside ``files_dir`` or a destination that could leave the game folder."""
    base = os.path.realpath(prepared.files_dir)
    files: list[ModFile] = []
    bad: list[str] = []
    for m in prepared.plan.mappings:
        try:
            fold_path(m.source)
            fold_path(m.dest)
            m.source.encode("utf-8")
            m.dest.encode("utf-8")
        except (ValueError, UnicodeEncodeError):
            bad.append(m.source)
            continue
        full = os.path.join(prepared.files_dir, m.source)
        try:
            regular = stat.S_ISREG(os.lstat(full).st_mode)
        except OSError:
            regular = False
        if not regular or not _inside(os.path.realpath(full), base):
            bad.append(m.source)
            continue
        files.append(ModFile(source=m.source, dest=m.dest))
    if bad:
        shown = ", ".join(repr(b) for b in bad[:5])
        more = f" and {len(bad) - 5} more" if len(bad) > 5 else ""
        raise StagingError(f"can't install these files (missing, not regular files or unusable names): {shown}{more}")
    return files


def _clean_path(path: Path) -> str:
    text = str(path)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = os.fsencode(text).decode("utf-8", errors="replace")
    return text


# ---------------------------------------------------------------- deleting


def discard(prepared: Prepared) -> None:
    """Remove what is left of the temp folder (safe to call twice).

    Meant for ``finally`` blocks: a disk error is logged, not raised (a
    leftover temp folder does no harm); a folder that is not one of the app's
    install folders still raises ``StagingError``.
    """
    if not prepared.workdir.name.startswith(_WORKDIR_PREFIX):
        raise StagingError(f"refusing to delete {prepared.workdir}: not one of the app's install folders")
    try:
        _remove_tree(prepared.workdir, paths.tmp_dir(), 1, "the app's temporary folder")
    except OSError as exc:
        log.error("could not remove the temporary folder %s: %s", prepared.workdir, exc)


def remove_staging(folder: str | Path) -> None:
    """Delete a mod's staging folder -- only ever inside ``paths.mods_dir()``."""
    _remove_tree(Path(folder), paths.mods_dir(), 2, "a game's mods folder")


def _check_location(path: Path, base: Path, depth: int, what: str, action: str = "delete") -> str:
    """The realpath of *path*, which must lie exactly *depth* levels below
    *base* both as written (abspath) and resolved (realpath of both), and must
    not itself be a symlink; StagingError otherwise."""
    if os.path.islink(path):
        raise StagingError(f"refusing to {action} {path}: it is a symlink")
    real = os.path.realpath(path)
    for candidate, root in ((os.path.abspath(path), os.path.abspath(base)), (real, os.path.realpath(base))):
        rel = os.path.relpath(candidate, root)
        parts = rel.split(os.sep)
        if os.path.isabs(rel) or rel == os.curdir or os.pardir in parts or len(parts) != depth:
            raise StagingError(f"refusing to {action} {path}: it is not a folder in {what} ({base})")
    return real


def _remove_tree(path: Path, base: Path, depth: int, what: str) -> None:
    """Delete the folder *path* after ``_check_location``. Missing folders are
    fine; anything else raises StagingError."""
    real = _check_location(path, base, depth, what)
    if not os.path.lexists(real):
        return
    if os.path.islink(real) or not os.path.isdir(real):
        raise StagingError(f"refusing to delete {path}: it is not a folder")
    _make_deletable(real)
    shutil.rmtree(real)
    log.info("DELETE %s", real)


def _make_deletable(top: str) -> None:
    """Give the owner rwx on every folder below *top* (archives can carry
    read-only folders, whose contents could not be removed otherwise)."""
    needed = stat.S_IRWXU

    def fix(dir_path: str) -> None:
        try:
            st = os.lstat(dir_path)
            if stat.S_ISDIR(st.st_mode) and st.st_mode & needed != needed:
                os.chmod(dir_path, stat.S_IMODE(st.st_mode) | needed)
        except OSError:
            pass

    fix(top)
    for dirpath, dirnames, _filenames in os.walk(top):
        for d in dirnames:
            fix(os.path.join(dirpath, d))


# ---------------------------------------------------------------- helpers


def _inside(path: str, base: str) -> bool:
    return path.startswith(base.rstrip(os.sep) + os.sep)


def _inside_or_same(path: str, base: str) -> bool:
    return path == base or _inside(path, base)


def _mkdir(path: Path) -> None:
    if path.is_dir():
        return
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for p in reversed(missing):
        log.info("MKDIR %s", p)


def _move(source: Path, dest: Path) -> None:
    """Rename the folder *source* to *dest*, which must not exist. Across
    filesystems *source* is copied into a new *dest* and removed only once the
    copy is complete; a failed copy is removed again, so no partial folder is
    left behind."""
    if os.path.lexists(dest):
        raise StagingError(f"can't move {source} to {dest}: it already exists")
    try:
        os.rename(source, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        os.mkdir(dest)  # fails if the name was taken meanwhile; from here on dest is ours
        try:
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
            _delete_moved(source)
        except BaseException as failure:
            # Failed or interrupted before the move was complete: drop the copy.
            # (Both folders are the app's own copies of the user's archive or folder.)
            _delete_moved(dest)
            if isinstance(failure, shutil.Error):
                errors = failure.args[0] if failure.args and isinstance(failure.args[0], list) else []
                detail = errors[0][-1] if errors and isinstance(errors[0], tuple) else str(failure)
                raise StagingError(f"could not copy {source} to {dest}: {detail}") from failure
            raise
    log.info("MOVE %s -> %s", source, dest)


def _delete_moved(path: Path) -> None:
    """Remove a folder ``_move`` created itself, or one it has completely
    copied elsewhere. Errors are logged: the files are safe either way."""
    try:
        _make_deletable(os.fspath(path))
        shutil.rmtree(path)
        log.info("DELETE %s", path)
    except OSError as exc:
        log.error("could not remove %s: %s", path, exc)
