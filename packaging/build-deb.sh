#!/bin/bash
# Builds packaging/wiibridge_<version>_<arch>.deb (arch-specific: it holds a compiled helper). Unlike the AppImage, a
# .deb installs as root, so its postinst can apply the udev rule and
# setcap directly -- the user never runs a permission script by hand.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../wiimote_bridge.py)
ARCH=$(dpkg --print-architecture)
PKG=wiibridge_${VERSION}_${ARCH}
STAGE=build-deb/$PKG

rm -rf build-deb
mkdir -p "$STAGE"/{DEBIAN,usr/bin,usr/lib/wiibridge,usr/share/applications,usr/share/icons/hicolor/256x256/apps,lib/udev/rules.d,etc/apt/apt.conf.d}

cp ../wiimote_bridge.py ../wiimote_gui.py "$STAGE/usr/lib/wiibridge/"
gcc -O2 -Wall -static -o "$STAGE/usr/lib/wiibridge/wiimote-hci" helper/wiimote-hci.c 2>/dev/null \
    || gcc -O2 -Wall -o "$STAGE/usr/lib/wiibridge/wiimote-hci" helper/wiimote-hci.c
"$STAGE/usr/lib/wiibridge/wiimote-hci" selftest >/dev/null || { echo "helper self-test failed" >&2; exit 1; }
cp AppDir/wiibridge.png "$STAGE/usr/share/icons/hicolor/256x256/apps/wiibridge.png"

cat > "$STAGE/usr/bin/wiibridge" <<'EOF'
#!/bin/sh
exec python3 /usr/lib/wiibridge/wiimote_gui.py "$@"
EOF

cat > "$STAGE/usr/share/applications/wiibridge.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=WiiBridge
Comment=Connect and remap Wii Remotes (including clones) without an emulator
Exec=wiibridge
Icon=wiibridge
Categories=Utility;Game;
Terminal=false
EOF

# uaccess grants the logged-in desktop user an ACL on /dev/uinput, so no
# group membership (and no logout) is needed; GROUP=input is the fallback.
cat > "$STAGE/lib/udev/rules.d/60-wiibridge-uinput.rules" <<'EOF'
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF

# Re-applies the hcitool/hciconfig capabilities: upgrading the bluez
# package replaces those binaries and silently drops them.
cat > "$STAGE/usr/lib/wiibridge/fix-caps.sh" <<'EOF'
#!/bin/sh
setcap cap_net_raw,cap_net_admin+eip /usr/lib/wiibridge/wiimote-hci
# Older tools, best effort only (absent on newer Fedora-based distros).
for b in hcitool hciconfig btmon; do
    p=$(command -v $b) && setcap cap_net_raw,cap_net_admin+eip "$p" || true
done
exit 0
EOF
cat > "$STAGE/etc/apt/apt.conf.d/99wiibridge" <<'EOF'
DPkg::Post-Invoke { "/usr/lib/wiibridge/fix-caps.sh || true"; };
EOF

cat > "$STAGE/DEBIAN/control" <<EOF
Package: wiibridge
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3, python3-evdev, python3-tk, bluez, libcap2-bin
Maintainer: wiibridge <noreply@example.invalid>
Description: Wii Remote support without an emulator
 Native Linux support for Wii Remotes, including clones that normal
 Bluetooth pairing can't see. Exposes them as joysticks, with a GUI for
 button remapping and IR pointer support.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
/usr/lib/wiibridge/fix-caps.sh
udevadm control --reload-rules || true
udevadm trigger --name-match=uinput || true
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
if [ "$1" = remove ] || [ "$1" = purge ]; then
    for b in hcitool hciconfig btmon; do
        p=$(command -v $b) && setcap -r "$p" 2>/dev/null || true
    done
    udevadm control --reload-rules 2>/dev/null || true
fi
exit 0
EOF

chmod 755 "$STAGE/usr/bin/wiibridge" "$STAGE/usr/lib/wiibridge/fix-caps.sh" \
          "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

fakeroot dpkg-deb --build "$STAGE" "$PKG.deb"
echo "Built packaging/$PKG.deb"
