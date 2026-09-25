#!/usr/bin/env python3
"""
Live status + remapping GUI for wiimote_bridge.py.

Connects to the daemon's local Unix socket (no root needed for the GUI
itself). Shows up to 4 connected remotes side by side, each in its own
compact column with live button state and independent remapping -- every
remote can have its own button -> gamepad/keyboard mapping and its own IR
pointer settings, since the daemon tracks mapping per Bluetooth address.
Changes take effect live -- the daemon rebuilds its virtual input device
for that remote immediately.
"""

import filecmp
import json
import os
import queue
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import wiimote_bridge as bridge
from wiimote_bridge import INPUT_NAMES, SOCK_PATH

MAX_SLOTS = 4

def _stage_helper():
    """Copy the bundled raw-HCI helper out of the AppImage to the user's
    cache and point the bridge at it. The one-time grant applies the
    scan capability to THIS copy: root can't read files inside the AppImage's
    FUSE mount, and system binaries (hcitool etc.) may be absent or, on
    immutable distros, impossible to modify. A kernel drops file capabilities
    whenever a file is written to, so a process running as the user cannot
    tamper with the granted copy while keeping the privilege.
    Returns the staged path, or None (deb install / source checkout: the
    bridge finds its own)."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiimote-hci")
    if not os.path.exists(src):
        return None
    dest_dir = os.path.expanduser("~/.cache/wii-control/bin")
    dest = os.path.join(dest_dir, "wiimote-hci")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        if not (os.path.exists(dest) and filecmp.cmp(src, dest, shallow=False)):
            tmp = dest + ".new"
            shutil.copy2(src, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)  # a new file: any old grant is gone, so it must be re-granted
    except OSError:
        return None
    os.environ["WIIMOTE_HCI"] = dest
    return dest


HELPER_PATH = _stage_helper()


def _stage_setup_script():
    """Root can't read files inside the AppImage's FUSE mount (it's
    mounted for the launching user only), so sudo/pkexec on the script's
    in-mount path fails with "Permission denied". Copy it to an ordinary
    location first and point everything at the copy."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup-permissions.sh")
    dest_dir = os.path.expanduser("~/.cache/wii-control")
    dest = os.path.join(dest_dir, "setup-permissions.sh")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return src


SETUP_SCRIPT = _stage_setup_script()


def _daemon_reachable():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        s.close()
        return True
    except OSError:
        return False


def stop_leftover_daemon():
    """A standalone daemon from an older version (or started by hand) would
    hold the adapters and the socket, so this window couldn't own them. If
    one is answering on the socket and belongs to this user, stop it."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        pid, uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        s.close()
    except OSError:
        return
    if uid != os.getuid() or pid == os.getpid():
        return
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"wiimote_bridge" not in f.read():
                return
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):  # up to 3s for it to let go of the socket
        time.sleep(0.1)
        if not os.path.exists(SOCK_PATH) or not _daemon_reachable():
            return


def request_permission_grant():
    """Runs the one-time setup through pkexec, which is the desktop's own
    native password dialog (drawn by the system, not by this app).
    Returns (ok, message)."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return False, "pkexec (polkit) isn't installed, so there is no system password dialog to use."
    try:
        r = subprocess.run([pkexec, "bash", SETUP_SCRIPT] + ([HELPER_PATH] if HELPER_PATH else []),
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the password dialog."
    except OSError as ex:
        return False, str(ex)
    detail = (r.stderr or r.stdout).strip()[-300:]
    if r.returncode == 0:
        return True, ""
    if r.returncode == 126:
        return False, "Cancelled -- the password dialog was dismissed."
    if r.returncode == 127:
        return False, ("Couldn't get authorization. Either the password was wrong, or this desktop "
                       "session has no polkit authentication agent running, so no system dialog "
                       "could appear." + (f"\n({detail})" if detail else ""))
    return False, f"Setup failed (exit {r.returncode}): {detail}"


SHORT_NAMES = {
    "A": "A", "B": "B", "ONE": "1", "TWO": "2", "MINUS": "-", "PLUS": "+",
    "HOME": "Home", "DPAD_UP": "Up", "DPAD_DOWN": "Down",
    "DPAD_LEFT": "Left", "DPAD_RIGHT": "Right",
}

GAMEPAD_BUTTONS = [
    "BTN_A", "BTN_B", "BTN_X", "BTN_Y", "BTN_TL", "BTN_TR", "BTN_TL2", "BTN_TR2",
    "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR",
    "BTN_DPAD_UP", "BTN_DPAD_DOWN", "BTN_DPAD_LEFT", "BTN_DPAD_RIGHT",
]

# Analog stick directions: the button held = the stick pushed fully that way.
STICK_CHOICES = [
    "LSTICK_UP", "LSTICK_DOWN", "LSTICK_LEFT", "LSTICK_RIGHT",
    "RSTICK_UP", "RSTICK_DOWN", "RSTICK_LEFT", "RSTICK_RIGHT",
]

KEYSYM_TO_EVDEV = {
    "space": "KEY_SPACE", "Return": "KEY_ENTER", "Escape": "KEY_ESC",
    "Tab": "KEY_TAB", "BackSpace": "KEY_BACKSPACE",
    "Up": "KEY_UP", "Down": "KEY_DOWN", "Left": "KEY_LEFT", "Right": "KEY_RIGHT",
    "Shift_L": "KEY_LEFTSHIFT", "Shift_R": "KEY_RIGHTSHIFT",
    "Control_L": "KEY_LEFTCTRL", "Control_R": "KEY_RIGHTCTRL",
    "Alt_L": "KEY_LEFTALT", "Alt_R": "KEY_RIGHTALT",
    "comma": "KEY_COMMA", "period": "KEY_DOT", "minus": "KEY_MINUS", "equal": "KEY_EQUAL",
}


def keysym_to_code(keysym):
    if keysym in KEYSYM_TO_EVDEV:
        return KEYSYM_TO_EVDEV[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit():
        return f"KEY_{keysym.upper()}"
    if len(keysym) == 1:
        ch = keysym.upper()
        if ch.isalpha() or ch.isdigit():
            return f"KEY_{ch}"
    return None


def fmt_mapping(spec):
    if not spec or spec.get("kind") == "none":
        return "-"
    code = spec.get("code", "")
    return code.replace("BTN_", "").replace("KEY_", "").replace("STICK_", "Stk ").replace("_", " ").title()


class DiagnosticsDialog(tk.Toplevel):
    """Everything needed to diagnose a problem in one selectable box, with
    a copy button -- so nobody has to go looking for log files."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Diagnostics")
        log_path = os.path.expanduser("~/.cache/wii-control/app.log")
        try:
            with open(log_path) as f:
                tail = "".join(f.readlines()[-150:])
        except OSError:
            tail = "(no log file yet)"
        self.text_value = bridge.diagnostics() + "\n\n--- app log (last 150 lines) ---\n" + tail

        box = tk.Text(self, width=100, height=34, wrap="none")
        box.insert("1.0", self.text_value)
        box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        row = tk.Frame(self)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.note = tk.Label(row, text="", fg="#2e7d32")
        tk.Button(row, text="Copy to clipboard", command=self._copy).pack(side="left")
        self.note.pack(side="left", padx=10)
        tk.Button(row, text="Close", command=self.destroy).pack(side="right")

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text_value)
        self.update()
        self.note.config(text="Copied. Keep this app open while you paste.")


class RemapDialog(tk.Toplevel):
    def __init__(self, parent, label, on_set):
        super().__init__(parent)
        self.title(f"Remap {label}")
        self.resizable(False, False)
        self.on_set = on_set

        tk.Label(self, text=f"Button: {label}", font=("", 11, "bold")).pack(padx=16, pady=(14, 4))
        tk.Label(self, text="Press any keyboard key to bind it\n(Esc cancels)", justify="center").pack(padx=16, pady=4)

        tk.Frame(self, height=1, bg="#888").pack(fill="x", padx=16, pady=10)

        tk.Label(self, text="...or a gamepad button / analog stick\ndirection (held = stick fully pushed):", justify="center").pack(padx=16)
        self.combo = ttk.Combobox(self, values=GAMEPAD_BUTTONS + STICK_CHOICES, state="readonly")
        self.combo.pack(padx=16, pady=(4, 10))
        self.combo.bind("<<ComboboxSelected>>", self._on_combo)

        btnrow = tk.Frame(self)
        btnrow.pack(pady=(0, 14))
        tk.Button(btnrow, text="Unmap", command=self._on_unmap).pack(side="left", padx=6)
        tk.Button(btnrow, text="Cancel", command=self.destroy).pack(side="left", padx=6)

        self.bind("<Key>", self._on_key)
        self.grab_set()
        self.focus_set()

    def _on_key(self, event):
        if event.keysym == "Escape":
            self.destroy()
            return
        code = keysym_to_code(event.keysym)
        if code:
            self.on_set("key", code)
            self.destroy()

    def _on_combo(self, _event):
        choice = self.combo.get()
        self.on_set("axis" if choice in STICK_CHOICES else "button", choice)
        self.destroy()

    def _on_unmap(self):
        self.on_set("none", "")
        self.destroy()


class DeviceColumn:
    """One compact panel bound to a Bluetooth address once a remote occupies
    this slot; shows a placeholder while the slot is empty."""

    def __init__(self, parent, app, col_index):
        self.app = app
        self.addr = None
        self.mapping = {}

        self.frame = tk.LabelFrame(parent, text=f"Slot {col_index + 1}: empty", padx=6, pady=4)
        self.frame.grid(row=0, column=col_index, padx=5, pady=5, sticky="n")

        ptr_row = tk.Frame(self.frame)
        ptr_row.pack(fill="x", pady=(0, 4))
        self.ptr_var = tk.BooleanVar(value=False)
        self.joy_var = tk.BooleanVar(value=True)
        self.ix_var = tk.BooleanVar(value=True)
        self.iy_var = tk.BooleanVar(value=False)
        self._suppress = True
        self.ptr_checks = [
            tk.Checkbutton(ptr_row, text="Show as joystick", variable=self.joy_var, command=self._send_pointer),
            tk.Checkbutton(ptr_row, text="Pointer", variable=self.ptr_var, command=self._send_pointer),
            tk.Checkbutton(ptr_row, text="Flip X", variable=self.ix_var, command=self._send_pointer),
            tk.Checkbutton(ptr_row, text="Flip Y", variable=self.iy_var, command=self._send_pointer),
        ]
        for c in self.ptr_checks:
            c.config(state="disabled")
        self.ptr_checks[0].pack(side="top", anchor="w")
        self.ptr_checks[1].pack(side="top", anchor="w")
        self.ptr_checks[2].pack(side="left")
        self.ptr_checks[3].pack(side="left")
        self._suppress = False

        self.row_buttons = {}
        for name in INPUT_NAMES:
            btn = tk.Button(
                self.frame, text=f"{SHORT_NAMES[name]}: -", width=14, anchor="w",
                bg="#444444", fg="white", relief="flat",
                command=lambda n=name: self._open_remap(n),
            )
            btn.pack(fill="x", pady=1)
            self.row_buttons[name] = btn

    def bind_addr(self, addr):
        if self.addr == addr:
            return
        self.addr = addr
        short = addr[-8:] if addr else "empty"
        self.frame.config(text=f"Remote: {short}" if addr else "Slot: empty")
        state = "normal" if addr else "disabled"
        for w in self.row_buttons.values():
            w.config(state=state, bg="#444444")
        for c in self.ptr_checks:
            c.config(state=state)

    def set_mapping(self, mapping):
        self.mapping = mapping
        for name, btn in self.row_buttons.items():
            spec = mapping.get(name, {})
            btn.config(text=f"{SHORT_NAMES[name]}: {fmt_mapping(spec)}")

    def set_pointer_config(self, cfg):
        self._suppress = True
        self.joy_var.set(cfg.get("joystick", True))
        self.ptr_var.set(cfg.get("enabled", False))
        self.ix_var.set(cfg.get("invert_x", True))
        self.iy_var.set(cfg.get("invert_y", False))
        self._suppress = False

    def set_button_state(self, name, pressed):
        btn = self.row_buttons.get(name)
        if btn:
            btn.config(bg="#4CAF50" if pressed else "#444444")

    def _send_pointer(self):
        if self._suppress or not self.addr:
            return
        self.app.send({
            "type": "set_pointer", "addr": self.addr,
            "enabled": self.ptr_var.get(), "joystick": self.joy_var.get(), "invert_x": self.ix_var.get(), "invert_y": self.iy_var.get(),
        })

    def _open_remap(self, name):
        if not self.addr:
            return
        RemapDialog(self.frame, f"{name} ({self.addr[-5:]})", lambda kind, code: self.app.send(
            {"type": "set_mapping", "addr": self.addr, "input": name, "kind": kind, "code": code}
        ))


class GuiApp:
    def __init__(self, root):
        self.root = root
        self.sock = None
        self.msg_queue = queue.Queue()
        self.slots = []          # list[DeviceColumn], fixed size MAX_SLOTS
        self.slot_of_addr = {}   # addr -> slot index
        self.mappings = {}       # addr -> latest mapping message
        self.pointers = {}       # addr -> latest pointer config

        self.bridge_thread = None

        self._build_ui()
        stop_leftover_daemon()
        ok, msg = self._try_start_bridge()
        if not ok:
            self._show_setup(msg)
        threading.Thread(target=self._socket_worker, daemon=True).start()
        self.root.after(50, self._poll_queue)

    def _build_ui(self):
        self.root.title(f"WiiBridge v{bridge.__version__}")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        controllers_tab = tk.Frame(notebook)
        notebook.add(controllers_tab, text="Controllers")

        self.setup_frame = tk.LabelFrame(controllers_tab, text="One-time setup needed", padx=10, pady=8)
        tk.Label(
            self.setup_frame, justify="left", wraplength=620,
            text="WiiBridge needs permission to scan for remotes over "
                 "Bluetooth and to create virtual controllers. Your computer "
                 "restricts both by default. Click the button to grant it -- "
                 "your system will ask for your password in its own dialog. "
                 "This is needed once per computer; after that the app starts "
                 "with no prompts.",
        ).pack(anchor="w")
        self.grant_btn = tk.Button(self.setup_frame, text="Grant permission...", command=self._on_grant)
        self.grant_btn.pack(anchor="w", pady=(8, 0))
        self.setup_detail = tk.Label(self.setup_frame, text="", justify="left", wraplength=620, fg="#555")
        self.setup_msg = tk.Label(self.setup_frame, text="", justify="left", wraplength=620, fg="#c62828")

        self.status_lbl = tk.Label(controllers_tab, text="Connecting to daemon...", anchor="w")
        self.status_lbl.pack(fill="x", padx=8, pady=(8, 2))
        self.scan_lbl = tk.Label(controllers_tab, text="", anchor="w", justify="left", wraplength=620, fg="#c62828")

        cols = tk.Frame(controllers_tab)
        cols.pack(padx=5, pady=(0, 8))
        for i in range(MAX_SLOTS):
            self.slots.append(DeviceColumn(cols, self, i))

        settings_tab = tk.Frame(notebook)
        notebook.add(settings_tab, text="Settings")
        self._build_settings_tab(settings_tab)
        notebook.bind("<<NotebookTabChanged>>", lambda e: self._maybe_refresh_adapters(notebook, settings_tab))

    def _build_settings_tab(self, parent):
        tk.Label(parent, text="Bluetooth Adapters", font=("", 11, "bold")).pack(anchor="w", padx=10, pady=(12, 2))
        tk.Label(
            parent, fg="#555", justify="left", wraplength=520,
            text="Remotes are automatically spread across every adapter listed here. "
                 "Plug in another USB Bluetooth adapter and press Search to pick it up.",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        self.adapters_tree = ttk.Treeview(parent, columns=("addr", "count", "devices"), show="tree headings", height=6)
        self.adapters_tree.heading("#0", text="Adapter")
        self.adapters_tree.column("#0", width=80)
        self.adapters_tree.heading("addr", text="Address")
        self.adapters_tree.heading("count", text="Connected")
        self.adapters_tree.heading("devices", text="Remote Addresses")
        self.adapters_tree.column("addr", width=140)
        self.adapters_tree.column("count", width=70, anchor="center")
        self.adapters_tree.column("devices", width=280)
        self.adapters_tree.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(parent, justify="left", anchor="w", wraplength=520,
                 text="Show as joystick: untick to make the remote act only as a keyboard "
                      "(and pointer), so games never see a gamepad. Gamepad-button "
                      "mappings do nothing in that mode - use keys.\n"
                      "Pointer: use the remote's IR camera to move the mouse. "
                      "Flip X / Flip Y: reverse the horizontal / vertical pointer direction "
                      "if it moves the wrong way (Flip X is on by default because the camera "
                      "sees the IR source mirrored).").pack(anchor="w", padx=10, pady=(0, 8))

        tk.Button(parent, text="Search for Adapters", command=self._refresh_adapters).pack(anchor="w", padx=10, pady=(0, 10))

        tk.Label(parent, text=f"Version {bridge.__version__}", anchor="w").pack(anchor="w", padx=10, pady=(0, 6))
        log_path = os.path.expanduser("~/.cache/wii-control/app.log")
        tk.Label(parent, text="Log file (what the service is doing, and any errors):", anchor="w").pack(anchor="w", padx=10)
        tk.Label(parent, text=log_path, anchor="w", fg="#555").pack(anchor="w", padx=10)

        def show_diagnostics():
            DiagnosticsDialog(self.root)
        tk.Button(parent, text="Diagnostics (copy this when reporting a problem)...",
                  command=show_diagnostics).pack(anchor="w", padx=10, pady=(0, 6))

        def open_log():
            try:
                subprocess.Popen(["xdg-open", log_path])
            except OSError:
                pass
        tk.Button(parent, text="Open log", command=open_log).pack(anchor="w", padx=10, pady=(4, 10))

    def _maybe_refresh_adapters(self, notebook, settings_tab):
        if notebook.select() == str(settings_tab):
            self._refresh_adapters()

    def _refresh_adapters(self):
        self.send({"type": "get_adapters"})

    def _update_adapters(self, adapters):
        self.adapters_tree.delete(*self.adapters_tree.get_children())
        for a in adapters:
            devices = a.get("devices", [])
            self.adapters_tree.insert(
                "", "end", text=a.get("hci", "?"), iid=a.get("hci", "?"),
                values=(a.get("addr", ""), len(devices), ", ".join(devices) or "-"),
            )

    def _socket_worker(self):
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK_PATH)
                self.sock = s
                self.msg_queue.put(("status", True))
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise OSError("daemon closed connection")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line.strip():
                            self.msg_queue.put(("msg", json.loads(line)))
            except (OSError, json.JSONDecodeError):
                self.sock = None
                self.msg_queue.put(("status", False))
                time.sleep(2)

    def _try_start_bridge(self):
        """Runs the bridge in this process (see bridge.serve) once every
        permission is in place. Returns (ok, why-not)."""
        missing = bridge.missing_permissions()
        if missing:
            return False, "Still missing: " + "; ".join(missing)
        if self.bridge_thread and self.bridge_thread.is_alive():
            return True, ""

        def run():
            try:
                bridge.serve()
            except BaseException as ex:  # SystemExit included: it must not vanish silently in a thread
                self.msg_queue.put(("bridge_failed", str(ex) or type(ex).__name__))

        self.bridge_thread = threading.Thread(target=run, daemon=True)
        self.bridge_thread.start()
        return True, ""

    def _show_setup(self, detail=""):
        self.setup_frame.pack(fill="x", padx=8, pady=(8, 2), before=self.status_lbl)
        if detail:
            self.setup_detail.config(text=detail)
            self.setup_detail.pack(anchor="w", pady=(6, 0))

    def _on_grant(self):
        self.grant_btn.config(state="disabled")
        self.setup_msg.config(text="Waiting for the system password dialog...", fg="#555")
        self.setup_msg.pack(anchor="w", pady=(6, 0))

        def work():
            ok, msg = request_permission_grant()
            if ok:
                ok, why = self._try_start_bridge()
                msg = "" if ok else f"Permission granted, but the service still can't start. {why}"
            self.msg_queue.put(("grant_result", (ok, msg)))

        threading.Thread(target=work, daemon=True).start()

    def _on_grant_result(self, ok, msg):
        if ok:
            self.setup_frame.pack_forget()
            return
        self.grant_btn.config(state="normal", text="Try again...")
        self.setup_msg.config(text=msg, fg="#c62828")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "status":
                    self._set_connected(payload)
                elif kind == "grant_result":
                    self._on_grant_result(*payload)
                elif kind == "bridge_failed":
                    self.scan_lbl.config(text=f"The service stopped: {payload}")
                    self.scan_lbl.pack(fill="x", padx=8, after=self.status_lbl)
                else:
                    self._handle_msg(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _set_connected(self, connected):
        self.status_lbl.config(
            text="Ready - hold the SYNC button on a remote to connect it" if connected else "Service not running",
            fg="#2e7d32" if connected else "#c62828",
        )
        if connected:
            self.setup_frame.pack_forget()
            self._refresh_adapters()

    def _handle_msg(self, msg):
        t = msg.get("type")
        if t == "devices":
            self._sync_slots(msg.get("addrs", []), msg.get("slots", {}))
        elif t == "mapping":
            self.mappings[msg.get("addr")] = msg.get("mapping", {})
            col = self._slot_for(msg.get("addr"))
            if col:
                col.set_mapping(self.mappings[msg.get("addr")])
        elif t == "pointer":
            self.pointers[msg.get("addr")] = msg.get("config", {})
            col = self._slot_for(msg.get("addr"))
            if col:
                col.set_pointer_config(self.pointers[msg.get("addr")])
        elif t == "state":
            col = self._slot_for(msg.get("addr"))
            if col:
                for name, pressed in msg.get("buttons", {}).items():
                    col.set_button_state(name, pressed)
        elif t == "scan":
            err = msg.get("error", "")
            if err:
                self.scan_lbl.config(text=f"Bluetooth scanning problem: {err}")
                self.scan_lbl.pack(fill="x", padx=8, after=self.status_lbl)
                self.status_lbl.config(text="Not scanning for remotes", fg="#c62828")
            else:
                self.scan_lbl.pack_forget()
                self._set_connected(True)
        elif t == "adapters":
            self._update_adapters(msg.get("adapters", []))

    def _slot_for(self, addr):
        idx = self.slot_of_addr.get(addr)
        return self.slots[idx] if idx is not None else None

    def _sync_slots(self, addrs, slot_map):
        """Column assignment always mirrors the daemon's own player-slot
        assignment (same index it uses for the remote's physical LED) --
        never computed independently here, so the two can't disagree."""
        addrs = set(addrs)
        for addr in list(self.slot_of_addr):
            if addr not in addrs:
                idx = self.slot_of_addr.pop(addr)
                self.slots[idx].bind_addr(None)
        for addr in addrs:
            idx = slot_map.get(addr)
            if idx is None or not (0 <= idx < MAX_SLOTS):
                continue
            if self.slot_of_addr.get(addr) == idx:
                continue
            # Clear any stale occupant of this slot (e.g. a ghost entry
            # from a connection that hadn't been recognized as dead yet).
            for other_addr, other_idx in list(self.slot_of_addr.items()):
                if other_idx == idx and other_addr != addr:
                    del self.slot_of_addr[other_addr]
                    self.slots[other_idx].bind_addr(None)
            self.slot_of_addr[addr] = idx
            self.slots[idx].bind_addr(addr)
            # Config messages may have arrived before the slot was bound.
            if addr in self.mappings:
                self.slots[idx].set_mapping(self.mappings[addr])
            if addr in self.pointers:
                self.slots[idx].set_pointer_config(self.pointers[addr])

    def send(self, obj):
        try:
            if self.sock:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass


def main():
    root = tk.Tk()
    if not bridge.acquire_single_instance():
        root.withdraw()
        messagebox.showinfo("Already running", "WiiBridge is already running.")
        return

    # Nothing else captures the service's output now that it runs in this
    # process, so keep it (and any GUI exception) somewhere findable.
    log_dir = os.path.expanduser("~/.cache/wii-control")
    os.makedirs(log_dir, exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(log_dir, "app.log"), "w", buffering=1)

    GuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
