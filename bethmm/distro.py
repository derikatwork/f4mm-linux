"""Which family of Linux distribution this is, so the app can name the right
commands when something is missing (``apt`` on Ubuntu, ``dnf`` on Fedora)."""

from __future__ import annotations

import functools
from pathlib import Path

OS_RELEASE = Path("/etc/os-release")


@functools.cache
def family() -> str:
    """"fedora" (Fedora and distributions based on it), "debian" (Ubuntu,
    Linux Mint, Pop!_OS, Debian ...), or "" for anything else."""
    try:
        text = OS_RELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    fields = {}
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq:
            fields[key.strip()] = value.strip().strip("\"'")
    ids = {fields.get("ID", "").lower(), *fields.get("ID_LIKE", "").lower().split()}
    if "fedora" in ids:
        return "fedora"
    if ids & {"debian", "ubuntu"}:
        return "debian"
    return ""
