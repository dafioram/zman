"""Pools: list, pool view, scrub, create, import, export, destroy."""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..errors import Back, Cancel, Gone
from ..runner import TARGET, run
from ..textfmt import fmt_ago, fmt_size, fmt_time, plural, strip_ctl, strip_ctl_block, to_int
from ..ui import (Col, Extra, Fetched, ListView, Msg, Opt, Sort, ask_text, choose, confirm, emit,
                  item_header, logs_menu, LogSource, menu, nav, paint, pick_candidates, preview,
                  show_text, typed_confirm, wait_enter)
from . import helptext
from .common import CANCELLED, attached, changed, leave_if_done, quick, report
from .diskops import disk_line, free_disks, partition_disks, partition_steps, still_free


# ---------------------------------------------------------------- list


def pool_health(p: M.Pool) -> Tuple[str, str]:
    probs = M.pool_problems(p.status)
    word = p.health or (p.status.state if p.status else "?")
    extra = [x for x in probs if x != word.lower()]
    if extra:
        return f"{word}, {extra[0]}", "bad"
    return word, M.state_style(word) if not probs else "bad"


def pools_screen() -> None:
    def fetch() -> Fetched:
        pools, err = zfs.list_pools()
        return Fetched(pools, list(CONFIG.warnings), err)

    now = time.time
    cols = [
        Col("NAME", lambda p: p.name, max=24, min=6, flex=True, want=12),
        Col("HEALTH", lambda p: pool_health(p)[0], keep=True, style=lambda p: pool_health(p)[1]),
        Col("SIZE", lambda p: fmt_size(p.size), right=True, min=5),
        Col("ALLOC", lambda p: fmt_size(p.alloc), right=True, min=5, drop=3),
        Col("FREE", lambda p: fmt_size(p.free), right=True, min=5),
        Col("CAP", lambda p: f"{p.cap}%" if p.cap is not None else "-", right=True, drop=2),
        Col("FRAG", lambda p: f"{p.frag}%" if p.frag is not None else "-", right=True, drop=1),
        Col("SCRUB", lambda p: M.scan_text(p.status.scan, now())[0] if p.status else "-",
            style=lambda p: M.scan_text(p.status.scan, now())[1] if p.status else "", max=34, min=8,
            flex=True, want=20, drop=4),
    ]
    sorts = [
        Sort("problems first", lambda p: (0 if M.pool_problems(p.status) else 1, p.name)),
        Sort("name", lambda p: p.name),
        Sort("size", lambda p: -(p.size or 0)),
        Sort("free space", lambda p: (p.free or 0)),
    ]
    ListView("Pools", fetch, cols, sorts, lambda p: f"{p.name} {p.health} {p.guid}",
             lambda p: pool_view(p.name),
             extras=[Extra("c", "create pool", create_pool_flow), Extra("i", "import pool", import_flow)],
             help_text=helptext.POOLS, empty="No pools. Press c to create one or i to import one.").run()


# ---------------------------------------------------------------- pool view


def describe_layout(st: M.PoolStatus) -> str:
    tops = [v for v in st.vdevs if v.depth == 1 and v.section == "data"]
    if not tops:
        return "-"
    kinds = {v.kind for v in tops}
    if kinds == {"leaf"}:
        text = "1 disk, no redundancy" if len(tops) == 1 else f"{len(tops)} disks striped, no redundancy"
    elif len(tops) == 1 and tops[0].kind != "leaf":
        n = len(st.children(tops[0].idx))
        name = tops[0].name.rsplit("-", 1)[0]
        text = f"{name} of {n} devices"
    elif kinds == {"mirror"}:
        n = len(st.children(tops[0].idx))
        text = f"{len(tops)} mirrors of {n} devices (striped)"
    else:
        text = ", ".join(v.name for v in tops)
    extras = []
    for sec, word in (("spares", "spare"), ("logs", "log device"), ("cache", "cache device"),
                      ("special", "special device"), ("dedup", "dedup device")):
        n = len([v for v in st.vdevs if v.section == sec and v.is_leaf])
        if n:
            extras.append(plural(n, word))
    return text + (" + " + ", ".join(extras) if extras else "")


def pool_view(name: str, first_msg: str = "") -> None:
    msg = Msg()
    if first_msg:
        msg.ok(first_msg)
    while True:
        pool = zfs.get_pool(name)
        if pool is None:
            leave_if_done(msg)
            raise Gone("pool")
        st = pool.status or M.PoolStatus(name=name, state=pool.health)
        props = zfs.pool_props(name)
        datasets, _ = zfs.list_datasets(root=name)
        n_fs = len([d for d in datasets if not d.is_volume])
        n_vol = len([d for d in datasets if d.is_volume])
        disks, _ = zfs.list_disks()
        health, hstyle = pool_health(pool)
        rows: List[Tuple[str, object]] = [("Health", (health, hstyle))]
        if st.status:
            rows.append(("Problem", (strip_ctl(st.status), "warn")))
        if st.action:
            rows.append(("To fix", (strip_ctl(st.action), "dim")))
        used = f"{fmt_size(pool.size)} total, {fmt_size(pool.alloc)} used ({pool.cap or 0}%), {fmt_size(pool.free)} free"
        if pool.frag is not None:
            used += f" · fragmentation {pool.frag}%"
        rows.append(("Space", used))
        rows.append(("Layout", describe_layout(st)))
        stext, sstyle = M.scan_text(st.scan, time.time())
        rows.append(("Scrub", (stext, sstyle)))
        rows.append(("Errors", (st.errors_text or "-", "bad" if st.data_errors else "")))
        opt_bits = []
        for p in ("autoexpand", "autotrim", "ashift"):
            if p in props:
                opt_bits.append(f"{p} {props[p][0]}")
        if opt_bits:
            rows.append(("Options", (" · ".join(opt_bits), "dim")))
        rows.append(("Contains", f"{plural(n_fs, 'dataset')}, {plural(n_vol, 'volume')}"))
        header = item_header(name, f"guid {pool.guid}" if pool.guid else "", rows)

        scan = st.scan
        scrub_running = scan.kind == "scrub" and scan.state == "running"
        scrub_paused = scan.kind == "scrub" and scan.state == "paused"
        resilvering = scan.kind == "resilver" and scan.state == "running"
        has_errors = bool(st.data_errors or any(v.errors for v in st.vdevs) or st.state not in ("ONLINE", ""))
        leaves = [v for v in st.leaves() if v.section in ("data", "special", "dedup", "logs", "cache")]
        trim_ok = _any_trim_capable(leaves, disks)
        expand = (to_int(props.get("expandsize", ("-", ""))[0]) or 0) > 0
        opts = [
            Opt("1", "Devices"),
            Opt("2", f"Datasets ({n_fs})"),
            Opt("3", "Snapshot several datasets", show=bool(datasets)),
            Opt("4", "Resume scrub" if scrub_paused else "Start scrub", show=not scrub_running and not resilvering),
            Opt("5", "Pause scrub", show=scrub_running),
            Opt("6", "Stop scrub", "warn", show=scrub_running or scrub_paused),
            Opt("7", f"{(scan.kind or 'scrub').capitalize()} progress", show=scan.active),
            Opt("8", "Clear errors", show=has_errors),
            Opt("9", "Trim free space", show=trim_ok),
            Opt("10", "Add a spare disk"),
            Opt("11", "History"),
            Opt("12", "Events"),
            Opt("13", "Settings"),
            Opt("14", "Expand onto bigger disks", show=expand),
            Opt("15", "Upgrade features", "warn", show=M.upgrade_available(props)),
            Opt("16", "Raw details"),
            Opt("17", "Export", "warn"),
            Opt("18", "Destroy pool", "bad"),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .devices import devices_screen
            nav(msg, devices_screen, name)
        elif key == "2":
            from .datasets import datasets_screen
            nav(msg, datasets_screen, f"Datasets in {name}", name)
        elif key == "3":
            nav(msg, snapshot_several_flow, msg, name, datasets)
        elif key == "4":
            nav(msg, scrub_flow, msg, name, "resume" if scrub_paused else "start")
        elif key == "5":
            nav(msg, scrub_flow, msg, name, "pause")
        elif key == "6":
            nav(msg, scrub_flow, msg, name, "stop")
        elif key == "7":
            nav(msg, scan_view, name)
        elif key == "8":
            nav(msg, clear_flow, msg, name, "")
        elif key == "9":
            nav(msg, trim_flow, msg, name)
        elif key == "10":
            nav(msg, add_spare_flow, msg, name)
        elif key == "11":
            nav(msg, logs_menu, history_source(name))
        elif key == "12":
            nav(msg, logs_menu, events_source(name))
        elif key == "13":
            nav(msg, pool_settings, name)
        elif key == "14":
            nav(msg, expand_flow, msg, name, st)
        elif key == "15":
            nav(msg, upgrade_flow, msg, name, props)
        elif key == "16":
            raw_details(name)
        elif key == "17":
            nav(msg, export_flow, msg, name, n_fs, n_vol)
        elif key == "18":
            nav(msg, destroy_pool_flow, msg, pool, datasets)


def _any_trim_capable(leaves: Sequence[M.Vdev], disks: Sequence[M.Disk]) -> bool:
    for v in leaves:
        if not v.path or v.state in ("UNAVAIL", "REMOVED"):
            continue
        d, _ = zfs.disk_for_path(v.path, disks)
        if d is None:
            if os.path.isfile(v.path):      # file-backed vdev
                return True
            continue
        if d.media != "HDD":
            return True
    return False


def recheck_pool(name: str, guid: str) -> Optional[M.Pool]:
    p = zfs.get_pool(name, guids=False)
    if p is None or (guid and p.guid != guid):
        return None
    return p


# ---------------------------------------------------------------- scrub, clear, trim


def scrub_flow(msg: Msg, name: str, mode: str) -> None:
    pool = zfs.get_pool(name, guids=False)
    if pool is None:
        raise Gone("pool")
    if mode in ("start", "resume"):
        args = ["zpool", "scrub", name]
        preview(f"{'Resume' if mode == 'resume' else 'Start'} scrub of {name}", [
            "Reads every block in the pool and checks it against its checksum, repairing",
            "bad copies from redundancy. The pool stays usable but slower while it runs.",
            ("It runs in the background; you can pause or stop it here.", "dim"),
        ], [args])
        if not confirm(f"{'Resume' if mode == 'resume' else 'Start'} scrub of {name}?", True):
            raise Cancel()
    elif mode == "pause":
        args = ["zpool", "scrub", "-p", name]
        preview(f"Pause scrub of {name}", ["Stops reading for now; Resume scrub continues where it left off."], [args])
        if not confirm(f"Pause scrub of {name}?", True):
            raise Cancel()
    else:
        args = ["zpool", "scrub", "-s", name]
        preview(f"Stop scrub of {name}", [
            ("Stops the scrub and throws away its progress; the next scrub starts from the beginning.", "warn"),
            "Use Pause scrub instead to continue later.",
        ], [args])
        if not confirm(f"Stop scrub of {name}?", False):
            raise Cancel()
    cur = recheck_pool(name, pool.guid)
    before = pool.status.scan.state if pool.status else ""
    if cur is None or not cur.status or cur.status.scan.state != before:
        changed(msg, name, "started" if mode in ("start", "resume") else "changed")
        return
    done = {"start": "Scrub started.", "resume": "Scrub resumed.", "pause": "Scrub paused.", "stop": "Scrub stopped."}
    quick(msg, args, done[mode])


def scan_lines(st: M.PoolStatus) -> List[str]:
    s = st.scan
    out = []
    if s.state == "none":
        return ["No scrub has run on this pool yet."]
    state = {"running": "running", "paused": "paused", "finished": "finished", "canceled": "stopped"}[s.state]
    bits = [state]
    if s.pct is not None and s.active:
        bits.append(f"{s.pct:.1f}% done")
    if s.to_go and s.state == "running":
        bits.append(f"{s.to_go} to go")
    out.append(f"State:     {', '.join(bits)}")
    if s.when:
        label = "Started:" if s.active else "Ended:"
        out.append(f"{label:<11}{fmt_time(s.when)} ({fmt_ago(s.when)})")
    if s.repaired:
        out.append(f"{'Repaired:' if s.kind == 'scrub' else 'Copied:':<11}{s.repaired}")
    if s.state == "finished":
        out.append(f"Errors:    {s.errors}")
    res = [v for v in st.leaves() if "resilver" in v.note]
    if res:
        out.append("Being resilvered: " + ", ".join(v.label for v in res))
    out.append("")
    out += [paint(x, "dim") for x in s.lines]
    return out


def scan_view(name: str) -> None:
    msg = Msg()
    while True:
        pool = zfs.get_pool(name, guids=False)
        if pool is None:
            raise Gone("pool")
        st = pool.status or M.PoolStatus(name=name)
        title = f"{(st.scan.kind or 'scrub').capitalize()} of {name}"
        header = [paint(title, "title")] + scan_lines(st)
        key = menu(header, [Opt("a", "Auto-refresh (Ctrl-C stops)")], msg, keys_hint=["Enter refresh"])
        if key == "a":
            try:
                while True:
                    pool = zfs.get_pool(name, guids=False)
                    if pool is None:
                        raise Gone("pool")
                    st = pool.status or M.PoolStatus(name=name)
                    emit([paint(title, "title") + "  " + paint(f"auto-refresh every {CONFIG.refresh_interval}s · Ctrl-C stops", "dim")]
                         + scan_lines(st))
                    if not st.scan.active:
                        msg.ok(f"{(st.scan.kind or 'scrub').capitalize()} finished.")
                        if M.pool_problems(st):
                            msg.add("The pool still reports problems; see its view (Clear errors resets the counts).",
                                    "dim")
                        break
                    time.sleep(CONFIG.refresh_interval)
            except KeyboardInterrupt:
                pass


def clear_flow(msg: Msg, name: str, device: str) -> None:
    args = ["zpool", "clear", name] + ([device] if device else [])
    what = f"device {device}" if device else f"pool {name}"
    preview(f"Clear errors on {what}", [
        "Resets the error counters and lets ZFS try faulted devices again.",
        ("It does not repair data; run a scrub afterwards to check.", "dim"),
    ], [args])
    if not confirm(f"Clear errors on {what}?", True):
        raise Cancel()
    if zfs.get_pool(name, guids=False) is None:
        raise Gone("pool")
    quick(msg, args)


def trim_flow(msg: Msg, name: str) -> None:
    args = ["zpool", "trim", name]
    preview(f"Trim {name}", [
        "Tells the pool's SSDs which blocks are free, so they stay fast.",
        ("It runs in the background. Disks that cannot trim make ZFS report an error; nothing is harmed.", "dim"),
    ], [args])
    if not confirm(f"Start trim of {name}?", True):
        raise Cancel()
    if zfs.get_pool(name, guids=False) is None:
        raise Gone("pool")
    quick(msg, args, "Trim started.")


# ---------------------------------------------------------------- spares, settings, expand, upgrade


def add_spare_flow(msg: Msg, name: str) -> None:
    pool = zfs.get_pool(name, guids=False)
    if pool is None:
        raise Gone("pool")
    disks, uses, _, err = zfs.disk_inventory()
    st = pool.status or M.PoolStatus(name=name)
    min_size = 0
    for v in st.leaves():
        if v.section == "data" and v.path:
            d, p = zfs.disk_for_path(v.path, disks)
            if d:
                size = p.size if p else d.size
                min_size = max(min_size, size)
    cands = free_disks(disks, uses, min_size)
    if not cands:
        msg.warn("No free disk is large enough to stand in for this pool's disks." if min_size else "No free disks.")
        return
    i = choose(f"Add a spare to {name}", [disk_line(d, uses[d.name]) for d in cands], explain=[
        f"A spare must be at least as large as the disks it may replace ({fmt_size(min_size)}).",
    ])
    if i is None:
        raise Cancel()
    d = cands[i]
    use = uses[d.name]
    args = ["zpool", "add"] + (["-f"] if use.warn else []) + [name, "spare", d.dev_path]
    lines: List[object] = [
        f"Adds {d.name} ({fmt_size(d.size)}, {d.describe() or d.media}) to {name} as a hot spare.",
        "When a disk fails, ZFS's event daemon (zed) swaps the spare in and resilvers onto it.",
        ("Everything on the disk is erased.", "bad"),
    ]
    if use.warn:
        lines.append((f"{d.name} {use.warn}.", "warn"))
    preview(f"Add spare to {name}", lines, [args])
    if not typed_confirm(d.name, "erase it and add it as a spare"):
        raise Cancel()
    if not still_free([d]):
        changed(msg, d.name, "added")
        return
    quick(msg, args, f"Done. {d.name} is a spare of {name}.")


def pool_settings(name: str) -> None:
    msg = Msg()
    while True:
        if zfs.get_pool(name, guids=False) is None:
            raise Gone("pool")
        props = zfs.pool_props(name)
        ae = props.get("autoexpand", ("?", ""))[0]
        at = props.get("autotrim", ("?", ""))[0]
        header = [paint(f"Settings of pool {name}", "title"),
                  paint("Autoexpand: grow the pool when all disks of a vdev get bigger.", "dim"),
                  paint("Autotrim: tell SSDs about freed space as it happens.", "dim")]
        key = menu(header, [Opt("1", f"Autoexpand: {ae}"), Opt("2", f"Autotrim: {at}")], msg)
        if key in ("1", "2"):
            prop = "autoexpand" if key == "1" else "autotrim"
            cur = ae if key == "1" else at
            i = choose(f"{prop.capitalize()} for {name}", ["on", "off"], 0 if cur == "on" else 1)
            if i is None:
                continue
            val = ["on", "off"][i]
            if val == cur:
                continue
            args = ["zpool", "set", f"{prop}={val}", name]
            preview(f"Set {prop} on {name}", [f"Changes {prop} from {cur} to {val}."], [args])
            if confirm(f"Set {prop}={val}?", True):
                quick(msg, args)
            else:
                msg.warn(CANCELLED)


def expand_flow(msg: Msg, name: str, st: M.PoolStatus) -> None:
    leaves = [v for v in st.leaves() if v.section == "data" and v.state == "ONLINE"]
    steps = [(f"expand {v.label}", ["zpool", "online", "-e", name, v.guid or v.path or v.name]) for v in leaves]
    if not steps:
        msg.warn("No online devices to expand.")
        return
    ok, _ = attached(f"Expand {name} onto bigger disks", [
        "Lets the pool use the extra space on disks that were replaced with bigger ones.",
        "Data is not moved or changed.",
    ], steps, ask_first=True, default=True)
    msg.ok("Done.") if ok else msg.warn("Not all devices were expanded; see the pool's space line.")


def upgrade_flow(msg: Msg, name: str, props: Dict[str, Tuple[str, str]]) -> None:
    feats = sorted(k[len("feature@"):] for k, v in props.items() if k.startswith("feature@") and v[0] == "disabled")
    args = ["zpool", "upgrade", name]
    lines: List[object] = [
        f"Enables {plural(len(feats), 'feature')} this ZFS version supports: " + ", ".join(feats[:8])
        + (" ..." if len(feats) > 8 else ""),
        ("After this, systems with older ZFS versions may not be able to import the pool.", "warn"),
        ("This cannot be undone. Boot pools may need a matching bootloader; GRUB reads few features.", "warn"),
    ]
    preview(f"Upgrade features of {name}", lines, [args])
    if not confirm(f"Upgrade {name}?", False):
        raise Cancel()
    if zfs.get_pool(name, guids=False) is None:
        raise Gone("pool")
    quick(msg, args)


def raw_details(name: str) -> None:
    r1 = run(["zpool", "status", "-v", "-P", name])
    r2 = run(["zpool", "get", "all", name])
    text = f"$ zpool status -v -P {name}\n{r1.out or r1.err}\n$ zpool get all {name}\n{r2.out or r2.err}"
    show_text(strip_ctl_block(text), f"Raw details of {name}")


# ---------------------------------------------------------------- history and events


def history_source(name: str) -> LogSource:
    def fetch(n: Optional[int], since, stamps: bool) -> Tuple[str, str]:
        print(paint(f"Reading history of {name}...", "dim"))
        r = run(["zpool", "history", name], timeout=CONFIG.long_timeout)
        if not r.ok:
            return "", r.reason
        entries = M.parse_history(r.out)
        if since is not None:
            entries = [e for e in entries if e[0] is not None and e[0] >= since]
        if n is not None:
            entries = entries[-n:]
        lines = [line if stamps or e is None else line.split(" ", 1)[-1] for e, line in entries]
        return "\n".join(lines), ""

    return LogSource(f"History of {name}", fetch)


def events_source(name: str) -> LogSource:
    def fetch(n: Optional[int], since, stamps: bool) -> Tuple[str, str]:
        r = run(["zpool", "events", name])
        if not r.ok:
            return "", r.reason
        lines = [strip_ctl(x) for x in r.out.splitlines() if x.strip() and not x.startswith("TIME")]
        if since is not None:
            lines = [x for x in lines if (M.parse_event_time(x) or since) >= since and M.parse_event_time(x)]
        if n is not None:
            lines = lines[-n:]
        if not stamps:
            lines = [" ".join(x.split()[4:]) for x in lines]
        return "\n".join(lines), ""

    def follow() -> None:
        emit([paint(f"Following events of {name}", "title"), paint("Ctrl-C stops and returns.", "dim"), ""])
        from ..runner import run_fg
        run_fg(["zpool", "events", "-f", name])

    return LogSource(f"Events of {name}", fetch, follow)


# ---------------------------------------------------------------- snapshot several


def snapshot_several_flow(msg: Msg, pool: str, datasets: Sequence[M.Dataset]) -> None:
    from datetime import datetime
    if not datasets:
        msg.warn(f"{pool} has no datasets.")
        return
    lines = [f"{d.name}  {fmt_size(d.used)}  {'volume' if d.is_volume else M.dataset_state(d)[0]}" for d in datasets]
    picked = pick_candidates(f"Snapshot several datasets in {pool}", lines, [
        "Pick the datasets to snapshot at the same moment, e.g. 1 3-5 or all.",
        "ZFS takes them in one step, so they all match the same point in time.",
    ])
    chosen = [datasets[i] for i in picked]
    default = M.default_snapshot_name(CONFIG.snapshot_prefix, datetime.now())
    print()
    print(f"Name: {paint('@' + default, 'bold')}")
    print(paint("(Enter keeps this name, or type your own)", "dim"))
    raw = ask_text("> ") or default
    raw = raw.lstrip("@")
    err = M.check_component(raw, "snapshot name")
    if err:
        msg.warn(err)
        return
    args = ["zfs", "snapshot"] + [f"{d.name}@{raw}" for d in chosen]
    quick(msg, args, f"Done. Took {plural(len(chosen), 'snapshot')} @{raw}.")


# ---------------------------------------------------------------- export, destroy


def export_flow(msg: Msg, name: str, n_fs: int, n_vol: int) -> None:
    pool = zfs.get_pool(name, guids=False)
    if pool is None:
        raise Gone("pool")
    args = ["zpool", "export", name]
    preview(f"Export {name}", [
        f"Unmounts its {plural(n_fs, 'dataset')}, closes its {plural(n_vol, 'volume')} and releases the disks.",
        "The data stays on the disks: import the pool again here (Pools, i) or on another machine.",
        ("Programs, shares or VMs using it must be stopped first.", "warn"),
    ], [args])
    if not confirm(f"Export {name}?", False):
        raise Cancel()
    if recheck_pool(name, pool.guid) is None:
        changed(msg, name, "exported")
        return
    report(msg, run(args, timeout=CONFIG.long_timeout), f"Exported pool {name}.")


def destroy_pool_flow(msg: Msg, pool: M.Pool, datasets: Sequence[M.Dataset]) -> None:
    name = pool.name
    snaps, _ = zfs.list_snapshots(dataset=name, recursive=True)
    st = pool.status or M.PoolStatus(name=name)
    devs = [v.label for v in st.leaves()]
    held = [s for s in snaps if s.holds]
    args = ["zpool", "destroy", name]
    lines: List[object] = [
        (f"Destroys pool {name} and ALL data in it:", "bad"),
        f"  {plural(len([d for d in datasets if not d.is_volume]), 'dataset')}, "
        f"{plural(len([d for d in datasets if d.is_volume]), 'volume')}, {plural(len(snaps), 'snapshot')}"
        f" · {fmt_size(pool.alloc)} of data",
        f"  devices: {', '.join(devs[:6])}{' ...' if len(devs) > 6 else ''}",
    ]
    if held:
        lines.append((f"  {plural(len(held), 'snapshot')} have holds; destroying the pool ignores holds.", "warn"))
    lines += ["", ("This cannot be undone.", "bad"),
              ("To keep the data and just detach the disks, use Export instead.", "dim")]
    preview(f"Destroy pool {name}", lines, [args])
    if not typed_confirm(name, "destroy the pool and all its data"):
        raise Cancel()
    if recheck_pool(name, pool.guid) is None:
        changed(msg, name, "destroyed")
        return
    report(msg, run(args, timeout=CONFIG.long_timeout), f"Removed pool {name}")


# ---------------------------------------------------------------- import


def import_flow(msg: Msg) -> None:
    print(paint("Looking for pools on attached disks (can take a while)...", "dim"))
    cands, err = zfs.import_candidates(search_dir=TARGET.by_id_dir)
    if err:
        msg.bad(f"Failed: {err}")
        return
    if not cands:
        msg.warn("No pools to import.")
        return
    i = choose("Import a pool", [f"{c.name}  id {c.id}  {c.state}" for c in cands])
    if i is None:
        raise Cancel()
    c = cands[i]
    force = c.needs_force
    args = ["zpool", "import", "-d", TARGET.by_id_dir] + (["-f"] if force else []) + [c.id]
    lines: List[object] = [f"Pool:   {c.name} (id {c.id})", ("State:  " + c.state, M.state_style(c.state))]
    if c.status:
        lines.append(("Status: " + c.status, "warn"))
    if c.action:
        lines.append(("Action: " + c.action, "dim"))
    lines.append("")
    lines += c.config[:12]
    if force:
        lines += ["", ("This pool was last used by another system. Importing it here while that system", "bad"),
                  ("still uses it destroys the pool. Make sure the other system is off.", "bad")]
    preview(f"Import {c.name}", lines, [args])
    if not confirm(f"Import {c.name}?", not force):
        raise Cancel()
    again, _ = zfs.import_candidates(search_dir=TARGET.by_id_dir)
    if not any(x.id == c.id for x in again):
        changed(msg, c.name, "imported")
        return
    print(paint("Importing...", "dim"))
    r = run(args, timeout=CONFIG.long_timeout)
    if report(msg, r, f"Done. Imported {c.name}."):
        nav(msg, pool_view, c.name, f"Done. Imported {c.name}.")


# ---------------------------------------------------------------- create


ASHIFTS = [(12, "12 (4K sectors; right for almost all disks)"), (13, "13 (8K sectors; some SSDs)"),
           (9, "9 (512-byte sectors; only very old disks)")]
COMPRESSIONS = [("lz4", "lz4 (fast; the usual choice)"), ("zstd", "zstd (smaller, uses more CPU)"), ("off", "off")]


def ask_pool_name(existing: Sequence[str]) -> str:
    while True:
        raw = ask_text("Pool name: ")
        if not raw:
            raise Cancel()
        err = M.check_pool_name(raw)
        if not err and raw in existing:
            err = f"A pool named {raw} already exists."
        if not err:
            return raw
        print(paint(err, "warn"))


def create_pool_flow(msg: Msg) -> None:
    disks, uses, statuses, err = zfs.disk_inventory()
    free = free_disks(disks, uses)
    if not free:
        msg.warn("No free disks. A disk must not be mounted, in a pool, or used by LVM, RAID or swap.")
        return
    pools, _ = zfs.list_pools()
    emit([paint("Create a pool", "title"), "",
          f"Free disks: {', '.join(d.name for d in free)}",
          paint("Names use letters, digits and _ . : - and start with a letter. Ctrl-C cancels.", "dim"), ""])
    name = ask_pool_name([p.name for p in pools])
    i = choose(f"New pool {name}: layout", [f"{lay.label:<16} {lay.explain}" for lay in M.LAYOUTS])
    if i is None:
        raise Cancel()
    layout = M.LAYOUTS[i]
    while True:
        picked = pick_candidates(f"Disks for {name} ({layout.label})", [disk_line(d, uses[d.name]) for d in free], [
            f"{layout.label}: {layout.explain}. Pick e.g. 1 2 or 1-4.",
            "Everything on the picked disks will be erased.",
        ] + (["Disks are paired by size: the two largest form the first mirror, and so on."]
             if layout.key == "mirrors" else []),
            styles=["warn" if uses[d.name].warn else "" for d in free])
        chosen = [free[j] for j in picked]
        err = M.check_layout(layout.key, len(chosen))
        if not err:
            break
        emit([paint(err, "warn")])
        wait_enter()
    create_setup(msg, name, layout, chosen, uses)


def create_setup(msg: Msg, name: str, layout: M.Layout, chosen: List[M.Disk], uses: Dict[str, M.DiskUse]) -> None:
    o = M.CreateOpts(autotrim=True)
    sizes = [d.size for d in chosen]
    smsg = Msg()
    while True:
        cap = M.capacity_estimate(layout.key, [M.headroom_size(sizes)] * len(sizes) if o.headroom else sizes)
        part = M.headroom_size(sizes) if o.headroom else 0
        header = [paint(f"New pool {name}", "title"),
                  f"Layout:   {layout.label} of {plural(len(chosen), 'disk')}",
                  "Disks:    " + ", ".join(f"{d.name} {fmt_size(d.size)}" for d in chosen),
                  f"Capacity: about {fmt_size(cap)} before ZFS overhead"]
        if layout.key == "mirrors":
            pairs = M.pair_disks(sizes)
            header.append("Mirrors:  " + "; ".join(f"{chosen[a].name}+{chosen[b].name}" for a, b in pairs))
        if o.headroom:
            header.append(paint(f"Each disk gets one partition of {fmt_size(part)}, a little below the smallest disk,", "dim"))
            header.append(paint("so a replacement that is a few MB smaller still fits.", "dim"))
        elif len(set(sizes)) > 1 and layout.key != "single":
            header.append(paint("Capacity follows the smallest disk. With autoexpand on (zman sets it), it grows", "dim"))
            header.append(paint("once the smaller disks are replaced with bigger ones.", "dim"))
        opts = [
            Opt("1", "Create"),
            Opt("2", f"Disk sizes: {'leave headroom' if o.headroom else 'whole disks (ZFS partitions them)'}",
                show=layout.key != "single"),
            Opt("3", f"Mountpoint: {o.mountpoint or '/' + name}"),
            Opt("4", f"Compression: {o.compression}"),
            Opt("5", f"Sector size (ashift): {o.ashift}"),
            Opt("6", f"Encryption: {'on (passphrase)' if o.encryption else 'off'}"),
            Opt("7", f"Autotrim: {'on' if o.autotrim else 'off'}"),
            Opt("8", f"Access times: {'on' if o.atime else 'off'}"),
        ]
        try:
            key = menu(header, opts, smsg)
        except Back:
            raise Cancel()
        if key == "1":
            if run_create(msg, name, layout, chosen, uses, o):
                return
        elif key == "2":
            j = choose("Disk sizes", ["Whole disks: ZFS partitions each disk itself (usual)",
                                      "Leave headroom: one partition per disk, a little below the smallest"],
                       1 if o.headroom else 0)
            if j is not None:
                o.headroom = j == 1
        elif key == "3":
            raw = ask_text(f"Mountpoint (absolute path, 'none', or blank for /{name}): ")
            if raw in ("", f"/{name}"):
                o.mountpoint = ""
            elif raw == "none" or (raw.startswith("/") and " " not in raw and len(raw) < 4000):
                o.mountpoint = raw
            else:
                smsg.warn("A mountpoint is an absolute path like /srv/data, or none.")
        elif key == "4":
            j = choose("Compression", [c[1] for c in COMPRESSIONS], [c[0] for c in COMPRESSIONS].index(o.compression))
            if j is not None:
                o.compression = COMPRESSIONS[j][0]
        elif key == "5":
            j = choose("Sector size (ashift)", [a[1] for a in ASHIFTS], [a[0] for a in ASHIFTS].index(o.ashift),
                       explain=["It cannot be changed after the pool is created."])
            if j is not None:
                o.ashift = ASHIFTS[j][0]
        elif key == "6":
            j = choose("Encryption", ["off", "on: zpool asks for a passphrase when the pool is created"],
                       1 if o.encryption else 0,
                       explain=["Encryption cannot be turned off later.",
                                "Without the passphrase the data cannot be recovered.",
                                "After a reboot the key must be loaded before datasets mount."])
            if j is not None:
                o.encryption = j == 1
        elif key == "7":
            o.autotrim = not o.autotrim
        elif key == "8":
            o.atime = not o.atime


def run_create(msg: Msg, name: str, layout: M.Layout, chosen: List[M.Disk], uses: Dict[str, M.DiskUse],
               o: M.CreateOpts) -> bool:
    sizes = [d.size for d in chosen]
    warned = [d for d in chosen if uses[d.name].warn]
    part = M.headroom_size(sizes) if o.headroom else 0
    devs = [f"{d.by_id}-part1" if d.by_id else d.path + "1" for d in chosen] if o.headroom else [d.dev_path for d in chosen]
    args = M.create_args(name, layout.key, devs, sizes, o, force=bool(warned) or o.headroom)
    lines: List[object] = [("These disks will be erased:", "bad")]
    for d in chosen:
        lines.append(f"  {d.name}  {fmt_size(d.size)}  {d.describe() or d.media}")
        if uses[d.name].warn:
            lines.append((f"      {uses[d.name].warn}", "warn"))
    lines.append("")
    lines.append(f"Then creates pool {name}: {layout.label}, about "
                 f"{fmt_size(M.capacity_estimate(layout.key, [part] * len(sizes) if part else sizes))}.")
    if o.encryption:
        lines.append(("zpool will ask for the new passphrase twice.", "warn"))
    cmds = [a for _, a in partition_steps(chosen, part, f"zfs-{name}")] if o.headroom else []
    preview(f"Create pool {name}", lines, cmds + [args])
    if not typed_confirm(name, "erase these disks and create the pool"):
        msg.warn(CANCELLED)
        return True
    if warned:
        print(paint(f"{', '.join(d.name for d in warned)} still hold data or labels (shown above).", "warn"))
        if not typed_confirm("erase", "wipe them anyway"):
            msg.warn(CANCELLED)
            return True
    pools, _ = zfs.list_pools()
    if any(p.name == name for p in pools) or not still_free(chosen):
        changed(msg, "The disks or pool names", "created")
        return True
    if o.headroom:
        paths, perr = partition_disks(chosen, part, f"zfs-{name}")
        if perr:
            msg.bad(f"Failed: {perr}")
            msg.add("The disks may now be partially partitioned; no pool was created.", "dim")
            return True
        args = M.create_args(name, layout.key, paths, sizes, o, force=True)
    ok, reason = attached(f"Creating pool {name}", [], [("zpool create", args)])
    if not ok:
        msg.bad(f"Failed: {reason}")
        return True
    msg.ok(f"Done. Created pool {name}.")
    nav(msg, pool_view, name, f"Done. Created pool {name}.")
    return True
