# Wii Remote Control

Native Linux OS support for Wii Remotes -- including third-party/clone
remotes -- with no emulator involved. Connected remotes show up as normal
`/dev/input/jsN` joysticks, with a GUI for live status, per-remote button
remapping (to gamepad buttons or keyboard keys), and IR pointer (mouse)
support.

## Why this exists

These remotes, genuine and clones alike, only answer Bluetooth inquiry
using the **Limited Inquiry Access Code (LIAC)** -- the same code a real
Wii console uses. Every normal OS Bluetooth path (`bluetoothctl`,
GNOME/KDE Bluetooth, default `hcitool inq`) scans with the **General
Inquiry Access Code (GIAC)** instead, so these remotes are structurally
invisible to standard OS pairing on any adapter, any machine. They also
don't implement SDP, so BlueZ's normal HID pairing/bonding flow fails even
if you already know the address.

Dolphin works around both of these internally: it does its own LIAC
inquiry and connects raw L2CAP sockets straight to the fixed HID PSMs
(0x11 control / 0x13 interrupt), bypassing BlueZ's pairing/SDP machinery
entirely. `wiimote_bridge.py` does the same thing standalone, and
publishes what it reads as real Linux input devices via `uinput`, so any
application that reads a standard joystick/keyboard/mouse works --
Dolphin is not involved anywhere in this pipeline.

## Components

- **`wiimote_bridge.py`** -- the daemon. Discovers remotes, connects to
  them, parses their Bluetooth HID reports, and exposes each one to the
  OS as `uinput` devices (gamepad, keyboard, and optionally an absolute
  IR-pointer device). Runs as a normal user, not root -- see
  the .deb, or the AppImage's one-time setup button, for what makes that
  possible. Supports multiple remotes concurrently, spread across every
  Bluetooth adapter present on the machine (useful if one adapter hits a
  hardware connection-count limit -- some cheap dongles cap out around 2
  simultaneous connections).
- **`wiimote_gui.py`** -- a Tkinter GUI. Shows up to 4 connected remotes
  side by side, each with live button state, independent remapping, IR
  pointer controls, and a Settings tab listing detected Bluetooth
  adapters. It runs the daemon **inside the same process**, so the service
  only exists while the window is open: closing the app disconnects every
  remote and removes the virtual devices, and nothing is left running in
  the background. Only one copy can run at a time (an `flock` on
  `$XDG_RUNTIME_DIR/wii-control.lock`, which the kernel releases however
  the process dies, so a crash never leaves a stale lock); a second launch
  just says it's already running. GUI and daemon still talk over a local
  Unix socket (`/tmp/wiimote_bridge.sock`).

### Why the daemon doesn't need root

Only `/dev/uinput` and raw HCI operations (the LIAC inquiry scan) need
elevated privilege -- Bluetooth L2CAP data sockets (the actual connection
to each remote) need none. Rather than running the whole daemon as root
for the sake of those two things, the setup grants them
narrowly, once:
- a udev rule with `uaccess` for `/dev/uinput`, giving the logged-in desktop
  user an ACL (no group membership or logout needed)
- `cap_net_raw`/`cap_net_admin` via `setcap` on the app's bundled
  `wiimote-hci` helper, which the app shells out to for raw Bluetooth

An earlier version of this launched the daemon via `pkexec` on every run
instead. That turned out to fail silently on at least one machine --
`pkexec` just returned with no prompt and no error when no polkit
authentication agent was running in the session -- which is a bad
foundation for something meant to "just work," so it's gone.

Config (button mappings, pointer settings) is stored per remote (by
Bluetooth address) under `~/.config/wii_control/`.

## Running it

### .deb (recommended, Debian/Ubuntu/Mint)

```
packaging/build-deb.sh
sudo apt install ./packaging/wii-control_<version>_all.deb
wii-control
```

The install itself (which already runs as root) applies everything the
daemon needs: a udev rule giving the logged-in desktop user access to
`/dev/uinput` (via `uaccess`, so no group membership or logout), and
`setcap` on the app's bundled helper. An apt hook re-applies the `setcap`
after `bluez` upgrades, which would otherwise silently drop it. There is
no separate permission script to run and no password prompt from the app.

### AppImage (no install, self-contained)

```
packaging/build-appimage.sh
packaging/Wii-Remote-Control-<version>-x86_64.AppImage
```

The GUI always opens. The first time on a computer it shows a "One-time
setup needed" panel with a **Grant permission...** button. Clicking it
brings up your desktop's own password dialog (via `pkexec`/polkit -- drawn
by the system, not by this app, which never sees your password) and runs
`setup-permissions.sh` as root once. After that the app starts with no
prompts. If the system has no polkit authentication agent, the button
shows why instead of failing silently.

It bundles its own Python interpreter with `evdev` and Tkinter, so it
doesn't depend on the host's Python (the first build downloads that
portable interpreter and caches it in `packaging/AppDir/usr/python/`,
~95MB uncompressed, ~27MB in the built AppImage).

### Headless / systemd (optional -- runs with no window, the opposite of the above)

```
sudo python3 wiimote_bridge.py          # run directly, or:
sudo cp packaging/wiimote-bridge.service /etc/systemd/system/
sudo systemctl enable --now wiimote-bridge.service
```

Run it as your own user (not root). Don't run this alongside the GUI: they
share the single-instance lock, so whichever starts second refuses to start.

## Connecting a remote

Hold the **SYNC** button under the battery cover (more reliable than
holding 1+2) for a few seconds. The daemon polls for new remotes every
few seconds and connects automatically.

## IR pointer

Enable "Ptr" on a connected remote's column in the GUI, then point it at
an IR source a few feet away (a real Wii Sensor Bar, or even two candles
-- flames are strong IR emitters). The B button doubles as a left click
whenever the pointer is active, independent of B's own button mapping.

Pointer defaults to off per remote to save Bluetooth bandwidth (relevant
if you're running several remotes off one adapter); enable it per remote
as needed.

## Requirements

For the AppImage: nothing from the Bluetooth tools (`hcitool`/`hciconfig`/
`btmon` are not used). It does use, for the one-time permission button
only: `pkexec` (polkit), `setcap` (libcap) and `udevadm`, `bash`, `stat`.
Bluetooth and `uinput` support must be in the kernel, as on any distro.
- A C compiler (`gcc`) is needed once, on whichever machine *builds* the
  AppImage, to compile `evdev`'s native extension into the bundled
  interpreter -- not needed on machines that just run the built AppImage.

For the manual/systemd path (not the AppImage, which bundles these):
- `python3-evdev`
- Python's Tkinter (`python3-tk`)

## Known issue

IR pointer position tracking is confirmed working correctly at the X11
protocol level (verified with `xdotool getmouselocation` -- the reported
cursor position updates in real time), but in at least one VM test
environment the drawn cursor icon didn't visually follow. Suspected to be
a virtual-display cursor-plane quirk specific to that VM rather than a
bug in the input device itself; needs confirming on real hardware.

## How remotes are found

The remote only answers a *limited* Bluetooth inquiry. On the host that
exposed this (kernel 7.0), neither `hcitool inq --iac=liac` nor the
kernel's own "limited discovery" (`btmgmt find -l`) actually sends one --
both end up as a general scan, so the remote is never heard even though
permissions, adapters and errors all look healthy. Sending the identical
Inquiry as a raw HCI command works immediately (a capture on that host
showed the remote answering within 21 ms and continuously, a general-only
device *not* answering, and an unused address hearing nothing).

The app therefore does its own raw HCI, through a small bundled program,
`packaging/helper/wiimote-hci.c` (static, needs only libc): it lists
adapters, brings them up, and sends the Inquiry itself, decoding the
Inquiry Result events directly. That also means **no dependency on
`hcitool`, `hciconfig` or `btmon`** -- Fedora-based distros no longer ship
them, and immutable ones (Bazzite, Silverblue, SteamOS) couldn't have
capabilities set on system binaries anyway. The one-time grant applies the
scan capability to the helper's own copy in `~/.cache/wii-control/bin/`
(writable everywhere). A kernel drops file capabilities whenever a file is
written to, so code running as you can't alter the granted copy and keep
the privilege; a new app version that changes the helper simply asks for
the grant again.

A raw socket sees the adapter's replies to *every* program's commands, so
the helper only trusts the first status reply after it sends (found on real
hardware: a second scanner's "busy" reply was being mistaken for its own).
If another program is scanning at the same time -- often the desktop's own
Bluetooth settings window -- the controller refuses a second inquiry, and
the app says so.

Without the helper (running from a source checkout) the app falls back to
the system `hcitool`/`btmon` methods, described below.

## Troubleshooting: "answers every scan as a general scan"

(Only relevant to the fallback method.)

The app checks each Bluetooth adapter by scanning with an inquiry access
code that no device answers. If any device turns up, that adapter (or its
driver stack) is running an ordinary general inquiry no matter what scan
type it's asked for. Wii remotes only answer the limited inquiry, so such
an adapter can never hear one -- everything else can look perfectly
healthy. The app skips it and says so; use a different adapter (an
external USB dongle is fine). Observed on one host with a MediaTek
adapter that worked correctly on another machine, so it isn't necessarily
the chip. You can run the same check by hand:

```
hcitool -i hci0 inq --iac=0x9e8b01 --flush --length=4
```

Anything listed means the requested scan type is being ignored.
