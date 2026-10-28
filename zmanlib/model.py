"""Data classes, parsers for ZFS tool output, and pure decision functions.

Nothing here runs a command, so all of it can be tested without ZFS installed.
Output from the tools is treated as untrusted: a line that does not parse is
skipped, never raised.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .textfmt import strip_ctl, to_float, to_int

GIB = 1024 ** 3
MIB = 1024 ** 2
ZFS_PART_TYPE = "6A898CC3-1DD2-11B2-99A6-080020736631"

# ---------------------------------------------------------------- pools


POOL_LIST_PROPS = ["name", "size", "allocated", "free", "fragmentation", "capacity", "health", "guid"]
VDEV_STATES = {"ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED", "AVAIL", "INUSE", "SPLIT"}
SECTIONS = {"logs", "cache", "spares", "special", "dedup"}
_GROUP = re.compile(r"^(mirror|raidz\d?|draid[^-\s]*|replacing|spare|indirect)-\d+$")


@dataclass
class ScanInfo:
    kind: str = ""          # scrub, resilver or ""
    state: str = "none"     # none, running, paused, finished, canceled
    pct: Optional[float] = None
    to_go: str = ""
    errors: int = 0
    when: Optional[float] = None   # start time if running/paused, end time if finished
    repaired: str = ""
    lines: List[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ("running", "paused")


@dataclass
class Vdev:
    idx: int
    parent: int              # index of the parent row, -1 for top rows
    depth: int
    section: str             # data, logs, cache, spares, special, dedup
    kind: str                # pool, section, mirror, raidz, draid, replacing, spare, indirect, leaf
    name: str
    state: str = ""
    read: int = 0
    write: int = 0
    cksum: int = 0
    note: str = ""
    path: str = ""           # device path for leaves (from the name, or "was <path>")
    guid: str = ""

    @property
    def errors(self) -> int:
        return self.read + self.write + self.cksum

    @property
    def is_leaf(self) -> bool:
        return self.kind == "leaf"

    @property
    def label(self) -> str:
        if self.is_leaf and self.path:
            return os.path.basename(self.path)
        return self.name


@dataclass
class PoolStatus:
    name: str
    state: str = ""
    status: str = ""
    action: str = ""
    scan: ScanInfo = field(default_factory=ScanInfo)
    errors_text: str = ""
    data_errors: int = 0
    vdevs: List[Vdev] = field(default_factory=list)

    def children(self, idx: int) -> List[Vdev]:
        return [v for v in self.vdevs if v.parent == idx]

    def leaves(self) -> List[Vdev]:
        return [v for v in self.vdevs if v.is_leaf]

    def find(self, key: str) -> Optional[Vdev]:
        """Find a device by guid, path, name or label."""
        for v in self.vdevs:
            if key and key in (v.guid, v.path, v.name):
                return v
        for v in self.vdevs:
            if v.is_leaf and v.label == key:
                return v
        return None


@dataclass
class Pool:
    name: str
    size: Optional[int] = None
    alloc: Optional[int] = None
    free: Optional[int] = None
    frag: Optional[int] = None
    cap: Optional[int] = None
    health: str = ""
    guid: str = ""
    status: Optional[PoolStatus] = None


def parse_pool_list(text: str) -> List[Pool]:
    pools = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < len(POOL_LIST_PROPS) or not f[0].strip():
            continue
        pools.append(Pool(
            name=strip_ctl(f[0]), size=to_int(f[1]), alloc=to_int(f[2]), free=to_int(f[3]),
            frag=to_int(f[4]), cap=to_int(f[5]), health=strip_ctl(f[6]).strip(), guid=f[7].strip(),
        ))
    return pools


def _count(tok: str) -> Optional[int]:
    """Error counters: exact with -p, but tolerate 1.2K style."""
    m = re.match(r"^(\d+(?:\.\d+)?)([KMGT]?)$", tok)
    if not m:
        return None
    mult = {"": 1, "K": 1000, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _parse_config_row(body: str) -> Optional[Tuple[int, str, str, List[int], str]]:
    indent = len(body) - len(body.lstrip(" "))
    toks = body.split()
    if not toks:
        return None
    # Name may in theory contain spaces (file vdevs): the state token ends it.
    si = None
    for i in range(1, len(toks)):
        if toks[i] in VDEV_STATES:
            si = i
            break
    if si is None:
        return indent // 2, " ".join(toks), "", [], ""
    name = " ".join(toks[:si])
    state = toks[si]
    rest = toks[si + 1:]
    counts: List[int] = []
    while rest and len(counts) < 3:
        c = _count(rest[0])
        if c is None:
            break
        counts.append(c)
        rest = rest[1:]
    return indent // 2, name, state, counts, " ".join(rest)


def parse_scan(lines: Sequence[str]) -> ScanInfo:
    info = ScanInfo(lines=[strip_ctl(x).strip() for x in lines if x.strip()])
    if not info.lines:
        return info
    first = info.lines[0]
    whole = " ".join(info.lines)
    m = re.match(r"^(scrub|resilver) in progress since (.+)$", first)
    if m:
        info.kind, info.state, info.when = m.group(1), "running", parse_c_date(m.group(2))
    else:
        m2 = re.match(r"^(scrub|resilver) paused since (.+)$", first)
        if m2:
            info.kind, info.state, info.when = m2.group(1), "paused", parse_c_date(m2.group(2))
        m3 = re.match(r"^(scrub repaired|resilvered) (\S+) in (.+?) with (\d+) errors on (.+)$", first)
        if m3:
            info.kind = "scrub" if m3.group(1).startswith("scrub") else "resilver"
            info.state = "finished"
            info.repaired = m3.group(2)
            info.errors = int(m3.group(4))
            info.when = parse_c_date(m3.group(5))
        m4 = re.match(r"^(scrub|resilver) canceled on (.+)$", first)
        if m4:
            info.kind, info.state, info.when = m4.group(1), "canceled", parse_c_date(m4.group(2))
    p = re.search(r"([\d.]+)% done", whole)
    if p:
        info.pct = to_float(p.group(1))
    t = re.search(r"((?:\d+ days? )?\d+:\d\d:\d\d) to go", whole)
    if t:
        info.to_go = t.group(1)
    r = re.search(r"(\S+) (?:repaired|resilvered),", whole)
    if r and info.state in ("running", "paused"):
        info.repaired = r.group(1)
    return info


def parse_c_date(text: str) -> Optional[float]:
    """Dates as zpool prints them in the C locale: 'Sun Oct 11 00:24:02 2026'."""
    s = " ".join(text.split())
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    return None


def parse_status(text: str) -> Dict[str, PoolStatus]:
    """Parse `zpool status -P -p` text for one or more pools.

    zpool status has no scripted mode before OpenZFS 2.3 (JSON came in 2.3),
    and Debian 12 ships 2.1, so this reads the stable text layout.
    """
    pools: Dict[str, PoolStatus] = {}
    cur: Optional[PoolStatus] = None
    key = ""
    vals: Dict[str, List[str]] = {}
    stack: List[Tuple[int, int]] = []   # (depth, idx)
    section = "data"

    def finish() -> None:
        if cur is None:
            return
        cur.status = " ".join(x.strip() for x in vals.get("status", []))
        cur.action = " ".join(x.strip() for x in vals.get("action", []))
        cur.scan = parse_scan(vals.get("scan", []))
        err = " ".join(x.strip() for x in vals.get("errors", []))
        cur.errors_text = strip_ctl(err)
        m = re.search(r"(\d+) data errors?", err)
        cur.data_errors = int(m.group(1)) if m else 0

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("\t") or line.startswith("        "):
            if cur is None:
                continue
            if key == "config":
                body = line[1:] if line.startswith("\t") else line[8:]
                if body.lstrip().startswith("NAME") and "STATE" in body:
                    continue
                row = _parse_config_row(body)
                if row is None:
                    continue
                depth, name, state, counts, note = row
                name = strip_ctl(name)
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                parent = stack[-1][1] if stack else -1
                idx = len(cur.vdevs)
                if depth == 0 and name == cur.name and not cur.vdevs:
                    kind, section = "pool", "data"
                elif depth == 0 and name in SECTIONS and not state:
                    kind, section = "section", name
                else:
                    g = _GROUP.match(name)
                    if g:
                        kind = g.group(1).rstrip("0123456789") if g.group(1).startswith("raidz") else g.group(1)
                        if kind.startswith("draid"):
                            kind = "draid"
                    else:
                        kind = "leaf"
                v = Vdev(idx=idx, parent=parent, depth=depth, section=section, kind=kind, name=name,
                         state=state, note=strip_ctl(note))
                if counts:
                    v.read = counts[0]
                    v.write = counts[1] if len(counts) > 1 else 0
                    v.cksum = counts[2] if len(counts) > 2 else 0
                if kind == "leaf":
                    if name.startswith("/"):
                        v.path = name
                    else:
                        m = re.search(r"was (/\S+)", note)
                        v.path = m.group(1) if m else ""
                cur.vdevs.append(v)
                stack.append((depth, idx))
            else:
                vals.setdefault(key, []).append(line.strip())
            continue
        m = re.match(r"^\s*([a-z]+):(?:\s(.*))?$", line)
        if not m:
            continue
        k, v_text = m.group(1), (m.group(2) or "").strip()
        if k == "pool":
            finish()
            cur = PoolStatus(name=strip_ctl(v_text))
            pools[cur.name] = cur
            vals = {}
            stack = []
            section = "data"
            key = "pool"
            continue
        if cur is None:
            continue
        key = k
        if k == "state":
            cur.state = v_text
        elif k != "config":
            vals.setdefault(k, []).append(v_text)
    finish()
    return pools


def attach_guids(by_path: Dict[str, PoolStatus], by_guid: Dict[str, PoolStatus]) -> None:
    """`zpool status -g` prints the same tree with guids as names; copy them over row by row."""
    for name, st in by_path.items():
        other = by_guid.get(name)
        if not other or len(other.vdevs) != len(st.vdevs):
            continue
        for a, b in zip(st.vdevs, other.vdevs):
            if a.depth == b.depth and b.name.isdecimal():
                a.guid = b.name


def parse_props(text: str) -> Dict[str, Tuple[str, str]]:
    """`get -H -p -o property,value,source` → {property: (value, source)}."""
    props: Dict[str, Tuple[str, str]] = {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            props[f[0].strip()] = (strip_ctl(f[1]), strip_ctl(f[2]))
        elif len(f) == 2:
            props[f[0].strip()] = (strip_ctl(f[1]), "")
    return props


def upgrade_available(props: Dict[str, Tuple[str, str]]) -> bool:
    compat = props.get("compatibility", ("off", ""))[0]
    if compat not in ("off", "-", ""):
        return False
    return any(k.startswith("feature@") and v[0] == "disabled" for k, v in props.items())


def state_style(state: str) -> str:
    s = state.upper()
    if s in ("ONLINE", "AVAIL"):
        return "ok"
    if s in ("OFFLINE", "INUSE", "SPLIT"):
        return "warn"
    if s == "":
        return "dim"
    return "bad"


def vdev_state_text(v: Vdev) -> Tuple[str, str]:
    if v.kind == "section":
        return "", "dim"
    word = v.state or "-"
    style = state_style(v.state)
    if v.errors:
        word = f"{word}, {v.errors} errors"
        style = "bad"
    note = v.note
    if "resilvering" in note or "awaiting resilver" in note:
        word += " (resilvering)"
        if style == "ok":
            style = "warn"
    return word, style


def pool_problems(st: Optional[PoolStatus]) -> List[str]:
    """Short problem words for one pool, worst first. Empty when healthy."""
    if st is None:
        return []
    probs = []
    if st.state and st.state != "ONLINE":
        probs.append(st.state.lower())
    bad = [v for v in st.leaves() if v.errors or state_style(v.state) == "bad"]
    if bad:
        probs.append(f"{len(bad)} device{'s' if len(bad) != 1 else ''} with errors"
                     if any(v.errors for v in bad) else
                     f"{len(bad)} device{'s' if len(bad) != 1 else ''} {bad[0].state.lower()}")
    if st.data_errors:
        probs.append(f"{st.data_errors} data errors")
    if st.scan.state == "finished" and st.scan.errors:
        probs.append(f"{st.scan.kind} found {st.scan.errors} errors")
    return probs


def scan_text(scan: ScanInfo, now: float) -> Tuple[str, str]:
    """One short line about the last or current scrub/resilver, and its style."""
    from .textfmt import fmt_duration
    if scan.state == "none":
        return "never scrubbed", "warn"
    if scan.state == "running":
        pct = f" {scan.pct:.0f}%" if scan.pct is not None else ""
        togo = f", {scan.to_go} to go" if scan.to_go else ""
        return f"{scan.kind}{pct}{togo}", "warn"
    if scan.state == "paused":
        pct = f" at {scan.pct:.0f}%" if scan.pct is not None else ""
        return f"{scan.kind} paused{pct}", "warn"
    if scan.state == "canceled":
        return f"{scan.kind} stopped", "warn"
    age = f" {fmt_duration(now - scan.when)} ago" if scan.when else ""
    if scan.errors:
        return f"{scan.kind}{age}, {scan.errors} errors", "bad"
    return f"{scan.kind}{age}, 0 errors", "ok"


def pool_redundant(st: PoolStatus) -> bool:
    """True when every data top-level vdev can lose a device."""
    tops = [v for v in st.vdevs if v.depth == 1 and v.section == "data"]
    return bool(tops) and all(v.kind in ("mirror", "raidz", "draid") for v in tops)


# ---------------------------------------------------------------- datasets

DATASET_PROPS = [
    "name", "type", "used", "available", "referenced", "mountpoint", "mounted", "canmount",
    "compression", "compressratio", "quota", "refquota", "reservation", "refreservation",
    "recordsize", "volsize", "volblocksize", "encryption", "encryptionroot", "keystatus",
    "origin", "usedbysnapshots", "usedbydataset", "usedbychildren", "usedbyrefreservation",
    "creation", "atime", "readonly", "guid",
]


@dataclass
class Dataset:
    name: str
    type: str
    used: Optional[int] = None
    avail: Optional[int] = None
    refer: Optional[int] = None
    mountpoint: str = ""
    mounted: bool = False
    canmount: str = ""
    compression: str = ""
    ratio: Optional[float] = None
    quota: Optional[int] = None
    refquota: Optional[int] = None
    reservation: Optional[int] = None
    recordsize: Optional[int] = None
    volsize: Optional[int] = None
    volblocksize: Optional[int] = None
    encryption: str = "off"
    encroot: str = ""
    keystatus: str = ""
    origin: str = ""
    used_snap: Optional[int] = None
    used_ds: Optional[int] = None
    used_child: Optional[int] = None
    used_refres: Optional[int] = None
    creation: Optional[int] = None
    atime: str = ""
    readonly: str = ""
    guid: str = ""
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def pool(self) -> str:
        return self.name.split("/", 1)[0]

    @property
    def parent(self) -> str:
        return self.name.rsplit("/", 1)[0] if "/" in self.name else ""

    @property
    def leaf(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def is_volume(self) -> bool:
        return self.type == "volume"

    @property
    def is_root(self) -> bool:
        return "/" not in self.name

    @property
    def encrypted(self) -> bool:
        return self.encryption not in ("off", "", "-")

    @property
    def locked(self) -> bool:
        return self.keystatus == "unavailable"

    @property
    def has_mount_path(self) -> bool:
        return self.mountpoint.startswith("/")


def _none_if_dash(v: str) -> str:
    v = strip_ctl(v).strip()
    return "" if v == "-" else v


def parse_datasets(text: str) -> List[Dataset]:
    out = []
    n = len(DATASET_PROPS)
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) != n or not f[0]:
            continue
        raw = dict(zip(DATASET_PROPS, f))
        q = to_int(raw["quota"])
        out.append(Dataset(
            name=strip_ctl(raw["name"]), type=raw["type"].strip(),
            used=to_int(raw["used"]), avail=to_int(raw["available"]), refer=to_int(raw["referenced"]),
            mountpoint=_none_if_dash(raw["mountpoint"]), mounted=raw["mounted"].strip() == "yes",
            canmount=_none_if_dash(raw["canmount"]), compression=_none_if_dash(raw["compression"]),
            ratio=to_float(raw["compressratio"]), quota=q or None,
            refquota=to_int(raw["refquota"]) or None, reservation=to_int(raw["reservation"]) or None,
            recordsize=to_int(raw["recordsize"]), volsize=to_int(raw["volsize"]),
            volblocksize=to_int(raw["volblocksize"]), encryption=_none_if_dash(raw["encryption"]) or "off",
            encroot=_none_if_dash(raw["encryptionroot"]), keystatus=_none_if_dash(raw["keystatus"]),
            origin=_none_if_dash(raw["origin"]), used_snap=to_int(raw["usedbysnapshots"]),
            used_ds=to_int(raw["usedbydataset"]), used_child=to_int(raw["usedbychildren"]),
            used_refres=to_int(raw["usedbyrefreservation"]), creation=to_int(raw["creation"]),
            atime=_none_if_dash(raw["atime"]), readonly=_none_if_dash(raw["readonly"]),
            guid=raw["guid"].strip(), raw=raw,
        ))
    return out


def dataset_state(ds: Dataset) -> Tuple[str, str]:
    """The state word for the list and header, and its style."""
    limit = ds.quota
    if limit and ds.used is not None:
        frac = ds.used / limit
        if frac >= 0.99:
            return "quota full", "bad"
        if frac >= 0.90:
            return "near quota", "warn"
    if ds.locked:
        return "locked", "warn"
    if ds.is_volume:
        return "ok", "ok"
    if ds.mounted:
        return "mounted", "ok"
    if ds.canmount == "off" or ds.mountpoint in ("none", ""):
        return "not mountable", "dim"
    if ds.mountpoint == "legacy":
        return "legacy mount", "dim"
    if ds.canmount == "noauto":
        return "not mounted (noauto)", "dim"
    return "not mounted", "warn"


# ---------------------------------------------------------------- snapshots

SNAPSHOT_PROPS = ["name", "used", "referenced", "creation", "createtxg", "userrefs", "clones", "guid",
                  "defer_destroy"]


@dataclass
class Snapshot:
    name: str
    used: Optional[int] = None
    refer: Optional[int] = None
    creation: Optional[int] = None
    txg: int = 0
    holds: int = 0
    clones: List[str] = field(default_factory=list)
    guid: str = ""
    defer: bool = False
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def dataset(self) -> str:
        return self.name.split("@", 1)[0]

    @property
    def short(self) -> str:
        return self.name.split("@", 1)[1] if "@" in self.name else self.name

    @property
    def pool(self) -> str:
        return self.name.split("/", 1)[0].split("@", 1)[0]

    def fingerprint(self) -> Tuple[str, str, int, Tuple[str, ...]]:
        return (self.name, self.guid, self.holds, tuple(self.clones))


def parse_snapshots(text: str) -> List[Snapshot]:
    out = []
    n = len(SNAPSHOT_PROPS)
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) != n or "@" not in f[0]:
            continue
        raw = dict(zip(SNAPSHOT_PROPS, f))
        clones = [strip_ctl(c) for c in raw["clones"].split(",") if c.strip() and c.strip() != "-"]
        out.append(Snapshot(
            name=strip_ctl(raw["name"]), used=to_int(raw["used"]), refer=to_int(raw["referenced"]),
            creation=to_int(raw["creation"]), txg=to_int(raw["createtxg"]) or 0,
            holds=to_int(raw["userrefs"]) or 0, clones=clones, guid=raw["guid"].strip(),
            defer=raw["defer_destroy"].strip() == "on", raw=raw,
        ))
    return out


def parse_holds(text: str) -> List[str]:
    """`zfs holds -H snap` → hold tags."""
    tags = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) >= 2 and f[1].strip():
            tags.append(strip_ctl(f[1]).strip())
    return tags


def snapshot_blockers(s: Snapshot) -> List[str]:
    reasons = []
    if s.holds:
        reasons.append(f"it has {s.holds} hold{'s' if s.holds != 1 else ''}")
    if s.clones:
        reasons.append(f"{len(s.clones)} clone{'s' if len(s.clones) != 1 else ''} depend on it")
    return reasons


def rollback_plan(target: Snapshot, snaps: Sequence[Snapshot]) -> Tuple[List[Snapshot], List[str]]:
    """Snapshots a rollback to `target` would destroy, and reasons it must not run."""
    newer = sorted((s for s in snaps if s.dataset == target.dataset and s.txg > target.txg),
                   key=lambda s: s.txg)
    blockers = []
    for s in newer:
        for r in snapshot_blockers(s):
            blockers.append(f"@{s.short}: {r}")
    return newer, blockers


def prune_candidates(snaps: Sequence[Snapshot]) -> List[Snapshot]:
    """Snapshots that bulk pruning may offer: no holds, no clones. Largest first."""
    ok = [s for s in snaps if not s.holds and not s.clones and not s.defer]
    return sorted(ok, key=lambda s: (-(s.used or 0), s.name))


def parse_destroy_preview(text: str) -> Tuple[List[str], Optional[int]]:
    """`zfs destroy -nvp` → (names that would go, bytes reclaimed or None)."""
    names: List[str] = []
    reclaim: Optional[int] = None
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("destroy\t"):
            names.append(strip_ctl(line.split("\t", 1)[1]).strip())
        elif line.startswith("would destroy "):
            names.append(strip_ctl(line[len("would destroy "):]).strip())
        elif line.startswith("reclaim\t"):
            reclaim = to_int(line.split("\t", 1)[1])
        elif line.startswith("would reclaim "):
            reclaim = to_int(line[len("would reclaim "):])
    return names, reclaim


def default_snapshot_name(prefix: str, now: datetime) -> str:
    return f"{prefix}-{now:%Y-%m-%d-%H%M}"


# ---------------------------------------------------------------- names

_COMPONENT = re.compile(r"^[A-Za-z0-9_.:-]+$")
_POOL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_RESERVED_POOL_PREFIX = ("mirror", "raidz", "draid", "spare")
_RESERVED_POOL = {"log", "cache", "special", "dedup"}


def check_pool_name(name: str) -> str:
    """'' when valid, otherwise a short reason."""
    if not name:
        return "A name is needed."
    if len(name) > 200:
        return "That name is too long."
    if not _POOL.match(name):
        return "Use letters, digits, and _ . : - only, starting with a letter."
    low = name.lower()
    if low in _RESERVED_POOL or any(low.startswith(p) for p in _RESERVED_POOL_PREFIX):
        return f"ZFS reserves the name {name}."
    if re.match(r"^c[0-9]", low):
        return "ZFS does not allow names that start with c and a digit."
    return ""


def check_component(name: str, what: str = "name") -> str:
    if not name:
        return f"A {what} is needed."
    if name in (".", ".."):
        return f"{name} is not a valid {what}."
    if len(name) > 200:
        return f"That {what} is too long."
    if not _COMPONENT.match(name):
        return f"Use letters, digits, and _ . : - only in the {what}."
    return ""


def check_child_path(path: str) -> str:
    """A dataset path below a pool, e.g. 'home/photos'."""
    if not path:
        return "A name is needed."
    for part in path.split("/"):
        err = check_component(part)
        if err:
            return err
    return ""


# ---------------------------------------------------------------- disks


@dataclass
class Part:
    name: str
    path: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    mounts: List[str] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)   # crypt, lvm, raid1...
    by_id: str = ""

    @property
    def number(self) -> int:
        m = re.search(r"(\d+)$", self.name)
        return int(m.group(1)) if m else 0


@dataclass
class Disk:
    name: str
    path: str
    size: int = 0
    model: str = ""
    serial: str = ""
    tran: str = ""
    rota: bool = True
    fstype: str = ""
    label: str = ""
    mounts: List[str] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)
    parts: List[Part] = field(default_factory=list)
    by_id: str = ""
    raw: Dict[str, object] = field(default_factory=dict)

    @property
    def media(self) -> str:
        if self.tran == "nvme" or self.name.startswith("nvme"):
            return "NVMe"
        return "HDD" if self.rota else "SSD"

    @property
    def all_mounts(self) -> List[str]:
        out = list(self.mounts)
        for p in self.parts:
            out.extend(p.mounts)
        return out

    @property
    def all_holders(self) -> List[str]:
        out = list(self.holders)
        for p in self.parts:
            out.extend(p.holders)
        return out

    @property
    def dev_path(self) -> str:
        """The stable path to hand to zpool: by-id when there is one."""
        return self.by_id or self.path

    def describe(self) -> str:
        bits = [x for x in (self.model, f"serial {self.serial}" if self.serial else "") if x]
        return " · ".join(bits)


_SKIP_DISKS = ("zd", "zram", "ram", "nbd", "loop", "fd", "sr")


def _as_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip() in ("1", "true")


def _mounts_of(node: Dict[str, object]) -> List[str]:
    out = []
    mp = node.get("mountpoint")
    if mp:
        out.append(str(mp))
    for m in node.get("mountpoints") or []:
        if m and str(m) not in out:
            out.append(str(m))
    return [strip_ctl(m) for m in out]


def _walk_holders(node: Dict[str, object]) -> Tuple[List[str], List[str]]:
    """Types and mountpoints of everything stacked on top of a disk or partition."""
    types, mounts = [], []
    for ch in node.get("children") or []:
        if not isinstance(ch, dict):
            continue
        types.append(str(ch.get("type") or "?"))
        mounts.extend(_mounts_of(ch))
        t, m = _walk_holders(ch)
        types.extend(t)
        mounts.extend(m)
    return types, mounts


def parse_lsblk(text: str) -> Tuple[List[Disk], str]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], "lsblk printed something zman could not read."
    devs = data.get("blockdevices") if isinstance(data, dict) else None
    if not isinstance(devs, list):
        return [], "lsblk printed something zman could not read."
    disks = []
    for d in devs:
        if not isinstance(d, dict) or d.get("type") != "disk":
            continue
        name = strip_ctl(d.get("kname") or d.get("name") or "")
        size = to_int(d.get("size")) or 0
        if not name or name.startswith(_SKIP_DISKS) or size <= 0:
            continue
        disk = Disk(
            name=name, path=strip_ctl(d.get("path") or f"/dev/{name}"), size=size,
            model=strip_ctl(d.get("model") or "").strip(), serial=strip_ctl(d.get("serial") or "").strip(),
            tran=strip_ctl(d.get("tran") or ""), rota=_as_bool(d.get("rota")),
            fstype=strip_ctl(d.get("fstype") or ""), label=strip_ctl(d.get("label") or ""),
            mounts=_mounts_of(d), raw=d,
        )
        for ch in d.get("children") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("type") == "part":
                types, mounts = _walk_holders(ch)
                pname = strip_ctl(ch.get("kname") or ch.get("name") or "")
                disk.parts.append(Part(
                    name=pname, path=strip_ctl(ch.get("path") or f"/dev/{pname}"),
                    size=to_int(ch.get("size")) or 0, fstype=strip_ctl(ch.get("fstype") or ""),
                    label=strip_ctl(ch.get("label") or ""), mounts=_mounts_of(ch) + mounts,
                    holders=types,
                ))
            else:
                disk.holders.append(str(ch.get("type") or "?"))
                disk.mounts.extend(_mounts_of(ch))
                t, m = _walk_holders(ch)
                disk.holders.extend(t)
                disk.mounts.extend(m)
        disks.append(disk)
    return disks, ""


def _by_id_rank(link: str) -> int:
    order = [("ata-", 0), ("nvme-eui.", 6), ("nvme-nvme.", 6), ("nvme-", 1), ("scsi-", 2),
             ("usb-", 3), ("virtio-", 3), ("mmc-", 3), ("wwn-", 5)]
    for prefix, rank in order:
        if link.startswith(prefix):
            return rank
    return 4


def pick_by_id(links: Dict[str, str]) -> Dict[str, str]:
    """{by-id link name: kernel name} → {kernel name: best link name}."""
    best: Dict[str, str] = {}
    for link in sorted(links):
        kname = links[link]
        cur = best.get(kname)
        if cur is None or (_by_id_rank(link), len(link)) < (_by_id_rank(cur), len(cur)):
            best[kname] = link
    return best


@dataclass
class DiskUse:
    word: str
    style: str
    free: bool
    pool: str = ""
    warn: str = ""     # "has ext4 data" etc. for free disks that are not blank


def disk_use(d: Disk, members: Dict[str, Tuple[str, str]]) -> DiskUse:
    """How a disk is used. `members` maps kernel names of pool devices to (pool, state)."""
    for n in [d.name] + [p.name for p in d.parts]:
        if n in members:
            pool, state = members[n]
            if state == "AVAIL":
                return DiskUse(f"pool {pool} (spare)", "ok", False, pool)
            if state == "INUSE":
                return DiskUse(f"pool {pool} (spare in use)", "warn", False, pool)
            return DiskUse(f"pool {pool}" + ("" if state == "ONLINE" else f" ({state})"),
                           "ok" if state == "ONLINE" else state_style(state), False, pool)
    mounts = d.all_mounts
    if "[SWAP]" in mounts:
        return DiskUse("swap", "dim", False)
    if "/" in mounts:
        return DiskUse("system disk (mounted /)", "dim", False)
    if mounts:
        return DiskUse(f"mounted {mounts[0]}", "dim", False)
    holders = [h for h in d.all_holders if h != "part"]
    if holders:
        return DiskUse(f"in use ({holders[0]})", "dim", False)
    fstypes = [x for x in [d.fstype] + [p.fstype for p in d.parts] if x]
    if "LVM2_member" in fstypes:
        return DiskUse("LVM member", "dim", False)
    if "linux_raid_member" in fstypes:
        return DiskUse("RAID member", "dim", False)
    if "zfs_member" in fstypes:
        labels = [p.label for p in d.parts if p.fstype == "zfs_member" and p.label] + \
                 ([d.label] if d.fstype == "zfs_member" and d.label else [])
        name = labels[0] if labels else "unknown"
        return DiskUse(f"free, old ZFS label ({name})", "warn", True,
                       warn=f"has a ZFS label of pool {name}, which may be an exported pool")
    if fstypes:
        return DiskUse(f"free, has {fstypes[0]} data", "warn", True, warn=f"has {fstypes[0]} data")
    if d.parts:
        word = f"has {len(d.parts)} partition{'s' if len(d.parts) != 1 else ''}"
        return DiskUse(f"free, {word}", "warn", True, warn=word)
    return DiskUse("free", "ok", True)


def pool_members(statuses: Dict[str, PoolStatus], to_kname: Callable[[str], str]) -> Dict[str, Tuple[str, str]]:
    out: Dict[str, Tuple[str, str]] = {}
    for st in statuses.values():
        for v in st.leaves():
            if v.path:
                k = to_kname(v.path)
                if k:
                    out[k] = (st.name, v.state)
    return out


def whole_disk_layout(d: Optional[Disk]) -> bool:
    """True when ZFS partitioned this disk itself (data partition 1 plus a small partition 9)."""
    if d is None:
        return True
    return any(p.number == 9 and p.size <= 64 * MIB for p in d.parts)


def headroom_size(sizes: Sequence[int]) -> int:
    """Partition size for 'leave headroom': below the smallest disk by 0.5% (at least 1 GiB),
    in whole MiB, leaving room for the GPT at both ends."""
    smallest = min(sizes)
    margin = max(GIB, smallest // 200)
    size = smallest - margin - 2 * MIB
    return max(0, size - size % MIB)


def sfdisk_script(size_bytes: int, label: str) -> str:
    sectors = size_bytes // 512
    return f"label: gpt\nstart=2048, size={sectors}, type={ZFS_PART_TYPE}, name=\"{label}\"\n"


# ---------------------------------------------------------------- pool creation


@dataclass
class Layout:
    key: str
    label: str
    explain: str
    min_disks: int
    parity: int = 0


LAYOUTS = [
    Layout("single", "Single disk", "1 disk, no redundancy", 1),
    Layout("mirror", "Mirror", "2+ disks, keeps working while one disk is left", 2),
    Layout("raidz1", "RAIDZ1", "3+ disks, survives 1 failed disk", 3, 1),
    Layout("raidz2", "RAIDZ2", "4+ disks, survives 2 failed disks", 4, 2),
    Layout("raidz3", "RAIDZ3", "5+ disks, survives 3 failed disks", 5, 3),
    Layout("mirrors", "Striped mirrors", "pairs of disks, like RAID 10", 4),
]
LAYOUT_BY_KEY = {lay.key: lay for lay in LAYOUTS}


def check_layout(key: str, n: int) -> str:
    lay = LAYOUT_BY_KEY[key]
    if key == "single" and n != 1:
        return "A single-disk pool uses exactly 1 disk."
    if n < lay.min_disks:
        return f"{lay.label} needs at least {lay.min_disks} disks."
    if key == "mirrors" and n % 2:
        return "Striped mirrors need an even number of disks."
    return ""


def pair_disks(sizes: Sequence[int]) -> List[Tuple[int, int]]:
    """Index pairs for striped mirrors: largest with next largest, so sizes match."""
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i])
    return [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]


def capacity_estimate(key: str, sizes: Sequence[int]) -> int:
    if not sizes:
        return 0
    if key == "single":
        return sizes[0]
    if key == "mirror":
        return min(sizes)
    if key == "mirrors":
        return sum(min(sizes[a], sizes[b]) for a, b in pair_disks(sizes))
    parity = LAYOUT_BY_KEY[key].parity
    return (len(sizes) - parity) * min(sizes)


def vdev_spec(key: str, devs: Sequence[str], sizes: Sequence[int]) -> List[str]:
    if key == "single":
        return [devs[0]]
    if key == "mirrors":
        spec: List[str] = []
        for a, b in pair_disks(sizes):
            spec += ["mirror", devs[a], devs[b]]
        return spec
    return [key, *devs]


@dataclass
class CreateOpts:
    mountpoint: str = ""      # "" = ZFS default /<pool>
    compression: str = "lz4"
    ashift: int = 12
    encryption: bool = False
    autotrim: bool = True
    atime: bool = False
    headroom: bool = False


def create_args(name: str, key: str, devs: Sequence[str], sizes: Sequence[int], o: CreateOpts,
                force: bool) -> List[str]:
    args = ["zpool", "create"]
    if force:
        args.append("-f")
    args += ["-o", f"ashift={o.ashift}", "-o", "autoexpand=on", "-o", f"autotrim={'on' if o.autotrim else 'off'}",
             "-O", f"compression={o.compression}", "-O", f"atime={'on' if o.atime else 'off'}",
             "-O", "xattr=sa"]
    if o.encryption:
        args += ["-O", "encryption=on", "-O", "keyformat=passphrase", "-O", "keylocation=prompt"]
    if o.mountpoint and o.mountpoint != f"/{name}":
        args += ["-m", o.mountpoint]
    args.append(name)
    args += vdev_spec(key, devs, sizes)
    return args


# ---------------------------------------------------------------- import, iostat, arc, history


@dataclass
class ImportCandidate:
    name: str
    id: str
    state: str = ""
    status: str = ""
    action: str = ""
    config: List[str] = field(default_factory=list)

    @property
    def needs_force(self) -> bool:
        text = (self.status + " " + self.action).lower()
        return "'-f'" in text or "-f flag" in text or "another system" in text


def parse_import(text: str) -> List[ImportCandidate]:
    out: List[ImportCandidate] = []
    cur: Optional[ImportCandidate] = None
    key = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^\s*([a-z]+):(?:\s(.*))?$", line)
        if m and not line.startswith("\t"):
            k, v = m.group(1), strip_ctl(m.group(2) or "").strip()
            if k == "pool":
                cur = ImportCandidate(name=v, id="")
                out.append(cur)
            elif cur is not None:
                if k == "id":
                    cur.id = v
                elif k == "state":
                    cur.state = v
                elif k in ("status", "action"):
                    setattr(cur, k, v)
            key = k
            continue
        if cur is None:
            continue
        if key == "config":
            cur.config.append(strip_ctl(line.replace("\t", "  ", 1)).rstrip())
        elif key in ("status", "action"):
            setattr(cur, key, (getattr(cur, key) + " " + strip_ctl(line).strip()).strip())
    return [c for c in out if c.name and c.id]


@dataclass
class IoStat:
    pool: str
    alloc: Optional[int]
    free: Optional[int]
    rops: Optional[int]
    wops: Optional[int]
    rbw: Optional[int]
    wbw: Optional[int]


def parse_iostat(text: str) -> List[IoStat]:
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 7 or not f[0].strip():
            continue
        out.append(IoStat(strip_ctl(f[0]).strip(), *(to_int(x) for x in f[1:7])))
    return out


def parse_arcstats(text: str) -> Dict[str, int]:
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and f[2].lstrip("-").isdecimal():
            out[f[0]] = int(f[2])
    return out


def parse_history(text: str) -> List[Tuple[Optional[datetime], str]]:
    out = []
    for line in text.splitlines():
        if line.startswith("History for") or not line.strip():
            continue
        line = strip_ctl(line)
        ts = None
        first = line.split(" ", 1)[0]
        try:
            ts = datetime.strptime(first, "%Y-%m-%d.%H:%M:%S")
        except ValueError:
            pass
        out.append((ts, line))
    return out


def parse_event_time(line: str) -> Optional[datetime]:
    """'Oct 27 2026 14:22:01.123456789 ...' as zpool events prints it."""
    toks = line.split()
    if len(toks) < 4:
        return None
    try:
        return datetime.strptime(" ".join(toks[:4]).split(".")[0], "%b %d %Y %H:%M:%S")
    except ValueError:
        return None
