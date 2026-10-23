"""How deploy operations and their results are put into words (shared by the
command line and the GUI)."""

from __future__ import annotations

from bethmm import deploy, plugins

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


def _names(names: list[str], limit: int = 6) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def plugins_lines(change: plugins.PluginsChange | None) -> list[str]:
    """What happened to plugins.txt, one line per kind of change (empty: nothing)."""
    if change is None or not change.changed:
        return []
    would = change.dry_run
    out = []
    for done, maybe, names in (("added", "would add", change.added), ("put back", "would put back", change.restored),
                               ("removed", "would remove", change.removed),
                               ("switched", "would switch", change.switched),
                               ("moved", "would move", change.moved)):
        if names:
            out.append(f"plugins.txt: {maybe if would else done} {_names(names)}")
    if change.dated:
        out.append(f"{'would set' if would else 'set'} the file date of {_names(change.dated)} "
                   "(this game's load order goes by file dates)")
    if change.backup is not None:
        out.append(f"(the previous plugins.txt is kept as {change.backup})")
    return out
