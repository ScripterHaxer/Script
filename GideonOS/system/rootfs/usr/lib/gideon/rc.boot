#!/bin/sh
# GideonOS early boot: kernel filesystems, identity, devices, then services.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

mnt() { # mnt TYPE TARGET OPTIONS - skips mounts the initramfs already moved here
    # (checks /proc/mounts: `mountpoint` is unreliable for overlayfs directories)
    if [ "$2" = /proc ]; then [ -e /proc/self ] && return 0
    else awk -v t="$2" '$2 == t { f = 1 } END { exit !f }' /proc/mounts && return 0; fi
    mount -t "$1" -o "$3" "$1" "$2"
}

mnt proc     /proc nosuid,noexec,nodev
# gideon.debug=1 on the kernel command line traces boot and service startup.
case " $(cat /proc/cmdline) " in *" gideon.debug=1 "*) export GIDEON_DEBUG=1; set -x ;; esac
mnt sysfs    /sys  nosuid,noexec,nodev
mnt devtmpfs /dev  nosuid,mode=0755
mkdir -p /dev/pts /dev/shm
mnt devpts   /dev/pts gid=5,mode=0620,ptmxmode=0666,nosuid,noexec
mnt tmpfs    /dev/shm nosuid,nodev,mode=1777
mnt tmpfs    /run  nosuid,nodev,mode=0755
mnt tmpfs    /tmp  nosuid,nodev,mode=1777
mkdir -p /run/gideon/services /run/gideon/sessions /run/gideon/media /run/user /var/log/gideon
chmod 0755 /run/user /run/gideon/sessions

echo 4 > /proc/sys/kernel/printk

# Identity from configuration (gideon-config set system.hostname NAME).
host="$(gideon-config get system.hostname gideon)"
hostname "$host"
echo "$host" > /etc/hostname
sed -i "s/^127\.0\.1\.1.*/127.0.1.1   $host/" /etc/hosts

# Devices: coldplug what already exists; the `devices` service (mdev -d)
# then handles hotplug events from the kernel over netlink.
mdev -s

gideon-service boot || echo "gideon: some services failed to start (gideon-service list)" > /dev/console

. /etc/os-release
echo "gideon: $PRETTY_NAME booted in $(cut -d' ' -f1 /proc/uptime)s" > /dev/kmsg
echo "GIDEONOS_BOOT_OK version=$VERSION_ID kernel=$(uname -r)" > /run/gideon/boot-status
