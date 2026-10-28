"""Builds the mock system used by the walkthrough: a fake /dev tree and a JSON state
with the awkward cases (faulted and missing devices, a running scrub, a held and a
cloned snapshot, a locked dataset, a busy dataset, free and in-use disks...)."""
from __future__ import annotations

import json
import os
import stat
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TB, GB, MB = 10 ** 12, 10 ** 9, 1024 * 1024
TOOLS = ["zfs", "zpool", "lsblk", "sfdisk", "udevadm", "systemctl"]


def _disk(name, size, model, serial, by_id, rota=True, tran="sata", parts=None, fstype="", label=""):
    return {"name": name, "size": size, "model": model, "serial": serial, "by_id": by_id, "rota": rota,
            "tran": tran, "parts": parts or [], "fstype": fstype, "label": label}


def _zparts(name, size, pool):
    return [{"name": f"{name}1", "size": size - 9 * MB, "fstype": "zfs_member", "label": pool},
            {"name": f"{name}9", "size": 8 * MB, "fstype": "", "label": ""}]


def build(root: str) -> dict:
    """Create the mock in `root`; returns env vars to run zman against it."""
    os.makedirs(os.path.join(root, "dev"), exist_ok=True)
    os.makedirs(os.path.join(root, "by-id"), exist_ok=True)
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    for tool in TOOLS:
        p = os.path.join(bindir, tool)
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\nexec python3 "{HERE}/mockzfs.py" {tool} "$@"\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    open(os.path.join(root, "dev", "zfs"), "w").close()

    disks = [
        _disk("sda", 500 * GB, "Samsung SSD 870", "S6PNNX0T111111", "ata-Samsung_SSD_870_EVO_500GB_S6PNNX0T111111",
              rota=False, parts=[{"name": "sda1", "size": 512 * MB, "fstype": "vfat", "label": "", "mountpoint": "/boot/efi"},
                                 {"name": "sda2", "size": 499 * GB, "fstype": "ext4", "label": "", "mountpoint": "/"}]),
        _disk("sdb", 4 * TB, "WDC WD40EFRX", "WD-WCC7K1111111", "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1111111",
              parts=_zparts("sdb", 4 * TB, "tank")),
        _disk("sdc", 4 * TB, "WDC WD40EFRX", "WD-WCC7K2222222", "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K2222222",
              parts=_zparts("sdc", 4 * TB, "tank")),
        _disk("sde", 4 * TB, "ST4000VN008", "ZDH1SPARE1", "ata-ST4000VN008-2DR166_ZDH1SPARE1",
              parts=_zparts("sde", 4 * TB, "tank")),
        _disk("sdf", 2 * TB, "ST2000DM008", "ZFL0AAAA", "ata-ST2000DM008-2FR102_ZFL0AAAA",
              parts=_zparts("sdf", 2 * TB, "backup")),
        _disk("sdg", 2 * TB, "ST2000DM008", "ZFL0BBBB", "ata-ST2000DM008-2FR102_ZFL0BBBB",
              parts=_zparts("sdg", 2 * TB, "backup")),
        _disk("nvme0n1", 1 * TB, "Samsung SSD 980 PRO 1TB", "S5GXNF0R123456",
              "nvme-Samsung_SSD_980_PRO_1TB_S5GXNF0R123456", rota=False, tran="nvme"),
        _disk("sdi", 3 * TB, "TOSHIBA HDWD130", "X0TOSH3TB", "ata-TOSHIBA_HDWD130_X0TOSH3TB",
              parts=[{"name": "sdi1", "size": 3 * TB - 2 * MB, "fstype": "ext4", "label": "olddata"}]),
        _disk("sdj", 4 * TB, "WDC WD40EFZX", "WD-NEWDISK4TB", "ata-WDC_WD40EFZX-68AWUN0_WD-NEWDISK4TB"),
        _disk("sdk", 6 * TB, "ST6000VN001", "ZAD6LVM", "ata-ST6000VN001-2BB186_ZAD6LVM", fstype="LVM2_member"),
        _disk("sdl", 2 * TB, "WDC WD20EARS", "WD-OLDZFS", "ata-WDC_WD20EARS-00MVWB0_WD-OLDZFS",
              parts=_zparts("sdl", 2 * TB, "oldpool")),
        _disk("sdm", 3500 * GB, "HGST HDN724040", "PK1SMALLER", "ata-HGST_HDN724040ALE640_PK1SMALLER"),
    ]
    for i, d in enumerate(disks):
        open(os.path.join(root, "dev", d["name"]), "w").close()
        link = os.path.join(root, "by-id", d["by_id"])
        os.symlink(os.path.join("..", "dev", d["name"]), link)
        os.symlink(os.path.join("..", "dev", d["name"]), os.path.join(root, "by-id", f"wwn-0x5000c500{i:08x}"))
        for p in d["parts"]:
            open(os.path.join(root, "dev", p["name"]), "w").close()
            n = p["name"][len(d["name"]):].lstrip("p")
            os.symlink(os.path.join("..", "dev", p["name"]), os.path.join(root, "by-id", f"{d['by_id']}-part{n}"))

    def bid(name):
        d = next(x for x in disks if x["name"] == name)
        return os.path.join(root, "by-id", d["by_id"] + "-part1")

    now = int(time.time())
    day = 86400
    finished = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(now - 16 * day))
    since = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(now - 3 * 3600))
    state = {
        "txg": 5000,
        "guid_counter": 1100000000000000000,
        "disks": disks,
        "pools": {
            "tank": {
                "name": "tank", "guid": "15378823766524254427", "size": 3990 * GB, "frag": 4,
                "props": {"ashift": ["12", "local"], "autoexpand": ["on", "local"], "autotrim": ["off", "default"],
                          "feature@async_destroy": ["enabled", "local"], "feature@zstd_compress": ["active", "local"]},
                "vdevs": [{"type": "mirror", "name": "mirror-0", "guid": "4011111111111111111", "children": [
                    {"type": "disk", "path": bid("sdb"), "state": "ONLINE", "guid": "5011111111111111111"},
                    {"type": "disk", "path": bid("sdc"), "state": "FAULTED", "guid": "5022222222222222222",
                     "r": 0, "w": 12, "c": 3, "note": "too many errors"},
                ]}],
                "spares": [{"type": "disk", "path": bid("sde"), "state": "AVAIL", "guid": "5033333333333333333"}],
                "scan": {"kind": "scrub", "state": "finished", "when": finished, "errors": 0, "repaired": "0B"},
                "history": [time.strftime("%Y-%m-%d.%H:%M:%S", time.localtime(now - 400 * day))
                            + " zpool create -o ashift=12 tank mirror sdb sdc",
                            time.strftime("%Y-%m-%d.%H:%M:%S", time.localtime(now - 2 * day))
                            + " zfs snapshot tank/home@daily-2026-10-26"],
                "events": ["Oct 27 2026 09:01:12.000000000 ereport.fs.zfs.io",
                           "Oct 27 2026 09:01:13.000000000 resource.fs.zfs.statechange"],
            },
            "backup": {
                "name": "backup", "guid": "9876543210987654321", "size": 3990 * GB, "frag": 11,
                "props": {"ashift": ["12", "local"], "autoexpand": ["off", "default"], "autotrim": ["off", "default"],
                          "feature@async_destroy": ["enabled", "local"], "feature@draid": ["disabled", "local"],
                          "feature@zilsaxattr": ["disabled", "local"]},
                "vdevs": [{"type": "raidz1", "name": "raidz1-0", "guid": "4022222222222222222", "children": [
                    {"type": "disk", "path": bid("sdf"), "state": "ONLINE", "guid": "6011111111111111111"},
                    {"type": "disk", "path": bid("sdg"), "state": "ONLINE", "guid": "6022222222222222222"},
                    {"type": "disk", "path": os.path.join(root, "by-id", "ata-ST2000DM008-2FR102_ZFL0GONE-part1"),
                     "state": "UNAVAIL", "guid": "6033333333333333333", "missing": True},
                ]}],
                "scan": {"kind": "scrub", "state": "running", "since": since, "pct": 25.0},
                "frozen_scan": True,
                "history": [],
            },
        },
        "datasets": {
            "tank": {"type": "filesystem", "props": {"compression": "lz4", "atime": "off"}, "data": 200000,
                     "guid": "7000000000000000001", "mounted": True, "creation": now - 400 * day},
            "tank/home": {"type": "filesystem", "props": {}, "data": 810 * GB, "guid": "7000000000000000002",
                          "mounted": True, "ratio": "1.42", "creation": now - 400 * day},
            "tank/home/alice": {"type": "filesystem", "props": {}, "data": 120 * GB, "guid": "7000000000000000003",
                                "mounted": True, "creation": now - 300 * day},
            "tank/media": {"type": "filesystem", "props": {"quota": str(1 * TB), "recordsize": "1048576"},
                           "data": 950 * GB, "guid": "7000000000000000004", "mounted": True, "creation": now - 200 * day},
            "tank/secure": {"type": "filesystem", "props": {"encryption": "aes-256-gcm", "keyformat": "passphrase"},
                            "data": 5 * GB, "guid": "7000000000000000005", "mounted": False, "locked": True,
                            "creation": now - 100 * day},
            "tank/secure/docs": {"type": "filesystem", "props": {}, "data": 2 * GB, "guid": "7000000000000000006",
                                 "mounted": False, "creation": now - 100 * day},
            "tank/vm": {"type": "filesystem", "props": {"canmount": "off"}, "data": 100000,
                        "guid": "7000000000000000007", "mounted": False, "creation": now - 90 * day},
            "tank/vm/win10": {"type": "volume", "props": {}, "data": 60 * GB, "volsize": 100 * GB, "refres": 0,
                              "guid": "7000000000000000008", "creation": now - 90 * day},
            "tank/app": {"type": "filesystem", "props": {}, "data": 3 * GB, "guid": "7000000000000000009",
                         "mounted": True, "busy": True, "creation": now - 50 * day},
            "tank/home-restore": {"type": "filesystem", "props": {}, "data": 1 * GB, "guid": "7000000000000000010",
                                  "mounted": True, "origin": "tank/home@before-upgrade", "creation": now - 5 * day},
            "backup": {"type": "filesystem", "props": {"compression": "zstd"}, "data": 100000,
                       "guid": "7000000000000000011", "mounted": True, "creation": now - 700 * day},
            "backup/hosts": {"type": "filesystem", "props": {}, "data": 1200 * GB, "guid": "7000000000000000012",
                             "mounted": True, "creation": now - 700 * day},
        },
        "snapshots": {
            "tank/home@manual-2026-10-01": {"used": int(4.1 * GB), "refer": 790 * GB, "creation": now - 26 * day,
                                            "txg": 4000, "guid": "8000000000000000001", "holds": ["keep"]},
            "tank/home@before-upgrade": {"used": 300 * MB, "refer": 800 * GB, "creation": now - 6 * day,
                                         "txg": 4500, "guid": "8000000000000000002", "holds": []},
            "tank/home@daily-2026-10-26": {"used": int(1.2 * GB), "refer": 805 * GB, "creation": now - 1 * day,
                                           "txg": 4800, "guid": "8000000000000000003", "holds": []},
            "tank/home@daily-2026-10-27": {"used": 800 * MB, "refer": 809 * GB, "creation": now - 2 * 3600,
                                           "txg": 4900, "guid": "8000000000000000004", "holds": []},
            "tank/media@2026-09": {"used": 50 * GB, "refer": 900 * GB, "creation": now - 40 * day,
                                   "txg": 3000, "guid": "8000000000000000005", "holds": []},
            "tank/vm/win10@pre-update": {"used": 2 * GB, "refer": 58 * GB, "creation": now - 10 * day,
                                         "txg": 4300, "guid": "8000000000000000006", "holds": []},
            "backup/hosts@weekly-1": {"used": 20 * GB, "refer": 1100 * GB, "creation": now - 7 * day,
                                      "txg": 4400, "guid": "8000000000000000007", "holds": []},
        },
        "importable": [{"name": "archive", "id": "12345678901234567890", "state": "ONLINE", "foreign": True,
                        "saved": {"pool": {"name": "archive", "guid": "12345678901234567890", "size": 1990 * GB,
                                           "props": {"ashift": ["12", "local"]},
                                           "vdevs": [{"type": "disk", "path": os.path.join(root, "by-id", "ata-ARCHIVE-part1"),
                                                      "state": "ONLINE", "guid": "9100000000000000001"}],
                                           "scan": {}},
                                  "datasets": {"archive": {"type": "filesystem", "props": {}, "data": 500 * GB,
                                                           "guid": "7100000000000000001", "mounted": True}},
                                  "snapshots": {}}}],
    }
    path = os.path.join(root, "state.json")
    with open(path, "w") as f:
        json.dump(state, f, indent=1)
    return {"ZMAN_MOCK_STATE": path, "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
            "ZMAN_MOCK_ROOT": root}
