"""Datasets and volumes: lists, views, create, settings, rename, mount, keys, destroy."""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from .. import model as M
from .. import zfs
from ..config import CONFIG
from ..errors import Back, Cancel, Gone, Leave
from ..runner import run
from ..textfmt import fmt_ago, fmt_size, fmt_time, parse_amount, plural, strip_ctl_block, to_int
from ..ui import (Col, Fetched, ListView, Msg, Opt, Sort, ask_text, choose, confirm, item_header, menu, nav,
                  paint, preview, show_text, typed_confirm, wait_enter)
from . import helptext
from .common import CANCELLED, attached, changed, leave_if_done, quick, report

VOL_ALIGN = 128 * 1024   # a multiple of every common volblocksize


# ---------------------------------------------------------------- lists


def datasets_screen(title: str = "Datasets", root: str = "", only: Optional[Sequence[str]] = None,
                    exclude_root: bool = False) -> None:
    def fetch() -> Fetched:
        items, err = zfs.list_datasets(types="filesystem", root=root)
        if only is not None:
            items = [d for d in items if d.name in only]
        if exclude_root:
            items = [d for d in items if d.name != root]
        return Fetched(items, list(CONFIG.warnings) if not root else [], err)

    cols = [
        Col("NAME", lambda d: d.name, max=60, min=10, flex=True, want=24),
        Col("STATE", lambda d: M.dataset_state(d)[0], keep=True, style=lambda d: M.dataset_state(d)[1]),
        Col("USED", lambda d: fmt_size(d.used), right=True, min=5),
        Col("AVAIL", lambda d: fmt_size(d.avail), right=True, min=5, drop=2),
        Col("REFER", lambda d: fmt_size(d.refer), right=True, min=5, drop=1),
        Col("MOUNTPOINT", lambda d: d.mountpoint or "-", max=50, min=8, flex=True, want=16, drop=3),
    ]
    sorts = [
        Sort("name", lambda d: d.name),
        Sort("used, largest first", lambda d: -(d.used or 0)),
        Sort("problems first", lambda d: ({"bad": 0, "warn": 1}.get(M.dataset_state(d)[1], 2), d.name)),
    ]
    ListView(title, fetch, cols, sorts, lambda d: f"{d.name} {d.mountpoint} {M.dataset_state(d)[0]} {d.guid}",
             lambda d: dataset_view(d.name), help_text=helptext.DATASETS,
             empty="No datasets." if not root else "No datasets here.").run()


def volumes_screen(title: str = "Volumes", root: str = "") -> None:
    def fetch() -> Fetched:
        items, err = zfs.list_datasets(types="volume", root=root)
        return Fetched(items, list(CONFIG.warnings) if not root else [], err)

    cols = [
        Col("NAME", lambda d: d.name, max=60, min=10, flex=True, want=24),
        Col("STATE", lambda d: M.dataset_state(d)[0], keep=True, style=lambda d: M.dataset_state(d)[1]),
        Col("SIZE", lambda d: fmt_size(d.volsize), right=True, min=5),
        Col("USED", lambda d: fmt_size(d.used), right=True, min=5),
        Col("REFER", lambda d: fmt_size(d.refer), right=True, min=5, drop=1),
        Col("DEVICE", lambda d: f"/dev/zvol/{d.name}", max=60, min=10, flex=True, want=20, drop=2),
    ]
    sorts = [
        Sort("name", lambda d: d.name),
        Sort("used, largest first", lambda d: -(d.used or 0)),
        Sort("problems first", lambda d: ({"bad": 0, "warn": 1}.get(M.dataset_state(d)[1], 2), d.name)),
    ]
    ListView(title, fetch, cols, sorts, lambda d: f"{d.name} {d.guid}", lambda d: volume_view(d.name),
             help_text=helptext.VOLUMES, empty="No volumes.").run()


def open_dataset(name: str) -> None:
    """Open the right view for a filesystem or volume."""
    ds = zfs.get_dataset(name)
    if ds is None:
        raise Gone("dataset")
    if ds.is_volume:
        volume_view(name)
    else:
        dataset_view(name)


# ---------------------------------------------------------------- shared header bits


def _encryption_text(ds: M.Dataset) -> Tuple[str, str]:
    if not ds.encrypted:
        return "off", ""
    key = "key not loaded" if ds.locked else "key loaded"
    root = "" if ds.encroot == ds.name else f", root {ds.encroot}"
    return f"{ds.encryption}, {key}{root}", "warn" if ds.locked else ""


def _quota_text(ds: M.Dataset) -> str:
    if not ds.quota:
        return "none"
    pct = f" ({(ds.used or 0) * 100 // ds.quota}% used)" if ds.used is not None else ""
    return f"{fmt_size(ds.quota)}{pct}"


def _snap_summary(snaps: Sequence[M.Snapshot]) -> str:
    if not snaps:
        return "none"
    newest = max(snaps, key=lambda s: s.txg)
    return f"{len(snaps)}, newest {fmt_ago(newest.creation)}"


def _children(name: str) -> List[M.Dataset]:
    items, _ = zfs.list_datasets(root=name, depth=1)
    return [d for d in items if d.name != name]


# ---------------------------------------------------------------- dataset view


def dataset_view(name: str, first_msg: str = "") -> None:
    msg = Msg()
    if first_msg:
        msg.ok(first_msg)
    while True:
        ds = zfs.get_dataset(name)
        if ds is None:
            leave_if_done(msg)
            raise Gone("dataset")
        if ds.is_volume:
            volume_view(name)
            return
        snaps, _ = zfs.list_snapshots(dataset=name)
        kids = _children(name)
        word, style = M.dataset_state(ds)
        rows: List[Tuple[str, object]] = [("State", (word, style))]
        used = fmt_size(ds.used)
        parts = [f"data {fmt_size(ds.used_ds)}", f"snapshots {fmt_size(ds.used_snap)}"]
        if ds.used_child:
            parts.append(f"children {fmt_size(ds.used_child)}")
        rows.append(("Used", used + "  " + paint("(" + ", ".join(parts) + ")", "dim")))
        rows.append(("Available", fmt_size(ds.avail)))
        rows.append(("Mountpoint", ds.mountpoint or "-"))
        ratio = f", ratio {ds.ratio:.2f}x" if ds.ratio else ""
        rows.append(("Compression", f"{ds.compression}{ratio}"))
        rows.append(("Quota", _quota_text(ds)))
        if ds.encrypted:
            rows.append(("Encryption", _encryption_text(ds)))
        if ds.origin:
            rows.append(("Origin", (f"{ds.origin} (this is a clone)", "dim")))
        rows.append(("Snapshots", _snap_summary(snaps)))
        rows.append(("Created", (fmt_time(ds.creation), "dim")))
        header = item_header(name, "", rows)
        is_root = ds.is_root
        loaded = ds.encrypted and not ds.locked
        opts = [
            Opt("1", f"Snapshots ({len(snaps)})"),
            Opt("2", "Take snapshot"),
            Opt("3", f"Child datasets ({len(kids)})", show=bool(kids)),
            Opt("4", "New child dataset"),
            Opt("5", "New volume here"),
            Opt("6", "Settings"),
            Opt("7", "Mount", show=not ds.mounted and ds.has_mount_path and ds.canmount != "off" and not ds.locked),
            Opt("8", "Unmount", show=ds.mounted),
            Opt("9", "Load key", show=ds.encroot == name and ds.locked),
            Opt("10", "Unload key", "warn", show=ds.encroot == name and loaded),
            Opt("11", "Rename", "warn", show=not is_root),
            Opt("12", f"Pool: {ds.pool}"),
            Opt("13", f"Parent: {ds.parent}", show=not is_root),
            Opt("14", f"Origin: {ds.origin}", show=bool(ds.origin)),
            Opt("15", f"Encryption root: {ds.encroot}", show=ds.encrypted and bool(ds.encroot) and ds.encroot != name),
            Opt("16", "Raw details"),
            Opt("17", "Destroy", "bad", show=not is_root),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .snapshots import snapshots_screen
            nav(msg, snapshots_screen, name)
        elif key == "2":
            from .snapshots import take_snapshot_flow
            nav(msg, take_snapshot_flow, msg, ds, len(kids))
        elif key == "3":
            nav(msg, datasets_screen, f"Datasets in {name}", name, None, True)
        elif key == "4":
            nav(msg, new_dataset_flow, msg, ds)
        elif key == "5":
            nav(msg, new_volume_flow, msg, ds)
        elif key == "6":
            nav(msg, settings_screen, name)
        elif key == "7":
            mount_flow(msg, ds)
        elif key == "8":
            quick(msg, ["zfs", "unmount", name], f"Done. Unmounted {name}.")
        elif key == "9":
            nav(msg, load_key_flow, msg, ds)
        elif key == "10":
            nav(msg, unload_key_flow, msg, ds)
        elif key == "11":
            new = rename_flow(msg, ds)
            if new:
                name = new
        elif key == "12":
            from .pools import pool_view
            nav(msg, pool_view, ds.pool)
        elif key == "13":
            nav(msg, open_dataset, ds.parent)
        elif key == "14":
            from .snapshots import snapshot_view
            nav(msg, snapshot_view, ds.origin)
        elif key == "15":
            nav(msg, open_dataset, ds.encroot)
        elif key == "16":
            raw_details(name)
        elif key == "17":
            nav(msg, destroy_flow, msg, ds)


def raw_details(name: str) -> None:
    r = run(["zfs", "get", "all", name])
    show_text(strip_ctl_block(r.out or r.err), f"Raw details of {name}")


def mount_flow(msg: Msg, ds: M.Dataset) -> None:
    quick(msg, ["zfs", "mount", ds.name], f"Done. Mounted at {ds.mountpoint}.")


# ---------------------------------------------------------------- volume view


def volume_view(name: str, first_msg: str = "") -> None:
    msg = Msg()
    if first_msg:
        msg.ok(first_msg)
    while True:
        ds = zfs.get_dataset(name)
        if ds is None:
            leave_if_done(msg)
            raise Gone("volume")
        snaps, _ = zfs.list_snapshots(dataset=name)
        word, style = M.dataset_state(ds)
        dev = f"/dev/zvol/{name}"
        thin = not to_int(ds.raw.get("refreservation", "0"))
        rows: List[Tuple[str, object]] = [
            ("State", (word, style)),
            ("Size", fmt_size(ds.volsize)),
            ("Used", fmt_size(ds.used) + "  " + paint(f"(data {fmt_size(ds.used_ds)}, snapshots "
                                                      f"{fmt_size(ds.used_snap)}, reserved {fmt_size(ds.used_refres)})", "dim")),
            ("Device", dev if os.path.exists(dev) else (f"{dev} (not present)", "warn")),
            ("Compression", f"{ds.compression}" + (f", ratio {ds.ratio:.2f}x" if ds.ratio else "")),
            ("Space", "thin provisioned (no reservation)" if thin else "reserved up front"),
        ]
        if ds.encrypted:
            rows.append(("Encryption", _encryption_text(ds)))
        if ds.origin:
            rows.append(("Origin", (f"{ds.origin} (this is a clone)", "dim")))
        rows.append(("Snapshots", _snap_summary(snaps)))
        header = item_header(name, "volume", rows)
        loaded = ds.encrypted and not ds.locked
        opts = [
            Opt("1", f"Snapshots ({len(snaps)})"),
            Opt("2", "Take snapshot"),
            Opt("3", "Settings"),
            Opt("4", "Grow"),
            Opt("5", "Load key", show=ds.encroot == name and ds.locked),
            Opt("6", "Unload key", "warn", show=ds.encroot == name and loaded),
            Opt("7", "Rename", "warn"),
            Opt("8", f"Pool: {ds.pool}"),
            Opt("9", f"Parent: {ds.parent}"),
            Opt("10", f"Origin: {ds.origin}", show=bool(ds.origin)),
            Opt("11", f"Encryption root: {ds.encroot}", show=ds.encrypted and bool(ds.encroot) and ds.encroot != name),
            Opt("12", "Raw details"),
            Opt("13", "Destroy", "bad"),
        ]
        key = menu(header, opts, msg)
        if key == "1":
            from .snapshots import snapshots_screen
            nav(msg, snapshots_screen, name)
        elif key == "2":
            from .snapshots import take_snapshot_flow
            nav(msg, take_snapshot_flow, msg, ds, 0)
        elif key == "3":
            nav(msg, settings_screen, name)
        elif key == "4":
            nav(msg, grow_flow, msg, ds)
        elif key == "5":
            nav(msg, load_key_flow, msg, ds)
        elif key == "6":
            nav(msg, unload_key_flow, msg, ds)
        elif key == "7":
            new = rename_flow(msg, ds)
            if new:
                name = new
        elif key == "8":
            from .pools import pool_view
            nav(msg, pool_view, ds.pool)
        elif key == "9":
            nav(msg, open_dataset, ds.parent)
        elif key == "10":
            from .snapshots import snapshot_view
            nav(msg, snapshot_view, ds.origin)
        elif key == "11":
            nav(msg, open_dataset, ds.encroot)
        elif key == "12":
            raw_details(name)
        elif key == "13":
            nav(msg, destroy_flow, msg, ds)


# ---------------------------------------------------------------- choices for settings


COMPRESSION_CHOICES = [("lz4", "lz4 (fast; the usual choice)"), ("zstd", "zstd (smaller, more CPU)"),
                       ("gzip", "gzip (small, slow)"), ("off", "off")]
RECORDSIZE_CHOICES = [("16K", "16K (databases)"), ("64K", "64K (VM images)"), ("128K", "128K (general use)"),
                      ("1M", "1M (large media files, backups)")]
ONOFF = [("on", "on"), ("off", "off")]


def _value_choice(title: str, choices: Sequence[Tuple[str, str]], current: str, inherit_label: str,
                  explain: Sequence[str] = ()) -> Optional[str]:
    """Returns a value, '' for 'inherit/reset', or None when backed out."""
    labels = [lab for _, lab in choices] + ([inherit_label] if inherit_label else [])
    cur = next((i for i, (v, _) in enumerate(choices) if v.lower() == current.lower()), None)
    i = choose(title, labels, cur, explain=explain)
    if i is None:
        return None
    if i >= len(choices):
        return ""
    return choices[i][0]


def _ask_amount(prompt: str, allow_none: bool) -> Optional[int]:
    """None means 'none'; raises Cancel on Ctrl-C."""
    raw = ask_text(prompt)
    if not raw or (allow_none and raw.lower() == "none"):
        if not allow_none:
            raise Cancel()
        return None
    try:
        n = parse_amount(raw)
    except ValueError as e:
        raise Leave(str(e) + " Try 500G, 2T or 1.5TB.", "warn")
    return n


def _source_text(src: str) -> str:
    if src.startswith("inherited from"):
        return src
    return {"local": "set here", "default": "default", "-": "", "received": "received"}.get(src, src)


def _fmt_prop(prop: str, value: str) -> str:
    if prop in ("quota", "reservation", "refquota", "refreservation"):
        n = to_int(value)
        return "none" if not n else fmt_size(n)
    if prop in ("recordsize", "volsize"):
        n = to_int(value)
        if n and n % 1024 == 0:
            k = n // 1024
            return f"{k // 1024}M" if k % 1024 == 0 else f"{k}K"
        return value
    return value


# ---------------------------------------------------------------- settings


FS_SETTINGS = [("1", "compression", "Compression"), ("2", "quota", "Quota"), ("3", "reservation", "Reservation"),
               ("4", "recordsize", "Record size"), ("5", "atime", "Access times"), ("6", "mountpoint", "Mountpoint"),
               ("7", "readonly", "Read-only")]
VOL_SETTINGS = {"compression", "readonly"}


def settings_screen(name: str) -> None:
    msg = Msg()
    while True:
        ds = zfs.get_dataset(name)
        if ds is None:
            raise Gone("dataset")
        props = zfs.dataset_props(name, [p for _, p, _ in FS_SETTINGS])
        opts = []
        for key, prop, label in FS_SETTINGS:
            val, src = props.get(prop, ("-", "-"))
            src_t = _source_text(src)
            text = f"{label}: {_fmt_prop(prop, val)}" + (f" ({src_t})" if src_t else "")
            opts.append(Opt(key, text, show=(prop in VOL_SETTINGS) if ds.is_volume else True))
        header = [paint(f"Settings of {name}", "title"),
                  paint("Settings not set here come from the parent dataset (inherited) or ZFS's default.", "dim")]
        key = menu(header, opts, msg)
        entry = next((e for e in FS_SETTINGS if e[0] == key), None)
        if entry is None:
            continue
        _, prop, label = entry
        val, src = props.get(prop, ("-", "-"))
        try:
            change_setting(msg, ds, prop, label, val, src)
        except Cancel:
            msg.warn(CANCELLED)
        except Leave as lv:
            msg.set(lv.message, lv.style)


def change_setting(msg: Msg, ds: M.Dataset, prop: str, label: str, val: str, src: str) -> None:
    reset_label = "Reset to default" if ds.is_root else f"Inherit from {ds.parent}"
    if src in ("default", "-") or src.startswith("inherited"):
        reset_label = ""
    notes: List[object] = []
    if prop == "compression":
        new = _value_choice(f"{label} for {ds.name}", COMPRESSION_CHOICES, val, reset_label,
                            ["Applies to data written from now on; existing data stays as it is."])
        notes.append(("Only data written from now on is affected.", "dim"))
    elif prop == "recordsize":
        new = _value_choice(f"{label} for {ds.name}", RECORDSIZE_CHOICES, _fmt_prop(prop, val), reset_label,
                            ["The largest block ZFS uses for files. Applies to files written from now on."])
        notes.append(("Only files written from now on use the new size.", "dim"))
    elif prop in ("atime", "readonly"):
        new = _value_choice(f"{label} for {ds.name}", ONOFF, val, reset_label,
                            ["Access times record each file read; off saves writes."] if prop == "atime"
                            else ["Read-only blocks all changes to files in this dataset."])
    elif prop in ("quota", "reservation"):
        what = ("The most space this dataset and everything below it may use."
                if prop == "quota" else "Space set aside for this dataset that others cannot take.")
        print(paint(what, "dim"))
        n = _ask_amount(f"{label} (e.g. 500G, 2T; 'none' removes it; blank cancels): ", allow_none=True)
        new = "none" if n is None else str(n)
        if n is None and val in ("0", "none", "-"):
            raise Cancel()
    elif prop == "mountpoint":
        j = choose(f"Mountpoint for {ds.name}", ["Custom path", "none (not mounted)"]
                   + ([reset_label] if reset_label else []), explain=[
                       f"Now: {val}", "Changing it unmounts the dataset and mounts it at the new place;",
                       "programs and shares using the old path stop finding the files."])
        if j is None:
            raise Cancel()
        if j == 0:
            raw = ask_text("New mountpoint (absolute path): ")
            if not raw.startswith("/") or " " in raw:
                raise Leave("A mountpoint is an absolute path like /srv/data.", "warn")
            new = raw
        elif j == 1:
            new = "none"
        else:
            new = ""
        notes.append((f"Files move from {val} to the new place; anything using the old path breaks.", "warn"))
    else:
        return
    if new is None:
        raise Cancel()
    args = ["zfs", "inherit", prop, ds.name] if new == "" else ["zfs", "set", f"{prop}={new}", ds.name]
    shown = "inherited/default" if new == "" else _fmt_prop(prop, new)
    preview(f"Change {label.lower()} of {ds.name}", [f"{label}: {_fmt_prop(prop, val)} → {shown}"] + notes, [args])
    risky = prop in ("mountpoint", "readonly")
    if not confirm(f"Change {label.lower()}?", not risky):
        raise Cancel()
    if zfs.get_dataset(ds.name) is None:
        raise Gone("dataset")
    quick(msg, args)


# ---------------------------------------------------------------- create


def new_dataset_flow(msg: Msg, parent: M.Dataset) -> None:
    print()
    print(paint("Ctrl-C cancels.", "dim"))
    child = ask_text(f"Name: {parent.name}/")
    if not child:
        raise Cancel()
    err = M.check_child_path(child)
    if err:
        raise Leave(err, "warn")
    full = f"{parent.name}/{child}"
    if zfs.get_dataset(full) is not None:
        raise Leave(f"{full} already exists.", "warn")
    pprops = zfs.dataset_props(parent.name, ["compression", "recordsize", "mountpoint"])
    p_comp = pprops.get("compression", ("lz4", ""))[0]
    p_rec = _fmt_prop("recordsize", pprops.get("recordsize", ("131072", ""))[0])
    p_mnt = pprops.get("mountpoint", ("", ""))[0]
    inherited_mnt = f"{p_mnt.rstrip('/')}/{child}" if p_mnt.startswith("/") else p_mnt
    sets: Dict[str, str] = {}
    encrypt = False
    smsg = Msg()
    while True:
        def shown(prop: str, inherit_val: str) -> str:
            if prop in sets:
                return _fmt_prop(prop, sets[prop])
            return f"{inherit_val} (inherited)"
        enc_text = (f"inherited ({parent.encryption}, root {parent.encroot})" if parent.encrypted
                    else ("on (passphrase)" if encrypt else "off"))
        header = [paint(f"New dataset {full}", "title"),
                  paint(f"Unchanged settings are inherited from {parent.name}.", "dim")]
        opts = [
            Opt("1", "Create"),
            Opt("2", f"Compression:  {shown('compression', p_comp)}"),
            Opt("3", f"Quota:        {_fmt_prop('quota', sets.get('quota', 'none'))}"),
            Opt("4", f"Record size:  {shown('recordsize', p_rec)}"),
            Opt("5", f"Mountpoint:   {shown('mountpoint', inherited_mnt)}"),
            Opt("6", f"Encryption:   {enc_text}"),
        ]
        try:
            key = menu(header, opts, smsg)
        except Back:
            raise Cancel()
        try:
            if key == "1":
                break
            if key == "2":
                v = _value_choice("Compression", COMPRESSION_CHOICES, sets.get("compression", p_comp),
                                  f"Inherit ({p_comp})")
                _apply(sets, "compression", v)
            elif key == "3":
                n = _ask_amount("Quota (e.g. 500G, 2T; blank or 'none' for no quota): ", allow_none=True)
                _apply(sets, "quota", "" if n is None else str(n))
            elif key == "4":
                v = _value_choice("Record size", RECORDSIZE_CHOICES, sets.get("recordsize", p_rec),
                                  f"Inherit ({p_rec})")
                _apply(sets, "recordsize", v)
            elif key == "5":
                j = choose("Mountpoint", [f"Inherit ({inherited_mnt})", "Custom path", "none (not mounted)"])
                if j == 0:
                    sets.pop("mountpoint", None)
                elif j == 1:
                    raw = ask_text("Mountpoint (absolute path): ")
                    if raw.startswith("/") and " " not in raw:
                        sets["mountpoint"] = raw
                    else:
                        smsg.warn("A mountpoint is an absolute path like /srv/data.")
                elif j == 2:
                    sets["mountpoint"] = "none"
            elif key == "6":
                if parent.encrypted:
                    smsg.warn(f"Encryption is inherited from {parent.encroot} and cannot be changed here.")
                else:
                    j = choose("Encryption", ["off", "on: zfs asks for a passphrase when it creates the dataset"],
                               1 if encrypt else 0, explain=[
                                   "Encryption cannot be turned off later.",
                                   "Without the passphrase the data cannot be recovered.",
                                   "After a reboot the key must be loaded (Load key) before it mounts."])
                    if j is not None:
                        encrypt = j == 1
        except Cancel:
            smsg.warn(CANCELLED)
        except Leave as lv:
            smsg.set(lv.message, lv.style)
    args = ["zfs", "create"]
    for k, v in sets.items():
        args += ["-o", f"{k}={v}"]
    if encrypt:
        args += ["-o", "encryption=on", "-o", "keyformat=passphrase", "-o", "keylocation=prompt"]
    args.append(full)
    preview(f"Create {full}", [f"Creates a new dataset under {parent.name}."]
            + ([("zfs will ask for the new passphrase twice.", "warn")] if encrypt else []), [args])
    if not confirm(f"Create {full}?", True):
        raise Cancel()
    if zfs.get_dataset(parent.name) is None or zfs.get_dataset(full) is not None:
        changed(msg, parent.name, "created")
        return
    if encrypt:
        ok, reason = attached(f"Creating {full}", [], [("zfs create", args)])
        if not ok:
            msg.bad(f"Failed: {reason}")
            return
    elif not quick(msg, args):
        return
    nav(msg, dataset_view, full, "Done.")


def _apply(sets: Dict[str, str], prop: str, value: Optional[str]) -> None:
    """None: backed out, keep as is. '': inherit (drop the setting). Else set it."""
    if value is None:
        return
    if value == "":
        sets.pop(prop, None)
    else:
        sets[prop] = value


def new_volume_flow(msg: Msg, parent: M.Dataset) -> None:
    print()
    print(paint("Ctrl-C cancels.", "dim"))
    child = ask_text(f"Volume name: {parent.name}/")
    if not child:
        raise Cancel()
    err = M.check_child_path(child)
    if err:
        raise Leave(err, "warn")
    full = f"{parent.name}/{child}"
    if zfs.get_dataset(full) is not None:
        raise Leave(f"{full} already exists.", "warn")
    size = _ask_amount("Size (e.g. 100G, 1T): ", allow_none=False)
    size = _align(size or 0)
    if size <= 0:
        raise Leave("A volume needs a size.", "warn")
    thin = False
    comp = ""
    smsg = Msg()
    while True:
        header = [paint(f"New volume {full}", "title"),
                  paint("A volume is a block device (/dev/zvol/...) for VMs, iSCSI or swap-like uses.", "dim")]
        opts = [Opt("1", "Create"), Opt("2", f"Size: {fmt_size(size)}"),
                Opt("3", f"Thin provisioned: {'yes' if thin else 'no'}"),
                Opt("4", f"Compression: {comp or 'inherited'}")]
        try:
            key = menu(header, opts, smsg)
        except Back:
            raise Cancel()
        try:
            if key == "1":
                break
            if key == "2":
                n = _ask_amount("Size (e.g. 100G, 1T): ", allow_none=False)
                size = _align(n or size)
            elif key == "3":
                j = choose("Thin provisioning", ["no: reserve the full size now (safe)",
                                                 "yes: take space only as it is written (can run out)"], 1 if thin else 0)
                if j is not None:
                    thin = j == 1
            elif key == "4":
                v = _value_choice("Compression", COMPRESSION_CHOICES, comp, "Inherit")
                if v is not None:
                    comp = v
        except Cancel:
            smsg.warn(CANCELLED)
        except Leave as lv:
            smsg.set(lv.message, lv.style)
    args = ["zfs", "create", "-V", str(size)] + (["-s"] if thin else []) + \
           (["-o", f"compression={comp}"] if comp else []) + [full]
    lines: List[object] = [f"Creates a {fmt_size(size)} volume at /dev/zvol/{full}."]
    if thin:
        lines.append(("Thin: if the pool fills up, writes to the volume fail.", "warn"))
    preview(f"Create {full}", lines, [args])
    if not confirm(f"Create {full}?", True):
        raise Cancel()
    if zfs.get_dataset(full) is not None:
        changed(msg, full, "created")
        return
    if quick(msg, args):
        nav(msg, volume_view, full, "Done.")


def _align(n: int) -> int:
    return ((n + VOL_ALIGN - 1) // VOL_ALIGN) * VOL_ALIGN


def grow_flow(msg: Msg, ds: M.Dataset) -> None:
    print()
    print(paint(f"Now {fmt_size(ds.volsize)}. zman only grows volumes; shrinking would cut off data.", "dim"))
    n = _ask_amount("New size (e.g. 200G): ", allow_none=False)
    new = _align(n or 0)
    if not ds.volsize or new <= ds.volsize:
        raise Leave(f"The new size must be larger than {fmt_size(ds.volsize)}.", "warn")
    args = ["zfs", "set", f"volsize={new}", ds.name]
    preview(f"Grow {ds.name}", [f"Grows the volume from {fmt_size(ds.volsize)} to {fmt_size(new)}.",
                                ("Whatever uses it must still grow its own partition and filesystem.", "dim")], [args])
    if not confirm(f"Grow {ds.name}?", True):
        raise Cancel()
    cur = zfs.get_dataset(ds.name)
    if cur is None or cur.volsize != ds.volsize:
        changed(msg, ds.name, "resized")
        return
    quick(msg, args)


# ---------------------------------------------------------------- rename, keys, destroy


def rename_flow(msg: Msg, ds: M.Dataset) -> str:
    try:
        print()
        print(paint(f"Now {ds.name}. Type the new name below the pool; Ctrl-C cancels.", "dim"))
        rest = ask_text(f"New name: {ds.pool}/")
        if not rest:
            raise Cancel()
        err = M.check_child_path(rest)
        if err:
            msg.warn(err)
            return ""
        new = f"{ds.pool}/{rest}"
        if new == ds.name:
            raise Cancel()
        if new.startswith(ds.name + "/"):
            msg.warn("A dataset cannot move inside itself.")
            return ""
        props = zfs.dataset_props(ds.name, ["mountpoint"])
        mnt, src = props.get("mountpoint", ("", ""))
        args = ["zfs", "rename", ds.name, new]
        lines: List[object] = [f"Renames {ds.name} to {new}. Its snapshots and children move with it."]
        if not ds.is_volume and mnt.startswith("/") and src != "local":
            lines.append((f"Its mountpoint is inherited, so its files move from {mnt} to the new place;", "warn"))
            lines.append(("programs and shares using the old path stop finding them.", "warn"))
        if ds.is_volume:
            lines.append((f"Its device moves to /dev/zvol/{new}; update whatever uses it.", "warn"))
        preview(f"Rename {ds.name}", lines, [args])
        if not confirm(f"Rename to {new}?", False):
            raise Cancel()
        cur = zfs.get_dataset(ds.name)
        if cur is None or cur.guid != ds.guid or zfs.get_dataset(new) is not None:
            changed(msg, ds.name, "renamed")
            return ""
        if quick(msg, args):
            return new
        return ""
    except Cancel:
        msg.warn(CANCELLED)
        return ""


def load_key_flow(msg: Msg, ds: M.Dataset) -> None:
    under, _ = zfs.list_datasets(root=ds.name)
    to_mount = [d for d in under if d.encroot == ds.name and not d.is_volume and d.has_mount_path
                and d.canmount == "on" and not d.mounted]
    steps = [("load key", ["zfs", "load-key", ds.name])] + [(f"mount {d.name}", ["zfs", "mount", d.name]) for d in to_mount]
    ok, reason = attached(f"Load key of {ds.name}", [
        "zfs asks for the passphrase, then zman mounts the datasets that use this key."], steps)
    msg.ok("Done.") if ok else msg.bad(f"Failed: {reason}")


def unload_key_flow(msg: Msg, ds: M.Dataset) -> None:
    under, _ = zfs.list_datasets(root=ds.name)
    mounted = sorted([d for d in under if d.encroot == ds.name and d.mounted], key=lambda d: -d.name.count("/"))
    steps = [(f"unmount {d.name}", ["zfs", "unmount", d.name]) for d in mounted]
    steps.append(("unload key", ["zfs", "unload-key", ds.name]))
    lines: List[object] = ["Locks the data: nothing using this key can be read until the key is loaded again."]
    if mounted:
        lines.append(f"First unmounts: {', '.join(d.name for d in mounted)}")
    preview(f"Unload key of {ds.name}", lines, [a for _, a in steps])
    if not confirm(f"Unload the key of {ds.name}?", False):
        raise Cancel()
    ok, reason = attached(f"Unload key of {ds.name}", [], steps)
    msg.ok("Done.") if ok else msg.bad(f"Failed: {reason}")


def destroy_blockers(ds: M.Dataset, snaps: Sequence[M.Snapshot]) -> List[str]:
    """Reasons zman will not destroy a dataset tree: held snapshots, clones outside it."""
    out = []
    inside = ds.name + "/"
    for s in snaps:
        if s.holds:
            out.append(f"{s.name} has a hold")
        for c in s.clones:
            if c != ds.name and not c.startswith(inside):
                out.append(f"{c} is a clone of {s.name}")
    return out


def destroy_flow(msg: Msg, ds: M.Dataset) -> None:
    snaps, _ = zfs.list_snapshots(dataset=ds.name, recursive=True)
    names, _, err = zfs.destroy_preview(ds.name, recursive=True)
    blockers = destroy_blockers(ds, snaps)
    kind = "volume" if ds.is_volume else "dataset"
    if blockers:
        preview(f"Destroy {ds.name}", [("zman will not destroy it:", "bad")]
                + [f"  {b}" for b in blockers[:12]]
                + ["", ("Release the holds, or destroy or promote the clones, first.", "dim")])
        wait_enter()
        raise Cancel()
    sub = [n for n in names if "@" not in n and n != ds.name]
    nsnap = len([n for n in names if "@" in n])
    args = ["zfs", "destroy"] + (["-r"] if (sub or nsnap) else []) + [ds.name]
    lines: List[object] = [(f"Destroys {ds.name} and all its data:", "bad"),
                           f"  {fmt_size(ds.used)} · {plural(len(sub), 'dataset')} below it · {plural(nsnap, 'snapshot')}"]
    for n in sub[:8]:
        lines.append(f"  {n}")
    if len(sub) > 8:
        lines.append(f"  ... and {len(sub) - 8} more")
    if ds.mounted:
        lines.append(("It is unmounted first; programs and shares using it must close it.", "warn"))
    if err:
        lines.append((f"ZFS said: {err}", "warn"))
    lines.append(("This cannot be undone.", "bad"))
    preview(f"Destroy {kind} {ds.name}", lines, [args])
    if not typed_confirm(ds.leaf, f"destroy {ds.name} and its data"):
        raise Cancel()
    names2, _, _ = zfs.destroy_preview(ds.name, recursive=True)
    snaps2, _ = zfs.list_snapshots(dataset=ds.name, recursive=True)
    if sorted(names2) != sorted(names) or destroy_blockers(ds, snaps2):
        changed(msg, ds.name, "destroyed")
        return
    report(msg, run(args, timeout=CONFIG.long_timeout), f"Removed {kind} {ds.name}")
