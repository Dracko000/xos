#!/usr/bin/env bash
#
# xOS alpha ISO builder
#
# Usage:
#   sudo ./build.sh host              # install build dependencies on the VPS
#   sudo ./build.sh all               # bootstrap + provision + pack ISO
#   sudo ./build.sh rootfs            # bootstrap/provision the chroot only
#   sudo ./build.sh iso               # repack the ISO from the existing chroot
#   sudo ./build.sh clean             # remove the build tree and output ISO
#
# Useful overrides:
#   MODEL=phi3:mini                   # or llama3.1:8b
#   XOS_PASSWORD=xos                  # local alpha console password
#   BUILD_DIR=/srv/xos
#   OUT=dist/xos-alpha.iso
#   SKIP_MODEL_PULL=1                 # build bootable ISO without weights
#   FORCE_ROOTFS=1                    # debootstrap from scratch
#   NEED_GB=14
#
# The generated image is a LIVE image. It boots read-only from the ISO with a
# RAM-backed writable overlay; changes are lost on reboot.
set -Eeuo pipefail
IFS=$'\n\t'

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-/srv/xos}"
ROOTFS="${ROOTFS:-$BUILD_DIR/rootfs}"
ISO_TREE="${ISO_TREE:-$BUILD_DIR/iso}"
DIST_DIR="${DIST_DIR:-$PROJECT_DIR/dist}"
OUT="${OUT:-$DIST_DIR/xos-alpha.iso}"

DISTRO="${DISTRO:-noble}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"
SECURITY_MIRROR="${SECURITY_MIRROR:-http://security.ubuntu.com/ubuntu}"
ARCH="${ARCH:-amd64}"
MODEL="${MODEL:-phi3:mini}"
XOS_PASSWORD="${XOS_PASSWORD:-${NALAR_PASSWORD:-}}"
TZ_NAME="${TZ_NAME:-Asia/Jakarta}"
SKIP_MODEL_PULL="${SKIP_MODEL_PULL:-0}"
FORCE_ROOTFS="${FORCE_ROOTFS:-0}"
NEED_GB="${NEED_GB:-14}"
SQUASHFS_LEVEL="${SQUASHFS_LEVEL:-15}"
KIOSK_USER="${KIOSK_USER:-${XOS_USER:-}}"
KIOSK_UID="1000"

MOUNTS=()
HOST_DEPS=(
    debootstrap
    squashfs-tools
    xorriso
    grub-pc-bin
    grub-efi-amd64-bin
    grub-common
    mtools
    dosfstools
    rsync
    curl
    ca-certificates
    zstd
    xz-utils
    file
)

log()  { printf '\033[1;36m[xos]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[xos] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[xos] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

require_root() {
    [[ ${EUID} -eq 0 ]] || die "jalankan sebagai root: sudo $0 ${1:-all}"
}

prompt_credentials() {
    local user_input pass_input pass_confirm

    # 1. Kiosk Username
    KIOSK_USER="${KIOSK_USER:-${XOS_USER:-}}"
    if [[ -z "$KIOSK_USER" ]]; then
        if [[ -t 0 ]]; then
            while true; do
                printf '\033[1;36m[xos]\033[0m Konfigurasi Username kiosk [default: xos]: '
                read -r user_input
                user_input="${user_input:-xos}"
                if [[ "$user_input" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
                    KIOSK_USER="$user_input"
                    break
                fi
                warn "Username hanya boleh huruf kecil, angka, underscore, atau dash (harus diawali huruf/underscore)."
            done
        else
            KIOSK_USER="xos"
        fi
    fi

    # 2. Kiosk Password
    XOS_PASSWORD="${XOS_PASSWORD:-${NALAR_PASSWORD:-}}"
    if [[ -z "$XOS_PASSWORD" ]]; then
        if [[ -t 0 ]]; then
            while true; do
                printf '\033[1;36m[xos]\033[0m Masukkan password untuk user (%s): ' "$KIOSK_USER"
                read -rs pass_input
                echo
                if [[ -z "$pass_input" ]]; then
                    warn "Password tidak boleh kosong."
                    continue
                fi
                if [[ "$pass_input" == *:* ]]; then
                    warn "Password tidak boleh mengandung karakter ':'."
                    continue
                fi
                if [[ "$pass_input" == *$'\n'* ]]; then
                    warn "Password tidak boleh mengandung newline."
                    continue
                fi
                printf '\033[1;36m[xos]\033[0m Konfirmasi password: '
                read -rs pass_confirm
                echo
                if [[ "$pass_input" != "$pass_confirm" ]]; then
                    warn "Konfirmasi password tidak cocok. Silakan coba lagi."
                    continue
                fi
                XOS_PASSWORD="$pass_input"
                break
            done
        else
            die "XOS_PASSWORD harus diset jika menjalankan dalam mode non-interaktif (contoh: XOS_PASSWORD='...' ./build.sh all)"
        fi
    fi

    log "Kredensial siap: user='$KIOSK_USER'"
}

validate_inputs() {
    [[ "$MODEL" =~ ^[A-Za-z0-9._:/-]+$ ]] || die "MODEL mengandung karakter tidak valid"
    [[ "$TZ_NAME" =~ ^[A-Za-z0-9._+/-]+$ ]] || die "TZ_NAME tidak valid"
    [[ "$KIOSK_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "KIOSK_USER tidak valid"
    [[ -n "$XOS_PASSWORD" ]] || die "XOS_PASSWORD tidak boleh kosong"
    [[ "$XOS_PASSWORD" != *$'\n'* ]] || die "XOS_PASSWORD tidak boleh mengandung newline"
    [[ "$XOS_PASSWORD" != *:* ]] || die "XOS_PASSWORD tidak boleh mengandung karakter ':'"
}

cleanup_mounts() {
    local i mountpoint
    for ((i = ${#MOUNTS[@]} - 1; i >= 0; i--)); do
        mountpoint="${MOUNTS[$i]}"
        if mountpoint -q "$mountpoint" 2>/dev/null; then
            umount "$mountpoint" 2>/dev/null || umount -l "$mountpoint" 2>/dev/null || true
        fi
    done
    MOUNTS=()
}

mount_chroot() {
    local root="$1"
    mkdir -p "$root/dev" "$root/dev/pts" "$root/proc" "$root/sys" "$root/run"

    if ! mountpoint -q "$root/dev"; then
        mount --bind /dev "$root/dev"
        MOUNTS+=("$root/dev")
    fi
    if ! mountpoint -q "$root/dev/pts"; then
        mount --bind /dev/pts "$root/dev/pts"
        MOUNTS+=("$root/dev/pts")
    fi
    if ! mountpoint -q "$root/proc"; then
        mount -t proc proc "$root/proc"
        MOUNTS+=("$root/proc")
    fi
    if ! mountpoint -q "$root/sys"; then
        mount -t sysfs sysfs "$root/sys"
        MOUNTS+=("$root/sys")
    fi
    if ! mountpoint -q "$root/run"; then
        mount -t tmpfs -o nosuid,nodev,mode=0755 tmpfs "$root/run"
        MOUNTS+=("$root/run")
    fi
}

check_space() {
    mkdir -p "$BUILD_DIR" "$DIST_DIR"
    local avail_kb need_kb
    avail_kb="$(df -Pk "$BUILD_DIR" | awk 'NR==2 {print $4}')"
    need_kb=$((NEED_GB * 1024 * 1024))
    if ((avail_kb < need_kb)); then
        die "ruang disk kurang: butuh ${NEED_GB} GiB, tersedia $((avail_kb / 1024 / 1024)) GiB di $BUILD_DIR"
    fi
}

stage_host() {
    require_root host
    log "Memasang dependensi build di VPS..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${HOST_DEPS[@]}"
    log "Dependensi siap."
}

bootstrap_rootfs() {
    if [[ -e "$ROOTFS/etc/debian_version" && "$FORCE_ROOTFS" != "1" ]]; then
        log "Rootfs sudah ada: $ROOTFS (lewati debootstrap)"
        return
    fi
    if [[ "$FORCE_ROOTFS" == "1" ]]; then
        log "FORCE_ROOTFS=1: menghapus rootfs lama..."
        cleanup_mounts
        rm -rf "$ROOTFS"
    fi

    mkdir -p "$ROOTFS"
    log "debootstrap $DISTRO ($ARCH) ke $ROOTFS ..."
    debootstrap \
        --arch="$ARCH" \
        --variant=minbase \
        --components=main,restricted,universe,multiverse \
        --include=ca-certificates,curl,gnupg,locales,tzdata \
        "$DISTRO" "$ROOTFS" "$MIRROR"

    cat >"$ROOTFS/etc/apt/sources.list" <<EOF
deb $MIRROR $DISTRO main restricted universe multiverse
deb $MIRROR $DISTRO-updates main restricted universe multiverse
deb $MIRROR $DISTRO-backports main restricted universe multiverse
deb $SECURITY_MIRROR $DISTRO-security main restricted universe multiverse
EOF
}

provision_rootfs() {
    log "Menyiapkan mount chroot..."
    mount_chroot "$ROOTFS"

    # Avoid a dangling systemd-resolved symlink inside the build chroot.
    rm -f "$ROOTFS/etc/resolv.conf"
    cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

    rm -rf "$ROOTFS/tmp/xos-config"
    mkdir -p "$ROOTFS/tmp/xos-config"
    cp -a "$PROJECT_DIR/config/." "$ROOTFS/tmp/xos-config/"
    install -Dm755 "$PROJECT_DIR/shell/xos_shell.py" \
        "$ROOTFS/tmp/xos-config/xos_shell.py"
    install -Dm755 "$PROJECT_DIR/tools/provision.sh" \
        "$ROOTFS/tmp/provision.sh"

    log "Menjalankan provisioning di chroot (paket, Ollama, model, systemd)..."
    if ! chroot "$ROOTFS" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        MODEL="$MODEL" \
        XOS_PASSWORD="$XOS_PASSWORD" \
        NALAR_PASSWORD="$XOS_PASSWORD" \
        TZ_NAME="$TZ_NAME" \
        KIOSK_USER="$KIOSK_USER" \
        KIOSK_UID="$KIOSK_UID" \
        SKIP_MODEL_PULL="$SKIP_MODEL_PULL" \
        /bin/bash /tmp/provision.sh; then
        die "provisioning gagal. Periksa log di atas."
    fi

    rm -rf "$ROOTFS/tmp/xos-config" "$ROOTFS/tmp/provision.sh"
    cleanup_mounts
    log "Provisioning selesai."
}

stage_rootfs() {
    require_root rootfs
    prompt_credentials
    validate_inputs
    check_space
    bootstrap_rootfs
    provision_rootfs
}

make_boot_images() {
    local iso_tree="$1"
    local work="$BUILD_DIR/boot"
    local cfg="$iso_tree/boot/grub/grub.cfg"
    local efi_img="$iso_tree/boot/grub/efiboot.img"
    local bios_img="$iso_tree/boot/grub/bios.img"
    local core_img="$work/core.img"
    local grubx64="$work/grubx64.efi"
    local bios_modules efi_modules
    bios_modules="linux normal iso9660 biosdisk search search_fs_file all_video video gfxterm font test true part_msdos part_gpt boot"
    efi_modules="linux normal iso9660 search search_fs_file all_video video gfxterm font test true part_msdos part_gpt boot"

    rm -rf "$work"
    mkdir -p "$work" "$iso_tree/EFI/BOOT"

    log "Membuat GRUB BIOS core..."
    grub-mkstandalone \
        --format=i386-pc \
        --output="$core_img" \
        --locales="" \
        --fonts="" \
        --themes="" \
        --compress=xz \
        --install-modules="$bios_modules" \
        --modules="$bios_modules" \
        "boot/grub/grub.cfg=$cfg"

    cat /usr/lib/grub/i386-pc/cdboot.img "$core_img" >"$bios_img"

    log "Membuat GRUB EFI standalone..."
    grub-mkstandalone \
        --format=x86_64-efi \
        --output="$grubx64" \
        --locales="" \
        --fonts="" \
        --themes="" \
        --compress=xz \
        --install-modules="$efi_modules" \
        --modules="$efi_modules" \
        "boot/grub/grub.cfg=$cfg"

    dd if=/dev/zero of="$efi_img" bs=1M count=16 status=none
    mkfs.vfat -F 16 -n XOSEFI "$efi_img" >/dev/null
    mmd -i "$efi_img" ::/EFI ::/EFI/BOOT
    mcopy -i "$efi_img" "$grubx64" ::/EFI/BOOT/BOOTX64.EFI
    cp "$grubx64" "$iso_tree/EFI/BOOT/BOOTX64.EFI"
}

pack_iso() {
    require_root iso
    [[ -e "$ROOTFS/etc/debian_version" ]] || die "rootfs belum ada. Jalankan: sudo $0 rootfs"
    cleanup_mounts

    local kver
    kver="$(find "$ROOTFS/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -n 1)"
    [[ -n "$kver" ]] || die "tidak menemukan kernel di $ROOTFS/lib/modules"
    [[ -f "$ROOTFS/boot/vmlinuz-$kver" ]] || die "vmlinuz-$kver tidak ditemukan"
    [[ -f "$ROOTFS/boot/initrd.img-$kver" ]] || die "initrd.img-$kver tidak ditemukan"

    log "Menyiapkan ISO tree..."
    rm -rf "$ISO_TREE"
    mkdir -p "$ISO_TREE/casper" "$ISO_TREE/boot/grub" "$ISO_TREE/.disk"

    cp "$ROOTFS/boot/vmlinuz-$kver" "$ISO_TREE/casper/vmlinuz"
    cp "$ROOTFS/boot/initrd.img-$kver" "$ISO_TREE/casper/initrd"
    install -m 0644 "$PROJECT_DIR/config/grub.cfg" "$ISO_TREE/boot/grub/grub.cfg"
    printf 'xOS alpha 0.1 amd64 - built %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        >"$ISO_TREE/.disk/info"

    # Keep the top-level mount-point directories (/dev, /proc, /sys, /run,
    # /tmp) in the squashfs. Casper and systemd must be able to mount onto
    # them; excluding the directories themselves causes init to panic.
    cat >"$BUILD_DIR/squashfs-exclude.txt" <<EOF
proc/*
sys/*
dev/*
run/*
tmp/*
var/cache/apt/archives/*
var/cache/apt/pkgcache.bin
var/cache/apt/srcpkgcache.bin
var/lib/apt/lists/*
root/.cache/*
home/$KIOSK_USER/.cache/*
home/nalar/.cache/*
EOF

    log "Membuat squashfs (zstd level $SQUASHFS_LEVEL). Ini bisa lama..."
    mksquashfs "$ROOTFS" "$ISO_TREE/casper/filesystem.squashfs" \
        -noappend \
        -comp zstd \
        -Xcompression-level "$SQUASHFS_LEVEL" \
        -wildcards \
        -ef "$BUILD_DIR/squashfs-exclude.txt" \
        -no-progress

    du -sx --block-size=1 "$ROOTFS" | cut -f1 >"$ISO_TREE/casper/filesystem.size"
    chroot "$ROOTFS" dpkg-query -W --showformat='${Package} ${Version}\n' \
        >"$ISO_TREE/casper/filesystem.manifest"

    make_boot_images "$ISO_TREE"

    log "Membuat ISO hybrid BIOS+UEFI..."
    mkdir -p "$DIST_DIR"
    rm -f "$OUT" "$OUT.sha256"
    xorriso -as mkisofs \
        -iso-level 3 \
        -full-iso9660-filenames \
        -r \
        -joliet \
        -joliet-long \
        -volid "XOS" \
        -publisher "xOS alpha" \
        -eltorito-boot boot/grub/bios.img \
            -no-emul-boot \
            -boot-load-size 4 \
            -boot-info-table \
            --grub2-boot-info \
            --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
        -eltorito-alt-boot \
            -e boot/grub/efiboot.img \
            -no-emul-boot \
            -isohybrid-gpt-basdat \
            -append_partition 2 0xef "$ISO_TREE/boot/grub/efiboot.img" \
        -o "$OUT" \
        "$ISO_TREE"

    # Store a relative filename so the checksum file stays valid after the
    # ISO is copied to another machine.
    ( cd "$DIST_DIR" && sha256sum "$(basename "$OUT")" >"$(basename "$OUT").sha256" )
    log "ISO selesai: $OUT"
    log "Ukuran: $(du -h "$OUT" | cut -f1)"
    log "SHA256: $(cut -d' ' -f1 "$OUT.sha256")"
    log "Selanjutnya: salin ISO ke host VirtualBox dan boot sebagai CD/DVD."
}

stage_clean() {
    require_root clean
    log "Membersihkan build tree dan output..."
    cleanup_mounts
    rm -rf "$BUILD_DIR"
    rm -f "$OUT" "$OUT.sha256"
    log "Selesai."
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    trap cleanup_mounts EXIT

    case "${1:-all}" in
        host)   stage_host ;;
        rootfs) stage_rootfs ;;
        iso)    pack_iso ;;
        all)
            require_root all
            prompt_credentials
            stage_host
            stage_rootfs
            pack_iso
            ;;
        clean)  stage_clean ;;
        -h|--help|help) usage ;;
        *) die "perintah tidak dikenal: $1 (gunakan host|rootfs|iso|all|clean)" ;;
    esac
fi
