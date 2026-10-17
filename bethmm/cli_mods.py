"""Mod management commands (phase 2): install, list, enable/disable, move,
uninstall, conflicts, deploy, purge, verify, status, check-links, history, use.

Every command that changes a game folder goes through ``deploy`` and accepts
``--dry-run`` (also switched on for everything by ``bethmm config --dry-run on``),
which prints what would happen and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from bethmm import deploy, paths, staging, winecheck
from bethmm.discovery import DiscoveredGame, get_games
from bethmm.games import GAMES
from bethmm.manifest import Manifest, ManifestError, ModFile, ModRecord
from bethmm.placement import Confidence, Target
from bethmm.settings import Settings, load_settings, save_settings

TARGETS = {"data": Target.DATA, "game": Target.GAME_ROOT}


def register(sub: argparse._SubParsersAction, error: type[Exception]) -> None:
    global CliError
    CliError = error

    def game_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--game", choices=sorted(GAMES), help="which game (default: the one chosen with 'bethmm use')")

    def dry_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")

    p = sub.add_parser("use", help="choose the game that mod commands act on")
    p.add_argument("game", choices=sorted(GAMES))
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("install", help="install a mod from an archive (or extracted folder) and deploy it")
    p.add_argument("source", help="a .zip/.7z/.rar archive or an extracted folder")
    game_arg(p)
    p.add_argument("--root", action="append", metavar="DIR", help="install this folder of the archive (repeatable)")
    p.add_argument("--target", choices=sorted(TARGETS), help="put the root into the Data folder or the game folder")
    p.add_argument("--name", help="name to show in the mod list (on an update: renames the mod)")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--new", action="store_true", help="install as a separate mod even if it looks like an update")
    which.add_argument("--replace", metavar="MOD", help="install as the new version of this mod (number or name)")
    p.add_argument("--no-deploy", "--disabled", dest="no_deploy", action="store_true",
                   help="add the mod disabled: nothing of it is linked until you enable it")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    dry_arg(p)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("list", help="list installed mods (the lowest in the list wins conflicts)")
    game_arg(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    for name, enabled in (("enable", True), ("disable", False)):
        p = sub.add_parser(name, help=f"{name} mods and redeploy")
        p.add_argument("mods", nargs="+", metavar="MOD", help="number from 'list', name, or part of a name")
        game_arg(p)
        dry_arg(p)
        p.set_defaults(func=cmd_enable, enabled=enabled)

    p = sub.add_parser("move", help="change a mod's place in the list (lower wins) and redeploy")
    p.add_argument("mod", metavar="MOD")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--up", action="store_true", help="one place up (loses more conflicts)")
    where.add_argument("--down", action="store_true", help="one place down (wins more conflicts)")
    where.add_argument("--top", action="store_true", help="to the top (lowest priority)")
    where.add_argument("--bottom", action="store_true", help="to the bottom (highest priority)")
    where.add_argument("--to", type=int, metavar="N", help="to position N")
    game_arg(p)
    dry_arg(p)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("uninstall", help="remove a mod: undeploy it and delete its staged files")
    p.add_argument("mods", nargs="+", metavar="MOD")
    game_arg(p)
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    dry_arg(p)
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("conflicts", help="show which mods overwrite which files")
    p.add_argument("mod", nargs="?", metavar="MOD", help="only this mod")
    game_arg(p)
    p.add_argument("--files", action="store_true", help="list every conflicting file")
    p.set_defaults(func=cmd_conflicts)

    p = sub.add_parser("deploy", help="(re)link every enabled mod into the game folder")
    game_arg(p)
    dry_arg(p)
    p.add_argument("-v", "--verbose", action="store_true", help="list every file operation")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("purge", help="remove everything the app put in the game folder (mods stay installed)")
    game_arg(p)
    p.add_argument("--all", action="store_true", help="every game")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    dry_arg(p)
    p.add_argument("-v", "--verbose", action="store_true", help="list every file operation")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("verify", help="check the game folder against the manifest")
    game_arg(p)
    p.add_argument("--repair", action="store_true", help="fix what can be fixed safely, then redeploy")
    p.add_argument("--release-held", action="store_true",
                   help="with --repair: stop tracking originals whose path now holds a newer file (e.g. after a "
                        "game update); they are kept in backups/<game>/.superseded/")
    dry_arg(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("status", help="summary of installed and deployed mods per game")
    p.add_argument("--deployed-only", action="store_true",
                   help="list what the app still has in game folders; exit with status 1 if anything (for scripts)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check-links", help="test that Wine follows the app's links for a game")
    p.add_argument("game", nargs="?", choices=sorted(GAMES))
    p.add_argument("--wine", help="Wine binary to use (default: the game's Lutris runner)")
    p.add_argument("--timeout", type=float, default=120, help="seconds to wait for Wine (default 120)")
    dry_arg(p)
    p.set_defaults(func=cmd_check_links)

    p = sub.add_parser("history", help="recent file operations")
    game_arg(p)
    p.add_argument("-n", type=int, default=30, help="how many (default 30)")
    p.set_defaults(func=cmd_history)


CliError: type[Exception] = Exception


# ---------------------------------------------------------------- helpers


def _choose_game(args, settings: Settings, games: list[DiscoveredGame], *, detected: str | None = None) -> DiscoveredGame:
    wanted = getattr(args, "game", None) or detected or settings.current_game
    if wanted is None and len(games) == 1:
        wanted = games[0].game_id
    if wanted is None:
        found = ", ".join(g.game_id for g in games) or "none found"
        raise CliError(f"which game? Add --game, or pick one with 'bethmm use <game>' (installed: {found})")
    game = next((g for g in games if g.game_id == wanted), None)
    if game is None:
        raise CliError(f"{GAMES[wanted].name} was not found. See 'bethmm games', or set it with "
                       f"'bethmm override {wanted} --root <game folder>'")
    return game


def _context(args) -> tuple[Settings, list[DiscoveredGame]]:
    settings = load_settings()
    games, _ = get_games(settings)
    return settings, games


def _target(game: DiscoveredGame, settings: Settings) -> deploy.GameTarget:
    return deploy.GameTarget(game.game_id, game.root, settings.override(game.game_id).link_mode)


def _dry(args, settings: Settings) -> bool:
    dry = bool(getattr(args, "dry_run", False)) or settings.dry_run
    if dry and settings.dry_run and not getattr(args, "dry_run", False):
        print("(dry-run mode is on: nothing will change; 'bethmm config --dry-run off' to turn it off)")
    return dry


def _confirm(question: str, args) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise CliError(f"{question} -- add --yes to confirm when not running in a terminal")
    reply = input(f"{question} [y/N] ").strip().lower()
    return reply in ("y", "yes")


class _Rollback(Exception):
    pass


@contextmanager
def _preview(manifest: Manifest) -> Iterator[None]:
    """Make changes inside a transaction that is always rolled back."""
    try:
        with manifest.transaction():
            yield
            raise _Rollback
    except _Rollback:
        pass


def _mod_names(manifest: Manifest, game_id: str) -> dict[int, str]:
    return {m.id: m.name for m in manifest.mods(game_id)}


_VERBS = {
    "link": ("linked", "would link", "file"),
    "unlink": ("removed", "would remove", "link"),
    "backup": ("backed up", "would back up", "original game file"),
    "restore": ("restored", "would restore", "original game file"),
    "dropbackup": ("dropped", "would drop", "spare backup copy"),
    "mkdir": ("created", "would create", "folder"),
    "rmdir": ("removed", "would remove", "folder"),
}


def _describe_op(op: deploy.Op, names: dict[int, str]) -> str:
    if op.kind == "link":
        return f"link     {op.path}  ->  {names.get(op.mod_id or 0, '?')}"
    if op.kind == "backup":
        return f"backup   {op.path}  (original moved to {op.source})"
    if op.kind == "restore":
        return f"restore  {op.path}  (from {op.source})"
    if op.kind == "forget":
        return f"forget   {op.path}  ({op.note})"
    if op.kind == "dropbackup":
        return f"drop     {op.source}  (spare copy: the original {op.path} is back in place)"
    if op.kind == "unlink" and op.note:
        return f"unlink   {op.path}  ({op.note})"
    return f"{op.kind:<8} {op.path}"


def _report(result: deploy.SyncResult | None, manifest: Manifest, *, verbose: bool = False) -> int:
    """Print a deploy/purge result; returns 1 if some operations failed."""
    if result is None:
        print("The game is purged, so nothing was linked. Run 'bethmm deploy' to put the mods back.")
        return 0
    names = _mod_names(manifest, result.plan.game_id)
    ops = result.done
    if result.dry_run:
        changes = [op for op in ops if op.kind != "forget"]
        if not changes:
            print("Dry run: nothing would change.")
        else:
            print(f"Dry run: {len(changes)} change(s) would be made in {result.plan.root}:")
            for op in ops:
                print(f"  {_describe_op(op, names)}")
    else:
        counts: dict[str, int] = {}
        for op in ops:
            counts[op.kind] = counts.get(op.kind, 0) + 1
        parts = []
        for kind in ("link", "unlink", "backup", "restore", "dropbackup", "mkdir", "rmdir"):
            if counts.get(kind):
                done, _, noun = _VERBS[kind]
                n = counts[kind]
                parts.append(f"{done} {n} {noun}{'s' if n != 1 else ''}")
        print(("Done: " + ", ".join(parts) + ".") if parts else "Nothing needed to change.")
        if verbose:
            for op in ops:
                print(f"  {_describe_op(op, names)}")
    for path, reason in result.plan.blocked:
        print(f"  ! not deployed: {path}: {reason}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    for op, reason in result.failed:
        print(f"  ! failed: {_describe_op(op, names)}: {reason}")
    return 1 if result.failed else 0


# ---------------------------------------------------------------- commands


def cmd_use(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    settings.current_game = game.game_id
    save_settings(settings)
    print(f"Mod commands now act on {game.name} ({game.root}).")
    return 0


def cmd_install(args) -> int:
    from bethmm.cli import format_plan, link_owners, _resolve_against_game

    settings, games = _context(args)
    installed = [g.game_id for g in games]
    source = Path(args.source).expanduser()
    if not source.exists():
        raise CliError(f"{source} does not exist")
    target = TARGETS.get(args.target) if args.target else None
    try:
        prepared = staging.prepare(source, sevenzip=settings.sevenzip, installed_games=installed,
                                   roots=args.root, target=target)
    except ValueError as exc:
        # Only "can't tell where the files belong" is answered by choosing a target.
        hint = " (add --target data or --target game)" if not args.target and "belong" in str(exc) else ""
        raise CliError(f"{exc}{hint}") from exc
    try:
        plan = prepared.plan
        if plan.target is Target.UNKNOWN or not plan.mappings:
            print(format_plan(plan, None, None, [], installed=installed, source=str(args.source)))
            print()
            if plan.is_fomod:
                raise CliError("this is a FOMOD installer; the guided installer arrives in phase 4. "
                               "For now choose the folders to install with --root (and --target).")
            if not plan.unplaced and not plan.candidate_roots:
                skipped = f" ({len(plan.skipped)} file(s) such as readmes are never installed)" if plan.skipped else ""
                raise CliError(f"the archive contains no files to install{skipped}")
            raise CliError("choose what to install with --root <folder> and --target data|game (see above)")
        detected = _detected_game(plan, settings, installed)
        game = _choose_game(args, settings, games, detected=detected)

        resolved = _resolve_against_game(plan, game, link_owners(game.game_id))
        print(format_plan(plan, game.game_id, game, resolved, installed=installed, source=str(args.source),
                          requested=bool(args.game)))
        for warning in prepared.warnings:
            print(f"  ! {warning}")
        print()
        if args.game:
            if plan.game.game_id and plan.game.game_id != args.game:
                print(f"note: this looks like a {GAMES[plan.game.game_id].name} mod, but you chose {game.name}.")
        elif detected is None:
            print(f"note: the game can't be told from the files; installing into {game.name}"
                  + (" (your current game)." if game.game_id == settings.current_game else "."))
        elif settings.current_game and settings.current_game != game.game_id:
            print(f"note: this looks like a {game.name} mod, so it goes there, not into your current game "
                  f"({GAMES[settings.current_game].name}). Add --game to choose.")

        dry = _dry(args, settings)
        target_ = _target(game, settings)
        with Manifest(lock=not dry) as manifest:
            if args.replace:
                existing: ModRecord | None = _find(manifest, game.game_id, args.replace)
            elif args.new:
                existing = None
            else:
                existing = staging.find_existing(manifest, game.game_id, prepared, args.name)
            name = args.name or (existing.name if existing else staging.suggested_name(prepared))
            if existing is not None:
                old = f" {existing.version}" if existing.version else ""
                new = f" {plan.nexus.version}" if plan.nexus and plan.nexus.version else ""
                print(f"This updates '{existing.name}'{old} to{new or ' the new files'}, keeping its place in the list"
                      + ("." if args.replace else " (use --new to install it separately)."))
                if name != existing.name:
                    print(f"It is renamed to '{name}'.")
                if args.no_deploy:
                    print("It becomes disabled (--no-deploy): its files are removed from the game folder.")
            elif args.no_deploy:
                print(f"'{name}' goes to the bottom of the list, disabled (--no-deploy): nothing of it is linked "
                      "until you enable it.")
            else:
                print(f"'{name}' goes to the bottom of the list, so it wins conflicts with mods already installed.")
            state = manifest.game_state(game.game_id)
            purged = state is not None and not state.deployed
            sync = not purged and (existing is not None or not args.no_deploy)
            if sync:
                deploy.preflight(manifest, target_)  # fail before installing, not after
            if dry:
                return _preview_install(manifest, game.game_id, target_, prepared, existing, name, args, sync)
            verb = "Update" if existing else "Install"
            if not _confirm(f"{verb} '{existing.name if existing else name}' into {game.name}?", args):
                print("Cancelled.")
                return 1
            result = staging.commit(prepared, manifest, game.game_id,
                                    name=args.name if existing else name, replace=existing,
                                    enabled=False if args.no_deploy else None)
            print(f"{'Updated' if result.replaced else 'Installed'} '{result.mod.name}'"
                  + (" (disabled)." if args.no_deploy else "."))
            status = 0
            if sync:
                status = _report(deploy.refresh(manifest, target_), manifest)
            elif purged:
                print("The game is purged, so nothing was linked. Run 'bethmm deploy' to put the mods back.")
            else:
                print(f"Nothing was linked. 'bethmm enable \"{result.mod.name}\"' puts it into the game.")
            if result.old_folder is not None:
                old = str(result.old_folder) + "/"
                still_linked = [d for d in manifest.deployed(game.game_id).values() if d.source.startswith(old)]
                if still_linked:
                    staging.retire(result.old_folder)
                    print(f"  ! kept the previous files in {result.old_folder}: {len(still_linked)} link(s) still "
                          "point there. Run 'bethmm deploy'; then 'bethmm verify --repair' deletes the folder.")
                else:
                    staging.remove_staging(result.old_folder)
            return status
    finally:
        staging.discard(prepared)


def _detected_game(plan, settings: Settings, installed: list[str]) -> str | None:
    """The game the files are for, if they tell. A weak guess (e.g. from .ba2
    archives) still wins over a current game the files can't be for."""
    guess = plan.game
    if guess.game_id and guess.confidence >= Confidence.MEDIUM:
        return guess.game_id
    if guess.candidates and settings.current_game not in guess.candidates:
        return next((g for g in guess.candidates if g in installed), None)
    return None


def _preview_install(manifest: Manifest, game_id: str, target: deploy.GameTarget, prepared: staging.Prepared,
                     existing: ModRecord | None, name: str, args, sync: bool) -> int:
    """Show what installing would do to the game folder: the mod is added
    (pointing at the extracted files) inside a transaction that is rolled back."""
    # An absolute folder: mods_dir / folder is then the extracted files themselves.
    folder = os.path.abspath(prepared.files_dir)
    files = [ModFile(m.source, m.dest) for m in prepared.plan.mappings]
    with _preview(manifest):
        if existing is not None:
            manifest.update_mod(existing.id, folder=folder, files=files, name=name)
            if args.no_deploy:
                manifest.set_enabled(existing.id, False)
        else:
            manifest.add_mod(game_id, name, folder, files, enabled=not args.no_deploy)
        print(f"Nothing was {'updated' if existing else 'installed'} (dry run).")
        if not sync:
            state = manifest.game_state(game_id)
            why = "the game is purged" if state is not None and not state.deployed else "the mod would be disabled"
            print(f"Nothing would be linked: {why}.")
            return 0
        return _report(deploy.apply_plan(manifest, deploy.plan_sync(manifest, target), dry_run=True), manifest)


def cmd_list(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    with Manifest() as manifest:
        mods = manifest.mods(game.game_id)
        summary = deploy.conflict_summary(manifest, game.game_id)
        names = {m.id: m.name for m in mods}
        state = manifest.game_state(game.game_id)
        files = {m.id: len(manifest.mod_files(m.id)) for m in mods}
    if args.json:
        print(json.dumps([{
            "position": i, "id": m.id, "name": m.name, "enabled": m.enabled, "version": m.version,
            "nexus_id": m.nexus_id, "files": files[m.id],
            "overwrites": [names[o] for o in summary.get(m.id, deploy.ModConflicts()).overwrites],
            "overwritten_by": [names[o] for o in summary.get(m.id, deploy.ModConflicts()).overwritten_by],
        } for i, m in enumerate(mods, start=1)], indent=2))
        return 0
    if not mods:
        print(f"No mods installed for {game.name}. Install one with: bethmm install <archive>")
        return 0
    deployed = state is None or state.deployed
    print(f"{game.name} -- {len(mods)} mod(s); lower in the list wins conflicts."
          + ("" if deployed else "  [PURGED: run 'bethmm deploy' to put them back]"))
    width = max(len(m.name) for m in mods)
    for i, m in enumerate(mods, start=1):
        c = summary.get(m.id)
        notes = []
        if c and c.overwrites:
            notes.append("overwrites " + ", ".join(names[o] for o in c.overwrites))
        if c and c.overwritten_by:
            notes.append("overwritten by " + ", ".join(names[o] for o in c.overwritten_by))
        box = "[x]" if m.enabled else "[ ]"
        version = m.version or ""
        count = f"{files[m.id]} file{'' if files[m.id] == 1 else 's'}"
        print(f"{i:>3} {box} {m.name:<{width}}  {version:<10} {count:>11}  {'; '.join(notes)}".rstrip())
    return 0


def _mutate_and_refresh(args, mutate: Callable[[Manifest, deploy.GameTarget], str]) -> int:
    """Run *mutate* (which changes the mod list and returns what it did), then
    redeploy -- or, in a dry run, show the redeploy and roll the change back."""
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    target = _target(game, settings)
    dry = _dry(args, settings)
    with Manifest(lock=not dry) as manifest:
        _preflight(manifest, target)
        if dry:
            with _preview(manifest):
                describe = mutate(manifest, target)
                state = manifest.game_state(game.game_id)
                plan = deploy.plan_sync(manifest, target) if state is None or state.deployed else None
                result = deploy.apply_plan(manifest, plan, dry_run=True) if plan else None
                print(f"(dry run) {describe}")
                return _report(result, manifest)
        describe = mutate(manifest, target)
        print(describe)
        return _report(deploy.refresh(manifest, target), manifest)


def _preflight(manifest: Manifest, target: deploy.GameTarget) -> None:
    """Fail before the mod list changes if the redeploy after it is bound to
    fail (game folder moved, hardlinks across filesystems)."""
    state = manifest.game_state(target.game_id)
    if state is None or state.deployed:
        deploy.preflight(manifest, target)


def cmd_enable(args) -> int:
    def mutate(manifest: Manifest, target: deploy.GameTarget) -> str:
        mods = [_find(manifest, target.game_id, query) for query in args.mods]
        for mod in mods:
            manifest.set_enabled(mod.id, args.enabled)
        word = "Enabled" if args.enabled else "Disabled"
        return f"{word} " + ", ".join(f"'{m.name}'" for m in mods) + "."
    return _mutate_and_refresh(args, mutate)


def _find(manifest: Manifest, game_id: str, query: str) -> ModRecord:
    try:
        return manifest.find_mod(game_id, query)
    except ManifestError as exc:
        raise CliError(str(exc)) from exc


def cmd_move(args) -> int:
    def mutate(manifest: Manifest, target: deploy.GameTarget) -> str:
        mods = manifest.mods(target.game_id)
        mod = _find(manifest, target.game_id, args.mod)
        order = [m.id for m in mods]
        index = order.index(mod.id)
        order.pop(index)
        if args.up:
            new = max(index - 1, 0)
        elif args.down:
            new = min(index + 1, len(order))
        elif args.top:
            new = 0
        elif args.bottom:
            new = len(order)
        else:
            if not 1 <= args.to <= len(mods):
                raise CliError(f"--to must be between 1 and {len(mods)}")
            new = args.to - 1
        order.insert(new, mod.id)
        manifest.set_order(target.game_id, order)
        return f"Moved '{mod.name}' to position {new + 1} of {len(mods)}."
    return _mutate_and_refresh(args, mutate)


def cmd_uninstall(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    target = _target(game, settings)
    dry = _dry(args, settings)
    with Manifest(lock=not dry) as manifest:
        mods = [_find(manifest, game.game_id, q) for q in args.mods]
        names = ", ".join(f"'{m.name}'" for m in mods)
        _preflight(manifest, target)
        if dry:
            with _preview(manifest):
                for mod in mods:
                    manifest.set_enabled(mod.id, False)
                state = manifest.game_state(game.game_id)
                result = deploy.apply_plan(manifest, deploy.plan_sync(manifest, target), dry_run=True) \
                    if state is None or state.deployed else None
                print(f"Would uninstall {names} and delete their staged files.")
                return _report(result, manifest)
        if not _confirm(f"Uninstall {names} from {game.name}? Their staged files are deleted.", args):
            print("Cancelled.")
            return 1
        for mod in mods:
            manifest.set_enabled(mod.id, False)
        result = deploy.refresh(manifest, target)
        status = _report(result, manifest) if result is not None else 0
        for mod in mods:
            try:
                manifest.remove_mod(mod.id)
            except ManifestError:
                print(f"  ! '{mod.name}' still has links that could not be removed; it stays installed (disabled). "
                      "Run 'bethmm verify --repair'.")
                status = 1
                continue
            staging.remove_staging(mod.staging_dir())
            print(f"Uninstalled '{mod.name}'.")
        return status


def cmd_conflicts(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    with Manifest() as manifest:
        names = _mod_names(manifest, game.game_id)
        desired, providers = deploy.desired_files(manifest, game.game_id)
        deployed = manifest.deployed(game.game_id)
        only = _find(manifest, game.game_id, args.mod).id if args.mod else None
        summary = deploy.conflict_summary(manifest, game.game_id)
    shown = [mid for mid in names if only in (None, mid) and mid in summary]
    if not shown:
        print("No conflicts between enabled mods." if only is None else f"'{names[only]}' has no conflicts.")
        return 0
    for mid in shown:
        c = summary[mid]
        print(names[mid])
        for other, n in c.overwrites.items():
            print(f"  overwrites {n} file(s) of {names[other]}")
        for other, n in c.overwritten_by.items():
            print(f"  overwritten by {names[other]} in {n} file(s)")
    if args.files:
        print("\nFiles provided by more than one enabled mod (the last one wins):")
        for key, mod_ids in sorted(providers.items()):
            if len(mod_ids) > 1 and (only is None or only in mod_ids):
                where = deployed[key].path if key in deployed else desired[key][1].dest
                print(f"  {where}: {' < '.join(names[m] for m in mod_ids)}")
    return 0


def cmd_deploy(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    dry = _dry(args, settings)
    with Manifest(lock=not dry) as manifest:
        try:
            result = deploy.deploy(manifest, _target(game, settings), dry_run=dry)
        except deploy.DeployError as exc:
            raise CliError(str(exc)) from exc
        return _report(result, manifest, verbose=args.verbose)


def cmd_purge(args) -> int:
    settings, games = _context(args)
    dry = _dry(args, settings)
    with Manifest(lock=not dry) as manifest:
        if args.all:
            game_ids = [g for g in manifest.game_ids() if manifest.has_footprint(g)]
            if not game_ids:
                print("Nothing is deployed in any game folder.")
                return 0
        else:
            game_ids = [_choose_game(args, settings, games).game_id]
        by_id = {g.game_id: g for g in games}
        if not dry and not _confirm(
            f"Remove everything the app put in the folder of {', '.join(GAMES[g].name for g in game_ids)}?", args
        ):
            print("Cancelled.")
            return 1
        status = 0
        for game_id in game_ids:
            state = manifest.game_state(game_id)
            root = by_id[game_id].root if game_id in by_id else Path(state.root) if state else None
            if root is None:
                print(f"{GAMES[game_id].name}: nothing recorded.")
                continue
            target = deploy.GameTarget(game_id, Path(root), settings.override(game_id).link_mode)
            print(f"{GAMES[game_id].name}:")
            try:
                status |= _report(deploy.purge(manifest, target, dry_run=dry), manifest, verbose=args.verbose)
            except deploy.DeployError as exc:
                print(f"  ! {exc}")
                status = 1
            if not dry and game_id in by_id:
                for line in winecheck.remove_leftovers(game_id, by_id[game_id].data_dir):
                    print(f"  {line}")
        if not dry and status == 0:
            print("Mods stay installed; 'bethmm deploy' puts them back.")
        return status


def cmd_verify(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    target = _target(game, settings)
    dry = _dry(args, settings)
    if args.release_held and not args.repair:
        raise CliError("--release-held goes with --repair")
    with Manifest(lock=args.repair and not dry) as manifest:
        report = deploy.verify(manifest, target)
        retired, unknown = _unused_staging(manifest, game.game_id)
        problems = not report.ok or bool(retired)
        if not problems:
            print(f"{game.name}: everything the app deployed is in place.")
        else:
            print(f"{game.name}: found problems:")
        for record in report.missing:
            print(f"  missing link: {record.path}")
        for record in report.foreign:
            print(f"  replaced by another file (left alone): {record.path}")
        for rel in report.orphan_links:
            print(f"  stray link into the mods folder: {rel}")
        for backup in report.missing_backups:
            print(f"  backup file gone: {backup.backup} (original of {backup.path})")
        for full in report.orphan_backups:
            print(f"  unrecorded backup: {full}")
        for backup in report.held_backups:
            print(f"  original kept in the backups because another file is at its path: {backup.path} "
                  f"({backup.backup})")
        for full in report.temp_leftovers:
            print(f"  half-copied file from an interrupted move: {full}")
        for path in report.missing_dirs:
            print(f"  folder gone: {path}")
        for entry in report.interrupted:
            print(f"  interrupted operation: {entry.action} {entry.path or ''} ({entry.ts})")
        for folder in retired:
            print(f"  previous version of a mod that nothing uses any more: {folder}")
        for path in report.left_alone:
            print(f"  note: {path} is a file the app didn't create, where a mod's link was; it is left alone "
                  "(delete it to let the mod link there again)")
        for folder in unknown:
            print(f"  note: {folder} is in the mods folder, but no installed mod uses it; the app leaves it alone")
        if not problems:
            if args.repair and not dry:
                for line in winecheck.remove_leftovers(game.game_id, game.data_dir):
                    print(f"  {line}")
            return 0
        if not args.repair:
            print("Run 'bethmm verify --repair' to fix what can be fixed safely.")
            return 1
        for line in deploy.repair(manifest, target, report, dry_run=dry, release_held=args.release_held):
            print(f"  {'would ' if dry else ''}{line}")
        state = manifest.game_state(game.game_id)
        purged = state is not None and not state.deployed
        if dry:
            for folder in retired:
                print(f"  would delete {folder}")
            print(f"  then it would {'purge again (to restore originals)' if purged else 'redeploy'}; after the "
                  f"repair, 'bethmm {'purge' if purged else 'deploy'} --dry-run' shows exactly what that changes.")
            return 0
        for line in winecheck.remove_leftovers(game.game_id, game.data_dir):
            print(f"  {line}")
        # The repair can free originals (e.g. a half-made link is gone): a purged
        # game gets them back with a purge, a deployed one is brought up to date.
        try:
            result = deploy.purge(manifest, target) if purged else deploy.refresh(manifest, target)
        except deploy.DeployError as exc:
            print(f"  ! {exc}")
            return 1
        status = _report(result, manifest)
        for folder in _unused_staging(manifest, game.game_id)[0]:
            staging.remove_staging(folder)
            print(f"  deleted {folder}")
        return status


def _unused_staging(manifest: Manifest, game_id: str) -> tuple[list[Path], list[Path]]:
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
    unused = [
        p for p in entries
        if p.name not in used and p.name != winecheck.TEST_DIR
        and not any(src.startswith(str(p) + "/") for src in sources)
    ]
    retired = [p for p in unused if staging.is_retired(p)]
    return retired, [p for p in unused if p not in retired]


def cmd_status(args) -> int:
    with Manifest() as manifest:
        game_ids = manifest.game_ids()
        if args.deployed_only:
            return _deployed_only(manifest, game_ids)
        if not game_ids:
            print("No mods installed yet.")
            return 0
        for g in game_ids:
            mods = manifest.mods(g)
            state = manifest.game_state(g)
            links = len(manifest.deployed(g))
            backups = len(manifest.backups(g))
            name = GAMES[g].name if g in GAMES else g
            flag = "deployed" if state is None or state.deployed else "purged"
            print(f"{name}: {len(mods)} mod(s), {sum(m.enabled for m in mods)} enabled; {links} link(s), "
                  f"{backups} original(s) backed up [{flag}]" + (f"\n  folder: {state.root}" if state else ""))
    return 0


def _deployed_only(manifest: Manifest, game_ids: list[str]) -> int:
    """What the app still has in game folders; 1 if a purge has anything to do."""
    busy = False
    for g in game_ids:
        links, backups = manifest.deployed(g), manifest.backups(g)
        if not links and not backups:
            continue
        state = manifest.game_state(g)
        root = Path(state.root) if state is not None else None
        held: list = []
        if root is not None and root.is_dir():
            held = deploy.verify(manifest, deploy.GameTarget(g, root)).held_backups
        parts = []
        if links:
            parts.append(f"{len(links)} link(s) in {root or 'the game folder'}")
        restorable = len(backups) - len(held)
        if restorable:
            parts.append(f"{restorable} original game file(s) in the app's backups that a purge puts back")
        if held:
            parts.append(f"{len(held)} original(s) kept in the backups because a newer file is at their path "
                         "(safe to leave; 'bethmm verify --repair --release-held' stops tracking them)")
        print(f"{GAMES[g].name if g in GAMES else g}: " + "; ".join(parts))
        busy = busy or bool(links) or restorable > 0
    if busy:
        print("Run 'bethmm purge --all' to put the game folders back as they were.")
    return 1 if busy else 0


def cmd_check_links(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    if _dry(args, settings):
        print(f"Dry run: would put a test link in {game.data_dir}, have Wine read it, and remove it again.")
        return 0
    print(f"Testing whether Wine follows links in {game.data_dir} ...")
    # Holding the lock keeps a concurrent 'verify --repair' from removing the test link.
    with Manifest(lock=True):
        result = winecheck.check_links(game, link_mode=settings.override(game.game_id).link_mode,
                                       wine=args.wine, timeout=args.timeout)
    print(result.message)
    if result.wine:
        print(f"  wine: {result.wine}")
    if result.output and result.ok is not True:
        print("  output: " + result.output.replace("\n", "\n          "))
    return 0 if result.ok else 1


def cmd_history(args) -> int:
    settings = load_settings()
    game_id = getattr(args, "game", None) or settings.current_game
    with Manifest() as manifest:
        entries = manifest.history(game_id, limit=args.n)
    if not entries:
        print("No file operations recorded yet.")
        return 0
    for e in entries:
        extra = f" -> {e.source}" if e.source and e.action in ("link", "backup") else ""
        problem = f"  [{e.status}: {e.detail}]" if e.status not in ("done",) else ""
        print(f"{e.ts}  {e.game_id or '-':<9} {e.action:<8} {e.path or ''}{extra}{problem}")
    return 0
