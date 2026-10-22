#!/bin/sh
# Casper creates a passwordless live user with passwordless sudo on every
# boot. This alpha image is local-only, but we still restore a known password
# and remove Casper's sudoers drop-in before the kiosk starts.
set -eu

TARGET_USER="xos"

rm -f /etc/sudoers.d/casper

if ! id "$TARGET_USER" >/dev/null 2>&1; then
    exit 0
fi

if [ -s /etc/xos/alpha-password ]; then
    password="$(cat /etc/xos/alpha-password)"
    printf '%s:%s\n' "$TARGET_USER" "$password" | chpasswd
else
    passwd -l "$TARGET_USER" >/dev/null 2>&1 || true
fi
