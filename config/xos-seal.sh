#!/bin/sh
# Casper creates a passwordless live user with passwordless sudo on every
# boot. This alpha image is local-only, but we still remove Casper's sudoers
# drop-in and make the kiosk account unloginable before the kiosk starts.
#
# The account is NOT locked with `passwd -l`: xos-kiosk.service runs with
# PAMName=login, and some PAM account stacks reject locked accounts, which
# would prevent the shell from starting. Instead we install an unguessable
# random password that is never stored anywhere. Console login is therefore
# impossible while the service still starts normally.
set -eu

TARGET_USER="xos"

rm -f /etc/sudoers.d/casper

if ! id "$TARGET_USER" >/dev/null 2>&1; then
    exit 0
fi

if [ -s /etc/xos/alpha-password ]; then
    # Explicitly enabled debug console: restore the build-time password.
    password="$(cat /etc/xos/alpha-password)"
    printf '%s:%s\n' "$TARGET_USER" "$password" | chpasswd
else
    # No stored credential: random, discarded, unguessable password.
    password="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
    printf '%s:%s\n' "$TARGET_USER" "$password" | chpasswd
fi
unset password
