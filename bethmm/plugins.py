"""plugins.txt: which plugins a game loads (and, for most games, in which order).

The file lives in the game's Wine prefix
(``drive_c/users/<user>/AppData/Local/<game folder>/plugins.txt``) and comes
in two formats (``games.PluginsTxtFormat``):

* **asterisk** (Fallout 4, Skyrim SE, Starfield): every plugin the game knows,
  in load order; ``*Name.esp`` is active, ``Name.esp`` is not. The game's own
  masters and DLCs are loaded anyway and aren't listed;
* **plain** (Fallout: New Vegas, Fallout 3, Oblivion, Skyrim LE): one active
  plugin per line, the game's own masters included; the load order comes from
  the plugin files' modification times.

Lines that aren't plugins (``#`` comments, blank lines) are kept where they
are, and so are the file's encoding (Windows-1252 or UTF-8) and line endings.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from bethmm import paths
from bethmm.games import GAMES, GameDef, PluginsTxtFormat
from bethmm.manifest import DeployedFile, Manifest, PluginRecord
from bethmm.plugin_header import PLUGIN_EXTS, PluginHeaderError, read_plugin_header

if TYPE_CHECKING:
    from bethmm.deploy import GameTarget

log = logging.getLogger(__name__)


def is_plugin_name(name: str) -> bool:
    return "/" not in name and "\\" not in name and name.lower().endswith(PLUGIN_EXTS)


def key(name: str) -> str:
    """The case-insensitive key of a plugin name."""
    return name.strip().lower()


@dataclass
class Entry:
    name: str
    active: bool = True


@dataclass
class PluginsFile:
    """plugins.txt as read: its plugin entries in order, and the other lines
    (kept as text) where they were."""

    path: Path | None
    format: PluginsTxtFormat
    lines: list[Entry | str] = field(default_factory=list)
    encoding: str = "cp1252"
    newline: str = "\r\n"
    #: False: there was no file (nothing written yet).
    exists: bool = False

    @property
    def entries(self) -> list[Entry]:
        return [line for line in self.lines if isinstance(line, Entry)]

    def find(self, name: str) -> Entry | None:
        wanted = key(name)
        return next((e for e in self.entries if key(e.name) == wanted), None)

    def index(self, name: str) -> int | None:
        """Position of *name* among the plugin entries."""
        wanted = key(name)
        return next((i for i, e in enumerate(self.entries) if key(e.name) == wanted), None)

    def active(self) -> list[str]:
        return [e.name for e in self.entries if e.active]


def parse(data: bytes, fmt: PluginsTxtFormat, path: Path | None = None) -> PluginsFile:
    try:
        text = data.decode("utf-8")
        encoding = "utf-8" if any(ord(c) > 127 for c in text) else "cp1252"
    except UnicodeDecodeError:
        text, encoding = data.decode("cp1252", errors="replace"), "cp1252"
    text = text.lstrip("﻿")
    newline = "\r\n" if "\r\n" in text or not text else "\n"
    pf = PluginsFile(path, fmt, encoding=encoding, newline=newline, exists=True)
    seen: set[str] = set()
    for raw in re.split(r"\r?\n", text):
        line = raw.strip()
        if not line or line.startswith("#"):
            pf.lines.append(raw.rstrip("\r"))
            continue
        active = True
        if fmt is PluginsTxtFormat.ASTERISK:
            active = line.startswith("*")
            line = line.lstrip("*").strip()
        if not is_plugin_name(line) or key(line) in seen:
            pf.lines.append(raw.rstrip("\r"))  # not a plugin, or listed twice: kept as it is
            continue
        seen.add(key(line))
        pf.lines.append(Entry(line, active))
    while pf.lines and pf.lines[-1] == "":
        pf.lines.pop()  # the final newline
    return pf


def read(path: Path | None, fmt: PluginsTxtFormat) -> PluginsFile:
    """The plugins.txt at *path*; an empty one (``exists`` False) if there is none."""
    if path is None:
        return PluginsFile(None, fmt)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return PluginsFile(path, fmt)
    return parse(data, fmt, path)


def render(pf: PluginsFile) -> bytes:
    out = []
    for line in pf.lines:
        if isinstance(line, Entry):
            if pf.format is PluginsTxtFormat.ASTERISK:
                out.append(("*" if line.active else "") + line.name)
            elif line.active:
                out.append(line.name)
        else:
            out.append(line)
    text = pf.newline.join(out) + (pf.newline if out else "")
    try:
        return text.encode(pf.encoding)
    except UnicodeEncodeError:
        return text.encode("utf-8")


#: The list of Creation Club plugins a game loads by itself, in its folder.
CCC_FILES = {"fallout4": "Fallout4.ccc", "skyrimse": "Skyrim.ccc", "starfield": "Starfield.ccc"}


def creation_club(game_id: str, root: Path | None) -> list[str]:
    """The Creation Club plugins the game loads by itself (its .ccc list)."""
    name = CCC_FILES.get(game_id)
    if name is None or root is None:
        return []
    try:
        path = next((p for p in Path(root).iterdir() if p.name.lower() == name.lower()), None)
        text = path.read_bytes().decode("utf-8", errors="replace") if path is not None else ""
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if is_plugin_name(line.strip())]


def implicit(gdef: GameDef, root: Path | None = None) -> set[str]:
    """Keys of the plugins the game loads without them being listed."""
    return {key(n) for n in gdef.implicit_plugins} | {key(n) for n in creation_club(gdef.id, root)}


def active_keys(pf: PluginsFile, gdef: GameDef, root: Path | None = None) -> set[str]:
    """Keys of the plugins the game loads (given they exist in Data)."""
    return {key(e.name) for e in pf.entries if e.active} | implicit(gdef, root)


# ---------------------------------------------------------------- keeping plugins.txt in line

#: How many earlier copies of each game's plugins.txt are kept (plus the first one).
KEEP_BACKUPS = 30
#: Seconds between the dates the app gives its plugins in games whose load
#: order comes from file dates.
DATE_STEP = 60


class PluginsError(Exception):
    """plugins.txt can't be changed as asked."""


@dataclass
class PluginsChange:
    """What a sync (or an edit) did to plugins.txt (or, in a dry run, would do)."""

    path: Path | None
    dry_run: bool = False
    #: Lines added for plugins the app linked into Data.
    added: list[str] = field(default_factory=list)
    #: Lines put back where they were before the app took them out.
    restored: list[str] = field(default_factory=list)
    #: Lines taken out: the app no longer has the plugin in Data.
    removed: list[str] = field(default_factory=list)
    #: Plugins switched on or off, and moved (edits).
    switched: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    #: Plugins whose file date the app set (games whose load order is by date).
    dated: list[str] = field(default_factory=list)
    #: The copy of plugins.txt made before writing it.
    backup: Path | None = None
    written: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.restored or self.removed or self.switched or self.moved or self.dated)


def data_folder(root: Path) -> Path:
    """The game's Data folder, in any case."""
    try:
        return next((p for p in root.iterdir() if p.name.lower() == "data" and p.is_dir()), root / "Data")
    except OSError:
        return root / "Data"


class _Listing:
    """The files directly in the Data folder, by lower-case name (read once)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        try:
            self.names = {p.name.lower(): p for p in data_dir.iterdir()}
        except OSError:
            self.names = {}

    def get(self, name: str) -> Path | None:
        return self.names.get(name.lower())


class _Masters:
    """Whether plugins load with the masters (.esm/.esl, or a master-flagged
    .esp), reading each header at most once."""

    def __init__(self, listing: _Listing) -> None:
        self.listing = listing
        self.known: dict[str, bool] = {}

    def __call__(self, name: str) -> bool:
        k = key(name)
        if k not in self.known:
            path = None if k.endswith((".esm", ".esl")) else self.listing.get(name)
            try:
                self.known[k] = k.endswith((".esm", ".esl")) or (
                    path is not None and read_plugin_header(path).is_master_flag)
            except PluginHeaderError:
                self.known[k] = False
        return self.known[k]


def app_plugins(deployed: dict[str, DeployedFile]) -> dict[str, DeployedFile]:
    """Key -> record of the plugins the app has linked directly into Data."""
    out = {}
    for record in deployed.values():
        parts = record.path.split("/")
        if len(parts) == 2 and parts[0].lower() == "data" and is_plugin_name(parts[1]):
            out[key(parts[1])] = record
    return out


def _insert(pf: PluginsFile, entry: Entry, at: int) -> None:
    """Put *entry* at position *at* among the plugin entries."""
    entries = pf.entries
    if at >= len(entries):
        pf.lines.append(entry)
        return
    pf.lines.insert(pf.lines.index(entries[at]), entry)


def _new_place(pf: PluginsFile, name: str, is_master: _Masters) -> int:
    """Where a new plugin goes: a master after the last master, anything else at the end."""
    entries = pf.entries
    if pf.format is PluginsTxtFormat.ASTERISK and is_master(name):
        masters = [i for i, e in enumerate(entries) if is_master(e.name)]
        return masters[-1] + 1 if masters else 0
    return len(entries)


def _seed(pf: PluginsFile, gdef: GameDef, listing: _Listing) -> None:
    """A new plain-format plugins.txt lists the game's own masters, or the game wouldn't load them."""
    if pf.exists or pf.format is not PluginsTxtFormat.PLAIN or pf.entries:
        return
    for name in gdef.official_masters:
        found = listing.get(name)
        if found is not None:
            pf.lines.append(Entry(found.name, True))


def sync(manifest: Manifest, target: GameTarget, *, dry_run: bool = False,
         deployed: dict[str, DeployedFile] | None = None) -> PluginsChange | None:
    """Bring plugins.txt in line with the plugins the app has in Data
    (*deployed*: the links as they will be after a dry run's plan; default:
    as recorded). None: nothing to do and nothing known about the file."""
    gdef = GAMES[target.game_id]
    now = manifest.deployed(target.game_id)
    ours = app_plugins(now if deployed is None else deployed)
    going = set(app_plugins(now)) - set(ours)  # a dry run's plan unlinks them; they are still there
    records = manifest.plugin_records(target.game_id)
    if target.plugins_txt is None:
        if ours or any(r.listed for r in records.values()):
            return PluginsChange(None, dry_run, warnings=[
                "plugins.txt wasn't updated: the game's Wine prefix (or Wine user) isn't known; set it in Game "
                f"settings or with 'bethmm override {target.game_id} --prefix ...'"])
        return None
    data_dir = data_folder(Path(target.root))
    listing = _Listing(data_dir)
    is_master = _Masters(listing)
    pf = read(target.plugins_txt, gdef.plugins_format)
    original = render(pf) if pf.exists else None
    change = PluginsChange(target.plugins_txt, dry_run)
    new = dict(records)
    official = {key(n) for n in gdef.official_masters}
    backed_up = {k.split("/", 1)[1].lower() for k in manifest.backups(target.game_id) if k.count("/") == 1}

    # Lines of plugins the app no longer has in Data come out; each remembers
    # the plugin before it in the list as it was, so they go back in order.
    before = {key(e.name): key(pf.entries[i - 1].name) if i else "" for i, e in enumerate(pf.entries)}
    for k, rec in sorted(records.items()):
        if k in ours:
            continue
        entry = pf.find(rec.name)
        if entry is None:
            if rec.listed:  # taken out by someone else meanwhile
                new[k] = PluginRecord(rec.game_id, rec.name, False, rec.active, None)
            continue
        if listing.get(rec.name) is not None and (k not in going or k in backed_up):
            continue  # a file of the game's (or the user's) is there now: its line stays
        pf.lines.remove(entry)
        change.removed.append(entry.name)
        new[k] = PluginRecord(rec.game_id, entry.name, False, entry.active, before[k])

    if any(k not in official for k in ours):
        _seed(pf, gdef, listing)
    order = {m.id: i for i, m in enumerate(manifest.mods(target.game_id))}
    waiting: list[tuple[str, str, bool, str | None]] = []  # key, name, active, after
    for k, record in sorted(ours.items(), key=lambda item: (order.get(item[1].mod_id, -1), item[0])):
        if k in official:
            continue  # the game's own master, replaced by a mod: the game lists it itself
        name = record.path.split("/")[1]
        entry = pf.find(name)
        rec = new.get(k)
        if entry is not None:
            new[k] = PluginRecord(target.game_id, entry.name, True, entry.active, None)
        elif rec is None:
            _insert(pf, Entry(name, True), _new_place(pf, name, is_master))
            change.added.append(name)
            new[k] = PluginRecord(target.game_id, name, True, True, None)
        elif not rec.listed:
            if pf.format is PluginsTxtFormat.PLAIN and not rec.active:
                continue  # switched off: a plain list only names active plugins
            waiting.append((k, name, rec.active, rec.after))
        elif pf.format is PluginsTxtFormat.PLAIN:
            new[k] = PluginRecord(target.game_id, name, False, False, None)  # switched off in the launcher
        else:
            _insert(pf, Entry(name, False), _new_place(pf, name, is_master))  # kept in the list, switched off
            change.added.append(name)
            new[k] = PluginRecord(target.game_id, name, True, False, None)

    # Back where they were: each after the plugin it followed, once that one is there.
    while waiting:
        ready = [w for w in waiting if w[3] == "" or (w[3] and pf.index(w[3]) is not None)]
        if not ready:
            ready = waiting[:1]  # what it followed is gone: a new plugin's place
        for k, name, active, after in ready:
            at = 0 if after == "" else pf.index(after) + 1 if after and pf.index(after) is not None \
                else _new_place(pf, name, is_master)
            _insert(pf, Entry(name, active), at)
            change.restored.append(name)
            new[k] = PluginRecord(target.game_id, name, True, active, None)
            waiting.remove((k, name, active, after))

    _finish(manifest, target, pf, original, records, new, change, ours, data_dir)
    return change


def _finish(manifest: Manifest, target: GameTarget, pf: PluginsFile, original: bytes | None,
            records: dict[str, PluginRecord], new: dict[str, PluginRecord], change: PluginsChange,
            ours: dict[str, DeployedFile], data_dir: Path) -> None:
    """Write plugins.txt (after backing it up), then the records; then, for a
    game whose load order is by file date, date the app's plugins."""
    content = render(pf)
    if content != original and (pf.entries or pf.exists):
        if not change.dry_run:
            change.backup = _backup(target.plugins_txt, target.game_id)
            _write(target.plugins_txt, content)
            change.written = True
            manifest.journal(target.game_id, "plugins", str(target.plugins_txt), None,
                             _summary(change), status="done")
    if not change.dry_run and new != records:
        with manifest.transaction():
            for k in records.keys() - new.keys():
                manifest.forget_plugin(target.game_id, k)
            for k, rec in new.items():
                if records.get(k) != rec:
                    manifest.record_plugin(rec)
    if GAMES[target.game_id].timestamp_load_order:
        change.dated = _date(manifest, pf, ours, data_dir, change.dry_run)


def _summary(change: PluginsChange) -> str:
    bits = [f"{what} {', '.join(names)}" for what, names in (
        ("added", change.added), ("put back", change.restored), ("removed", change.removed),
        ("switched", change.switched), ("moved", change.moved)) if names]
    return "; ".join(bits) or "rewritten"


def _backup(path: Path, game_id: str) -> Path | None:
    """Copy plugins.txt into the app's plugins-backups folder (keeping the
    first copy and the latest ones)."""
    if not path.is_file():
        return None
    folder = paths.plugins_backups_dir(game_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = folder / f"plugins-{stamp}.txt"
    shutil.copy2(path, dest)
    log.info("BACKUP %s -> %s", path, dest)
    first = folder / "plugins-first.txt"
    if not first.exists():
        shutil.copy2(path, first)
    copies = sorted(p for p in folder.glob("plugins-2*.txt"))
    for old in copies[:-KEEP_BACKUPS]:
        old.unlink()
        log.info("DELETE %s (an older copy of plugins.txt)", old)
    return dest


def _write(path: Path, content: bytes) -> None:
    """Replace plugins.txt in one step (a new file renamed over it)."""
    if not path.parent.is_dir():
        if not path.parent.parent.is_dir():
            raise PluginsError(f"{path.parent.parent} doesn't exist; has the game been started once?")
        path.parent.mkdir()
        log.info("MKDIR %s", path.parent)
    fd, temp = tempfile.mkstemp(prefix=".plugins-", suffix=".txt", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(path, temp)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    log.info("WRITE %s", path)


def _date(manifest: Manifest, pf: PluginsFile, ours: dict[str, DeployedFile], data_dir: Path,
          dry_run: bool) -> list[str]:
    """Give the app's active plugins file dates in the order plugins.txt lists
    them, after every plugin it didn't put there (the game's own and the
    user's: their dates are never changed)."""
    newest = 0.0
    try:
        for entry in os.scandir(data_dir):
            if is_plugin_name(entry.name) and key(entry.name) not in ours and entry.is_file():
                newest = max(newest, entry.stat().st_mtime)
    except OSError:
        return []
    dated = []
    when = int(newest) + DATE_STEP
    for entry in pf.entries:
        record = ours.get(key(entry.name))
        if record is None or not entry.active:
            continue
        source = Path(record.source)
        try:
            st = os.stat(source)
        except OSError:
            continue
        if int(st.st_mtime) != when:
            dated.append(entry.name)
            if not dry_run:
                os.utime(source, ns=(st.st_atime_ns, when * 1_000_000_000))
                log.info("DATE %s (load order position %d)", source, len(dated))
                if record.link_type == "hardlink":
                    st = os.stat(source)
                    with manifest.transaction():
                        manifest.record_link(replace(record, size=st.st_size, mtime_ns=st.st_mtime_ns))
        when += DATE_STEP
    return dated


# ---------------------------------------------------------------- the load order


@dataclass
class PluginInfo:
    """One plugin as the load order shows it."""

    name: str
    active: bool
    #: The game loads it whatever plugins.txt says (its own masters).
    implicit: bool = False
    #: The file is in Data.
    present: bool = True
    master: bool = False
    light: bool = False
    #: The installed mod whose link it is (None: not one of the app's).
    mod_id: int | None = None
    masters: list[str] = field(default_factory=list)
    #: Masters that aren't loaded, and masters that load after it.
    missing: list[str] = field(default_factory=list)
    late: list[str] = field(default_factory=list)
    #: Its place can be changed (in games whose load order is by file date,
    #: only the app's own plugins: other files' dates are never changed).
    movable: bool = True


def _date_ordered(gdef: GameDef) -> bool:
    return gdef.timestamp_load_order and gdef.plugins_format is PluginsTxtFormat.PLAIN


def load_order(manifest: Manifest, target: GameTarget) -> list[PluginInfo]:
    """The game's plugins in the order it loads them, with what each needs."""
    gdef = GAMES[target.game_id]
    data_dir = data_folder(Path(target.root))
    pf = read(target.plugins_txt, gdef.plugins_format)
    ours = app_plugins(manifest.deployed(target.game_id))
    try:
        files = {key(e.name): e for e in os.scandir(data_dir) if is_plugin_name(e.name) and e.is_file()}
    except OSError:
        files = {}
    listed = {key(e.name): e for e in pf.entries}
    club = creation_club(target.game_id, Path(target.root))
    implicit_keys = implicit(gdef, Path(target.root))

    def info(name: str, active: bool, *, is_implicit: bool = False) -> PluginInfo:
        k = key(name)
        entry = files.get(k)
        header = None
        if entry is not None:
            try:
                header = read_plugin_header(entry.path)
            except PluginHeaderError:
                pass
        record = ours.get(k)
        return PluginInfo(entry.name if entry is not None else name, active, is_implicit, entry is not None,
                          k.endswith((".esm", ".esl")) or bool(header and header.is_master_flag),
                          k.endswith(".esl") or bool(header and header.is_light_flag and gdef.supports_esl),
                          record.mod_id if record is not None else None,
                          list(header.masters) if header is not None else [],
                          movable=not is_implicit and (not _date_ordered(gdef) or record is not None))

    order: list[PluginInfo] = []
    if _date_ordered(gdef):
        def when(k: str) -> tuple:
            try:
                st = os.stat(files[k].path)
            except OSError:
                return (1, 0.0, k)
            plugin = info(files[k].name, True)
            return (0 if plugin.master else 1, st.st_mtime, k)
        for k in sorted(files, key=when):
            order.append(info(files[k].name, k in listed))
        order += [info(e.name, True) for k, e in listed.items() if k not in files]
    else:
        official = [n for n in dict.fromkeys(list(gdef.implicit_plugins) + club) if key(n) in files]
        order += [info(n, True, is_implicit=True) for n in official]
        order += [info(e.name, e.active) for k, e in listed.items() if k not in implicit_keys]
        order += [info(files[k].name, False) for k in sorted(files) if k not in listed and k not in implicit_keys]

    loaded: dict[str, int] = {}
    for i, plugin in enumerate(order):
        if plugin.active and plugin.present:
            loaded.setdefault(key(plugin.name), i)
    for i, plugin in enumerate(order):
        if not plugin.active:
            continue
        plugin.missing = [m for m in plugin.masters if key(m) not in loaded]
        plugin.late = [m for m in plugin.masters if loaded.get(key(m), -1) > i]
    return order


def _edit(manifest: Manifest, target: GameTarget, dry_run: bool,
          change_file: Callable[[PluginsFile, dict[str, PluginRecord], PluginsChange], None]) -> PluginsChange:
    if target.plugins_txt is None:
        raise PluginsError("the game's plugins.txt isn't known: set its Wine prefix (and Wine user) in Game "
                           f"settings or with 'bethmm override {target.game_id} --prefix ...'")
    gdef = GAMES[target.game_id]
    pf = read(target.plugins_txt, gdef.plugins_format)
    original = render(pf) if pf.exists else None
    records = manifest.plugin_records(target.game_id)
    new = dict(records)
    change = PluginsChange(target.plugins_txt, dry_run)
    data_dir = data_folder(Path(target.root))
    _seed(pf, gdef, _Listing(data_dir))
    change_file(pf, new, change)
    _finish(manifest, target, pf, original, records, new, change, app_plugins(manifest.deployed(target.game_id)),
            data_dir)
    return change


def set_active(manifest: Manifest, target: GameTarget, names: list[str], active: bool, *,
               dry_run: bool = False) -> PluginsChange:
    """Switch plugins on or off (in plugins.txt)."""
    order = {key(p.name): p for p in load_order(manifest, target)}

    def change_file(pf: PluginsFile, new: dict[str, PluginRecord], change: PluginsChange) -> None:
        is_master = _Masters(_Listing(data_folder(Path(target.root))))
        for name in names:
            plugin = order.get(key(name))
            if plugin is None:
                raise PluginsError(f"{name} isn't a plugin of this game (not in Data, not in plugins.txt)")
            if plugin.implicit:
                raise PluginsError(f"{plugin.name} is one of the game's own masters: the game always loads it")
            if not plugin.present and active:
                raise PluginsError(f"{plugin.name} isn't in the Data folder")
            entry = pf.find(plugin.name)
            k = key(plugin.name)
            if pf.format is PluginsTxtFormat.ASTERISK:
                if entry is None:
                    entry = Entry(plugin.name, active)
                    _insert(pf, entry, _new_place(pf, plugin.name, is_master))
                elif entry.active == active:
                    continue
                entry.active = active
            elif active:
                if entry is not None:
                    continue
                _insert(pf, Entry(plugin.name, True), len(pf.entries))
            else:
                if entry is None:
                    continue
                pf.lines.remove(entry)
            change.switched.append(f"{plugin.name} {'on' if active else 'off'}")
            if k in new or plugin.mod_id is not None:
                new[k] = PluginRecord(target.game_id, plugin.name, pf.find(plugin.name) is not None, active, None)

    return _edit(manifest, target, dry_run, change_file)


def _apply_order(pf: PluginsFile, wanted: list[PluginInfo], new: dict[str, PluginRecord], bring_in: set[str],
                 date_ordered: bool) -> None:
    """Put plugins.txt's lines into the order of *wanted*. The lines' slots stay
    where they were (comments too); plugins in *bring_in* that aren't listed
    get a line (switched off). By date, only the app's lines change places."""
    if date_ordered:
        lines = [pf.find(p.name) for p in wanted if p.movable]
    else:
        lines = [pf.find(p.name) or (Entry(p.name, False) if key(p.name) in bring_in else None)
                 for p in wanted if not p.implicit]
    lines = [e for e in lines if e is not None]
    moving = {key(e.name) for e in lines}
    slots = [i for i, line in enumerate(pf.lines) if isinstance(line, Entry) and key(line.name) in moving]
    for slot, entry in zip(slots, lines):
        pf.lines[slot] = entry
    pf.lines.extend(lines[len(slots):])
    for entry in lines:
        if key(entry.name) in new:
            new[key(entry.name)] = replace(new[key(entry.name)], listed=True, after=None)


def _check_movable(plugin: PluginInfo, gdef: GameDef) -> None:
    if plugin.implicit:
        raise PluginsError(f"{plugin.name} can't be moved: it is one of the game's own masters")
    if not plugin.movable:
        raise PluginsError(f"{plugin.name} can't be moved: this game's load order goes by file dates, and the app "
                           "only changes the dates of the plugins it installed")
    if _date_ordered(gdef) and not plugin.active:
        raise PluginsError(f"switch {plugin.name} on first: in this game only active plugins have a place in the "
                           "load order")


def move(manifest: Manifest, target: GameTarget, name: str, position: int, *,
         dry_run: bool = False) -> PluginsChange:
    """Move a plugin to *position* in the load order (as :func:`load_order` numbers it, from 0)."""
    gdef = GAMES[target.game_id]
    order = load_order(manifest, target)
    k = key(name)
    plugin = next((p for p in order if key(p.name) == k), None)
    if plugin is None:
        raise PluginsError(f"{name} isn't a plugin of this game (not in Data, not in plugins.txt)")
    _check_movable(plugin, gdef)
    rest = [p for p in order if key(p.name) != k]
    position = max(0, min(position, len(rest)))
    wanted = rest[:position] + [plugin] + rest[position:]

    def change_file(pf: PluginsFile, new: dict[str, PluginRecord], change: PluginsChange) -> None:
        _apply_order(pf, wanted, new, {k}, _date_ordered(gdef))
        change.moved.append(plugin.name)

    return _edit(manifest, target, dry_run, change_file)


def reorder(manifest: Manifest, target: GameTarget, names: list[str], *, dry_run: bool = False) -> PluginsChange:
    """Put the plugins named in *names* into that order, in the places those
    plugins have now; the others stay where they are."""
    gdef = GAMES[target.game_id]
    order = load_order(manifest, target)
    by_key = {key(p.name): p for p in order}
    chosen = []
    for name in dict.fromkeys(key(n) for n in names):
        plugin = by_key.get(name)
        if plugin is None:
            raise PluginsError(f"{name} isn't a plugin of this game (not in Data, not in plugins.txt)")
        _check_movable(plugin, gdef)
        chosen.append(plugin)
    places = [i for i, p in enumerate(order) if key(p.name) in {key(c.name) for c in chosen}]
    wanted = list(order)
    for place, plugin in zip(places, chosen):
        wanted[place] = plugin
    moved = [c.name for place, c in zip(places, chosen) if order[place] is not c]

    def change_file(pf: PluginsFile, new: dict[str, PluginRecord], change: PluginsChange) -> None:
        _apply_order(pf, wanted, new, {key(c.name) for c in chosen}, _date_ordered(gdef))
        change.moved.extend(moved)

    return _edit(manifest, target, dry_run, change_file)


#: Load order sorters (e.g. LOOT, which the app doesn't run yet): name ->
#: function taking the load order and returning plugin names in the order
#: they should load. :func:`sort` applies one.
SORTERS: dict[str, Callable[[list[PluginInfo]], list[str]]] = {}


def sort(manifest: Manifest, target: GameTarget, sorter: str, *, dry_run: bool = False) -> PluginsChange:
    """Put the plugins into the order *sorter* gives."""
    if sorter not in SORTERS:
        available = ", ".join(sorted(SORTERS)) or "none yet (LOOT support is planned)"
        raise PluginsError(f"no load order sorter called {sorter!r}; available: {available}")
    order = load_order(manifest, target)
    names = [n for n in SORTERS[sorter](order)
             if any(key(p.name) == key(n) and p.movable and (p.active or not _date_ordered(GAMES[target.game_id]))
                    for p in order)]
    return reorder(manifest, target, names, dry_run=dry_run)
