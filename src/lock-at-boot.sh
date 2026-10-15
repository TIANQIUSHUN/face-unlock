#!/bin/sh
# 锁屏。走 GNOME 自己的 D-Bus 接口，不需要 root，也不依赖 XAUTHORITY。
# 失败时退回 loginctl，两者都不行就打印原因（别静默失败——静默失败等于门禁失效）。
set -u

if command -v dbus-send >/dev/null 2>&1; then
    if dbus-send --session --print-reply \
         --dest=org.gnome.ScreenSaver /org/gnome/ScreenSaver \
         org.gnome.ScreenSaver.Lock >/dev/null 2>&1; then
        echo "lock-at-boot: 已通过 org.gnome.ScreenSaver.Lock 锁屏"
        exit 0
    fi
fi

sid=$(loginctl show-user "$(id -un)" --property=Display --value 2>/dev/null)
if [ -n "${sid:-}" ] && loginctl lock-session "$sid" 2>/dev/null; then
    echo "lock-at-boot: dbus 不可用，已退回 loginctl lock-session $sid"
    exit 0
fi

echo "lock-at-boot: 锁屏失败 —— 门禁没有生效，桌面会直接暴露。请手动 face-ctl lock 并查日志" >&2
exit 1
