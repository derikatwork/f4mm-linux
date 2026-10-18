"""How deploy operations and their results are put into words (shared by the
command line and the GUI)."""

from __future__ import annotations

from bethmm import deploy

#: op kind -> (done, would, noun) for summaries like "linked 3 files".
VERBS = {
    "link": ("linked", "would link", "file"),
    "unlink": ("removed", "would remove", "link"),
    "backup": ("backed up", "would back up", "original game file"),
    "restore": ("restored", "would restore", "original game file"),
    "dropbackup": ("dropped", "would drop", "spare backup copy"),
    "mkdir": ("created", "would create", "folder"),
    "rmdir": ("removed", "would remove", "folder"),
}


def describe_op(op: deploy.Op, names: dict[int, str]) -> str:
    """One line for an operation; *names* maps mod ids to names."""
    if op.kind == "link":
        return f"link     {op.path}  ->  {names.get(op.mod_id or 0, '?')}"
    if op.kind == "backup":
        return f"backup   {op.path}  (original moved to {op.source})"
    if op.kind == "restore":
        return f"restore  {op.path}  (from {op.source})"
    if op.kind == "forget":
        return f"forget   {op.path}  ({op.note})"
    if op.kind == "dropbackup":
        return f"drop     {op.source}  (spare copy: the original {op.path} is back in place)"
    if op.kind == "reclaim":
        return f"reclaim  {op.path}  (its original, from {op.source})"
    if op.kind == "rerecord":
        return f"record   {op.path}  (the app's link, {op.note}: its folder was renamed)"
    if op.kind in ("unlink", "rmdir") and op.note:
        return f"{op.kind:<8} {op.path}  ({op.note})"
    return f"{op.kind:<8} {op.path}"


def summary(ops: list[deploy.Op], *, dry_run: bool = False) -> str:
    """'linked 2 files, backed up 1 original game file' (empty when nothing changes)."""
    counts: dict[str, int] = {}
    for op in ops:
        counts[op.kind] = counts.get(op.kind, 0) + 1
    parts = []
    for kind in ("link", "unlink", "backup", "restore", "dropbackup", "mkdir", "rmdir"):
        if counts.get(kind):
            done, would, noun = VERBS[kind]
            n = counts[kind]
            parts.append(f"{would if dry_run else done} {n} {noun}{'s' if n != 1 else ''}")
    return ", ".join(parts)


def changes(result: deploy.SyncResult) -> list[deploy.Op]:
    """The operations of *result* that change something on disk."""
    return [op for op in result.done if op.kind != "forget"]
