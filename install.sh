#!/usr/bin/env bash
# Install BETH Mod Manager for the current user on Ubuntu-based distros
# (Ubuntu, Linux Mint, Pop!_OS, Zorin, elementary, KDE neon ...).
#
#   ./install.sh                 install or update (run again after 'git pull')
#   ./install.sh --yes           don't ask before installing system packages
#   ./install.sh --dev           also install the test tools and run the tests
#   ./install.sh --no-gui        command line only: no Qt, no menu entry (removes
#                                the menu entry and bethmm-gui of an earlier install)
#   ./install.sh --no-system-packages
#                                never call apt; only check what is there
#   ./install.sh --uninstall     remove the app (keeps your mods, backups and settings);
#                                refuses while mods are still deployed in a game folder
#   ./install.sh --force         install even when run as root / uninstall even
#                                though mods are still deployed
#
# What it does:
#   1. checks for Python 3.11+, the 7z program and the system libraries Qt needs,
#      offering to install what is missing with apt (python3-venv, 7zip, 7zip-rar
#      or the p7zip equivalents, libxcb-cursor0 ...);
#   2. creates a private Python environment in ~/.local/share/beth-mod-manager/venv
#      with the app (and the Qt toolkit for its GUI) installed from this folder
#      (keep this folder: updates are a 'git pull' away);
#   3. puts the 'bethmm' and 'bethmm-gui' commands in ~/.local/bin and a
#      "BETH Mod Manager" entry in your applications menu.

# 'sh install.sh' runs dash on Ubuntu: start again with bash.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

APP_NAME="BETH Mod Manager"
APP_SLUG="beth-mod-manager"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_SLUG"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_SLUG"
VENV="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/bethmm"
GUI_LAUNCHER="$BIN_DIR/bethmm-gui"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP_SLUG.desktop"
DESKTOP_MARKER="X-BethMM-Installed-By=install.sh"  # = bethmm.gui.desktop.MARKER
ICON="$REPO_DIR/bethmm/gui/icon.svg"

ASSUME_YES=0
DEV=0
GUI=1
SYSTEM_PACKAGES=1
UNINSTALL=0
FORCE=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# The comment block at the top of this file.
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --dev) DEV=1 ;;
        --no-gui) GUI=0 ;;
        --no-system-packages) SYSTEM_PACKAGES=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
    shift
done

# Under sudo, $HOME is root's: an install would be root's, an uninstall would find nothing.
if [ "$(id -u)" -eq 0 ] && [ "$FORCE" -eq 0 ] && { [ "$UNINSTALL" -eq 0 ] || [ -n "${SUDO_USER:-}${PKEXEC_UID:-}" ]; }; then
    die "run this as your normal user, not with sudo (it asks for sudo itself when needed)"
fi

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }

find_python() {
    local candidate
    for candidate in python3 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# Links and menu entries this script made (anything else there is left alone).
our_link() { [ -L "$1" ] && [ "$(readlink -m "$1")" = "$(readlink -m "$2")" ]; }
our_desktop_file() { [ -f "$DESKTOP_FILE" ] && [ ! -L "$DESKTOP_FILE" ] && grep -qxF "$DESKTOP_MARKER" "$DESKTOP_FILE"; }

remove_gui_entries() {
    if our_link "$GUI_LAUNCHER" "$VENV/bin/bethmm-gui"; then
        rm "$GUI_LAUNCHER"
        say "removed $GUI_LAUNCHER"
    fi
    if our_desktop_file; then
        rm "$DESKTOP_FILE"
        say "removed $DESKTOP_FILE"
    fi
}

confirm() {
    # confirm "question" -> 0 for yes. Defaults to yes; --yes answers for you.
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ -t 0 ] || die "$1 -- re-run with --yes to answer yes non-interactively"
    local reply
    read -r -p "$1 [Y/n] " reply
    case "$reply" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------ uninstall

if [ "$UNINSTALL" -eq 1 ]; then
    step "Uninstalling $APP_NAME"
    if [ -L "$LAUNCHER" ] && [ "$(readlink -m "$LAUNCHER")" != "$(readlink -m "$VENV/bin/bethmm")" ]; then
        die "the bethmm command points at $(readlink "$LAUNCHER"), not at $VENV -- run this with the XDG_DATA_HOME you installed with"
    fi
    if [ ! -e "$VENV" ] && [ ! -L "$LAUNCHER" ] && ! our_link "$GUI_LAUNCHER" "$VENV/bin/bethmm-gui" && ! our_desktop_file; then
        die "no installation found in $DATA_DIR (for this user: $HOME)"
    fi
    if [ -e "$VENV" ] && [ "$FORCE" -eq 0 ]; then
        # The installed app answers, or (if its Python is gone) the app in this
        # folder. -I: a 'bethmm' folder in the current directory can't stand in.
        probe='import bethmm.cli'
        run='import sys; from bethmm.cli import main; sys.exit(main(["status", "--deployed-only"]))'
        if "$VENV/bin/python" -I -c "$probe" >/dev/null 2>&1; then
            check=("$VENV/bin/python" -I -c "$run")
        elif PY="$(find_python)" && "$PY" -I -c "import sys; sys.path.insert(0, sys.argv[1]); $probe" "$REPO_DIR" >/dev/null 2>&1; then
            check=("$PY" -I -c "import sys; sys.path.insert(0, sys.argv[1]); $run" "$REPO_DIR")
        else
            die "can't run the app to check that no mods are deployed (its environment is broken and no usable Python 3.11+ with python3-yaml was found); fix that, or use --force"
        fi
        set +e
        status="$("${check[@]}" 2>&1)"
        code=$?
        set -e
        if [ "$code" -eq 3 ]; then
            printf '%s\n' "$status" >&2
            die "the app still has files in a game folder (see above). Run 'bethmm purge --all' first, or --force to uninstall anyway"
        elif [ "$code" -ne 0 ]; then
            printf '%s\n' "$status" >&2
            die "could not check whether mods are still deployed (see above); fix that, or use --force"
        fi
        [ -z "$status" ] || say "$status"
    fi
    if our_link "$LAUNCHER" "$VENV/bin/bethmm"; then
        rm "$LAUNCHER"
        say "removed $LAUNCHER"
    fi
    remove_gui_entries
    if [ -d "$VENV" ]; then
        rm -rf "$VENV"
        say "removed $VENV"
    fi
    say ""
    say "Kept (delete by hand if you no longer want them):"
    say "  settings        : $CONFIG_DIR"
    say "  mods, backups,  : $DATA_DIR"
    say "  log and manifest"
    exit 0
fi

# ------------------------------------------------------------------ checks

step "Checking your system"

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *" ubuntu "*|*" debian "*) say "distro: ${PRETTY_NAME:-$ID}" ;;
        *) warn "this script is written for Ubuntu-based distros; found ${PRETTY_NAME:-unknown}."
           warn "install Python 3.11+, its venv module and 7z yourself, then re-run with --no-system-packages"
           SYSTEM_PACKAGES=0 ;;
    esac
fi

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
pkg_available() {
    local candidate
    candidate="$(apt-cache policy "$1" 2>/dev/null | sed -n 's/^ *Candidate: //p')"
    [ -n "$candidate" ] && [ "$candidate" != "(none)" ]
}

PY="$(find_python || true)"
PACKAGES=()
NOTES=()

if [ -z "$PY" ]; then
    say "Python 3.11 or newer: not found ($(python3 --version 2>/dev/null || echo 'no python3'))"
    PACKAGES+=(python3.11 python3.11-venv)
else
    say "python: $PY ($("$PY" --version))"
    if ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then
        version="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
        if [ "$(basename "$PY")" = "python3" ]; then PACKAGES+=(python3-venv); else PACKAGES+=("python${version}-venv"); fi
    fi
fi

if command -v 7z >/dev/null 2>&1 || command -v 7zz >/dev/null 2>&1; then
    say "7z: $(command -v 7z || command -v 7zz)"
    SEVEN_ZIP_PKG=""
    if pkg_installed 7zip; then SEVEN_ZIP_PKG=7zip; elif pkg_installed p7zip-full; then SEVEN_ZIP_PKG=p7zip-full; fi
else
    say "7z: not found"
    SEVEN_ZIP_PKG=""
fi

if [ "$SYSTEM_PACKAGES" -eq 1 ]; then
    if [ -z "$SEVEN_ZIP_PKG" ] && ! command -v 7z >/dev/null 2>&1 && ! command -v 7zz >/dev/null 2>&1; then
        if pkg_available 7zip; then SEVEN_ZIP_PKG=7zip; else SEVEN_ZIP_PKG=p7zip-full; fi
        PACKAGES+=("$SEVEN_ZIP_PKG")
    fi
    # RAR support lives in a separate package from Ubuntu's multiverse repository.
    case "$SEVEN_ZIP_PKG" in
        7zip) RAR_PKG=7zip-rar ;;
        p7zip-full) RAR_PKG=p7zip-rar ;;
        *) RAR_PKG="" ;;
    esac
    if [ -n "$RAR_PKG" ] && ! pkg_installed "$RAR_PKG"; then
        if pkg_available "$RAR_PKG"; then
            PACKAGES+=("$RAR_PKG")
        else
            NOTES+=(".rar archives need $RAR_PKG from the multiverse repository:  sudo add-apt-repository multiverse && sudo apt install $RAR_PKG")
        fi
    fi
fi

# The libraries Qt's X11 support needs that a desktop install may lack (most
# often libxcb-cursor0). Names a release doesn't have are skipped: the check
# after installing reports anything still missing.
GUI_LIBS=(libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
          libxcb-render-util0 libxcb-shape0 libxcb-util1 libxcb-xinerama0 libxcb-xkb1 libegl1 libgl1
          libfontconfig1 libdbus-1-3)
if [ "$GUI" -eq 1 ] && [ "$SYSTEM_PACKAGES" -eq 1 ]; then
    missing_libs=()
    for lib in "${GUI_LIBS[@]}"; do
        if ! pkg_installed "$lib" && pkg_available "$lib"; then missing_libs+=("$lib"); fi
    done
    if [ "${#missing_libs[@]}" -gt 0 ]; then
        say "libraries for the GUI: missing ${missing_libs[*]}"
        PACKAGES+=("${missing_libs[@]}")
    else
        say "libraries for the GUI: present"
    fi
fi

if [ "${#PACKAGES[@]}" -gt 0 ]; then
    if [ "$SYSTEM_PACKAGES" -eq 0 ]; then
        die "missing: ${PACKAGES[*]} -- install them and run this again"
    fi
    step "Installing system packages: ${PACKAGES[*]}"
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "sudo is not available; install ${PACKAGES[*]} as root and run this again"
        SUDO=sudo
    fi
    confirm "Install ${PACKAGES[*]} with apt (needs your password)?" || die "cancelled; install ${PACKAGES[*]} yourself and run this again"
    $SUDO apt-get update
    $SUDO apt-get install -y "${PACKAGES[@]}"
    PY="${PY:-$(find_python || true)}"
    [ -n "$PY" ] || die "Python 3.11+ still not found after installing packages"
    "$PY" -c 'import ensurepip, venv' 2>/dev/null || die "the Python venv module is still missing for $PY"
fi
command -v 7z >/dev/null 2>&1 || command -v 7zz >/dev/null 2>&1 || die "7z is still missing"

# ------------------------------------------------------------------ app

step "Installing $APP_NAME into $VENV"
[ "$GUI" -eq 1 ] && say "(the first time this downloads the Qt toolkit for the GUI, about 100 MB)"
# A distro upgrade can leave the environment's Python gone, too old, or a new
# version that doesn't see the packages installed for the old one.
if [ -e "$VENV" ] && ! "$VENV/bin/python" -I -c 'import sys, pip; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    say "the existing environment in $VENV can't be used (its Python changed); recreating it"
    rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$DATA_DIR"
    "$PY" -m venv "$VENV"
fi
EXTRA_NAMES=()
[ "$GUI" -eq 1 ] && EXTRA_NAMES+=(gui)
[ "$DEV" -eq 1 ] && EXTRA_NAMES+=(dev)
EXTRAS=""
[ "${#EXTRA_NAMES[@]}" -eq 0 ] || EXTRAS="[$(IFS=,; echo "${EXTRA_NAMES[*]}")]"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir --editable "$REPO_DIR$EXTRAS"

mkdir -p "$BIN_DIR"
if [ -e "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ]; then
    die "$LAUNCHER exists and is not a link made by this script; move it away and run this again"
fi
ln -sfn "$VENV/bin/bethmm" "$LAUNCHER"
say "command: $LAUNCHER -> $VENV/bin/bethmm"
"$LAUNCHER" --version

if [ "$GUI" -eq 1 ]; then
    step "Adding the GUI to your applications menu"
    if [ -e "$GUI_LAUNCHER" ] && [ ! -L "$GUI_LAUNCHER" ]; then
        die "$GUI_LAUNCHER exists and is not a link made by this script; move it away and run this again"
    fi
    ln -sfn "$VENV/bin/bethmm-gui" "$GUI_LAUNCHER"
    say "command: $GUI_LAUNCHER -> $VENV/bin/bethmm-gui"
    if [ -e "$DESKTOP_FILE" ] && ! our_desktop_file; then
        warn "$DESKTOP_FILE exists and was not made by this script; left as it is (no menu entry added)"
    else
        mkdir -p "$(dirname "$DESKTOP_FILE")"
        "$VENV/bin/python" -I -m bethmm.gui.desktop "$VENV/bin/bethmm-gui" "$ICON" > "$DESKTOP_FILE.tmp"
        mv -f "$DESKTOP_FILE.tmp" "$DESKTOP_FILE"
        say "menu entry: $DESKTOP_FILE"
    fi
    # Can Qt start? Offscreen first (the toolkit itself), then what its X11 plugin links to.
    if ! qt_error="$(QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -I -c 'from PySide6 import QtWidgets' 2>&1)"; then
        warn "the GUI can't load Qt: ${qt_error##*$'\n'}"
        warn "the command line works; see \"If the GUI doesn't start\" in the README"
    else
        qt_dir="$("$VENV/bin/python" -I -c 'import os, PySide6; print(os.path.join(os.path.dirname(PySide6.__file__), "Qt"))')"
        xcb_missing="$(ldd "$qt_dir/plugins/platforms/libqxcb.so" 2>/dev/null | awk '/not found/ { print $1 }' | sort -u | tr '\n' ' ')"
        if [ -n "$xcb_missing" ]; then
            warn "the GUI may not start in an X11 session: missing ${xcb_missing% }"
            warn "on Ubuntu:  sudo apt install ${GUI_LIBS[*]}"
        fi
    fi
else
    remove_gui_entries
fi

if [ "$DEV" -eq 1 ]; then
    step "Running the tests"
    (cd "$REPO_DIR" && "$VENV/bin/python" -m pytest -q)
fi

# ------------------------------------------------------------------ done

step "Done"
for note in "${NOTES[@]+"${NOTES[@]}"}"; do warn "$note"; done
case ":$PATH:" in
    *":$BIN_DIR:"*) RUN="bethmm" ;;
    *) RUN="$LAUNCHER"
       warn "$BIN_DIR is not on your PATH yet. Ubuntu adds it at login once the folder exists,"
       warn "so log out and back in (until then, use the full path below)." ;;
esac
say "Keep this folder ($REPO_DIR): the app runs from it. To update:  git pull && ./install.sh"
say ""
if [ "$GUI" -eq 1 ]; then
    say "Open \"$APP_NAME\" from your applications menu, or run:  ${RUN}-gui"
    say ""
fi
say "On the command line, try:"
say "  $RUN games"
say "  $RUN analyze ~/Downloads/<some mod>.7z"
