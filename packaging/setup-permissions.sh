#!/bin/bash
# One-time setup so wiimote_bridge.py can run as a normal user, with no
# pkexec/sudo prompt on every launch. Run this once with sudo; log out
# and back in afterward for the group membership change to take effect.
#
# What it does, and why root isn't needed for any of it at runtime:
#   - /dev/uinput is root-only (0600) by default. A udev rule + group
#     membership grants the "input" group read/write access to it --
#     the same mechanism many controller-remapping tools use.
#   - Raw HCI operations (the LIAC inquiry scan) need CAP_NET_RAW /
#     CAP_NET_ADMIN. Rather than running the whole daemon as root for
#     this, grant those two capabilities directly to the hcitool/
#     hciconfig binaries via setcap -- the daemon just shells out to
#     them, so it never needs elevated privilege itself.
#   - Bluetooth L2CAP data sockets (the actual connection to each
#     remote) need no special privilege at all on Linux.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-$USER}"

echo "Granting $TARGET_USER access to /dev/uinput..."
usermod -aG input "$TARGET_USER"
cat > /etc/udev/rules.d/99-wiimote-uinput.rules <<'EOF'
KERNEL=="uinput", MODE="0660", GROUP="input"
EOF
udevadm control --reload-rules
udevadm trigger /dev/uinput 2>/dev/null || true

echo "Granting hcitool/hciconfig raw Bluetooth HCI capability..."
setcap cap_net_raw,cap_net_admin+eip "$(command -v hcitool)"
setcap cap_net_raw,cap_net_admin+eip "$(command -v hciconfig)"

echo
echo "Done. Log out and back in (so the 'input' group membership takes"
echo "effect), then run the AppImage -- it will no longer ask for a"
echo "password."
