import shutil
import stat
import subprocess
import zipfile

import pytest

from bethmm import archive
from bethmm.archive import ArchiveError, SevenZipMissing, extract_archive, find_sevenzip, list_archive

needs_7z = pytest.mark.skipif(not any(shutil.which(n) for n in archive.SEVENZIP_NAMES), reason="7z not installed")


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_missing_7z_explains_how_to_install(monkeypatch):
    monkeypatch.setattr(archive.shutil, "which", lambda name: None)
    with pytest.raises(SevenZipMissing) as exc:
        find_sevenzip()
    assert "sudo apt install 7zip" in str(exc.value)


def test_configured_7z_path_must_exist(tmp_path):
    with pytest.raises(SevenZipMissing):
        find_sevenzip(str(tmp_path / "nope" / "7z"))


def test_parse_slt_handles_dirs_files_and_empty_values():
    out = (
        "7-Zip 23.01\n\nListing archive: x.zip\n\n--\nPath = x.zip\nType = zip\n\n----------\n"
        "Path = Mod\nFolder = +\nSize = 0\n\n"
        "Path = Mod\\Textures\\a.dds\nFolder = -\nSize = 12\nCRC =\n\n"
        "Path = Mod/b.esp\nSize = 3\nAttributes = A -rw-r--r--\n\n"
    )
    entries = archive._parse_slt(out)
    assert [(e.path, e.is_dir, e.size) for e in entries] == [
        ("Mod", True, 0),
        ("Mod/Textures/a.dds", False, 12),
        ("Mod/b.esp", False, 3),
    ]


@needs_7z
def test_extract_zip_with_wrapper_folder(tmp_path):
    src = _zip(tmp_path / "CoolMod-123-1-0.zip", {
        "CoolMod v1.2/Textures/a.dds": b"dds",
        "CoolMod v1.2/CoolMod.esp": b"TES4",
        "readme.txt": b"hi",
    })
    names = [e.path for e in list_archive(src) if not e.is_dir]
    assert sorted(names) == ["CoolMod v1.2/CoolMod.esp", "CoolMod v1.2/Textures/a.dds", "readme.txt"]
    result = extract_archive(src, tmp_path / "out")
    assert result.files == ["CoolMod v1.2/CoolMod.esp", "CoolMod v1.2/Textures/a.dds", "readme.txt"]
    assert (tmp_path / "out" / "CoolMod v1.2" / "Textures" / "a.dds").read_bytes() == b"dds"
    assert result.warnings == []


@needs_7z
def test_extract_7z_and_unicode_names(tmp_path):
    src_dir = tmp_path / "src"
    (src_dir / "Mod Ä").mkdir(parents=True)
    (src_dir / "Mod Ä" / "Ünïcode.esp").write_bytes(b"x")
    exe = find_sevenzip()
    subprocess.run([exe, "a", "-bd", str(tmp_path / "m.7z"), "Mod Ä"], cwd=src_dir, check=True, capture_output=True)
    result = extract_archive(tmp_path / "m.7z", tmp_path / "out")
    assert result.files == ["Mod Ä/Ünïcode.esp"]


@needs_7z
def test_refuses_path_traversal(tmp_path):
    src = _zip(tmp_path / "evil.zip", {"../evil.txt": b"x", "ok.txt": b"y"})
    with pytest.raises(ArchiveError, match="unsafe"):
        extract_archive(src, tmp_path / "work" / "out")
    assert not (tmp_path / "work" / "evil.txt").exists()
    assert not (tmp_path / "work" / "out").exists()


@needs_7z
def test_symlinks_in_archive_are_removed(tmp_path):
    src = tmp_path / "link.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("Mod/real.txt", b"data")
        info = zipfile.ZipInfo("Mod/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
        info = zipfile.ZipInfo("Mod/relative")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "real.txt")
    result = extract_archive(src, tmp_path / "out")
    assert result.files == ["Mod/real.txt"]
    assert not (tmp_path / "out" / "Mod" / "link").is_symlink()
    assert not (tmp_path / "out" / "Mod" / "relative").is_symlink()
    assert any("outside the mod" in w for w in result.warnings)
    assert any("removed symlink Mod/relative" in w for w in result.warnings)


@needs_7z
def test_destination_must_be_empty(tmp_path):
    src = _zip(tmp_path / "m.zip", {"a.esp": b"x"})
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old").write_text("x")
    with pytest.raises(ArchiveError, match="not empty"):
        extract_archive(src, tmp_path / "out")


@needs_7z
def test_corrupt_archive_raises(tmp_path):
    bad = tmp_path / "bad.7z"
    bad.write_bytes(b"this is not an archive")
    with pytest.raises(ArchiveError):
        extract_archive(bad, tmp_path / "out")


@needs_7z
def test_password_protected_archive_is_reported(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.esp").write_bytes(b"x")
    exe = find_sevenzip()
    subprocess.run([exe, "a", "-bd", "-psecret", "-mhe=on", str(tmp_path / "p.7z"), "a.esp"],
                   cwd=tmp_path / "s", check=True, capture_output=True)
    with pytest.raises(ArchiveError, match="password"):
        extract_archive(tmp_path / "p.7z", tmp_path / "out")


def test_not_a_file(tmp_path):
    with pytest.raises(ArchiveError):
        list_archive(tmp_path / "missing.7z")
