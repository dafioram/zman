"""Reading ZFS and disk state. Every function takes the runner as a parameter so
tests can pass a fake one."""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import model as M
from .runner import TARGET, Result, run

Runner = Callable[..., Result]


def pool_statuses(runner: Runner = run, name: str = "", guids: bool = False) -> Tuple[Dict[str, M.PoolStatus], str]:
    args = ["zpool", "status", "-P", "-p"] + ([name] if name else [])
    r = runner(args)
    statuses = M.parse_status(r.out)
    err = "" if r.ok else r.reason
    if guids and statuses:
        g = runner(["zpool", "status", "-g", "-p"] + ([name] if name else []))
        if g.ok:
            M.attach_guids(statuses, M.parse_status(g.out))
    return statuses, err


def list_pools(runner: Runner = run) -> Tuple[List[M.Pool], str]:
    r = runner(["zpool", "list", "-H", "-p", "-o", ",".join(M.POOL_LIST_PROPS)])
    if not r.ok:
        return [], r.reason
    pools = M.parse_pool_list(r.out)
    err = ""
    if pools:
        statuses, err = pool_statuses(runner)
        for p in pools:
            p.status = statuses.get(p.name)
    return pools, err


def get_pool(name: str, runner: Runner = run, guids: bool = True) -> Optional[M.Pool]:
    r = runner(["zpool", "list", "-H", "-p", "-o", ",".join(M.POOL_LIST_PROPS), name])
    if not r.ok:
        return None
    pools = M.parse_pool_list(r.out)
    if not pools:
        return None
    statuses, _ = pool_statuses(runner, name, guids=guids)
    pools[0].status = statuses.get(name)
    return pools[0]


def pool_props(name: str, runner: Runner = run) -> Dict[str, Tuple[str, str]]:
    r = runner(["zpool", "get", "-H", "-p", "-o", "property,value,source", "all", name])
    return M.parse_props(r.out) if r.ok else {}


def list_datasets(runner: Runner = run, types: str = "filesystem,volume", root: str = "",
                  depth: Optional[int] = None) -> Tuple[List[M.Dataset], str]:
    args = ["zfs", "list", "-H", "-p", "-t", types, "-o", ",".join(M.DATASET_PROPS)]
    if root:
        args += ["-r"] + (["-d", str(depth)] if depth is not None else []) + [root]
    r = runner(args)
    if not r.ok:
        if root and "does not exist" in (r.err + r.out):
            return [], ""
        return M.parse_datasets(r.out), r.reason
    return M.parse_datasets(r.out), ""


def get_dataset(name: str, runner: Runner = run) -> Optional[M.Dataset]:
    r = runner(["zfs", "list", "-H", "-p", "-o", ",".join(M.DATASET_PROPS), name])
    items = M.parse_datasets(r.out) if r.ok else []
    return items[0] if items else None


def dataset_props(name: str, props: Sequence[str], runner: Runner = run) -> Dict[str, Tuple[str, str]]:
    r = runner(["zfs", "get", "-H", "-p", "-o", "property,value,source", ",".join(props), name])
    return M.parse_props(r.out) if r.ok else {}


def list_snapshots(runner: Runner = run, dataset: str = "", recursive: bool = False) -> Tuple[List[M.Snapshot], str]:
    args = ["zfs", "list", "-H", "-p", "-t", "snapshot", "-o", ",".join(M.SNAPSHOT_PROPS), "-s", "createtxg"]
    if dataset:
        args += (["-r"] if recursive else ["-d", "1"]) + [dataset]
    r = runner(args, timeout=max(60, _long()))
    if not r.ok:
        if dataset and "does not exist" in (r.err + r.out):
            return [], ""
        return M.parse_snapshots(r.out), r.reason
    return M.parse_snapshots(r.out), ""


def get_snapshot(name: str, runner: Runner = run) -> Optional[M.Snapshot]:
    r = runner(["zfs", "list", "-H", "-p", "-t", "snapshot", "-o", ",".join(M.SNAPSHOT_PROPS), name])
    items = M.parse_snapshots(r.out) if r.ok else []
    return items[0] if items else None


def holds(name: str, runner: Runner = run) -> List[str]:
    r = runner(["zfs", "holds", "-H", name])
    return M.parse_holds(r.out) if r.ok else []


def destroy_preview(target: str, recursive: bool = False, runner: Runner = run) -> Tuple[List[str], Optional[int], str]:
    args = ["zfs", "destroy", "-n", "-v", "-p"] + (["-r"] if recursive else []) + [target]
    r = runner(args)
    names, reclaim = M.parse_destroy_preview(r.out)
    return names, reclaim, ("" if r.ok else r.reason)


LSBLK_COLS = "NAME,KNAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,FSTYPE,LABEL,MOUNTPOINT"


def kname_of(path: str) -> str:
    """Kernel name (sdb1) behind a device path or by-id link; '' when it is not there."""
    try:
        if not os.path.exists(path):
            return ""
        return os.path.basename(os.path.realpath(path))
    except OSError:
        return ""


def by_id_links(directory: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return links
    for n in names:
        k = kname_of(os.path.join(directory, n))
        if k:
            links[n] = k
    return links


def list_disks(runner: Runner = run) -> Tuple[List[M.Disk], str]:
    r = runner(["lsblk", "-J", "-b", "-o", LSBLK_COLS])
    if not r.ok:
        return [], r.reason
    disks, err = M.parse_lsblk(r.out)
    best = M.pick_by_id(by_id_links(TARGET.by_id_dir))
    for d in disks:
        if d.name in best:
            d.by_id = os.path.join(TARGET.by_id_dir, best[d.name])
        for p in d.parts:
            if p.name in best:
                p.by_id = os.path.join(TARGET.by_id_dir, best[p.name])
    return disks, err


def disk_inventory(runner: Runner = run) -> Tuple[List[M.Disk], Dict[str, M.DiskUse], Dict[str, M.PoolStatus], str]:
    disks, err = list_disks(runner)
    statuses, serr = pool_statuses(runner)
    members = M.pool_members(statuses, kname_of)
    uses = {d.name: M.disk_use(d, members) for d in disks}
    return disks, uses, statuses, err or serr


def disk_for_path(path: str, disks: Sequence[M.Disk]) -> Tuple[Optional[M.Disk], Optional[M.Part]]:
    k = kname_of(path)
    for d in disks:
        if d.name == k:
            return d, None
        for p in d.parts:
            if p.name == k:
                return d, p
    return None, None


def import_candidates(runner: Runner = run, search_dir: str = "") -> Tuple[List[M.ImportCandidate], str]:
    args = ["zpool", "import"] + (["-d", search_dir] if search_dir else [])
    r = runner(args, timeout=max(120, _long()))
    found = M.parse_import(r.out + "\n" + r.err)
    if found or r.ok:
        return found, ""
    if "no pools available" in (r.out + r.err):
        return [], ""
    return [], r.reason


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def arcstats(path: str = "/proc/spl/kstat/zfs/arcstats") -> Dict[str, int]:
    return M.parse_arcstats(read_file(path))


def scrub_schedule(cron_path: str = "/etc/cron.d/zfsutils-linux", runner: Runner = run) -> List[str]:
    """Where Debian schedules scrubs: its cron job, and any zfs-scrub systemd timers."""
    out = []
    for line in read_file(cron_path).splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "scrub" in s:
            out.append(f"cron: {s}")
    if "systemctl" in TARGET.bins:
        r = runner(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager", "zfs-scrub*"])
        if r.ok:
            for line in r.out.splitlines():
                parts = line.split()
                units = [p for p in parts if p.endswith(".timer")]
                if units:
                    out.append(f"systemd timer: {units[0]}")
    return out


def _long() -> int:
    from .config import CONFIG
    return CONFIG.long_timeout
