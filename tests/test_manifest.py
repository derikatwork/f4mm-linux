"""Tests for the SQLite manifest (bethmm.manifest)."""

from __future__ import annotations

import sqlite3

import pytest

from bethmm import paths
from bethmm.manifest import (
    SCHEMA_VERSION,
    BackupRecord,
    DeployedFile,
    Manifest,
    ManifestError,
    ManifestLocked,
    ModFile,
)

GAME = "fallout4"


@pytest.fixture
def m():
    with Manifest(lock=True) as manifest:
        yield manifest


def files(*dests: str) -> list[ModFile]:
    return [ModFile(d, d) for d in dests]


# ---------------------------------------------------------------- schema


def test_creates_schema_in_data_dir():
    with Manifest(lock=True) as manifest:
        assert manifest.path == paths.manifest_path()
    assert paths.manifest_path().is_file()
    db = sqlite3.connect(paths.manifest_path())
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "games", "mods", "mod_files", "deployed", "backups", "created_dirs", "journal"} <= tables
    assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    db.close()


def test_a_view_never_creates_the_manifest(tmp_path):
    """Opened without the lock (to look, or for a dry run), nothing is written."""
    path = tmp_path / "sub" / "m.db"
    with Manifest(path) as view:
        assert view.mods(GAME) == []
        with view.transaction():  # a preview: rolled back by the caller, here kept in memory only
            view.add_mod(GAME, "A", "A", files("Data/a.esp"))
    assert not path.exists() and not path.parent.exists()


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "sub" / "m.db"
    with Manifest(path, lock=True) as manifest:
        manifest.add_mod(GAME, "A", "A", files("Data/a.esp"))
    with Manifest(path) as manifest:
        assert [mod.name for mod in manifest.mods(GAME)] == ["A"]


def test_newer_schema_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),))
    db.commit()
    db.close()
    with pytest.raises(ManifestError, match="newer version"):
        Manifest(path)
    db = sqlite3.connect(path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    db.close()
    assert tables == {"meta"}  # nothing was created in the newer manifest


def test_newer_schema_releases_lock(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('schema_version', '999')")
    db.commit()
    db.close()
    with pytest.raises(ManifestError):
        Manifest(path, lock=True)
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = ?", (str(SCHEMA_VERSION),))
    db.commit()
    db.close()
    with Manifest(path, lock=True):
        pass


def test_corrupt_database_is_a_manifest_error(tmp_path):
    path = tmp_path / "m.db"
    path.write_bytes(b"this is not a sqlite database at all" * 100)
    with pytest.raises(ManifestError, match="not a readable manifest"):
        Manifest(path, lock=True)
    path.unlink()
    with Manifest(path, lock=True):  # lock was released
        pass


def test_older_schema_without_version_row_gets_one(tmp_path):
    path = tmp_path / "m.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    db.close()
    with Manifest(path, lock=True):
        pass
    db = sqlite3.connect(path)
    assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    db.close()


# ---------------------------------------------------------------- mods


def test_add_mod_goes_to_the_bottom_with_highest_priority(m):
    a = m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    b = m.add_mod(GAME, "B", "B", files("Data/b.esp"))
    c = m.add_mod(GAME, "C", "C", files("Data/c.esp"), enabled=False)
    assert a.priority < b.priority < c.priority
    assert [mod.id for mod in m.mods(GAME)] == [a.id, b.id, c.id]
    assert c.enabled is False and a.enabled is True
    assert m.mods("skyrimse") == []


def test_add_mod_stores_details_and_staging_dir(m):
    mod = m.add_mod(GAME, "Armor", "Armor (2)", files("Data/a.esp"), version="1.2", nexus_id=42,
                    source_archive="/dl/armor.7z", plan={"root": "x", "rule": "data"})
    again = m.mod(mod.id)
    assert again == mod
    assert (again.version, again.nexus_id, again.source_archive) == ("1.2", 42, "/dl/armor.7z")
    assert again.plan == {"root": "x", "rule": "data"}
    assert again.installed_at
    assert again.staging_dir() == paths.mods_dir(GAME) / "Armor (2)"


def test_priorities_are_per_game(m):
    m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    s = m.add_mod("skyrimse", "S", "S", files("Data/s.esp"))
    assert s.priority == 1


def test_duplicate_folder_is_a_manifest_error(m):
    m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    with pytest.raises(ManifestError, match="staging folder"):
        m.add_mod(GAME, "A again", "A", files("Data/b.esp"))
    assert [mod.name for mod in m.mods(GAME)] == ["A"]
    m.add_mod("skyrimse", "A", "A", files("Data/a.esp"))  # same folder name, other game: fine


@pytest.mark.parametrize("source", ["/etc/passwd", "../outside.txt", "a/../../b", "", "/"])
def test_add_mod_rejects_bad_source(m, source):
    with pytest.raises(ManifestError, match="invalid staged file"):
        m.add_mod(GAME, "A", "A", [ModFile(source, "Data/a.esp")])
    assert m.mods(GAME) == []


@pytest.mark.parametrize("dest", ["/abs/path", "../x", "Data/../../x", "", "C:/Windows/x"])
def test_add_mod_rejects_bad_dest(m, dest):
    with pytest.raises(ManifestError, match="invalid destination"):
        m.add_mod(GAME, "A", "A", [ModFile("a.esp", dest)])
    assert m.mods(GAME) == []


def test_mod_files_deduplicates_by_folded_dest(m):
    mod = m.add_mod(GAME, "A", "A", [
        ModFile("one/Textures/a.dds", "Data/Textures/a.dds"),
        ModFile("two/textures/A.DDS", "data/textures/A.DDS"),
        ModFile("b.esp", "Data/b.esp"),
    ])
    got = m.mod_files(mod.id)
    assert len(got) == 2
    # The later duplicate replaces the earlier.
    assert ModFile("two/textures/A.DDS", "data/textures/A.DDS") in got
    assert ModFile("b.esp", "Data/b.esp") in got


def test_mod_files_backslashes_fold_like_slashes(m):
    mod = m.add_mod(GAME, "A", "A", [ModFile("a.dds", "Data\\Textures\\a.dds"),
                                     ModFile("b.dds", "Data/Textures/A.dds")])
    assert len(m.mod_files(mod.id)) == 1


def test_mod_lookup_errors(m):
    with pytest.raises(ManifestError):
        m.mod(12345)


def test_update_mod_keeps_priority_and_enabled(m):
    a = m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    b = m.add_mod(GAME, "B", "B", files("Data/b.esp"))
    m.add_mod(GAME, "C", "C", files("Data/c.esp"))
    m.set_enabled(b.id, False)
    updated = m.update_mod(b.id, folder="B v2", files=files("Data/b2.esp", "Data/b3.esp"), name="B2", version="2.0",
                           plan={"rule": "x"})
    assert (updated.priority, updated.enabled) == (b.priority, False)
    assert (updated.folder, updated.name, updated.version, updated.plan) == ("B v2", "B2", "2.0", {"rule": "x"})
    assert [f.dest for f in m.mod_files(b.id)] == ["Data/b2.esp", "Data/b3.esp"]
    assert [mod.id for mod in m.mods(GAME)][1] == b.id
    # Without files the files stay.
    m.update_mod(b.id, name="B3")
    assert len(m.mod_files(b.id)) == 2
    assert m.mod(a.id).name == "A"


def test_update_mod_to_taken_folder_fails_without_changes(m):
    a = m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    m.add_mod(GAME, "B", "B", files("Data/b.esp"))
    with pytest.raises(ManifestError):
        m.update_mod(a.id, folder="B", files=files("Data/x.esp"))
    assert m.mod(a.id).folder == "A"
    assert [f.dest for f in m.mod_files(a.id)] == ["Data/a.esp"]


def test_update_mod_bad_files_change_nothing(m):
    a = m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    with pytest.raises(ManifestError):
        m.update_mod(a.id, name="new", files=[ModFile("x", "../escape")])
    assert m.mod(a.id).name == "A"
    assert [f.dest for f in m.mod_files(a.id)] == ["Data/a.esp"]


# ---------------------------------------------------------------- find_mod


@pytest.fixture
def three(m):
    a = m.add_mod(GAME, "Armor Retexture", "Armor Retexture", files("Data/a.esp"))
    b = m.add_mod(GAME, "Armor Retexture HD", "armor-hd", files("Data/b.esp"))
    c = m.add_mod(GAME, "Weapon Pack", "Weapons", files("Data/c.esp"))
    return a, b, c


def test_find_mod_by_position(m, three):
    a, b, c = three
    assert m.find_mod(GAME, "1").id == a.id
    assert m.find_mod(GAME, " 3 ").id == c.id
    for bad in ("0", "4", "99"):
        with pytest.raises(ManifestError, match="no installed mod"):
            m.find_mod(GAME, bad)


def test_find_mod_by_id(m, three):
    a, b, c = three
    assert m.find_mod(GAME, f"#{b.id}").id == b.id
    with pytest.raises(ManifestError):
        m.find_mod(GAME, "#999")


def test_find_mod_by_exact_name_folder_or_unique_substring(m, three):
    a, b, c = three
    # "Armor Retexture" is also a substring of the HD one: the exact name wins.
    assert m.find_mod(GAME, "armor retexture").id == a.id
    assert m.find_mod(GAME, "ARMOR-HD").id == b.id
    assert m.find_mod(GAME, "weapons").id == c.id
    assert m.find_mod(GAME, "pack").id == c.id
    assert m.find_mod(GAME, "HD").id == b.id


def test_find_mod_ambiguous_and_missing(m, three):
    with pytest.raises(ManifestError, match="several mods"):
        m.find_mod(GAME, "armor")
    with pytest.raises(ManifestError, match="no installed mod"):
        m.find_mod(GAME, "nothing like this")
    with pytest.raises(ManifestError):
        m.find_mod(GAME, "")
    with pytest.raises(ManifestError):
        m.find_mod(GAME, "   ")


def test_find_mod_same_name_twice_is_ambiguous(m):
    m.add_mod(GAME, "Same", "Same", files("Data/a.esp"))
    m.add_mod(GAME, "Same", "Same (2)", files("Data/b.esp"))
    with pytest.raises(ManifestError, match="several"):
        m.find_mod(GAME, "same")
    assert m.find_mod(GAME, "same (2)").folder == "Same (2)"


@pytest.mark.parametrize("query", ["²", "#²", "³4", "½"])
def test_find_mod_unicode_digits_do_not_crash(m, three, query):
    with pytest.raises(ManifestError):
        m.find_mod(GAME, query)


def test_find_mod_other_game_is_not_found(m, three):
    with pytest.raises(ManifestError):
        m.find_mod("skyrimse", "1")


# ---------------------------------------------------------------- order / enable / remove


def test_set_order_renumbers(m, three):
    a, b, c = three
    m.set_order(GAME, [c.id, a.id, b.id])
    assert [mod.id for mod in m.mods(GAME)] == [c.id, a.id, b.id]
    assert [mod.priority for mod in m.mods(GAME)] == [1, 2, 3]
    m.set_order(GAME, iter([b.id, c.id, a.id]))  # any iterable
    assert [mod.id for mod in m.mods(GAME)] == [b.id, c.id, a.id]


@pytest.mark.parametrize("make_order", [
    lambda a, b, c: [a.id, b.id],                  # missing one
    lambda a, b, c: [a.id, b.id, c.id, c.id],      # duplicate
    lambda a, b, c: [a.id, b.id, b.id],            # duplicate instead of one
    lambda a, b, c: [a.id, b.id, c.id, 999],       # unknown
    lambda a, b, c: [],
])
def test_set_order_validation(m, three, make_order):
    before = [mod.id for mod in m.mods(GAME)]
    with pytest.raises(ManifestError, match="exactly once"):
        m.set_order(GAME, make_order(*three))
    assert [mod.id for mod in m.mods(GAME)] == before


def test_set_order_rejects_mod_of_other_game(m, three):
    a, b, c = three
    s = m.add_mod("skyrimse", "S", "S", files("Data/s.esp"))
    with pytest.raises(ManifestError):
        m.set_order(GAME, [a.id, b.id, s.id])


def test_set_enabled(m, three):
    a, _, _ = three
    m.set_enabled(a.id, False)
    assert m.mod(a.id).enabled is False
    m.set_enabled(a.id, True)
    assert m.mod(a.id).enabled is True


def test_remove_mod_refuses_while_links_are_deployed(m, three):
    a, b, _ = three
    m.record_link(DeployedFile(GAME, "Data/a.esp", a.id, "/staged/a.esp", "symlink"))
    with pytest.raises(ManifestError, match="undeploy"):
        m.remove_mod(a.id)
    assert m.mod(a.id)
    m.remove_mod(b.id)
    with pytest.raises(ManifestError):
        m.mod(b.id)
    assert m.mod_files(b.id) == []  # cascaded
    m.forget_link(GAME, "data/A.ESP")
    m.remove_mod(a.id)
    assert [mod.name for mod in m.mods(GAME)] == ["Weapon Pack"]


def test_add_after_remove_still_goes_to_bottom(m, three):
    a, b, c = three
    m.remove_mod(c.id)
    d = m.add_mod(GAME, "D", "D", files("Data/d.esp"))
    assert [mod.id for mod in m.mods(GAME)] == [a.id, b.id, d.id]


# ---------------------------------------------------------------- deployment state


def test_game_state(m):
    assert m.game_state(GAME) is None
    m.set_game_state(GAME, "/games/fo4", deployed=True)
    state = m.game_state(GAME)
    assert (state.root, state.deployed) == ("/games/fo4", True)
    m.set_game_state(GAME, "/games/fo4b", deployed=False)
    state = m.game_state(GAME)
    assert (state.root, state.deployed) == ("/games/fo4b", False)
    assert m.game_ids() == [GAME]


def test_deployed_crud_with_folded_keys(m):
    link = DeployedFile(GAME, "Data/Textures/A.dds", 3, "/s/a.dds", "hardlink", 11, 22)
    m.record_link(link)
    assert m.deployed(GAME) == {"DATA/TEXTURES/A.DDS": link}
    # Same folded path replaces the record.
    other = DeployedFile(GAME, "data/textures/a.DDS", 4, "/s/b.dds", "symlink")
    m.record_link(other)
    assert m.deployed(GAME) == {"DATA/TEXTURES/A.DDS": other}
    assert m.deployed("skyrimse") == {}
    m.forget_link(GAME, "DATA/textures/A.dds")
    assert m.deployed(GAME) == {}


def test_backups_crud_with_folded_keys(m):
    m.record_backup(BackupRecord(GAME, "Data/Fallout4.esm", "/b/Data/Fallout4.esm"))
    got = m.backups(GAME)
    assert list(got) == ["DATA/FALLOUT4.ESM"]
    assert got["DATA/FALLOUT4.ESM"].backup == "/b/Data/Fallout4.esm"
    assert got["DATA/FALLOUT4.ESM"].created_at  # filled in
    with pytest.raises(sqlite3.IntegrityError):
        m.record_backup(BackupRecord(GAME, "data/fallout4.ESM", "/b/other"))
    m.forget_backup(GAME, "data/FALLOUT4.esm")
    assert m.backups(GAME) == {}


def test_created_dirs_crud_with_folded_keys(m):
    m.record_dir(GAME, "Data/NewFolder")
    m.record_dir(GAME, "Data/NewFolder/Sub")
    assert m.created_dirs(GAME) == {"DATA/NEWFOLDER": "Data/NewFolder", "DATA/NEWFOLDER/SUB": "Data/NewFolder/Sub"}
    # Recording again under another spelling updates the spelling (the folder was recreated).
    m.record_dir(GAME, "Data/newfolder")
    assert m.created_dirs(GAME)["DATA/NEWFOLDER"] == "Data/newfolder"
    m.forget_dir(GAME, "DATA/NEWFOLDER/sub")
    assert list(m.created_dirs(GAME)) == ["DATA/NEWFOLDER"]


def test_left_alone_crud_with_folded_keys(m):
    assert m.left_alone(GAME) == {}
    m.record_left_alone(GAME, "Data/Textures/A.dds")
    m.record_left_alone(GAME, "data/textures/a.DDS")  # same path: spelling updated
    assert m.left_alone(GAME) == {"DATA/TEXTURES/A.DDS": "data/textures/a.DDS"}
    assert m.left_alone("skyrimse") == {}
    assert not m.has_footprint(GAME)  # nothing of the app's is in the folder
    m.forget_left_alone(GAME, "DATA/Textures/A.dds")
    assert m.left_alone(GAME) == {}


def test_schema_1_is_upgraded(tmp_path):
    path = tmp_path / "m.db"
    with Manifest(path, lock=True) as manifest:
        manifest.add_mod(GAME, "A", "A", files("Data/a.esp"))
    db = sqlite3.connect(path)
    db.execute("DROP TABLE left_alone")
    # The version 1 table had no size / mtime_ns.
    db.execute("DROP TABLE deployed")
    db.execute("CREATE TABLE deployed (game_id TEXT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL, "
               "mod_id INTEGER NOT NULL, source TEXT NOT NULL, link_type TEXT NOT NULL, inode INTEGER, dev INTEGER, "
               "PRIMARY KEY (game_id, path_key))")
    db.execute("INSERT INTO deployed VALUES ('fallout4', 'Data/old.esp', 'DATA/OLD.ESP', 1, '/s', 'hardlink', 5, 6)")
    db.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    view_before = path.read_bytes()
    with Manifest(path) as view:  # a view (dry run) upgrades a copy, never the file
        assert [mod.name for mod in view.mods(GAME)] == ["A"]
        assert view.deployed(GAME)["DATA/OLD.ESP"].size is None
    assert path.read_bytes() == view_before
    with Manifest(path, lock=True) as manifest:
        assert [mod.name for mod in manifest.mods(GAME)] == ["A"]
        manifest.record_left_alone(GAME, "Data/x")
        old = manifest.deployed(GAME)["DATA/OLD.ESP"]
        assert (old.inode, old.dev, old.size, old.mtime_ns) == (5, 6, None, None)
        new = DeployedFile(GAME, "Data/new.esp", 1, "/s2", "hardlink", 7, 8, 100, 123456789)
        manifest.record_link(new)
        assert manifest.deployed(GAME)["DATA/NEW.ESP"] == new
    db = sqlite3.connect(path)
    assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    db.close()


def test_has_footprint(m):
    assert not m.has_footprint()
    assert not m.has_footprint(GAME)
    m.set_game_state(GAME, "/g", True)
    m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    assert not m.has_footprint(GAME)  # mods and game state alone are no footprint
    for record, forget in (
        (lambda: m.record_link(DeployedFile(GAME, "Data/a.esp", 1, "/s", "symlink")),
         lambda: m.forget_link(GAME, "Data/a.esp")),
        (lambda: m.record_backup(BackupRecord(GAME, "Data/a.esp", "/b")), lambda: m.forget_backup(GAME, "Data/a.esp")),
        (lambda: m.record_dir(GAME, "Data/X"), lambda: m.forget_dir(GAME, "Data/X")),
    ):
        record()
        assert m.has_footprint(GAME)
        assert m.has_footprint()
        assert not m.has_footprint("skyrimse")
        forget()
        assert not m.has_footprint(GAME)


def test_game_ids_union(m):
    m.add_mod("a", "A", "A", files("x"))
    m.record_link(DeployedFile("b", "x", 1, "/s", "symlink"))
    m.record_backup(BackupRecord("c", "x", "/b"))
    m.record_dir("d", "x")
    m.set_game_state("e", "/e", True)
    assert m.game_ids() == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------- journal


def test_journal_ids_finish_and_unfinished(m):
    first = m.journal(GAME, "link", "Data/a.esp", "/s/a.esp", status="intent")
    second = m.journal(GAME, "backup", "Data/b.esp", status="intent")
    third = m.journal("skyrimse", "link", "x", status="intent")
    assert first < second < third
    assert [e.id for e in m.unfinished_journal(GAME)] == [first, second]
    m.finish_journal(first, "done")
    m.finish_journal(second, "failed", "Permission denied")
    assert m.unfinished_journal(GAME) == []
    assert [e.id for e in m.unfinished_journal("skyrimse")] == [third]
    entries = {e.id: e for e in m.history(GAME)}
    assert entries[first].status == "done"
    assert entries[first].source == "/s/a.esp"
    assert entries[first].detail is None
    assert (entries[second].status, entries[second].detail) == ("failed", "Permission denied")


def test_history_is_oldest_first_and_limited(m):
    ids = [m.journal(GAME, "link", f"f{i}") for i in range(10)]
    m.journal("skyrimse", "link", "other")
    assert [e.id for e in m.history(GAME, limit=3)] == ids[-3:]
    assert [e.path for e in m.history(GAME)] == [f"f{i}" for i in range(10)]
    assert len(m.history(limit=100)) == 11
    assert m.history(limit=1)[0].path == "other"


def test_journal_intent_survives_a_crash(tmp_path):
    path = tmp_path / "m.db"
    manifest = Manifest(path, lock=True)
    entry = manifest.journal(GAME, "link", "Data/a.esp", status="intent")
    # Simulate the process dying: no close, no commit call.
    manifest._release_lock()
    del manifest
    with Manifest(path) as again:
        assert [e.id for e in again.unfinished_journal(GAME)] == [entry]


# ---------------------------------------------------------------- locking


def test_exclusive_lock(tmp_path):
    path = tmp_path / "m.db"
    first = Manifest(path, lock=True)
    try:
        with pytest.raises(ManifestLocked):
            Manifest(path, lock=True)
        # A read-only manifest still works while another process holds the lock.
        with Manifest(path) as reader:
            assert reader.mods(GAME) == []
        first.add_mod(GAME, "A", "A", files("Data/a.esp"))
        with Manifest(path) as reader:
            assert [mod.name for mod in reader.mods(GAME)] == ["A"]
    finally:
        first.close()
    with Manifest(path, lock=True) as second:
        assert [mod.name for mod in second.mods(GAME)] == ["A"]


def test_lock_released_by_context_manager_even_on_error(tmp_path):
    path = tmp_path / "m.db"
    with pytest.raises(RuntimeError):
        with Manifest(path, lock=True):
            raise RuntimeError("boom")
    with Manifest(path, lock=True):
        pass


def test_lock_is_a_manifest_error_subclass():
    assert issubclass(ManifestLocked, ManifestError)


def test_close_twice_is_harmless(tmp_path):
    manifest = Manifest(tmp_path / "m.db", lock=True)
    manifest.close()
    manifest.close()


# ---------------------------------------------------------------- transactions


def test_transaction_rolls_back_on_exception(m):
    m.add_mod(GAME, "A", "A", files("Data/a.esp"))
    with pytest.raises(RuntimeError):
        with m.transaction():
            m.record_dir(GAME, "Data/X")
            m.record_link(DeployedFile(GAME, "Data/a.esp", 1, "/s", "symlink"))
            raise RuntimeError("boom")
    assert m.created_dirs(GAME) == {}
    assert m.deployed(GAME) == {}
    assert [mod.name for mod in m.mods(GAME)] == ["A"]


def test_transaction_rolls_back_on_base_exception(m):
    with pytest.raises(KeyboardInterrupt):
        with m.transaction():
            m.record_dir(GAME, "Data/X")
            raise KeyboardInterrupt
    assert m.created_dirs(GAME) == {}
    # And the connection is usable afterwards.
    m.record_dir(GAME, "Data/Y")
    assert list(m.created_dirs(GAME)) == ["DATA/Y"]


def test_nested_transaction_rolls_back_everything(m):
    with pytest.raises(RuntimeError):
        with m.transaction():
            m.record_dir(GAME, "Data/Outer")
            with m.transaction():
                m.record_dir(GAME, "Data/Inner")
            raise RuntimeError
    assert m.created_dirs(GAME) == {}


def test_transaction_commits(tmp_path):
    path = tmp_path / "m.db"
    with Manifest(path, lock=True) as manifest:
        with manifest.transaction():
            manifest.record_dir(GAME, "Data/X")
    with Manifest(path) as manifest:
        assert list(manifest.created_dirs(GAME)) == ["DATA/X"]


def test_add_mod_is_atomic(m):
    # A bad file in the list leaves no half-added mod behind.
    with pytest.raises(ManifestError):
        m.add_mod(GAME, "A", "A", [ModFile("a", "Data/a.esp"), ModFile("b", "../b")])
    assert m.mods(GAME) == []
