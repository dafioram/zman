"""Snapshots: list, view, take, hold, clone, diff, rollback, destroy."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..errors import Cancel, Gone, Leave
from ..runner import run
from ..textfmt import fmt_ago, fmt_duration, fmt_size, fmt_time, plural, strip_ctl, strip_ctl_block
from ..ui import (Col, Fetched, ListView, Msg, Opt, Sort, ask_text, choose, confirm, item_header, menu, nav,
                  page_text, paint, preview, typed_confirm, wait_enter)
from . import helptext
from .common import CANCELLED, changed, leave_if_done, quick, report


def _hold_tags(snaps: Sequence[M.Snapshot]) -> Dict[str, List[str]]:
    """Tags of held snapshots, in one zfs call."""
    held = [s.name for s in snaps if s.holds]
    out: Dict[str, List[str]] = {}
    for i in range(0, len(held), 200):
        r = run(["zfs", "holds", "-H"] + held[i:i + 200])
        for line in r.out.splitlines():
            f = line.split("\t")
            if len(f) >= 2:
                out.setdefault(strip_ctl(f[0]).strip(), []).append(strip_ctl(f[1]).strip())
    return out


def snapshots_screen(dataset: str = "") -> None:
    state: Dict[str, Dict[str, List[str]]] = {"tags": {}}

    def fetch() -> Fetched:
        snaps, err = zfs.list_snapshots(dataset=dataset)
        state["tags"] = _hold_tags(snaps)
        return Fetched(snaps, list(CONFIG.warnings) if not dataset else [], err)

    def hold_text(s: M.Snapshot) -> str:
        if not s.holds:
            return "-"
        tags = state["tags"].get(s.name)
        return ",".join(tags) if tags else str(s.holds)

    now = datetime.now().timestamp
    cols = [
        Col("NAME", (lambda s: "@" + s.short) if dataset else (lambda s: s.name), max=70, min=10, flex=True, want=28),
        Col("CREATED", lambda s: fmt_duration(now() - s.creation) + " ago" if s.creation else "-", right=True,
            keep=True),
        Col("USED", lambda s: fmt_size(s.used), right=True, min=5),
        Col("REFER", lambda s: fmt_size(s.refer), right=True, min=5, drop=2),
        Col("HOLD", hold_text, max=16, min=4, drop=1, style=lambda s: "warn" if s.holds else ""),
        Col("CLONES", lambda s: str(len(s.clones)) if s.clones else "-", right=True, drop=3),
    ]
    sorts = [
        Sort("newest first", lambda s: -s.txg),
        Sort("oldest first", lambda s: s.txg),
        Sort("used, largest first", lambda s: -(s.used or 0)),
        Sort("name", lambda s: s.name),
    ]
    title = f"Snapshots of {dataset}" if dataset else "Snapshots"
    ListView(title, fetch, cols, sorts, lambda s: f"{s.name} {s.guid} {' '.join(state['tags'].get(s.name, []))}",
             lambda s: snapshot_view(s.name), help_text=helptext.SNAPSHOTS,
             header=lambda: [paint("Used = space freed if only this one is destroyed.", "dim")],
             empty="No snapshots." + ("" if not dataset else " Take one from the dataset's view.")).run()


def snapshot_view(name: str) -> None:
    msg = Msg()
    while True:
        s = zfs.get_snapshot(name)
        if s is None:
            leave_if_done(msg)
            raise Gone("snapshot")
        ds = zfs.get_dataset(s.dataset)
        tags = zfs.holds(name) if s.holds else []
        rows: List[Tuple[str, object]] = [
            ("Created", f"{fmt_time(s.creation)} ({fmt_ago(s.creation)})"),
            ("Used", fmt_size(s.used) + "  " + paint("(freed if only this one is destroyed)", "dim")),
            ("Refers", fmt_size(s.refer) + "  " + paint("(size of the data at that moment)", "dim")),
            ("Holds", (", ".join(tags) if tags else ("none" if not s.holds else str(s.holds)),
                       "warn" if s.holds else "")),
            ("Clones", ", ".join(s.clones) if s.clones else "none"),
        ]
        is_fs = ds is not None and not ds.is_volume
        if is_fs and ds.mounted and ds.has_mount_path:
            rows.append(("Browse", f"{ds.mountpoint.rstrip('/')}/.zfs/snapshot/{s.short}"))
        blockers = M.snapshot_blockers(s)
        header = item_header(name, "", rows)
        if blockers:
            header.append(paint(f"Destroy is not offered because {' and '.join(blockers)}.", "dim"))
        opts = [
            Opt("1", "Changes since this snapshot", show=is_fs),
            Opt("2", "Hold"),
            Opt("3", "Release hold", show=s.holds > 0),
            Opt("4", "Clone to new dataset"),
            Opt("5", "Rename"),
            Opt("6", f"Dataset: {s.dataset}"),
            Opt("7", f"Clones ({len(s.clones)})", show=bool(s.clones)),
            Opt("8", "Roll back", "bad"),
            Opt("9", "Destroy", "bad", show=not blockers),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            diff_view(msg, s)
        elif key == "2":
            nav(msg, hold_flow, msg, s)
        elif key == "3":
            nav(msg, release_flow, msg, s, tags)
        elif key == "4":
            nav(msg, clone_flow, msg, s)
        elif key == "5":
            new = rename_flow(msg, s)
            if new:
                name = new
        elif key == "6":
            from .datasets import open_dataset
            nav(msg, open_dataset, s.dataset)
        elif key == "7":
            from .datasets import datasets_screen, open_dataset
            if len(s.clones) == 1:
                nav(msg, open_dataset, s.clones[0])
            else:
                nav(msg, datasets_screen, f"Clones of {name}", "", list(s.clones))
        elif key == "8":
            nav(msg, rollback_flow, msg, s, ds)
        elif key == "9":
            nav(msg, destroy_flow, msg, s)


# ---------------------------------------------------------------- take


def take_snapshot_flow(msg: Msg, ds: M.Dataset, n_children: int) -> None:
    default = M.default_snapshot_name(CONFIG.snapshot_prefix, datetime.now())
    print()
    print(f"Name: {ds.name}@{paint(default, 'bold')}")
    print(paint("(Enter keeps this name, or type your own; Ctrl-C cancels)", "dim"))
    raw = ask_text("> ").lstrip("@") or default
    err = M.check_component(raw, "snapshot name")
    if err:
        raise Leave(err, "warn")
    recursive = False
    if n_children:
        recursive = confirm(f"Include its {plural(n_children, 'child dataset')}?", False)
    args = ["zfs", "snapshot"] + (["-r"] if recursive else []) + [f"{ds.name}@{raw}"]
    quick(msg, args, f"Done. Took {ds.name}@{raw}" + (" and its children." if recursive else "."))


# ---------------------------------------------------------------- small actions


def diff_view(msg: Msg, s: M.Snapshot) -> None:
    print(paint(f"Comparing {s.name} with now (can take a while)...", "dim"))
    r = run(["zfs", "diff", "-F", s.name], timeout=CONFIG.long_timeout)
    if not r.ok:
        report(msg, r)
        return
    body = strip_ctl_block(r.out)
    if not body.strip():
        msg.ok(f"No changes since {s.name}.")
        return
    legend = ("Changes since the snapshot. First column: + added, - removed, M modified, R renamed.\n"
              "Second column: F file, / directory, @ symlink, others are special files.\n\n")
    page_text(legend + body, f"Changes since {s.name}")


def hold_flow(msg: Msg, s: M.Snapshot) -> None:
    print()
    print(paint("A hold stops the snapshot from being destroyed until it is released.", "dim"))
    tag = ask_text(f"Hold tag (Enter keeps '{CONFIG.hold_tag}'): ") or CONFIG.hold_tag
    err = M.check_component(tag, "tag")
    if err:
        raise Leave(err, "warn")
    quick(msg, ["zfs", "hold", tag, s.name], f"Done. {s.name} is held ({tag}).")


def release_flow(msg: Msg, s: M.Snapshot, tags: Sequence[str]) -> None:
    if not tags:
        raise Leave("No holds to release.", "warn")
    if len(tags) == 1:
        tag = tags[0]
    else:
        i = choose(f"Release which hold on {s.name}?", list(tags))
        if i is None:
            raise Cancel()
        tag = tags[i]
    args = ["zfs", "release", tag, s.name]
    left = len(tags) - 1
    note = "It can then be destroyed." if left == 0 else f"{plural(left, 'other hold')} stay."
    if not confirm(f"Release hold '{tag}' on {s.name}? {note}", True):
        raise Cancel()
    quick(msg, args)


def clone_flow(msg: Msg, s: M.Snapshot) -> None:
    pool = s.pool
    rel = s.dataset[len(pool) + 1:] if "/" in s.dataset else ""
    suggestion = f"{rel + '-' if rel else ''}{s.short}".replace("/", "-")
    print()
    print(paint("The clone is a new, writable dataset that starts with the snapshot's data.", "dim"))
    print(paint(f"(Enter keeps {pool}/{suggestion}; Ctrl-C cancels)", "dim"))
    raw = ask_text(f"New dataset name: {pool}/") or suggestion
    err = M.check_child_path(raw)
    if err:
        raise Leave(err, "warn")
    new = f"{pool}/{raw}"
    if zfs.get_dataset(new) is not None:
        raise Leave(f"{new} already exists.", "warn")
    args = ["zfs", "clone", s.name, new]
    preview(f"Clone {s.name}", [
        f"Creates {new} from the snapshot. It shares data with the snapshot, so it is small at first.",
        ("While the clone exists, the snapshot cannot be destroyed.", "dim"),
    ], [args])
    if not confirm(f"Create {new}?", True):
        raise Cancel()
    if quick(msg, args):
        from .datasets import open_dataset
        nav(msg, open_dataset, new)


def rename_flow(msg: Msg, s: M.Snapshot) -> str:
    try:
        print()
        raw = ask_text(f"New name: {s.dataset}@").lstrip("@")
        if not raw or raw == s.short:
            raise Cancel()
        err = M.check_component(raw, "snapshot name")
        if err:
            msg.warn(err)
            return ""
        new = f"{s.dataset}@{raw}"
        if quick(msg, ["zfs", "rename", s.name, new]):
            return new
        return ""
    except Cancel:
        msg.warn(CANCELLED)
        return ""


# ---------------------------------------------------------------- rollback and destroy


def rollback_flow(msg: Msg, s: M.Snapshot, ds: Optional[M.Dataset]) -> None:
    snaps, _ = zfs.list_snapshots(dataset=s.dataset)
    target = next((x for x in snaps if x.name == s.name), None)
    if target is None:
        raise Gone("snapshot")
    newer, blockers = M.rollback_plan(target, snaps)
    if blockers:
        preview(f"Roll back {s.dataset}", [("zman will not roll back, because newer snapshots would be destroyed:", "bad")]
                + [f"  {b}" for b in blockers[:12]]
                + ["", ("Release those holds or remove those clones first, or copy files out of", "dim"),
                   ("the snapshot instead.", "dim")])
        wait_enter()
        raise Cancel()
    args = ["zfs", "rollback"] + (["-r"] if newer else []) + [s.name]
    lines: List[object] = [(f"Discards every change made to {s.dataset} since {fmt_time(s.creation)}.", "bad")]
    if newer:
        lines.append((f"Also destroys {plural(len(newer), 'newer snapshot')}:", "bad"))
        for x in newer[:10]:
            lines.append(f"  @{x.short}  {fmt_size(x.used)}  {fmt_ago(x.creation)}")
        if len(newer) > 10:
            lines.append(f"  ... and {len(newer) - 10} more")
        lines.append(("A snapshot you take now would be newer too, so the rollback would destroy it.", "dim"))
        lines.append(("To keep the current state as well, use Clone to new dataset instead.", "dim"))
    if ds is not None and ds.is_volume:
        lines.append(("Stop whatever uses the volume (VM, iSCSI) before rolling back.", "warn"))
    elif ds is not None and ds.mounted:
        lines.append((f"Programs with files open in {ds.mountpoint} may misbehave; close them first.", "dim"))
    lines.append(("This cannot be undone.", "bad"))
    leaf = s.dataset.rsplit("/", 1)[-1]
    preview(f"Roll back {s.dataset} to @{s.short}", lines, [args])
    if not typed_confirm(leaf, "roll back"):
        raise Cancel()
    snaps2, _ = zfs.list_snapshots(dataset=s.dataset)
    t2 = next((x for x in snaps2 if x.name == s.name), None)
    if t2 is None:
        changed(msg, s.name, "rolled back")
        return
    newer2, blockers2 = M.rollback_plan(t2, snaps2)
    if blockers2 or [x.fingerprint() for x in newer2] != [x.fingerprint() for x in newer]:
        changed(msg, s.dataset, "rolled back")
        return
    report(msg, run(args, timeout=CONFIG.long_timeout), f"Done. {s.dataset} is back at @{s.short}.")


def destroy_flow(msg: Msg, s: M.Snapshot) -> None:
    names, reclaim, err = zfs.destroy_preview(s.name)
    args = ["zfs", "destroy", s.name]
    lines: List[object] = [f"Destroys snapshot {s.name} ({fmt_ago(s.creation)}).",
                           f"Frees about {fmt_size(reclaim if reclaim is not None else s.used)}.",
                           ("The dataset's current files are not touched.", "dim")]
    if err:
        lines.append((f"ZFS said: {err}", "warn"))
    preview(f"Destroy {s.name}", lines, [args])
    if not confirm(f"Destroy {s.name}?", False):
        raise Cancel()
    cur = zfs.get_snapshot(s.name)
    if cur is None or cur.fingerprint() != s.fingerprint():
        changed(msg, s.name, "removed")
        return
    report(msg, run(args), f"Removed snapshot {s.name}")
