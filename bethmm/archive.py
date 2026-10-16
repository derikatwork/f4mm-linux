"""List and extract mod archives (.zip/.7z/.rar) with the system 7z binary."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

#: Binaries tried in order. ``7za`` can't open .rar files but handles the rest.
SEVENZIP_NAMES = ("7z", "7zz", "7za")

INSTALL_HINT = (
    "Install it with:  sudo apt install 7zip 7zip-rar\n"
    "(7zip-rar is in Ubuntu's multiverse repository and adds .rar support; on older\n"
    "Ubuntu-based releases use:  sudo apt install p7zip-full p7zip-rar)\n"
    "or set the path to your 7z binary in the settings."
)


class ArchiveError(Exception):
    pass


class SevenZipMissing(ArchiveError):
    def __init__(self, detail: str = "") -> None:
        msg = "The 7z program was not found, so mod archives can't be opened."
        if detail:
            msg += f" ({detail})"
        super().__init__(f"{msg}\n{INSTALL_HINT}")


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


def _failure(archive: Path, proc: subprocess.CompletedProcess, sevenzip: str) -> ArchiveError:
    output = (_decode(proc.stderr) + "\n" + _decode(proc.stdout)).strip()
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "error" in ln.lower() or "can not" in ln.lower() or "cannot" in ln.lower()]
    detail = "; ".join(errors[-3:]) or (lines[-1] if lines else f"exit code {proc.returncode}")
    msg = f"7z could not read {archive.name}: {detail}"
    lowered = output.lower()
    if "wrong password" in lowered or "enter password" in lowered or "encrypted" in lowered:
        msg = f"{archive.name} is password-protected; that isn't supported."
    elif archive.suffix.lower() == ".rar" and (
        "unsupported" in lowered or "can not open the file as archive" in lowered
        or "cannot open the file as archive" in lowered or Path(sevenzip).name == "7za"
    ):
        msg += "\nThis 7z build can't open .rar files. Install RAR support:  sudo apt install 7zip-rar"
    return ArchiveError(msg)


def list_archive(archive: str | Path, sevenzip: str | None = None) -> list[ArchiveEntry]:
    archive = Path(archive)
    if not archive.is_file():
        raise ArchiveError(f"{archive} is not a file")
    exe = find_sevenzip(sevenzip)
    proc = _run([exe, "l", "-slt", "-p", "--", str(archive)])
    if proc.returncode != 0:
        raise _failure(archive, proc, exe)
    return _parse_slt(_decode(proc.stdout))


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

    Archives containing absolute paths or ``..`` are refused before anything
    is extracted. Symlinks that 7z creates from the archive are removed (a
    mod has no business shipping links) and reported in ``warnings``.
    """
    archive = Path(archive)
    dest = Path(dest)
    entries = list_archive(archive, sevenzip)
    bad = [e.path for e in entries if _unsafe(e.path)]
    if bad:
        raise ArchiveError(f"{archive.name} contains unsafe paths and was not extracted: {', '.join(bad[:5])}")
    if dest.exists() and any(dest.iterdir()):
        raise ArchiveError(f"extraction folder {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    exe = find_sevenzip(sevenzip)
    log.info("EXTRACT %s -> %s", archive, dest)
    proc = _run([exe, "x", "-y", "-bd", "-p", f"-o{dest}", "--", str(archive)])
    warnings: list[str] = []
    if proc.returncode != 0:
        output = _decode(proc.stdout) + "\n" + _decode(proc.stderr)
        errors = [ln.strip() for ln in output.splitlines() if ln.strip().startswith("ERROR:")]
        # 7z refuses links pointing outside the archive and exits with 2; that
        # alone is not a reason to fail the whole mod.
        if errors and all("Dangerous link path was ignored" in e for e in errors):
            for e in errors:
                warnings.append(f"7z skipped a link that points outside the mod: {e.split(':', 2)[-1].strip()}")
        else:
            raise _failure(archive, proc, exe)

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
    return ExtractResult(files, warnings)
