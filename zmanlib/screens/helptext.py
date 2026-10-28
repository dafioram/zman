"""Help texts. Each list explains its keys, then its columns and status words."""
from __future__ import annotations

LIST_KEYS = """\
Keys
  1-N      open the item with that number (numbers count from the top of the page)
  Enter    refresh: read the live state again
  n / p    next / previous page
  s        choose how the list is sorted
  f        filter: type words that must all match; a blank filter clears it
  h        this help
  b        back    m  main menu    q  quit
  Ctrl-C at a prompt is the same as b; Ctrl-D quits.
"""

MAIN = """\
zman manages ZFS on this machine. It runs the zfs and zpool tools for you and
shows the exact command before anything long or destructive.

Words ZFS uses
  pool       Disks grouped into one storage space. Everything lives in a pool.
  vdev       A building block of a pool: one disk, a mirror or a RAIDZ group.
  mirror     Two or more disks holding the same data. Works while one is left.
  RAIDZ      Disks with parity. RAIDZ1/2/3 survive 1/2/3 failed disks.
  dataset    A filesystem inside a pool, with its own settings and snapshots.
  volume     A block device inside a pool (zvol), e.g. for a VM disk.
  snapshot   A read-only copy of a dataset at one moment. Costs only the space
             of data that changed since.
  clone      A writable dataset made from a snapshot.
  hold       A tag that stops a snapshot from being destroyed.
  scrub      Reads every block and checks it, repairing from redundancy.
  resilver   Copies data onto a new or returning disk.
  trim       Tells SSDs which blocks are free.
  ARC        ZFS's read cache in memory.

Keys everywhere
  numbers pick things · Enter refresh · b back · m main menu · q quit
  Ctrl-C at a prompt goes back; while a command runs it stops that command.
  Ctrl-D quits.

Sizes are 1000-based (1 GB = 1,000,000,000 bytes), the way drives are sold.
When you type a size, 500G means 500 GB; write 500GiB for 1024-based.

zman never schedules anything. For automatic snapshots use a tool such as
sanoid or zfs-auto-snapshot; their snapshots show up here as usual.
"""

POOLS = LIST_KEYS + """\
  c        create a new pool from free disks
  i        import a pool that is on disks attached to this machine

Columns
  HEALTH   ONLINE is healthy. DEGRADED means a device failed but the data is
           still safe; replace the device soon. FAULTED or UNAVAIL means the
           pool cannot work until devices come back.
  SIZE / ALLOC / FREE / CAP   raw pool space; datasets see a little less.
  FRAG     free-space fragmentation; high values slow writes.
  SCRUB    the last or current scrub or resilver, and errors it found.
"""

DEVICES = LIST_KEYS + """\
Rows are the pool's layout as a tree: the pool, its vdevs (mirror-0,
raidz1-0), and the disks in them. Sections like logs, cache and spares list
special devices.

Columns
  STATE    ONLINE is healthy. DEGRADED: a member below has a problem.
           FAULTED: ZFS stopped using it after too many errors.
           UNAVAIL / REMOVED: the device is missing.
           OFFLINE: taken offline on purpose. AVAIL / INUSE: spare states.
           "N errors" counts read, write and checksum errors since the last
           clear. (resilvering) means data is being copied onto it.
  SIZE     the disk or partition size.
  SERIAL   the drive's serial number, printed on its label.
"""

DATASETS = LIST_KEYS + """\
Columns
  STATE    mounted, not mounted (yellow when it should be), locked (encryption
           key not loaded), near quota / quota full (90% / 99% of its quota),
           not mountable (canmount=off or no mountpoint), legacy mount
           (mounted through /etc/fstab instead).
  USED     space used by the dataset, its children and its snapshots.
  AVAIL    space it can still use.
  REFER    data the dataset itself holds right now.
  MOUNTPOINT   where its files appear.
"""

VOLUMES = LIST_KEYS + """\
Columns
  STATE    ok, locked (encryption key not loaded), near quota / quota full.
  SIZE     the size the volume shows to whatever uses it.
  USED     space it takes in the pool, including snapshots.
  DEVICE   the block device path, /dev/zvol/<pool>/<name>.
"""

SNAPSHOTS = LIST_KEYS + """\
Columns
  CREATED  how long ago it was taken.
  USED     space only this snapshot holds: what destroying it alone would
           free. Several snapshots can share data, so destroying a group can
           free more than the sum of their USED.
  REFER    the size of the data as it was at that moment.
  HOLD     hold tags; a held snapshot cannot be destroyed until released.
  CLONES   datasets made from it; it cannot be destroyed while they exist.
"""

DISKS = LIST_KEYS + """\
Columns
  USE      pool <name>: part of that pool (its state if not ONLINE).
           free: blank, can be used for a pool.
           free, has <fs> data / partitions / old ZFS label: not in use right
           now, but holds something; using it erases that.
           mounted, swap, LVM member, RAID member, in use: used by the system;
           zman never offers these.
  TYPE     HDD, SSD or NVMe.
  BY-ID    the stable name zman gives to zpool, so the pool finds the disk
           even when sdX names change at boot.
"""

IOSTAT = LIST_KEYS + """\
  a        auto-refresh: redraw every few seconds until Ctrl-C

Each refresh measures for one second.
Columns
  READ OPS / WRITE OPS   operations per second.
  READ / WRITE           bytes per second.
  ALLOC / FREE           space used and free.
"""

SPACE = LIST_KEYS + """\
Rows are datasets and volumes, largest first.
Columns
  USED       everything the dataset uses: DATA + SNAPS + CHILDREN + RESERVED.
  DATA       its own files right now.
  SNAPS      space only its snapshots hold.
  CHILDREN   space used by datasets below it.
"""
