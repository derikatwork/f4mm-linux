"""Check that Wine follows the app's links for a game ("bethmm check-links").

Wine reads symlinked files like any other file, but a Flatpak sandbox or an
unusual runner could still get in the way, so this checks it for real:

1. write a small file with random content into
   ``paths.mods_dir(game_id)/.linktest/`` (inside the mods folder on purpose:
   if the app is killed mid-check, ``deploy.verify`` finds the stray link as
   an orphan pointing into the mods folder and ``repair`` removes it);
2. link it into the game's Data folder as ``bethmm-linktest-<random>.txt``
   (symlink, or hardlink when *link_mode* is "hardlink");
3. run ``<wine> cmd /c type <windows path of the link>`` with
   ``WINEPREFIX=<prefix>``, ``WINEDEBUG=-all`` and a timeout, and check that the
   output contains the random content;
4. always (``finally``) remove the link and the test file, and log every
   file operation.

The test file is created under a temporary name and locked (``flock``) until
the cleanup, so a running check is recognisable. A check killed before its
cleanup leaves its test file behind, and possibly the link: a symlink
``deploy.verify`` reports as an orphan, but a hardlink is an ordinary file to
it. :func:`remove_leftovers` (run at the start of every check) removes such a
link only while it is still exactly the link to a leftover test file (same
symlink target, or same inode), then the test file; when a hardlink can't be
removed the test file is kept, as the proof that the link is the app's.

Finding Wine (first hit wins): the *wine* argument; the runner Lutris uses for
this game -- read ``wine.version`` from the game's Lutris yml (matched by
``game.prefix`` == the game's prefix, or by slug) and look for
``<lutris data>/runners/wine/<version>/bin/wine``, and for Proton-style
runners ``<lutris data>/runners/proton/<version>/files/bin/wine`` (lutris data =
``~/.local/share/lutris`` or the Flatpak
``~/.var/app/net.lutris.Lutris/data/lutris``), then, as Lutris does, Steam's
``compatibilitytools.d`` folders and ``steamapps/common`` (``files/bin/wine``
or ``dist/bin/wine``); Lutris's fixed paths for "winehq-staging",
"winehq-devel" and "wine-development"; any ``runners/wine/*/bin/wine``
(newest by name); ``wine`` on PATH. When none is found the result has
``ok=None`` and a message explaining how to test by hand (install a small mod,
start the game, see if it shows up).

Windows paths: a path inside ``<prefix>/drive_c`` becomes ``C:\\...``; any other
absolute path becomes ``Z:\\...`` (Wine maps Z: to /).

Never touches anything else in the game folder, and refuses to run if the
game's Data folder doesn't exist.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from bethmm import paths
from bethmm.discovery import DiscoveredGame, LutrisPaths, _expand_home, _slug_from_stem

log = logging.getLogger(__name__)

LINK_MODES = ("symlink", "hardlink")
LINK_PREFIX = "bethmm-linktest-"
TEST_DIR = ".linktest"
FLATPAK_ID = "net.lutris.Lutris"
#: Where a runner's wine binary sits below ``runners/``, for a given version.
_RUNNER_LAYOUTS = (
    ("wine", "bin/wine"),
    ("wine", "files/bin/wine"),
    ("proton", "files/bin/wine"),
    ("proton", "bin/wine"),
)
#: Fixed paths Lutris uses for these ``wine.version`` values.
_SYSTEM_WINES = {
    "winehq-devel": "/opt/wine-devel/bin/wine",
    "winehq-staging": "/opt/wine-staging/bin/wine",
    "wine-development": "/usr/lib/wine-development/wine",
}
#: Where a Proton build's wine binary sits (older Proton: ``dist``).
_PROTON_LAYOUTS = ("files/bin/wine", "dist/bin/wine")
_MAX_OUTPUT = 4000
_DIGITS = re.compile(r"(\d+)")
#: A test file (or one still being written, ``.tmp``) in ``.linktest``.
_TEST_NAME = re.compile(re.escape(LINK_PREFIX) + r"[0-9a-f]+\.txt(\.tmp)?")
_STALE_SECONDS = 60


@dataclass
class LinkCheck:
    #: True = Wine read the file through the link; False = it could not;
    #: None = the check could not run (no Wine, no prefix ...).
    ok: bool | None
    message: str
    wine: str | None = None
    windows_path: str | None = None
    #: Combined stdout/stderr of the Wine command (trimmed), for troubleshooting.
    output: str = ""


# ---------------------------------------------------------------- paths


def windows_path(path: str | Path, prefix: str | Path | None) -> str:
    """How Wine running in *prefix* names *path* (the last component is never
    resolved, so a link stays a link)."""
    path = os.path.normpath(os.path.abspath(os.fspath(path)))
    if prefix is not None:
        drive_c = os.path.normpath(os.path.abspath(os.path.join(os.fspath(prefix), "drive_c")))
        rel = _relative_to(path, drive_c)
        if rel is None:
            # The prefix may be spelled through a symlink (or the other way round).
            parent, name = os.path.split(path)
            real_parent = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(path)
            rel = _relative_to(real_parent, os.path.realpath(drive_c))
        if rel is not None:
            return "C:\\" + rel.replace("/", "\\")
    return "Z:" + path.replace("/", "\\") if path != "/" else "Z:\\"


def _relative_to(path: str, base: str) -> str | None:
    """*path* relative to *base* ("" for base itself), or None if outside."""
    if path == base:
        return ""
    base = base.rstrip("/")
    if path.startswith(base + "/"):
        return path[len(base) + 1:]
    return None


def _under(path: str | Path, base: Path) -> bool:
    return _relative_to(os.path.abspath(os.fspath(path)), os.path.abspath(base)) is not None


# ---------------------------------------------------------------- finding Wine


def _lutris_data_dirs(home: Path) -> list[Path]:
    return [home / ".local" / "share" / "lutris", _flatpak_root(home) / "data" / "lutris"]


def _flatpak_root(home: Path) -> Path:
    return home / ".var" / "app" / FLATPAK_ID


def _proton_dirs(home: Path) -> list[Path]:
    """Other folders Lutris searches for a Proton version: Steam's compatibility
    tools (where ProtonUp-Qt and umu put GE-Proton) and Steam's own Protons;
    the Flatpak Lutris one last."""
    steam = home / ".local" / "share" / "Steam"
    return [
        steam / "compatibilitytools.d",
        home / ".steam" / "root" / "compatibilitytools.d",
        home / ".steam" / "steam" / "compatibilitytools.d",
        steam / "steamapps" / "common",
        _flatpak_root(home) / "data" / "Steam" / "compatibilitytools.d",
    ]


def _is_executable(path: str | Path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _read_yml(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.debug("could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _text(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _wine_section(data: dict | None) -> dict:
    section = data.get("wine") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def _find_game_yml(game: DiscoveredGame, home: Path) -> tuple[Path, dict] | None:
    """The game's Lutris yml: the first matching its prefix, else the first
    matching its slug."""
    want_prefix = os.path.realpath(game.prefix) if game.prefix is not None else None
    by_slug: tuple[Path, dict] | None = None
    for config_dir in LutrisPaths.default(home).config_dirs:
        try:
            files = sorted(p for p in config_dir.iterdir() if p.suffix == ".yml" and os.path.isfile(p))
        except OSError:
            continue
        for path in files:
            data = _read_yml(path)
            if data is None:
                continue
            section = data.get("game")
            prefix = _expand_home(section.get("prefix"), home) if isinstance(section, dict) else None
            if want_prefix is not None and prefix is not None and prefix.is_absolute() \
                    and os.path.realpath(prefix) == want_prefix:
                return path, data
            if by_slug is None and game.lutris_slug and game.lutris_slug in (
                    _slug_from_stem(path.stem), data.get("game_slug")):
                by_slug = (path, data)
    return by_slug


def _runner_wine(yml_path: Path, data: dict, home: Path) -> str | None:
    """The wine binary of the runner a Lutris game yml asks for, if installed."""
    wine = _wine_section(data)
    if not _text(wine.get("version")):
        # No per-game choice: Lutris uses the runner's default (runners/wine.yml).
        wine = _wine_section(_read_yml(yml_path.parent.parent / "runners" / "wine.yml"))
    version = _text(wine.get("version"))
    if version is None:
        return None
    if version == "system":
        return shutil.which("wine")
    if version in _SYSTEM_WINES:
        return _SYSTEM_WINES[version] if _is_executable(_SYSTEM_WINES[version]) else None
    if version == "custom":
        custom = _expand_home(wine.get("custom_wine_path"), home)
        return str(custom) if custom is not None and custom.is_absolute() and _is_executable(custom) else None
    if "/" in version or "\0" in version or version in (".", ".."):
        return None
    data_dirs = _lutris_data_dirs(home)
    proton_dirs = _proton_dirs(home)
    if _under(yml_path, _flatpak_root(home)):
        data_dirs.reverse()
        proton_dirs.insert(0, proton_dirs.pop())
    candidates = [data_dir / "runners" / kind / version / rel
                  for data_dir in data_dirs for kind, rel in _RUNNER_LAYOUTS]
    candidates += [proton_dir / version / rel for proton_dir in proton_dirs for rel in _PROTON_LAYOUTS]
    return next((str(c) for c in candidates if _is_executable(c)), None)


def _natural_key(name: str) -> list:
    return [int(part) if i % 2 else part.lower() for i, part in enumerate(_DIGITS.split(name))]


def _newest_runner(home: Path) -> str | None:
    found: list[tuple[list, int, Path]] = []
    for order, data_dir in enumerate(_lutris_data_dirs(home)):
        try:
            with os.scandir(data_dir / "runners" / "wine") as it:
                names = [e.name for e in it]
        except OSError:
            continue
        for name in names:
            candidate = data_dir / "runners" / "wine" / name / "bin" / "wine"
            if _is_executable(candidate):
                found.append((_natural_key(name), -order, candidate))
    return str(max(found)[2]) if found else None


def find_wine(game: DiscoveredGame, *, home: Path | None = None) -> str | None:
    """*home* defaults to ``Path.home()`` (tests point it at a fake home)."""
    home = home or Path.home()
    return _locate_wine(_find_game_yml(game, home), home)


def _locate_wine(hit: tuple[Path, dict] | None, home: Path) -> str | None:
    """:func:`find_wine` after the game's yml (*hit*) was looked up."""
    wine = _runner_wine(*hit, home) if hit is not None else None
    return wine or _newest_runner(home) or shutil.which("wine")


# ---------------------------------------------------------------- the check


def _random_token(nbytes: int) -> str:
    return secrets.token_hex(nbytes)


def _trim(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_OUTPUT:
        return text
    half = _MAX_OUTPUT // 2
    return text[:half] + "\n[...]\n" + text[-half:]


def _name_taken(folder: Path, name: str) -> bool:
    """Whether *name* exists in *folder*, compared case-insensitively (as Wine would)."""
    if os.path.lexists(folder / name):
        return True
    folded = name.casefold()
    return any(entry.casefold() == folded for entry in os.listdir(folder))


def check_links(
    game: DiscoveredGame,
    *,
    link_mode: str = "symlink",
    wine: str | None = None,
    timeout: float = 120,
    home: Path | None = None,
) -> LinkCheck:
    if link_mode not in LINK_MODES:
        raise ValueError(f"link mode must be one of {LINK_MODES}, not {link_mode!r}")
    home = home or Path.home()
    game_id = game.game_id

    if game.prefix is None:
        return LinkCheck(None, f"No Wine prefix is known for {game.name}, so the check can't run. Set it with "
                               f"'bethmm override {game_id} --prefix PATH' (the folder that contains drive_c).")
    prefix = Path(game.prefix)
    if not (prefix / "drive_c").is_dir():
        # Wine would create a brand new prefix there.
        return LinkCheck(None, f"The Wine prefix {prefix} doesn't exist or has no drive_c folder, so the check "
                               f"can't run. Fix it with 'bethmm override {game_id} --prefix PATH'.")
    data_dir = Path(game.data_dir)
    if not data_dir.is_dir():
        return LinkCheck(None, f"The game's Data folder {data_dir} doesn't exist, so there is nothing to test "
                               f"(check the game folder with 'bethmm override {game_id} --root PATH').")

    remove_leftovers(game_id, data_dir)
    hit = _find_game_yml(game, home)
    if wine is None:
        wine = _locate_wine(hit, home)
    if wine is None:
        return LinkCheck(None, "Couldn't find Wine (no Lutris runner for this game, none under "
                               "~/.local/share/lutris/runners and no 'wine' on PATH), so the check can't run. "
                               "To test by hand: install and deploy a small mod (one that adds a single "
                               "plugin or texture), start the game from Lutris and see if the mod shows up.")
    flatpak = (hit is not None and _under(hit[0], _flatpak_root(home))) or _under(wine, _flatpak_root(home))

    name = f"{LINK_PREFIX}{_random_token(4)}.txt"
    link = data_dir / name
    win = windows_path(link, prefix)
    try:
        if _name_taken(data_dir, name):
            return LinkCheck(None, f"{link} already exists; it was left alone and the check did not run. "
                                   "Run the check again.", wine, win)
    except OSError as exc:
        return LinkCheck(None, f"Can't read the Data folder {data_dir}: {exc.strerror or exc}", wine, win)

    test_dir = Path(os.path.abspath(paths.mods_dir(game_id) / TEST_DIR))
    test_file = test_dir / name
    token = _random_token(16)
    state: dict = {}
    try:
        result = _run_check(game, link_mode, wine, timeout, prefix, data_dir, link, win,
                            test_dir, test_file, token, flatpak, state)
    finally:
        problems = _cleanup(link, test_file, test_dir, link_mode, state)
    if problems:
        result.message += " " + " ".join(problems)
    return result


def _run_check(
    game: DiscoveredGame, link_mode: str, wine: str, timeout: float, prefix: Path, data_dir: Path,
    link: Path, win: str, test_dir: Path, test_file: Path, token: str, flatpak: bool, state: dict,
) -> LinkCheck:
    """Steps 1-3; *state* records what was created, for the cleanup."""
    try:
        if not test_dir.is_dir():
            test_dir.mkdir(parents=True, exist_ok=True)
            log.info("MKDIR %s", test_dir)
        # Locked under a temporary name first, so remove_leftovers (in another
        # process) never sees this file unlocked; the lock lasts until the cleanup.
        partial = test_file.with_name(test_file.name + ".tmp")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        state["file"], state["fd"] = partial, fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            log.debug("could not lock %s: %s", partial, exc)
        os.write(fd, f"bethmm link test {token}\r\n".encode("ascii"))
        os.rename(partial, test_file)
        state["file"] = test_file
        log.info("WRITE %s", test_file)
        st = os.fstat(fd)
        state["id"] = (st.st_ino, st.st_dev)
    except OSError as exc:
        return LinkCheck(None, f"Can't write the test file {test_file}: {exc.strerror or exc}", wine, win)

    state["link"] = "pending"  # an interrupt as the call returns must still remove the link
    try:
        if link_mode == "symlink":
            os.symlink(str(test_file), link)
        else:
            os.link(test_file, link)
    except FileExistsError:
        state["link"] = False
        return LinkCheck(None, f"{link} already exists; it was left alone and the check did not run. "
                               "Run the check again.", wine, win)
    except OSError as exc:
        state["link"] = False
        return LinkCheck(None, _link_error(link_mode, link, data_dir, exc), wine, win)
    state["link"] = True
    log.info("LINK %s -> %s (%s)", link, test_file, link_mode)

    cmd = [wine, "cmd", "/c", "type", win]
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all"}
    log.info("RUN %s (WINEPREFIX=%s)", shlex.join(cmd), prefix)
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        out, err = _decode(exc.stdout), _decode(exc.stderr)
        output = _trim(out + "\n" + err)
        if token in out or token in err:
            # Wine printed the file, but a program it started (one run at session
            # start, a persistent wineserver) kept the output open.
            log.info("Wine read the test file; its output stayed open past %g seconds", timeout)
            return LinkCheck(True, _success_message(game, link_mode, data_dir, flatpak), wine, win, output)
        log.warning("Wine did not finish within %g seconds", timeout)
        return LinkCheck(False, f"Wine did not finish within {timeout:g} seconds. The first start in a prefix "
                                "can be slow (Wine may be updating it), so try again. "
                                + _failure_advice(game.game_id, link_mode), wine, win, output)
    except OSError as exc:
        log.warning("could not start %s: %s", wine, exc)
        return LinkCheck(None, f"Could not start Wine ({wine}): {exc.strerror or exc}. Pick another one with "
                               "--wine PATH.", wine, win)

    output = _trim(_decode(proc.stdout) + "\n" + _decode(proc.stderr))
    log.info("Wine exited with code %s", proc.returncode)
    if token in _decode(proc.stdout) or token in _decode(proc.stderr):
        return LinkCheck(True, _success_message(game, link_mode, data_dir, flatpak), wine, win, output)
    exit_note = f" (Wine exited with code {proc.returncode})" if proc.returncode else ""
    return LinkCheck(False, f"Wine could not read the test file through the {link_mode} in {data_dir}"
                            f"{exit_note}. " + _failure_advice(game.game_id, link_mode), wine, win, output)


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def _link_error(link_mode: str, link: Path, data_dir: Path, exc: OSError) -> str:
    reason = exc.strerror or str(exc)
    if link_mode == "hardlink" and exc.errno == errno.EXDEV:
        return (f"Can't create a hardlink in {data_dir}: hardlinks only work when the app's mods folder "
                f"({paths.mods_dir()}) and the game are on the same filesystem, and they aren't. "
                "Keep symlink mode for this game.")
    if link_mode == "symlink" and exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
        return (f"Can't create a symlink in {data_dir} ({reason}): the drive probably doesn't support "
                "symlinks (FAT/exFAT or some network shares). Hardlinks won't work there either; "
                "move the game to a Linux filesystem.")
    return f"Can't create the test {link_mode} {link}: {reason}"


def _success_message(game: DiscoveredGame, link_mode: str, data_dir: Path, flatpak: bool) -> str:
    if link_mode == "hardlink":
        return (f"Wine read the test file through a hardlink in {data_dir}: {game.name} will see mods "
                "deployed in hardlink mode.")
    message = (f"Wine read the test file through a symlink in {data_dir}: {game.name} will see mods "
               "deployed with symlinks.")
    if flatpak:
        message += (f" Note: this game seems to run in the Flatpak version of Lutris, and this check ran Wine "
                    f"outside its sandbox. If mods don't show up in the game, let the sandbox read the mods "
                    f"folder ('flatpak override --user --filesystem={paths.mods_dir()} {FLATPAK_ID}') or "
                    f"switch to hardlinks ('bethmm override {game.game_id} --link-mode hardlink').")
    return message


def _failure_advice(game_id: str, link_mode: str) -> str:
    if link_mode == "hardlink":
        return ("A hardlink is an ordinary file to Wine, so the problem is Wine or the prefix rather than the "
                "links: check the output below and that the game starts from Lutris.")
    return ("The game may not see mods deployed with symlinks. A likely cause is a sandboxed Wine (for "
            f"example Lutris installed as a Flatpak) that can't reach the app's mods folder ({paths.mods_dir()}) "
            "the links point to. If Wine itself failed (see the output below), fix that first; otherwise "
            f"switch this game to hardlinks: 'bethmm override {game_id} --link-mode hardlink' (the mods "
            "folder and the game must be on the same filesystem).")


def _cleanup(link: Path, test_file: Path, test_dir: Path, link_mode: str, state: dict) -> list[str]:
    """Step 4: remove only what this check created. Returns problems to report."""
    problems: list[str] = []
    keep_file = False
    if state.get("link"):
        try:
            st = os.lstat(link)
            if link_mode == "symlink":
                ours = stat.S_ISLNK(st.st_mode) and os.readlink(link) == str(test_file)
            else:
                ours = stat.S_ISREG(st.st_mode) and (st.st_ino, st.st_dev) == state.get("id")
            if ours:
                os.unlink(link)
                log.info("UNLINK %s", link)
            else:
                log.warning("KEEP %s (replaced by something the app didn't create)", link)
        except FileNotFoundError:
            if state["link"] is True:
                log.warning("the test link %s was already gone", link)
        except OSError as exc:
            log.error("could not remove the test link %s: %s", link, exc)
            problems.append(f"Could not remove the test link {link} ({exc.strerror or exc}); please delete it.")
            # The test file is the only proof that a hardlink is the app's.
            keep_file = link_mode == "hardlink"
    written = state.get("file")
    if written and keep_file:
        log.warning("KEEP %s (proves that %s is the app's, for the next check to remove both)", written, link)
    elif written:
        try:
            os.unlink(written)
            log.info("DELETE %s", written)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("could not delete the test file %s: %s", written, exc)
            problems.append(f"Could not delete the test file {written} ({exc.strerror or exc}).")
    fd = state.pop("fd", None)
    if fd is not None:
        os.close(fd)  # releases the lock
    try:
        test_dir.rmdir()
        log.info("RMDIR %s", test_dir)
    except OSError:
        pass  # missing, or another check is using it
    return problems


def remove_leftovers(game_id: str, data_dir: str | Path) -> list[str]:
    """Clean up after checks of *game_id* that were killed before their cleanup
    ran (see the module docstring); files of a check still running (locked)
    are left alone, and so is everything when *data_dir* doesn't exist (the
    test files are the proof). Returns what was done."""
    data_dir = Path(data_dir)
    test_dir = Path(os.path.abspath(paths.mods_dir(game_id) / TEST_DIR))
    if not data_dir.is_dir():
        return []
    try:
        names = sorted(n for n in os.listdir(test_dir) if _TEST_NAME.fullmatch(n))
    except OSError:
        return []
    done: list[str] = []
    for name in names:
        test_file = test_dir / name
        try:
            fd = os.open(test_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                continue
            # A ".tmp" file was never linked (the link is made after the rename);
            # a fresh one may belong to a check that hasn't locked it yet.
            if name.endswith(".tmp") and time.time() - st.st_mtime < _STALE_SECONDS:
                continue
            link = data_dir / name
            had_link = not name.endswith(".tmp") and os.path.lexists(link)
            mode = "symlink" if os.path.islink(link) else "hardlink"
            state = {"link": had_link, "file": test_file, "id": (st.st_ino, st.st_dev)}
            log.info("cleaning up after an interrupted link check: %s", test_file)
            done.extend(_cleanup(link, test_file, test_dir, mode, state))
            if had_link and not os.path.lexists(link):
                done.append(f"removed the test link {link} left by an interrupted check")
            if not os.path.lexists(test_file):
                done.append(f"deleted the test file {test_file} left by an interrupted check")
        except OSError as exc:
            log.debug("leaving %s alone: %s", test_file, exc)  # locked by a running check, or unreadable
        finally:
            os.close(fd)
    return done
