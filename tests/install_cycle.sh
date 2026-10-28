#!/bin/sh
# install → reinstall → uninstall into a temporary prefix, plus the refusals.
# Needs no root and touches nothing outside its temporary folder.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export ZMAN_PREFIX="$WORK/prefix" ZMAN_CONF_DIR="$WORK/etc/zman"
export PYTHONDONTWRITEBYTECODE=1

fail() { echo "FAIL: $*" >&2; exit 1; }
check() { "$@" || fail "$*"; }

# 1. Fresh install.
"$SRC/install.sh" --yes >/dev/null
check test -x "$ZMAN_PREFIX/bin/zman"
check test -f "$ZMAN_PREFIX/lib/zman/.zman-install-marker"
check test -f "$ZMAN_CONF_DIR/zman.conf"
v=$("$ZMAN_PREFIX/bin/zman" --version)
[ "$v" = "zman $(cat "$SRC/VERSION")" ] || fail "version after install: $v"

# The installed copy must not depend on the source folder.
copy="$WORK/src"
cp -R "$SRC" "$copy"
rm -rf "$copy/.git"
next=$(( $(cat "$SRC/VERSION") + 1 ))
echo "$next" > "$copy/VERSION"
echo "# kept" >> "$ZMAN_CONF_DIR/zman.conf"

# 2. Reinstall a newer version in place, from another folder; the config is kept.
"$copy/install.sh" --yes >/dev/null
v=$("$ZMAN_PREFIX/bin/zman" --version)
[ "$v" = "zman $next" ] || fail "version after reinstall: $v"
grep -q "# kept" "$ZMAN_CONF_DIR/zman.conf" || fail "reinstall replaced the config"
rm -rf "$copy"
v=$("$ZMAN_PREFIX/bin/zman" --version)
[ "$v" = "zman $next" ] || fail "installed copy depends on its source folder"
for leftover in "$ZMAN_PREFIX"/lib/.zman-stage.* "$ZMAN_PREFIX"/lib/.zman-old.*; do
    [ -e "$leftover" ] && fail "staging folder left behind: $leftover"
done

# 3. Not a terminal → exit 2 with the standard message.
set +e
msg=$("$ZMAN_PREFIX/bin/zman" </dev/null 2>&1 >/dev/null)
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "non-terminal run exited $rc"
[ "$msg" = "zman is interactive; run it in a terminal." ] || fail "non-terminal message: $msg"

# 4. Uninstall with --purge removes everything it made.
"$SRC/uninstall.sh" --yes --purge >/dev/null
[ -e "$ZMAN_PREFIX/bin/zman" ] && fail "launcher left behind"
[ -e "$ZMAN_PREFIX/lib/zman" ] && fail "program left behind"
[ -e "$ZMAN_CONF_DIR/zman.conf" ] && fail "config left behind"

# 5. Refuse to overwrite or remove files zman did not create.
mkdir -p "$ZMAN_PREFIX/bin"
echo "#!/bin/sh" > "$ZMAN_PREFIX/bin/zman"
echo "echo someone else's zman" >> "$ZMAN_PREFIX/bin/zman"
if "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install overwrote a foreign launcher"; fi
grep -q "someone else" "$ZMAN_PREFIX/bin/zman" || fail "foreign launcher was changed"
if "$SRC/uninstall.sh" --yes >/dev/null 2>&1; then fail "uninstall removed a foreign launcher"; fi
check test -f "$ZMAN_PREFIX/bin/zman"
rm "$ZMAN_PREFIX/bin/zman"

mkdir -p "$ZMAN_PREFIX/lib/zman"
echo "data" > "$ZMAN_PREFIX/lib/zman/important"
if "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install overwrote a foreign folder"; fi
check test -f "$ZMAN_PREFIX/lib/zman/important"
rm -rf "$ZMAN_PREFIX/lib/zman"

ln -s /tmp "$ZMAN_PREFIX/lib/zman"
if "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install followed a symlink"; fi
rm "$ZMAN_PREFIX/lib/zman"

# 6. A config written by someone else is kept even with --purge.
"$SRC/install.sh" --yes >/dev/null
echo "[zman]" > "$ZMAN_CONF_DIR/zman.conf"
"$SRC/uninstall.sh" --yes --purge >/dev/null
check test -f "$ZMAN_CONF_DIR/zman.conf"

echo "install cycle: ok"
