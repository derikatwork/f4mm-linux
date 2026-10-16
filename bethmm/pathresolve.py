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

"Planned" entries are directories/files a deployment intends to create but
hasn't yet. ``resolve(..., plan=True)`` records the result so a later
``resolve("data/TEXTURES/bar.dds")`` lands in the same new folder, which is
how two mods (or one sloppy mod) using different case end up merged.

Directory listings are cached per resolver; call :meth:`CaseResolver.invalidate`
after changing the tree outside the resolver.

The resolver never creates, moves or deletes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

Convention = Literal["lower", "capitalized", "none"]


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


def infer_dir_convention(existing_dir_names: Iterable[str]) -> Convention:
    """Classify sibling directory names as "lower", "capitalized" or "none"."""
    raise NotImplementedError


def apply_convention(name: str, convention: Convention) -> str:
    """Spell a new directory *name* according to *convention* (see module doc)."""
    raise NotImplementedError


class CaseResolver:
    def __init__(self, base: str | Path):
        """*base* is the real directory everything is resolved under (e.g. the
        game root). It may not exist yet, in which case everything is new."""
        raise NotImplementedError

    def resolve(self, rel: str | PurePosixPath, *, is_dir: bool = False, plan: bool = False) -> ResolvedPath:
        """Resolve *rel* (POSIX separators; backslashes are also accepted and
        treated as separators) under the base.

        Raises ``ValueError`` for absolute paths, empty paths, or paths with
        ``..`` components. ``.`` and empty components are dropped.

        With ``plan=True`` the resolved new directories (and the final file or
        directory) are recorded as planned so later lookups resolve into them.
        """
        raise NotImplementedError

    def invalidate(self, rel_dir: str | PurePosixPath | None = None) -> None:
        """Forget cached listings (all, or just *rel_dir*'s). Planned entries are kept."""
        raise NotImplementedError

    def clear_plan(self) -> None:
        """Forget every planned entry."""
        raise NotImplementedError


def resolve_case_insensitive(base: str | Path, rel: str | PurePosixPath, *, is_dir: bool = False) -> ResolvedPath:
    """One-off convenience wrapper: ``CaseResolver(base).resolve(rel, is_dir=is_dir)``."""
    return CaseResolver(base).resolve(rel, is_dir=is_dir)
