from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from bethmm import deploy, paths, winecheck
from bethmm.discovery import DiscoveredGame
from bethmm.manifest import Manifest
from bethmm.winecheck import LinkCheck, check_links, find_wine, windows_path
from helpers import make_tree

FLATPAK = ".var/app/net.lutris.Lutris"
NATIVE_DATA = ".local/share/lutris"
FLATPAK_DATA = f"{FLATPAK}/data/lutris"
GE = "lutris-GE-Proton8-26-x86_64"

#: A stand-in for Wine that implements ``wine cmd /c type <path>``: C:\ maps to
#: $WINEPREFIX/drive_c, Z:\ to /, and the file is read following symlinks, as
#: Wine does. Each call is recorded in a JSON file next to the script.
FAKE_WINE = r'''#!{python}
import json, os, sys, time

MODE = {mode!r}
RECORD = {record!r}

args = sys.argv[1:]
call = {{"args": args, "WINEPREFIX": os.environ.get("WINEPREFIX"), "WINEDEBUG": os.environ.get("WINEDEBUG")}}
path = None
if args[:3] == ["cmd", "/c", "type"] and len(args) == 4:
    win = args[3]
    rest = win[3:].replace("\\", "/")
    if win[:3].upper() == "C:\\":
        path = os.path.join(os.environ["WINEPREFIX"], "drive_c", rest)
    elif win[:3].upper() == "Z:\\":
        path = "/" + rest
if path is not None and os.path.lexists(path):
    st = os.lstat(path)
    call["is_symlink"] = os.path.islink(path)
    call["target"] = os.readlink(path) if os.path.islink(path) else None
    call["nlink"] = st.st_nlink
calls = json.load(open(RECORD)) if os.path.exists(RECORD) else []
calls.append(call)
json.dump(calls, open(RECORD, "w"))

if MODE == "sleep":
    time.sleep(30)
elif MODE == "silent":
    sys.exit(0)
elif MODE == "fail":
    sys.stderr.write("wine: could not load kernel32.dll, status c0000135\n")
    sys.exit(1)
elif MODE == "nosymlink" and path is not None and os.path.islink(path):
    print("The system cannot find the file specified.")
    sys.exit(1)
elif MODE == "replace" and path is not None:
    os.unlink(path)
    with open(path, "w") as fh:
        fh.write("the user's own file\n")
    sys.exit(1)
if path is None or not os.path.exists(path):
    print("The system cannot find the file specified.")
    sys.exit(1)
sys.stdout.write(open(path, encoding="ascii").read())
if MODE == "linger":
    # Like a program that Wine's session start launches (a Run key, a
    # persistent wineserver): it inherits stdout/stderr and keeps running.
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        time.sleep(30)
        os._exit(0)
    open(RECORD + ".pid", "w").write(str(pid))
'''


# ---------------------------------------------------------------- builders


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture(autouse=True)
def empty_path(tmp_path, monkeypatch):
    """No real Wine on PATH: tests that want one put it in this folder."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def make_executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def fake_wine(directory: Path, mode: str = "ok") -> Path:
    record = directory / f"wine-{mode}.calls.json"
    text = FAKE_WINE.format(python=sys.executable, mode=mode, record=str(record))
    return make_executable(directory / f"wine-{mode}", text)


def calls(wine: Path | str) -> list[dict]:
    record = Path(f"{wine}.calls.json")
    return json.loads(record.read_text()) if record.exists() else []


def make_game(base: Path, *, inside_prefix: bool = True, data: bool = True, prefix: bool = True,
              slug: str | None = "fallout-4") -> DiscoveredGame:
    pfx = base / "Games" / "gog" / "fallout-4"
    make_tree(pfx, ["system.reg", "user.reg", "drive_c/windows/system32/", "drive_c/users/derik/"])
    root = pfx / "drive_c" / "GOG Games" / "Fallout 4 GOTY" if inside_prefix else base / "Library" / "Fallout 4"
    make_tree(root, {
        "Fallout4.exe": b"MZ",
        "Data/Fallout4.esm": b"TES4 vanilla",
        "Data/Textures/vanilla.dds": b"dds",
    })
    if not data:
        (root / "Data" / "Fallout4.esm").unlink()
        (root / "Data" / "Textures" / "vanilla.dds").unlink()
        (root / "Data" / "Textures").rmdir()
        (root / "Data").rmdir()
    return DiscoveredGame(
        game_id="fallout4",
        name="Fallout 4",
        root=root,
        exe=root / "Fallout4.exe",
        data_dir=root / "Data",
        prefix=pfx if prefix else None,
        lutris_slug=slug,
    )


def write_yml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def lutris_yml(prefix: Path | str | None, version: str | None = GE, **wine) -> dict:
    game = {"exe": "drive_c/GOG Games/Fallout 4 GOTY/Fallout4Launcher.exe"}
    if prefix is not None:
        game["prefix"] = str(prefix)
    if version is not None:
        wine["version"] = version
    return {"game": game, "system": {}, "wine": wine}


def runner(home: Path, data: str, version: str, kind: str = "wine", rel: str = "bin/wine") -> Path:
    return make_executable(home / data / "runners" / kind / version / rel)


def snapshot(root: Path) -> dict:
    """Everything about a tree except folder timestamps (which a created and
    removed file changes)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[p] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[p] = ("dir", st.st_mode)
            else:
                out[p] = ("file", st.st_mode, st.st_ino, st.st_nlink, st.st_mtime_ns,
                          hashlib.sha256(Path(p).read_bytes()).hexdigest())
    return out


def leftovers(game: DiscoveredGame) -> list[str]:
    """Test files or links that the check left behind."""
    found = [p.name for p in game.data_dir.iterdir() if p.name.lower().startswith("bethmm-linktest")]
    test_dir = paths.mods_dir(game.game_id) / ".linktest"
    if os.path.lexists(test_dir):
        found.append(str(test_dir))
    return found


@pytest.fixture
def fixed_token(monkeypatch):
    """Make the link name and file content predictable."""
    values = {4: "abcd1234", 16: "0123456789abcdef" * 2}
    monkeypatch.setattr(winecheck, "_random_token", lambda n: values[n])
    return values


# ---------------------------------------------------------------- windows_path


def test_windows_path_inside_drive_c(tmp_path):
    prefix = tmp_path / "pfx"
    path = prefix / "drive_c" / "GOG Games" / "Fallout 4 GOTY" / "Data" / "x.txt"
    assert windows_path(path, prefix) == "C:\\GOG Games\\Fallout 4 GOTY\\Data\\x.txt"
    assert windows_path(str(path), str(prefix)) == "C:\\GOG Games\\Fallout 4 GOTY\\Data\\x.txt"
    assert windows_path(prefix / "drive_c", prefix) == "C:\\"


def test_windows_path_outside_prefix_uses_z(tmp_path):
    prefix = tmp_path / "pfx"
    path = tmp_path / "Library" / "Skyrim" / "Data" / "x.txt"
    assert windows_path(path, prefix) == "Z:" + str(path).replace("/", "\\")
    assert windows_path(path, None) == "Z:" + str(path).replace("/", "\\")
    assert windows_path("/", prefix) == "Z:\\"
    # A folder merely named like drive_c is not inside it.
    assert windows_path(prefix / "drive_cx" / "a", prefix).startswith("Z:\\")
    assert windows_path(prefix / "a.txt", prefix).startswith("Z:\\")


def test_windows_path_through_symlinked_prefix(tmp_path):
    real = tmp_path / "real-pfx"
    (real / "drive_c" / "Games").mkdir(parents=True)
    alias = tmp_path / "alias-pfx"
    alias.symlink_to(real)
    assert windows_path(real / "drive_c" / "Games" / "a.txt", alias) == "C:\\Games\\a.txt"
    assert windows_path(alias / "drive_c" / "Games" / "a.txt", real) == "C:\\Games\\a.txt"


def test_windows_path_never_resolves_the_file_itself(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text("x")
    link = prefix / "drive_c" / "link.txt"
    link.symlink_to(target)
    assert windows_path(link, prefix) == "C:\\link.txt"


def test_windows_path_relative_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert windows_path("a/b.txt", None) == "Z:" + str(tmp_path / "a" / "b.txt").replace("/", "\\")


# ---------------------------------------------------------------- find_wine


def test_find_wine_native_lutris_runner_by_prefix(home, tmp_path):
    game = make_game(tmp_path, slug=None)
    write_yml(home / NATIVE_DATA / "games" / "something-else-1700000000.yml", lutris_yml(game.prefix))
    wine = runner(home, NATIVE_DATA, GE)
    runner(home, NATIVE_DATA, "lutris-9.0-x86_64")  # newer by name, but not the game's
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_prefix_compared_after_realpath(home, tmp_path):
    game = make_game(tmp_path, slug=None)
    alias = tmp_path / "alias"
    alias.symlink_to(game.prefix)
    write_yml(home / ".config/lutris/games" / "fo4-1.yml", lutris_yml(alias))
    wine = runner(home, NATIVE_DATA, GE)
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_tilde_prefix_in_yml(home):
    game = make_game(home)
    game.lutris_slug = None
    write_yml(home / NATIVE_DATA / "games" / "fo4-1.yml", lutris_yml("~/Games/gog/fallout-4"))
    wine = runner(home, NATIVE_DATA, GE)
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_by_slug(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "other-game-1600000000.yml", lutris_yml(tmp_path / "other", "lutris-7.2"))
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1700000000.yml", lutris_yml("relative/prefix"))
    runner(home, NATIVE_DATA, "lutris-7.2")
    wine = runner(home, NATIVE_DATA, GE)
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_prefix_match_beats_slug_match(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(tmp_path / "old-prefix", "lutris-7.2"))
    write_yml(home / NATIVE_DATA / "games" / "zzz-2.yml", lutris_yml(game.prefix))
    runner(home, NATIVE_DATA, "lutris-7.2")
    wine = runner(home, NATIVE_DATA, GE)
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_flatpak_proton_runner(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / FLATPAK_DATA / "games" / "fallout-4-1700000000.yml", lutris_yml(game.prefix, "GE-Proton9-20"))
    wine = runner(home, FLATPAK_DATA, "GE-Proton9-20", "proton", "files/bin/wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_native_proton_runner(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, "GE-Proton9-20"))
    wine = runner(home, NATIVE_DATA, "GE-Proton9-20", "proton", "files/bin/wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_flatpak_yml_prefers_flatpak_runner(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / f"{FLATPAK}/config/lutris/games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    runner(home, NATIVE_DATA, GE)
    flatpak = runner(home, FLATPAK_DATA, GE)
    assert find_wine(game, home=home) == str(flatpak)


def test_find_wine_falls_back_to_newest_runner(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, "not-installed"))
    runner(home, NATIVE_DATA, "lutris-7.2-2-x86_64")
    newest = runner(home, FLATPAK_DATA, "lutris-10.0-x86_64")
    runner(home, NATIVE_DATA, "lutris-9.0-x86_64")
    make_executable(home / NATIVE_DATA / "runners" / "wine" / "lutris-99.0" / "bin" / "wine").chmod(0o644)
    runner(home, NATIVE_DATA, "zz-proton-only", "proton", "files/bin/wine")
    assert find_wine(game, home=home) == str(newest)


def test_find_wine_without_any_yml_uses_runner(home, tmp_path):
    game = make_game(tmp_path)
    wine = runner(home, NATIVE_DATA, "lutris-8.0")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_falls_back_to_path(home, tmp_path, empty_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    wine = make_executable(empty_path / "wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_none(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    assert find_wine(game, home=home) is None


def test_find_wine_skips_non_executable_runner(home, tmp_path, empty_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    runner(home, NATIVE_DATA, GE).chmod(0o644)
    (home / NATIVE_DATA / "runners" / "wine" / "lutris-8.0" / "bin" / "wine").mkdir(parents=True)
    wine = make_executable(empty_path / "wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_system_version_means_path(home, tmp_path, empty_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, "system"))
    runner(home, NATIVE_DATA, GE)
    wine = make_executable(empty_path / "wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_custom_wine_path(home, tmp_path):
    game = make_game(tmp_path)
    custom = make_executable(tmp_path / "my-wine" / "bin" / "wine")
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml",
              lutris_yml(game.prefix, "custom", custom_wine_path=str(custom)))
    runner(home, NATIVE_DATA, GE)
    assert find_wine(game, home=home) == str(custom)


def test_find_wine_uses_lutris_default_runner_version(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, None))
    write_yml(home / NATIVE_DATA / "runners" / "wine.yml", {"wine": {"version": "lutris-7.2"}})
    wine = runner(home, NATIVE_DATA, "lutris-7.2")
    runner(home, NATIVE_DATA, "lutris-9.0")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_ignores_path_like_versions_and_broken_ymls(home, tmp_path, empty_path):
    game = make_game(tmp_path)
    cfg = home / NATIVE_DATA / "games"
    write_yml(cfg / "broken-1.yml", "game: [unclosed\n")
    write_yml(cfg / "list-2.yml", "- a\n- b\n")
    (cfg / "binary-3.yml").write_bytes(b"\xff\xfe\x00garbage")
    write_yml(cfg / "fallout-4-4.yml", lutris_yml(game.prefix, "../../../../evil"))
    make_executable(home / NATIVE_DATA / "runners" / "evil" / "bin" / "wine")
    wine = make_executable(empty_path / "wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_ignores_other_games_ymls(home, tmp_path, empty_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "oblivion-2.yml", lutris_yml(tmp_path / "obl", "GE-Proton9-20"))
    # Only reachable through the other game's yml (proton runners aren't a fallback).
    runner(home, FLATPAK_DATA, "GE-Proton9-20", "proton", "files/bin/wine")
    wine = make_executable(empty_path / "wine")
    assert find_wine(game, home=home) == str(wine)


# ---------------------------------------------------------------- check_links: success


def test_check_links_symlink_success(home, tmp_path, fixed_token, caplog):
    caplog.set_level(logging.INFO, logger="bethmm.winecheck")
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)
    before = snapshot(game.prefix)

    result = check_links(game, wine=str(wine), home=home)

    assert isinstance(result, LinkCheck)
    assert result.ok is True, result.message
    assert "symlink" in result.message and "will see" in result.message
    assert result.wine == str(wine)
    assert result.windows_path == "C:\\GOG Games\\Fallout 4 GOTY\\Data\\bethmm-linktest-abcd1234.txt"
    assert fixed_token[16] in result.output
    (call,) = calls(wine)
    assert call["args"] == ["cmd", "/c", "type", result.windows_path]
    assert call["WINEPREFIX"] == str(game.prefix)
    assert call["WINEDEBUG"] == "-all"
    test_file = paths.mods_dir("fallout4") / ".linktest" / "bethmm-linktest-abcd1234.txt"
    assert call["is_symlink"] and call["target"] == str(test_file)

    assert leftovers(game) == []
    assert snapshot(game.prefix) == before
    messages = [r.getMessage() for r in caplog.records if r.name == "bethmm.winecheck"]
    link = game.data_dir / "bethmm-linktest-abcd1234.txt"
    assert f"WRITE {test_file}" in messages
    assert f"LINK {link} -> {test_file} (symlink)" in messages
    assert f"UNLINK {link}" in messages
    assert f"DELETE {test_file}" in messages


def test_check_links_hardlink_success(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)
    before = snapshot(game.prefix)

    result = check_links(game, link_mode="hardlink", wine=str(wine), home=home)

    assert result.ok is True, result.message
    assert "hardlink" in result.message
    (call,) = calls(wine)
    assert call["is_symlink"] is False and call["nlink"] == 2
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before


def test_check_links_game_outside_prefix_uses_z_drive(home, tmp_path):
    game = make_game(tmp_path, inside_prefix=False)
    wine = fake_wine(tmp_path)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is True, result.message
    assert result.windows_path.startswith("Z:\\") and result.windows_path.endswith(".txt")
    assert leftovers(game) == []


def test_check_links_finds_lutris_runner(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    runners = home / NATIVE_DATA / "runners" / "wine" / GE / "bin"
    runners.mkdir(parents=True)
    wine = fake_wine(tmp_path)
    (runners / "wine").symlink_to(wine)

    result = check_links(game, home=home)

    assert result.ok is True, result.message
    assert result.wine == str(runners / "wine")
    assert "Flatpak" not in result.message
    assert len(calls(wine)) == 1


def test_check_links_flatpak_game_gets_a_sandbox_note(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / FLATPAK_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix))
    wine = fake_wine(tmp_path)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is True
    assert "Flatpak" in result.message and "net.lutris.Lutris" in result.message
    assert "--link-mode hardlink" in result.message


def test_check_links_unique_names_each_run(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)
    first = check_links(game, wine=str(wine), home=home)
    second = check_links(game, wine=str(wine), home=home)
    assert first.ok and second.ok
    assert first.windows_path != second.windows_path
    assert leftovers(game) == []


# ---------------------------------------------------------------- check_links: failure


@pytest.mark.parametrize("mode", ["silent", "fail", "nosymlink"])
def test_check_links_wine_failure(home, tmp_path, mode):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, mode)
    before = snapshot(game.prefix)

    result = check_links(game, wine=str(wine), home=home)

    assert result.ok is False
    assert "bethmm override fallout4 --link-mode hardlink" in result.message
    assert "Flatpak" in result.message
    assert len(calls(wine)) == 1
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before
    if mode == "fail":
        assert "exited with code 1" in result.message
        assert "kernel32" in result.output


def test_check_links_hardlink_failure_does_not_suggest_hardlinks(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "silent")
    result = check_links(game, link_mode="hardlink", wine=str(wine), home=home)
    assert result.ok is False
    assert "--link-mode hardlink" not in result.message
    assert leftovers(game) == []


def test_check_links_timeout(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "sleep")
    before = snapshot(game.prefix)

    result = check_links(game, wine=str(wine), timeout=1, home=home)

    assert result.ok is False
    assert "1 seconds" in result.message
    assert "--link-mode hardlink" in result.message
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before


def test_check_links_wine_missing(home, tmp_path):
    game = make_game(tmp_path)
    result = check_links(game, wine=str(tmp_path / "no-such-wine"), home=home)
    assert result.ok is None
    assert "Could not start Wine" in result.message
    assert leftovers(game) == []


@pytest.mark.parametrize("exc", [PermissionError(errno.EACCES, "Permission denied"),
                                 OSError(errno.ENOEXEC, "Exec format error")])
def test_check_links_wine_not_runnable(home, tmp_path, monkeypatch, fixed_token, exc):
    game = make_game(tmp_path)

    def run(cmd, **kwargs):
        assert os.path.islink(game.data_dir / "bethmm-linktest-abcd1234.txt")
        raise exc

    monkeypatch.setattr(winecheck.subprocess, "run", run)
    result = check_links(game, wine="/usr/bin/wine", home=home)
    assert result.ok is None
    assert exc.strerror in result.message
    assert leftovers(game) == []


def test_check_links_cleans_up_when_subprocess_raises(home, tmp_path, monkeypatch, fixed_token):
    game = make_game(tmp_path)
    before = snapshot(game.prefix)
    seen = {}

    def run(cmd, **kwargs):
        link = game.data_dir / "bethmm-linktest-abcd1234.txt"
        seen["link"] = os.path.islink(link)
        seen["kwargs"] = kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(winecheck.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        check_links(game, wine="/usr/bin/wine", timeout=7, home=home)
    assert seen["link"] is True
    assert seen["kwargs"]["timeout"] == 7
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["capture_output"] is True
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before


def test_check_links_leaves_a_replaced_link_alone(home, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bethmm.winecheck")
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "replace")

    result = check_links(game, link_mode="symlink", wine=str(wine), home=home)

    assert result.ok is False
    (name,) = [p.name for p in game.data_dir.iterdir() if p.name.startswith("bethmm-linktest")]
    assert (game.data_dir / name).read_text() == "the user's own file\n"
    assert not (paths.mods_dir("fallout4") / ".linktest").exists()
    assert any(r.getMessage().startswith("KEEP ") for r in caplog.records)


def test_check_links_leaves_a_replaced_hardlink_alone(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "replace")
    result = check_links(game, link_mode="hardlink", wine=str(wine), home=home)
    assert result.ok is False
    (name,) = [p.name for p in game.data_dir.iterdir() if p.name.startswith("bethmm-linktest")]
    assert (game.data_dir / name).read_text() == "the user's own file\n"


# ---------------------------------------------------------------- check_links: refusals


@pytest.mark.parametrize("existing", ["bethmm-linktest-abcd1234.txt", "BETHMM-LinkTest-ABCD1234.TXT"])
def test_check_links_refuses_existing_link_name(home, tmp_path, fixed_token, existing):
    game = make_game(tmp_path)
    (game.data_dir / existing).write_text("not ours")
    wine = fake_wine(tmp_path)
    before = snapshot(game.prefix)

    result = check_links(game, wine=str(wine), home=home)

    assert result.ok is None
    assert "already exists" in result.message
    assert calls(wine) == []
    assert (game.data_dir / existing).read_text() == "not ours"
    assert snapshot(game.prefix) == before
    assert not (paths.mods_dir("fallout4") / ".linktest").exists()


def test_check_links_link_appearing_at_the_last_moment_is_left_alone(home, tmp_path, monkeypatch, fixed_token):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)
    real_symlink = os.symlink

    def racing_symlink(src, dst, *args, **kwargs):
        Path(dst).write_text("someone else's")
        return real_symlink(src, dst, *args, **kwargs)

    monkeypatch.setattr(winecheck.os, "symlink", racing_symlink)
    result = check_links(game, wine=str(wine), home=home)

    assert result.ok is None and "already exists" in result.message
    assert (game.data_dir / "bethmm-linktest-abcd1234.txt").read_text() == "someone else's"
    assert calls(wine) == []
    assert not (paths.mods_dir("fallout4") / ".linktest").exists()


def test_check_links_refuses_missing_data_folder(home, tmp_path):
    game = make_game(tmp_path, data=False)
    wine = fake_wine(tmp_path)
    before = snapshot(game.prefix)

    result = check_links(game, wine=str(wine), home=home)

    assert result.ok is None
    assert str(game.data_dir) in result.message
    assert not game.data_dir.exists()
    assert calls(wine) == []
    assert snapshot(game.prefix) == before


def test_check_links_without_prefix(home, tmp_path):
    game = make_game(tmp_path, prefix=False)
    wine = fake_wine(tmp_path)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is None
    assert "prefix" in result.message and "--prefix" in result.message
    assert calls(wine) == []
    assert leftovers(game) == []


def test_check_links_refuses_prefix_without_drive_c(home, tmp_path):
    game = make_game(tmp_path, inside_prefix=False)
    game.prefix = tmp_path / "not-a-prefix"
    wine = fake_wine(tmp_path)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is None
    assert "drive_c" in result.message
    assert not game.prefix.exists()
    assert calls(wine) == []


def test_check_links_without_wine(home, tmp_path):
    game = make_game(tmp_path)
    result = check_links(game, home=home)
    assert result.ok is None
    assert result.wine is None
    assert "by hand" in result.message and "start the game" in result.message
    assert leftovers(game) == []


def test_check_links_hardlink_across_filesystems(home, tmp_path, monkeypatch):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)

    def cross_device_link(src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(winecheck.os, "link", cross_device_link)
    before = snapshot(game.prefix)
    result = check_links(game, link_mode="hardlink", wine=str(wine), home=home)
    assert result.ok is None
    assert "same filesystem" in result.message
    assert calls(wine) == []
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before


def test_check_links_symlink_not_supported(home, tmp_path, monkeypatch):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)

    def no_symlinks(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(winecheck.os, "symlink", no_symlinks)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is None
    assert "doesn't support symlinks" in result.message
    assert leftovers(game) == []


def test_check_links_rejects_unknown_link_mode(home, tmp_path):
    game = make_game(tmp_path)
    with pytest.raises(ValueError):
        check_links(game, link_mode="copy", wine="/bin/true", home=home)
    assert leftovers(game) == []


def test_check_links_reports_failed_cleanup(home, tmp_path, monkeypatch, fixed_token):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path)
    link = game.data_dir / "bethmm-linktest-abcd1234.txt"
    real_unlink = os.unlink

    def stubborn_unlink(path, *args, **kwargs):
        if Path(path) == link:
            raise OSError(errno.EIO, "Input/output error")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(winecheck.os, "unlink", stubborn_unlink)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is True
    assert "Could not remove the test link" in result.message and str(link) in result.message
    assert os.path.islink(link)
    assert not (paths.mods_dir("fallout4") / ".linktest" / link.name).exists()


# ---------------------------------------------------------------- review: interruptions and leftovers

KILLABLE_CHECK = r'''
import json, sys
from pathlib import Path
from bethmm import winecheck
from bethmm.discovery import DiscoveredGame
game = DiscoveredGame.from_dict(json.loads(sys.argv[1]))
winecheck.check_links(game, link_mode=sys.argv[2], wine=sys.argv[3], home=Path(sys.argv[4]), timeout=60)
'''


def start_check(game: DiscoveredGame, link_mode: str, wine: Path, home: Path) -> subprocess.Popen:
    """Run check_links in another process and return once Wine is running
    (the test link exists)."""
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo)}
    proc = subprocess.Popen([sys.executable, "-c", KILLABLE_CHECK, json.dumps(game.to_dict()), link_mode,
                             str(wine), str(home)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not calls(wine):
            assert proc.poll() is None, "the check ended before Wine ran"
            assert time.monotonic() < deadline, "Wine never ran"
            time.sleep(0.05)
    except BaseException:
        kill_check(proc)
        raise
    return proc


def kill_check(proc: subprocess.Popen) -> None:
    """SIGKILL the check and its Wine: no finally block runs."""
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def test_killed_symlink_check_leaves_only_an_orphan_that_verify_repairs(home, tmp_path):
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "sleep")
    before = snapshot(game.prefix)
    kill_check(start_check(game, "symlink", wine, home))
    (name,) = [n for n in leftovers(game) if n.startswith("bethmm-linktest-")]
    assert os.path.islink(game.data_dir / name)

    target = deploy.GameTarget("fallout4", game.root)
    with Manifest(tmp_path / "manifest.db") as manifest:
        report = deploy.verify(manifest, target)
        assert report.orphan_links == [f"Data/{name}"]
        deploy.repair(manifest, target, report)
    assert not os.path.lexists(game.data_dir / name)
    assert snapshot(game.prefix) == before


@pytest.mark.parametrize("link_mode", ["symlink", "hardlink"])
def test_next_check_removes_what_a_killed_check_left(home, tmp_path, link_mode):
    """A stray hardlink is invisible to deploy.verify (it is an ordinary file),
    so the check itself has to clean up after a killed run."""
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "sleep")
    before = snapshot(game.prefix)
    kill_check(start_check(game, link_mode, wine, home))
    (name,) = [n for n in leftovers(game) if n.startswith("bethmm-linktest-")]
    if link_mode == "hardlink":
        with Manifest(tmp_path / "manifest.db") as manifest:
            assert deploy.verify(manifest, deploy.GameTarget("fallout4", game.root, "hardlink")).ok

    result = check_links(game, link_mode=link_mode, wine=str(fake_wine(tmp_path)), home=home)

    assert result.ok is True, result.message
    assert leftovers(game) == []
    assert snapshot(game.prefix) == before


def test_leftover_cleanup_never_touches_a_running_check(home, tmp_path):
    game = make_game(tmp_path)
    sleeper = fake_wine(tmp_path, "sleep")
    running = start_check(game, "hardlink", sleeper, home)
    try:
        (name,) = [n for n in leftovers(game) if n.startswith("bethmm-linktest-")]
        result = check_links(game, link_mode="hardlink", wine=str(fake_wine(tmp_path)), home=home)
        assert result.ok is True, result.message
        assert (game.data_dir / name).stat().st_nlink == 2
        assert winecheck.remove_leftovers("fallout4", game.data_dir) == []
        assert (game.data_dir / name).stat().st_nlink == 2
    finally:
        kill_check(running)
    assert winecheck.remove_leftovers("fallout4", game.data_dir)
    assert leftovers(game) == []


def test_leftover_cleanup_keeps_files_it_cannot_prove_are_its_own(home, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bethmm.winecheck")
    game = make_game(tmp_path)
    test_dir = paths.mods_dir("fallout4") / ".linktest"
    # Same name as a stale test file, but a different file (not our inode / not our target).
    make_tree(test_dir, {"bethmm-linktest-0badf00d.txt": "stale", "bethmm-linktest-feedface.txt": "stale",
                         "notes.txt": "not a test file"})
    (game.data_dir / "bethmm-linktest-0badf00d.txt").write_text("someone else's")
    (game.data_dir / "bethmm-linktest-feedface.txt").symlink_to(game.data_dir / "Fallout4.esm")
    os.link(test_dir / "notes.txt", game.data_dir / "notes.txt")
    before = snapshot(game.data_dir)

    winecheck.remove_leftovers("fallout4", game.data_dir)

    assert snapshot(game.data_dir) == before
    assert sorted(os.listdir(test_dir)) == ["notes.txt"]
    assert sum(r.getMessage().startswith("KEEP ") for r in caplog.records) == 2


def test_leftover_cleanup_without_data_folder_does_nothing(home, tmp_path):
    game = make_game(tmp_path, data=False)
    make_tree(paths.mods_dir("fallout4") / ".linktest", {"bethmm-linktest-0badf00d.txt": "stale"})
    assert winecheck.remove_leftovers("fallout4", game.data_dir) == []
    assert (paths.mods_dir("fallout4") / ".linktest" / "bethmm-linktest-0badf00d.txt").exists()


def test_unremovable_hardlink_keeps_its_proof(home, tmp_path, monkeypatch, fixed_token):
    """If the test hardlink can't be removed, the test file stays so a later
    run can still prove the link is the app's and remove it."""
    game = make_game(tmp_path)
    link = game.data_dir / "bethmm-linktest-abcd1234.txt"
    real_unlink = os.unlink
    with monkeypatch.context() as m:
        m.setattr(winecheck.os, "unlink",
                  lambda p, *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "Input/output error"))
                  if Path(p) == link else real_unlink(p, *a, **k))
        result = check_links(game, link_mode="hardlink", wine=str(fake_wine(tmp_path)), home=home)
    assert result.ok is True and "Could not remove the test link" in result.message
    assert link.stat().st_nlink == 2

    winecheck.remove_leftovers("fallout4", game.data_dir)
    assert leftovers(game) == []


@pytest.mark.parametrize("link_mode", ["symlink", "hardlink"])
def test_interrupt_right_after_linking_still_cleans_up(home, tmp_path, monkeypatch, link_mode):
    game = make_game(tmp_path)
    func = "symlink" if link_mode == "symlink" else "link"
    real = getattr(os, func)

    def interrupted(src, dst, *args, **kwargs):
        real(src, dst, *args, **kwargs)
        raise KeyboardInterrupt  # Ctrl-C arriving as the call returns

    monkeypatch.setattr(winecheck.os, func, interrupted)
    with pytest.raises(KeyboardInterrupt):
        check_links(game, link_mode=link_mode, wine=str(fake_wine(tmp_path)), home=home)
    assert leftovers(game) == []


def test_output_held_open_after_wine_read_the_file_is_a_success(home, tmp_path):
    """A program started with the Wine session (a Run key such as a launcher,
    or a persistent wineserver) inherits the output pipe: the timeout hits
    although Wine already printed the file through the link."""
    game = make_game(tmp_path)
    wine = fake_wine(tmp_path, "linger")
    try:
        result = check_links(game, wine=str(wine), timeout=2, home=home)
    finally:
        pid_file = Path(f"{wine}.calls.json.pid")
        if pid_file.exists():
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
    assert result.ok is True, result.message
    assert "--link-mode hardlink" not in result.message
    assert leftovers(game) == []


# ---------------------------------------------------------------- review: real-world runners and paths


@pytest.mark.parametrize("where", [
    ".local/share/Steam/compatibilitytools.d",
    ".steam/root/compatibilitytools.d",
    ".steam/steam/compatibilitytools.d",
    f"{FLATPAK}/data/Steam/compatibilitytools.d",
])
def test_find_wine_proton_in_steam_compatibility_tools(home, tmp_path, where):
    """ProtonUp-Qt and umu put GE-Proton here, and Lutris looks here for a
    Proton ``wine.version``; a different Wine is only a last resort."""
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, "GE-Proton9-20"))
    runner(home, NATIVE_DATA, "lutris-9.0-x86_64")
    wine = make_executable(home / where / "GE-Proton9-20" / "files" / "bin" / "wine")
    assert find_wine(game, home=home) == str(wine)


def test_find_wine_valve_proton_in_steam_library(home, tmp_path):
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, "Proton 5.0"))
    runner(home, NATIVE_DATA, "lutris-9.0-x86_64")
    wine = make_executable(home / ".local/share/Steam/steamapps/common/Proton 5.0/dist/bin/wine")
    assert find_wine(game, home=home) == str(wine)


@pytest.mark.parametrize("version", ["winehq-staging", "winehq-devel", "wine-development"])
def test_find_wine_lutris_named_system_wines(home, tmp_path, monkeypatch, version):
    """Lutris maps these versions to fixed paths (/opt/wine-staging/bin/wine ...)."""
    game = make_game(tmp_path)
    write_yml(home / NATIVE_DATA / "games" / "fallout-4-1.yml", lutris_yml(game.prefix, version))
    runner(home, NATIVE_DATA, "lutris-9.0-x86_64")
    fixed = make_executable(tmp_path / "opt" / version / "bin" / "wine")
    assert version in winecheck._SYSTEM_WINES
    monkeypatch.setitem(winecheck._SYSTEM_WINES, version, str(fixed))
    assert find_wine(game, home=home) == str(fixed)


def test_check_links_path_with_spaces_and_unicode(home, tmp_path, fixed_token):
    game = make_game(tmp_path)
    data = game.prefix / "drive_c" / "GOG Games" / "Fallout 4 – GOTY Édition (日本語)" / "Data"
    data.mkdir(parents=True)
    game.root, game.data_dir = data.parent, data
    wine = fake_wine(tmp_path)
    result = check_links(game, wine=str(wine), home=home)
    assert result.ok is True, result.message
    assert result.windows_path == ("C:\\GOG Games\\Fallout 4 – GOTY Édition (日本語)\\Data\\"
                                   "bethmm-linktest-abcd1234.txt")
    assert calls(wine)[0]["args"][3] == result.windows_path
    assert leftovers(game) == []


def test_leftover_cleanup_removes_only_stale_partial_files(home, tmp_path):
    game = make_game(tmp_path)
    test_dir = paths.mods_dir("fallout4") / ".linktest"
    make_tree(test_dir, {"bethmm-linktest-0badf00d.txt.tmp": "bethmm link", "bethmm-linktest-feedface.txt.tmp": ""})
    old = test_dir / "bethmm-linktest-0badf00d.txt.tmp"
    os.utime(old, (1_600_000_000, 1_600_000_000))
    # A Data file with the partial file's base name is never touched.
    (game.data_dir / "bethmm-linktest-0badf00d.txt").write_text("someone else's")

    winecheck.remove_leftovers("fallout4", game.data_dir)

    assert sorted(os.listdir(test_dir)) == ["bethmm-linktest-feedface.txt.tmp"]  # fresh: a check may own it
    assert (game.data_dir / "bethmm-linktest-0badf00d.txt").read_text() == "someone else's"
