"""Command-line interface.

Read-only commands:

    bethmm games [--rescan] [--json]
    bethmm analyze <archive-or-folder> [--game ID] [--root DIR]... [--target data|game] [--json]
    bethmm plugin-info <plugin>...
    bethmm list | conflicts | status | history | verify     (see cli_mods)

Settings:

    bethmm use <game>
    bethmm config [--games-dir PATH] [--scan-depth N] [--7z PATH] [--dry-run on|off]
    bethmm override <game> [--root PATH] [--prefix PATH] [--wine-user NAME]
                           [--link-mode symlink|hardlink] [--ignore|--no-ignore] [--clear]

Commands that change a game folder (all accept --dry-run): install, enable,
disable, move, uninstall, deploy, purge, verify --repair -- see ``cli_mods``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from bethmm import APP_NAME, __version__, deploy, paths
from bethmm.archive import ArchiveError, extract_archive
from bethmm.deploy import DeployError
from bethmm.manifest import BUSY_MESSAGE, Manifest, ManifestError
from bethmm.staging import StagingError
from bethmm.discovery import DiscoveredGame, cache_is_current, get_games
from bethmm.games import GAMES
from bethmm.log import setup_logging
from bethmm.pathresolve import CaseResolver, fold_path
from bethmm.placement import (
    Confidence,
    PlacementPlan,
    Rule,
    Target,
    analyze_files,
    plan_for_roots,
    scan_directory,
)
from bethmm.plugin_header import PluginHeaderError, candidate_games, read_plugin_header
from bethmm.settings import LINK_MODES, GameOverride, Settings, SettingsError, load_settings, save_settings

log = logging.getLogger(__name__)

#: Leftover analyze-* extraction folders older than this are removed.
STALE_TMP_SECONDS = 6 * 3600


class CliError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    # File names from archives need not be valid UTF-8; never crash printing them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        setup_logging()
    except OSError as exc:
        print(f"warning: logging disabled: {exc}", file=sys.stderr)
    previous = {sig: signal.signal(sig, _exit_on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return args.func(args) or 0
    except (CliError, ArchiveError, SettingsError, DeployError, ManifestError, StagingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        busy = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
        print(f"error: {BUSY_MESSAGE if busy else f'the manifest: {exc}'}", file=sys.stderr)
        return 1
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        log.error("%s%s", exc.strerror or exc, where)
        print(f"error: {exc.strerror or exc}{where}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _exit_on_signal(signum, _frame) -> None:
    # Raising here unwinds through the finally blocks that remove temp folders.
    raise SystemExit(128 + signum)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bethmm", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(metavar="command")

    p = sub.add_parser("games", help="list the Bethesda games found under Lutris")
    p.add_argument("--rescan", action="store_true", help="ignore the cache and search again")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_games)

    p = sub.add_parser("analyze", help="show where a mod archive's files would go (changes nothing)")
    p.add_argument("path", help="a .zip/.7z/.rar archive or an already-extracted folder")
    p.add_argument("--game", choices=sorted(GAMES), help="check against this game instead of the detected one")
    p.add_argument("--root", action="append", metavar="DIR",
                   help="install this folder of the archive instead of the detected one (repeatable)")
    p.add_argument("--target", choices=("data", "game"),
                   help="put the root into the Data folder or the game folder instead of the detected place")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("plugin-info", help="show a plugin's masters and which game it is for")
    p.add_argument("plugins", nargs="+")
    p.set_defaults(func=cmd_plugin_info)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("--games-dir", help="Lutris games folder to scan (default ~/Games)")
    p.add_argument("--scan-depth", type=int, help="folder levels to search below each scan root")
    p.add_argument("--7z", dest="sevenzip", help="path to the 7z binary ('' = search PATH)")
    p.add_argument("--dry-run", choices=("on", "off"),
                   help="'on' makes every command only show what it would do")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("override", help="manually set or clear a game's paths")
    p.add_argument("game", choices=sorted(GAMES))
    p.add_argument("--root", help="game folder (the one containing the game's .exe)")
    p.add_argument("--prefix", help="Wine prefix (the folder containing drive_c)")
    p.add_argument("--wine-user", help="user folder name inside drive_c/users")
    p.add_argument("--link-mode", choices=LINK_MODES)
    p.add_argument("--ignore", dest="ignore", action="store_true", default=None, help="hide this game")
    p.add_argument("--no-ignore", dest="ignore", action="store_false")
    p.add_argument("--clear", action="store_true", help="remove every override for this game")
    p.set_defaults(func=cmd_override)

    from bethmm import cli_mods

    cli_mods.register(sub, CliError)
    return parser


# ---------------------------------------------------------------- games


def cmd_games(args) -> int:
    settings = load_settings()
    from_cache = not args.rescan and cache_is_current(settings)
    games, warnings = get_games(settings, rescan=args.rescan)
    if args.json:
        print(json.dumps({"games": [g.to_dict() for g in games], "warnings": warnings}, indent=2))
        return 0
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not games:
        print(f"No games found. Searched {settings.games_dir} and Lutris's game list.")
        print("Set a path manually with:  bethmm override <game> --root /path/to/game")
        return 0
    for g in games:
        print(_describe_game(g))
        print()
    if from_cache:
        print("(cached results; run 'bethmm games --rescan' to search again)")
    return 0


def _describe_game(g: DiscoveredGame) -> str:
    def mark(path: Path | None) -> str:
        if path is None:
            return "unknown"
        return f"{path}" + ("" if path.exists() else "  (does not exist yet)")

    lines = [
        f"{g.name}  [{g.game_id}]",
        f"  game folder : {g.root}",
        f"  executable  : {g.exe.name}",
        f"  data folder : {mark(g.data_dir)}",
        f"  wine prefix : {mark(g.prefix)}",
        f"  wine user   : {g.wine_user or 'unknown'}",
        f"  plugins.txt : {mark(g.plugins_txt)}",
        f"  store       : {g.store}",
        f"  found via   : {g.source}" + (f" (Lutris: {g.lutris_name or g.lutris_slug})" if g.lutris_slug or g.lutris_name else ""),
    ]
    for alt in g.alternatives:
        lines.append(f"  also found  : {alt}")
    for w in g.warnings:
        lines.append(f"  warning     : {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------- analyze


def cmd_analyze(args) -> int:
    settings = load_settings()
    games, _ = get_games(settings)
    installed = [g.game_id for g in games]
    source = Path(args.path).expanduser()
    if source.is_dir():
        plan = _plan(source, args, installed, source.name)
    elif source.is_file():
        plan = _analyze_archive(source, args, settings, installed)
    else:
        raise CliError(f"{source} does not exist")

    game_id = args.game or plan.game.game_id
    game = next((g for g in games if g.game_id == game_id), None)
    resolved = _resolve_against_game(plan, game, link_owners(game.game_id)) if game else []

    if args.json:
        data = plan.to_dict()
        data["checked_against"] = str(game.root) if game else None
        data["resolved"] = resolved
        print(json.dumps(data, indent=2))
    else:
        print(format_plan(plan, game_id, game, resolved, installed=installed, source=str(args.path)))
    return 0


def _plan(folder: Path, args, installed: list[str], archive_name: str) -> PlacementPlan:
    """Automatic plan for an extracted mod, or the user's --root/--target choice."""
    files, reader, warnings = scan_directory(folder)
    common = dict(header_reader=reader, installed_games=installed, archive_name=archive_name)
    plan = analyze_files(files, **common)
    if args.root or args.target:
        target = {"data": Target.DATA, "game": Target.GAME_ROOT, None: None}[args.target]
        roots = args.root or [plan.root]
        try:
            plan = plan_for_roots(files, roots, target, **common)
        except ValueError as exc:
            hint = "" if args.target else " (add --target data or --target game)"
            raise CliError(f"{exc}{hint}") from exc
    plan.warnings[:0] = warnings
    return plan


def _analyze_archive(archive: Path, args, settings: Settings, installed: list[str]) -> PlacementPlan:
    tmp = paths.tmp_dir()
    tmp.mkdir(parents=True, exist_ok=True)
    _remove_stale_extractions(tmp)
    workdir = Path(tempfile.mkdtemp(prefix="analyze-", dir=tmp))
    log.info("MKDIR %s (temporary extraction)", workdir)
    try:
        result = extract_archive(archive, workdir / "x", settings.sevenzip)
        plan = _plan(workdir / "x", args, installed, archive.name)
        plan.warnings.extend(result.warnings)
        return plan
    finally:
        _remove_tree(workdir)


def _remove_tree(folder: Path) -> None:
    """Delete one of our own temporary folders, logging what happened."""
    def on_error(_func, path, exc_info) -> None:
        log.warning("could not delete %s: %s", path, exc_info[1])

    shutil.rmtree(folder, onerror=on_error)
    log.info("DELETE %s (temporary extraction)", folder)


def _remove_stale_extractions(tmp: Path) -> None:
    """Remove analyze-*/install-* folders left behind by a run that was killed."""
    cutoff = time.time() - STALE_TMP_SECONDS
    try:
        entries = list(os.scandir(tmp))
    except OSError:
        return
    for entry in entries:
        try:
            stale = entry.name.startswith(("analyze-", "install-")) and entry.is_dir(follow_symlinks=False) \
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
        except OSError:
            continue
        if stale:
            _remove_tree(Path(entry.path))


def link_owners(game_id: str, manifest: Manifest | None = None) -> dict[str, tuple[int, str]]:
    """Folded path -> (id, name) of the installed mod whose link the app put
    there (read-only; empty when nothing was ever installed)."""
    if manifest is None:
        if not paths.manifest_path().exists():
            return {}
        try:
            with Manifest() as opened:
                return link_owners(game_id, opened)
        except ManifestError:
            return {}
    names = {m.id: m.name for m in manifest.mods(game_id)}
    return {key: (d.mod_id, names.get(d.mod_id, "another mod")) for key, d in manifest.deployed(game_id).items()}


def _resolve_against_game(plan: PlacementPlan, game: DiscoveredGame,
                          owners: dict[str, tuple[int, str]] | None = None,
                          replacing: int | None = None) -> list[dict]:
    """Where each mapping would land in the real game folder (read-only).
    *owners* (from :func:`link_owners`) tells the app's links from game
    files; *replacing* is the mod this one updates."""
    if not game.root.is_dir():
        return []
    resolver = CaseResolver(game.root)
    owners = owners or {}
    out = []
    for m in plan.mappings:
        r = resolver.resolve(m.dest, plan=True)
        owner = owners.get(fold_path(str(r.rel)))
        if r.type_conflict:
            status = "conflict: a folder/file of the other type already exists there"
        elif r.exists_on_disk and owner is not None and owner[0] == replacing:
            status = "replaces the previous version's file"
        elif r.exists_on_disk and owner is not None:
            status = f"overrides {owner[1]} (its file stays installed)"
        elif r.exists_on_disk:
            status = "replaces an existing game file (it would be backed up)"
        elif r.exists:
            status = "duplicate within this mod"
        else:
            status = "new"
        out.append({
            "source": m.source,
            "dest": str(r.rel),
            "case_adjusted": _case_note(m.dest, r.rel, game.root),
            "status": status,
            "new_dirs": [str(d) for d in r.new_dirs],
            "notes": list(r.ambiguous),
        })
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


_RULE_TEXT = {
    Rule.FOMOD: "FOMOD installer",
    Rule.DATA_FOLDER: "has a Data folder",
    Rule.DATA_CONTENT: "plugins/archives/Data folders",
    Rule.GAME_ROOT_CONTENT: "game-folder files",
    Rule.UNKNOWN: "nothing recognised",
    Rule.MANUAL: "your choice",
}

_TARGET_TEXT = {
    Target.DATA: "the Data folder",
    Target.GAME_ROOT: "the game folder, next to the .exe",
    Target.UNKNOWN: "unknown - you will need to choose",
}


def format_plan(
    plan: PlacementPlan,
    game_id: str | None,
    game: DiscoveredGame | None,
    resolved: list[dict],
    *,
    installed: list[str] | None = None,
    source: str = "<archive>",
    requested: bool = True,
) -> str:
    out: list[str] = []
    if plan.nexus is not None:
        n = plan.nexus
        bits = [n.display_name]
        if n.version:
            bits.append(f"version {n.version}")
        if n.mod_id is not None:
            bits.append(f"Nexus mod {n.mod_id}")
        out.append("Mod      : " + ", ".join(bits))

    guess = plan.game
    if guess.game_id:
        name = GAMES[guess.game_id].name
        out.append(f"Game     : {name} ({_conf(guess.confidence)} confidence)")
    else:
        out.append("Game     : could not tell")
        if installed and not game_id:
            out.append(f"           re-run with --game {'|'.join(installed)} to check it against a game folder")
    for reason in guess.reasons:
        out.append(f"           - {reason}")
    if game_id and game_id != guess.game_id:
        why = "as requested" if requested else "(your current game)"
        out.append(f"           checking against {GAMES[game_id].name} {why}")

    rule = _RULE_TEXT[plan.rule]
    if plan.target is Target.UNKNOWN:
        out.append(f"Installs : {_TARGET_TEXT[plan.target]} ({rule})")
    else:
        out.append(f"Installs : into {_TARGET_TEXT[plan.target]} - {_conf(plan.confidence)} confidence ({rule})")
    if plan.root and plan.root != "/".join(plan.wrapper_dirs):
        out.append(f"           mod root inside archive: {plan.root}/")
    for reason in plan.reasons:
        out.append(f"           - {reason}")
    for warning in plan.warnings:
        out.append(f"  ! {warning}")

    if plan.is_fomod:
        out.append("")
        out.append(f"This is a FOMOD installer ({plan.fomod_config}). The guided installer arrives in a later")
        out.append("phase; until then you pick which sub-folders to install.")

    if plan.candidate_roots:
        out.append("")
        out.append("Folders that could be installed:")
        for c in plan.candidate_roots:
            out.append(f"  {c.path or '(top level)'}/  -> {_TARGET_TEXT[c.target]}")

    if plan.unplaced:
        out.append("")
        out.append(f"Files in the mod ({len(plan.unplaced)}):")
        out.extend(_tree(plan.unplaced))
        example = next((c.path for c in plan.candidate_roots if c.path), None) or plan.unplaced[0].split("/")[0]
        out.append("")
        out.append("Preview a choice with:")
        out.append(f"  bethmm analyze {_quote(source)} --root {_quote(example)} [--root ANOTHER] [--target data|game]")

    if plan.mappings:
        out.append("")
        where = f" in {game.root}" if game and resolved else ""
        out.append(f"Files ({len(plan.mappings)}){where}:")
        if resolved:
            for note in dict.fromkeys(n for r in resolved for n in r["notes"]):
                out.append(f"  ! {note}")
            for r in resolved:
                note = "" if r["status"] == "new" else f"   [{r['status']}]"
                if r["case_adjusted"]:
                    note += f"   [{r['case_adjusted']}]"
                out.append(f"  {r['source']}  ->  {r['dest']}{note}")
        else:
            for m in plan.mappings:
                out.append(f"  {m.source}  ->  {m.dest}")
        if game is None and game_id:
            out.append(f"  ({GAMES[game_id].name} was not found, so destinations weren't checked against a real folder)")
        elif game is None:
            out.append("  (no game chosen, so destinations weren't checked against a real folder)")

    if plan.skipped:
        out.append("")
        out.append(f"Not installed ({len(plan.skipped)}):")
        for s in plan.skipped[:50]:
            out.append(f"  {s}")
        if len(plan.skipped) > 50:
            out.append(f"  ... and {len(plan.skipped) - 50} more")
    return "\n".join(out)


def _conf(c: Confidence) -> str:
    return c.name.lower()


def _tree(files: list[str], limit: int = 60) -> list[str]:
    """Indented folder tree of *files* (relative POSIX paths)."""
    lines: list[str] = []
    shown: set[str] = set()
    for path in files:
        parts = path.split("/")
        for depth in range(len(parts) - 1):
            folder = "/".join(parts[: depth + 1])
            if folder not in shown:
                shown.add(folder)
                lines.append("  " + "    " * depth + parts[depth] + "/")
        lines.append("  " + "    " * (len(parts) - 1) + parts[-1])
    if len(lines) > limit:
        lines = lines[:limit] + [f"  ... and {len(lines) - limit} more lines"]
    return lines


def _quote(text: str) -> str:
    return text if text and all(c.isalnum() or c in "-_./~" for c in text) else "'" + text.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- plugin-info


def cmd_plugin_info(args) -> int:
    status = 0
    for name in args.plugins:
        try:
            h = read_plugin_header(name)
        except PluginHeaderError as exc:
            print(f"{name}: {exc}")
            status = 1
            continue
        kind = "Oblivion-style" if h.header_size == 20 else f"form version {h.form_version}"
        votes = candidate_games(h, Path(name).name)
        flags = ["ESM"] if h.is_master_flag else []
        # 0x200 means "light" only in Fallout 4 and Skyrim SE; show other bits raw.
        if h.is_light_flag and votes and votes[0].game_id in ("fallout4", "skyrimse"):
            flags.append("light/ESL")
        other = h.flags & ~0x1 & ~(0x200 if "light/ESL" in flags else 0)
        if other:
            flags.append(f"0x{other:x}")
        print(f"{name}:")
        print(f"  header  : HEDR {h.hedr_version}, {kind}" + (f", flags {', '.join(flags)}" if flags else ""))
        if h.author:
            print(f"  author  : {h.author}")
        print(f"  masters : {', '.join(h.masters) if h.masters else '(none)'}")
        if votes:
            print(f"  game    : {GAMES[votes[0].game_id].name}  ({votes[0].reason})")
            for v in votes[1:]:
                print(f"            or {GAMES[v.game_id].name}  ({v.reason})")
        else:
            print("  game    : unknown")
    return status


# ---------------------------------------------------------------- settings


def cmd_config(args) -> int:
    settings = load_settings()
    changed = False
    if args.games_dir is not None:
        settings.games_dir = args.games_dir
        changed = True
    if args.scan_depth is not None:
        if args.scan_depth < 1:
            raise CliError("--scan-depth must be at least 1")
        settings.scan_depth = args.scan_depth
        changed = True
    if args.sevenzip is not None:
        settings.sevenzip = args.sevenzip or None
        changed = True
    if args.dry_run is not None:
        settings.dry_run = args.dry_run == "on"
        changed = True
    if changed:
        save_settings(settings)
        rescan = args.games_dir is not None or args.scan_depth is not None
        print(f"Saved {paths.settings_path()}." + (" Run 'bethmm games --rescan' to search again." if rescan else ""))
    print(json.dumps(settings.to_dict(), indent=2))
    print(f"\nsettings : {paths.settings_path()}\ndata     : {paths.data_dir()}\nlog      : {paths.log_path()}")
    return 0


def cmd_override(args) -> int:
    settings = load_settings()
    if args.clear:
        settings.game_overrides.pop(args.game, None)
    else:
        o = settings.game_overrides.get(args.game) or GameOverride()
        if args.root is not None:
            o.root = os.path.abspath(os.path.expanduser(args.root)) if args.root else None
        if args.prefix is not None:
            o.prefix = os.path.abspath(os.path.expanduser(args.prefix)) if args.prefix else None
        if args.wine_user is not None:
            o.wine_user = args.wine_user or None
        if args.link_mode is not None:
            o.link_mode = args.link_mode
        if args.ignore is not None:
            o.ignore = args.ignore
        settings.game_overrides[args.game] = o
    save_settings(settings)
    games, warnings = get_games(settings)
    match = next((g for g in games if g.game_id == args.game), None)
    if match:
        print(_describe_game(match))
        mode = settings.override(args.game).link_mode
        print(f"  links  : {mode}s")
        if mode == "hardlink":
            try:
                deploy.check_link_mode(deploy.GameTarget(args.game, match.root, mode))
            except deploy.DeployError as exc:
                print(f"warning: {exc}", file=sys.stderr)
    else:
        print(f"{GAMES[args.game].name} is not currently listed (not found, or ignored).")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
