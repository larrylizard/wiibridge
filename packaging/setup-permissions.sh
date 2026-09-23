#!/bin/bash
# One-time setup so wiimote_bridge.py can run as a normal user. Run as
# root (the GUI's "Grant permission" button does this through the system's
# own password dialog via pkexec; `sudo` works too).
#
#   - /dev/uinput is root-only by default. A udev rule with `uaccess`
#     grants the logged-in desktop user an ACL on it -- no group
#     membership and no logout needed. GROUP=input is the fallback.
#   - Raw HCI operations (the LIAC inquiry scan) need CAP_NET_RAW /
#     CAP_NET_ADMIN. Rather than running the daemon as root, grant those
#     to hcitool/hciconfig, which the daemon shells out to.
#   - btmon gets the same, to read the raw scan's results.
#   - Bluetooth L2CAP data sockets need no privilege at all on Linux.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Must run as root (pkexec or sudo)." >&2
    exit 1
fi

cat > /etc/udev/rules.d/60-wii-control-uinput.rules <<'EOF'
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF
udevadm control --reload-rules
udevadm trigger --name-match=uinput

for b in hcitool hciconfig; do
    setcap cap_net_raw,cap_net_admin+eip "$(command -v "$b")"
done
# btmon reads the results of the raw remote scan (see RawInquiry in
# wiimote_bridge.py). Optional: without it the app falls back to an older
# scan method that some kernels can't do correctly.
if command -v btmon >/dev/null 2>&1; then
    setcap cap_net_raw,cap_net_admin+eip "$(command -v btmon)"
fi

echo "Done."
