"""Picking free disks and preparing them (shared by pool creation, replace, attach, spares)."""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .. import model as M
from ..runner import TARGET, run
from ..textfmt import fmt_size
from ..ui import paint


def disk_line(d: M.Disk, use: M.DiskUse) -> str:
    bits = [d.name.ljust(8), fmt_size(d.size).rjust(7), d.media.ljust(4)]
    desc = d.describe()
    if desc:
        bits.append(desc)
    line = "  ".join(bits)
    if use.warn:
        line += f"  [{use.warn}]"
    return line


def free_disks(disks: Sequence[M.Disk], uses: Dict[str, M.DiskUse], min_size: int = 0,
               exclude: Sequence[str] = ()) -> List[M.Disk]:
    out = [d for d in disks if uses.get(d.name) and uses[d.name].free and d.name not in exclude
           and d.size >= min_size]
    return sorted(out, key=lambda d: (-d.size, d.name))


def partition_path(d: M.Disk, n: int = 1) -> str:
    if d.by_id:
        return f"{d.by_id}-part{n}"
    sep = "p" if re.search(r"\d$", d.path) else ""
    return f"{d.path}{sep}{n}"


def disk_fingerprint(d: M.Disk) -> Tuple[str, int, str, str]:
    return (d.name, d.size, d.serial, d.by_id)


def still_free(chosen: Sequence[M.Disk]) -> bool:
    """Re-check right before erasing: same disks, same identity, still free."""
    from ..zfs import disk_inventory
    disks, uses, _, _ = disk_inventory()
    now = {d.name: d for d in disks}
    for d in chosen:
        cur = now.get(d.name)
        if cur is None or disk_fingerprint(cur) != disk_fingerprint(d) or not uses[cur.name].free:
            return False
    return True


def partition_steps(chosen: Sequence[M.Disk], size: int, label: str) -> List[Tuple[str, List[str]]]:
    """The sfdisk commands (for showing); the script itself goes on stdin."""
    return [(f"partition {d.name}", ["sfdisk", "--wipe", "always", "--wipe-partitions", "always", d.dev_path])
            for d in chosen]


def wait_for(path: str, seconds: float = 15) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.25)
    return os.path.exists(path)


def partition_disks(chosen: Sequence[M.Disk], size: int, label: str) -> Tuple[List[str], str]:
    """Give each disk one ZFS partition of `size` bytes. Returns (partition paths, error)."""
    if "sfdisk" not in TARGET.bins:
        return [], "sfdisk is not installed. Hint: apt install fdisk"
    script = M.sfdisk_script(size, label)
    paths = []
    for d in chosen:
        print(paint(f"Partitioning {d.name} ({d.dev_path})...", "dim"))
        r = run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", d.dev_path], input=script)
        if not r.ok:
            return paths, f"sfdisk on {d.name}: {r.reason}"
        paths.append(partition_path(d))
    if "udevadm" in TARGET.bins:
        run(["udevadm", "settle"])
    for p in paths:
        if not wait_for(p):
            return paths, f"{p} did not appear after partitioning."
    return paths, ""


def size_need(st: M.PoolStatus, v: M.Vdev, disks: Sequence[M.Disk]) -> Tuple[int, Optional[int]]:
    """For replacing or mirroring device `v`: (minimum disk size, partition size or None).

    Partition size is set when the pool's devices are single partitions zman made with
    'leave headroom' (no ZFS whole-disk layout); new disks then get the same partition."""
    from ..zfs import disk_for_path
    refs = [v]
    if v.parent >= 0:
        refs += [s for s in st.children(v.parent) if s.idx != v.idx]
    min_disk = 0
    part_size: Optional[int] = None
    for ref in refs:
        if not ref.path:
            continue
        d, p = disk_for_path(ref.path, disks)
        if d is None:
            continue
        if p is not None and not M.whole_disk_layout(d):
            part_size = p.size if part_size is None else min(part_size, p.size)
            min_disk = max(min_disk, p.size + 4 * M.MIB)
        else:
            size = p.size if p is not None else d.size
            min_disk = size if not min_disk else min(min_disk, size)
        if ref is v:
            break
    return min_disk, part_size
