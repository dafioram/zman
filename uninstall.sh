#!/bin/sh
# Remove zman: the launcher and the program. Asks first, and asks separately
# about the config. Never touches ZFS or its data.
#
#   ./uninstall.sh                 ask; keep the config unless you say otherwise
#   ./uninstall.sh --yes           no questions; keeps the config
#   ./uninstall.sh --yes --purge   no questions; removes the config too
#
# ZMAN_PREFIX (default /usr/local) and ZMAN_CONF_DIR (default /etc/zman) as for install.sh.
set -eu

APP=zman
PREFIX=${ZMAN_PREFIX:-/usr/local}
CONF_DIR=${ZMAN_CONF_DIR:-/etc/zman}
BIN="$PREFIX/bin/$APP"
LIB="$PREFIX/lib/$APP"
CONF="$CONF_DIR/$APP.conf"
MARKER=".$APP-install-marker"
TAG="# zman-launcher: installed by zman install.sh; uninstall.sh removes it"
CONF_HEAD="# zman configuration."

YES=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --purge) PURGE=1 ;;
        --help|-h)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "uninstall.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

die() { echo "uninstall.sh: $*" >&2; exit 1; }

ask() {
    printf '%s ' "$1"
    read -r answer || answer=""
    case "$answer" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# Check everything before removing anything.
remove_lib=0
remove_bin=0
if [ -L "$LIB" ]; then die "$LIB is a symlink; not touching it."; fi
if [ -e "$LIB" ]; then
    [ -f "$LIB/$MARKER" ] || die "$LIB was not installed by zman; not touching it."
    remove_lib=1
fi
if [ -L "$BIN" ]; then die "$BIN is a symlink; not touching it."; fi
if [ -e "$BIN" ]; then
    grep -qxF "$TAG" "$BIN" 2>/dev/null || die "$BIN is not zman's launcher; not touching it."
    remove_bin=1
fi

conf_ours=0
if [ -f "$CONF" ] && [ ! -L "$CONF" ] && [ ! -L "$CONF_DIR" ]; then
    first=$(head -n 1 "$CONF" 2>/dev/null || true)
    case "$first" in
        "$CONF_HEAD"*) conf_ours=1 ;;
    esac
fi

if [ "$remove_lib" -eq 0 ] && [ "$remove_bin" -eq 0 ] && [ "$conf_ours" -eq 0 ]; then
    echo "zman is not installed in $PREFIX. Nothing was changed."
    exit 0
fi

if [ "$YES" -eq 0 ]; then
    ask "Remove zman from $PREFIX? [y/N]" || { echo "Cancelled. Nothing was changed."; exit 0; }
fi

if [ "$remove_bin" -eq 1 ]; then rm -f "$BIN"; echo "Removed $BIN"; fi
if [ "$remove_lib" -eq 1 ]; then rm -rf "$LIB"; echo "Removed $LIB"; fi

if [ "$conf_ours" -eq 1 ]; then
    drop=0
    if [ "$PURGE" -eq 1 ]; then
        drop=1
    elif [ "$YES" -eq 0 ] && ask "Also remove the config $CONF? [y/N]"; then
        drop=1
    fi
    if [ "$drop" -eq 1 ]; then
        rm -f "$CONF"
        rmdir "$CONF_DIR" 2>/dev/null || true
        echo "Removed $CONF"
    else
        echo "Kept $CONF"
    fi
elif [ -e "$CONF" ]; then
    echo "Kept $CONF (it was not written by zman's installer)."
fi
echo "Done."
