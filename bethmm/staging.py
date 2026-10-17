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
``StagingError``). Every file operation is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from bethmm.manifest import Manifest, ModRecord
from bethmm.placement import PlacementPlan, Target


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
    """
    raise NotImplementedError


def suggested_name(prepared: Prepared) -> str:
    """Display name for the mod: the Nexus name from the archive file name,
    else the archive/folder name without extension."""
    raise NotImplementedError


def folder_name(name: str, existing: Sequence[str] = ()) -> str:
    """A safe staging folder name for *name*, unique against *existing*
    (case-insensitively). See module docstring for the rules."""
    raise NotImplementedError


def find_existing(manifest: Manifest, game_id: str, prepared: Prepared, name: str | None = None) -> ModRecord | None:
    """An installed mod this one would update: the same Nexus mod ID, else the
    same name (case-insensitive). None when there is no such mod."""
    raise NotImplementedError


def commit(
    prepared: Prepared,
    manifest: Manifest,
    game_id: str,
    *,
    name: str | None = None,
    replace: ModRecord | None = None,
) -> CommitResult:
    """Move the extracted files into the mods folder and record the mod.

    Raises ``StagingError`` when the plan has no mappings or its target is
    UNKNOWN (the user must choose a root/target first), when the plan is a
    FOMOD without mappings, or when *replace* belongs to another game.
    The manifest's ``plan`` field stores ``{"root", "target", "rule",
    "wrapper_dirs", "skipped": <count>}`` from the PlacementPlan.
    """
    raise NotImplementedError


def discard(prepared: Prepared) -> None:
    """Remove what is left of the temp folder (safe to call twice)."""
    raise NotImplementedError


def remove_staging(folder: str | Path) -> None:
    """Delete a mod's staging folder -- only ever inside ``paths.mods_dir()``."""
    raise NotImplementedError
