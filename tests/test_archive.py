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
    # 7z versions differ: some refuse the absolute link themselves, others
    # create it and we remove it. Either way it is reported.
    assert any("Mod/link" in w or "/etc/passwd" in w for w in result.warnings)
    assert any("Mod/relative" in w for w in result.warnings)


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


# --- error reporting (canned 7z output) -------------------------------------


def _proc(stdout: str, code: int = 2):
    return subprocess.CompletedProcess([], code, stdout.encode(), b"")


LISTING_HEAD = "7-Zip 23.01 (x64)\n\n--\nPath = m.rar\nType = Rar5\n\n----------\nPath = a.esp\nEncrypted = -\n\n"


def test_rar5_without_codec_suggests_rar_support_not_password(tmp_path):
    out = LISTING_HEAD + "ERROR: Unsupported Method : a.esp\n\nSub items Errors: 1\n\nArchives with Errors: 1\n"
    err = archive._failure(tmp_path / "Mod-1-1-0.rar", _proc(out), "/usr/bin/7z")
    assert "password" not in str(err)
    assert "Unsupported Method : a.esp" in str(err)
    assert "sudo apt install 7zip-rar" in str(err)


def test_p7zip_gets_the_p7zip_rar_package_name(tmp_path):
    out = "7-Zip [64] 16.02 : p7zip Version 16.02\nERROR: Unsupported Method : a.esp\nSub items Errors: 1\n"
    assert "sudo apt install p7zip-rar" in str(archive._failure(tmp_path / "m.rar", _proc(out), "/usr/bin/7z"))


def test_crc_error_says_download_again_and_hides_counters(tmp_path):
    out = "ERROR: CRC Failed : big.dds\n\nSub items Errors: 1\n\nArchives with Errors: 1\n"
    msg = str(archive._failure(tmp_path / "m.7z", _proc(out), "/usr/bin/7z"))
    assert "CRC Failed : big.dds" in msg and "Sub items" not in msg
    assert "download it again" in msg


def test_disk_full_is_reported_as_disk_space(tmp_path):
    out = "System ERROR:\nerrno=28 : No space left on device\nArchives with Errors: 1\n"
    msg = str(archive._failure(tmp_path / "m.zip", _proc(out), "/usr/bin/7z", tmp_path / "tmp" / "x"))
    assert "Not enough disk space" in msg and str(tmp_path / "tmp") in msg


def test_wrong_password_is_reported(tmp_path):
    out = "ERROR: Wrong password : a.esp\nSub items Errors: 1\n"
    assert "password-protected" in str(archive._failure(tmp_path / "m.zip", _proc(out), "/usr/bin/7z"))


def test_failures_are_logged(tmp_path, caplog):
    with caplog.at_level("ERROR", logger="bethmm"):
        archive._failure(tmp_path / "m.7z", _proc("ERROR: Data Error : x.esp\n"), "/usr/bin/7z")
    assert "Data Error : x.esp" in caplog.text


@needs_7z
def test_truncated_download_is_not_called_password_protected(tmp_path):
    src = _zip(tmp_path / "full.zip", {f"Mod/textures/{i}.dds": bytes(2000) + bytes([i]) for i in range(20)})
    cut = tmp_path / "Cut-1-1-0.zip"
    cut.write_bytes(src.read_bytes()[: src.stat().st_size // 2])
    with pytest.raises(ArchiveError) as exc:
        extract_archive(cut, tmp_path / "out")
    assert "password" not in str(exc.value)


@needs_7z
def test_backslash_separators_become_folders(tmp_path):
    src = tmp_path / "win.zip"
    with zipfile.ZipFile(src, "w") as zf:
        for name in ("Data\\BackslashMod.esp", "Data\\Textures\\a.dds", "data\\textures\\b.dds"):
            info = zipfile.ZipInfo(name)
            info.create_system = 0
            zf.writestr(info, b"x")
    result = extract_archive(src, tmp_path / "out")
    assert result.files == ["Data/BackslashMod.esp", "Data/Textures/a.dds", "Data/Textures/b.dds"]


@needs_7z
def test_backslash_traversal_refuses_the_archive(tmp_path):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as zf:
        info = zipfile.ZipInfo("..\\evil.txt")
        info.create_system = 0
        zf.writestr(info, b"x")
        zf.writestr("ok.esp", b"x")
    with pytest.raises(ArchiveError, match="unsafe"):
        extract_archive(src, tmp_path / "work" / "out")
    assert not (tmp_path / "work" / "evil.txt").exists()


def test_split_backslash_names_drops_unsafe_leftovers(tmp_path):
    (tmp_path / "..\\evil.txt").write_bytes(b"x")
    (tmp_path / "Data\\a.esp").write_bytes(b"x")
    warnings = []
    archive._split_backslash_names(tmp_path, warnings)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["a.esp"]
    assert (tmp_path / "Data" / "a.esp").is_file()
    assert any("unsafe" in w for w in warnings)


def test_cannot_open_reports_cause_without_repeating_the_path(tmp_path):
    arc = tmp_path / "Broken-1-1-0.7z"
    out = (f"Listing archive: {arc}\n\nERROR: {arc} : {arc}\nOpen ERROR: Cannot open the file as [7z] archive\n\n"
           "\nERRORS:\nUnexpected end of archive\n\n\nErrors: 1\n")
    msg = str(archive._failure(arc, _proc(out), "/usr/bin/7z"))
    assert str(tmp_path) not in msg
    assert "Cannot open the file as [7z] archive; Unexpected end of archive" in msg
    assert "download it again" in msg


@needs_7z
def test_real_truncated_7z_message(tmp_path):
    src_dir = tmp_path / "s"
    make = src_dir / "Mod"
    make.mkdir(parents=True)
    (make / "a.dds").write_bytes(bytes(range(256)) * 50)
    subprocess.run([find_sevenzip(), "a", "-bd", str(tmp_path / "m.7z"), "Mod"], cwd=src_dir, check=True, capture_output=True)
    cut = tmp_path / "Cut-1-1-0.7z"
    cut.write_bytes((tmp_path / "m.7z").read_bytes()[:100])
    with pytest.raises(ArchiveError) as exc:
        extract_archive(cut, tmp_path / "out")
    msg = str(exc.value)
    assert "download it again" in msg and "password" not in msg and str(tmp_path) not in msg


@needs_7z
def test_an_archive_bigger_than_the_free_space_is_refused_before_extracting(tmp_path, monkeypatch):
    src = _zip(tmp_path / "big.zip", {"Data/a.esp": b"x" * 5000})
    real = shutil.disk_usage
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda p: real(p)._replace(free=4000))
    with pytest.raises(ArchiveError, match="unpacks to .*but only .* is free"):
        extract_archive(src, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


@needs_7z
@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_a_link_to_a_folder_outside_cannot_be_written_through(tmp_path, target):
    """The archive's link is created (or not) before the file "under" it: 7z
    must not write that file into the folder the link points to."""
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "through.zip"
    with zipfile.ZipFile(src, "w") as zf:
        info = zipfile.ZipInfo("Data/lnk")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, str(outside) if target == "absolute" else "../../outside")
        zf.writestr("Data/lnk/pwned.txt", b"x")
    try:
        result = extract_archive(src, tmp_path / "out")
    except ArchiveError:
        result = None
    assert list(outside.iterdir()) == []
    if result is not None:
        assert not (tmp_path / "out" / "Data" / "lnk").is_symlink()
