#!/bin/bash
# One-time setup so wiimote_bridge.py can run as a normal user. Run as
# root (the GUI's "Grant permission" button does this through the system's
# own password dialog via pkexec; `sudo` works too).
#
#   - /dev/uinput is root-only by default. A udev rule with `uaccess`
#     grants the logged-in desktop user an ACL on it -- no group
#     membership and no logout needed. GROUP=input is the fallback.
#   - Raw HCI operations (the remote scan) need CAP_NET_RAW / CAP_NET_ADMIN.
#     Rather than running the app as root, grant those to the app's small
#     bundled helper (wiimote-hci), which it shells out to.
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

# The app's own helper (path passed by the app): the only thing it needs.
# Must be a plain file owned by the invoking user, not a symlink -- and note
# a kernel drops file capabilities whenever the file is written to, so the
# user can't alter the granted copy and keep the privilege.
HELPER="${1:-}"
if [ -n "$HELPER" ]; then
    OWNER="${PKEXEC_UID:-${SUDO_UID:-}}"
    if [ -L "$HELPER" ] || [ ! -f "$HELPER" ] || [ -z "$OWNER" ] || [ "$(stat -c %u "$HELPER")" != "$OWNER" ]; then
        echo "Refusing to grant capabilities to $HELPER (not a plain file owned by you)." >&2
        exit 1
    fi
    setcap cap_net_raw,cap_net_admin+eip "$HELPER"
fi

# Older installs / source checkouts use the system tools instead. Best
# effort only: they may be absent (newer Fedora-based distros) or on a
# read-only /usr (immutable distros), and that must not fail the setup.
for b in hcitool hciconfig btmon; do
    p="$(command -v "$b" 2>/dev/null || true)"
    if [ -n "$p" ]; then
        setcap cap_net_raw,cap_net_admin+eip "$p" 2>/dev/null || true
    fi
done

echo "Done."
