#!/bin/sh
# Boot the harness emulator, make Caddy's internal root a system CA, then serve adb on every interface at 5037 for the
# apps-runner (interop/apps/android_device.py). Readiness: /tmp/ready exists once the CA is in place.
set -eu
ROOT=/caddy-data/harness-root.crt
i=0
while [ ! -s "$ROOT" ]; do
    i=$((i + 1)); [ "$i" -gt 60 ] && { echo "boot: no CA at $ROOT after 60s" >&2; exit 1; }
    sleep 1
done
[ -e /dev/kvm ] || { echo "boot: no /dev/kvm: run the emulator service with the kvm device" >&2; exit 1; }

# A fresh data partition every boot: no state carries from one run to the next. `-gpu guest` renders inside the guest,
# so the emulator's host-side renderer (gfxstream) never runs: under swiftshader_indirect the emulator segfaulted the
# moment Summit 1.88 drew its first frame (with or without Vulkan), and under swangle_indirect it hit a deliberate trap
# (ud2) in gfxstream's GLES decoder, gles2_decoder_context_t::decode, the moment Thunder (Flutter) drew. Both were the
# emulator dying, not the app, and guest rendering runs all three apps.
emulator -avd harness -no-window -no-audio -no-boot-anim -no-snapshot -wipe-data -gpu guest \
    -no-metrics -netdelay none -netspeed full &
emulator=$!
adb start-server
adb wait-for-device
i=0
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; do
    i=$((i + 1)); [ "$i" -gt 300 ] && { echo "boot: the emulator never finished booting" >&2; exit 1; }
    sleep 1
done

# The system CA store is read-only: /system/etc/security/cacerts up to Android 13, the conscrypt APEX's cacerts from 14
# on. A tmpfs over /system/etc/security/cacerts, refilled with the stock roots plus the harness root, needs no remount and
# no reboot. From 14 on it is then bind-mounted over the APEX directory inside zygote's mount namespace (and those of the
# apps already running), so every app started afterwards reads it. Named by OpenSSL's old subject hash, as Android names
# its roots.
hash=$(openssl x509 -inform PEM -subject_hash_old -in "$ROOT" -noout)
# adbd restarts as root; just after boot it can drop the request ("unable to connect for root: closed")
i=0
until [ "$(adb shell id -u 2>/dev/null | tr -d '\r')" = 0 ]; do
    i=$((i + 1)); [ "$i" -gt 30 ] && { echo "boot: adbd never ran as root" >&2; exit 1; }
    adb root > /dev/null 2>&1 || true
    sleep 2
    adb wait-for-device
done

# Certificate Transparency: from Android 17 an app targeting it refuses a certificate without SCTs that chains to a
# system CA, and the harness CA's certificates can have none (Summit: "NOT_ENOUGH_SCTS"). A real instance's publicly
# logged certificate has them, so this is the harness's own artefact. Its switches are read-only flags; the documented
# off state is a device with no CT log list ("if no log list is available on the device ... CT enforcement will be
# disabled", developer.android.com/privacy-and-security/certificate-transparency-policy). So the list the boot
# downloaded goes, and an immutable empty file takes the directory's place, which the updater cannot replace.
adb shell "set -e
    if [ -d /data/misc/keychain ]; then
        rm -rf /data/misc/keychain/ct
        touch /data/misc/keychain/ct
        chattr +i /data/misc/keychain/ct
    fi"
adb push "$ROOT" "/data/local/tmp/$hash.0" > /dev/null
adb shell "set -e
    stock=/system/etc/security/cacerts
    [ -d /apex/com.android.conscrypt/cacerts ] && stock=/apex/com.android.conscrypt/cacerts
    mkdir -p -m 700 /data/local/tmp/cacerts
    cp \$stock/* /data/local/tmp/cacerts/
    mount -t tmpfs tmpfs /system/etc/security/cacerts
    cp /data/local/tmp/cacerts/* /system/etc/security/cacerts/
    cp /data/local/tmp/$hash.0 /system/etc/security/cacerts/
    chown root:root /system/etc/security/cacerts/*
    chmod 644 /system/etc/security/cacerts/*
    chcon u:object_r:system_file:s0 /system/etc/security/cacerts/*
    if [ -d /apex/com.android.conscrypt/cacerts ]; then
        for zygote in \$(pidof zygote) \$(pidof zygote64); do
            nsenter --mount=/proc/\$zygote/ns/mnt -- mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts
            for pid in \$(ps -o PID= -P \$zygote); do
                nsenter --mount=/proc/\$pid/ns/mnt -- mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts || true
            done
        done
    fi
    settings put global window_animation_scale 0
    settings put global transition_animation_scale 0
    settings put global animator_duration_scale 0"
adb shell "ls /system/etc/security/cacerts/$hash.0" > /dev/null
# from 14 on, what an app reads: the APEX directory as zygote's children see it
if adb shell "[ -d /apex/com.android.conscrypt/cacerts ]"; then
    zygote=$(adb shell "pidof zygote64 || pidof zygote" | tr -d '\r' | cut -d' ' -f1)
    adb shell "nsenter --mount=/proc/$zygote/ns/mnt -- ls /apex/com.android.conscrypt/cacerts/$hash.0" > /dev/null \
        || { echo "boot: the harness CA is not in zygote's view of the conscrypt APEX" >&2; exit 1; }
fi
echo "boot: $(adb shell dumpsys webviewupdate | grep -m1 'Current WebView package' | tr -d '\r')"
echo "boot: ready (harness CA $hash.0)"
touch /tmp/ready

# hand adb to the network: the runner connects to android-emulator:5037
adb kill-server
adb -a -P 5037 nodaemon server &
wait $emulator
