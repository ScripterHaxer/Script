#!/bin/sh
# GideonOS early boot: bring up kernel filesystems, devices and services.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

mnt() { # mnt TYPE TARGET OPTIONS
    mountpoint -q "$2" || mount -t "$1" -o "$3" "$1" "$2"
}

mnt proc     /proc nosuid,noexec,nodev
mnt sysfs    /sys  nosuid,noexec,nodev
mnt devtmpfs /dev  nosuid,mode=0755
mkdir -p /dev/pts /dev/shm
mnt devpts   /dev/pts gid=5,mode=0620,ptmxmode=0666,nosuid,noexec
mnt tmpfs    /dev/shm nosuid,nodev,mode=1777
mnt tmpfs    /run  nosuid,nodev,mode=0755
mnt tmpfs    /tmp  nosuid,nodev,mode=1777
mkdir -p /run/gideon /var/log /var/run 2>/dev/null

# Kernel log level: keep the console readable.
echo 4 > /proc/sys/kernel/printk

hostname -F /etc/hostname

# Device discovery: mdev handles hotplug and coldplugs existing devices.
echo /sbin/mdev > /proc/sys/kernel/hotplug
mdev -s

ip link set lo up

# Services: each enabled service is an executable script in services.d,
# started in lexical order with the argument "start".
for svc in /etc/gideon/services.d/*; do
    [ -x "$svc" ] || continue
    "$svc" start || echo "gideon: service $(basename "$svc") failed to start" >&2
done

. /etc/os-release
uptime_s=$(cut -d' ' -f1 /proc/uptime)
echo "gideon: $PRETTY_NAME booted in ${uptime_s}s" > /dev/kmsg
echo "GIDEONOS_BOOT_OK version=$VERSION_ID kernel=$(uname -r)" > /run/gideon/boot-status
