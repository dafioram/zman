"""Space & cleanup: where space goes, and pruning snapshots."""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Sequence, Tuple

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..runner import run
from ..textfmt import fmt_ago, fmt_size, plural, strip_ctl
from ..ui import (Col, Fetched, ListView, Msg, Opt, Sort, choose, menu, nav, paint, pick_candidates, preview,
                  show_text, term_size, typed_confirm, wrap_value)
from . import helptext
from .common import CANCELLED, changed, cmd_text

CHUNK = 100


def space_screen() -> None:
    msg = Msg()
    while True:
        print(paint("Reading pools, datasets and snapshots (can take a while)...", "dim"))
        pools, perr = zfs.list_pools()
        datasets, derr = zfs.list_datasets()
        snaps, serr = zfs.list_snapshots()
        header = [paint("Space & cleanup", "title")]
        for e in (perr, derr, serr):
            if e:
                header.append(paint(strip_ctl(e), "bad"))
        width = max([len(p.name) for p in pools] + [4])
        for p in pools:
            mine = [d for d in datasets if d.pool == p.name]
            data = sum(d.used_ds or 0 for d in mine)
            snap = sum(d.used_snap or 0 for d in mine)
            res = sum(d.used_refres or 0 for d in mine)
            header.append(f"{p.name:<{width}}  {fmt_size(p.size)} · {fmt_size(p.alloc)} used ({p.cap or 0}%) · "
                          f"{fmt_size(p.free)} free")
            header.append(" " * (width + 2) + paint(f"data {fmt_size(data)} · snapshots {fmt_size(snap)} · "
                                                     f"reserved {fmt_size(res)}", "dim"))
        counts: Dict[str, int] = {}
        for s in snaps:
            counts[s.dataset] = counts.get(s.dataset, 0) + 1
        top = sorted((d for d in datasets if d.used_snap), key=lambda d: -(d.used_snap or 0))[:5]
        if top:
            header.append("")
            header.append("Snapshots holding the most space:")
            for d in top:
                header.append(f"  {d.name}  {fmt_size(d.used_snap)} in {plural(counts.get(d.name, 0), 'snapshot')}")
        cands = M.prune_candidates(snaps)
        opts = [Opt("1", f"Prune snapshots ({len(cands)} can be pruned)"), Opt("2", "Space by dataset")]
        key = menu(header, opts, msg)
        if key == "1":
            nav(msg, prune_flow, msg)
        elif key == "2":
            nav(msg, space_by_dataset)


def space_by_dataset() -> None:
    def fetch() -> Fetched:
        items, err = zfs.list_datasets()
        return Fetched(items, [], err)

    cols = [
        Col("NAME", lambda d: d.name, max=60, min=10, flex=True, want=24),
        Col("USED", lambda d: fmt_size(d.used), right=True, min=5, keep=True),
        Col("DATA", lambda d: fmt_size(d.used_ds), right=True, min=5),
        Col("SNAPS", lambda d: fmt_size(d.used_snap), right=True, min=5),
        Col("CHILDREN", lambda d: fmt_size(d.used_child), right=True, min=5, drop=2),
        Col("RESERVED", lambda d: fmt_size(d.used_refres), right=True, min=5, drop=1),
    ]
    sorts = [Sort("used, largest first", lambda d: -(d.used or 0)),
             Sort("snapshot space, largest first", lambda d: -(d.used_snap or 0)),
             Sort("name", lambda d: d.name)]
    from .datasets import open_dataset
    ListView("Space by dataset", fetch, cols, sorts, lambda d: d.name, lambda d: open_dataset(d.name),
             help_text=helptext.SPACE, empty="No datasets.").run()


def group_by_dataset(snaps: Sequence[M.Snapshot]) -> "OrderedDict[str, List[M.Snapshot]]":
    out: "OrderedDict[str, List[M.Snapshot]]" = OrderedDict()
    for s in snaps:
        out.setdefault(s.dataset, []).append(s)
    return out


def destroy_commands(snaps: Sequence[M.Snapshot]) -> List[List[str]]:
    """One `zfs destroy ds@a,b,c` per dataset (chunked), the way ZFS takes several at once."""
    cmds = []
    for ds, items in group_by_dataset(snaps).items():
        for i in range(0, len(items), CHUNK):
            part = items[i:i + CHUNK]
            cmds.append(["zfs", "destroy", f"{ds}@" + ",".join(s.short for s in part)])
    return cmds


def prune_flow(msg: Msg) -> None:
    pools, err = zfs.list_pools()
    if err:
        msg.bad(f"Failed: {err}")
        return
    if not pools:
        msg.warn("No pools to clean up.")
        return
    index = choose("Select a pool to prune", [p.name for p in pools])
    if index is None:
        return
    pool = pools[index].name
    snaps, err = zfs.list_snapshots(dataset=pool, recursive=True)
    if err:
        msg.bad(f"Failed: {err}")
        return
    cands = M.prune_candidates(snaps)
    if not cands:
        msg.warn("Nothing to clean up.")
        return
    w = min(50, max(len(s.name) for s in cands))
    lines = [f"{s.name:<{w}}  {fmt_size(s.used):>7}  {fmt_ago(s.creation):>8}" for s in cands]
    picked = pick_candidates(f"Prune snapshots in {pool}", lines, [
        "Largest first. Held snapshots and snapshots with clones are not listed.",
        "Used is what each frees alone; destroying several can free more. Pick e.g. 1 3-5.",
    ])
    chosen = [cands[i] for i in picked]
    cmds = destroy_commands(chosen)
    print(paint("Working out how much space this frees...", "dim"))
    total = 0
    known = True
    batches = _cmd_groups(chosen)
    for c, (_, items) in zip(cmds, batches):
        names, reclaim, err = zfs.destroy_preview(c[-1])
        if err or set(names) != {s.name for s in items}:
            msg.bad(f"Cannot verify what {c[-1]} would destroy." + (f" ZFS said: {err}" if err else ""))
            return
        if reclaim is None:
            known = False
        else:
            total += reclaim
    groups = group_by_dataset(chosen)
    lines_out: List[object] = [
        (f"Destroys {plural(len(chosen), 'snapshot')} in {plural(len(groups), 'dataset')}.", "bad"),
        f"Frees about {fmt_size(total)}." if known else "ZFS did not say how much space this frees.",
        ("The datasets' current files are not touched. This cannot be undone.", "dim"), "",
    ]
    targets = [f"  {s.name}" for s in chosen]
    width, height = term_size()
    if sum(len(wrap_value(line, width)) for line in lines_out + targets) + len(cmds) + 5 < height and all(
            len(cmd_text(c)) + len("Command: ") <= width for c in cmds):
        preview("Prune snapshots", lines_out + targets, cmds)
    else:
        show_text("\n".join(targets + [""] + [cmd_text(c) for c in cmds]), f"Full prune plan for {pool}")
        preview("Prune snapshots", lines_out)
    n = str(len(chosen))
    if not typed_confirm(n, f"destroy {n} snapshots"):
        msg.warn(CANCELLED)
        return
    again, err = zfs.list_snapshots(dataset=pool, recursive=True)
    if err:
        msg.bad(f"Cannot recheck snapshots to prune: {err}")
        return
    now = {s.name: s for s in again}
    for s in chosen:
        cur = now.get(s.name)
        if cur is None or cur.fingerprint() != s.fingerprint():
            changed(msg, s.name, "removed")
            return
    removed = 0
    failures: List[str] = []
    for c, (ds, items) in zip(cmds, batches):
        print(paint(f"Destroying {plural(len(items), 'snapshot')} of {ds}...", "dim"))
        r = run(c, timeout=CONFIG.long_timeout)
        if r.ok:
            removed += len(items)
        else:
            failures.append(f"{ds}: {r.reason}")
    if failures:
        msg.bad(f"Removed {plural(removed, 'snapshot')}; failed: {failures[0]}"
                + (f" (and {len(failures) - 1} more)" if len(failures) > 1 else ""))
    else:
        msg.ok(f"Removed {plural(removed, 'snapshot')}")


def _cmd_groups(snaps: Sequence[M.Snapshot]) -> List[Tuple[str, List[M.Snapshot]]]:
    out = []
    for ds, items in group_by_dataset(snaps).items():
        for i in range(0, len(items), CHUNK):
            out.append((ds, items[i:i + CHUNK]))
    return out
