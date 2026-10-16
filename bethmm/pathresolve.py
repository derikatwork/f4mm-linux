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


def _is_cased(c: str) -> bool:
    return c.isupper() or c.islower()


def infer_dir_convention(existing_dir_names: Iterable[str]) -> Convention:
    """Classify sibling directory names as "lower", "capitalized" or "none"."""
    lower = capitalized = True
    seen = False
    for name in existing_dir_names:
        first = next((c for c in name if _is_cased(c)), None)
        if first is None:
            continue
        seen = True
        if name != name.lower():
            lower = False
        if not first.isupper():
            capitalized = False
        if not lower and not capitalized:
            return "none"
    if not seen:
        return "none"
    return "lower" if lower else "capitalized"


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
    """Entries of one directory: exact name -> is_dir, plus a folded-name index."""

    __slots__ = ("entries", "index")

    def __init__(self) -> None:
        self.entries: dict[str, bool] = {}
        self.index: dict[str, list[str]] = {}

    def add(self, name: str, is_dir: bool) -> None:
        if name not in self.entries:
            self.index.setdefault(_fold(name), []).append(name)
        self.entries[name] = is_dir

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
        self._planned: dict[_Key, _Listing] = {}

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
            matches = sorted(set(disk.matches(folded)) | set(planned.matches(folded)))
            if matches:
                name = comp if comp in matches else matches[0]
                if len(matches) > 1:
                    where = "/".join(key) or "."
                    names = ", ".join(repr(m) for m in matches)
                    ambiguous.append(f"{where!r} has entries differing only in case: {names}; using {name!r}")
                on_disk = name in disk.entries
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
                siblings = list(disk.dir_names()) + list(planned.dir_names())
                resolved.append(apply_convention(comp, infer_dir_convention(siblings)))
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
        are forgotten too since they may be stale as well.
        """
        if rel_dir is None:
            self._listings.clear()
            return
        prefix = tuple(_fold(p) for p in _split(rel_dir, allow_empty=True))
        n = len(prefix)
        for key in list(self._listings):
            if len(key) >= n and tuple(_fold(p) for p in key[:n]) == prefix:
                del self._listings[key]

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
        return listing


def resolve_case_insensitive(base: str | Path, rel: str | PurePosixPath, *, is_dir: bool = False) -> ResolvedPath:
    """One-off convenience wrapper: ``CaseResolver(base).resolve(rel, is_dir=is_dir)``."""
    return CaseResolver(base).resolve(rel, is_dir=is_dir)
