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
  adapters. Talks to the daemon over a local Unix socket
  (`/tmp/wiimote_bridge.sock`) and launches it on demand if it isn't
  already running.

### Why the daemon doesn't need root

Only `/dev/uinput` and raw HCI operations (the LIAC inquiry scan) need
elevated privilege -- Bluetooth L2CAP data sockets (the actual connection
to each remote) need none. Rather than running the whole daemon as root
for the sake of those two things, the setup grants them
narrowly, once:
- a udev rule with `uaccess` for `/dev/uinput`, giving the logged-in desktop
  user an ACL (no group membership or logout needed)
- `cap_net_raw`/`cap_net_admin` via `setcap` on the `hcitool`/`hciconfig`
  binaries themselves, which the daemon just shells out to

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
sudo apt install ./packaging/wii-control_0.1.0_all.deb
wii-control
```

The install itself (which already runs as root) applies everything the
daemon needs: a udev rule giving the logged-in desktop user access to
`/dev/uinput` (via `uaccess`, so no group membership or logout), and
`setcap` on `hcitool`/`hciconfig`. An apt hook re-applies the `setcap`
after `bluez` upgrades, which would otherwise silently drop it. There is
no separate permission script to run and no password prompt from the app.

### AppImage (no install, self-contained)

```
packaging/build-appimage.sh
packaging/Wii-Remote-Control-x86_64.AppImage
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

### Manual / systemd (alternative, e.g. headless setups)

```
sudo python3 wiimote_bridge.py          # run directly, or:
sudo cp packaging/wiimote-bridge.service /etc/systemd/system/
sudo systemctl enable --now wiimote-bridge.service
```

Then run `python3 wiimote_gui.py` as your normal user for the GUI.

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

For the AppImage:
- `bluez` (`hcitool`, `hciconfig`, `bluetoothd`)
- `pkexec` (polkit) and `setcap`, for the one-time permission button
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
