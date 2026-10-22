"""Operations shared by the command line and the GUI.

Each function does one user-level thing (install a mod, change the mod
list, uninstall, verify and repair, ...) on top of :mod:`bethmm.staging`,
:mod:`bethmm.deploy` and the manifest, and returns what happened as data.
Nothing here prints or asks: the CLI and the GUI present the results and do
the asking. Callers hold the manifest (and its lock) for the whole operation.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, TypeVar

from bethmm import deploy, paths, staging, winecheck
from bethmm.discovery import DiscoveredGame
from bethmm.games import GAMES
from bethmm.manifest import Manifest, ManifestError, ModRecord
from bethmm.pathresolve import CaseResolver, fold_path
from bethmm.placement import Confidence, PlacementPlan, Target
from bethmm.settings import Settings

T = TypeVar("T")


class ServiceError(Exception):
    """A request that can't be carried out as asked (nothing was changed)."""


CHANGED_ELSEWHERE = ("the mod list was changed elsewhere (from the command line or another window) since it "
                     "was shown; look at it again and retry")


def _identity(mod: ModRecord) -> tuple:
    return mod.id, mod.game_id, mod.folder, mod.installed_at


def current(manifest: Manifest, mods: list[ModRecord]) -> list[ModRecord]:
    """*mods* as the manifest has them now. ServiceError if one is gone or its
    id now belongs to another mod (or a new version of it): records read
    earlier must not decide what is uninstalled or deleted."""
    out = []
    for mod in mods:
        try:
            now = manifest.mod(mod.id)
        except ManifestError:
            raise ServiceError(f"'{mod.name}' is no longer installed: {CHANGED_ELSEWHERE}") from None
        if _identity(now) != _identity(mod):
            raise ServiceError(f"'{mod.name}' changed: {CHANGED_ELSEWHERE}")
        out.append(now)
    return out


def check_unchanged(manifest: Manifest, game_id: str, shown: list[ModRecord]) -> None:
    """ServiceError unless *game_id*'s mod list is still *shown*: the same
    mods, in the same order, enabled the same way."""
    def key(m: ModRecord) -> tuple:
        return (*_identity(m), m.enabled)

    if [key(m) for m in manifest.mods(game_id)] != [key(m) for m in shown]:
        raise ServiceError(CHANGED_ELSEWHERE[0].upper() + CHANGED_ELSEWHERE[1:])


def target_for(game: DiscoveredGame, settings: Settings) -> deploy.GameTarget:
    return deploy.GameTarget(game.game_id, game.root, settings.override(game.game_id).link_mode)


class _Rollback(Exception):
    pass


@contextmanager
def preview(manifest: Manifest) -> Iterator[None]:
    """Make changes inside a transaction that is always rolled back (for
    dry runs: plan against the changed mod list, change nothing)."""
    try:
        with manifest.transaction():
            yield
            raise _Rollback
    except _Rollback:
        pass


def preflight(manifest: Manifest, target: deploy.GameTarget) -> None:
    """Raise :class:`deploy.DeployError` before the mod list changes if the
    redeploy after it is bound to fail (game folder moved or unusable,
    hardlinks across filesystems). Nothing to check for a purged game."""
    state = manifest.game_state(target.game_id)
    if state is None or state.deployed:
        deploy.preflight(manifest, target)


def _dry_sync(manifest: Manifest, target: deploy.GameTarget) -> deploy.SyncResult | None:
    """What a refresh would do now (None for a purged game: nothing)."""
    state = manifest.game_state(target.game_id)
    if state is not None and not state.deployed:
        return None
    return deploy.apply_plan(manifest, deploy.plan_sync(manifest, target), dry_run=True)


# ---------------------------------------------------------------- mod list


@dataclass
class ModRow:
    """A mod as the mod list shows it."""

    position: int
    mod: ModRecord
    files: int
    #: Names of the mods this one overwrites / is overwritten by (enabled mods only).
    overwrites: list[str] = field(default_factory=list)
    overwritten_by: list[str] = field(default_factory=list)


def mod_rows(manifest: Manifest, game_id: str) -> list[ModRow]:
    with manifest.snapshot():  # a mod installed meanwhile elsewhere is in both reads or neither
        mods = manifest.mods(game_id)
        names = {m.id: m.name for m in mods}
        summary = deploy.conflict_summary(manifest, game_id)
        rows = []
        for i, m in enumerate(mods, start=1):
            c = summary.get(m.id, deploy.ModConflicts())
            rows.append(ModRow(i, m, len(manifest.mod_files(m.id)),
                               [names.get(o, "?") for o in c.overwrites],
                               [names.get(o, "?") for o in c.overwritten_by]))
    return rows


def moved_order(order: list[int], mod_id: int, new_index: int) -> list[int]:
    """*order* with *mod_id* moved to *new_index* (0-based, clamped)."""
    rest = [m for m in order if m != mod_id]
    new_index = max(0, min(new_index, len(rest)))
    return rest[:new_index] + [mod_id] + rest[new_index:]


def preview_change(manifest: Manifest, target: deploy.GameTarget,
                   mutate: Callable[[Manifest], T]) -> tuple[T, deploy.SyncResult | None]:
    """Dry run of a change to the mod list: *mutate* changes the manifest
    inside a transaction that is rolled back; returns its result and what the
    redeploy would do (None for a purged game)."""
    preflight(manifest, target)
    with preview(manifest):
        value = mutate(manifest)
        result = _dry_sync(manifest, target)
    return value, result


def apply_change(manifest: Manifest, target: deploy.GameTarget,
                 mutate: Callable[[Manifest], T]) -> tuple[T, deploy.SyncResult | None]:
    """Change the mod list with *mutate* and redeploy (unless the game is
    purged: then None). The checks that can fail run before the change."""
    preflight(manifest, target)
    value = mutate(manifest)
    return value, deploy.refresh(manifest, target)


# ---------------------------------------------------------------- install


def detected_game(plan: PlacementPlan, settings: Settings, installed: list[str]) -> str | None:
    """The game the files are for, if they tell. A weak guess (e.g. from .ba2
    archives) still wins over a current game the files can't be for."""
    guess = plan.game
    if guess.game_id and guess.confidence >= Confidence.MEDIUM:
        return guess.game_id
    if guess.candidates and settings.current_game not in guess.candidates:
        return next((g for g in guess.candidates if g in installed), None)
    return None


def installable_problem(plan: PlacementPlan) -> str | None:
    """Why *plan* can't be installed as it is (the user must choose folders
    or a target first), or None."""
    if plan.target is not Target.UNKNOWN and plan.mappings:
        return None
    if plan.is_fomod:
        return "this is a FOMOD installer; the guided installer arrives in phase 4. For now choose which of its " \
               "folders to install (and where)."
    if not plan.unplaced and not plan.candidate_roots:
        skipped = f" ({len(plan.skipped)} file(s) such as readmes are never installed)" if plan.skipped else ""
        return f"the archive contains no files to install{skipped}"
    return "it is not clear where these files go: choose the folder(s) to install and whether they go into Data " \
           "or the game folder"


@dataclass
class InstallChoice:
    """How a prepared archive would be installed."""

    game_id: str
    #: The installed mod this one updates (keeping its place), or None for a new mod.
    existing: ModRecord | None
    #: Name in the mod list afterwards.
    name: str
    #: The name the user gave, cleaned (None: keep the existing / suggested name).
    given_name: str | None
    #: Added (or, for an update, made) disabled.
    disabled: bool
    #: The game is purged: nothing is linked now.
    purged: bool
    #: Whether a redeploy follows the install.
    sync: bool
    #: An installed mod with the same name that doesn't look like the same mod
    #: (so it is not updated; --replace / "update this mod" would).
    same_name: str | None = None
    #: The update was asked for explicitly.
    replace_asked: bool = False


def choose_install(manifest: Manifest, game_id: str, prepared: staging.Prepared, *, name: str | None = None,
                   new: bool = False, replace: ModRecord | None = None, disabled: bool = False) -> InstallChoice:
    """Work out whether *prepared* is a new mod or an update, and its name."""
    given_name = staging.clean_text(name) if name else None
    if name is not None and not given_name:
        raise ServiceError("the mod name is empty")
    if replace is not None:
        existing: ModRecord | None = replace
    elif new:
        existing = None
    else:
        existing = staging.find_existing(manifest, game_id, prepared, given_name)
    final_name = given_name or (existing.name if existing else staging.suggested_name(prepared))
    same = None
    if existing is None and not new:
        same = next((m.name for m in manifest.mods(game_id) if m.name.casefold() == final_name.casefold()), None)
    state = manifest.game_state(game_id)
    purged = state is not None and not state.deployed
    sync = not purged and (existing is not None or not disabled)
    return InstallChoice(game_id, existing, final_name, given_name, disabled, purged, sync, same,
                         replace_asked=replace is not None)


def preview_install(manifest: Manifest, target: deploy.GameTarget, prepared: staging.Prepared,
                    choice: InstallChoice) -> tuple[deploy.SyncResult | None, int]:
    """What installing would do to the game folder (None when nothing would be
    linked: the game is purged, or the mod is added disabled), and the mod id
    its operations carry. The mod is added, pointing at the extracted files,
    in a transaction that is rolled back."""
    if choice.sync:
        preflight(manifest, target)
    # An absolute folder: mods_dir / folder is then the extracted files themselves.
    folder = os.path.abspath(prepared.files_dir)
    files = staging.checked_files(prepared)  # refuses what the real install refuses
    with preview(manifest):
        if choice.existing is not None:
            mod_id = choice.existing.id
            manifest.update_mod(mod_id, folder=folder, files=files, name=choice.name)
            if choice.disabled:
                manifest.set_enabled(mod_id, False)
        else:
            mod_id = manifest.add_mod(choice.game_id, choice.name, folder, files, enabled=not choice.disabled).id
        result = _dry_sync(manifest, target) if choice.sync else None
    return result, mod_id


@dataclass
class InstallResult:
    commit: staging.CommitResult
    #: The redeploy that followed (None: none was done, see choice.sync).
    sync: deploy.SyncResult | None
    #: The previous version's folder, kept because links still (may) point
    #: into it -- 'verify --repair' deletes it later. None: removed (or a new mod).
    kept_old_folder: Path | None = None
    #: How many links still point into the kept folder.
    still_linked: int = 0
    #: Why the previous version's folder couldn't be deleted (it is kept; the
    #: update itself is done). None: no error.
    cleanup_error: str | None = None


def install(manifest: Manifest, target: deploy.GameTarget, prepared: staging.Prepared,
            choice: InstallChoice) -> InstallResult:
    """Move the extracted files into the mods folder, record the mod, and
    redeploy. An update's previous folder is deleted once nothing links into it."""
    if choice.sync:
        preflight(manifest, target)  # fail before installing, not after
    result = staging.commit(prepared, manifest, choice.game_id,
                            name=choice.given_name if choice.existing else choice.name, replace=choice.existing,
                            enabled=False if choice.disabled else None)
    sync = deploy.refresh(manifest, target) if choice.sync else None
    out = InstallResult(result, sync)
    if result.old_folder is not None:
        still = links_into(manifest, choice.game_id, result.old_folder)
        if still:  # (marked as retired by commit)
            out.kept_old_folder, out.still_linked = result.old_folder, len(still)
        else:
            try:
                staging.remove_staging(result.old_folder)
            except (OSError, staging.StagingError) as exc:  # the update is done; repair deletes it later
                out.kept_old_folder, out.cleanup_error = result.old_folder, _reason(exc)
    return out


def _reason(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}" + (f" ({exc.filename})" if exc.filename else "")
    return str(exc)


@dataclass
class LinkOwners:
    """What the app has put into a game folder (from :func:`link_owners`)."""

    #: Folded path -> (id, name) of the installed mod whose link the app put there.
    links: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: Mod id -> its place in the list (the higher, the lower in the list: it wins).
    position: dict[int, int] = field(default_factory=dict)
    #: Folded paths of the folders the app made.
    dirs: set[str] = field(default_factory=set)


def link_owners(game_id: str, manifest: Manifest | None = None) -> LinkOwners:
    """The app's links and folders in *game_id*'s folder (read-only; empty
    when nothing was ever installed)."""
    if manifest is None:
        if not paths.manifest_path().exists():
            return LinkOwners()
        try:
            with Manifest() as opened:
                return link_owners(game_id, opened)
        except ManifestError:
            return LinkOwners()
    with manifest.snapshot():
        mods = manifest.mods(game_id)
        names = {m.id: m.name for m in mods}
        links = {key: (d.mod_id, names.get(d.mod_id, "another mod")) for key, d in manifest.deployed(game_id).items()}
        return LinkOwners(links, {m.id: i for i, m in enumerate(mods)}, set(manifest.created_dirs(game_id)))


def _ours_in_the_way(root: Path, rel: PurePosixPath, owners: LinkOwners) -> bool:
    """For a path where the game folder has a file instead of a folder (or a
    folder instead of a file): whether all that is in the way is the app's
    -- its link, or a folder it made holding only its links and folders -- so
    a deploy can clear it. A folder or file of the game's is never removed."""
    for i in range(1, len(rel.parts)):
        above = root.joinpath(*rel.parts[:i])
        if not os.path.lexists(above):
            return False  # in the way only within this mod
        if above.is_symlink() or not above.is_dir():
            return fold_path("/".join(rel.parts[:i])) in owners.links
    folder = root / rel
    if folder.is_symlink() or not folder.is_dir() or fold_path(str(rel)) not in owners.dirs:
        return False
    for path, dirs, files in os.walk(folder):
        here = Path(path).relative_to(root)
        for name in dirs:
            key = fold_path(str(here / name))
            if key not in (owners.links if (Path(path) / name).is_symlink() else owners.dirs):
                return False
        if any(fold_path(str(here / name)) not in owners.links for name in files):
            return False
    return True


@dataclass
class ResolvedFile:
    """Where one file of a mod lands in the real game folder."""

    source: str
    dest: str
    status: str
    #: Why the destination is spelled differently from the mod's, if it is.
    case_adjusted: str | None = None
    new_dirs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"source": self.source, "dest": self.dest, "case_adjusted": self.case_adjusted,
                "status": self.status, "new_dirs": self.new_dirs, "notes": self.notes}


def resolve_against_game(plan: PlacementPlan, game: DiscoveredGame, owners: LinkOwners | None = None,
                         replacing: int | None = None) -> list[ResolvedFile]:
    """Where each mapping would land in the real game folder (read-only).
    *owners* (from :func:`link_owners`) tells the app's links from game
    files; *replacing* is the mod this one updates (it keeps its place in
    the list; a new mod goes to the bottom)."""
    if not game.root.is_dir():
        return []
    resolver = CaseResolver(game.root)
    owners = owners or LinkOwners()
    links = owners.links
    place = owners.position.get(replacing) if replacing is not None else None

    def loses_to(other: tuple[int, str]) -> bool:
        return other[0] != replacing and place is not None and owners.position.get(other[0], -1) > place

    def against(other: tuple[int, str], what: str) -> str:
        if other[0] == replacing:
            return "replaces the previous version's file"
        if loses_to(other):
            return f"loses to {other[1]}, lower in the list ({what} stays in the game)"
        return f"overrides {other[1]} ({what} stays installed)"

    out = []
    for m in plan.mappings:
        r = resolver.resolve(m.dest, plan=True)
        key = fold_path(str(r.rel))
        owner = links.get(key)
        # Another mod's file where this needs a folder (or the other way round)
        # is a conflict like any other: the mod lower in the list wins.
        clash = None
        if r.type_conflict:
            parts = key.split("/")
            clash = next((links[k] for k in ("/".join(parts[:i]) for i in range(1, len(parts))) if k in links),
                         None) or next((o for k, o in links.items() if k.startswith(key + "/")), None)
        # A win counts only when all that is in the way is the app's to clear.
        if clash is not None and (loses_to(clash) or _ours_in_the_way(game.root, r.rel, owners)):
            status = against(clash, "its file or folder at this path")
        elif r.type_conflict:
            status = "conflict: a folder/file of the other type already exists there"
        elif r.exists_on_disk and owner is not None:
            status = against(owner, "its file")
        elif r.exists_on_disk:
            status = "replaces an existing game file (it would be backed up)"
        elif r.exists:
            status = "duplicate within this mod"
        else:
            status = "new"
        out.append(ResolvedFile(m.source, str(r.rel), status, _case_note(m.dest, r.rel, game.root),
                                [str(d) for d in r.new_dirs], list(r.ambiguous)))
    return out


def _case_note(wanted: str, rel, root: Path) -> str | None:
    """Why the resolved path is spelled differently from the mod's, if it is."""
    wanted_parts = wanted.replace("\\", "/").split("/")
    reasons = set()
    for i, (mine, actual) in enumerate(zip(wanted_parts, rel.parts)):
        if mine != actual:
            on_disk = os.path.lexists(root.joinpath(*rel.parts[: i + 1]))
            reasons.add("case matched to existing folder" if on_disk else "new folder spelled like the game's folders")
    return "; ".join(sorted(reasons)) or None


# ---------------------------------------------------------------- uninstall


@dataclass
class UninstallResult:
    #: The redeploy that removed their links (None: the game is purged).
    sync: deploy.SyncResult | None
    removed: list[ModRecord] = field(default_factory=list)
    #: Mods that still have links that couldn't be removed: they stay installed, disabled.
    still_linked: list[ModRecord] = field(default_factory=list)
    #: Staging folders kept because an interrupted run may have linked into them.
    kept_folders: list[tuple[ModRecord, Path]] = field(default_factory=list)
    #: Uninstalled mods whose staged files couldn't be deleted: (mod, folder, why).
    not_deleted: list[tuple[ModRecord, Path, str]] = field(default_factory=list)


def _disable_all(mods: list[ModRecord]) -> Callable[[Manifest], None]:
    def mutate(manifest: Manifest) -> None:
        for mod in mods:
            manifest.set_enabled(mod.id, False)
    return mutate


def preview_uninstall(manifest: Manifest, target: deploy.GameTarget,
                      mods: list[ModRecord]) -> deploy.SyncResult | None:
    return preview_change(manifest, target, _disable_all(current(manifest, mods)))[1]


def uninstall(manifest: Manifest, target: deploy.GameTarget, mods: list[ModRecord]) -> UninstallResult:
    """Disable *mods*, redeploy, then remove them and delete their staged files."""
    mods = current(manifest, mods)
    _, sync = apply_change(manifest, target, _disable_all(mods))
    out = UninstallResult(sync)
    for mod in mods:
        folder = mod.staging_dir()
        # Marked first: if the app dies while deleting a big folder,
        # 'verify --repair' can tell the leftover is its own.
        if folder.is_dir() and not folder.is_symlink():
            staging.retire(folder)
        try:
            manifest.remove_mod(mod.id)
        except ManifestError:
            staging.unretire(folder)
            out.still_linked.append(mod)
            continue
        if links_into(manifest, mod.game_id, folder):
            out.kept_folders.append((mod, folder))
        else:
            try:
                staging.remove_staging(folder)
            except (OSError, staging.StagingError) as exc:  # uninstalled; repair deletes the rest later
                out.not_deleted.append((mod, folder, _reason(exc)))
        out.removed.append(mod)
    return out


def links_into(manifest: Manifest, game_id: str, folder: Path) -> list[str]:
    """Paths in the game folder that link (or, by the journal, may link) into *folder*."""
    prefix = str(folder) + "/"
    found = [d.path for d in manifest.deployed(game_id).values() if d.source.startswith(prefix)]
    found += [e.path for e in manifest.unfinished_journal(game_id)
              if e.action == "link" and e.path and (e.source or "").startswith(prefix)]
    return found


# ---------------------------------------------------------------- verify / repair


def unused_staging(manifest: Manifest, game_id: str) -> tuple[list[Path], list[Path]]:
    """Folders in the game's mods folder that no installed mod and no deployed
    link uses: those an update marked as retired (the app deletes them) and
    any others (left alone: the app can't tell they are its own)."""
    base = paths.mods_dir(game_id)
    try:
        entries = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return [], []
    used = {m.folder for m in manifest.mods(game_id)}
    sources = [d.source for d in manifest.deployed(game_id).values()]
    sources += [e.source for e in manifest.unfinished_journal(game_id) if e.action == "link" and e.source]
    unused = [
        p for p in entries
        if p.name not in used and p.name != winecheck.TEST_DIR
        and not any(src.startswith(str(p) + "/") for src in sources)
    ]
    retired = [p for p in unused if staging.is_retired(p)]
    return retired, [p for p in unused if p not in retired]


@dataclass
class Check:
    """What 'verify' found."""

    report: deploy.VerifyReport
    #: Previous versions of mods nothing uses any more (repair deletes them).
    retired: list[Path]
    #: Other unknown folders in the mods folder (left alone).
    unknown: list[Path]

    @property
    def problems(self) -> bool:
        """Something repair can work on. Links found in another copy of the
        game are only reported: repair never touches them."""
        return not replace(self.report, links_elsewhere=[]).ok or bool(self.retired)


def check(manifest: Manifest, target: deploy.GameTarget) -> Check:
    report = deploy.verify(manifest, target)
    retired, unknown = unused_staging(manifest, target.game_id)
    return Check(report, retired, unknown)


@dataclass
class RepairOutcome:
    actions: list[str] = field(default_factory=list)
    #: The purge (purged game) or redeploy after the repair.
    sync: deploy.SyncResult | None = None
    #: Why that purge or redeploy couldn't run.
    sync_error: str | None = None
    purged: bool = False
    #: Retired staging folders deleted (or, in a dry run, that would be).
    deleted: list[Path] = field(default_factory=list)
    #: Leftovers of the Wine link check removed.
    leftovers: list[str] = field(default_factory=list)
    #: Retired folders that couldn't be deleted: (folder, why). The rest went on.
    not_deleted: list[tuple[Path, str]] = field(default_factory=list)


def repair(manifest: Manifest, target: deploy.GameTarget, found: Check, *, release_held: bool = False,
           dry_run: bool = False, data_dir: Path | None = None) -> RepairOutcome:
    """Fix what :func:`check` found, then purge again (purged game: freed
    originals go back) or redeploy, and delete unused retired folders. In a
    dry run only the repair actions (and the folders) are listed."""
    out = RepairOutcome(deploy.repair(manifest, target, found.report, dry_run=dry_run, release_held=release_held))
    state = manifest.game_state(target.game_id)
    out.purged = state is not None and not state.deployed
    if dry_run:
        out.deleted = list(found.retired)
        return out
    if data_dir is not None:
        out.leftovers = winecheck.remove_leftovers(target.game_id, data_dir)
    try:
        out.sync = deploy.purge(manifest, target) if out.purged else deploy.refresh(manifest, target)
    except deploy.DeployError as exc:
        out.sync_error = str(exc)
        return out
    for folder in unused_staging(manifest, target.game_id)[0]:
        try:
            staging.remove_staging(folder)
        except (OSError, staging.StagingError) as exc:
            out.not_deleted.append((folder, _reason(exc)))
            continue
        out.deleted.append(folder)
    return out


# ---------------------------------------------------------------- status


@dataclass
class Footprint:
    """What the app still has in one game's folder."""

    game_id: str
    root: Path | None
    links: int
    unfinished: int
    #: Originals a purge puts back.
    restorable: int
    #: Originals kept because a newer file is at their path (safe to leave).
    held: int
    #: Why the game folder can't be used now, if it can't.
    unusable: str | None = None

    @property
    def name(self) -> str:
        return GAMES[self.game_id].name if self.game_id in GAMES else self.game_id

    @property
    def busy(self) -> bool:
        """A purge has something to do."""
        return bool(self.links or self.unfinished or self.restorable)


def footprints(manifest: Manifest) -> list[Footprint]:
    """Per game with anything recorded: links, interrupted runs, originals."""
    out = []
    for g in manifest.game_ids():
        links, backups = manifest.deployed(g), manifest.backups(g)
        unfinished = manifest.unfinished_journal(g)
        if not links and not backups and not unfinished:
            continue
        state = manifest.game_state(g)
        root = Path(state.root) if state is not None else None
        report = deploy.verify(manifest, deploy.GameTarget(g, root)) if root is not None else None
        held = report.held_backups if report is not None and not report.root_missing else []
        out.append(Footprint(g, root, len(links), len(unfinished), len(backups) - len(held), len(held),
                             report.missing_reason if report is not None and report.root_missing else None))
    return out
