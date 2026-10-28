"""A pool's devices: layout list, device view, replace, attach, detach, offline."""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..errors import Cancel, Gone, Leave
from ..runner import run
from ..textfmt import fmt_size, plural
from ..ui import (Col, Fetched, ListView, Msg, Opt, Sort, choose, confirm, item_header, menu, nav,
                  preview, typed_confirm)
from . import helptext
from .common import changed, leave_if_done, quick, report
from .diskops import disk_line, free_disks, partition_disks, partition_steps, size_need, still_free


def _key(v: M.Vdev) -> str:
    """What to call a device in zpool commands: its guid when known, else its path or name."""
    return v.guid or v.path or v.name


def _row_name(v: M.Vdev) -> str:
    depth = max(0, v.depth - 1) + (1 if v.section != "data" and v.kind != "section" else 0)
    return "  " * depth + v.label


def devices_screen(pool: str) -> None:
    state: Dict[str, object] = {}

    def fetch() -> Fetched:
        p = zfs.get_pool(pool)
        if p is None or p.status is None:
            raise Gone("pool")
        disks, err = zfs.list_disks()
        state["disks"] = disks
        rows = [v for v in p.status.vdevs if v.kind != "pool"]
        return Fetched(rows, [], err)

    def disk_of(v: M.Vdev) -> Tuple[Optional[M.Disk], Optional[M.Part]]:
        if not v.path:
            return None, None
        return zfs.disk_for_path(v.path, state.get("disks", []))  # type: ignore[arg-type]

    def size_of(v: M.Vdev) -> str:
        d, p = disk_of(v)
        if p:
            return fmt_size(p.size)
        return fmt_size(d.size) if d else "-"

    cols = [
        Col("DEVICE", _row_name, max=60, min=10, flex=True, want=28, middle=True),
        Col("STATE", lambda v: M.vdev_state_text(v)[0], keep=True, style=lambda v: M.vdev_state_text(v)[1]),
        Col("SIZE", size_of, right=True, min=5, drop=2),
        Col("SERIAL", lambda v: (disk_of(v)[0].serial if disk_of(v)[0] else ""), max=24, min=6, drop=1),
        Col("TYPE", lambda v: (disk_of(v)[0].media if disk_of(v)[0] else ""), drop=3),
    ]
    sorts = [
        Sort("layout", lambda v: v.idx),
        Sort("problems first", lambda v: (0 if M.vdev_state_text(v)[1] == "bad" else 1, v.idx)),
    ]
    ListView(f"Devices of {pool}", fetch, cols, sorts, lambda v: f"{v.name} {v.path} {v.guid} {v.state}",
             lambda v: device_view(pool, _key(v)), help_text=helptext.DEVICES,
             empty="No devices.").run()


def role_text(st: M.PoolStatus, v: M.Vdev) -> str:
    if v.kind == "section":
        return f"section for {v.name}"
    if v.parent < 0:
        return "-"
    parent = st.vdevs[v.parent]
    if v.section == "spares":
        return "hot spare"
    if v.section in ("cache", "logs"):
        return {"cache": "read cache (L2ARC)", "logs": "write log (SLOG)"}[v.section]
    if parent.kind == "pool":
        return "single-disk vdev, no redundancy" if v.is_leaf else "top-level vdev"
    if parent.kind == "replacing":
        return f"being replaced inside {parent.name}"
    if parent.kind == "spare":
        return f"spare in use inside {parent.name}"
    n = len(st.children(parent.idx))
    return f"member of {parent.name} ({plural(n, 'device')})"


def device_view(pool: str, key: str) -> None:
    msg = Msg()
    while True:
        p = zfs.get_pool(pool)
        if p is None or p.status is None:
            raise Gone("pool")
        st = p.status
        v = st.find(key)
        if v is None:
            leave_if_done(msg)
            raise Gone("device")
        disks, _ = zfs.list_disks()
        d, part = zfs.disk_for_path(v.path, disks) if v.path else (None, None)
        word, style = M.vdev_state_text(v)
        rows: List[Tuple[str, object]] = [("Pool", (f"{pool} ({st.state})", M.state_style(st.state))),
                                          ("Role", role_text(st, v)), ("State", (word, style))]
        if v.kind not in ("section",):
            rows.append(("Errors", f"read {v.read}, write {v.write}, checksum {v.cksum}"))
        if v.note:
            rows.append(("Note", (v.note, "dim")))
        if v.is_leaf:
            if d:
                bits = [d.name, fmt_size(part.size if part else d.size), d.media]
                if d.model:
                    bits.append(d.model)
                if d.serial:
                    bits.append(f"serial {d.serial}")
                rows.append(("Disk", " · ".join(bits)))
            else:
                rows.append(("Disk", ("not found on this machine", "warn")))
            rows.append(("Path", v.path or v.name))
        else:
            kids = st.children(v.idx)
            rows.append(("Contains", ", ".join(f"{k.label} ({k.state})" for k in kids) or "-"))
        header = item_header(v.label, f"guid {v.guid}" if v.guid else "", rows)

        parent = st.vdevs[v.parent] if v.parent >= 0 else None
        siblings = st.children(parent.idx) if parent else []
        in_data = v.section in ("data", "special", "dedup")
        missing = v.state in ("UNAVAIL", "REMOVED")
        redundant_parent = parent is not None and parent.kind in ("mirror", "raidz", "draid", "replacing", "spare")
        others_ok = any(s.idx != v.idx and s.state == "ONLINE" for s in siblings)
        leaf = v.is_leaf
        replace_in_place = leaf and in_data and missing and bool(v.path) and os.path.exists(v.path)
        opts = [
            Opt("1", "Replace with another disk", show=leaf and (in_data or v.section == "logs")
                and parent is not None and parent.kind not in ("replacing",)),
            Opt("2", "Replace in place (new disk at the same path)", show=replace_in_place),
            Opt("3", "Offline", "warn", show=leaf and in_data and v.state in ("ONLINE", "DEGRADED", "FAULTED")
                and redundant_parent and others_ok),
            Opt("4", "Online", show=leaf and v.state == "OFFLINE"),
            Opt("5", "Clear errors", show=leaf and (v.errors > 0 or v.state == "FAULTED")),
            Opt("6", "Attach a mirror disk", show=leaf and v.section == "data" and v.state == "ONLINE"
                and parent is not None and parent.kind in ("pool", "mirror")),
            Opt("7", "Detach", "bad", show=leaf and parent is not None and (
                (parent.kind == "mirror" and len(siblings) > 1 and others_ok)
                or parent.kind in ("replacing", "spare"))),
            Opt("8", "Remove from pool", "bad", show=leaf and (
                (v.section == "spares" and v.state == "AVAIL") or v.section in ("cache", "logs"))),
            Opt("9", f"Disk: {d.name}" if d else "Disk", show=d is not None),
            Opt("10", f"Pool: {pool}"),
        ]
        key_in = menu(header, opts, msg)
        if key_in == "1":
            nav(msg, replace_flow, msg, pool, key)
        elif key_in == "2":
            nav(msg, replace_in_place_flow, msg, pool, key)
        elif key_in == "3":
            nav(msg, offline_flow, msg, pool, key)
        elif key_in == "4":
            nav(msg, simple_device_flow, msg, pool, key, "online")
        elif key_in == "5":
            from .pools import clear_flow
            nav(msg, clear_flow, msg, pool, _key(v))
        elif key_in == "6":
            nav(msg, attach_flow, msg, pool, key)
        elif key_in == "7":
            nav(msg, detach_flow, msg, pool, key)
        elif key_in == "8":
            nav(msg, remove_flow, msg, pool, key)
        elif key_in == "9" and d is not None:
            from .disks import disk_view
            nav(msg, disk_view, d.name)
        elif key_in == "10":
            from .pools import pool_view
            nav(msg, pool_view, pool)


def _load(pool: str, key: str) -> Tuple[M.Pool, M.PoolStatus, M.Vdev]:
    p = zfs.get_pool(pool)
    if p is None or p.status is None:
        raise Gone("pool")
    v = p.status.find(key)
    if v is None:
        raise Gone("device")
    return p, p.status, v


def _same(pool: str, key: str, v: M.Vdev) -> bool:
    p = zfs.get_pool(pool)
    if p is None or p.status is None:
        return False
    cur = p.status.find(key)
    return cur is not None and cur.state == v.state and cur.path == v.path


def _pick_new_disk(title: str, need: int, part_size: Optional[int], explain: Sequence[str]) -> Tuple[M.Disk, M.DiskUse]:
    disks, uses, _, _ = zfs.disk_inventory()
    free = free_disks(disks, uses)
    big = [d for d in free if d.size >= need]
    small = [d for d in free if d.size < need]
    if not big:
        if small:
            raise Leave(f"No free disk is large enough; it needs at least {fmt_size(need)}.", "warn")
        raise Leave("No free disks. Connect the new disk, then try again.", "warn")
    ex = list(explain) + [f"Needs at least {fmt_size(need)}." if need else ""]
    if small:
        ex.append(f"Not shown: {', '.join(d.name for d in small)} (too small).")
    if part_size:
        ex.append(f"This pool uses partitions of {fmt_size(part_size)}; the new disk gets the same.")
    i = choose(title, [disk_line(d, uses[d.name]) for d in big], explain=[e for e in ex if e])
    if i is None:
        raise Cancel()
    return big[i], uses[big[i].name]


def _prepare_and_run(msg: Msg, d: M.Disk, part_size: Optional[int], label: str,
                     make_args, done: str) -> bool:
    """Partition the new disk if the pool uses partitions, then run the zpool command."""
    if part_size:
        paths, err = partition_disks([d], part_size, label)
        if err:
            msg.bad(f"Failed: {err}")
            return False
        dev = paths[0]
    else:
        dev = d.dev_path
    r = run(make_args(dev), timeout=CONFIG.long_timeout)
    return report(msg, r, done)


def replace_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    disks, _ = zfs.list_disks()
    need, part_size = size_need(st, v, disks)
    new, use = _pick_new_disk(f"Replace {v.label} in {pool}", need, part_size,
                              ["Pick the new disk. Everything on it will be erased."])
    force = bool(use.warn) or bool(part_size)

    def make(dev: str) -> List[str]:
        return ["zpool", "replace"] + (["-f"] if force else []) + [pool, _key(v), dev]

    shown_dev = (new.by_id + "-part1" if new.by_id else new.path + "1") if part_size else new.dev_path
    cmds = [a for _, a in partition_steps([new], part_size or 0, f"zfs-{pool}")] if part_size else []
    lines: List[object] = [
        f"Old: {v.label} ({v.state})",
        f"New: {new.name}  {fmt_size(new.size)}  {new.describe() or new.media}",
        "",
        "ZFS copies (resilvers) the data onto the new disk. The pool stays usable;",
        "it may be slower until the resilver finishes. The old disk is dropped after that.",
        (f"Everything on {new.name} is erased.", "bad"),
    ]
    if use.warn:
        lines.append((f"{new.name} {use.warn}.", "warn"))
    preview(f"Replace a device in {pool}", lines, cmds + [make(shown_dev)])
    if not typed_confirm(new.name, "erase it and use it as the replacement"):
        raise Cancel()
    if not _same(pool, key, v) or not still_free([new]):
        changed(msg, f"{v.label} or {new.name}", "replaced")
        return
    if _prepare_and_run(msg, new, part_size, f"zfs-{pool}", make, "Resilver started."):
        from .pools import scan_view
        nav(msg, scan_view, pool)


def replace_in_place_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    disks, uses, _, _ = zfs.disk_inventory()
    d, _part = zfs.disk_for_path(v.path, disks)
    if d is None:
        msg.warn(f"Nothing is at {v.path} right now.")
        return
    args = ["zpool", "replace", pool, v.path]
    preview(f"Replace {v.label} in place", [
        f"The disk now at {v.path} is {d.name} ({fmt_size(d.size)}, {d.describe() or d.media}).",
        "ZFS treats it as a new disk and resilvers the data onto it.",
        (f"Everything on {d.name} is erased.", "bad"),
    ], [args])
    if not typed_confirm(d.name, "erase it and resilver onto it"):
        raise Cancel()
    if not _same(pool, key, v):
        changed(msg, v.label, "replaced")
        return
    if report(msg, run(args, timeout=CONFIG.long_timeout), "Resilver started."):
        from .pools import scan_view
        nav(msg, scan_view, pool)


def offline_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    args = ["zpool", "offline", pool, _key(v)]
    preview(f"Take {v.label} offline", [
        f"ZFS stops using {v.label}. The pool keeps working on the other devices,",
        "without that redundancy, until you bring it online again or replace it.",
        ("Use this before pulling a failing disk out of the machine.", "dim"),
    ], [args])
    if not confirm(f"Take {v.label} offline?", False):
        raise Cancel()
    if not _same(pool, key, v):
        changed(msg, v.label, "taken offline")
        return
    quick(msg, args)


def simple_device_flow(msg: Msg, pool: str, key: str, action: str) -> None:
    p, st, v = _load(pool, key)
    args = ["zpool", action, pool, _key(v)]
    preview(f"Bring {v.label} online", [
        "ZFS starts using the device again and resilvers whatever changed while it was away.",
    ], [args])
    if not confirm(f"Bring {v.label} online?", True):
        raise Cancel()
    if not _same(pool, key, v):
        changed(msg, v.label, "changed")
        return
    quick(msg, args)


def attach_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    disks, _ = zfs.list_disks()
    need, part_size = size_need(st, v, disks)
    new, use = _pick_new_disk(f"Attach a mirror disk to {v.label}", need, part_size,
                              ["The new disk becomes a mirror of this one. Everything on it will be erased."])
    force = bool(use.warn) or bool(part_size)

    def make(dev: str) -> List[str]:
        return ["zpool", "attach"] + (["-f"] if force else []) + [pool, _key(v), dev]

    parent = st.vdevs[v.parent]
    after = "a 2-way mirror" if parent.kind == "pool" else f"a {len(st.children(parent.idx)) + 1}-way mirror"
    shown_dev = (new.by_id + "-part1" if new.by_id else new.path + "1") if part_size else new.dev_path
    cmds = [a for _, a in partition_steps([new], part_size or 0, f"zfs-{pool}")] if part_size else []
    lines: List[object] = [
        f"Adds {new.name} ({fmt_size(new.size)}, {new.describe() or new.media}) as a mirror of {v.label}.",
        f"That part of the pool becomes {after}. ZFS copies (resilvers) the data onto it.",
        (f"Everything on {new.name} is erased.", "bad"),
    ]
    if use.warn:
        lines.append((f"{new.name} {use.warn}.", "warn"))
    preview(f"Attach a mirror disk in {pool}", lines, cmds + [make(shown_dev)])
    if not typed_confirm(new.name, "erase it and attach it"):
        raise Cancel()
    if not _same(pool, key, v) or not still_free([new]):
        changed(msg, f"{v.label} or {new.name}", "attached")
        return
    if _prepare_and_run(msg, new, part_size, f"zfs-{pool}", make, "Resilver started."):
        from .pools import scan_view
        nav(msg, scan_view, pool)


def detach_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    parent = st.vdevs[v.parent]
    left = [s for s in st.children(parent.idx) if s.idx != v.idx]
    args = ["zpool", "detach", pool, _key(v)]
    if parent.kind == "replacing":
        lines: List[object] = [f"Cancels the replacement in {parent.name} by dropping {v.label}."]
    elif parent.kind == "spare":
        lines = [f"Drops {v.label} from {parent.name}. If it is the spare, it goes back to the spares list;",
                 "if it is the failed disk, the spare takes its place for good."]
    else:
        lines = [f"Removes {v.label} from {parent.name}; {plural(len(left), 'device')} stay in it.",
                 "The disk's data is no longer part of the pool."]
        if len(left) == 1:
            lines.append((f"{parent.name} then has no redundancy: one more failure there loses data.", "warn"))
        if any(s.state != "ONLINE" for s in left):
            lines.append(("Some of the remaining devices are not ONLINE.", "bad"))
    preview(f"Detach {v.label}", lines, [args])
    if not confirm(f"Detach {v.label} from {parent.name}?", False):
        raise Cancel()
    if not _same(pool, key, v):
        changed(msg, v.label, "detached")
        return
    report(msg, run(args), f"Done. Detached {v.label}.")


def remove_flow(msg: Msg, pool: str, key: str) -> None:
    p, st, v = _load(pool, key)
    args = ["zpool", "remove", pool, _key(v)]
    what = {"spares": "spare", "cache": "cache device", "logs": "log device"}.get(v.section, "device")
    preview(f"Remove {v.label}", [f"Removes the {what} {v.label} from {pool}. Pool data is not affected."], [args])
    if not confirm(f"Remove {v.label} from {pool}?", False):
        raise Cancel()
    if not _same(pool, key, v):
        changed(msg, v.label, "removed")
        return
    report(msg, run(args), f"Removed {what} {v.label}")
