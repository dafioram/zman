"""Sample output of the real tools, in the formats zman parses (OpenZFS 2.1/2.2, C locale)."""

STATUS_TWO_POOLS = """\
  pool: rpool
 state: ONLINE
  scan: scrub repaired 0B in 00:01:02 with 0 errors on Sun Oct 11 00:25:03 2026
config:

\tNAME                                                     STATE     READ WRITE CKSUM
\trpool                                                    ONLINE       0     0     0
\t  mirror-0                                               ONLINE       0     0     0
\t    /dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S1-part3    ONLINE       0     0     0
\t    /dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S2-part3    ONLINE       0     0     0

errors: No known data errors

  pool: tank
 state: DEGRADED
status: One or more devices could not be used because the label is missing or
\tinvalid.  Sufficient replicas exist for the pool to continue
\tfunctioning in a degraded state.
action: Replace the device using 'zpool replace'.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-4J
  scan: resilver in progress since Tue Oct 27 10:00:00 2026
\t1.20T scanned at 1.50G/s, 600G issued at 750M/s, 2.40T total
\t150G resilvered, 25.00% done, 00:40:00 to go
config:

\tNAME                                             STATE     READ WRITE CKSUM
\ttank                                             DEGRADED     0     0     0
\t  raidz2-0                                       DEGRADED     0     0     0
\t    /dev/disk/by-id/ata-A-part1                  ONLINE       0     0     0
\t    /dev/disk/by-id/ata-B-part1                  ONLINE       0     0     2
\t    replacing-2                                  DEGRADED     0     0     0
\t      15782312322937497023                       UNAVAIL      0     0     0  was /dev/disk/by-id/ata-C-part1
\t      /dev/disk/by-id/ata-D-part1                ONLINE       0     0     0  (resilvering)
\t    /dev/disk/by-id/ata-E-part1                  ONLINE       0     0     0
\tspecial
\t  mirror-1                                       ONLINE       0     0     0
\t    /dev/disk/by-id/nvme-F-part1                 ONLINE       0     0     0
\t    /dev/disk/by-id/nvme-G-part1                 ONLINE       0     0     0
\tlogs
\t  /dev/disk/by-id/nvme-H-part2                   ONLINE       0     0     0
\tcache
\t  /dev/disk/by-id/nvme-I-part3                   ONLINE       0     0     0
\tspares
\t  /dev/disk/by-id/ata-J-part1                    AVAIL
\t  /dev/disk/by-id/ata-K-part1                    INUSE     currently in use

errors: 3 data errors, use '-v' for a list
"""

STATUS_GUIDS = STATUS_TWO_POOLS.replace(
    "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S1-part3", "11111111111111111111").replace(
    "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S2-part3", "22222222222222222222").replace(
    "mirror-0     ", "33333333333333333333")

STATUS_SCRUB_RUNNING = """\
  pool: data
 state: ONLINE
  scan: scrub in progress since Tue Oct 27 09:00:00 2026
\t3.01T scanned at 1.21G/s, 1.20T issued at 494M/s, 4.80T total
\t0B repaired, 25.10% done, 02:07:43 to go
config:

\tNAME                          STATE     READ WRITE CKSUM
\tdata                          ONLINE       0     0     0
\t  /dev/disk/by-id/ata-X-part1 ONLINE       0     0     0

errors: No known data errors
"""

STATUS_PAUSED = """\
  pool: data
 state: ONLINE
  scan: scrub paused since Tue Oct 27 11:00:00 2026
\tscrub started on Tue Oct 27 09:00:00 2026
\t3.01T scanned, 1.20T issued, 4.80T total
\t0B repaired, 25.10% done
config:

\tNAME                          STATE     READ WRITE CKSUM
\tdata                          ONLINE       0     0     0
\t  /dev/disk/by-id/ata-X-part1 ONLINE       0     0     0

errors: No known data errors
"""

STATUS_FAULTED_NEVER = """\
  pool: small
 state: ONLINE
status: One or more devices has experienced an unrecoverable error.  An
\tattempt was made to correct the error.  Applications are unaffected.
action: Determine if the device needs to be replaced, and clear the errors
\tusing 'zpool clear' or replace the device with 'zpool replace'.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-9P
  scan: none requested
config:

\tNAME            STATE     READ WRITE CKSUM
\tsmall           ONLINE       0     0     0
\t  /var/tmp/f1   ONLINE       0     0 1.20K
\t  /var/tmp/f2   OFFLINE      0     0     0

errors: No known data errors
"""

POOL_LIST = "rpool\t996432412672\t102341234567\t894091178105\t5\t10\tONLINE\t1234567890\n" \
            "tank\t15994458112000\t9001234567890\t6993223544110\t-\t56\tDEGRADED\t987654321\n" \
            "broken\tline\n"

POOL_GET = "size\t996432412672\t-\nautoexpand\ton\tlocal\nautotrim\toff\tdefault\nashift\t12\tlocal\n" \
           "expandsize\t-\t-\ncompatibility\toff\tdefault\nfeature@async_destroy\tenabled\tlocal\n" \
           "feature@draid\tdisabled\tlocal\n"

DATASET_ROW = "\t".join([
    "tank/home", "filesystem", "1000000", "5000000", "800000", "/tank/home", "yes", "on", "lz4", "1.42x",
    "0", "0", "0", "0", "131072", "-", "-", "off", "-", "-", "-", "150000", "800000", "50000", "0",
    "1700000000", "off", "off", "1234",
])
VOLUME_ROW = "\t".join([
    "tank/vm/disk", "volume", "2000000", "5000000", "900000", "-", "-", "-", "zstd", "1.10x",
    "0", "0", "0", "2100000", "-", "2000000", "16384", "aes-256-gcm", "tank/vm", "unavailable", "-", "0",
    "900000", "0", "1100000", "1700000001", "-", "off", "5678",
])

SNAPSHOT_ROWS = "\n".join([
    "tank/home@a\t1000\t800000\t1700000000\t100\t0\t\t11\toff",
    "tank/home@b\t2000\t810000\t1700000100\t200\t1\t\t12\toff",
    "tank/home@c\t3000\t820000\t1700000200\t300\t0\ttank/clone1,tank/clone2\t13\toff",
    "tank/home@d\t4000\t830000\t1700000300\t400\t0\t-\t14\toff",
    "garbage line",
])

DESTROY_PREVIEW = "destroy\ttank/home@a\ndestroy\ttank/home@b\nreclaim\t123456\n"
DESTROY_PREVIEW_OLD = "would destroy tank/home@a\nwould reclaim 4096\n"

LSBLK = """{
   "blockdevices": [
      {"name":"sda", "kname":"sda", "path":"/dev/sda", "size":500107862016, "type":"disk", "model":"Samsung SSD 870", "serial":"S6P", "rota":false, "tran":"sata", "fstype":null, "label":null, "mountpoint":null,
         "children": [
            {"name":"sda1", "kname":"sda1", "path":"/dev/sda1", "size":536870912, "type":"part", "fstype":"vfat", "mountpoint":"/boot/efi"},
            {"name":"sda2", "kname":"sda2", "path":"/dev/sda2", "size":499570991104, "type":"part", "fstype":"ext4", "mountpoint":"/"}
         ]
      },
      {"name":"sdb", "kname":"sdb", "path":"/dev/sdb", "size":"4000787030016", "type":"disk", "model":"WDC  WD40EFRX ", "serial":"WD-1", "rota":"1", "tran":"sata", "fstype":null, "mountpoint":null,
         "children": [
            {"name":"sdb1", "kname":"sdb1", "path":"/dev/sdb1", "size":4000776716288, "type":"part", "fstype":"zfs_member", "label":"tank", "mountpoint":null},
            {"name":"sdb9", "kname":"sdb9", "path":"/dev/sdb9", "size":8388608, "type":"part", "fstype":null, "mountpoint":null}
         ]
      },
      {"name":"sdc", "kname":"sdc", "path":"/dev/sdc", "size":4000787030016, "type":"disk", "model":"WDC", "serial":"WD-2", "rota":true, "tran":"sata", "fstype":null, "mountpoint":null},
      {"name":"sdd", "kname":"sdd", "path":"/dev/sdd", "size":3000592982016, "type":"disk", "model":"T", "serial":"T-3", "rota":true, "tran":"usb", "fstype":"LVM2_member", "mountpoint":null},
      {"name":"sde", "kname":"sde", "path":"/dev/sde", "size":2000398934016, "type":"disk", "model":"S", "serial":"S-4", "rota":true, "tran":"sata", "fstype":null, "mountpoint":null,
         "children": [
            {"name":"sde1", "kname":"sde1", "path":"/dev/sde1", "size":2000397885440, "type":"part", "fstype":"crypto_LUKS", "mountpoint":null,
               "children": [{"name":"luks-x", "kname":"dm-0", "path":"/dev/mapper/luks-x", "size":2000381108224, "type":"crypt", "fstype":"ext4", "mountpoint":"/home"}]}
         ]
      },
      {"name":"sdf", "kname":"sdf", "path":"/dev/sdf", "size":1000204886016, "type":"disk", "model":"M", "serial":"M-5", "rota":true, "tran":"sata", "fstype":null, "mountpoint":null,
         "children": [{"name":"sdf1", "kname":"sdf1", "path":"/dev/sdf1", "size":1000203837440, "type":"part", "fstype":"ext4", "label":"old", "mountpoint":null}]
      },
      {"name":"sdg", "kname":"sdg", "path":"/dev/sdg", "size":1000204886016, "type":"disk", "model":"M", "serial":"M-6", "rota":true, "tran":"sata", "fstype":"swap", "mountpoint":"[SWAP]"},
      {"name":"zd0", "kname":"zd0", "path":"/dev/zd0", "size":107374182400, "type":"disk"},
      {"name":"loop0", "kname":"loop0", "path":"/dev/loop0", "size":4096, "type":"loop"},
      {"name":"sr0", "kname":"sr0", "path":"/dev/sr0", "size":1073741312, "type":"rom"},
      {"name":"nvme0n1", "kname":"nvme0n1", "path":"/dev/nvme0n1", "size":1000204886016, "type":"disk", "model":"Samsung SSD 980 PRO\\u001b[31m", "serial":"S5", "rota":false, "tran":"nvme", "fstype":null, "mountpoint":null}
   ]
}"""

IMPORT = """\
   pool: archive
     id: 12345678901234567890
  state: ONLINE
 status: The pool was last accessed by another system.
 action: The pool can be imported using its name or numeric identifier and
\tthe '-f' flag.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-EY
 config:

\tarchive     ONLINE
\t  mirror-0  ONLINE
\t    sdx     ONLINE
\t    sdy     ONLINE

   pool: spare
     id: 999
  state: ONLINE
 action: The pool can be imported using its name or numeric identifier.
 config:

\tspare       ONLINE
\t  sdz       ONLINE
"""

HISTORY = """\
History for 'tank':
2026-01-01.10:00:00 zpool create -o ashift=12 tank mirror sdb sdc
2026-10-26.00:00:01 zfs snapshot tank/home@daily-2026-10-26
2026-10-27.00:00:01 zfs snapshot tank/home@daily-2026-10-27
"""


ARCSTATS = """\
13 1 0x01 123 33456 1234 5678
name                            type data
hits                            4    9700
misses                          4    300
size                            4    8000000000
c_max                           4    16000000000
"""
