"""Mod management commands (phase 2): install, list, enable/disable, move,
uninstall, conflicts, deploy, purge, verify, status, check-links, history, use.

Every command that changes a game folder goes through ``deploy`` and accepts
``--dry-run`` (also switched on for everything by ``bethmm config --dry-run on``),
which prints what would happen and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from bethmm import deploy, describe, service, staging, winecheck
from bethmm.discovery import DiscoveredGame, get_games
from bethmm.games import GAMES
from bethmm.manifest import Manifest, ManifestError, ModRecord
from bethmm.placement import Target
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
    p.add_argument("--forget-missing", action="store_true",
                   help="the game folder is gone for good (not just on a drive that isn't mounted): forget the "
                        "app's links there and keep the originals it backed up in backups/<game>/.superseded/")
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
                   help="list what the app still has in game folders; exit with status 3 if a purge has anything "
                        "to do (for scripts)")
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

#: Exit status of 'status --deployed-only' when a purge has something to do
#: (install.sh tells it apart from the app failing to run).
DEPLOYED_STATUS = 3


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
    try:
        reply = input(f"{question} [y/N] ").strip().lower()
    except EOFError:  # Ctrl-D: no
        print()
        return False
    if reply not in ("y", "yes"):
        return False
    if load_settings().dry_run:  # switched on elsewhere while the question was open
        raise CliError("dry-run mode was switched on while this was waiting for an answer; nothing was changed")
    return True


def _mod_names(manifest: Manifest, game_id: str) -> dict[int, str]:
    return {m.id: m.name for m in manifest.mods(game_id)}


_describe_op = describe.describe_op


def _report(result: deploy.SyncResult | None, manifest: Manifest, *, verbose: bool = False,
            names: dict[int, str] | None = None) -> int:
    """Print a deploy/purge result; returns 1 if some operations failed."""
    if result is None:
        print("The game is purged, so nothing was linked. Run 'bethmm deploy' to put the mods back.")
        return 0
    names = names if names is not None else _mod_names(manifest, result.plan.game_id)
    ops = result.done
    if result.forgotten:
        state = manifest.game_state(result.plan.game_id)
        where = state.root if state is not None else result.plan.root
        print(f"{'Dry run: would forget' if result.dry_run else 'Forgot'} what the app had in {where} "
              "(that install is gone):")
        for note in result.forgotten:
            print(f"  {note}")
        return 0
    if result.dry_run:
        changes = [op for op in ops if op.kind != "forget"]
        if not changes:
            print("Dry run: nothing would change.")
        else:
            print(f"Dry run: {len(changes)} change(s) would be made in {result.plan.root}:")
            for op in ops:
                print(f"  {_describe_op(op, names)}")
    else:
        done = describe.summary(ops)
        print(f"Done: {done}." if done else "Nothing needed to change.")
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
    from bethmm.cli import format_plan

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
        problem = service.installable_problem(plan)
        if problem is not None:
            print(format_plan(plan, None, None, [], installed=installed, source=str(args.source)))
            print()
            if plan.is_fomod:
                raise CliError("this is a FOMOD installer; the guided installer arrives in phase 4. "
                               "For now choose the folders to install with --root (and --target).")
            if not plan.unplaced and not plan.candidate_roots:
                raise CliError(problem)
            raise CliError("choose what to install with --root <folder> and --target data|game (see above)")
        detected = service.detected_game(plan, settings, installed)
        game = _choose_game(args, settings, games, detected=detected)

        dry = _dry(args, settings)
        target_ = _target(game, settings)
        with Manifest(lock=not dry) as manifest:
            replace_mod = _find(manifest, game.game_id, args.replace) if args.replace else None
            choice = service.choose_install(manifest, game.game_id, prepared, name=args.name, new=args.new,
                                            replace=replace_mod, disabled=args.no_deploy)
            existing, name = choice.existing, choice.name

            resolved = service.resolve_against_game(plan, game, service.link_owners(game.game_id, manifest),
                                                    replacing=existing.id if existing else None)
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
                if choice.same_name:
                    print(f"  (a mod called '{choice.same_name}' is installed too; it doesn't look like the same mod "
                          f"-- use --replace \"{choice.same_name}\" if this is its new version)")
            if choice.sync:
                service.preflight(manifest, target_)  # fail before installing, not after
            if dry:
                return _preview_install(manifest, target_, prepared, choice)
            verb = "Update" if existing else "Install"
            if not _confirm(f"{verb} '{existing.name if existing else name}' into {game.name}?", args):
                print("Cancelled.")
                return 1
            result = service.install(manifest, target_, prepared, choice)
            mod = result.commit.mod
            print(f"{'Updated' if result.commit.replaced else 'Installed'} '{mod.name}'"
                  + (" (disabled)." if args.no_deploy else "."))
            status = 0
            if choice.sync:
                status = _report(result.sync, manifest)
            elif choice.purged:
                print("The game is purged, so nothing was linked. Run 'bethmm deploy' to put the mods back.")
            else:
                print(f"Nothing was linked. 'bethmm enable \"{mod.name}\"' puts it into the game.")
            if result.cleanup_error is not None:
                print(f"  ! could not delete the previous files in {result.kept_old_folder}: {result.cleanup_error}. "
                      "'bethmm verify --repair' deletes them once that is fixed.")
            elif result.kept_old_folder is not None:
                print(f"  ! kept the previous files in {result.kept_old_folder}: {result.still_linked} link(s) still "
                      "point there. Run 'bethmm deploy'; then 'bethmm verify --repair' deletes the folder.")
            return status
    finally:
        staging.discard(prepared)


def _preview_install(manifest: Manifest, target: deploy.GameTarget, prepared: staging.Prepared,
                     choice: service.InstallChoice) -> int:
    """Show what installing would do to the game folder."""
    result, mod_id = service.preview_install(manifest, target, prepared, choice)
    print(f"Nothing was {'updated' if choice.existing else 'installed'} (dry run).")
    if result is None:
        why = "the game is purged" if choice.purged else "the mod would be disabled"
        print(f"Nothing would be linked: {why}.")
        return 0
    return _report(result, manifest, names={**_mod_names(manifest, choice.game_id), mod_id: choice.name})


def cmd_list(args) -> int:
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    with Manifest() as manifest:
        rows = service.mod_rows(manifest, game.game_id)
        state = manifest.game_state(game.game_id)
    if args.json:
        print(json.dumps([{
            "position": r.position, "id": r.mod.id, "name": r.mod.name, "enabled": r.mod.enabled,
            "version": r.mod.version, "nexus_id": r.mod.nexus_id, "files": r.files,
            "overwrites": r.overwrites, "overwritten_by": r.overwritten_by,
        } for r in rows], indent=2))
        return 0
    if not rows:
        print(f"No mods installed for {game.name}. Install one with: bethmm install <archive>")
        return 0
    deployed = state is None or state.deployed
    print(f"{game.name} -- {len(rows)} mod(s); lower in the list wins conflicts."
          + ("" if deployed else "  [PURGED: run 'bethmm deploy' to put them back]"))
    width = max(len(r.mod.name) for r in rows)
    for r in rows:
        notes = []
        if r.overwrites:
            notes.append("overwrites " + ", ".join(r.overwrites))
        if r.overwritten_by:
            notes.append("overwritten by " + ", ".join(r.overwritten_by))
        box = "[x]" if r.mod.enabled else "[ ]"
        version = r.mod.version or ""
        count = f"{r.files} file{'' if r.files == 1 else 's'}"
        print(f"{r.position:>3} {box} {r.mod.name:<{width}}  {version:<10} {count:>11}  {'; '.join(notes)}".rstrip())
    return 0


def _mutate_and_refresh(args, mutate: Callable[[Manifest, deploy.GameTarget], str]) -> int:
    """Run *mutate* (which changes the mod list and returns what it did), then
    redeploy -- or, in a dry run, show the redeploy and roll the change back."""
    settings, games = _context(args)
    game = _choose_game(args, settings, games)
    target = _target(game, settings)
    dry = _dry(args, settings)
    with Manifest(lock=not dry) as manifest:
        def change(m: Manifest) -> str:
            return mutate(m, target)

        if dry:
            describe, result = service.preview_change(manifest, target, change)
            print(f"(dry run) {describe}")
            return _report(result, manifest)
        describe, result = service.apply_change(manifest, target, change)
        print(describe)
        return _report(result, manifest)


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
        service.preflight(manifest, target)
        if dry:
            result = service.preview_uninstall(manifest, target, mods)
            print(f"Would uninstall {names} and delete their staged files.")
            return _report(result, manifest)
        if not _confirm(f"Uninstall {names} from {game.name}? Their staged files are deleted.", args):
            print("Cancelled.")
            return 1
        outcome = service.uninstall(manifest, target, mods)
        status = _report(outcome.sync, manifest) if outcome.sync is not None else 0
        kept = {mod.id: folder for mod, folder in outcome.kept_folders}
        stuck = {mod.id for mod in outcome.still_linked}
        not_deleted = {mod.id: (folder, why) for mod, folder, why in outcome.not_deleted}
        for mod in mods:
            if mod.id in stuck:
                print(f"  ! '{mod.name}' still has links that could not be removed; it stays installed (disabled). "
                      "Run 'bethmm verify --repair'.")
                status = 1
                continue
            if mod.id in kept:
                print(f"  ! kept the files of '{mod.name}' in {kept[mod.id]}: an interrupted run may have linked "
                      "them; 'bethmm verify --repair' deletes the folder once nothing uses it.")
            if mod.id in not_deleted:
                folder, why = not_deleted[mod.id]
                print(f"  ! could not delete the files of '{mod.name}' in {folder}: {why}. "
                      "'bethmm verify --repair' deletes them once that is fixed.")
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
                where = deployed[key].path if key in deployed else desired[key][1].dest if key in desired else key
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
            game_ids = [_recorded_game(args, settings, games, manifest)]
        by_id = {g.game_id: g for g in games}
        if not dry and not _confirm(
            f"Remove everything the app put in the folder of {', '.join(GAMES[g].name for g in game_ids)}?", args
        ):
            print("Cancelled.")
            return 1
        status = 0
        forgot = False
        for game_id in game_ids:
            state = manifest.game_state(game_id)
            root = by_id[game_id].root if game_id in by_id else Path(state.root) if state else None
            if root is None:
                print(f"{GAMES[game_id].name}: nothing recorded.")
                continue
            target = deploy.GameTarget(game_id, Path(root), settings.override(game_id).link_mode)
            print(f"{GAMES[game_id].name}:")
            try:
                result = deploy.purge(manifest, target, dry_run=dry, forget_missing=args.forget_missing)
                status |= _report(result, manifest, verbose=args.verbose)
                forgot = forgot or bool(result.forgotten)
            except deploy.DeployError as exc:
                print(f"  ! {exc}")
                status = 1
            if not dry and game_id in by_id:
                for line in winecheck.remove_leftovers(game_id, by_id[game_id].data_dir):
                    print(f"  {line}")
        if not dry and status == 0:
            print("Mods stay installed." + ("" if forgot else " 'bethmm deploy' puts them back."))
        return status


def _recorded_game(args, settings: Settings, games: list[DiscoveredGame], manifest: Manifest) -> str:
    """The game a command is for -- also one that is no longer found but the
    app still has records of (its folder is gone, or its drive isn't mounted)."""
    try:
        return _choose_game(args, settings, games).game_id
    except CliError:
        wanted = getattr(args, "game", None) or settings.current_game
        if wanted is None or manifest.game_state(wanted) is None:
            raise
        return wanted


def cmd_verify(args) -> int:
    settings, games = _context(args)
    dry = _dry(args, settings)
    if args.release_held and not args.repair:
        raise CliError("--release-held goes with --repair")
    with Manifest(lock=args.repair and not dry) as manifest:
        game_id = _recorded_game(args, settings, games, manifest)
        found_game = next((g for g in games if g.game_id == game_id), None)
        name = GAMES[game_id].name
        if found_game is not None:
            target = _target(found_game, settings)
        else:  # no longer found: check where the mods were deployed
            target = deploy.GameTarget(game_id, Path(manifest.game_state(game_id).root),
                                       settings.override(game_id).link_mode)

        def remove_leftovers() -> None:
            for line in winecheck.remove_leftovers(game_id, found_game.data_dir) if found_game is not None else ():
                print(f"  {line}")

        found = service.check(manifest, target)
        report, retired, unknown, problems = found.report, found.retired, found.unknown, found.problems
        if report.root_missing:
            print(f"{name}: the game folder can't be used ({report.missing_reason}), so nothing was checked.")
            print("  If it is on a drive that isn't mounted, mount it and run this again. If you moved or reinstalled")
            print("  the game, 'bethmm games --rescan' finds it again. If that install is gone,")
            print(f"  'bethmm purge --forget-missing --game {game_id}' forgets the app's links there.")
            return 1
        for note in report.notes:
            print(f"{name}: note: {note}")
        if not problems:
            print(f"{name}: everything the app deployed is in place.")
        else:
            print(f"{name}: found problems:")
        if report.moved_from is not None:
            why = deploy.why_gone(report.moved_from, game_id, manifest) or "it holds none of the app's links"
            print(f"  note: the mods were deployed into {report.moved_from} ({why}); the app's links are in "
                  f"{report.root}, so that was checked")
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
            print(f"  file left by an interrupted move: {full}")
        for backup in report.restorable:
            print(f"  original not back in the game folder yet: {backup.path} ({backup.backup})")
        for path in report.missing_dirs:
            print(f"  folder gone: {path}")
        for entry in report.interrupted:
            print(f"  interrupted operation: {entry.action} {entry.path or ''} ({entry.ts})")
        for folder in retired:
            print(f"  mod files nothing uses any more (a previous version, an uninstalled mod or an "
                  f"interrupted install; --repair deletes them): {folder}")
        if report.out_of_date:
            some = ", ".join(report.out_of_date[:3]) + (", ..." if len(report.out_of_date) > 3 else "")
            print(f"  not up to date with the mod list (a change was saved but not deployed, e.g. the app was "
                  f"stopped): a deploy changes {len(report.out_of_date)} file(s): {some}")
        for path in report.left_alone:
            print(f"  note: {path} is a file the app didn't create, where a mod's link was; it is left alone "
                  "(delete it to let the mod link there again)")
        for old, now in report.case_moved:
            print(f"  note: the link {old} is now at {now} (a folder was renamed); the next deploy records that")
        for folder in unknown:
            print(f"  note: {folder} is in the mods folder, but no installed mod uses it; the app leaves it alone")
        if report.links_elsewhere:
            print(f"  {len(report.links_elsewhere)} link(s) into the mods folder are in {target.root}, which is not "
                  f"the folder the mods were deployed into ({report.root}); left alone, e.g. "
                  f"{report.links_elsewhere[0]}. If that is the install you play, purge here and deploy there.")
        if not problems:
            if args.repair and not dry:
                remove_leftovers()
            return 0
        if not args.repair:
            print("Run 'bethmm verify --repair' to fix what can be fixed safely.")
            return 1
        outcome = service.repair(manifest, target, found, release_held=args.release_held, dry_run=dry,
                                 data_dir=found_game.data_dir if found_game is not None else None)
        for line in outcome.actions:
            print(f"  {'would ' if dry else ''}{line}")
        if dry:
            for folder in outcome.deleted:
                print(f"  would delete {folder}")
            purged = outcome.purged
            print(f"  then it would {'purge again (to restore originals)' if purged else 'redeploy'}; after the "
                  f"repair, 'bethmm {'purge' if purged else 'deploy'} --dry-run' shows exactly what that changes.")
            return 0
        for line in outcome.leftovers:
            print(f"  {line}")
        # The repair can free originals (e.g. a half-made link is gone): a purged
        # game gets them back with a purge, a deployed one is brought up to date.
        if outcome.sync_error is not None:
            print(f"  ! {outcome.sync_error}")
            return 1
        status = _report(outcome.sync, manifest)
        for folder in outcome.deleted:
            print(f"  deleted {folder}")
        for folder, why in outcome.not_deleted:
            print(f"  ! could not delete {folder}: {why}")
            status = 1
        return status


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
    """What the app still has in game folders; DEPLOYED_STATUS if a purge has anything to do."""
    busy = False
    for fp in service.footprints(manifest):
        parts = []
        if fp.links:
            parts.append(f"{fp.links} link(s) in {fp.root or 'the game folder'}")
        if fp.unfinished:
            parts.append(f"{fp.unfinished} operation(s) of an interrupted run that a purge cleans up")
        if fp.restorable:
            parts.append(f"{fp.restorable} original game file(s) in the app's backups that a purge puts back")
        if fp.held:
            parts.append(f"{fp.held} original(s) kept in the backups because a newer file is at their path "
                         "(safe to leave; 'bethmm verify --repair --release-held' stops tracking them)")
        if fp.unusable:
            parts.append(f"the game folder can't be used now ({fp.unusable})")
        print(f"{fp.name}: " + "; ".join(parts))
        busy = busy or fp.busy
    if busy:
        print("Run 'bethmm purge --all' to put the game folders back as they were.")
    return DEPLOYED_STATUS if busy else 0


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
