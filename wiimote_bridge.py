#!/usr/bin/env python3
"""
Bridges Wii Remotes (including third-party/clone remotes) to native Linux
input devices, without Dolphin or any emulator involved.

Why this exists: these remotes (genuine and clones alike) only respond to
Bluetooth inquiry using the Limited Inquiry Access Code (LIAC), the same
code a real Wii console uses. Every normal OS Bluetooth tool (bluetoothctl,
GNOME/KDE Bluetooth, default `hcitool inq`) scans with the General Inquiry
Access Code (GIAC) instead, so these remotes are permanently invisible to
them -- on any adapter, any machine. They also don't implement SDP, so
BlueZ's normal HID pairing/bonding flow fails even if you already know the
address. Dolphin works around both issues internally; this script does the
same thing standalone: LIAC inquiry to find the remote, then raw L2CAP
sockets straight to the fixed HID PSMs (0x11 control / 0x13 interrupt),
skipping BlueZ pairing/SDP entirely. Received reports are translated into
evdev/uinput events, so the OS sees a normal input device.

Each logical Wiimote button can be mapped (see mapping.json, edited live
via wiimote_gui.py) to either a gamepad button (BTN_*) or a keyboard key
(KEY_*). A local Unix socket (SOCK_PATH) broadcasts live button state and
the current mapping for the GUI, and accepts mapping changes from it.

Requires root (raw Bluetooth sockets + /dev/uinput).
"""

import json
import os
import re
import socket
import subprocess
import threading
import time

from evdev import UInput, AbsInfo, ecodes as e

CTRL_PSM = 0x11
INTR_PSM = 0x13

SCAN_INTERVAL = 4.0        # seconds between LIAC inquiry passes
SCAN_LENGTH = 4            # hcitool --length units of 1.28s
RECONNECT_BACKOFF = 3.0
STALE_TIMEOUT = 5.0        # seconds without any report before treating a remote as dead
WATCHDOG_INTERVAL = 2.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Config lives under /etc, not next to the script: the daemon always runs
# as root regardless of who launched it (systemd, sudo, or an AppImage's
# pkexec), and an AppImage's own directory is a read-only mount at runtime
# so it can't hold writable state anyway.
CONFIG_DIR = "/etc/wii_control"
MAPPING_PATH = os.path.join(CONFIG_DIR, "mapping.json")
POINTER_PATH = os.path.join(CONFIG_DIR, "pointer.json")
SOCK_PATH = "/tmp/wiimote_bridge.sock"

# IR camera init sequence and sensitivity values per WiiBrew's Wiimote page
# (verified against the raw wikitext, not a lossy summary -- getting the
# sensitivity bytes wrong makes the camera silently return garbage).
IR_SENSITIVITY_BLOCK1 = bytes([0x02, 0x00, 0x00, 0x71, 0x01, 0x00, 0xAA, 0x00, 0x64])  # "Wii level 3"
IR_SENSITIVITY_BLOCK2 = bytes([0x63, 0x03])
IR_MODE_EXTENDED = 0x03  # 12 bytes: X/Y/size for up to 4 tracked points

MAC_RE = re.compile(
    r"^\s*([0-9A-Fa-f:]{17})\s+clock offset:\s*0x[0-9A-Fa-f]+\s+class:\s*0x([0-9A-Fa-f]+)",
    re.MULTILINE,
)

# Core Buttons bit layout: (byte index within (b0, b1), bitmask)
INPUT_NAMES = [
    "A", "B", "ONE", "TWO", "MINUS", "PLUS", "HOME",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
]
INPUT_BITS = {
    "DPAD_LEFT": (0, 0x01), "DPAD_RIGHT": (0, 0x02),
    "DPAD_DOWN": (0, 0x04), "DPAD_UP": (0, 0x08), "PLUS": (0, 0x10),
    "TWO": (1, 0x01), "ONE": (1, 0x02), "B": (1, 0x04),
    "A": (1, 0x08), "MINUS": (1, 0x10), "HOME": (1, 0x80),
}

DEFAULT_MAPPING = {
    "A": {"kind": "button", "code": "BTN_A"},
    "B": {"kind": "button", "code": "BTN_B"},
    "ONE": {"kind": "button", "code": "BTN_X"},
    "TWO": {"kind": "button", "code": "BTN_Y"},
    "MINUS": {"kind": "button", "code": "BTN_SELECT"},
    "PLUS": {"kind": "button", "code": "BTN_START"},
    "HOME": {"kind": "button", "code": "BTN_MODE"},
    "DPAD_UP": {"kind": "button", "code": "BTN_DPAD_UP"},
    "DPAD_DOWN": {"kind": "button", "code": "BTN_DPAD_DOWN"},
    "DPAD_LEFT": {"kind": "button", "code": "BTN_DPAD_LEFT"},
    "DPAD_RIGHT": {"kind": "button", "code": "BTN_DPAD_RIGHT"},
}

REGISTRY = {}  # addr -> Wiimote, for live mapping updates from the GUI
REGISTRY_LOCK = threading.Lock()

PLAYER_SLOTS = {}  # addr -> player slot 0-3, so each remote lights a distinct LED
PLAYER_SLOTS_LOCK = threading.Lock()


def assign_player_slot(addr):
    with PLAYER_SLOTS_LOCK:
        if addr in PLAYER_SLOTS:
            return PLAYER_SLOTS[addr]
        used = set(PLAYER_SLOTS.values())
        for i in range(4):
            if i not in used:
                PLAYER_SLOTS[addr] = i
                return i
        return 0


def release_player_slot(addr):
    with PLAYER_SLOTS_LOCK:
        PLAYER_SLOTS.pop(addr, None)


def devices_message():
    """The GUI must mirror the daemon's own player-slot assignment (used
    for the physical LED) rather than compute its own column order --
    otherwise the two can disagree about which column is which remote."""
    with REGISTRY_LOCK:
        addrs = list(REGISTRY.keys())
    with PLAYER_SLOTS_LOCK:
        slots = {a: PLAYER_SLOTS.get(a, 0) for a in addrs}
    return {"type": "devices", "addrs": addrs, "slots": slots}


# A uinput device that only declares a couple of arbitrary KEY_* codes is
# often not recognized by udev/libinput as an actual keyboard, so its
# events never reach the focused window even though the kernel emits them
# correctly. Declaring the full standard key range up front avoids that.
FULL_KEYBOARD_CODES = sorted(
    code for name, code in e.ecodes.items()
    if isinstance(code, int) and name.startswith("KEY_") and code < 0xF0
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


ADAPTER_RE = re.compile(r"^(hci\d+):.*?\n\s*BD Address:\s*([0-9A-Fa-f:]{17})", re.MULTILINE | re.DOTALL)


def list_adapters():
    """Local Bluetooth adapters as [(hci_name, bd_addr), ...]. Cheap
    dongles (e.g. CSR8510) often hard-cap around 2 simultaneous ACL
    connections regardless of data rate -- inquiry itself starts failing
    once that ceiling is hit. Spreading remotes across more than one
    adapter works around that. (This kernel doesn't expose an `address`
    sysfs attribute per hciN, so shell out to hciconfig instead.)"""
    try:
        out = subprocess.run(["hciconfig", "-a"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    adapters = [(name, addr.upper()) for name, addr in ADAPTER_RE.findall(out)]
    for name, _addr in adapters:
        # An adapter can come up administratively DOWN (e.g. after a
        # passthrough/replug); bring it up so inquiry can actually work.
        # Idempotent and cheap if it's already up.
        try:
            subprocess.run(["hciconfig", name, "up"], capture_output=True, timeout=3)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return adapters


def discover_candidates(hci_name):
    """One LIAC inquiry pass on a given adapter. Returns MAC addresses in
    the Bluetooth 'Peripheral' major device class -- in practice nothing
    but a synced Wii Remote answers a LIAC inquiry at all, so this filter
    mainly guards against odd false positives rather than being load-bearing."""
    try:
        out = subprocess.run(
            ["hcitool", "-i", hci_name, "inq", "--iac=liac", "--flush", f"--length={SCAN_LENGTH}"],
            capture_output=True, text=True, timeout=SCAN_LENGTH * 1.28 + 5,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as ex:
        log(f"{hci_name}: discovery failed: {ex}")
        return set()

    found = set()
    for addr, cod_hex in MAC_RE.findall(out):
        cod = int(cod_hex, 16)
        major_device_class = (cod >> 8) & 0x1F
        if major_device_class == 0x05:  # Peripheral
            found.add(addr.upper())
    return found


class Mapping:
    """Per-device (by Bluetooth address) button mapping, so up to several
    connected remotes can be remapped independently. Persisted as
    {"default": {...}, "devices": {"AA:BB:...": {...}, ...}}; a device not
    yet seen inherits a copy of "default" the first time it's looked up."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.default = dict(DEFAULT_MAPPING)
        self.devices = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    data = json.load(f)
                self.default.update(data.get("default", {}))
                self.devices = data.get("devices", {})
            except (OSError, json.JSONDecodeError) as ex:
                log(f"mapping.json unreadable ({ex}), using defaults")

    def save(self):
        with open(self.path, "w") as f:
            json.dump({"default": self.default, "devices": self.devices}, f, indent=2)

    def get_for(self, addr):
        with self.lock:
            if addr not in self.devices:
                self.devices[addr] = dict(self.default)
                self.save()
            merged = dict(self.default)
            merged.update(self.devices[addr])
            return merged

    def set(self, addr, input_name, kind, code):
        with self.lock:
            dev = self.devices.setdefault(addr, dict(self.default))
            dev[input_name] = {"kind": kind, "code": code}
            self.save()


class PointerConfig:
    """Same per-device pattern as Mapping, for the IR-pointer feature."""

    DEFAULTS = {"enabled": False, "invert_x": True, "invert_y": False}

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.default = dict(self.DEFAULTS)
        self.devices = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    data = json.load(f)
                self.default.update(data.get("default", {}))
                self.devices = data.get("devices", {})
            except (OSError, json.JSONDecodeError) as ex:
                log(f"pointer.json unreadable ({ex}), using defaults")

    def save(self):
        with open(self.path, "w") as f:
            json.dump({"default": self.default, "devices": self.devices}, f, indent=2)

    def get_for(self, addr):
        with self.lock:
            if addr not in self.devices:
                self.devices[addr] = dict(self.default)
                self.save()
            merged = dict(self.default)
            merged.update(self.devices[addr])
            return merged

    def update(self, addr, **kwargs):
        with self.lock:
            dev = self.devices.setdefault(addr, dict(self.default))
            dev.update(kwargs)
            self.save()


class IPCServer(threading.Thread):
    """Unix socket, newline-delimited JSON, for wiimote_gui.py."""

    def __init__(self, sock_path, mapping, pointer_cfg):
        super().__init__(daemon=True)
        self.mapping = mapping
        self.pointer_cfg = pointer_cfg
        self.clients = []
        self.clients_lock = threading.Lock()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except PermissionError:
            # Leftover from a previous run under a different user (e.g. an
            # old root/pkexec-launched instance) -- /tmp's sticky bit means
            # only that file's owner can remove it.
            raise SystemExit(
                f"{sock_path} exists but is owned by another user and can't "
                f"be replaced. Remove it manually (sudo rm -f {sock_path}) and try again."
            )
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(sock_path)
        os.chmod(sock_path, 0o666)
        self.srv.listen(8)

    def run(self):
        while True:
            conn, _ = self.srv.accept()
            with self.clients_lock:
                self.clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()
            self._send_snapshot(conn)

    def _send_snapshot(self, conn):
        with REGISTRY_LOCK:
            addrs = list(REGISTRY.keys())
        for addr in addrs:
            self._send(conn, {"type": "mapping", "addr": addr, "mapping": self.mapping.get_for(addr)})
            self._send(conn, {"type": "pointer", "addr": addr, "config": self.pointer_cfg.get_for(addr)})
        self._send(conn, devices_message())

    def broadcast_device_snapshot(self, addr):
        self.broadcast({"type": "mapping", "addr": addr, "mapping": self.mapping.get_for(addr)})
        self.broadcast({"type": "pointer", "addr": addr, "config": self.pointer_cfg.get_for(addr)})

    def _client_loop(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle_msg(conn, json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle_msg(self, conn, msg):
        t = msg.get("type")
        if t == "set_mapping":
            addr = msg.get("addr")
            name, kind, code = msg.get("input"), msg.get("kind"), msg.get("code", "")
            if addr and name in INPUT_NAMES and kind in ("key", "button", "none") and (kind == "none" or code in e.ecodes):
                self.mapping.set(addr, name, kind, code)
                with REGISTRY_LOCK:
                    wm = REGISTRY.get(addr)
                if wm:
                    wm.rebuild_uinput()
                self.broadcast({"type": "mapping", "addr": addr, "mapping": self.mapping.get_for(addr)})
        elif t == "set_pointer":
            addr = msg.get("addr")
            if not addr:
                return
            kwargs = {}
            if "enabled" in msg:
                kwargs["enabled"] = bool(msg["enabled"])
            if "invert_x" in msg:
                kwargs["invert_x"] = bool(msg["invert_x"])
            if "invert_y" in msg:
                kwargs["invert_y"] = bool(msg["invert_y"])
            self.pointer_cfg.update(addr, **kwargs)
            if "enabled" in kwargs:
                with REGISTRY_LOCK:
                    wm = REGISTRY.get(addr)
                if wm:
                    wm.set_pointer_enabled(kwargs["enabled"])
            self.broadcast({"type": "pointer", "addr": addr, "config": self.pointer_cfg.get_for(addr)})
        elif t == "get_snapshot":
            self._send_snapshot(conn)
        elif t == "get_adapters":
            self._send_adapters(conn)

    def _send_adapters(self, conn):
        adapters = list_adapters()
        with REGISTRY_LOCK:
            by_local = {}
            for addr, wm in REGISTRY.items():
                by_local.setdefault(wm.local_addr, []).append(addr)
        result = [
            {"hci": name, "addr": bd_addr, "devices": by_local.get(bd_addr, [])}
            for name, bd_addr in adapters
        ]
        self._send(conn, {"type": "adapters", "adapters": result})

    def _send(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def broadcast(self, obj):
        line = (json.dumps(obj) + "\n").encode()
        with self.clients_lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(line)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)


class Wiimote:
    def __init__(self, addr, local_addr, mapping, pointer_cfg, ipc):
        self.addr = addr
        self.local_addr = local_addr
        self.mapping = mapping
        self.pointer_cfg = pointer_cfg
        self.ipc = ipc
        self.ctrl = None
        self.intr = None
        self.ui_pad = None
        self.ui_kbd = None
        self.ui_ptr = None
        self.ptr_touching = False
        self.ui_lock = threading.RLock()
        self.pressed = {name: False for name in INPUT_NAMES}
        self.last_report_time = time.time()

    def connect(self):
        self.ctrl = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        self.ctrl.bind((self.local_addr, 0))
        self.ctrl.settimeout(5)
        self.ctrl.connect((self.addr, CTRL_PSM))

        self.intr = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        self.intr.bind((self.local_addr, 0))
        self.intr.settimeout(5)
        self.intr.connect((self.addr, INTR_PSM))

        # Light this remote's own player LED (1-4) rather than always LED1.
        slot = assign_player_slot(self.addr)
        self.intr.send(bytes([0xA2, 0x11, 0x10 << slot]))

        want_pointer = self.pointer_cfg.get_for(self.addr)["enabled"]
        pointer_on = self._enable_ir_pointer() if want_pointer else False
        if not pointer_on:
            self.intr.send(bytes([0xA2, 0x12, 0x04, 0x31]))  # buttons + accel
        if want_pointer and not pointer_on:
            log(f"{self.addr}: could not confirm IR camera enable, staying in buttons+accel mode")
            self.pointer_cfg.update(self.addr, enabled=False)

        self.intr.settimeout(1.0)
        self.rebuild_uinput()
        if pointer_on:
            self._build_pointer_device()
        log(f"{self.addr}: connected (pointer {'on' if pointer_on else 'off'})")

    def _write_register(self, addr, data):
        """Output Report 0x16: write `data` (<=16 bytes) to a Wiimote
        control register. addr is the documented 3-byte register address
        (e.g. 0xb00030); bit 0x04 of the flags byte selects register space
        over EEPROM space."""
        payload = bytearray(16)
        payload[:len(data)] = data
        pkt = bytes([0xA2, 0x16, 0x04,
                     (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
                     len(data)]) + bytes(payload)
        self.intr.send(pkt)
        time.sleep(0.05)

    def _enable_ir_camera(self):
        self.intr.send(bytes([0xA2, 0x13, 0x04]))  # IR camera enable
        time.sleep(0.05)
        self.intr.send(bytes([0xA2, 0x1A, 0x04]))  # IR camera enable 2
        time.sleep(0.05)
        self._write_register(0xB00030, bytes([0x08]))
        self._write_register(0xB00000, IR_SENSITIVITY_BLOCK1)
        self._write_register(0xB0001A, IR_SENSITIVITY_BLOCK2)
        self._write_register(0xB00033, bytes([IR_MODE_EXTENDED]))
        self._write_register(0xB00030, bytes([0x08]))

    def _ir_camera_confirmed_on(self, timeout=0.5):
        """Ask for a Status Report (0x20) and check its IR-enabled flag
        (bit 0x08 of the flags byte) rather than trusting the write
        sequence blindly."""
        self.intr.send(bytes([0xA2, 0x15, 0x00]))
        old_timeout = self.intr.gettimeout()
        self.intr.settimeout(0.2)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                try:
                    data = self.intr.recv(64)
                except socket.timeout:
                    continue
                if len(data) >= 5 and data[0] == 0xA1 and data[1] == 0x20:
                    return bool(data[4] & 0x08)
        finally:
            self.intr.settimeout(old_timeout)
        return False

    def _enable_ir_pointer(self, attempts=3):
        """WiiBrew's own docs describe the IR camera's hardware init as
        landing in one of several states essentially at random and say to
        just repeat it -- so retry automatically instead of leaving the
        remote in a half-enabled state the user has to notice and fix by
        re-toggling the checkbox."""
        for attempt in range(1, attempts + 1):
            self._enable_ir_camera()
            self.intr.send(bytes([0xA2, 0x12, 0x04, 0x33]))
            if self._ir_camera_confirmed_on():
                return True
            log(f"{self.addr}: IR camera enable attempt {attempt}/{attempts} unconfirmed, retrying")
        return False

    def _build_pointer_device(self):
        with self.ui_lock:
            if self.ui_ptr:
                return
            # libinput only drives the on-screen cursor for a device that
            # fits one of its recognized absolute-pointer shapes (touch
            # screen / tablet tool); a bare ABS_X/Y device is invisible to
            # it. INPUT_PROP_DIRECT + BTN_TOUCH makes it a single-touch
            # touchscreen, and resolution must be non-zero or libinput
            # treats the device as "buggy" and ignores it. We hold BTN_TOUCH
            # asserted continuously (see _handle_ir) so it tracks like a
            # light gun rather than only moving while "touched".
            self.ui_ptr = UInput(
                {
                    e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH],
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, 0, 65535, 0, 0, 32)),
                        (e.ABS_Y, AbsInfo(0, 0, 65535, 0, 0, 32)),
                    ],
                },
                name=f"Wii Remote Pointer ({self.addr})",
                vendor=0x057E, product=0x0306, version=1,
                input_props=[e.INPUT_PROP_DIRECT],
            )
            self.ptr_touching = False

    def set_pointer_enabled(self, enabled):
        with self.ui_lock:
            if enabled and not self.ui_ptr:
                try:
                    confirmed = self._enable_ir_pointer()
                except OSError as ex:
                    log(f"{self.addr}: failed to enable pointer - {ex}")
                    return
                if not confirmed:
                    log(f"{self.addr}: pointer enable failed after retries")
                    try:
                        self.intr.send(bytes([0xA2, 0x12, 0x04, 0x31]))
                    except OSError:
                        pass
                    self.pointer_cfg.update(self.addr, enabled=False)
                    return
                self._build_pointer_device()
                log(f"{self.addr}: pointer enabled")
            elif not enabled and self.ui_ptr:
                try:
                    self.intr.send(bytes([0xA2, 0x12, 0x04, 0x31]))
                except OSError:
                    pass
                self.ui_ptr.close()
                self.ui_ptr = None
                log(f"{self.addr}: pointer disabled")

    def rebuild_uinput(self):
        btn_codes = set()
        for spec in self.mapping.get_for(self.addr).values():
            if spec.get("kind") == "button":
                code_id = e.ecodes.get(spec.get("code"))
                if code_id is not None:
                    btn_codes.add(code_id)
        pad_capabilities = {
            e.EV_KEY: sorted(btn_codes),
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, -128, 127, 0, 4, 0)),
                (e.ABS_Y, AbsInfo(0, -128, 127, 0, 4, 0)),
                (e.ABS_Z, AbsInfo(0, -128, 127, 0, 4, 0)),
            ],
        }
        with self.ui_lock:
            if self.ui_pad:
                self.ui_pad.close()
            self.ui_pad = UInput(
                pad_capabilities,
                name=f"Wii Remote ({self.addr})",
                vendor=0x057E, product=0x0306, version=1,
            )
            if self.ui_kbd is None:
                self.ui_kbd = UInput(
                    {e.EV_KEY: FULL_KEYBOARD_CODES},
                    name=f"Wii Remote Keyboard ({self.addr})",
                    vendor=0x057E, product=0x0306, version=1,
                )

    def _apply_buttons(self, b0, b1):
        """Called with self.ui_lock held."""
        changed = {}
        spec_map = self.mapping.get_for(self.addr)
        for name in INPUT_NAMES:
            byte_idx, bit = INPUT_BITS[name]
            val = bool((b0 if byte_idx == 0 else b1) & bit)
            if val != self.pressed[name]:
                changed[name] = val
        for name, val in changed.items():
            spec = spec_map.get(name)
            if not spec or spec.get("kind") == "none":
                continue
            code_id = e.ecodes.get(spec.get("code"))
            if code_id is None:
                continue
            if spec["kind"] == "key":
                self.ui_kbd.write(e.EV_KEY, code_id, 1 if val else 0)
                self.ui_kbd.syn()
            else:
                self.ui_pad.write(e.EV_KEY, code_id, 1 if val else 0)
        # B is the trigger finger's button, so it also always left-clicks
        # the pointer device when the IR pointer is active -- same as the
        # real Wii's UI convention -- independent of B's own mapping above.
        if "B" in changed and self.ui_ptr:
            self.ui_ptr.write(e.EV_KEY, e.BTN_LEFT, 1 if changed["B"] else 0)
            self.ui_ptr.syn()
        if changed:
            self.pressed.update(changed)
            self.ipc.broadcast({"type": "state", "addr": self.addr, "buttons": dict(self.pressed)})

    def _handle_ir(self, ir_bytes):
        """Called with self.ui_lock held. Extended-mode IR data: 4 objects
        x 3 bytes (X<7:0>, Y<7:0>, Y<9:8>|X<9:8>|Size<3:0>); an all-0xFF
        triple means that tracking slot is empty."""
        points = []
        for i in range(4):
            b0, b1, b2 = ir_bytes[3 * i], ir_bytes[3 * i + 1], ir_bytes[3 * i + 2]
            if b0 == 0xFF and b1 == 0xFF and b2 == 0xFF:
                continue
            x = b0 | (((b2 >> 4) & 0x03) << 8)
            y = b1 | (((b2 >> 6) & 0x03) << 8)
            points.append((x, y))
        if not self.ui_ptr:
            return
        if not points:
            if self.ptr_touching:
                self.ui_ptr.write(e.EV_KEY, e.BTN_TOUCH, 0)
                self.ui_ptr.syn()
                self.ptr_touching = False
            return
        if not self.ptr_touching:
            self.ui_ptr.write(e.EV_KEY, e.BTN_TOUCH, 1)
            self.ptr_touching = True

        if len(points) >= 2:
            # Two brightest tracked points = the two ends of the sensor
            # bar / IR source pair; point straight at their midpoint.
            mx = (points[0][0] + points[1][0]) / 2.0
            my = (points[0][1] + points[1][1]) / 2.0
        else:
            mx, my = points[0]

        cfg = self.pointer_cfg.get_for(self.addr)
        nx = mx / 1023.0
        ny = my / 767.0
        if cfg.get("invert_x", True):
            nx = 1.0 - nx
        if cfg.get("invert_y", False):
            ny = 1.0 - ny
        nx = min(max(nx, 0.0), 1.0)
        ny = min(max(ny, 0.0), 1.0)

        self.ui_ptr.write(e.EV_ABS, e.ABS_X, int(nx * 65535))
        self.ui_ptr.write(e.EV_ABS, e.ABS_Y, int(ny * 65535))
        self.ui_ptr.syn()

    def _handle_report(self, data):
        if len(data) < 4 or data[0] != 0xA1:
            return
        report_id = data[1]
        with self.ui_lock:
            if report_id == 0x31 and len(data) >= 7:
                b0, b1, ax, ay, az = data[2], data[3], data[4], data[5], data[6]
                self._apply_buttons(b0, b1)
                self.ui_pad.write(e.EV_ABS, e.ABS_X, ax - 128)
                self.ui_pad.write(e.EV_ABS, e.ABS_Y, ay - 128)
                self.ui_pad.write(e.EV_ABS, e.ABS_Z, az - 128)
                self.ui_pad.syn()
            elif report_id == 0x30 and len(data) >= 4:
                self._apply_buttons(data[2], data[3])
                self.ui_pad.syn()
            elif report_id == 0x33 and len(data) >= 19:
                b0, b1, ax, ay, az = data[2], data[3], data[4], data[5], data[6]
                self._apply_buttons(b0, b1)
                self.ui_pad.write(e.EV_ABS, e.ABS_X, ax - 128)
                self.ui_pad.write(e.EV_ABS, e.ABS_Y, ay - 128)
                self.ui_pad.write(e.EV_ABS, e.ABS_Z, az - 128)
                self.ui_pad.syn()
                self._handle_ir(data[7:19])
            # other report IDs (status/ack/extension) ignored for now

    def run(self):
        try:
            self.connect()
        except OSError as ex:
            log(f"{self.addr}: connect failed - {ex}")
            return

        with REGISTRY_LOCK:
            REGISTRY[self.addr] = self
        self.last_report_time = time.time()
        self.ipc.broadcast_device_snapshot(self.addr)
        self.ipc.broadcast(devices_message())

        try:
            while True:
                try:
                    data = self.intr.recv(64)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.last_report_time = time.time()
                self._handle_report(data)
        except OSError as ex:
            log(f"{self.addr}: link error - {ex}")
        finally:
            with REGISTRY_LOCK:
                REGISTRY.pop(self.addr, None)
            release_player_slot(self.addr)
            self.ipc.broadcast(devices_message())
            self.close()
            log(f"{self.addr}: disconnected")

    def force_disconnect(self):
        """Called by the stale-connection watchdog. Shutting down the
        sockets unblocks this Wiimote's own recv() loop, which then runs
        the normal disconnect cleanup in run()'s finally block."""
        for s in (self.ctrl, self.intr):
            try:
                if s:
                    s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        for s in (self.ctrl, self.intr):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        with self.ui_lock:
            if self.ui_pad:
                self.ui_pad.close()
            if self.ui_kbd:
                self.ui_kbd.close()
            if self.ui_ptr:
                self.ui_ptr.close()


def watchdog():
    """A physically dead remote (dead battery, out of range) doesn't
    always make the kernel notice quickly -- Bluetooth's own supervision
    timeout can take tens of seconds. Since every connected remote streams
    reports continuously, no report for STALE_TIMEOUT seconds means the
    link is actually gone; force it closed rather than waiting."""
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        now = time.time()
        with REGISTRY_LOCK:
            stale = [wm for wm in REGISTRY.values() if now - wm.last_report_time > STALE_TIMEOUT]
        for wm in stale:
            log(f"{wm.addr}: no data for {STALE_TIMEOUT:.0f}s, treating as disconnected")
            wm.force_disconnect()


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    mapping = Mapping(MAPPING_PATH)
    pointer_cfg = PointerConfig(POINTER_PATH)
    ipc = IPCServer(SOCK_PATH, mapping, pointer_cfg)
    ipc.start()
    threading.Thread(target=watchdog, daemon=True).start()

    active = {}  # addr -> thread
    log("Wiimote bridge starting. Hold SYNC (or 1+2) on the remote to connect it.")
    while True:
        for addr in list(active):
            if not active[addr].is_alive():
                del active[addr]

        adapters = list_adapters()
        if not adapters:
            log("no local Bluetooth adapters found")
        for hci_name, local_addr in adapters:
            for addr in discover_candidates(hci_name):
                if addr in active:
                    continue
                log(f"candidate found: {addr} (via {hci_name})")
                wm = Wiimote(addr, local_addr, mapping, pointer_cfg, ipc)
                t = threading.Thread(target=wm.run, daemon=True)
                active[addr] = t
                t.start()
                time.sleep(RECONNECT_BACKOFF)  # avoid slamming a second inquiry mid-connect

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
