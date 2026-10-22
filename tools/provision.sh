#!/usr/bin/env bash
#
# Runs INSIDE the debootstrap chroot. Not meant to be executed on the VPS host.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C.UTF-8

MODEL="${MODEL:?MODEL is required}"
XOS_PASSWORD="${XOS_PASSWORD:-${NALAR_PASSWORD:-xos}}"
TZ_NAME="${TZ_NAME:-Asia/Jakarta}"
KIOSK_USER="${KIOSK_USER:-xos}"
KIOSK_UID="${KIOSK_UID:-1000}"
SKIP_MODEL_PULL="${SKIP_MODEL_PULL:-0}"
BUILD_OLLAMA_PORT="${BUILD_OLLAMA_PORT:-11435}"

log()  { printf '\033[1;36m[chroot]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chroot] WARN:\033[0m %s\n' "$*" >&2; }

[[ -d /etc ]] || { echo "not in a chroot" >&2; exit 1; }

# ------------------------------------------------------------------ packages
# Some VPS networks have broken/slow IPv6 routes to Ubuntu mirrors. Prefer
# IPv4 for the build so apt does not stall on a black-holed AAAA record.
cat >/etc/apt/apt.conf.d/99xos-force-ipv4 <<'EOF'
Acquire::ForceIPv4 "true";
EOF

log "apt-get update..."
apt-get update

# Avoid blocking questions from packages pulled in by casper/ubiquity.
debconf-set-selections <<'EOF'
grub-pc grub-pc/install_devices_empty boolean true
keyboard-configuration keyboard-configuration/layoutcode string us
keyboard-configuration keyboard-configuration/modelcode string pc105
console-setup console-setup/charmap47 select UTF-8
locales locales/default_environment_locale select en_US.UTF-8
EOF

log "Memasang paket sistem, Xorg, Openbox, Tk, dan casper..."
apt-get install -y --no-install-recommends \
    systemd-sysv dbus dbus-x11 \
    linux-image-generic initramfs-tools casper \
    xserver-xorg-core \
    xserver-xorg-input-libinput \
    xserver-xorg-video-vesa \
    xserver-xorg-video-fbdev \
    xserver-xorg-video-vmware \
    xinit xauth x11-xserver-utils x11-utils \
    openbox unclutter \
    python3 python3-tk \
    fonts-dejavu-core \
    locales tzdata console-setup keyboard-configuration \
    sudo curl ca-certificates \
    iproute2 kmod pciutils usbutils less nano procps \
    zstd libgomp1

# ------------------------------------------------------------------ base config
log "Mengatur hostname, locale, timezone, dan keyboard..."
printf 'xos\n' >/etc/hostname
cat >/etc/hosts <<'EOF'
127.0.0.1 localhost
127.0.1.1 xos
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF

sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
sed -i 's/^# *id_ID.UTF-8 UTF-8/id_ID.UTF-8 UTF-8/' /etc/locale.gen
locale-gen
update-locale LANG=en_US.UTF-8

cat >/etc/default/keyboard <<'EOF'
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
EOF

ln -snf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
printf '%s\n' "$TZ_NAME" >/etc/timezone

# ------------------------------------------------------------------ kiosk user
log "Membuat user kiosk '$KIOSK_USER'..."
if ! id -u "$KIOSK_USER" >/dev/null 2>&1; then
    useradd --create-home --uid "$KIOSK_UID" --shell /bin/bash \
        --comment "xOS Kiosk" "$KIOSK_USER"
fi

for group in sudo video audio input render; do
    getent group "$group" >/dev/null 2>&1 || groupadd --system "$group"
    usermod -aG "$group" "$KIOSK_USER"
done

printf '%s:%s\n' "$KIOSK_USER" "$XOS_PASSWORD" | chpasswd
install -d -m 0755 -o "$KIOSK_USER" -g "$KIOSK_USER" "/home/$KIOSK_USER"

# ------------------------------------------------------------------ app files
log "Memasang xOS shell dan konfigurasi..."
install -d -m 0755 /opt/xos-shell /etc/xos
install -m 0755 /tmp/xos-config/xos_shell.py /opt/xos-shell/xos_shell.py
install -m 0644 /tmp/xos-config/system-prompt.txt /etc/xos/system-prompt.txt
install -m 0644 /tmp/xos-config/xos-shell.env /etc/xos/xos-shell.env
sed -i "s|^XOS_MODEL=.*|XOS_MODEL=$MODEL|" /etc/xos/xos-shell.env
install -m 0600 /dev/null /etc/xos/alpha-password
printf '%s' "$XOS_PASSWORD" >/etc/xos/alpha-password

cat >/usr/local/bin/xos-shell <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/xos-shell/xos_shell.py "$@"
EOF
chmod 0755 /usr/local/bin/xos-shell
ln -sf /usr/local/bin/xos-shell /usr/local/bin/nalar-shell

install -m 0755 /tmp/xos-config/xinitrc "/home/$KIOSK_USER/.xinitrc"
chown "$KIOSK_USER:$KIOSK_USER" "/home/$KIOSK_USER/.xinitrc"

install -d -m 0755 -o "$KIOSK_USER" -g "$KIOSK_USER" \
    "/home/$KIOSK_USER/.config/openbox"
chown -R "$KIOSK_USER:$KIOSK_USER" "/home/$KIOSK_USER"

# ------------------------------------------------------------------ services
log "Memasang unit systemd..."
install -m 0644 /tmp/xos-config/casper.conf /etc/casper.conf
sed -i "s|^export USERNAME=.*|export USERNAME=\"$KIOSK_USER\"|" /etc/casper.conf

install -m 0644 /tmp/xos-config/ollama.service /etc/systemd/system/ollama.service

install -m 0644 /tmp/xos-config/xos-kiosk.service /etc/systemd/system/xos-kiosk.service
sed -i "s|^User=.*|User=$KIOSK_USER|" /etc/systemd/system/xos-kiosk.service
sed -i "s|^Group=.*|Group=$KIOSK_USER|" /etc/systemd/system/xos-kiosk.service
sed -i "s|/home/xos|/home/$KIOSK_USER|g" /etc/systemd/system/xos-kiosk.service
sed -i "s|^Environment=USER=.*|Environment=USER=$KIOSK_USER|" /etc/systemd/system/xos-kiosk.service
sed -i "s|^Environment=LOGNAME=.*|Environment=LOGNAME=$KIOSK_USER|" /etc/systemd/system/xos-kiosk.service

install -m 0644 /tmp/xos-config/xos-seal.service /etc/systemd/system/xos-seal.service

install -m 0755 /tmp/xos-config/xos-seal.sh /usr/local/sbin/xos-seal
sed -i "s|^TARGET_USER=.*|TARGET_USER=\"$KIOSK_USER\"|" /usr/local/sbin/xos-seal

mkdir -p /etc/systemd/system/multi-user.target.wants
ln -sf /etc/systemd/system/ollama.service /etc/systemd/system/multi-user.target.wants/ollama.service
ln -sf /etc/systemd/system/xos-seal.service /etc/systemd/system/multi-user.target.wants/xos-seal.service
ln -sf /etc/systemd/system/xos-kiosk.service /etc/systemd/system/multi-user.target.wants/xos-kiosk.service
ln -sf /lib/systemd/system/multi-user.target /etc/systemd/system/default.target
rm -f /etc/systemd/system/getty.target.wants/getty@tty1.service

# ------------------------------------------------------------------ ollama
log "Memasang Ollama..."
if ! id -u ollama >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/ollama \
        --shell /usr/sbin/nologin ollama
fi
install -d -m 0750 -o ollama -g ollama /var/lib/ollama

# Upstream switched the Linux release asset from .tgz to .tar.zst in 2026.
# Keep a fallback so older mirrors and pinned versions still work.
ollama_archive=/tmp/ollama-linux-amd64.tar.zst
if ! curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 \
    https://ollama.com/download/ollama-linux-amd64.tar.zst \
    -o "$ollama_archive"; then
    warn "arsip .tar.zst gagal diunduh, mencoba .tgz lama..."
    ollama_archive=/tmp/ollama-linux-amd64.tgz
    curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 \
        https://ollama.com/download/ollama-linux-amd64.tgz \
        -o "$ollama_archive"
fi
tar --zstd -xf "$ollama_archive" -C /usr/local 2>/dev/null || \
    tar -xzf "$ollama_archive" -C /usr/local
rm -f "$ollama_archive"
[[ -x /usr/local/bin/ollama ]] || { echo "binary Ollama tidak ditemukan" >&2; exit 1; }
/usr/local/bin/ollama --version

if [[ "$SKIP_MODEL_PULL" != "1" ]]; then
    # The build host may already run Ollama (or a mock) on 11435. Pick the
    # first free loopback port so provisioning never collides with it.
    BUILD_OLLAMA_PORT="$(python3 - "$BUILD_OLLAMA_PORT" <<'PY'
import socket
import sys

first = int(sys.argv[1])
for port in [first, *range(11436, 11500)]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        continue
    else:
        print(port)
        raise SystemExit(0)
    finally:
        sock.close()
raise SystemExit("no free loopback port in 11435-11499")
PY
)"
    log "Mengunduh model '$MODEL' ke image (port build $BUILD_OLLAMA_PORT)..."
    mkdir -p /var/lib/ollama/.ollama/models
    chown -R ollama:ollama /var/lib/ollama

    OLLAMA_HOST="127.0.0.1:$BUILD_OLLAMA_PORT" \
    OLLAMA_MODELS=/var/lib/ollama/.ollama/models \
    HOME=/var/lib/ollama \
        /usr/local/bin/ollama serve >/tmp/ollama-build.log 2>&1 &
    ollama_pid=$!
    trap 'kill "$ollama_pid" 2>/dev/null || true' EXIT

    ready=0
    for _ in $(seq 1 120); do
        if curl -fsS "http://127.0.0.1:$BUILD_OLLAMA_PORT/api/version" >/dev/null 2>&1; then
            ready=1
            break
        fi
        if ! kill -0 "$ollama_pid" 2>/dev/null; then
            cat /tmp/ollama-build.log >&2 || true
            exit 1
        fi
        sleep 1
    done
    if [[ "$ready" != "1" ]]; then
        cat /tmp/ollama-build.log >&2 || true
        echo "Ollama build server tidak siap" >&2
        exit 1
    fi

    OLLAMA_HOST="127.0.0.1:$BUILD_OLLAMA_PORT" \
        /usr/local/bin/ollama pull "$MODEL"

    OLLAMA_HOST="127.0.0.1:$BUILD_OLLAMA_PORT" \
        /usr/local/bin/ollama list

    python3 - "$MODEL" "$BUILD_OLLAMA_PORT" <<'PY'
import json
import sys
import urllib.request

model, port = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(
    f"http://127.0.0.1:{port}/api/tags", timeout=15
) as response:
    data = json.load(response)
names = {item.get("name", "") for item in data.get("models", [])}
if model not in names and not any(
    name.split(":", 1)[0] == model for name in names
):
    print(f"model {model!r} tidak ditemukan; tersedia: {sorted(names)}", file=sys.stderr)
    raise SystemExit(1)
print(f"model OK: {model}")
PY

    kill "$ollama_pid" 2>/dev/null || true
    wait "$ollama_pid" 2>/dev/null || true
    trap - EXIT
    chown -R ollama:ollama /var/lib/ollama
    rm -f /tmp/ollama-build.log
else
    warn "SKIP_MODEL_PULL=1: ISO akan boot tetapi model belum tersedia."
fi

# ------------------------------------------------------------------ initramfs
log "Membangun initramfs dengan hook casper..."
update-initramfs -u -k all

log "Membersihkan cache build..."
apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
rm -rf /root/.cache "/home/$KIOSK_USER/.cache"
rm -rf /tmp/xos-config /tmp/nalar-os-config /tmp/ollama-build.log
: >/etc/machine-id
ln -snf /etc/machine-id /var/lib/dbus/machine-id
: >/etc/resolv.conf

log "Verifikasi cepat..."
python3 -m py_compile /opt/xos-shell/xos_shell.py
python3 -c 'import tkinter' >/dev/null
test -x /usr/local/bin/xos-shell
test -x /usr/local/bin/ollama
test -f "/home/$KIOSK_USER/.xinitrc"
log "Provisioning chroot selesai."
