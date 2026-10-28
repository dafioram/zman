#!/usr/bin/env python3
"""A fake zfs / zpool / lsblk / sfdisk / udevadm for the walkthrough.

State lives in a JSON file ($ZMAN_MOCK_STATE). Outputs follow the formats of the
real tools in scripted (-H -p) mode and of `zpool status` text, so zman's parsers
run against them unchanged. Only what zman calls is implemented.
"""
import json
import os
import sys
import time

STATE = os.environ.get("ZMAN_MOCK_STATE", "")
ROOT = os.path.dirname(STATE)
GIB = 1024 ** 3

DS_DEFAULTS = {
    "compression": "off", "recordsize": "131072", "atime": "on", "readonly": "off", "quota": "0",
    "reservation": "0", "refquota": "0", "refreservation": "0", "canmount": "on", "volblocksize": "16384",
    "encryption": "off", "keyformat": "none", "keylocation": "none",
}
INHERITABLE = {"compression", "recordsize", "atime", "readonly", "mountpoint", "encryption"}


def load():
    with open(STATE) as f:
        return json.load(f)


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def die(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def new_guid(s):
    s["guid_counter"] = s.get("guid_counter", 1000000000000000000) + 7919
    return str(s["guid_counter"])


def next_txg(s):
    s["txg"] = s.get("txg", 1000) + 1
    return s["txg"]


def log_history(s, pool, argv):
    s["pools"][pool].setdefault("history", []).append(
        time.strftime("%Y-%m-%d.%H:%M:%S") + " " + " ".join(argv))


# ------------------------------------------------------------------ pool tree helpers

def iter_leaves(node):
    if node.get("type") == "disk":
        yield node
    for ch in node.get("children", []):
        yield from iter_leaves(ch)


def all_vdev_lists(p):
    return [("data", p["vdevs"]), ("special", p.get("special", [])), ("logs", p.get("logs", [])),
            ("cache", p.get("cache", [])), ("spares", p.get("spares", []))]


def all_leaves(p):
    for _, lst in all_vdev_lists(p):
        for v in lst:
            yield from iter_leaves(v)


BAD = {"UNAVAIL", "FAULTED", "REMOVED", "OFFLINE"}


def node_state(node):
    if node.get("type") == "disk":
        return node.get("state", "ONLINE")
    kids = [node_state(c) for c in node.get("children", [])]
    bad = [k for k in kids if k in BAD or k == "DEGRADED"]
    t = node["type"]
    if not bad:
        return "ONLINE"
    good = len(kids) - len([k for k in kids if k in BAD])
    if t in ("mirror", "replacing", "spare"):
        return "DEGRADED" if good >= 1 else "UNAVAIL"
    if t.startswith("raidz"):
        par = int(t[-1]) if t[-1].isdigit() else 1
        return "DEGRADED" if len(kids) - good <= par else "UNAVAIL"
    return "DEGRADED"


def pool_state(p):
    if p.get("force_state"):
        return p["force_state"]
    states = [node_state(v) for v in p["vdevs"]]
    if any(st in BAD for st in states):
        return "UNAVAIL"
    if any(st == "DEGRADED" for st in states):
        return "DEGRADED"
    return "ONLINE"


def find_leaf(p, key):
    for _, lst in all_vdev_lists(p):
        for top in lst:
            for parent, node in walk(top, None):
                if node.get("type") == "disk" and key in (node.get("guid"), node.get("path"),
                                                          os.path.basename(node.get("path", ""))):
                    return parent, node, lst
                if node.get("type") != "disk" and key in (node.get("guid"), node.get("name")):
                    return parent, node, lst
    return None, None, None


def walk(node, parent):
    yield parent, node
    for ch in node.get("children", []):
        yield from walk(ch, node)


def renumber(p):
    """mirror-0, raidz1-1, replacing-2 ... numbered by position like ZFS does."""
    for idx, top in enumerate(p["vdevs"]):
        if top["type"] != "disk":
            top["name"] = f"{top['type']}-{idx}"
        for j, ch in enumerate(top.get("children", [])):
            if ch["type"] in ("replacing", "spare"):
                ch["name"] = f"{ch['type']}-{j}"


# ------------------------------------------------------------------ disks

def disk_by_path(s, path):
    real = os.path.realpath(path)
    base = os.path.basename(real)
    for d in s["disks"]:
        if d["name"] == base:
            return d, None
        for pt in d.get("parts", []):
            if pt["name"] == base:
                return d, pt
    return None, None


def part_name(d, n):
    return f"{d['name']}p{n}" if d["name"][-1].isdigit() else f"{d['name']}{n}"


def make_part(s, d, n, size, fstype="", label=""):
    name = part_name(d, n)
    d.setdefault("parts", [])
    d["parts"] = [p for p in d["parts"] if p["name"] != name]
    d["parts"].append({"name": name, "size": size, "fstype": fstype, "label": label})
    open(os.path.join(ROOT, "dev", name), "w").close()
    link = os.path.join(ROOT, "by-id", f"{d['by_id']}-part{n}")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.path.join("..", "dev", name), link)
    return link


def claim_disk(s, path, pool):
    """Whole disk → ZFS partitions it (part1 + part9). Partition → just label it."""
    d, pt = disk_by_path(s, path)
    if d is None:
        die(f"cannot open '{path}': no such device in /dev\nmust be a full path or shorthand device name")
    for other in s["pools"].values():
        for leaf in all_leaves(other):
            dd, _ = disk_by_path(s, leaf.get("path", ""))
            if dd is d and leaf.get("state") != "UNAVAIL":
                die(f"{path} is part of active pool '{other['name']}'")
    if pt is not None:
        pt["fstype"], pt["label"] = "zfs_member", pool
        return path, pt["size"]
    d["parts"] = []
    d["fstype"] = ""
    link = make_part(s, d, 1, d["size"] - 9 * 1024 * 1024, "zfs_member", pool)
    make_part(s, d, 9, 8 * 1024 * 1024)
    return link, d["size"] - 9 * 1024 * 1024


def release_disk_labels(s, path):
    d, pt = disk_by_path(s, path)
    if pt is not None:
        pt["fstype"], pt["label"] = "", ""


# ------------------------------------------------------------------ status text

def counters(node):
    return node.get("r", 0), node.get("w", 0), node.get("c", 0)


def status_text(s, p, guids):
    out = []
    st = pool_state(p)
    out.append(f"  pool: {p['name']}")
    out.append(f" state: {st}")
    leaves = list(all_leaves(p))
    if any(l.get("state") == "FAULTED" for l in leaves):
        out.append("status: One or more devices are faulted in response to persistent errors.")
        out.append("\tSufficient replicas exist for the pool to continue functioning in a")
        out.append("\tdegraded state.")
        out.append("action: Replace the faulted device, or use 'zpool clear' to mark the device")
        out.append("\trepaired.")
    elif any(l.get("state") == "UNAVAIL" for l in leaves):
        out.append("status: One or more devices could not be used because the label is missing or")
        out.append("\tinvalid.  Sufficient replicas exist for the pool to continue")
        out.append("\tfunctioning in a degraded state.")
        out.append("action: Replace the device using 'zpool replace'.")
        out.append("   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-4J")
    elif any(v[0] == "disabled" for k, v in p["props"].items() if k.startswith("feature@")):
        out.append("status: Some supported and requested features are not enabled on the pool.")
        out.append("\tThe pool can still be used, but some features are unavailable.")
        out.append("action: Enable all features using 'zpool upgrade'. Once this is done,")
        out.append("\tthe pool may no longer be accessible by software that does not support")
        out.append("\tthe features. See zpool-features(7) for details.")
    scan = p.get("scan") or {}
    out += scan_text(scan)
    out.append("config:")
    out.append("")
    rows = []

    def add(node, depth):
        if node["type"] == "disk":
            path = node["path"]
            st_ = node.get("state", "ONLINE")
            if guids:
                name = node["guid"]
            elif st_ in ("UNAVAIL",) and node.get("missing"):
                name = node["guid"]
            else:
                name = path
            note = node.get("note", "")
            if node.get("missing") and not guids:
                note = (note + " " if note else "") + f"was {path}"
            rows.append((depth, name, st_, counters(node), note))
        else:
            name = node["guid"] if guids else node["name"]
            rows.append((depth, name, node_state(node), counters(node), ""))
            for ch in node.get("children", []):
                add(ch, depth + 1)

    rows.append((0, p["name"], st, (0, 0, 0), ""))
    for v in p["vdevs"]:
        add(v, 1)
    for sec in ("special", "logs", "cache"):
        if p.get(sec):
            rows.append((0, sec, None, None, ""))
            for v in p[sec]:
                add(v, 1)
    spares = p.get("spares", [])
    if spares:
        rows.append((0, "spares", None, None, ""))
        for v in spares:
            name = v["guid"] if guids else v["path"]
            rows.append((1, name, v.get("state", "AVAIL"), None, v.get("note", "")))
    w = max(len("  " * d + n) for d, n, *_ in rows) + 2
    out.append("\t" + "NAME".ljust(w) + "STATE     READ WRITE CKSUM")
    for depth, name, st_, cnt, note in rows:
        line = "\t" + ("  " * depth + name).ljust(w)
        if st_ is not None:
            line += st_.ljust(8)
            if cnt is not None:
                line += "".join(str(x).rjust(6) for x in cnt)
            if note:
                line += "  " + note
        out.append(line.rstrip())
    out.append("")
    errs = p.get("data_errors", 0)
    out.append("errors: No known data errors" if not errs else f"errors: {errs} data errors, use '-v' for a list")
    return out


def scan_text(scan):
    k = scan.get("kind")
    if not k:
        return ["  scan: none requested"]
    st = scan["state"]
    if st == "running":
        pct = scan.get("pct", 0.0)
        lines = [f"  scan: {k} in progress since {scan['since']}",
                 "\t1.20T scanned at 1.50G/s, 600G issued at 750M/s, 2.40T total"]
        verb = "repaired" if k == "scrub" else "resilvered"
        lines.append(f"\t0B {verb}, {pct:.2f}% done, 00:40:00 to go")
        return lines
    if st == "paused":
        return [f"  scan: {k} paused since {scan['since']}",
                f"\t{k} started on {scan['since']}",
                "\t1.20T scanned, 600G issued, 2.40T total",
                f"\t0B repaired, {scan.get('pct', 0):.2f}% done"]
    if st == "canceled":
        return [f"  scan: {k} canceled on {scan['when']}"]
    if k == "scrub":
        return [f"  scan: scrub repaired {scan.get('repaired', '0B')} in 00:10:01 with {scan.get('errors', 0)} "
                f"errors on {scan['when']}"]
    return [f"  scan: resilvered {scan.get('repaired', '1.20G')} in 00:01:12 with {scan.get('errors', 0)} "
            f"errors on {scan['when']}"]


def advance_scans(s):
    """Each status read moves a running scrub/resilver forward, so the walkthrough sees it end."""
    changed_ = False
    now = time.strftime("%a %b %d %H:%M:%S %Y")
    for p in s["pools"].values():
        scan = p.get("scan") or {}
        if scan.get("state") != "running" or p.get("frozen_scan"):
            continue
        scan["pct"] = min(100.0, scan.get("pct", 0.0) + 34.0)
        changed_ = True
        if scan["pct"] >= 100.0:
            scan.update(state="finished", when=now, errors=0, repaired="0B" if scan["kind"] == "scrub" else "1.20G")
            if scan["kind"] == "resilver":
                finish_resilver(p)
    if changed_:
        save(s)


def finish_resilver(p):
    for _, lst in all_vdev_lists(p):
        for top in lst:
            for parent, node in list(walk(top, None)):
                if node.get("type") == "replacing":
                    new = node["children"][-1]
                    new.pop("note", None)
                    container = parent["children"] if parent else lst
                    container[container.index(node)] = new
                if node.get("type") == "disk" and "resilver" in node.get("note", ""):
                    node.pop("note", None)


# ------------------------------------------------------------------ datasets

def ds_get(s, name, prop):
    """(value, source) for a dataset property, with inheritance."""
    d = s["datasets"][name]
    local = d.get("props", {})
    if prop in local:
        return str(local[prop]), "local"
    if prop == "mountpoint":
        if d["type"] == "volume":
            return "-", "-"
        parent = name.rsplit("/", 1)[0] if "/" in name else None
        if parent:
            pv, psrc = ds_get(s, parent, "mountpoint")
            src = "inherited from " + (parent if psrc == "local" else psrc.replace("inherited from ", "")) \
                if psrc != "default" else "default"
            if pv.startswith("/"):
                return pv.rstrip("/") + "/" + name.rsplit("/", 1)[1], src
            return pv, src
        return "/" + name, "default"
    if prop in INHERITABLE and "/" in name:
        parent = name.rsplit("/", 1)[0]
        pv, psrc = ds_get(s, parent, prop)
        if psrc == "default":
            return pv, "default"
        if psrc == "local":
            return pv, f"inherited from {parent}"
        return pv, psrc
    if prop == "recordsize" and d["type"] == "volume":
        return "-", "-"
    return DS_DEFAULTS.get(prop, "-"), "default"


def children_of(s, name):
    return [n for n in s["datasets"] if n.startswith(name + "/")]


def snaps_of(s, name):
    return [n for n in s["snapshots"] if n.split("@")[0] == name]


def ds_used(s, name):
    d = s["datasets"][name]
    snap = sum(s["snapshots"][n]["used"] for n in snaps_of(s, name))
    child = sum(ds_used(s, c)[0] for c in children_of(s, name) if c.count("/") == name.count("/") + 1)
    refres = d.get("refres", 0)
    data = d.get("data", 0)
    return data + snap + child + refres, data, snap, child, refres


def encroot_of(s, name):
    n = name
    while True:
        local = s["datasets"][n].get("props", {})
        if local.get("encryption", "off") != "off":
            return n
        if "/" not in n:
            return ""
        n = n.rsplit("/", 1)[0]


def ds_value(s, name, prop):
    d = s["datasets"][name]
    pool = s["pools"][name.split("/")[0]]
    if prop == "name":
        return name
    if prop == "type":
        return d["type"]
    used, data, snap, child, refres = ds_used(s, name)
    if prop == "used":
        return str(used)
    if prop == "usedbydataset":
        return str(data)
    if prop == "usedbysnapshots":
        return str(snap)
    if prop == "usedbychildren":
        return str(child)
    if prop == "usedbyrefreservation":
        return str(refres)
    if prop == "available":
        free = pool["size"] - pool_alloc(s, pool["name"])
        q = int(ds_get(s, name, "quota")[0])
        if q:
            free = min(free, max(0, q - used))
        return str(max(0, free))
    if prop == "referenced":
        return str(d.get("refer", data))
    if prop == "mounted":
        if d["type"] == "volume":
            return "-"
        return "yes" if d.get("mounted") else "no"
    if prop == "compressratio":
        return d.get("ratio", "1.00")
    if prop == "volsize":
        return str(d["volsize"]) if d["type"] == "volume" else "-"
    if prop == "volblocksize":
        return "16384" if d["type"] == "volume" else "-"
    if prop == "encryptionroot":
        r = encroot_of(s, name)
        return r or "-"
    if prop == "encryption":
        r = encroot_of(s, name)
        return s["datasets"][r]["props"]["encryption"] if r else "off"
    if prop == "keystatus":
        r = encroot_of(s, name)
        if not r:
            return "-"
        return "unavailable" if s["datasets"][r].get("locked") else "available"
    if prop == "origin":
        return d.get("origin", "-")
    if prop == "creation":
        return str(d.get("creation", 1700000000))
    if prop == "guid":
        return d["guid"]
    if prop == "canmount":
        if d["type"] == "volume":
            return "-"
        return ds_get(s, name, prop)[0]
    if prop == "refreservation":
        return str(refres)
    return ds_get(s, name, prop)[0]


def pool_alloc(s, pool):
    return ds_used(s, pool)[0]


def ds_source(s, name, prop):
    if prop in ("name", "type", "used", "available", "referenced", "mounted", "compressratio", "creation",
                "guid", "usedbydataset", "usedbysnapshots", "usedbychildren", "usedbyrefreservation",
                "encryptionroot", "keystatus", "origin"):
        return "-"
    return ds_get(s, name, prop)[1]


def snap_value(s, name, prop):
    sn = s["snapshots"][name]
    if prop == "name":
        return name
    if prop == "used":
        return str(sn["used"])
    if prop == "referenced":
        return str(sn["refer"])
    if prop == "creation":
        return str(sn["creation"])
    if prop == "createtxg":
        return str(sn["txg"])
    if prop == "userrefs":
        return str(len(sn.get("holds", [])))
    if prop == "clones":
        clones = [n for n, d in s["datasets"].items() if d.get("origin") == name]
        return ",".join(clones)
    if prop == "guid":
        return sn["guid"]
    if prop == "defer_destroy":
        return "off"
    if prop == "type":
        return "snapshot"
    return "-"


# ------------------------------------------------------------------ zfs command

def opt_parse(args, flags_with_value=()):
    flags, opts, rest = set(), {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1 and not rest:
            if a in flags_with_value:
                opts.setdefault(a, []).append(args[i + 1])
                i += 2
                continue
            for ch in a[1:]:
                flags.add("-" + ch)
            i += 1
            continue
        rest.append(a)
        i += 1
    return flags, opts, rest


def zfs_cmd(argv):
    s = load()
    if not argv:
        die("usage: zfs command args ...", 2)
    cmd, args = argv[0], argv[1:]
    if cmd == "version":
        print("zfs-2.1.11-1\nzfs-kmod-2.1.11-1")
        return
    if cmd == "list":
        flags, opts, rest = opt_parse(args, ("-t", "-o", "-s", "-d", "-S"))
        types = ",".join(opts.get("-t", ["filesystem,volume"])).split(",")
        props = opts.get("-o", ["name,used,available,referenced,mountpoint"])[0].split(",")
        names = []
        if rest:
            target = rest[0]
            if "@" in target:
                if target not in s["snapshots"]:
                    die(f"cannot open '{target}': dataset does not exist")
            elif target not in s["datasets"]:
                die(f"cannot open '{target}': dataset does not exist")
            if "-r" in flags or "-d" in opts:
                depth = int(opts["-d"][0]) if "-d" in opts else 10 ** 6
                base = target.count("/")
                cand = [target] + children_of(s, target)
                cand = [c for c in cand if c.count("/") - base <= depth]
                for c in sorted(cand):
                    if "snapshot" in types:
                        if c.count("/") - base <= depth:
                            names += [n for n in snaps_of(s, c)]
                    if s["datasets"][c]["type"] in types:
                        names.append(c)
                if "snapshot" in types and "-d" in opts and depth == 1:
                    names = [n for n in names if "@" not in n or n.split("@")[0] == target] + \
                            [n for n in names if "@" not in n]
                    names = list(dict.fromkeys(names))
            else:
                names = [target]
        else:
            for n in sorted(s["datasets"]):
                if s["datasets"][n]["type"] in types:
                    names.append(n)
            if "snapshot" in types:
                names += list(s["snapshots"])
        if opts.get("-s") == ["createtxg"]:
            names.sort(key=lambda n: s["snapshots"][n]["txg"] if "@" in n else 0)
        for n in names:
            if "@" in n:
                if "snapshot" not in types and rest and n != rest[0]:
                    continue
                vals = [snap_value(s, n, p) for p in props]
            else:
                if s["datasets"][n]["type"] not in types and not (rest and n == rest[0] and "-r" not in flags):
                    continue
                vals = [ds_value(s, n, p) for p in props]
            print("\t".join(v if v != "" else "-" if p not in ("clones",) else "" for v, p in zip(vals, props)))
        return
    if cmd == "get":
        flags, opts, rest = opt_parse(args, ("-o", "-t", "-s"))
        props = rest[0].split(",")
        name = rest[1]
        if name not in s["datasets"]:
            die(f"cannot open '{name}': dataset does not exist")
        cols = opts.get("-o", ["name,property,value,source"])[0].split(",")
        if props == ["all"]:
            props = ["type", "creation", "used", "available", "referenced", "compressratio", "mounted",
                     "quota", "reservation", "recordsize", "mountpoint", "compression", "atime", "readonly",
                     "canmount", "encryption", "keystatus", "guid"]
            cols = cols if "-H" in flags else cols
        if "-H" not in flags:
            print("NAME  PROPERTY  VALUE  SOURCE")
        for p in props:
            row = {"name": name, "property": p, "value": ds_value(s, name, p), "source": ds_source(s, name, p)}
            print("\t".join(row[c] for c in cols))
        return
    if cmd == "holds":
        flags, _, rest = opt_parse(args)
        for n in rest:
            if n not in s["snapshots"]:
                die(f"cannot open '{n}': dataset does not exist")
            for t in s["snapshots"][n].get("holds", []):
                print(f"{n}\t{t}\tTue Oct 27 12:00 2026")
        return
    if cmd == "destroy":
        flags, _, rest = opt_parse(args)
        target = rest[0]
        dry = "-n" in flags
        if "@" in target:
            ds, snaps = target.split("@", 1)
            names = [f"{ds}@{x}" for x in snaps.split(",")]
            for n in names:
                if n not in s["snapshots"]:
                    die("could not find any snapshots to destroy; check snapshot names.")
                sn = s["snapshots"][n]
                if sn.get("holds"):
                    die(f"cannot destroy snapshot {n}: dataset is busy")
                if snap_value(s, n, "clones"):
                    die(f"cannot destroy '{n}': snapshot has dependent clones\nuse '-R' to destroy the following datasets:\n{snap_value(s, n, 'clones')}")
            if dry:
                for n in names:
                    print(f"destroy\t{n}")
                print(f"reclaim\t{sum(s['snapshots'][n]['used'] for n in names) + 4096 * len(names)}")
                return
            for n in names:
                del s["snapshots"][n]
            log_history(s, ds.split("/")[0], ["zfs"] + argv)
            save(s)
            return
        if target not in s["datasets"]:
            die(f"cannot open '{target}': dataset does not exist")
        tree = [target] + children_of(s, target)
        snaps = [n for t in tree for n in snaps_of(s, t)]
        if (len(tree) > 1 or snaps) and "-r" not in flags:
            die(f"cannot destroy '{target}': filesystem has children\nuse '-r' to destroy the following datasets:")
        if dry:
            for n in snaps + sorted(tree, key=lambda x: -x.count("/")):
                print(f"destroy\t{n}")
            return
        for t in tree:
            if s["datasets"][t].get("busy"):
                die(f"cannot unmount '/{t}': pool or dataset is busy")
        for n in snaps:
            del s["snapshots"][n]
        for t in tree:
            del s["datasets"][t]
        log_history(s, target.split("/")[0], ["zfs"] + argv)
        save(s)
        return
    if cmd == "snapshot":
        flags, opts, rest = opt_parse(args, ("-o",))
        txg = next_txg(s)
        for target in rest:
            ds, snap = target.split("@", 1)
            if ds not in s["datasets"]:
                die(f"cannot open '{ds}': dataset does not exist")
            tree = [ds] + (children_of(s, ds) if "-r" in flags else [])
            for t in tree:
                n = f"{t}@{snap}"
                if n in s["snapshots"]:
                    die(f"cannot create snapshot '{n}': dataset already exists")
                s["snapshots"][n] = {"used": 0, "refer": s["datasets"][t].get("data", 0), "creation": int(time.time()),
                                     "txg": txg, "guid": new_guid(s), "holds": []}
        log_history(s, rest[0].split("/")[0].split("@")[0], ["zfs"] + argv)
        save(s)
        return
    if cmd == "create":
        flags, opts, rest = opt_parse(args, ("-o", "-V"))
        name = rest[0]
        if name in s["datasets"]:
            die(f"cannot create '{name}': dataset already exists")
        parent = name.rsplit("/", 1)[0]
        if parent not in s["datasets"]:
            die(f"cannot create '{name}': parent does not exist")
        props = dict(o.split("=", 1) for o in opts.get("-o", []))
        if props.get("encryption") == "on":
            props["encryption"] = "aes-256-gcm"
            if not read_passphrase():
                die("Passphrases do not match.")
        if parent and encroot_of(s, parent) and s["datasets"][encroot_of(s, parent)].get("locked"):
            die(f"cannot create '{name}': encryption root's key is not loaded or provided")
        d = {"type": "volume" if "-V" in opts else "filesystem", "props": props, "data": 0, "refer": 0,
             "guid": new_guid(s), "creation": int(time.time()), "mounted": "-V" not in opts}
        if "-V" in opts:
            d["volsize"] = int(opts["-V"][0])
            d["refres"] = 0 if "-s" in flags else d["volsize"]
        s["datasets"][name] = d
        log_history(s, name.split("/")[0], ["zfs"] + argv)
        save(s)
        return
    if cmd in ("set", "inherit"):
        flags, _, rest = opt_parse(args)
        if cmd == "set":
            kv, name = rest[0], rest[1]
            k, v = kv.split("=", 1)
        else:
            k, name = rest[0], rest[1]
            v = None
        if name not in s["datasets"]:
            die(f"cannot open '{name}': dataset does not exist")
        d = s["datasets"][name]
        if k == "volsize":
            d["volsize"] = int(v)
        elif k == "quota" and v not in (None, "none", "0") and int(v) < ds_used(s, name)[0]:
            die(f"cannot set property for '{name}': size is less than current used or reserved space")
        elif v is None or (k in ("quota", "reservation") and v in ("none", "0")):
            d.setdefault("props", {}).pop(k, None)
        else:
            d.setdefault("props", {})[k] = v
        log_history(s, name.split("/")[0], ["zfs"] + argv)
        save(s)
        return
    if cmd == "rename":
        _, _, rest = opt_parse(args)
        old, new = rest
        if "@" in old:
            if old not in s["snapshots"]:
                die(f"cannot open '{old}': dataset does not exist")
            if new in s["snapshots"]:
                die(f"cannot rename to '{new}': dataset already exists")
            s["snapshots"][new] = s["snapshots"].pop(old)
        else:
            if old not in s["datasets"]:
                die(f"cannot open '{old}': dataset does not exist")
            if new in s["datasets"]:
                die(f"cannot rename to '{new}': dataset already exists")
            moves = [old] + children_of(s, old)
            for m in moves:
                nn = new + m[len(old):]
                s["datasets"][nn] = s["datasets"].pop(m)
                for sn in snaps_of(s, m):
                    s["snapshots"][nn + "@" + sn.split("@")[1]] = s["snapshots"].pop(sn)
        save(s)
        return
    if cmd == "clone":
        _, _, rest = opt_parse(args)
        snap, new = rest
        if snap not in s["snapshots"]:
            die(f"cannot open '{snap}': dataset does not exist")
        src = s["datasets"][snap.split("@")[0]]
        s["datasets"][new] = {"type": src["type"], "props": {}, "data": 0, "refer": s["snapshots"][snap]["refer"],
                              "guid": new_guid(s), "origin": snap, "creation": int(time.time()), "mounted": True}
        if src["type"] == "volume":
            s["datasets"][new]["volsize"] = src["volsize"]
        save(s)
        return
    if cmd == "rollback":
        flags, _, rest = opt_parse(args)
        snap = rest[0]
        ds = snap.split("@")[0]
        txg = s["snapshots"][snap]["txg"]
        newer = [n for n in snaps_of(s, ds) if s["snapshots"][n]["txg"] > txg]
        if newer and "-r" not in flags:
            die(f"cannot rollback to '{snap}': more recent snapshots or bookmarks exist\nuse '-r' to force deletion")
        for n in newer:
            del s["snapshots"][n]
        save(s)
        return
    if cmd in ("hold", "release"):
        tag, snap = args[-2], args[-1]
        if snap not in s["snapshots"]:
            die(f"cannot open '{snap}': dataset does not exist")
        holds = s["snapshots"][snap].setdefault("holds", [])
        if cmd == "hold":
            if tag in holds:
                die(f"cannot hold snapshot '{snap}': tag already exists on this dataset")
            holds.append(tag)
        else:
            if tag not in holds:
                die(f"cannot release hold from snapshot '{snap}': no such tag on this dataset")
            holds.remove(tag)
        save(s)
        return
    if cmd in ("mount", "unmount"):
        name = args[-1]
        if name not in s["datasets"]:
            die(f"cannot open '{name}': dataset does not exist")
        d = s["datasets"][name]
        if cmd == "unmount" and d.get("busy"):
            die(f"cannot unmount '/{name}': pool or dataset is busy")
        if cmd == "mount":
            r = encroot_of(s, name)
            if r and s["datasets"][r].get("locked"):
                die(f"cannot mount '{name}': encryption key not loaded")
        d["mounted"] = cmd == "mount"
        save(s)
        return
    if cmd == "load-key":
        name = args[-1]
        if not read_passphrase(confirm=False):
            die("Key load error: Incorrect key provided for 'x'.")
        s["datasets"][name]["locked"] = False
        save(s)
        print("1 / 1 key(s) successfully loaded")
        return
    if cmd == "unload-key":
        name = args[-1]
        s["datasets"][name]["locked"] = True
        save(s)
        return
    if cmd == "diff":
        _, _, rest = opt_parse(args)
        ds = rest[0].split("@")[0]
        mp = ds_get(s, ds, "mountpoint")[0]
        print(f"M\t/\t{mp}/")
        print(f"+\tF\t{mp}/new-report.txt")
        print(f"-\tF\t{mp}/old-notes.txt")
        print(f"R\tF\t{mp}/a.txt -> {mp}/b.txt")
        return
    die(f"mock zfs: unsupported command {cmd}", 2)


def read_passphrase(confirm=True):
    try:
        sys.stdout.write("Enter passphrase: ")
        sys.stdout.flush()
        a = sys.stdin.readline().strip()
        if confirm:
            sys.stdout.write("Re-enter passphrase: ")
            sys.stdout.flush()
            b = sys.stdin.readline().strip()
            return a and a == b
        return bool(a)
    except KeyboardInterrupt:
        return False


# ------------------------------------------------------------------ zpool command

def zpool_cmd(argv):
    s = load()
    cmd, args = argv[0], argv[1:]
    pools = s["pools"]

    def need_pool(name):
        if name not in pools:
            die(f"cannot open '{name}': no such pool")
        return pools[name]

    if cmd == "list":
        flags, opts, rest = opt_parse(args, ("-o",))
        props = opts.get("-o", ["name"])[0].split(",")
        names = rest or sorted(pools)
        for n in names:
            p = need_pool(n)
            alloc = pool_alloc(s, n)
            vals = {"name": n, "size": str(p["size"]), "allocated": str(alloc), "free": str(p["size"] - alloc),
                    "fragmentation": str(p.get("frag", 3)), "capacity": str(alloc * 100 // p["size"]),
                    "health": pool_state(p), "guid": p["guid"]}
            print("\t".join(vals[x] for x in props))
        return
    if cmd == "status":
        flags, _, rest = opt_parse(args)
        advance_scans(s)
        names = rest or sorted(pools)
        out = []
        for n in names:
            out += status_text(s, need_pool(n), "-g" in flags)
            out.append("")
        if not names:
            print("no pools available")
            return
        print("\n".join(out))
        return
    if cmd == "get":
        flags, opts, rest = opt_parse(args, ("-o",))
        cols = opts.get("-o", ["name,property,value,source"])[0].split(",")
        p = need_pool(rest[1])
        alloc = pool_alloc(s, p["name"])
        base = {"size": str(p["size"]), "allocated": str(alloc), "free": str(p["size"] - alloc),
                "health": pool_state(p), "guid": p["guid"], "expandsize": p.get("expandsize", "-"),
                "compatibility": "off"}
        rows = [(k, v, "-") for k, v in base.items()]
        for k, v in p["props"].items():
            if isinstance(v, list):
                rows.append((k, v[0], v[1]))
            else:
                rows.append((k, str(v), "local"))
        for k, v, src in rows:
            row = {"name": p["name"], "property": k, "value": v, "source": src}
            print("\t".join(row[c] for c in cols))
        return
    if cmd == "scrub":
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        scan = p.get("scan") or {}
        if scan.get("kind") == "resilver" and scan.get("state") == "running":
            die(f"cannot scrub {p['name']}: currently resilvering")
        now = time.strftime("%a %b %d %H:%M:%S %Y")
        if "-s" in flags:
            if scan.get("state") not in ("running", "paused"):
                die(f"cannot cancel scrubbing {p['name']}: there is no active scrub")
            p["scan"] = {"kind": "scrub", "state": "canceled", "when": now}
        elif "-p" in flags:
            scan["state"] = "paused"
        else:
            if scan.get("state") == "running":
                die(f"cannot scrub {p['name']}: currently scrubbing; use 'zpool scrub -s' to cancel the current scrub")
            if scan.get("state") == "paused":
                scan["state"] = "running"
            else:
                p["scan"] = {"kind": "scrub", "state": "running", "since": now, "pct": 0.0}
        log_history(s, p["name"], ["zpool"] + argv)
        save(s)
        return
    if cmd == "clear":
        p = need_pool(args[0])
        for leaf in all_leaves(p):
            if len(args) == 1 or args[1] in (leaf.get("guid"), leaf.get("path")):
                leaf["r"] = leaf["w"] = leaf["c"] = 0
                if leaf.get("state") == "FAULTED":
                    leaf["state"] = "ONLINE"
        p["data_errors"] = 0
        save(s)
        return
    if cmd in ("offline", "online"):
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        for key in rest[1:]:
            _, node, _ = find_leaf(p, key)
            if node is None:
                die(f"cannot {cmd} {key}: no such device in pool")
            if cmd == "offline":
                node["state"] = "OFFLINE"
            elif "-e" in flags:
                p["expandsize"] = "-"
            else:
                node["state"] = "ONLINE"
        save(s)
        return
    if cmd == "replace":
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        old = rest[1]
        new = rest[2] if len(rest) > 2 else None
        parent, node, lst = find_leaf(p, old)
        if node is None:
            die(f"cannot replace {old} with {new}: no such device in pool")
        if new is None:
            if not os.path.exists(node["path"]):
                die(f"cannot replace {old}: no such device")
            node["state"] = "ONLINE"
            node.pop("missing", None)
            node["note"] = "(resilvering)"
        else:
            path, size = claim_disk(s, new, p["name"])
            newleaf = {"type": "disk", "path": path, "state": "ONLINE", "guid": new_guid(s), "note": "(resilvering)"}
            group = {"type": "replacing", "name": "replacing-0", "guid": new_guid(s), "children": [node, newleaf]}
            container = parent["children"] if parent else lst
            container[container.index(node)] = group
        renumber(p)
        p["scan"] = {"kind": "resilver", "state": "running", "since": time.strftime("%a %b %d %H:%M:%S %Y"), "pct": 0.0}
        log_history(s, p["name"], ["zpool"] + argv)
        save(s)
        return
    if cmd == "attach":
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        parent, node, lst = find_leaf(p, rest[1])
        if node is None:
            die(f"cannot attach {rest[2]} to {rest[1]}: no such device in pool")
        path, size = claim_disk(s, rest[2], p["name"])
        newleaf = {"type": "disk", "path": path, "state": "ONLINE", "guid": new_guid(s), "note": "(resilvering)"}
        if parent is None:
            mirror = {"type": "mirror", "name": "mirror-0", "guid": new_guid(s), "children": [node, newleaf]}
            lst[lst.index(node)] = mirror
        else:
            parent["children"].append(newleaf)
        renumber(p)
        p["scan"] = {"kind": "resilver", "state": "running", "since": time.strftime("%a %b %d %H:%M:%S %Y"), "pct": 0.0}
        save(s)
        return
    if cmd == "detach":
        p = need_pool(args[0])
        parent, node, lst = find_leaf(p, args[1])
        if node is None or parent is None:
            die(f"cannot detach {args[1]}: only applicable to mirror and replacing vdevs")
        parent["children"].remove(node)
        release_disk_labels(s, node["path"])
        if len(parent["children"]) == 1:
            for _, top_list in all_vdev_lists(p):
                for i, top in enumerate(top_list):
                    if top is parent:
                        top_list[i] = parent["children"][0]
                    else:
                        for j, ch in enumerate(top.get("children", [])):
                            if ch is parent:
                                top["children"][j] = parent["children"][0]
        renumber(p)
        save(s)
        return
    if cmd == "add":
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        if rest[1] != "spare":
            die("mock: only spares can be added")
        for dev in rest[2:]:
            path, _ = claim_disk(s, dev, p["name"])
            p.setdefault("spares", []).append({"type": "disk", "path": path, "state": "AVAIL", "guid": new_guid(s)})
        save(s)
        return
    if cmd == "remove":
        p = need_pool(args[0])
        for sec in ("spares", "cache", "logs"):
            for v in p.get(sec, []):
                if args[1] in (v.get("guid"), v.get("path")):
                    p[sec].remove(v)
                    release_disk_labels(s, v["path"])
                    save(s)
                    return
        die(f"cannot remove {args[1]}: only inactive hot spares, cache, or log devices can be removed")
    if cmd == "create":
        flags, opts, rest = opt_parse(args, ("-o", "-O", "-m"))
        name = rest[0]
        if name in pools:
            die(f"cannot create '{name}': pool already exists")
        spec = rest[1:]
        if "keylocation=prompt" in opts.get("-O", []):
            if not read_passphrase():
                die("Passphrases do not match.")
        vdevs, total = [], 0
        i = 0
        while i < len(spec):
            t = spec[i]
            if t in ("mirror", "raidz1", "raidz2", "raidz3", "raidz"):
                j = i + 1
                kids = []
                while j < len(spec) and spec[j].startswith("/"):
                    path, size = claim_disk(s, spec[j], name)
                    kids.append({"type": "disk", "path": path, "state": "ONLINE", "guid": new_guid(s), "size": size})
                    j += 1
                sizes = [k.pop("size") for k in kids]
                par = 0 if t == "mirror" else int(t[-1]) if t[-1].isdigit() else 1
                total += min(sizes) * (1 if t == "mirror" else len(sizes) - par)
                vdevs.append({"type": t if t != "raidz" else "raidz1", "name": "", "guid": new_guid(s), "children": kids})
                i = j
            else:
                path, size = claim_disk(s, t, name)
                vdevs.append({"type": "disk", "path": path, "state": "ONLINE", "guid": new_guid(s)})
                total += size
                i += 1
        props = {}
        for o in opts.get("-o", []):
            k, v = o.split("=", 1)
            props[k] = v
        props["feature@async_destroy"] = ["enabled", "local"]
        pools[name] = {"name": name, "guid": new_guid(s), "size": total, "props": props, "vdevs": vdevs,
                       "scan": {}, "history": []}
        renumber(pools[name])
        dsprops = {}
        for o in opts.get("-O", []):
            k, v = o.split("=", 1)
            if k in ("keyformat", "keylocation"):
                continue
            dsprops[k] = "aes-256-gcm" if (k, v) == ("encryption", "on") else v
        if "-m" in opts:
            dsprops["mountpoint"] = opts["-m"][0]
        s["datasets"][name] = {"type": "filesystem", "props": dsprops, "data": 98304, "refer": 98304,
                               "guid": new_guid(s), "creation": int(time.time()), "mounted": True}
        log_history(s, name, ["zpool"] + argv)
        save(s)
        return
    if cmd in ("destroy", "export"):
        flags, _, rest = opt_parse(args)
        p = need_pool(rest[0])
        if any(d.get("busy") for n, d in s["datasets"].items() if n.split("/")[0] == p["name"]):
            die(f"cannot unmount '/{p['name']}': pool or dataset is busy")
        if cmd == "export":
            s.setdefault("importable", []).append({"name": p["name"], "id": p["guid"], "state": "ONLINE",
                                                   "saved": {"pool": p, "datasets": {n: d for n, d in s["datasets"].items() if n.split("/")[0] == p["name"]},
                                                             "snapshots": {n: d for n, d in s["snapshots"].items() if n.split("/")[0].split("@")[0] == p["name"]}}})
        else:
            for leaf in all_leaves(p):
                release_disk_labels(s, leaf["path"])
        for n in [n for n in s["datasets"] if n.split("/")[0] == p["name"]]:
            del s["datasets"][n]
        for n in [n for n in s["snapshots"] if n.split("/")[0].split("@")[0] == p["name"]]:
            del s["snapshots"][n]
        del pools[p["name"]]
        save(s)
        return
    if cmd == "import":
        flags, opts, rest = opt_parse(args, ("-d",))
        cands = s.get("importable", [])
        if not rest:
            if not cands:
                die("no pools available to import")
            for c in cands:
                print(f"   pool: {c['name']}")
                print(f"     id: {c['id']}")
                print(f"  state: {c['state']}")
                if c.get("foreign"):
                    print("status: The pool was last accessed by another system.")
                    print(" action: The pool can be imported using its name or numeric identifier and")
                    print("\tthe '-f' flag.")
                else:
                    print(" action: The pool can be imported using its name or numeric identifier.")
                print(" config:")
                print("")
                print(f"\t{c['name']}        ONLINE")
                print("\t  mirror-0  ONLINE")
                print("\t    disk-a  ONLINE")
                print("\t    disk-b  ONLINE")
                print("")
            return
        c = next((x for x in cands if rest[0] in (x["id"], x["name"])), None)
        if c is None:
            die(f"cannot import '{rest[0]}': no such pool available")
        if c.get("foreign") and "-f" not in flags:
            die(f"cannot import '{c['name']}': pool was previously in use from another system.\nThe pool can be imported, use 'zpool import -f' to import the pool.")
        saved = c["saved"]
        pools[c["name"]] = saved["pool"]
        s["datasets"].update(saved["datasets"])
        s["snapshots"].update(saved["snapshots"])
        cands.remove(c)
        save(s)
        return
    if cmd == "history":
        p = need_pool(args[-1])
        print(f"History for '{p['name']}':")
        for line in p.get("history", []):
            print(line)
        return
    if cmd == "events":
        flags, _, rest = opt_parse(args)
        if "-f" in flags:
            print("TIME                           CLASS")
            print("Oct 27 2026 14:22:01.123456789 sysevent.fs.zfs.config_sync")
            sys.stdout.flush()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                return
        print("TIME                           CLASS")
        for ev in s["pools"][rest[0]].get("events", []) if rest else []:
            print(ev)
        return
    if cmd == "upgrade":
        p = need_pool(args[-1])
        for k in list(p["props"]):
            if k.startswith("feature@") and isinstance(p["props"][k], list) and p["props"][k][0] == "disabled":
                p["props"][k] = ["enabled", "local"]
        save(s)
        print(f"Successfully upgraded '{p['name']}'")
        return
    if cmd == "trim":
        need_pool(args[-1])
        return
    if cmd == "set":
        k, v = args[0].split("=", 1)
        p = need_pool(args[1])
        p["props"][k] = v
        save(s)
        return
    die(f"mock zpool: unsupported command {cmd}", 2)


# ------------------------------------------------------------------ lsblk, sfdisk

def lsblk_cmd(argv):
    s = load()
    member = {}
    for p in s["pools"].values():
        for leaf in all_leaves(p):
            d, pt = disk_by_path(s, leaf.get("path", ""))
            if d is not None and not leaf.get("missing"):
                member[(pt or d)["name"]] = p["name"]
    devs = []
    for d in s["disks"]:
        if d.get("absent"):
            continue
        node = {"name": d["name"], "kname": d["name"], "path": f"/dev/{d['name']}", "size": d["size"],
                "type": "disk", "model": d.get("model"), "serial": d.get("serial"), "rota": d.get("rota", True),
                "tran": d.get("tran", "sata"), "fstype": d.get("fstype") or None, "label": d.get("label") or None,
                "mountpoint": d.get("mountpoint")}
        kids = []
        for pt in d.get("parts", []):
            k = {"name": pt["name"], "kname": pt["name"], "path": f"/dev/{pt['name']}", "size": pt["size"],
                 "type": "part", "model": None, "serial": None, "rota": d.get("rota", True), "tran": None,
                 "fstype": pt.get("fstype") or None, "label": pt.get("label") or None,
                 "mountpoint": pt.get("mountpoint")}
            if pt.get("holder"):
                k["children"] = [{"name": pt["holder"], "kname": "dm-0", "type": "crypt", "size": pt["size"],
                                  "mountpoint": "/home"}]
            kids.append(k)
        if kids:
            node["children"] = kids
        devs.append(node)
    devs.append({"name": "zd0", "kname": "zd0", "path": "/dev/zd0", "size": 1, "type": "disk"})
    devs.append({"name": "loop0", "kname": "loop0", "path": "/dev/loop0", "size": 4096, "type": "loop"})
    print(json.dumps({"blockdevices": devs}, indent=3))


def sfdisk_cmd(argv):
    s = load()
    path = argv[-1]
    d, _ = disk_by_path(s, path)
    if d is None:
        die(f"sfdisk: cannot open {path}: No such file or directory")
    script = sys.stdin.read()
    size = None
    for part in script.replace("\n", ",").split(","):
        part = part.strip()
        if part.startswith("size="):
            size = int(part.split("=", 1)[1]) * 512
    if size is None or size > d["size"]:
        die("sfdisk: failed to add #1 partition: Numerical result out of range")
    d["parts"] = []
    d["fstype"] = ""
    make_part(s, d, 1, size)
    save(s)
    print(f"Created a new GPT disklabel.\n{d['name']}1 : start= 2048, size= {size // 512}")


def main():
    tool = sys.argv[1]
    argv = sys.argv[2:]
    if tool == "zfs":
        zfs_cmd(argv)
    elif tool == "zpool":
        zpool_cmd(argv)
    elif tool == "lsblk":
        lsblk_cmd(argv)
    elif tool == "sfdisk":
        sfdisk_cmd(argv)
    elif tool in ("udevadm", "systemctl"):
        return
    else:
        die(f"mock: unknown tool {tool}", 2)


if __name__ == "__main__":
    main()
