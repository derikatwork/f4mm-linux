"""The manifest: a SQLite record of installed mods and of everything the app
did to each game folder.

``~/.local/share/beth-mod-manager/manifest.db`` holds:

* ``mods`` / ``mod_files``: installed mods in priority order (higher priority
  wins a file conflict; the list is shown lowest first, so the bottom wins)
  and, per mod, which staged file goes to which path under the game root;
* ``deployed``: every link the app created in a game folder;
* ``backups``: every original game file the app moved aside to make room for
  a link, and where it was moved;
* ``created_dirs``: every folder the app created in a game folder;
* ``left_alone``: paths where the user (or a game update) replaced one of the
  app's links with a file of their own; the app leaves that file alone until
  it is gone;
* ``games``: per game, the root the deployment belongs to and whether mods are
  currently deployed (``purge`` clears it);
* ``journal``: an append-only history of every file operation.

Paths inside a game folder are stored relative to the game root, in the exact
spelling used on disk, plus a case-folded key (``pathresolve.fold_path``) so
that ``Textures/A.dds`` and ``textures/a.DDS`` are the same entry.

Only one process may change the manifest at a time: opening it with
``lock=True`` takes an exclusive ``flock`` on ``manifest.db.lock`` next to the
database and raises :class:`ManifestLocked` if another process holds it.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from bethmm import paths
from bethmm.pathresolve import fold_path

#: 2 added ``left_alone`` and ``deployed.size`` / ``deployed.mtime_ns``.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    deployed INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS mods (
    id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    version TEXT,
    nexus_id INTEGER,
    source_archive TEXT,
    installed_at TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '{}',
    UNIQUE (game_id, folder)
);
CREATE INDEX IF NOT EXISTS mods_by_game ON mods (game_id, priority);
CREATE TABLE IF NOT EXISTS mod_files (
    mod_id INTEGER NOT NULL REFERENCES mods (id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    dest TEXT NOT NULL,
    dest_key TEXT NOT NULL,
    PRIMARY KEY (mod_id, dest_key)
);
CREATE TABLE IF NOT EXISTS deployed (
    game_id TEXT NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    mod_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    link_type TEXT NOT NULL,
    inode INTEGER,
    dev INTEGER,
    size INTEGER,
    mtime_ns INTEGER,
    PRIMARY KEY (game_id, path_key)
);
CREATE TABLE IF NOT EXISTS backups (
    game_id TEXT NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    backup TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (game_id, path_key)
);
CREATE TABLE IF NOT EXISTS created_dirs (
    game_id TEXT NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    PRIMARY KEY (game_id, path_key)
);
CREATE TABLE IF NOT EXISTS left_alone (
    game_id TEXT NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    PRIMARY KEY (game_id, path_key)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    game_id TEXT,
    action TEXT NOT NULL,
    path TEXT,
    source TEXT,
    detail TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_unfinished ON journal (status) WHERE status = 'intent';
"""


#: Default of ``update_mod``'s optional details: leave the stored value as it is.
KEEP = object()


class ManifestError(Exception):
    pass


class ManifestLocked(ManifestError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ModFile:
    #: Path relative to the mod's staging folder.
    source: str
    #: Path relative to the game root, in the mod's own spelling.
    dest: str


@dataclass
class ModRecord:
    id: int
    game_id: str
    name: str
    #: Staging folder name under ``paths.mods_dir(game_id)``.
    folder: str
    priority: int
    enabled: bool
    version: str | None = None
    nexus_id: int | None = None
    source_archive: str | None = None
    installed_at: str = ""
    #: How the files were placed (root folders, target, rule), for reference.
    plan: dict = field(default_factory=dict)

    def staging_dir(self) -> Path:
        return paths.mods_dir(self.game_id) / self.folder


@dataclass(frozen=True)
class DeployedFile:
    game_id: str
    #: Relative to the game root, spelled as on disk.
    path: str
    mod_id: int
    #: Absolute path of the staged file the link points to.
    source: str
    #: "symlink" or "hardlink".
    link_type: str
    inode: int | None = None
    dev: int | None = None
    #: Hardlinks: size and mtime when linked, so a new file that happens to
    #: reuse the inode number is not mistaken for the link.
    size: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True)
class BackupRecord:
    game_id: str
    #: Where the original lived, relative to the game root.
    path: str
    #: Absolute path of the moved original.
    backup: str
    created_at: str = ""


@dataclass(frozen=True)
class GameState:
    game_id: str
    root: str
    #: False after a purge: mods stay installed but nothing is in the game folder.
    deployed: bool


@dataclass(frozen=True)
class JournalEntry:
    id: int
    ts: str
    game_id: str | None
    action: str
    path: str | None
    #: The other path involved (link target, backup location ...).
    source: str | None
    detail: str | None
    #: "intent" (started), "done", "failed", "skipped" or "abandoned".
    status: str


BUSY_MESSAGE = "another bethmm command is using the manifest right now; try again when it has finished"


def _is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)


class Manifest:
    def __init__(self, path: str | Path | None = None, *, lock: bool = False) -> None:
        self.path = Path(path) if path is not None else paths.manifest_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd: int | None = None
        self._db: sqlite3.Connection | None = None
        if lock:
            self._acquire_lock()
        try:
            self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException as exc:
            if self._db is not None:
                self._db.close()
            self._release_lock()
            if _is_locked(exc):
                raise ManifestLocked(BUSY_MESSAGE) from exc
            if isinstance(exc, sqlite3.DatabaseError):
                raise ManifestError(f"{self.path} is not a readable manifest: {exc}") from exc
            raise

    # ------------------------------------------------------------ lifecycle

    def _acquire_lock(self) -> None:
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ManifestLocked(
                "another bethmm process is changing mods right now; wait for it to finish"
            ) from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def close(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
        finally:
            self._release_lock()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        # Check the version before creating anything, so a newer manifest is left untouched.
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
            row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            try:
                version = int(row["value"]) if row is not None else None
            except ValueError:
                raise ManifestError(f"{self.path} has an unreadable schema version {row['value']!r}") from None
            if version is not None and version > SCHEMA_VERSION:
                raise ManifestError(
                    f"{self.path} was written by a newer version of the app (schema {version}); update the app"
                )
            if version == SCHEMA_VERSION:
                return  # nothing to write: opening never waits for another process's changes
        self._db.executescript(_SCHEMA)  # creates whatever an older schema lacks
        columns = {r["name"] for r in self._db.execute("PRAGMA table_info(deployed)")}
        for column in ("size", "mtime_ns"):
            if column not in columns:
                try:
                    self._db.execute(f"ALTER TABLE deployed ADD COLUMN {column} INTEGER")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):  # another process upgraded it just now
                        raise
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several changes so they are saved together or not at all."""
        if self._db.in_transaction:
            yield
            return
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_locked(exc):
                raise ManifestLocked(BUSY_MESSAGE) from exc
            raise
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # ------------------------------------------------------------ games

    def game_state(self, game_id: str) -> GameState | None:
        row = self._db.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
        return GameState(row["game_id"], row["root"], bool(row["deployed"])) if row else None

    def set_game_state(self, game_id: str, root: str | Path, deployed: bool) -> None:
        self._db.execute(
            "INSERT INTO games (game_id, root, deployed) VALUES (?, ?, ?) "
            "ON CONFLICT (game_id) DO UPDATE SET root = excluded.root, deployed = excluded.deployed",
            (game_id, str(root), int(deployed)),
        )

    def game_ids(self) -> list[str]:
        """Every game with installed mods or anything recorded in its folder."""
        rows = self._db.execute(
            "SELECT game_id FROM mods UNION SELECT game_id FROM deployed UNION SELECT game_id FROM backups "
            "UNION SELECT game_id FROM created_dirs UNION SELECT game_id FROM games ORDER BY game_id"
        ).fetchall()
        return [r["game_id"] for r in rows]

    # ------------------------------------------------------------ mods

    @staticmethod
    def _mod(row: sqlite3.Row) -> ModRecord:
        try:
            plan = json.loads(row["plan"] or "{}")
        except ValueError:
            plan = {}
        return ModRecord(
            id=row["id"],
            game_id=row["game_id"],
            name=row["name"],
            folder=row["folder"],
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            version=row["version"],
            nexus_id=row["nexus_id"],
            source_archive=row["source_archive"],
            installed_at=row["installed_at"],
            plan=plan if isinstance(plan, dict) else {},
        )

    def add_mod(
        self,
        game_id: str,
        name: str,
        folder: str,
        files: Sequence[ModFile],
        *,
        version: str | None = None,
        nexus_id: int | None = None,
        source_archive: str | None = None,
        plan: dict | None = None,
        enabled: bool = True,
    ) -> ModRecord:
        """Add a mod at the bottom of the list (highest priority)."""
        rows = self._file_rows(files)
        with self.transaction(), self._folder_taken(game_id, folder):
            top = self._db.execute(
                "SELECT COALESCE(MAX(priority), 0) AS p FROM mods WHERE game_id = ?", (game_id,)
            ).fetchone()["p"]
            cur = self._db.execute(
                "INSERT INTO mods (game_id, name, folder, priority, enabled, version, nexus_id, source_archive, "
                "installed_at, plan) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game_id, name, folder, top + 1, int(enabled), version, nexus_id, source_archive, now(),
                 json.dumps(plan or {}, sort_keys=True)),
            )
            mod_id = cur.lastrowid
            self._insert_files(mod_id, rows)
        return self.mod(mod_id)

    @contextmanager
    def _folder_taken(self, game_id: str, folder: str | None) -> Iterator[None]:
        try:
            yield
        except sqlite3.IntegrityError as exc:
            raise ManifestError(f"another mod of {game_id} already uses the staging folder {folder!r}") from exc

    @staticmethod
    def _file_rows(files: Sequence[ModFile]) -> dict[str, tuple[str, str]]:
        """Validated files keyed by folded destination; a later duplicate (by
        case) replaces the earlier, so each mod has one file per destination."""
        rows: dict[str, tuple[str, str]] = {}
        for f in files:
            source = f.source.replace("\\", "/")
            if not source.strip("/") or source.startswith("/") or ".." in source.split("/"):
                raise ManifestError(f"invalid staged file path {f.source!r}")
            try:
                key = fold_path(f.dest)
            except ValueError as exc:
                raise ManifestError(f"invalid destination {f.dest!r}: {exc}") from None
            rows[key] = (f.source, f.dest)
        return rows

    def _insert_files(self, mod_id: int, rows: dict[str, tuple[str, str]]) -> None:
        self._db.executemany(
            "INSERT INTO mod_files (mod_id, source, dest, dest_key) VALUES (?, ?, ?, ?)",
            [(mod_id, source, dest, key) for key, (source, dest) in rows.items()],
        )

    def update_mod(
        self,
        mod_id: int,
        *,
        folder: str | None = None,
        files: Sequence[ModFile] | None = None,
        name: str | None = None,
        version: "str | None | object" = KEEP,
        nexus_id: "int | None | object" = KEEP,
        source_archive: "str | None | object" = KEEP,
        plan: dict | None = None,
    ) -> ModRecord:
        """Replace a mod's files and/or details, keeping its place in the list.

        ``version``, ``nexus_id`` and ``source_archive`` are left alone unless
        given; passing None clears them (an update from a non-Nexus archive)."""
        rows = self._file_rows(files) if files is not None else None
        game_id = self.mod(mod_id).game_id
        with self.transaction(), self._folder_taken(game_id, folder):
            sets, values = [], []
            for column, value in (("folder", folder), ("name", name)):
                if value is not None:
                    sets.append(f"{column} = ?")
                    values.append(value)
            for column, value in (("version", version), ("nexus_id", nexus_id), ("source_archive", source_archive)):
                if value is not KEEP:
                    sets.append(f"{column} = ?")
                    values.append(value)
            if plan is not None:
                sets.append("plan = ?")
                values.append(json.dumps(plan, sort_keys=True))
            if sets:
                self._db.execute(f"UPDATE mods SET {', '.join(sets)} WHERE id = ?", (*values, mod_id))
            if rows is not None:
                self._db.execute("DELETE FROM mod_files WHERE mod_id = ?", (mod_id,))
                self._insert_files(mod_id, rows)
        return self.mod(mod_id)

    def mod(self, mod_id: int) -> ModRecord:
        row = self._db.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
        if row is None:
            raise ManifestError(f"no mod with id {mod_id}")
        return self._mod(row)

    def mods(self, game_id: str) -> list[ModRecord]:
        """Installed mods of *game_id*, lowest priority first (the bottom wins)."""
        rows = self._db.execute("SELECT * FROM mods WHERE game_id = ? ORDER BY priority, id", (game_id,)).fetchall()
        return [self._mod(r) for r in rows]

    def find_mod(self, game_id: str, query: str) -> ModRecord:
        """A mod by its position in the list ("3"), its id ("#12"), its exact
        name (any case), or a unique part of its name."""
        mods = self.mods(game_id)
        q = query.strip()
        if not q:
            raise ManifestError("no mod given")
        if q.startswith("#") and q[1:].isdecimal():
            match = [m for m in mods if m.id == int(q[1:])]
        elif q.isdecimal():
            index = int(q)
            match = [mods[index - 1]] if 1 <= index <= len(mods) else []
        else:
            low = q.lower()
            match = [m for m in mods if m.name.lower() == low or m.folder.lower() == low]
            if not match:
                match = [m for m in mods if low in m.name.lower()]
        if len(match) == 1:
            return match[0]
        if not match:
            raise ManifestError(f"no installed mod matches {query!r}")
        names = ", ".join(repr(m.name) for m in match[:5])
        raise ManifestError(f"{query!r} matches several mods ({names}); be more specific or use the number from 'list'")

    def mod_files(self, mod_id: int) -> list[ModFile]:
        rows = self._db.execute("SELECT source, dest FROM mod_files WHERE mod_id = ? ORDER BY dest_key", (mod_id,))
        return [ModFile(r["source"], r["dest"]) for r in rows]

    def set_enabled(self, mod_id: int, enabled: bool) -> None:
        self._db.execute("UPDATE mods SET enabled = ? WHERE id = ?", (int(enabled), mod_id))

    def set_order(self, game_id: str, ordered_ids: Sequence[int]) -> None:
        """Renumber priorities: *ordered_ids* lists every mod of the game, lowest priority first."""
        ordered_ids = list(ordered_ids)
        current = {m.id for m in self.mods(game_id)}
        if set(ordered_ids) != current or len(ordered_ids) != len(current):
            raise ManifestError("the new order must list every installed mod of the game exactly once")
        with self.transaction():
            for priority, mod_id in enumerate(ordered_ids, start=1):
                self._db.execute("UPDATE mods SET priority = ? WHERE id = ?", (priority, mod_id))

    def remove_mod(self, mod_id: int) -> None:
        if self._db.execute("SELECT 1 FROM deployed WHERE mod_id = ?", (mod_id,)).fetchone():
            raise ManifestError("the mod still has files deployed; undeploy it first")
        self._db.execute("DELETE FROM mods WHERE id = ?", (mod_id,))

    # ------------------------------------------------------------ deployment state

    def deployed(self, game_id: str) -> dict[str, DeployedFile]:
        """Links the app created in the game folder, keyed by folded path."""
        rows = self._db.execute("SELECT * FROM deployed WHERE game_id = ?", (game_id,))
        return {
            r["path_key"]: DeployedFile(r["game_id"], r["path"], r["mod_id"], r["source"], r["link_type"],
                                        r["inode"], r["dev"], r["size"], r["mtime_ns"])
            for r in rows
        }

    def record_link(self, link: DeployedFile) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO deployed (game_id, path, path_key, mod_id, source, link_type, inode, dev, "
            "size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (link.game_id, link.path, fold_path(link.path), link.mod_id, link.source, link.link_type,
             link.inode, link.dev, link.size, link.mtime_ns),
        )

    def forget_link(self, game_id: str, path: str) -> None:
        self._db.execute("DELETE FROM deployed WHERE game_id = ? AND path_key = ?", (game_id, fold_path(path)))

    def backups(self, game_id: str) -> dict[str, BackupRecord]:
        rows = self._db.execute("SELECT * FROM backups WHERE game_id = ?", (game_id,))
        return {r["path_key"]: BackupRecord(r["game_id"], r["path"], r["backup"], r["created_at"]) for r in rows}

    def record_backup(self, backup: BackupRecord) -> None:
        self._db.execute(
            "INSERT INTO backups (game_id, path, path_key, backup, created_at) VALUES (?, ?, ?, ?, ?)",
            (backup.game_id, backup.path, fold_path(backup.path), backup.backup, backup.created_at or now()),
        )

    def forget_backup(self, game_id: str, path: str) -> None:
        self._db.execute("DELETE FROM backups WHERE game_id = ? AND path_key = ?", (game_id, fold_path(path)))

    def created_dirs(self, game_id: str) -> dict[str, str]:
        """Folders the app created, folded key -> path as on disk."""
        rows = self._db.execute("SELECT path, path_key FROM created_dirs WHERE game_id = ?", (game_id,))
        return {r["path_key"]: r["path"] for r in rows}

    def record_dir(self, game_id: str, path: str) -> None:
        self._db.execute(
            "INSERT INTO created_dirs (game_id, path, path_key) VALUES (?, ?, ?) "
            "ON CONFLICT (game_id, path_key) DO UPDATE SET path = excluded.path",
            (game_id, path, fold_path(path)),
        )

    def forget_dir(self, game_id: str, path: str) -> None:
        self._db.execute("DELETE FROM created_dirs WHERE game_id = ? AND path_key = ?", (game_id, fold_path(path)))

    def left_alone(self, game_id: str) -> dict[str, str]:
        """Paths holding a file of the user's where the app's link was, folded key -> path."""
        rows = self._db.execute("SELECT path, path_key FROM left_alone WHERE game_id = ?", (game_id,))
        return {r["path_key"]: r["path"] for r in rows}

    def record_left_alone(self, game_id: str, path: str) -> None:
        self._db.execute(
            "INSERT INTO left_alone (game_id, path, path_key) VALUES (?, ?, ?) "
            "ON CONFLICT (game_id, path_key) DO UPDATE SET path = excluded.path",
            (game_id, path, fold_path(path)),
        )

    def forget_left_alone(self, game_id: str, path: str) -> None:
        self._db.execute("DELETE FROM left_alone WHERE game_id = ? AND path_key = ?", (game_id, fold_path(path)))

    def has_footprint(self, game_id: str | None = None, *, dirs: bool = True) -> bool:
        """Whether the app has anything in a game folder: links, backups,
        operations an interrupted run started, and with *dirs* the folders it made."""
        where, args = ("WHERE game_id = ?", (game_id,)) if game_id is not None else ("", ())
        tables = ("deployed", "backups", "created_dirs") if dirs else ("deployed", "backups")
        for table in tables:
            if self._db.execute(f"SELECT 1 FROM {table} {where} LIMIT 1", args).fetchone():
                return True
        intent = f"{where} AND status = 'intent'" if where else "WHERE status = 'intent'"
        return self._db.execute(f"SELECT 1 FROM journal {intent} LIMIT 1", args).fetchone() is not None

    # ------------------------------------------------------------ journal

    def journal(self, game_id: str | None, action: str, path: str | None = None,
                source: str | None = None, detail: str | None = None, status: str = "done") -> int:
        """Append a journal entry and return its id. Saved immediately, so an
        "intent" entry survives a crash in the middle of the operation."""
        cur = self._db.execute(
            "INSERT INTO journal (ts, game_id, action, path, source, detail, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), game_id, action, path, source, detail, status),
        )
        return cur.lastrowid

    def finish_journal(self, entry_id: int, status: str, detail: str | None = None) -> None:
        if detail is None:
            self._db.execute("UPDATE journal SET status = ? WHERE id = ?", (status, entry_id))
        else:
            self._db.execute("UPDATE journal SET status = ?, detail = ? WHERE id = ?", (status, detail, entry_id))

    @staticmethod
    def _entry(r: sqlite3.Row) -> JournalEntry:
        return JournalEntry(r["id"], r["ts"], r["game_id"], r["action"], r["path"], r["source"], r["detail"],
                            r["status"])

    def unfinished_journal(self, game_id: str) -> list[JournalEntry]:
        """Operations that were started but never finished (an interrupted run)."""
        rows = self._db.execute(
            "SELECT * FROM journal WHERE game_id = ? AND status = 'intent' ORDER BY id", (game_id,)
        ).fetchall()
        return [self._entry(r) for r in rows]

    def history(self, game_id: str | None = None, limit: int = 50) -> list[JournalEntry]:
        """Most recent journal entries, oldest first."""
        where, args = ("WHERE game_id = ?", (game_id,)) if game_id is not None else ("", ())
        rows = self._db.execute(f"SELECT * FROM journal {where} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._entry(r) for r in reversed(rows)]
