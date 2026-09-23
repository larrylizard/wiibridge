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
  IR-pointer device). Requires root (raw Bluetooth sockets +
  `/dev/uinput`). Supports multiple remotes concurrently, spread across
  every Bluetooth adapter present on the machine (useful if one adapter
  hits a hardware connection-count limit -- some cheap dongles cap out
  around 2 simultaneous connections).
- **`wiimote_gui.py`** -- a Tkinter GUI. Shows up to 4 connected remotes
  side by side, each with live button state, independent remapping, IR
  pointer controls, and a Settings tab listing detected Bluetooth
  adapters. Talks to the daemon over a local Unix socket
  (`/tmp/wiimote_bridge.sock`) and launches it on demand via `pkexec` if
  it isn't already running.

Config (button mappings, pointer settings) is stored per remote (by
Bluetooth address) under `/etc/wii_control/`, since the daemon always runs
as root regardless of how it was launched.

## Running it

### AppImage (recommended)

```
packaging/build-appimage.sh
packaging/Wii-Remote-Control-x86_64.AppImage
```

Fully self-contained: it bundles its own Python interpreter with `evdev`
and Tkinter already installed, so it doesn't depend on what's on the
host's system Python (the first build downloads that portable interpreter
and caches it in `packaging/AppDir/usr/python/`, ~95MB uncompressed,
~27MB in the built AppImage). The GUI launches; if the daemon isn't
already running it prompts for your password via `pkexec` to start it,
using that same bundled interpreter. No separate install step needed
beyond `bluez` and `pkexec` being present on the host (see Requirements).

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
- `pkexec` (part of polkit), for the on-demand daemon launch
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
