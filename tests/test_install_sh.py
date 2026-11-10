"""Checks of install.sh that don't install anything: syntax, --help, and the
uninstall safety check (with a stand-in for the installed bethmm)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bethmm.gui import desktop

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"


def run(*args: str, env: dict | None = None, shell: str = "bash") -> subprocess.CompletedProcess:
    return subprocess.run([shell, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_shellcheck():
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_help(shell):
    result = run("--help", shell=shell)
    assert result.returncode == 0, result.stderr
    assert "--uninstall" in result.stdout and "--force" in result.stdout and "--no-gui" in result.stdout
    assert "set -euo" not in result.stdout and "BASH_VERSION" not in result.stdout


def test_unknown_option():
    result = run("--bogus")
    assert result.returncode != 0 and "unknown option" in result.stderr


def fake_install(tmp_path: Path, exit_code: int, output: str, *, gui: bool = False) -> dict:
    """A home with an installed app whose 'bethmm status --deployed-only'
    prints *output* and exits with *exit_code* (with *gui*, also the
    bethmm-gui command and the menu entry)."""
    home = tmp_path / "home"
    venv = home / ".local/share/beth-mod-manager/venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"  # the probe ('import bethmm.cli') works; running main() answers
    python.write_text(f"#!/bin/sh\ncase \"$3\" in *main*) echo '{output}'; exit {exit_code};; esac\nexit 0\n")
    python.chmod(0o755)
    bethmm = venv / "bin" / "bethmm"
    bethmm.write_text("#!/bin/sh\nexit 0\n")
    bethmm.chmod(0o755)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/bethmm").symlink_to(bethmm)
    if gui:
        (venv / "bin" / "bethmm-gui").write_text("#!/bin/sh\nexit 0\n")
        (home / ".local/bin/bethmm-gui").symlink_to(venv / "bin" / "bethmm-gui")
        (home / ".local/share/applications").mkdir(parents=True)
        (home / ".local/share/applications/beth-mod-manager.desktop").write_text(
            desktop.desktop_entry(str(venv / "bin" / "bethmm-gui"), "/repo/icon.svg"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    return env


def test_uninstall_refuses_while_deployed_and_says_why(tmp_path):
    env = fake_install(tmp_path, 3, "Fallout 4: 2 link(s) in /games/Fallout 4")
    result = run("--uninstall", env=env)
    assert result.returncode == 1
    assert "2 link(s)" in result.stderr and "purge --all" in result.stderr
    home = Path(env["HOME"])
    assert (home / ".local/share/beth-mod-manager/venv/bin/bethmm").exists()
    assert (home / ".local/bin/bethmm").is_symlink()


def test_uninstall_when_nothing_is_deployed(tmp_path):
    env = fake_install(tmp_path, 0, "")
    home = Path(env["HOME"])
    (home / ".local/share/beth-mod-manager/mods").mkdir()
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert not (home / ".local/share/beth-mod-manager/venv").exists()
    assert not os.path.lexists(home / ".local/bin/bethmm")
    assert (home / ".local/share/beth-mod-manager/mods").is_dir()  # mods are kept
    assert "Kept" in result.stdout


def test_uninstall_when_the_check_fails_says_so(tmp_path):
    env = fake_install(tmp_path, 1, "error: the manifest is busy")
    result = run("--uninstall", env=env)
    assert result.returncode == 1
    assert "could not check" in result.stderr and "still has files" not in result.stderr
    assert (Path(env["HOME"]) / ".local/share/beth-mod-manager/venv").exists()


def test_uninstall_with_nothing_installed_fails(tmp_path):
    home = tmp_path / "empty home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    result = run("--uninstall", env=env)
    assert result.returncode == 1 and "no installation found" in result.stderr


def test_uninstall_force_skips_the_check(tmp_path):
    env = fake_install(tmp_path, 3, "Fallout 4: 2 link(s)")
    result = run("--uninstall", "--force", env=env)
    assert result.returncode == 0, result.stderr
    assert not (Path(env["HOME"]) / ".local/share/beth-mod-manager/venv").exists()


def test_uninstall_with_a_broken_environment_still_checks(tmp_path):
    """The venv's Python is gone (e.g. after a distro upgrade): the check runs
    with the app in this folder, and nothing deployed means it goes ahead."""
    env = fake_install(tmp_path, 3, "never asked")
    home = Path(env["HOME"])
    (home / ".local/share/beth-mod-manager/venv/bin/python").unlink()
    (home / ".local/share/beth-mod-manager/venv/bin/python").symlink_to("/nonexistent/python3.11")
    env["BETHMM_DATA_DIR"] = str(home / "data")
    env["BETHMM_CONFIG_DIR"] = str(home / "config")
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert not (home / ".local/share/beth-mod-manager/venv").exists()


def test_uninstall_under_sudo_refuses(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("needs root")
    env = fake_install(tmp_path, 0, "")
    env["SUDO_USER"] = "someone"
    result = run("--uninstall", env=env)
    assert result.returncode == 1 and "not with sudo" in result.stderr


def test_uninstall_with_the_command_pointing_elsewhere_fails(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/bethmm").symlink_to("/opt/other/venv/bin/bethmm")
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    result = run("--uninstall", env=env)
    assert result.returncode == 1 and "points at /opt/other" in result.stderr
    assert (home / ".local/bin/bethmm").is_symlink()


def test_uninstall_recognises_its_own_launcher_however_spelled(tmp_path):
    env = fake_install(tmp_path, 0, "")
    home = Path(env["HOME"])
    launcher = home / ".local/bin/bethmm"
    launcher.unlink()
    launcher.symlink_to(f"{home}/.local/share//beth-mod-manager/venv/bin/bethmm")
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(launcher)


def test_uninstall_refuses_when_the_command_belongs_to_another_install(tmp_path):
    env = fake_install(tmp_path, 0, "")
    home = Path(env["HOME"])
    (home / ".local/bin/bethmm").unlink()
    (home / ".local/bin/bethmm").symlink_to("/opt/other/venv/bin/bethmm")
    result = run("--uninstall", env=env)
    assert result.returncode == 1 and "points at /opt/other" in result.stderr
    assert (home / ".local/share/beth-mod-manager/venv").exists()


def test_uninstall_removes_the_gui_command_and_menu_entry(tmp_path):
    env = fake_install(tmp_path, 0, "", gui=True)
    home = Path(env["HOME"])
    (home / ".local/share/applications/other.desktop").write_text("[Desktop Entry]\n")
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(home / ".local/bin/bethmm-gui")
    assert not os.path.lexists(home / ".local/share/applications/beth-mod-manager.desktop")
    assert (home / ".local/share/applications/other.desktop").exists()


def test_uninstall_leaves_a_menu_entry_and_command_it_did_not_make(tmp_path):
    env = fake_install(tmp_path, 0, "", gui=True)
    home = Path(env["HOME"])
    entry = home / ".local/share/applications/beth-mod-manager.desktop"
    entry.write_text("[Desktop Entry]\nName=Mine\nExec=/opt/mine\n")
    (home / ".local/bin/bethmm-gui").unlink()
    (home / ".local/bin/bethmm-gui").symlink_to("/opt/other/bethmm-gui")
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert entry.read_text() == "[Desktop Entry]\nName=Mine\nExec=/opt/mine\n"
    assert os.readlink(home / ".local/bin/bethmm-gui") == "/opt/other/bethmm-gui"


def test_uninstall_finds_a_leftover_menu_entry(tmp_path):
    env = fake_install(tmp_path, 0, "", gui=True)
    home = Path(env["HOME"])
    shutil.rmtree(home / ".local/share/beth-mod-manager/venv")
    (home / ".local/bin/bethmm").unlink()
    result = run("--uninstall", env=env)
    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(home / ".local/share/applications/beth-mod-manager.desktop")


def test_the_script_and_the_app_agree_on_the_menu_entry_marker():
    assert f'DESKTOP_MARKER="{desktop.MARKER}"' in SCRIPT.read_text()


def _exec_argv(entry: str) -> list[str]:
    """What a desktop environment runs for the entry's Exec line (string
    escapes, then field codes, then the quoting), with no files given."""
    (line,) = [x for x in entry.splitlines() if x.startswith("Exec=")]
    value, out, i = line[len("Exec="):], [], 0
    while i < len(value):
        if value[i] == "\\":
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    value = "".join(out)
    assert value.endswith(" %F")
    value = value[:-3].replace("%%", "%")
    # The spec's quoting: inside "...", a backslash escapes " ` $ and itself.
    args, current, quoted, i = [], None, False, 0
    while i < len(value):
        c = value[i]
        if quoted and c == "\\" and value[i + 1] in '"`$\\':
            current += value[i + 1]
            i += 1
        elif c == '"':
            quoted, current = not quoted, current or ""
        elif c == " " and not quoted:
            if current is not None:
                args.append(current)
            current = None
        else:
            current = (current or "") + c
        i += 1
    assert not quoted
    return args + ([current] if current is not None else [])


@pytest.mark.parametrize("exe", [
    "/home/me/.local/share/beth-mod-manager/venv/bin/bethmm-gui",
    "/home/Jo Smith/.local/share/beth-mod-manager/venv/bin/bethmm-gui",
    '/home/odd $HOME `x` "q" 100% back\\slash/venv/bin/bethmm-gui',
])
def test_menu_entry_runs_the_gui_whatever_the_path(exe):
    entry = desktop.desktop_entry(exe, "/repo/bethmm/gui/icon.svg")
    assert _exec_argv(entry) == [exe]
    assert entry.startswith("[Desktop Entry]\n") and desktop.MARKER in entry.splitlines()
    assert "Icon=/repo/bethmm/gui/icon.svg" in entry.splitlines()


def test_menu_entry_command_line(capsys):
    assert desktop.main(["/x/bethmm-gui", "/x/icon.svg"]) == 0
    assert "Exec=\"/x/bethmm-gui\" %F" in capsys.readouterr().out
    assert desktop.main([]) == 2


def test_the_test_tools_install_without_qt():
    """--dev --no-gui: pytest-qt refuses to start without a Qt binding, so it
    comes only with the GUI's test extra."""
    import tomllib
    extras = tomllib.loads((SCRIPT.parent / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert not any("pytest-qt" in r for r in extras["dev"])
    assert any("pytest-qt" in r for r in extras["gui-dev"])
    assert "EXTRA_NAMES+=(gui-dev)" in SCRIPT.read_text()


# ---------------------------------------------------------------- system packages, per distribution

TOOLS = ("dirname", "basename", "id", "grep", "sed", "awk", "readlink", "tr", "sort", "uname")


def system_check(tmp_path: Path, os_release: str, *, installed: tuple[str, ...] = ()) -> tuple[
        subprocess.CompletedProcess, list[list[str]]]:
    """Run 'install.sh --yes --force' on a stand-in system: *os_release*,
    *installed* packages, a package manager that records what it is asked
    to do, and no 7z, so the script stops right after the system packages."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    (bin_dir / "python3").symlink_to(sys.executable)
    record = tmp_path / "package-manager.log"
    listed = " ".join(installed)

    def script(name: str, body: str) -> None:
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)

    script("sudo", 'exec "$@"')
    script("dnf", f'echo "dnf $*" >> {record}')
    script("apt-get", f'echo "apt-get $*" >> {record}')
    script("rpm", f'for p in {listed}; do [ "$p" = "$3" ] && exit 0; done; exit 1')
    script("dpkg-query", f'for p in {listed}; do [ "$p" = "$4" ] && echo "install ok installed" && exit 0; done; exit 1')
    script("apt-cache", 'echo "  Installed: (none)"; echo "  Candidate: 1.0-1"')
    (tmp_path / "os-release").write_text(os_release)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": str(bin_dir), "HOME": str(home), "BETHMM_OS_RELEASE": str(tmp_path / "os-release")}
    result = run("--yes", "--force", env=env, shell=shutil.which("bash"))
    calls = [line.split() for line in record.read_text().splitlines()] if record.exists() else []
    return result, calls


FEDORA = 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=45\nPRETTY_NAME="Fedora Linux 45 (Workstation Edition)"\n'
UBUNTU = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n'


def test_fedora_gets_its_packages_with_dnf(tmp_path):
    """7zip, bsdtar (Fedora's 7-Zip has no RAR) and the Qt libraries that are missing."""
    present = ("libxcb", "libxkbcommon", "libglvnd-egl", "libglvnd-glx", "fontconfig", "dbus-libs",
               "libwayland-client", "libwayland-cursor", "xcb-util", "xcb-util-image", "xcb-util-keysyms",
               "xcb-util-renderutil", "libxkbcommon-x11")
    result, calls = system_check(tmp_path, FEDORA, installed=present)
    assert "distro: Fedora Linux 45 (Workstation Edition)" in result.stdout
    assert calls == [["dnf", "install", "-y", "bsdtar", "7zip", "xcb-util-cursor", "xcb-util-wm"]], result.stderr
    assert "7z is still missing" in result.stderr  # the stand-in dnf installed nothing


def test_fedora_derivatives_count_as_fedora(tmp_path):
    result, calls = system_check(tmp_path, 'ID=nobara\nID_LIKE="rhel centos fedora"\nPRETTY_NAME="Nobara"\n')
    assert calls and calls[0][:3] == ["dnf", "install", "-y"]


def test_ubuntu_keeps_its_apt_packages(tmp_path):
    result, calls = system_check(tmp_path, UBUNTU)
    assert "distro: Ubuntu 24.04.3 LTS" in result.stdout
    assert calls == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "7zip", "7zip-rar", "libxcb-cursor0", "libxkbcommon-x11-0", "libxcb-icccm4",
         "libxcb-image0", "libxcb-keysyms1", "libxcb-render-util0", "libxcb-shape0", "libxcb-util1",
         "libxcb-xinerama0", "libxcb-xkb1", "libegl1", "libgl1", "libfontconfig1", "libdbus-1-3"],
    ], result.stderr


def test_another_distro_installs_nothing_and_says_what_to_do(tmp_path):
    result, calls = system_check(tmp_path, 'ID=arch\nPRETTY_NAME="Arch Linux"\n')
    assert calls == []
    assert "written for Ubuntu-based distros and Fedora" in result.stderr
    assert "--no-system-packages" in result.stderr
