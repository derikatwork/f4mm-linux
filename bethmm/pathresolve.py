"""Case-insensitive path resolution against a real directory tree.

Windows ignores case; Linux doesn't. Before deploying ``Data/Textures/foo.dds``
into a game folder that already has ``data/textures/``, every component is
resolved against what is on disk so the file lands in the existing folder
instead of creating a duplicate that Wine would then see as ambiguous.

Rules for each path component, from the base downwards:

1. If the parent (on disk *or* planned, see below) has an entry whose name
   matches case-insensitively, use that entry's exact spelling. If several
   entries match (the folder is already a mess, e.g. both ``Textures`` and
   ``textures`` exist), prefer an exact-case match, otherwise the
   lexicographically first one, and record a warning in ``ambiguous``.
2. Otherwise the component is new. For directories the spelling follows the
   parent folder's existing convention: if every existing sub-directory name
   in the parent that contains a letter is all-lowercase, the new name is
   lowercased; if every one starts with an uppercase letter, a new name that is
   entirely lowercase gets its first letter capitalised (names that already
   contain uppercase letters, like ``F4SE`` or ``MCM``, are kept as they are);
   otherwise (mixed, or no sub-directories) the mod's own spelling is kept.
   The final component of a file path keeps the mod's spelling when new.

Names are compared the way Windows/Wine do it: each character is upper-cased
on its own when that gives a single character (so ``ä`` matches ``Ä`` and
``σ``/``ς`` match ``Σ``, but ``ß`` does not match ``ss``). Names are not
Unicode-normalised. A symlink to a directory counts as a directory; a dangling
symlink counts as a file.

"Planned" entries are directories/files a deployment intends to create but
hasn't yet. ``resolve(..., plan=True)`` records the result so a later
``resolve("data/TEXTURES/bar.dds")`` lands in the same new folder, which is
how two mods (or one sloppy mod) using different case end up merged. Planned
entries behave exactly like on-disk ones (matching and folder convention), so
resolving with a plan gives the same answer as deploying first and resolving
afterwards.

Directory listings are cached per resolver; call :meth:`CaseResolver.invalidate`
after changing the tree outside the resolver.

The resolver never creates, moves or deletes anything.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Literal

Convention = Literal["lower", "capitalized", "none"]

_Key = tuple[str, ...]

_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ResolvedPath:
    #: Relative path to use, with existing components spelled as on disk.
    rel: PurePosixPath
    #: ``base / rel``.
    path: Path
    #: True when the full path exists on disk or is planned.
    exists: bool
    #: True when the full path exists on disk (not merely planned).
    exists_on_disk: bool
    #: Directories (relative, in creation order) that don't exist on disk or in
    #: the plan and would have to be created; excludes the final component
    #: unless resolving with ``is_dir=True``.
    new_dirs: tuple[PurePosixPath, ...]
    #: Human-readable warnings about pre-existing case-variant duplicates.
    ambiguous: tuple[str, ...] = ()
    #: True when a component matches an existing entry of the wrong type
    #: (e.g. the mod has a file where the game has a directory).
    type_conflict: bool = False


def _fold_char(c: str) -> str:
    up = c.upper()
    return up if len(up) == 1 else c


def _fold(name: str) -> str:
    """Case-fold *name* like Windows: per-character, single-character upper case only."""
    up = name.upper()
    if len(up) == len(name):
        # No character expanded, so this is exactly the per-character mapping.
        return up
    return "".join(_fold_char(c) for c in name)


def _fold_key(key: Iterable[str]) -> _Key:
    return tuple(_fold(p) for p in key)


def _is_cased(c: str) -> bool:
    return c.isupper() or c.islower()


class _Tally:
    """Running classification of directory names (see :func:`infer_dir_convention`)."""

    __slots__ = ("seen", "lower", "capitalized")

    def __init__(self) -> None:
        self.seen = False  # some name has a cased letter
        self.lower = True  # ...and every such name is all-lowercase
        self.capitalized = True  # ...and every such name's first cased letter is uppercase

    def add(self, name: str) -> None:
        first = next((c for c in name if _is_cased(c)), None)
        if first is not None:
            self.seen = True
            self.lower = self.lower and name == name.lower()
            self.capitalized = self.capitalized and first.isupper()


def _convention(*tallies: _Tally) -> Convention:
    if not any(t.seen for t in tallies):
        return "none"
    if all(t.lower for t in tallies):
        return "lower"
    return "capitalized" if all(t.capitalized for t in tallies) else "none"


def infer_dir_convention(existing_dir_names: Iterable[str]) -> Convention:
    """Classify sibling directory names as "lower", "capitalized" or "none"."""
    tally = _Tally()
    for name in existing_dir_names:
        tally.add(name)
        if not tally.lower and not tally.capitalized:
            return "none"
    return _convention(tally)


def apply_convention(name: str, convention: Convention) -> str:
    """Spell a new directory *name* according to *convention* (see module doc)."""
    if convention == "lower":
        return "".join(_lower_char(c) for c in name)
    if convention == "capitalized" and name == name.lower():
        for i, c in enumerate(name):
            if _is_cased(c):
                up = c.upper()
                if len(up) == 1 and _fold_char(up) == _fold_char(c):
                    return name[:i] + up + name[i + 1:]
                return name
    return name


def _lower_char(c: str) -> str:
    # Only change case where it keeps the name equal under _fold, so the new
    # spelling still matches the mod's own spelling later (e.g. KELVIN SIGN).
    low = c.lower()
    return low if len(low) == 1 and _fold_char(low) == _fold_char(c) else c


def _split(rel: str | os.PathLike[str], *, allow_empty: bool = False) -> _Key:
    text = rel if isinstance(rel, str) else os.fspath(rel)
    if not isinstance(text, str):
        raise TypeError(f"expected a str path, got {type(text).__name__}")
    if "\0" in text:
        raise ValueError(f"path contains a NUL character: {rel!r}")
    text = text.replace("\\", "/")
    if text.startswith("/") or _DRIVE.match(text):
        raise ValueError(f"path must be relative: {rel!r}")
    parts = tuple(p for p in text.split("/") if p not in ("", "."))
    if ".." in parts:
        raise ValueError(f"path must not contain '..': {rel!r}")
    if not parts and not allow_empty:
        raise ValueError(f"empty path: {rel!r}")
    return parts


class _Listing:
    """Entries of one directory: exact name -> is_dir, a folded-name index and
    a running tally of the sub-directory naming convention."""

    __slots__ = ("entries", "index", "dirs")

    def __init__(self) -> None:
        self.entries: dict[str, bool] = {}
        self.index: dict[str, list[str]] = {}
        self.dirs = _Tally()

    def add(self, name: str, is_dir: bool) -> None:
        was_dir = self.entries.get(name)
        if was_dir is None:
            self.index.setdefault(_fold(name), []).append(name)
        self.entries[name] = is_dir
        if is_dir and not was_dir:
            self.dirs.add(name)
        elif was_dir and not is_dir:
            # A directory became a file (plan vs. a rescanned tree): recount.
            self.dirs = _Tally()
            for other in self.dir_names():
                self.dirs.add(other)

    def matches(self, folded: str) -> list[str]:
        return self.index.get(folded, [])

    def dir_names(self) -> Iterator[str]:
        return (name for name, is_dir in self.entries.items() if is_dir)


_EMPTY = _Listing()


def _entry_is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        # Symlink loops and unreadable targets can't be entered: treat as files.
        return False


class CaseResolver:
    def __init__(self, base: str | Path):
        """*base* is the real directory everything is resolved under (e.g. the
        game root). It may not exist yet, in which case everything is new."""
        self.base = Path(base)
        self._listings: dict[_Key, _Listing] = {}
        # Every folded prefix of every cached key -> the cached keys below it.
        self._cached_under: dict[_Key, set[_Key]] = {}
        self._planned: dict[_Key, _Listing] = {}
        # Entries on disk that a plan removes (exact spelling): resolved as absent.
        self._gone: set[_Key] = set()

    def forget(self, rel: str | PurePosixPath) -> None:
        """Resolve the entry at *rel* (its exact spelling on disk) as absent from
        now on: a plan removes it before what is resolved next is created."""
        self._gone.add(_split(rel))

    def resolve(self, rel: str | PurePosixPath, *, is_dir: bool = False, plan: bool = False) -> ResolvedPath:
        """Resolve *rel* (POSIX separators; backslashes are also accepted and
        treated as separators) under the base.

        Raises ``ValueError`` for absolute paths, empty paths, or paths with
        ``..`` components. ``.`` and empty components are dropped.

        With ``plan=True`` the resolved new directories (and the final file or
        directory) are recorded as planned so later lookups resolve into them.
        Nothing is recorded when the result has a type conflict.
        """
        parts = _split(rel)
        resolved: list[str] = []
        new_dirs: list[PurePosixPath] = []
        ambiguous: list[str] = []
        type_conflict = False
        exists = exists_on_disk = False
        # State of the directory the current component lives in.
        parent_on_disk = True  # the base is scanned; a missing base lists as empty
        parent_known = True  # on disk or planned, as a directory
        last_index = len(parts) - 1

        for i, comp in enumerate(parts):
            last = i == last_index
            want_dir = is_dir or not last
            key = tuple(resolved)
            disk = self._disk_listing(key) if parent_on_disk else _EMPTY
            planned = self._planned.get(key, _EMPTY) if parent_known else _EMPTY

            folded = _fold(comp)
            on_disk_names = [m for m in disk.matches(folded) if (*key, m) not in self._gone]
            if (*key, comp) in self._gone and on_disk_names:
                # Re-created exactly where the plan removes it (the app's own link
                # switching mods) -- not onto another entry spelled in another case.
                where = "/".join(key) or "."
                others = ", ".join(repr(m) for m in on_disk_names)
                ambiguous.append(f"{where!r} also has {others}, differing only in case from {comp!r}; using {comp!r}")
                on_disk_names = []
            matches = sorted(set(on_disk_names) | set(planned.matches(folded)))
            if matches:
                name = comp if comp in matches else matches[0]
                if len(matches) > 1:
                    where = "/".join(key) or "."
                    names = ", ".join(repr(m) for m in matches)
                    ambiguous.append(f"{where!r} has entries differing only in case: {names}; using {name!r}")
                on_disk = name in on_disk_names
                entry_is_dir = disk.entries[name] if on_disk else planned.entries[name]
                if entry_is_dir != want_dir:
                    type_conflict = True
                resolved.append(name)
                if last:
                    exists, exists_on_disk = True, on_disk
                elif entry_is_dir:
                    parent_on_disk, parent_known = on_disk, True
                else:
                    parent_on_disk = parent_known = False
                continue

            if want_dir:
                resolved.append(apply_convention(comp, _convention(disk.dirs, planned.dirs)))
                new_dirs.append(PurePosixPath(*resolved))
            else:
                resolved.append(comp)
            parent_on_disk = parent_known = False

        rel_path = PurePosixPath(*resolved)
        if plan and not type_conflict:
            for d in new_dirs:
                self._planned.setdefault(tuple(d.parts[:-1]), _Listing()).add(d.name, True)
            self._planned.setdefault(tuple(resolved[:-1]), _Listing()).add(resolved[-1], is_dir)

        return ResolvedPath(
            rel=rel_path,
            path=self.base / rel_path,
            exists=exists,
            exists_on_disk=exists_on_disk,
            new_dirs=tuple(new_dirs),
            ambiguous=tuple(ambiguous),
            type_conflict=type_conflict,
        )

    def invalidate(self, rel_dir: str | PurePosixPath | None = None) -> None:
        """Forget cached listings (all, or just *rel_dir*'s). Planned entries are kept.

        *rel_dir* is matched case-insensitively, and listings cached below it
        are forgotten too since they may be stale as well. Whether *rel_dir*
        itself exists is part of its parent's listing: after creating or
        removing a folder, invalidate the folder's parent.
        """
        if rel_dir is None:
            self._listings.clear()
            self._cached_under.clear()
            return
        prefix = _fold_key(_split(rel_dir, allow_empty=True))
        n = len(prefix)
        for key in self._cached_under.pop(prefix, ()):
            del self._listings[key]
            folded = _fold_key(key)
            for i in range(n):
                under = self._cached_under[folded[:i]]
                under.discard(key)
                if not under:
                    del self._cached_under[folded[:i]]
            for i in range(n + 1, len(folded) + 1):
                self._cached_under.pop(folded[:i], None)

    def clear_plan(self) -> None:
        """Forget every planned entry."""
        self._planned.clear()

    def _disk_listing(self, key: _Key) -> _Listing:
        listing = self._listings.get(key)
        if listing is None:
            listing = _Listing()
            try:
                with os.scandir(self.base.joinpath(*key)) as it:
                    for entry in it:
                        listing.add(entry.name, _entry_is_dir(entry))
            except OSError:
                # Missing, not a directory, or unreadable: nothing to match.
                pass
            self._listings[key] = listing
            folded = _fold_key(key)
            for i in range(len(folded) + 1):
                self._cached_under.setdefault(folded[:i], set()).add(key)
        return listing


def fold_path(rel: str | PurePosixPath) -> str:
    """Case-folded key for a relative path, using the same rules as the
    resolver (separators normalised, ``.`` dropped). Two paths that Windows
    would treat as the same file get the same key."""
    return "/".join(_fold_key(_split(rel)))


def resolve_case_insensitive(base: str | Path, rel: str | PurePosixPath, *, is_dir: bool = False) -> ResolvedPath:
    """One-off convenience wrapper: ``CaseResolver(base).resolve(rel, is_dir=is_dir)``."""
    return CaseResolver(base).resolve(rel, is_dir=is_dir)
