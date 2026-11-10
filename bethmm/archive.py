"""List and extract mod archives (.zip/.7z/.rar) with the system 7z binary.

A RAR archive the 7z can't read -- Fedora's 7-Zip is built without RAR
support -- is read with ``bsdtar`` (libarchive) instead, when it is
installed, with the same checks before and after extracting.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bethmm import distro

log = logging.getLogger(__name__)

#: Binaries tried in order. ``7za`` can't open .rar files but handles the rest.
SEVENZIP_NAMES = ("7z", "7zz", "7za")

INSTALL_HINT = (
    "Install it with:  sudo apt install 7zip 7zip-rar\n"
    "(7zip-rar is in Ubuntu's multiverse repository and adds .rar support; on older\n"
    "Ubuntu-based releases use:  sudo apt install p7zip-full p7zip-rar)\n"
    "or set the path to your 7z binary in the settings."
)
FEDORA_INSTALL_HINT = (
    "Install it with:  sudo dnf install 7zip bsdtar\n"
    "(bsdtar unpacks .rar files, which Fedora's 7-Zip can't)\n"
    "or set the path to your 7z binary in the settings."
)
#: How a RAR archive starts (RAR 1.5 to 4, and RAR 5).
_RAR_MAGIC = b"Rar!\x1a\x07"


class ArchiveError(Exception):
    pass


class SevenZipMissing(ArchiveError):
    def __init__(self, detail: str = "") -> None:
        msg = "The 7z program was not found, so mod archives can't be opened."
        if detail:
            msg += f" ({detail})"
        super().__init__(f"{msg}\n{FEDORA_INSTALL_HINT if distro.family() == 'fedora' else INSTALL_HINT}")


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    is_dir: bool
    size: int


@dataclass
class ExtractResult:
    #: Extracted regular files, relative POSIX paths, sorted.
    files: list[str]
    warnings: list[str] = field(default_factory=list)


def find_sevenzip(explicit: str | None = None) -> str:
    """Path of a usable 7z binary; raises :class:`SevenZipMissing`."""
    if explicit:
        path = shutil.which(os.path.expanduser(explicit))
        if not path:
            raise SevenZipMissing(f"configured 7z path {explicit!r} is not an executable")
        return path
    for name in SEVENZIP_NAMES:
        path = shutil.which(name)
        if path:
            return path
    raise SevenZipMissing()


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
    try:
        return subprocess.run(args, capture_output=True, env=env, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError as exc:
        raise SevenZipMissing(str(exc)) from exc


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


#: 7z's summary counters ("Sub items Errors: 1"), which say nothing about the cause.
_COUNTER = re.compile(r"^(sub items errors|archives with errors|errors|warnings)\s*:\s*\d+$", re.IGNORECASE)


def _error_lines(output: str, archive: Path | None = None) -> list[str]:
    """The lines of 7z output that name what went wrong, without the counters.

    The archive's full path is shortened to its name; an "ERROR: <path> : <path>"
    line that says nothing else is dropped.
    """
    lines = [ln.strip() for ln in output.splitlines()]
    if archive is not None:
        full = str(archive)
        lines = [ln.replace(full, archive.name) if full in ln else ln for ln in lines]
    found: list[str] = []
    in_block = False
    for i, line in enumerate(lines):
        low = line.lower()
        if not line:
            in_block = False
            continue
        if _COUNTER.match(line):
            continue
        if low in ("errors:", "warnings:"):
            # A header followed by one cause per line ("Unexpected end of archive").
            in_block = low == "errors:"
            continue
        if in_block:
            found.append(line)
        elif low.startswith("system error"):
            # The cause ("errno=28 : No space left on device") is on the next line.
            following = next((ln for ln in lines[i + 1:] if ln), "")
            found.append(following or line)
        elif low.startswith("error:"):
            detail = line[len("error:"):].strip()
            if archive is None or {p.strip() for p in detail.split(" : ")} != {archive.name}:
                found.append(detail)
        elif "can not open" in low or "cannot open" in low or low.startswith("e_fail"):
            found.append(line)
    return list(dict.fromkeys(e for e in found if e))


def _failure(archive: Path, proc: subprocess.CompletedProcess, sevenzip: str, dest: Path | None = None) -> ArchiveError:
    output = _decode(proc.stdout) + "\n" + _decode(proc.stderr)
    errors = _error_lines(output, archive)
    detail = "; ".join(errors[:3]) or f"7z exited with code {proc.returncode}"
    text = " ".join(errors).lower()
    msg = f"7z could not read {archive.name}: {detail}"
    if "wrong password" in text or "encrypted archive" in text or "encrypted file" in text:
        msg = f"{archive.name} is password-protected; that isn't supported."
    elif (archive.suffix.lower() == ".rar" or _is_rar(archive)) and _no_rar_support(text, sevenzip):
        if distro.family() == "fedora":
            msg += ("\nFedora's 7-Zip can't unpack .rar files. Install bsdtar, which the app uses for them:  "
                    "sudo dnf install bsdtar")
        else:
            package = "p7zip-rar" if "p7zip" in output.lower() else "7zip-rar"
            msg += f"\nThis 7z can't unpack this .rar file. Install RAR support:  sudo apt install {package}"
    elif "no space left" in text or "errno=28" in text:
        where = dest.parent if dest is not None else "the data folder"
        msg = f"Not enough disk space to extract {archive.name} into {where}. Free some space and try again."
    elif "permission denied" in text or "errno=13" in text:
        msg += "\nCheck that you can write to " + (str(dest) if dest is not None else "the data folder") + "."
    elif _looks_damaged(text):
        msg += "\nThe file looks damaged or incomplete; download it again."
    log.error("%s", msg.replace("\n", " "))
    return ArchiveError(msg)


def _looks_damaged(text: str) -> bool:
    return any(w in text for w in ("crc failed", "data error", "unexpected end", "headers error",
                                   "is not archive", "there are data after the end"))


def _no_rar_support(text: str, sevenzip: str) -> bool:
    """Whether 7z's errors (*text*, lower case) say it can't read RAR at all,
    rather than that this archive is damaged."""
    return ("unsupported method" in text or Path(sevenzip).name == "7za"
            or (("can not open the file as archive" in text or "cannot open the file as archive" in text)
                and not _looks_damaged(text)))


def _is_rar(archive: Path) -> bool:
    try:
        with open(archive, "rb") as fh:
            return fh.read(len(_RAR_MAGIC)) == _RAR_MAGIC
    except OSError:
        return False


@dataclass(frozen=True)
class _Reader:
    """The program that reads an archive: 7z, or bsdtar for a RAR archive the
    7z can't read."""

    exe: str
    bsdtar: bool = False


def list_archive(archive: str | Path, sevenzip: str | None = None) -> list[ArchiveEntry]:
    return _list(Path(archive), sevenzip)[0]


def _list(archive: Path, sevenzip: str | None) -> tuple[list[ArchiveEntry], _Reader]:
    if not archive.is_file():
        raise ArchiveError(f"{archive} is not a file")
    exe = find_sevenzip(sevenzip)
    proc = _run([exe, "l", "-slt", "-p", "--", str(archive)])
    if proc.returncode == 0:
        return _parse_slt(_decode(proc.stdout)), _Reader(exe)
    text = " ".join(_error_lines(_decode(proc.stdout) + "\n" + _decode(proc.stderr), archive)).lower()
    bsdtar = shutil.which("bsdtar") if _is_rar(archive) and _no_rar_support(text, exe) else None
    if bsdtar is None:
        raise _failure(archive, proc, exe)
    log.info("%s can't read RAR archives; reading %s with %s", exe, archive.name, bsdtar)
    proc = _run([bsdtar, "-t", "-v", "-f", str(archive)])
    if proc.returncode != 0:
        raise _bsdtar_failure(archive, proc)
    return _parse_bsdtar(_decode(proc.stdout), archive), _Reader(bsdtar, bsdtar=True)


#: A line of ``bsdtar -tv`` (LC_ALL=C): mode, links, owner, group, size, date, name.
_BSDTAR_LINE = re.compile(r"([-a-zA-Z?][-rwxsStT]{9}[+@.]?) +\d+ +\S+ +\S+ +(\d+) +"
                          r"[A-Z][a-z]{2} +\d{1,2} +(?:\d{1,2}:\d{2}|\d{4}) (.*)")


def _parse_bsdtar(output: str, archive: Path) -> list[ArchiveEntry]:
    """Parse ``bsdtar -tv``. A line that doesn't read as expected refuses the
    archive: its paths must be checked before anything is extracted."""
    entries: list[ArchiveEntry] = []
    for line in output.splitlines():
        if not line:
            continue
        match = _BSDTAR_LINE.fullmatch(line)
        if match is None:
            raise ArchiveError(f"couldn't read the list of files in {archive.name} (bsdtar printed {line!r})")
        mode, size, name = match.group(1), int(match.group(2)), match.group(3)
        if mode[0] == "l":
            name = name.split(" -> ", 1)[0]
        elif mode[0] == "h":
            name = name.split(" link to ", 1)[0]
        is_dir = mode[0] == "d" or name.endswith("/")
        entries.append(ArchiveEntry(name.replace("\\", "/").rstrip("/"), is_dir, 0 if is_dir else size))
    return entries


def _bsdtar_extract(archive: Path, dest: Path, bsdtar: str) -> None:
    """Extract with bsdtar's safe defaults: it refuses absolute paths and
    ``..``, and never writes through a symlink."""
    log.info("EXTRACT %s -> %s (bsdtar)", archive.resolve(), dest)
    proc = _run([bsdtar, "-x", "-f", str(archive), "-C", str(dest), "--no-same-owner", "--no-same-permissions",
                 "--no-acls", "--no-xattrs", "--no-fflags"])
    if proc.returncode != 0:
        raise _bsdtar_failure(archive, proc, dest)


def _bsdtar_failure(archive: Path, proc: subprocess.CompletedProcess, dest: Path | None = None) -> ArchiveError:
    lines = [ln.strip() for ln in (_decode(proc.stderr) + "\n" + _decode(proc.stdout)).splitlines() if ln.strip()]
    lines = [ln.replace(str(archive), archive.name) for ln in lines]
    detail = "; ".join(dict.fromkeys(lines[:3])) or f"bsdtar exited with code {proc.returncode}"
    text = " ".join(lines).lower()
    msg = f"bsdtar could not read {archive.name}: {detail}"
    if "encrypt" in text or "passphrase" in text or "password" in text:
        msg = f"{archive.name} is password-protected; that isn't supported."
    elif "through symlink" in text:
        msg = f"{archive.name} has a link that leads outside the mod, so it was not installed: {detail}"
    elif "no space left" in text:
        where = dest.parent if dest is not None else "the data folder"
        msg = f"Not enough disk space to extract {archive.name} into {where}. Free some space and try again."
    elif "permission denied" in text:
        msg += "\nCheck that you can write to " + (str(dest) if dest is not None else "the data folder") + "."
    elif any(w in text for w in ("truncated", "damaged", "corrupt", "bad ", "crc", "premature end")):
        msg += "\nThe file looks damaged or incomplete; download it again."
    else:
        msg += ("\nIf the archive is fine, unpack it with another tool and install the folder instead "
                "(File > Install extracted folder..., or 'bethmm install FOLDER').")
    log.error("%s", msg.replace("\n", " "))
    return ArchiveError(msg)


def _parse_slt(output: str) -> list[ArchiveEntry]:
    """Parse ``7z l -slt``: entries follow a ``----------`` line, one
    ``Key = Value`` per line, separated by blank lines."""
    _, sep, body = output.partition("\n----------\n")
    if not sep:
        return []
    entries: list[ArchiveEntry] = []
    for block in body.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, eq, value = line.partition(" = ")
            if eq:
                props[key.strip()] = value
            elif line.endswith(" ="):
                props[line[:-2].strip()] = ""
        if "Path" not in props:
            continue
        attrs = props.get("Attributes", "")
        is_dir = props.get("Folder") == "+" or attrs.startswith("D")
        try:
            size = int(props.get("Size") or 0)
        except ValueError:
            size = 0
        entries.append(ArchiveEntry(props["Path"].replace("\\", "/"), is_dir, size))
    return entries


def _unsafe(path: str) -> bool:
    p = PurePosixPath(path)
    return p.is_absolute() or ".." in p.parts or (len(path) > 1 and path[1] == ":")


def extract_archive(archive: str | Path, dest: str | Path, sevenzip: str | None = None) -> ExtractResult:
    """Extract *archive* into *dest*, which must not exist or be empty.

    Archives containing absolute paths or ``..``, or that unpack to more
    than the free disk space, are refused before anything is extracted.
    Symlinks that 7z (or bsdtar) creates from the archive are removed (a mod
    has no business shipping links) and reported in ``warnings``.
    """
    archive = Path(archive)
    dest = Path(dest)
    entries, reader = _list(archive, sevenzip)
    bad = [e.path for e in entries if _unsafe(e.path)]
    if bad:
        raise ArchiveError(f"{archive.name} contains unsafe paths and was not extracted: {', '.join(bad[:5])}")
    if dest.exists() and any(dest.iterdir()):
        raise ArchiveError(f"extraction folder {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    _check_space(archive, entries, dest)

    warnings: list[str] = []
    if reader.bsdtar:
        _bsdtar_extract(archive, dest, reader.exe)
    else:
        _sevenzip_extract(archive, dest, reader.exe, warnings)
    _split_backslash_names(dest, warnings)

    files: list[str] = []
    real_dest = os.path.realpath(dest)
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            if os.path.islink(full):
                os.unlink(full)
                if name in dirnames:
                    dirnames.remove(name)
                warnings.append(f"removed symlink {rel} from the extracted archive")
                log.warning("removed symlink %s extracted from %s", rel, archive)
            elif name in filenames:
                if not os.path.realpath(full).startswith(real_dest + os.sep):
                    raise ArchiveError(f"{archive.name} extracted a file outside the target folder: {rel}")
                files.append(rel)
    files.sort()
    log.info("EXTRACTED %d files from %s", len(files), archive.name)
    for warning in warnings:
        log.warning("%s: %s", archive.name, warning)
    return ExtractResult(files, warnings)


def _sevenzip_extract(archive: Path, dest: Path, exe: str, warnings: list[str]) -> None:
    log.info("EXTRACT %s -> %s", archive.resolve(), dest)
    proc = _run([exe, "x", "-y", "-bd", "-p", f"-o{dest}", "--", str(archive)])
    if proc.returncode != 0:
        errors = _error_lines(_decode(proc.stdout) + "\n" + _decode(proc.stderr))
        # 7z refuses links pointing outside the archive and exits with 2; that
        # alone is not a reason to fail the whole mod.
        if errors and all("Dangerous link path was ignored" in e for e in errors):
            for e in errors:
                warnings.append(f"7z skipped a link that points outside the mod: {e.split(':', 1)[-1].strip()}")
        else:
            raise _failure(archive, proc, exe, dest)


def _check_space(archive: Path, entries: list[ArchiveEntry], dest: Path) -> None:
    """Refuse an archive that can't fit: 7z would fill the disk before failing
    (and every other program on it would run out of space meanwhile)."""
    needed = sum(e.size for e in entries if not e.is_dir)
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        return  # 7z reports it, if it comes to that
    if needed > free:
        raise ArchiveError(f"{archive.name} unpacks to {_gigabytes(needed)}, but only {_gigabytes(free)} is free "
                           f"in {dest.parent}. Free some space and try again.")


def _gigabytes(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 / 10 else f"{size / 1024 ** 2:.1f} MB"


def _split_backslash_names(dest: Path, warnings: list[str]) -> None:
    """Turn files named like ``Data\\x.esp`` into ``Data/x.esp``.

    Zips made on Windows sometimes store backslash separators, and 7z on Linux
    then creates one flat file with backslashes in its name. Folders are
    merged with existing ones case-insensitively.
    """
    pending: list[tuple[Path, str]] = []
    for dirpath, _dirnames, filenames in os.walk(dest):
        pending.extend((Path(dirpath), name) for name in filenames if "\\" in name)
    for folder, name in pending:
        source = folder / name
        if source.is_symlink():
            continue
        parts = [p for p in name.split("\\") if p not in ("", ".")]
        if not parts or ".." in parts:
            source.unlink()
            log.info("DELETE %s (unsafe path)", source)
            warnings.append(f"removed {name!r}: unsafe path")
            continue
        target = folder
        for part in parts[:-1]:
            target = _child_ci(target, part)
        target = target / parts[-1]
        if os.path.lexists(target):
            source.unlink()
            log.info("DELETE %s (%s already exists)", source, target)
            warnings.append(f"removed {name!r}: {target.relative_to(dest).as_posix()} already exists")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        log.info("RENAME %s -> %s", source, target)


def _child_ci(folder: Path, name: str) -> Path:
    """*folder*/*name*, reusing an existing sub-folder that differs only in case."""
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False) and entry.name.lower() == name.lower():
                    return folder / entry.name
    except FileNotFoundError:
        pass
    return folder / name
