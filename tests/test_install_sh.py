"""Checks of install.sh that don't install anything: syntax, --help, and the
uninstall safety check (with a stand-in for the installed bethmm)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
    assert "--uninstall" in result.stdout and "--force" in result.stdout
    assert "set -euo" not in result.stdout and "BASH_VERSION" not in result.stdout


def test_unknown_option():
    result = run("--bogus")
    assert result.returncode != 0 and "unknown option" in result.stderr


def fake_install(tmp_path: Path, exit_code: int, output: str) -> dict:
    """A home with an installed app whose 'bethmm status --deployed-only'
    prints *output* and exits with *exit_code*."""
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
