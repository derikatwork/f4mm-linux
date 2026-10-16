"""Command-line interface.

Phase 1 commands only read: they never create, change or delete anything in a
game folder.

    bethmm games [--rescan] [--json]
    bethmm analyze <archive-or-folder> [--game ID] [--json]
    bethmm plugin-info <plugin>...
    bethmm config [--games-dir PATH] [--scan-depth N] [--7z PATH]
    bethmm override <game> [--root PATH] [--prefix PATH] [--wine-user NAME]
                           [--link-mode symlink|hardlink] [--ignore|--no-ignore] [--clear]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from bethmm import APP_NAME, __version__, paths
from bethmm.archive import ArchiveError, extract_archive
from bethmm.discovery import DiscoveredGame, get_games
from bethmm.games import GAMES
from bethmm.log import setup_logging
from bethmm.pathresolve import CaseResolver
from bethmm.placement import Confidence, PlacementPlan, Rule, Target, analyze_directory
from bethmm.plugin_header import PluginHeaderError, candidate_games, read_plugin_header
from bethmm.settings import LINK_MODES, GameOverride, Settings, SettingsError, load_settings, save_settings

log = logging.getLogger(__name__)


class CliError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        setup_logging()
    except OSError as exc:
        print(f"warning: logging disabled: {exc}", file=sys.stderr)
    try:
        return args.func(args) or 0
    except (CliError, ArchiveError, SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("plugin-info", help="show a plugin's masters and which game it is for")
    p.add_argument("plugins", nargs="+")
    p.set_defaults(func=cmd_plugin_info)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("--games-dir", help="Lutris games folder to scan (default ~/Games)")
    p.add_argument("--scan-depth", type=int, help="folder levels to search below each scan root")
    p.add_argument("--7z", dest="sevenzip", help="path to the 7z binary ('' = search PATH)")
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
    return parser


# ---------------------------------------------------------------- games


def cmd_games(args) -> int:
    settings = load_settings()
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
    if not args.rescan:
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
        plan = analyze_directory(source, installed_games=installed, archive_name=source.name)
    elif source.is_file():
        plan = _analyze_archive(source, settings, installed)
    else:
        raise CliError(f"{source} does not exist")

    game_id = args.game or plan.game.game_id
    game = next((g for g in games if g.game_id == game_id), None)
    resolved = _resolve_against_game(plan, game) if game else []

    if args.json:
        data = plan.to_dict()
        data["checked_against"] = str(game.root) if game else None
        data["resolved"] = resolved
        print(json.dumps(data, indent=2))
    else:
        print(format_plan(plan, game_id, game, resolved))
    return 0


def _analyze_archive(archive: Path, settings: Settings, installed: list[str]) -> PlacementPlan:
    paths.tmp_dir().mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="analyze-", dir=paths.tmp_dir()))
    try:
        result = extract_archive(archive, workdir / "x", settings.sevenzip)
        plan = analyze_directory(workdir / "x", installed_games=installed, archive_name=archive.name)
        plan.warnings.extend(result.warnings)
        return plan
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _resolve_against_game(plan: PlacementPlan, game: DiscoveredGame) -> list[dict]:
    """Where each mapping would land in the real game folder (read-only)."""
    if not game.root.is_dir():
        return []
    resolver = CaseResolver(game.root)
    out = []
    for m in plan.mappings:
        r = resolver.resolve(m.dest, plan=True)
        if r.type_conflict:
            status = "conflict: a folder/file of the other type already exists there"
        elif r.exists_on_disk:
            status = "replaces an existing game file (it would be backed up)"
        elif r.exists:
            status = "duplicate within this mod"
        else:
            status = "new"
        out.append({
            "source": m.source,
            "dest": str(r.rel),
            "case_adjusted": _case_note(m.dest, r.rel, r.new_dirs),
            "status": status,
            "new_dirs": [str(d) for d in r.new_dirs],
            "notes": list(r.ambiguous),
        })
    return out


def _case_note(wanted: str, rel, new_dirs) -> str | None:
    """Why the resolved path is spelled differently from the mod's, if it is."""
    wanted_parts = wanted.replace("\\", "/").split("/")
    new = {str(d) for d in new_dirs}
    reasons = set()
    for i, (mine, actual) in enumerate(zip(wanted_parts, rel.parts)):
        if mine != actual:
            prefix = "/".join(rel.parts[: i + 1])
            reasons.add("new folder spelled like the game's folders" if prefix in new else "case matched to existing folder")
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


def format_plan(plan: PlacementPlan, game_id: str | None, game: DiscoveredGame | None, resolved: list[dict]) -> str:
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
        out.append("Game     : could not tell - please choose")
    for reason in guess.reasons:
        out.append(f"           - {reason}")
    if game_id and game_id != guess.game_id:
        out.append(f"           checking against {GAMES[game_id].name} as requested")

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
        out.append("phase; until then you will pick which sub-folders to install.")

    if plan.candidate_roots:
        out.append("")
        out.append("Folders that could be installed:")
        for c in plan.candidate_roots:
            out.append(f"  {c.path or '(top level)'}/  -> {_TARGET_TEXT[c.target]}")

    if plan.mappings:
        out.append("")
        where = f" in {game.root}" if game and resolved else ""
        out.append(f"Files ({len(plan.mappings)}){where}:")
        if resolved:
            for r in resolved:
                note = "" if r["status"] == "new" else f"   [{r['status']}]"
                if r["case_adjusted"]:
                    note += f"   [{r['case_adjusted']}]"
                out.append(f"  {r['source']}  ->  {r['dest']}{note}")
        else:
            for m in plan.mappings:
                out.append(f"  {m.source}  ->  {m.dest}")
        if game is None and plan.target is not Target.UNKNOWN:
            out.append("  (game not installed/found, so destinations weren't checked against a real folder)")

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
    if changed:
        save_settings(settings)
        print(f"Saved {paths.settings_path()}. Run 'bethmm games --rescan' to search again.")
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
    else:
        print(f"{GAMES[args.game].name} is not currently listed (not found, or ignored).")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
